#!/usr/bin/env python3
"""PyInstaller entry point. Also the tool runner and the self-test.

A frozen build has no separate interpreter to spawn, so `sys.executable` is
the bundle itself and every host step re-execs THIS binary with
`--fod-run-tool <script> ...`. The dispatch below turns that back into an
ordinary script invocation.

`runpy.run_path(..., run_name="__main__")` is load-bearing rather than
stylistic. 28 of the 50 files in tools/ have no `main()`: they bind
argv-derived values at module scope and do the work at import time (see
tools/export_cod1_demo_viewmodels.py, which reads argv at :37 and exports at
:864). Importing such a module a second time in the same process would
silently reuse the first call's arguments and produce the wrong output with
no error. A fresh process plus run_path gives every one of them exactly the
semantics it has today, with zero changes to the tools themselves.

Order matters: the dispatch has to run before anything imports the exporter
GUI, or a tool step would pay tkinter's import cost 12 times per export.
"""

from __future__ import annotations

import os
import pathlib
import runpy
import sys

RUN_TOOL_FLAG = "--fod-run-tool"
SELFTEST_FLAG = "--fod-selftest"
PROBE_BLENDER_FLAG = "--fod-probe-blender"
PROBE_IMPORTER_FLAG = "--fod-probe-importer"
DETECT_COD_FLAG = "--fod-detect-cod"

EXPORTER_DIR = pathlib.Path(__file__).resolve().parent


