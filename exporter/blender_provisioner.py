#!/usr/bin/env python3
"""Acquire the pinned Blender the exporter runs, without asking the player.

Friends of Duty does not ship Blender and does not require the player to
install it. This module downloads the exact pinned build from blender.org on
first use, verifies it against a SHA-256 that is part of the exporter build,
extracts it into a per-user cache (never into the application - see
fod_paths), proves it can actually do the job, and only then promotes it.

Why a *pin* rather than "whatever Blender is on this machine": the export is
only reproducible if the Blender that produced the reference package is the
Blender that runs. Measured on the same input, Blender 4.2 and 4.5 emit a
25,782,332-byte / 84,628-accessor player.glb versus 21,111,852 / 53,162 - and
blender.org's download button now serves 5.2 LTS, so "install Blender" would
in practice mean "install something we never tested". The pin removes the
whole question.

Order of resolution, first hit wins (Docs/EXPORTER_SINGLE_EXECUTABLE.md 4.4):

    explicit --blender path -> $FOD_BLENDER -> managed cache -> download

There is deliberately no PATH search and no /Applications scan when frozen:
silently adopting a player's Blender 5.2 is the failure this design exists to
prevent. A manually supplied Blender still has to pass the same probe.

Everything here is written so that an interrupted run is always safe to
repeat: the download resumes, a half-extracted tree is never promoted, and
two exporters racing on the same cache serialise on a lock rather than
corrupting it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import fod_paths

PIN_FILENAME = "blender_pin.json"

#: Emitted at most this often during a download, so a 400 MB transfer does
#: not produce tens of thousands of log lines.
PROGRESS_INTERVAL_SECONDS = 0.5
DOWNLOAD_CHUNK = 1 << 20
DOWNLOAD_ATTEMPTS = 4
CONNECT_TIMEOUT_SECONDS = 30
#: A stale lock older than this is assumed to belong to a process that died.
LOCK_STALE_SECONDS = 3600
PROBE_TIMEOUT_SECONDS = 180

#: blender.org answers a bare urllib User-Agent with 403.
USER_AGENT = "FriendsOfDutyExporter/1.0 (+https://github.com/anarqz)"

STAMP_NAME = ".fod_blender_ok"

ProgressFn = Callable[[str, int, int, str], None]
LogFn = Callable[[str], None]


class ProvisionError(RuntimeError):
    """Blender could not be made available. The message is player-facing."""


class ProvisionCancelled(RuntimeError):
    """The player cancelled while the download was running."""


# ------------------------------------------------------------------- the pin

def _pin_path() -> Path:
    """The checked-in pin, for a source checkout.

    A shipped build reads the pin from `build_stamp`, which the packaging
    step generates: a loose, editable pin file would be a supply-chain hole,
    since anything able to rewrite it could also choose the hash its
    substituted archive is checked against.
    """
    return fod_paths.EXPORTER_DIR.parent / "packaging" / PIN_FILENAME


_PIN_CACHE: dict | None = None


def pin() -> dict:
    global _PIN_CACHE
    if _PIN_CACHE is not None:
        return _PIN_CACHE
    try:
        import build_stamp  # type: ignore

        blender_pin = getattr(build_stamp, "BLENDER_PIN", None)
        if blender_pin:
            _PIN_CACHE = blender_pin
            return _PIN_CACHE
    except ImportError:
        pass
    path = _pin_path()
    if not path.is_file():
        raise ProvisionError(
            f"The Blender pin is missing ({path}). This build is incomplete; "
            "reinstall Friends of Duty."
        )
    _PIN_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _PIN_CACHE


def pinned_version() -> tuple[int, ...]:
    return tuple(pin()["version_tuple"])


def pinned_version_label() -> str:
    return str(pin()["version"])


def host_target() -> str:
    """Which pinned artifact this machine needs.

    Raises rather than guessing: Blender publishes no Linux ARM build at
    all, so a Steam Deck-like aarch64 host has nothing to download and must
    be told that plainly instead of failing later inside a step.
    """
    machine = (os.uname().machine if hasattr(os, "uname") else
               os.environ.get("PROCESSOR_ARCHITECTURE", "")).lower()
    arm = machine in {"arm64", "aarch64"}
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if arm else "macos-x64"
    if sys.platform.startswith("linux"):
        if arm:
            raise ProvisionError(
                "Blender publishes no Linux ARM build, so content cannot be "
                "extracted on this machine. Use the remote-import option to "
                "load a package you generated on a Windows, Intel/Apple "
                "silicon Mac, or x86-64 Linux machine."
            )
        return "linux-x64"
    raise ProvisionError(f"Unsupported platform for extraction: {sys.platform}")


def target_pin(target: str | None = None) -> dict:
    target = target or host_target()
    targets = pin()["targets"]
    if target not in targets:
        raise ProvisionError(f"No pinned Blender build for '{target}'.")
    entry = dict(targets[target])
    entry["target"] = target
    return entry


# ----------------------------------------------------------------- cache i/o

def install_root(version_label: str | None = None) -> Path:
    return fod_paths.blender_cache_root() / (
        version_label or pinned_version_label())


def executable_in(root: Path, entry: dict) -> Path:
    return root / entry["executable"]


def _stamp_path(root: Path) -> Path:
    return root / STAMP_NAME


def _read_stamp(root: Path) -> dict | None:
    try:
        return json.loads(_stamp_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cached_blender(entry: dict | None = None) -> Path | None:
    """A previously provisioned Blender, or None.

    Requires both the recorded archive hash to match the current pin and the
    executable to still exist: a stamp alone would happily survive a partly
    deleted cache directory.
    """
    entry = entry or target_pin()
    root = install_root()
    stamp = _read_stamp(root)
    if not stamp or stamp.get("archive_sha256") != entry["sha256"]:
        return None
    executable = executable_in(root, entry)
    return executable if executable.exists() else None


# --------------------------------------------------------------------- lock

class _CacheLock:
    """Cross-process guard around the cache directory.

    The game can start `--cli` while a windowed exporter is already open, and
    two concurrent 400 MB downloads into one directory is a corruption path,
    not a hypothetical. O_EXCL is the portable primitive here; a lock left
    behind by a killed process is reclaimed once it goes stale.
    """

    def __init__(self, root: Path, log: LogFn):
        self._path = root / ".fod_provision.lock"
        self._log = log
        self._fd: int | None = None

    def __enter__(self) -> "_CacheLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        announced = False
        while True:
            try:
                self._fd = os.open(
                    self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return self
            except FileExistsError:
                try:
                    age = time.time() - self._path.stat().st_mtime
                except OSError:
                    continue
                if age > LOCK_STALE_SECONDS:
                    self._log(
                        "  reclaiming a stale Blender download lock "
                        f"({age / 60:.0f} minutes old)")
                    self._path.unlink(missing_ok=True)
                    continue
                if not announced:
                    self._log(
                        "Waiting for another Friends of Duty exporter to "
                        "finish preparing Blender...")
                    announced = True
                time.sleep(2.0)

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
        self._path.unlink(missing_ok=True)


# ----------------------------------------------------------------- download

def _ssl_context():
    """Prefer the bundled CA set, fall back to the platform store.

    A frozen CPython finds no system roots on macOS without the certificate
    step only the python.org installer performs, so relying on the platform
    store alone fails on first export with an opaque
    CERTIFICATE_VERIFY_FAILED. certifi is the reliable default; the platform
    store is the fallback that lets a TLS-intercepting corporate proxy work.
    """
    import ssl

    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download(url: str, destination: Path, expected_bytes: int,
              progress: ProgressFn | None, log: LogFn,
              cancel: threading.Event | None) -> None:
    """Fetch `url` to `destination`, resuming a partial file if present."""
    context = _ssl_context()
    label = destination.name
    last_emit = 0.0
    attempt = 0
    while True:
        attempt += 1
        have = destination.stat().st_size if destination.exists() else 0
        if expected_bytes and have >= expected_bytes:
            return
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            **({"Range": f"bytes={have}-"} if have else {}),
        })
        try:
            with urllib.request.urlopen(
                    request, timeout=CONNECT_TIMEOUT_SECONDS,
                    context=context) as response:
                # A server that ignores Range answers 200 with the whole
                # file; appending to what we already have would corrupt it.
                resuming = response.status == 206 and have > 0
                if have and not resuming:
                    have = 0
                total = expected_bytes
                if not total:
                    length = response.headers.get("Content-Length")
                    total = (int(length) + have) if length else 0
                mode = "ab" if resuming else "wb"
                with open(destination, mode) as handle:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise ProvisionCancelled()
                        chunk = response.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        have += len(chunk)
                        now = time.monotonic()
                        if progress and now - last_emit >= PROGRESS_INTERVAL_SECONDS:
                            last_emit = now
                            progress("download", have, total, label)
            if progress:
                progress("download", have, expected_bytes or have, label)
            if expected_bytes and have < expected_bytes:
                raise OSError(
                    f"connection closed after {have} of {expected_bytes} bytes")
            return
        except ProvisionCancelled:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            if attempt >= DOWNLOAD_ATTEMPTS:
                raise ProvisionError(
                    f"Could not download Blender from {url}\n"
                    f"  {error}\n"
                    f"{offline_instructions()}"
                ) from error
            delay = min(2 ** attempt, 15)
            log(f"  download failed ({error}); retrying in {delay}s "
                f"[{attempt}/{DOWNLOAD_ATTEMPTS - 1}]")
            time.sleep(delay)


def offline_instructions(entry: dict | None = None) -> str:
    """What to tell a player whose machine cannot reach blender.org.

    This is a deliverable, not a courtesy: on a locked-down or offline
    machine it is the only path to a working export, so it has to carry the
    exact URL, the exact hash and the exact destination.
    """
    entry = entry or target_pin()
    return (
        "\nTo continue without a working connection, download this file on "
        "another machine and copy it into place:\n"
        f"  URL      {entry['url']}\n"
        f"  SHA-256  {entry['sha256']}\n"
        f"  Save as  {_archive_path(entry)}\n"
        "Then run the exporter again. Alternatively, if you already have "
        f"Blender {pinned_version_label()} installed, start the exporter "
        "with  --blender <path to the Blender executable>."
    )


def _archive_path(entry: dict) -> Path:
    return fod_paths.blender_cache_root() / "archives" / entry["archive"]


def _sha256(path: Path, progress: ProgressFn | None = None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    last_emit = 0.0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            now = time.monotonic()
            if progress and now - last_emit >= PROGRESS_INTERVAL_SECONDS:
                last_emit = now
                progress("verify", done, total, path.name)
    return digest.hexdigest()


# ------------------------------------------------------------------ extract

def _extract(archive: Path, entry: dict, destination: Path,
             log: LogFn) -> None:
    """Unpack `archive` so that `destination/<entry.inner_dir>` exists."""
    destination.mkdir(parents=True, exist_ok=True)
    form = entry["form"]
    if form == "zip":
        import zipfile

        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
    elif form == "tar.xz":
        # lzma is not used anywhere else in this codebase, so a frozen build
        # missing _lzma would fail here and nowhere else. --fod-selftest
        # imports it explicitly for exactly that reason.
        import tarfile

        with tarfile.open(archive, "r:xz") as bundle:
            _safe_extract_tar(bundle, destination)
    elif form == "dmg":
        _extract_dmg(archive, entry, destination, log)
    else:
        raise ProvisionError(f"Unknown archive form '{form}'.")
    produced = destination / entry["inner_dir"]
    if not produced.exists():
        raise ProvisionError(
            f"The Blender archive did not contain '{entry['inner_dir']}'. "
            "The download may be corrupt; delete "
            f"{_archive_path(entry)} and try again."
        )


def _safe_extract_tar(bundle, destination: Path) -> None:
    """Refuse member paths that escape the destination.

    The archive is hash-pinned, so this cannot trigger on the real file; it
    exists so that a future pin bump to a compromised or malformed artifact
    cannot write outside the cache.
    """
    root = destination.resolve()
    for member in bundle.getmembers():
        target = (root / member.name).resolve()
        if not str(target).startswith(str(root)):
            raise ProvisionError(
                f"Refusing a Blender archive entry outside the cache: "
                f"{member.name}")
    # data_filter lands in 3.12+; older interpreters keep the legacy path.
    if hasattr(bundle, "extractall") and sys.version_info >= (3, 12):
        bundle.extractall(destination, filter="data")
    else:
        bundle.extractall(destination)


def _extract_dmg(archive: Path, entry: dict, destination: Path,
                 log: LogFn) -> None:
    """Mount, copy with ditto, unmount - always unmount.

    `ditto` rather than `cp -R`: Blender.app is a signed bundle, and cp
    mishandles the extended attributes and metadata the signature covers.
    An explicit mountpoint keeps two concurrent exporters from colliding on
    /Volumes/Blender.
    """
    mountpoint = Path(tempfile.mkdtemp(prefix="fod-blender-dmg-"))
    try:
        log(f"  mounting {archive.name}")
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify",
             "-mountpoint", str(mountpoint), str(archive)],
            check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        shutil.rmtree(mountpoint, ignore_errors=True)
        raise ProvisionError(
            f"Could not mount the Blender disk image: "
            f"{error.stderr.strip() or error}") from error
    try:
        source = mountpoint / entry["inner_dir"]
        if not source.exists():
            raise ProvisionError(
                f"The Blender disk image did not contain "
                f"{entry['inner_dir']}.")
        log(f"  copying {entry['inner_dir']}")
        subprocess.run(
            ["ditto", str(source), str(destination / entry["inner_dir"])],
            check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise ProvisionError(
            f"Could not copy Blender out of the disk image: "
            f"{error.stderr.strip() or error}") from error
    finally:
        subprocess.run(["hdiutil", "detach", str(mountpoint), "-quiet"],
                       capture_output=True, text=True)
        shutil.rmtree(mountpoint, ignore_errors=True)


# -------------------------------------------------------------------- probe

#: Run inside Blender. Checks the three things that would otherwise fail
#: minutes into an export: the version, the glTF exporter, and the private
#: internals tools/fod_export_common.py monkeypatches for speed.
_PROBE_SOURCE = '''
import json, sys
import bpy

report = {"version": list(bpy.app.version), "hooks": {}}

# Optional second argument: the importer's python/ root. Probing it HERE
# rather than in the exporter's own process is deliberate. sys.executable is
# the exporter under PyInstaller, so a `sys.executable -c` probe hands the
# snippet to our own argparse ("unrecognized arguments: -c ..."); and
# importing the GPL extension into the proprietary process would breach the
# boundary section 9.1 rests on. Blender already loads it, so Blender asks.
importer_root = sys.argv[-1] if len(sys.argv) > 1 else None
if importer_root and importer_root != "--":
    try:
        sys.path.insert(0, importer_root)
        from cod_asset_importer.cod_asset_importer import (
            XMODEL_LOD_API_VERSION as _lod,
        )
        report["importerLodApi"] = int(_lod)
    except Exception as error:
        report["importerLodApi"] = 0
        report["importerError"] = f"{type(error).__name__}: {error}"
try:
    report["gltf_op"] = hasattr(bpy.ops.export_scene, "gltf")
except Exception:
    report["gltf_op"] = False
try:
    from io_scene_gltf2.blender.exp.exporter import GlTF2Exporter
    report["hooks"]["dedup"] = hasattr(
        GlTF2Exporter, "_GlTF2Exporter__append_unique_and_get_index")
except Exception:
    report["hooks"]["dedup"] = False
try:
    from io_scene_gltf2.io.com import gltf2_io
    report["hooks"]["animation"] = hasattr(gltf2_io, "Animation")
except Exception:
    report["hooks"]["animation"] = False
try:
    from io_scene_gltf2.blender.exp.animation.sampled import sampling_cache
    report["hooks"]["sampling_range"] = hasattr(sampling_cache, "get_range")
except Exception:
    report["hooks"]["sampling_range"] = False
sys.stdout.write("FOD_PROBE " + json.dumps(report) + "\\n")
sys.stdout.flush()
'''


def probe(executable: Path, log: LogFn = lambda _m: None,
          importer_root: Path | None = None) -> dict:
    """Ask a candidate Blender whether it can actually run the export.

    With `importer_root`, the same probe also reports the compiled importer's
    XMODEL_LOD_API_VERSION. One Blender launch answers both questions, and it
    answers them about the process that will really do the work.
    """
    with tempfile.TemporaryDirectory(prefix="fod-blender-probe-") as scratch:
        script = Path(scratch) / "fod_probe.py"
        script.write_text(_PROBE_SOURCE, encoding="utf-8")
        try:
            result = subprocess.run(
                [str(executable), "--background", "--factory-startup",
                 "--python", str(script),
                 "--", str(importer_root) if importer_root else "--"],
                capture_output=True, text=True, errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS,
                env=fod_paths.child_env("blender"))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProvisionError(
                f"Blender at {executable} could not be started: {error}"
            ) from error
    for line in result.stdout.splitlines():
        if line.startswith("FOD_PROBE "):
            return json.loads(line[len("FOD_PROBE "):])
    tail = "\n".join(
        (result.stdout + result.stderr).strip().splitlines()[-8:])
    raise ProvisionError(
        f"Blender at {executable} did not answer the readiness probe.\n{tail}")


def importer_status(executable: Path, importer_root: Path,
                    required: int = 1) -> tuple[bool, str]:
    """Is the shipped importer capable of authored-LOD export?

    Asked of Blender, not of us: see the note in _PROBE_SOURCE.
    """
    try:
        report = probe(executable, importer_root=importer_root)
    except ProvisionError as error:
        return False, str(error)
    version = report.get("importerLodApi", 0)
    if version >= required:
        return True, (f"cod_asset_importer ready with authored XModel LOD "
                      f"API {version} (verified inside Blender)")
    detail = report.get("importerError", "")
    return False, (
        f"the bundled cod_asset_importer does not expose authored XModel LOD "
        f"API {required} (reported {version})"
        + (f": {detail}" if detail else ""))


def _require_usable(executable: Path, report: dict) -> None:
    version = tuple(report.get("version", ())[:3])
    if version != pinned_version():
        raise ProvisionError(
            f"Blender at {executable} reports version "
            f"{'.'.join(str(p) for p in version) or 'unknown'}, but this "
            f"exporter requires exactly {pinned_version_label()}. "
            "Content produced by another version is not the content this "
            "build was tested against."
        )
    if not report.get("gltf_op"):
        raise ProvisionError(
            f"Blender at {executable} has no glTF exporter (io_scene_gltf2). "
            "This is not a stock Blender build.")
    missing = sorted(
        name for name, ok in report.get("hooks", {}).items() if not ok)
    if missing:
        # These are private glTF-exporter internals the export monkeypatches
        # for speed. They are documented as structure-neutral, so a miss
        # costs roughly 6x on skinned exports rather than changing output -
        # but on the pinned build all three are known to resolve, so a miss
        # means this is not the pinned build.
        raise ProvisionError(
            f"Blender at {executable} is missing glTF exporter internals "
            f"({', '.join(missing)}). This is not the pinned "
            f"{pinned_version_label()} build.")


# ------------------------------------------------------------------ resolve

def _provision(entry: dict, progress: ProgressFn | None, log: LogFn,
               cancel: threading.Event | None) -> Path:
    root = install_root()
    incoming = root.with_name(root.name + ".incoming")
    archive = _archive_path(entry)
    archive.parent.mkdir(parents=True, exist_ok=True)

    megabytes = entry["archive_bytes"] / (1024 * 1024)
    log(f"Preparing Blender {pinned_version_label()} "
        f"({megabytes:.0f} MB download, one time only)")
    log(f"  {entry['url']}")

    if archive.exists() and archive.stat().st_size == entry["archive_bytes"]:
        log("  archive already downloaded")
    else:
        _download(entry["url"], archive, entry["archive_bytes"],
                  progress, log, cancel)

    if progress:
        progress("verify", 0, entry["archive_bytes"], entry["archive"])
    digest = _sha256(archive, progress)
    if digest != entry["sha256"]:
        archive.unlink(missing_ok=True)
        raise ProvisionError(
            "The downloaded Blender archive does not match its expected "
            "checksum and has been deleted.\n"
            f"  expected {entry['sha256']}\n"
            f"  actual   {digest}\n"
            "This is usually a corrupted download - try again. If it "
            "repeats, the file served to this machine is not the one this "
            "build was pinned to; do not bypass this check."
        )
    log("  checksum verified")

    if cancel is not None and cancel.is_set():
        raise ProvisionCancelled()

    if progress:
        progress("extract", 0, 0, entry["archive"])
    shutil.rmtree(incoming, ignore_errors=True)
    log("  extracting")
    _extract(archive, entry, incoming, log)

    # Normalise: the archives unpack to a versioned directory (or an .app),
    # and the rest of the exporter wants one predictable layout.
    produced = incoming / entry["inner_dir"]
    if entry["inner_dir"] != Path(entry["executable"]).parts[0]:
        for child in produced.iterdir():
            shutil.move(str(child), str(incoming / child.name))
        produced.rmdir()

    executable = executable_in(incoming, entry)
    if not executable.exists():
        raise ProvisionError(
            f"Blender was extracted but {executable.name} is missing.")
    if os.name == "posix":
        _make_executable(incoming, entry)

    if progress:
        progress("check", 0, 0, pinned_version_label())
    log("  checking the extracted build")
    _require_usable(executable, probe(executable, log))

    shutil.rmtree(root, ignore_errors=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(incoming, root)
    _stamp_path(root).write_text(json.dumps({
        "version": pinned_version_label(),
        "target": entry["target"],
        "archive_sha256": entry["sha256"],
        "provisioned_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2), encoding="utf-8")

    # The archive is ~300-400 MB and has done its job. Keeping it would
    # double the cost of this feature on the player's disk for no benefit;
    # a re-provision re-downloads.
    archive.unlink(missing_ok=True)

    log(f"Blender {pinned_version_label()} ready")
    return executable_in(root, entry)


def _make_executable(root: Path, entry: dict) -> None:
    """Restore exec bits the extractor may have dropped.

    tarfile preserves modes and ditto preserves the bundle, so this is
    belt-and-braces for those paths - but a zip extraction on a POSIX host
    (a developer pulling the Windows archive) loses them outright.
    """
    candidates = [executable_in(root, entry)]
    candidates.extend(root.glob("*/python/bin/python3*"))
    candidates.extend(
        root.glob("Blender.app/Contents/Resources/*/python/bin/python3*"))
    for candidate in candidates:
        if candidate.is_file():
            candidate.chmod(candidate.stat().st_mode | 0o111)


def resolve(manual: Path | str | None = None, *,
            progress: ProgressFn | None = None,
            log: LogFn = print,
            cancel: threading.Event | None = None,
            allow_download: bool = True) -> Path:
    """The Blender the export should run. Downloads it if necessary.

    Raises ProvisionError with a player-readable message on failure, and
    ProvisionCancelled if `cancel` is set during the download.
    """
    entry = target_pin()

    override = manual or os.environ.get(fod_paths.BLENDER_ENV)
    if override:
        candidate = Path(override).expanduser()
        if not candidate.exists():
            raise ProvisionError(f"No Blender executable at {candidate}.")
        _require_usable(candidate, probe(candidate, log))
        log(f"Using Blender {pinned_version_label()} at {candidate}")
        return candidate

    cached = cached_blender(entry)
    if cached is not None:
        if progress:
            progress("ready", 1, 1, pinned_version_label())
        log(f"Blender {pinned_version_label()} ready (cached)")
        return cached

    if not allow_download:
        raise ProvisionError(
            f"Blender {pinned_version_label()} is not installed yet."
            + offline_instructions(entry))

    with _CacheLock(fod_paths.blender_cache_root(), log):
        # Another exporter may have finished while we waited for the lock.
        cached = cached_blender(entry)
        if cached is not None:
            log(f"Blender {pinned_version_label()} ready (cached)")
            if progress:
                progress("ready", 1, 1, pinned_version_label())
            return cached
        executable = _provision(entry, progress, log, cancel)
    if progress:
        progress("ready", 1, 1, pinned_version_label())
    return executable


def status() -> tuple[bool, str]:
    """One line for the requirements screen: is a download pending?"""
    try:
        entry = target_pin()
    except ProvisionError as error:
        return False, str(error)
    if cached_blender(entry) is not None:
        return True, f"Blender {pinned_version_label()} ready (cached)"
    megabytes = entry["archive_bytes"] / (1024 * 1024)
    return True, (
        f"Blender {pinned_version_label()} will be downloaded "
        f"automatically ({megabytes:.0f} MB, one time only)")


def main() -> int:
    """`python3 exporter/blender_provisioner.py [--check|--blender PATH]`."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report status without downloading")
    parser.add_argument("--blender", type=Path, help="probe this Blender")
    arguments = parser.parse_args()
    try:
        if arguments.check:
            ok, message = status()
            print(message)
            return 0 if ok else 1
        executable = resolve(arguments.blender)
    except ProvisionCancelled:
        print("Cancelled.")
        return 130
    except ProvisionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
