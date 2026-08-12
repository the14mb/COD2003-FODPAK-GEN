#!/usr/bin/env python3
"""Where the exporter is, and where it is allowed to write.

One module owns every path question that changes between a developer's
checkout and a shipped bundle, so nothing else has to ask `sys.frozen`.

The single most important rule here: **nothing the exporter downloads is
ever written inside the application**. On macOS that would invalidate the
code signature of `Friends of Duty Exporter.app` (measured: Blender writing
its own `__pycache__` already breaks the seal of a stock `/Applications/
Blender.app`, and `spctl` then refuses it). On every platform it would put
roughly a gigabyte of files beside the game that Steam's manifest does not
know about, which is a "Verify integrity of game files" hazard - the depot
`FileExclusion` lines cover `Content/*` and nothing else.

See Docs/EXPORTER_SINGLE_EXECUTABLE.md sections 4.4 and 6.2.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

EXPORTER_DIR = Path(__file__).resolve().parent

#: Marks every child process the exporter spawns, so tools can tell a
#: shipped run from a developer's checkout without inspecting argv.
BUNDLED_ENV = "FOD_BUNDLED"
#: Set when the parent was invoked with --game-callback. Children emit the
#: machine-readable `@fod` progress protocol only when this is "1"; without
#: it their stdout stays byte-identical to what a human sees today.
PROGRESS_ENV = "FOD_PROGRESS_PROTOCOL"
#: Player-supplied Blender override, honoured ahead of the managed cache.
BLENDER_ENV = "FOD_BLENDER"
#: Test/CI hook: relocates the managed Blender cache wholesale.
BLENDER_CACHE_ENV = "FOD_BLENDER_CACHE"

APP_DIR_NAME = "Friends of Duty"


def is_bundled() -> bool:
    """True inside a PyInstaller build, false in a source checkout."""
    return bool(getattr(sys, "frozen", False))


def self_exe() -> str:
    """The frozen executable.

    Under PyInstaller `sys.executable` is the bundle itself rather than an
    interpreter, which is exactly what the `--fod-run-tool` re-exec wants:
    host steps run as `argv[0] --fod-run-tool <tool.py> ...` instead of
    `python tool.py ...`, because there is no separate python to call.
    """
    return sys.executable


def payload_root() -> Path:
    """Directory holding the loose exporter sources.

    In a checkout that is `exporter/`. In a bundle it is the directory the
    launcher sits in - the `.py` files ship as readable source beside the
    binary so `_step_signature` can keep hashing them, which is what makes a
    one-line exporter fix invalidate one step instead of the whole package.
    """
    if is_bundled():
        return Path(self_exe()).resolve().parent
    return EXPORTER_DIR


def user_data_root() -> Path:
    """Per-user, writable, and outside both the game and the app bundle."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_DIR_NAME


def blender_cache_root() -> Path:
    """Parent of every managed Blender install.

    Version-keyed one level down (`<root>/4.5.1/`), so bumping the pin
    downloads beside the old tree rather than over it: a failed migration
    can never leave a half-replaced Blender that looks complete, and the
    previous version is removed only once the new one has passed its
    integrity probe.
    """
    override = os.environ.get(BLENDER_CACHE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return user_data_root() / "Blender"


def _drop(env: dict[str, str], *names: str) -> None:
    for name in names:
        env.pop(name, None)


def child_env(kind: str) -> dict[str, str]:
    """Environment for a spawned step. `kind` is 'host' or 'blender'.

    Starts from a copy of our own environment rather than an empty dict:
    stripping it would remove PATH, HOME, TEMP, DISPLAY and - on Windows -
    SystemRoot, without which subprocesses simply fail.

    The two kinds have opposite needs. Our re-exec'd frozen children *want*
    PyInstaller's bootloader variables; Blender must never see them, or it
    resolves our bundled CPython and OpenSSL instead of its own.
    """
    env = os.environ.copy()
    env[BUNDLED_ENV] = "1" if is_bundled() else "0"
    # Iteration order of a set of strings varies per process without this,
    # so any tool deriving output from an unsorted set would produce
    # different bytes run to run and make a reference diff uninterpretable.
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    if kind == "blender":
        # Deleted, never restored from PyInstaller's *_ORIG copies: on Linux
        # the game's own launcher prepends the game directory and Steam's
        # runtime to LD_LIBRARY_PATH before we are started, so *_ORIG holds
        # an already-poisoned value. Handing that to Blender points it at
        # libsteam_api.so and, under Steam Runtime 1.0 'scout', at a
        # 2012-vintage libstdc++ it will refuse with a GLIBCXX error.
        # Blender resolves its own libraries through its $ORIGIN RPATH.
        _drop(env, "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
              "LD_PRELOAD", "DYLD_INSERT_LIBRARIES")
        _drop(env, "_MEIPASS", "_MEIPASS2", "_PYI_ARCHIVE_FILE",
              "_PYI_APPLICATION_HOME_DIR")
        _drop(env, "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "OCIO",
              "BLENDER_USER_SCRIPTS", "BLENDER_USER_CONFIG",
              "BLENDER_USER_EXTENSIONS", "BLENDER_SYSTEM_SCRIPTS",
              "BLENDER_SYSTEM_RESOURCES", "BLENDER_USER_DATAFILES")
        # Blender writes bytecode into its own installation on first run.
        # Inside a signed macOS bundle that breaks the seal and `spctl`
        # starts refusing it; in the managed cache it merely invalidates the
        # signature check the provisioner recorded at install time. Cheaper
        # to prevent than to re-verify.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    else:
        _drop(env, "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")

    return env


def progress_protocol_enabled() -> bool:
    """True when a machine-readable consumer is reading our stdout."""
    return os.environ.get(PROGRESS_ENV) == "1"
