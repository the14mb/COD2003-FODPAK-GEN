#!/usr/bin/env python3
"""Build or fetch the cod-asset-importer compiled extension for this platform.

The Blender addon at tools/cod-asset-importer loads a compiled Rust extension
as a package submodule (`from .cod_asset_importer import ...`):
`cod_asset_importer.abi3.so` on macOS/Linux, `cod_asset_importer.pyd` on
Windows. The crate targets Python's stable ABI (PyO3 abi3-py38), so a single
artifact per OS+architecture works for any modern Python, including the
interpreter bundled with Blender.

**This is a developer/CI module. It is not part of the shipped payload.**
A player's exporter arrives with the extension already in place; a module
whose failure mode is "install rustup" has no business in a customer install.

Artifacts come from `packaging/importers/<target>/`, populated by
`make importer-fetch` from the CI matrix at
<https://github.com/anarqz/cod-asset-importer>. Building from the vendored
Rust source is still possible but is now **opt-in** (`--from-source`) rather
than an automatic fallback: an automatic fallback is what made a Rust
toolchain a de-facto prerequisite on all four targets, which is precisely
what the single-executable work exists to remove.

Usage:
  python3 exporter/build_importer.py [--check] [--require-lod]
  python3 exporter/build_importer.py --from-source [--force] [--require-lod]
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

EXPORTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = (
    EXPORTER_DIR
    if (EXPORTER_DIR / "tools").is_dir()
    else EXPORTER_DIR.parent
)
DEFAULT_IMPORTER_ROOT = (
    EXPORTER_DIR / "tools" / "cod-asset-importer"
    if (EXPORTER_DIR / "tools").is_dir()
    else REPO_ROOT / "tools" / "cod-asset-importer"
)
RUSTUP_URL = "https://rustup.rs"
REQUIRED_LOD_API_VERSION = 1

#: Where `make importer-fetch` drops CI artifacts, one directory per target.
IMPORTER_CACHE = REPO_ROOT / "packaging" / "importers"

#: Host -> the target name used by the importer CI matrix and by
#: packaging/importers/. Kept as one mapping so the workflow, the fetch
#: target and this module cannot drift apart.
#: Keys are platform.system() verbatim, which is "Darwin", not "macOS" --
#: platform_label() does that translation for humans and this must not.
CI_TARGETS = {
    ("Windows", "x86_64"): "windows-x64",
    ("Darwin", "arm64"): "macos-arm64",
    ("Darwin", "x86_64"): "macos-x64",
    ("Linux", "x86_64"): "linux-x64",
}


def ci_target(pair: tuple[str, str] | None = None) -> str | None:
    """This machine's artifact name, or None where no build is published.

    Linux ARM is deliberately absent: Blender publishes no Linux ARM build,
    so an importer for it could not be used even if we built one.
    """
    return CI_TARGETS.get(pair or host_platform())

LogFn = Callable[[str], None]


class BuildError(Exception):
    pass


# ------------------------------------------------------------------ platform

def expected_artifact() -> str:
    """File name the addon's `from .cod_asset_importer import ...` loads."""
    if platform.system() == "Windows":
        return "cod_asset_importer.pyd"
    return "cod_asset_importer.abi3.so"


def cdylib_name() -> str:
    """File name `cargo build` writes to target/release/."""
    system = platform.system()
    if system == "Windows":
        return "cod_asset_importer.dll"
    if system == "Darwin":
        return "libcod_asset_importer.dylib"
    return "libcod_asset_importer.so"


def _normalize_arch(machine: str) -> str:
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(
        machine.lower(), machine.lower())


def host_platform() -> tuple[str, str]:
    return platform.system(), _normalize_arch(platform.machine())


def platform_label(pair: tuple[str, str] | None = None) -> str:
    system, arch = pair or host_platform()
    return {"Darwin": "macOS"}.get(system, system) + " " + arch