def payload_root() -> pathlib.Path:
    """Directory holding the loose payload: the bundle dir, or exporter/."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return EXPORTER_DIR


def _run_tool(argv: list[str]) -> int:
    """`--fod-run-tool <script> [args...]` — execute one pipeline tool."""
    tool = pathlib.Path(argv[0]).resolve()
    if not tool.is_file():
        print(f"fod: no such tool: {tool}", file=sys.stderr)
        return 2
    # Rebuild argv exactly as a direct `python <tool> args...` would leave it.
    sys.argv = [str(tool), *argv[1:]]
    # tools/ modules import their siblings by bare name (cod1_archive_policy,
    # fod_export_common, ...), which only resolves because a direct
    # invocation puts the script's directory at sys.path[0].
    sys.path.insert(0, str(tool.parent))
    runpy.run_path(str(tool), run_name="__main__")
    return 0


def _selftest(argv: list[str]) -> int:
    """In-payload assertions. Ships, because it is also the support tool.

    Everything here is checkable without a Call of Duty install, which is
    what lets CI run it. `--provision` additionally exercises the real
    Blender download; it is opt-in because it moves ~350 MB.
    """
    provision = "--provision" in argv
    failures: list[str] = []

    def check(label: str, function) -> None:
        try:
            detail = function()
        except Exception as error:  # a selftest must report, never crash
            failures.append(f"{label}: {error}")
            print(f"FAIL {label}: {error}")
        else:
            print(f"OK   {label}" + (f" — {detail}" if detail else ""))

    def imports() -> str:
        import numpy
        import PIL
        import PIL.Image
        import ssl  # noqa: F401 — provisioner dependency, unused elsewhere
        import lzma  # noqa: F401 — Linux tar.xz, unused elsewhere
        import tkinter  # noqa: F401

        return f"numpy {numpy.__version__}, Pillow {PIL.__version__}"

    def gui() -> str:
        """Importing tkinter is not evidence that a window can open.

        A frozen build resolves `_tkinter.pyd` from `_internal` happily and
        then dies inside Tk_Init because `_tcl_data`/`_tk_data` were not
        collected, so `init.tcl` is missing. That failure surfaces only when
        a player double-clicks the exe -- and `main()` treats a broken GUI as
        a reason to fall back to `--cli`, which immediately exits complaining
        that `--game-dir` is required. The player sees a console blink and
        nothing else. So build a real root here and tear it down again.
        """
        import tkinter

        root = tkinter.Tk()
        try:
            root.withdraw()
            return f"Tk {root.tk.call('info', 'patchlevel')}"
        finally:
            root.destroy()

    def importer() -> str:
        """The same gate the GUI's first screen puts in front of the player.

        Worth asserting here precisely because it is not on the --cli path: a
        headless export verifies the importer inside Blender, so every CLI test
        can pass while a double-clicked exe refuses to leave Step 1. This calls
        what the requirements screen calls, so a payload that would strand a
        player fails the build instead.
        """
        import build_importer

        ok, message = build_importer.check(require_lod=True)
        if not ok:
            raise RuntimeError(message)
        return message

    def codecs() -> str:
        """Pillow discovers plugins by scanning; a frozen build can lose them.

        A missing DdsImagePlugin passes `import PIL` and then fails at step
        17 of a player's export, minutes in.
        """
        from PIL import Image

        Image.init()
        wanted = {"DDS", "TGA", "JPEG", "PNG"}
        missing = wanted - set(Image.ID)
        if missing:
            raise RuntimeError(f"missing Pillow codecs: {sorted(missing)}")
        return ", ".join(sorted(wanted))

    def certificates() -> str:
        import certifi

        path = pathlib.Path(certifi.where())
        if not path.is_file():
            raise RuntimeError(f"CA bundle missing at {path}")
        return f"certifi {path.stat().st_size} bytes"

    def pin() -> str:
        import blender_provisioner

        entry = blender_provisioner.target_pin()
        return f"{blender_provisioner.pinned_version_label()} {entry['target']}"

    check("frozen imports", imports)
    check("GUI toolkit", gui)
    check("importer probe", importer)
    check("Pillow codecs", codecs)
    check("CA bundle", certificates)
    check("Blender pin", pin)

    if provision:
        def provisioned() -> str:
            import blender_provisioner

            executable = blender_provisioner.resolve(log=print)
            report = blender_provisioner.probe(executable)
            blender_provisioner._require_usable(executable, report)
            return str(executable)

        check("Blender provision + probe", provisioned)
    else:
        print("SKIP Blender provision (pass --provision to download)")

    if failures:
        print(f"\n{len(failures)} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


def _probe_blender(argv: list[str]) -> int:
    import blender_provisioner

    try:
        executable = blender_provisioner.resolve(
            pathlib.Path(argv[0]) if argv else None,
            log=lambda message: print(message, file=sys.stderr),
        )
    except blender_provisioner.ProvisionError as error:
        print(error, file=sys.stderr)
        return 1
    print(executable)
    return 0


def _probe_importer(argv: list[str]) -> int:
    """`--fod-probe-importer <path>` — print the extension's LOD API version.

    build_importer.artifact_lod_api_version loads the native module in a child
    process on purpose: a .pyd built for another interpreter can abort the host
    outright rather than raise. It used to spawn `sys.executable -c <probe>`,
    which is correct from a checkout and impossible from a frozen build, where
    sys.executable is this binary and argparse rejects `-c`. The requirements
    screen then reported the shipped importer as missing and refused to let the
    player continue -- on the GUI path only, because a --cli export verifies the
    importer inside Blender instead and never ran this.

    Re-executing ourselves keeps the isolation and needs no interpreter.
    """
    import importlib.util

    if not argv:
        print("fod: --fod-probe-importer needs a path", file=sys.stderr)
        return 2
    path = pathlib.Path(argv[0]).resolve()
    spec = importlib.util.spec_from_file_location(
        "_fod_probe.cod_asset_importer", path
    )
    if spec is None or spec.loader is None:
        print(f"fod: cannot load {path}", file=sys.stderr)
        return 1
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print(getattr(module, "XMODEL_LOD_API_VERSION", 0))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Before ANY dispatch. The payload's .py files ship loose beside the
    # binary -- that is what lets _step_signature hash them -- and several of
    # them are only reachable this way: build_stamp carries the Blender pin,
    # and without it on the path the provisioner falls back to a packaging/
    # directory that exists in a checkout and not in a shipped payload.
    # Putting it first also guarantees the loose module wins over any copy
    # PyInstaller happened to freeze, so what runs is what was hashed.
    root = str(payload_root())
    if root not in sys.path:
        sys.path.insert(0, root)

    if argv and argv[0] == RUN_TOOL_FLAG:
        return _run_tool(argv[1:])
    if argv and argv[0] == SELFTEST_FLAG:
        return _selftest(argv[1:])
    if argv and argv[0] == PROBE_BLENDER_FLAG:
        return _probe_blender(argv[1:])
    if argv and argv[0] == PROBE_IMPORTER_FLAG:
        return _probe_importer(argv[1:])
    if argv and argv[0] == DETECT_COD_FLAG:
        import cod_autodetect

        found = cod_autodetect.detect()
        if found is None:
            return 1
        print(found)
        return 0

    import friends_of_duty_exporter

    return friends_of_duty_exporter.main()


if __name__ == "__main__":
    # The dispatch must not be reached with a stale FOD_* protocol flag from
    # a parent that has since exited; children are always given a fresh one.
    os.environ.setdefault("PYTHONHASHSEED", "0")
    raise SystemExit(main())
