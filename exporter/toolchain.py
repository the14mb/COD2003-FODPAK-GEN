#!/usr/bin/env python3
"""What produced a step's output, recorded in that step's signature.

Before this module, `_step_signature` hashed the exporter's own `.py` files
and the importer's native extension — but nothing about Blender. Upgrading
Blender therefore invalidated **zero** markers, and a resumed export happily
mixed 4.2 output with 4.5 output inside one package.

The fix is deliberately per-step rather than one global build stamp. A single
"payload changed" key would invalidate all 19 markers — including `maps`
(391 MB, 1,663 GLBs) and `props` (341 GLBs) — for a one-line UI fix, which is
strictly worse than the behaviour it replaced. Instead each step declares only
the parts of the toolchain it actually depends on:

  * Blender steps          -> the pinned Blender version
  * importer-dependent     -> the importer's version and native hash
  * host steps             -> interpreter, numpy and Pillow versions

so a Blender bump re-runs the six Blender steps and nothing else, and a numpy
bump re-runs the host steps and leaves the Blender output alone.

`EXPORTER_BUILD` is deliberately never hashed here. It changes on every CI
run, and hashing it would make every build a full re-export.

In a shipped bundle these values are baked into `build_stamp` at packaging
time; in a source checkout they are read live. Both paths produce the same
shape, so a marker written by one is comparable with the other only when the
values genuinely agree — which is the intent.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path

import blender_provisioner as fod_blender

#: Steps whose output comes out of Blender.
BLENDER_STEPS = frozenset({
    "viewmodels", "worldmodels", "shellcasing", "projectiles", "players",
    "props",
})

#: Steps that load the compiled cod_asset_importer extension. Every one is
#: also a Blender step today; kept as a separate set because Phase B moves
#: these to a direct writer while leaving other Blender steps behind.
IMPORTER_STEPS = frozenset({
    "viewmodels", "worldmodels", "shellcasing", "projectiles", "players",
    "props",
})

_native_cache: dict[str, str] = {}


def _module_version(name: str) -> str:
    try:
        return str(getattr(importlib.import_module(name), "__version__", "?"))
    except ImportError:
        return "absent"


def _stamp(attribute: str):
    try:
        import build_stamp  # type: ignore

        return getattr(build_stamp, attribute, None)
    except ImportError:
        return None


def blender_version() -> str:
    baked = _stamp("BLENDER_VERSION")
    if baked:
        return str(baked)
    try:
        return fod_blender.pinned_version_label()
    except Exception:
        # A missing pin is reported by the provisioner with a real message;
        # a signature must not be the thing that raises.
        return "unknown"


def importer_version() -> str:
    baked = _stamp("IMPORTER_VERSION")
    if baked:
        return str(baked)
    try:
        from pipeline import IMPORTER_PY_ROOT  # local import: circular at module scope

        init = IMPORTER_PY_ROOT / "cod_asset_importer" / "__init__.py"
        for line in init.read_text(encoding="utf-8").splitlines():
            if '"version"' in line:
                return line.split(":", 1)[1].strip().strip(",")
    except Exception:
        pass
    return "unknown"


def importer_native_sha256(path: Path | None = None) -> str:
    """Hash of the compiled extension actually on disk.

    `signature_files` already covers this file for the steps that use it, so
    this is belt-and-braces — but it is also the only value that survives
    into a bundle where the extension may be delivered separately from the
    sources.
    """
    if path is None:
        from pipeline import IMPORTER_NATIVE

        path = IMPORTER_NATIVE
    key = str(path)
    if key in _native_cache:
        return _native_cache[key]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = "missing"
    _native_cache[key] = digest
    return digest



def importer_python_sha256() -> str:
    """Hash of the importer's PYTHON wrapper, not just its native module.

    Closes a gap that silently retained broken output. The importer is a
    Python package plus a compiled extension, and a pure-Python change to the
    package leaves the native binary untouched -- so a step whose signature
    covers only the native cannot see it.

    That is not hypothetical. A recursion bug fixed in
    cod_asset_importer/importer.py left every material import failing; the
    markers for worldmodels, shellcasing and projectiles stayed valid because
    the .pyd had not changed, the resume logic skipped all three, and the
    package kept 73 texture PNGs' worth of broken output from the previous
    run. Only `props` happened to list importer.py in its signature_files.
    """
    try:
        from pipeline import IMPORTER_PY_ROOT

        package = IMPORTER_PY_ROOT / "cod_asset_importer"
        digest = hashlib.sha256()
        for source in sorted(package.glob("*.py")):
            digest.update(source.name.encode("utf-8"))
            digest.update(source.read_bytes())
        return digest.hexdigest()
    except (ImportError, OSError):
        return "missing"


def toolchain_for(step_key: str) -> dict:
    """The toolchain facts `step_key`'s output actually depends on."""
    facts: dict[str, str] = {}
    if step_key in BLENDER_STEPS:
        facts["blenderVersion"] = blender_version()
    if step_key in IMPORTER_STEPS:
        facts["importerVersion"] = importer_version()
        facts["importerNativeSha256"] = importer_native_sha256()
        # The wrapper too: a pure-Python importer change leaves the native
        # untouched, and without this the step would not re-run.
        facts["importerPythonSha256"] = importer_python_sha256()
    if step_key not in BLENDER_STEPS:
        # Host steps run in our own interpreter against numpy and Pillow;
        # the maps step alone is 229 KB of numpy-heavy BSP work, and a numpy
        # integer-promotion change is exactly the class of thing that moves
        # bytes without moving any source file.
        facts["interpreterVersion"] = ".".join(
            str(part) for part in sys.version_info[:3])
        facts["numpyVersion"] = _module_version("numpy")
        facts["pillowVersion"] = _module_version("PIL")
    return facts