def artifact_platform(path: Path) -> tuple[str, str] | None:
    """Best-effort (OS, arch) of a compiled extension from file magic;
    None when the format is not recognised."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    if head[:4] == b"\x7fELF":
        machine = int.from_bytes(head[18:20], "little")
        arch = {0x03: "x86", 0x3E: "x86_64", 0xB7: "arm64"}.get(
            machine, f"e_machine {machine:#x}")
        return "Linux", arch
    if head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"):
        cputype = int.from_bytes(head[4:8], "little")
        arch = {0x0100000C: "arm64", 0x01000007: "x86_64"}.get(
            cputype, f"cputype {cputype:#x}")
        return "Darwin", arch
    if head[:4] == b"\xca\xfe\xba\xbe":  # Mach-O universal (fat) binary
        return "Darwin", "universal"
    if head[:2] == b"MZ" and len(head) >= 0x40:
        pe_offset = int.from_bytes(head[0x3C:0x40], "little")
        if pe_offset + 6 <= len(head) and head[pe_offset:pe_offset + 4] == b"PE\0\0":
            machine = int.from_bytes(head[pe_offset + 4:pe_offset + 6], "little")
            arch = {0x14C: "x86", 0x8664: "x86_64", 0xAA64: "arm64"}.get(
                machine, f"machine {machine:#x}")
            return "Windows", arch
        return "Windows", "unknown"
    return None


def _matches_host(path: Path) -> bool:
    detected = artifact_platform(path)
    if detected is None:  # unrecognised format — assume usable
        return True
    system, arch = host_platform()
    return detected[0] == system and detected[1] in (arch, "universal")


def artifact_lod_api_version(path: Path) -> tuple[int | None, str]:
    """Probe the native module in an isolated interpreter.

    Returning zero is a valid v3.5 LOD0-only module. None means the extension
    could not be imported at all.
    """
    probe = (
        "import importlib.util, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[1]).resolve()\n"
        "spec = importlib.util.spec_from_file_location("
        "'_fod_probe.cod_asset_importer', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(getattr(module, 'XMODEL_LOD_API_VERSION', 0))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return None, detail or "native module probe failed"
    try:
        return int(result.stdout.strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        return None, "native module returned no LOD API version"


# --------------------------------------------------------------------- check

def _addon_dir(importer_root: Path) -> Path:
    return importer_root / "python" / "cod_asset_importer"


def check(
    importer_root: Path | None = None,
    require_lod: bool = False,
) -> tuple[bool, str]:
    """(ok, message) for the compiled extension on the current platform."""
    root = importer_root or DEFAULT_IMPORTER_ROOT
    addon = _addon_dir(root)
    if not addon.is_dir():
        return False, f"cod-asset-importer addon missing: {addon}"
    target = addon / expected_artifact()
    if not target.is_file():
        return False, (
            f"{target.name} missing for {platform_label()} — one-time local "
            f"setup needed (common macOS, Windows, and Linux hosts ship "
            f"prebuilt; other hosts build from the vendored Rust source and need "
            f"{RUSTUP_URL}). "
            f"Run: python3 exporter/build_importer.py")
    detected = artifact_platform(target)
    if detected is not None and not _matches_host(target):
        return False, (
            f"{target.name} is built for {platform_label(detected)} but this "
            f"machine is {platform_label()} — rebuild with "
            f"python3 exporter/build_importer.py --force (needs {RUSTUP_URL})")
    lod_api, probe_error = artifact_lod_api_version(target)
    if lod_api is None:
        return False, (
            f"{target.name} cannot be imported on {platform_label()}: "
            f"{probe_error}")
    if lod_api < REQUIRED_LOD_API_VERSION:
        message = (
            f"{target.name} ready for backward-compatible LOD0-only export "
            f"({target.stat().st_size:,} bytes, {platform_label()}); authored "
            "XModel LOD export requires importer v3.6+. Use the exporter's "
            "'Prepare importer' action, or rebuild the vendored source with: "
            f"{sys.executable} exporter/build_importer.py --require-lod "
            f"(requires the Rust toolchain from {RUSTUP_URL})")
        if require_lod:
            return False, message
        return True, message
    return True, (
        f"{target.name} ready with authored XModel LOD API {lod_api} "
        f"({target.stat().st_size:,} bytes, {platform_label()})")


# --------------------------------------------------------------------- build

def _install_from_cache(
    target: Path,
    log: LogFn,
    require_lod: bool = False,
) -> bool:
    """Install this host's artifact from packaging/importers/, if present.

    Populated by `make importer-fetch` from the CI matrix, which asserts
    XMODEL_LOD_API_VERSION == 1 before publishing anything. This is the
    normal acquisition path; everything below it is a fallback.
    """
    name = ci_target()
    if name is None:
        return False
    source = IMPORTER_CACHE / name / target.name
    if not source.is_file():
        return False
    if not _matches_host(source):
        detected = artifact_platform(source)
        log(f"cached {name}/{source.name} is for "
            f"{platform_label(detected) if detected else 'an unknown platform'}"
            f" — not usable on {platform_label()}")
        return False
    if require_lod:
        lod_api, error = artifact_lod_api_version(source)
        if lod_api is None or lod_api < REQUIRED_LOD_API_VERSION:
            log(f"cached {name}/{source.name} lacks authored XModel LOD "
                f"API {REQUIRED_LOD_API_VERSION}"
                + (f" ({error})" if error else ""))
            return False
    shutil.copy2(source, target)
    log(f"installed {target.name} from packaging/importers/{name} "
        f"({target.stat().st_size:,} bytes)")
    return True


def _extract_from_release(
    importer_root: Path,
    target: Path,
    log: LogFn,
    require_lod: bool = False,
) -> bool:
    """Install a matching prebuilt binary found inside release/*.zip, if any."""
    for zip_path in sorted((importer_root / "release").glob("*.zip")):
        try:
            archive = zipfile.ZipFile(zip_path)
        except (OSError, zipfile.BadZipFile) as error:
            log(f"skipping unreadable release archive {zip_path.name}: {error}")
            continue
        with archive:
            candidates = [name for name in archive.namelist()
                          if PurePosixPath(name).name == target.name]
            for member in candidates:
                with tempfile.NamedTemporaryFile(
                        dir=target.parent,
                        suffix=target.suffix,
                        delete=False) as staged:
                    staged_path = Path(staged.name)
                    with archive.open(member) as source:
                        shutil.copyfileobj(source, staged)
                if _matches_host(staged_path):
                    if require_lod:
                        lod_api, error = artifact_lod_api_version(staged_path)
                        if (
                            lod_api is None
                            or lod_api < REQUIRED_LOD_API_VERSION
                        ):
                            staged_path.unlink(missing_ok=True)
                            log(
                                f"{zip_path.name}:{member} lacks authored "
                                "XModel LOD API 1"
                                + (f" ({error})" if error else "")
                                + " — skipping LOD0-only prebuilt"
                            )
                            continue
                    os.replace(staged_path, target)
                    log(f"installed prebuilt {target.name} from "
                        f"{zip_path.name} ({target.stat().st_size:,} bytes)")
                    return True
                detected = artifact_platform(staged_path)
                staged_path.unlink(missing_ok=True)
                log(f"{zip_path.name}:{member} is for "
                    f"{platform_label(detected) if detected else 'an unknown platform'}"
                    f" — not usable on {platform_label()}")
    return False


def _cargo_build(importer_root: Path, target: Path, log: LogFn) -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        platform_extra = {
            "Windows": (
                " Windows also needs Visual Studio Build Tools with the "
                "'Desktop development with C++' workload."
            ),
            "Darwin": (
                " macOS also needs the Xcode Command Line Tools."
            ),
            "Linux": (
                " Linux also needs a native linker/compiler toolchain "
                "(for example the distribution's build-essential package)."
            ),
        }.get(platform.system(), "")
        raise BuildError(
            "cargo not found — a one-time local build of the "
            "cod-asset-importer extension needs the Rust toolchain. "
            f"Install it from {RUSTUP_URL}, restart the terminal, and re-run."
            + platform_extra)
    version = subprocess.run(
        [cargo, "--version"], capture_output=True, text=True)
    if version.returncode != 0:
        raise BuildError(
            f"'cargo --version' failed — the Rust toolchain looks broken. "
            f"Reinstall from {RUSTUP_URL}.")
    log(version.stdout.strip())

    manifest = importer_root / "rust" / "cod_asset_importer" / "Cargo.toml"
    if not manifest.is_file():
        raise BuildError(f"vendored Rust source missing: {manifest}")
    target_dir = manifest.parent / "target"
    argv = [cargo, "build", "--release",
            "--manifest-path", str(manifest),
            "--target-dir", str(target_dir)]
    env = dict(os.environ)
    # PyO3's build script probes a Python interpreter; pin it to ours so the
    # build works even when `python`/`python3` is missing from PATH.
    env.setdefault("PYO3_PYTHON", sys.executable)
    if platform.system() == "Darwin":
        # Extension modules must leave Python symbols unresolved until load
        # time. setuptools-rust/maturin inject these flags; plain `cargo
        # build` needs them spelled out or the link fails on macOS.
        env["RUSTFLAGS"] = (
            env.get("RUSTFLAGS", "")
            + " -C link-arg=-undefined -C link-arg=dynamic_lookup").strip()
    log("$ " + " ".join(argv))
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1, env=env)
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip("\n"))
    if process.wait() != 0:
        platform_extra = {
            "Windows": (
                " On Windows, verify Visual Studio Build Tools includes "
                "'Desktop development with C++'."
            ),
            "Darwin": (
                " On macOS, verify the Xcode Command Line Tools are installed."
            ),
            "Linux": (
                " On Linux, verify a native linker/compiler toolchain is "
                "installed."
            ),
        }.get(platform.system(), "")
        raise BuildError(
            f"cargo build failed with code {process.returncode} — see the "
            f"log above. If pieces of the toolchain are missing, reinstall "
            f"Rust from {RUSTUP_URL}."
            + platform_extra)

    produced = target_dir / "release" / cdylib_name()
    if not produced.is_file():
        raise BuildError(
            f"cargo build succeeded but {produced} was not produced")
    shutil.copyfile(produced, target)
    log(f"installed {target} ({target.stat().st_size:,} bytes)")


def build(
    importer_root: Path | None = None,
    force: bool = False,
    log: LogFn = print,
    require_lod: bool = False,
    from_source: bool = False,
) -> Path:
    """Ensure the platform's compiled extension exists; returns its path."""
    root = importer_root or DEFAULT_IMPORTER_ROOT
    addon = _addon_dir(root)
    if not addon.is_dir():
        raise BuildError(f"cod-asset-importer addon missing: {addon}")
    target = addon / expected_artifact()
    if not force and target.is_file() and _matches_host(target):
        lod_api, _ = artifact_lod_api_version(target)
        if (
            not require_lod
            or (
                lod_api is not None
                and lod_api >= REQUIRED_LOD_API_VERSION
            )
        ):
            capability = (
                f"LOD API {lod_api}"
                if lod_api
                else "LOD0-only"
            )
            log(
                f"{target.name} already present ({capability}) — "
                "nothing to build"
            )
            return target
    # --force means "rebuild from the checked-in Rust source", so it must
    # never reinstall a cached or archived artifact over the request.
    if not force:
        if _install_from_cache(target, log, require_lod=require_lod):
            return target
        if _extract_from_release(root, target, log, require_lod=require_lod):
            return target
    if not (force or from_source):
        # Deliberately NOT falling through to cargo. An automatic
        # build-from-source fallback is what made rustup plus a platform C++
        # linker a de-facto prerequisite on all four targets, which is
        # exactly what the shipped exporter must never ask of a player.
        raise BuildError(
            "No usable cod_asset_importer extension for "
            f"{platform_label()}"
            + (f" (authored XModel LOD API {REQUIRED_LOD_API_VERSION} "
               "required)" if require_lod else "")
            + ".\n"
            "Fetch the CI-built artifacts with:\n"
            "    make importer-fetch\n"
            "or, if you are changing the Rust source, build one locally:\n"
            "    python3 exporter/build_importer.py --from-source\n"
            f"(that needs the Rust toolchain from {RUSTUP_URL}.)")
    _cargo_build(root, target, log)
    if require_lod:
        lod_api, error = artifact_lod_api_version(target)
        if lod_api is None or lod_api < REQUIRED_LOD_API_VERSION:
            raise BuildError(
                "local importer build does not expose authored XModel LOD "
                f"API {REQUIRED_LOD_API_VERSION}"
                + (f": {error}" if error else ""))
    return target


# ---------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report extension status without building")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even when the extension is present")
    parser.add_argument(
        "--require-lod",
        action="store_true",
        help="require importer v3.6 authored XModel LOD support",
    )
    parser.add_argument(
        "--from-source",
        action="store_true",
        help="build from the vendored Rust source (needs the Rust toolchain)",
    )
    parser.add_argument("--importer-root", type=Path,
                        help="alternate cod-asset-importer checkout (testing)")
    args = parser.parse_args()
    if not args.check:
        try:
            build(
                args.importer_root,
                force=args.force,
                require_lod=args.require_lod,
                from_source=args.from_source,
            )
        except BuildError as error:
            print(f"FAIL {error}", file=sys.stderr)
            return 1
    ok, message = check(
        args.importer_root,
        require_lod=args.require_lod,
    )
    print(("OK   " if ok else "FAIL ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
