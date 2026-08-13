#!/usr/bin/env python3
"""Build the shipped exporter payload.

    python3 packaging/build_exporter.py --output Builds/Windows/Exporter

Produces the directory the game launches: a frozen CPython shell, the
exporter's own modules and the reachable tools/ closure as loose source, the
importer extension for this target, and a build stamp.

Two things are deliberately NOT here. Blender is not bundled -- the exporter
downloads the pinned build at runtime (Docs/EXPORTER_SINGLE_EXECUTABLE.md
section 5.2). And nothing is signed: the owner accepted an unsigned binary,
which on Windows costs an antivirus-reputation argument rather than a
user-visible prompt, because SmartScreen keys off Mark-of-the-Web and Steam
depot files do not carry it.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGING = REPO / "packaging"
EXPORTER = REPO / "exporter"
TOOLS = REPO / "tools"

sys.path.insert(0, str(PACKAGING))
sys.path.insert(0, str(EXPORTER))

import tools_closure  # noqa: E402

#: Exporter modules that ship. build_importer is a developer/CI tool whose
#: failure mode in a player's install is "install rustup"; it stays out.
SHIPPED_EXPORTER_MODULES = (
    "friends_of_duty_exporter.py", "pipeline.py", "package.py",
    "fod_launcher.py", "fod_paths.py", "progress.py", "toolchain.py",
    "blender_provisioner.py", "cod_autodetect.py",
)

#: Imports PyInstaller cannot discover on its own. Everything the payload
#: needs is reached through runpy from loose files, so the analysis of
#: fod_launcher.py sees almost nothing; these are unioned with the AST scan.
ALWAYS_HIDDEN = (
    "ssl", "certifi", "lzma", "bz2", "zlib", "tarfile", "zipfile",
    "tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk",
    "numpy", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFilter",
    "PIL.DdsImagePlugin", "PIL.TgaImagePlugin", "PIL.JpegImagePlugin",
    "PIL.PngImagePlugin", "PIL.BmpImagePlugin",
    "wave", "csv", "queue", "runpy", "hashlib", "struct", "binascii",
    "urllib.request", "urllib.error", "http.client", "email",
)


def scanned_imports() -> set[str]:
    """Top-level imports across every module that will ship.

    An AST scan rather than a hand-kept list: a frozen build missing one
    module fails at whichever step first touches it, minutes into a player's
    export, and a list nobody regenerates is how that happens.
    """
    found: set[str] = set()
    sources = [EXPORTER / name for name in SHIPPED_EXPORTER_MODULES]
    sources += [TOOLS / f"{name}.py" for name in tools_closure.closure()]
    for source in sources:
        if not source.is_file():
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module)
    return found


def importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def hidden_imports() -> list[str]:
    # bpy and mathutils exist only inside Blender. Freezing them is
    # impossible and, per section 9.1, would also drag GPL code into the
    # proprietary process -- the boundary the whole design rests on.
    blender_only = {"bpy", "mathutils", "bmesh", "bpy_extras", "gpu",
                    "io_scene_gltf2", "cod_asset_importer"}
    local = {p.stem for p in TOOLS.glob("*.py")} | {
        p.stem for p in EXPORTER.glob("*.py")}
    candidates = (set(ALWAYS_HIDDEN) | scanned_imports()) - blender_only - local
    resolved = sorted(n for n in candidates if importable(n))
    missing = sorted(n for n in candidates if not importable(n))
    if missing:
        print(f"  note: not importable here, skipped: {missing}")
    return resolved


def run_pyinstaller(work: Path, hidden: list[str]) -> Path:
    env = dict(os.environ)
    env["FOD_REPO_ROOT"] = str(REPO)
    env["FOD_HIDDEN_IMPORTS"] = json.dumps(hidden)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "--distpath", str(work / "dist"), "--workpath", str(work / "build"),
         str(PACKAGING / "fod_exporter.spec")],
        check=True, cwd=str(REPO), env=env,
    )
    return work / "dist" / "_internal_build"


def assemble(frozen: Path, output: Path, target: str) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    launcher = "FriendsOfDutyExporter" + (".exe" if os.name == "nt" else "")
    # PyInstaller >= 6 already emits <exe> + _internal/. Re-nesting that into
    # another _internal/ leaves the interpreter unable to find its own DLL
    # ("Failed to load Python DLL ... python312.dll"), so take the tree as it
    # comes and only build the layout ourselves on the older flat output.
    if (frozen / "_internal").is_dir():
        shutil.copytree(frozen, output, dirs_exist_ok=True)
    else:
        internal = output / "_internal"
        internal.mkdir(parents=True)
        for item in frozen.iterdir():
            destination = (output if item.name == launcher else internal) / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)
    if not (output / launcher).is_file():
        raise SystemExit(
            f"frozen build produced no {launcher}; got "
            f"{sorted(p.name for p in frozen.iterdir())[:8]}")

    for name in SHIPPED_EXPORTER_MODULES:
        shutil.copy2(EXPORTER / name, output / name)

    # Brand art, loose beside the binary like everything else the GUI reads at
    # runtime, because fod_paths.payload_root() resolves the same way in a
    # checkout and in a bundle. Missing art degrades to a plain window rather
    # than failing, but a shipped payload should never be missing it.
    assets_src = EXPORTER / "assets"
    if not assets_src.is_dir():
        raise SystemExit(
            f"brand assets missing: {assets_src}\n"
            "run: python3 packaging/make_brand_assets.py --artwork <dir>")
    shutil.copytree(assets_src, output / "assets")

    tools_out = output / "tools"
    tools_out.mkdir()
    for name in tools_closure.read_manifest():
        shutil.copy2(TOOLS / f"{name}.py", tools_out / f"{name}.py")

    # The importer ships as a loose file: Blender's interpreter loads it, and
    # it cannot import from inside a frozen archive.
    importer_src = TOOLS / "cod-asset-importer" / "python" / "cod_asset_importer"
    importer_out = tools_out / "cod-asset-importer" / "python" / "cod_asset_importer"
    importer_out.mkdir(parents=True)
    for source in importer_src.glob("*.py*"):
        shutil.copy2(source, importer_out / source.name)
    artifact = PACKAGING / "importers" / target / (
        "cod_asset_importer.pyd" if target.startswith("windows")
        else "cod_asset_importer.abi3.so")
    if not artifact.is_file():
        raise SystemExit(
            f"missing importer artifact {artifact}\nrun: make importer-fetch")
    shutil.copy2(artifact, importer_out / artifact.name)

    licenses = TOOLS / "cod-asset-importer"
    out_licenses = output / "LICENSES" / "cod-asset-importer"
    out_licenses.mkdir(parents=True)
    for name in ("LICENSE", "MODIFICATIONS.md", "PROVENANCE.md"):
        if (licenses / name).is_file():
            shutil.copy2(licenses / name, out_licenses / name)
    shutil.copytree(licenses / "rust", out_licenses / "rust",
                    ignore=shutil.ignore_patterns("target", "*.rlib"))


def stamp(output: Path, target: str) -> str:
    """Hash every payload file, then write the stamp naming that hash.

    Excludes the stamp and payload.json themselves, so the value is stable
    and needs no second freeze pass.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in output.rglob("*") if p.is_file()):
        rel = path.relative_to(output).as_posix()
        if rel in {"build_stamp.py", "payload.json"}:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    payload_sha = digest.hexdigest()

    pin = json.loads((PACKAGING / "blender_pin.json").read_text(encoding="utf-8"))
    import blender_provisioner, toolchain  # noqa: E402

    values = {
        "PAYLOAD_SHA256": payload_sha,
        "TARGET": target,
        "BLENDER_VERSION": pin["version"],
        "BLENDER_PIN": pin,
        "IMPORTER_VERSION": toolchain.importer_version(),
        "PYTHON_VERSION": ".".join(str(p) for p in sys.version_info[:3]),
    }
    (output / "build_stamp.py").write_text(
        '"""Generated by packaging/build_exporter.py. Do not edit."""\n\n'
        + "".join(f"{k} = {v!r}\n" for k, v in values.items()),
        encoding="utf-8")
    (output / "payload.json").write_text(
        json.dumps({k: v for k, v in values.items() if k != "BLENDER_PIN"}
                   | {"blenderPin": {"version": pin["version"],
                                     "sha256": pin["targets"][
                                         blender_target(target)]["sha256"]}},
                   indent=2) + "\n", encoding="utf-8")
    return payload_sha


def blender_target(target: str) -> str:
    return {"windows-x64": "windows-x64", "linux-x64": "linux-x64",
            "macos-arm64": "macos-arm64", "macos-x64": "macos-x64"}[target]


def host_target() -> str:
    machine = sysconfig.get_platform()
    if sys.platform == "win32":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if "arm64" in machine else "macos-x64"
    return "linux-x64"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", default=None)
    parser.add_argument("--skip-selftest", action="store_true")
    arguments = parser.parse_args()
    target = arguments.target or host_target()
    output = arguments.output.resolve()
    work = REPO / "Builds" / ".exporter-work"

    print(f"target      : {target}")
    hidden = hidden_imports()
    print(f"hiddenimports: {len(hidden)}")
    frozen = run_pyinstaller(work, hidden)
    assemble(frozen, output, target)
    payload_sha = stamp(output, target)

    files = [p for p in output.rglob("*") if p.is_file()]
    print(f"payload     : {len(files)} files, "
          f"{sum(p.stat().st_size for p in files):,} bytes")
    print(f"PAYLOAD_SHA256 = {payload_sha}")

    if not arguments.skip_selftest:
        launcher = output / ("FriendsOfDutyExporter.exe" if os.name == "nt"
                             else "FriendsOfDutyExporter")
        print(f"\n$ {launcher} --fod-selftest")
        result = subprocess.run([str(launcher), "--fod-selftest"])
        if result.returncode != 0:
            raise SystemExit("self-test failed; payload is not shippable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
