#!/usr/bin/env python3
"""Find the installed Friends of Duty, and launch it.

The exporter ships beside the game and is started from its launcher, next to
PLAY GAME and DEDICATED SERVER. So the finish line is not "a package exists
somewhere on disk", it is "the package is in the game's mods folder and the
player can press Play". Everything here exists to remove the steps in
between: the player should never have to know where the game lives, what a
mods folder is, or where the file they just built went.

Detection reuses cod_autodetect's Steam plumbing -- the registry lookup and
the libraryfolders.vdf parser -- because "where did Steam put a game" is the
same question for both titles and there should be one answer to it.

    python3 exporter/fod_install.py        # prints the install, or exits 1
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

import cod_autodetect

#: The Steam application, from the store URL.
STEAM_APP_ID = "4480880"

#: Folder name Steam installs into, plus what a manual or dev copy looks like.
INSTALL_DIR_NAMES = (
    "Friends of Duty",
    "FriendsOfDuty",
)

#: Where the game mounts packages from, relative to the install root.
MODS_DIRNAME = "mods"

#: What this tool's package is called. Fixed rather than chosen by the player:
#: it is the CoD 2003 pak, and a predictable name means re-running the exporter
#: replaces the previous one instead of accumulating copies the game all mounts.
PACKAGE_NAME = "cod1.fodpak"


def _steam_libraries() -> list[Path]:
    libraries: list[Path] = []
    for root in cod_autodetect._steam_roots():
        for library in cod_autodetect._library_folders(root):
            if library not in libraries:
                libraries.append(library)
    return libraries


def _from_app_manifest(library: Path) -> Path | None:
    """Read installdir out of appmanifest_<id>.acf.

    More reliable than guessing the folder name: Steam records the directory
    it actually used, which is what a renamed or localised install will have.
    """
    manifest = library / "steamapps" / f"appmanifest_{STEAM_APP_ID}.acf"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"installdir"\s+"([^"]+)"', text)
    if not match:
        return None
    candidate = library / "steamapps" / "common" / match.group(1)
    return candidate if candidate.is_dir() else None


def looks_like_install(path: Path) -> bool:
    """Cheap structural test for a Friends of Duty directory.

    Accepts either the shipped game or a development build: both carry an
    executable and a Unity data directory beside it. Deliberately loose --
    the cost of a false positive is a pre-filled path the player can change,
    while a false negative sends them hunting for a mods folder by hand.
    """
    if not path.is_dir():
        return False
    for pattern in ("FriendsOfDuty*.exe", "FriendsOfDuty*.app",
                    "FriendsOfDuty*.x86_64", "*_Data"):
        try:
            if any(path.glob(pattern)):
                return True
        except OSError:
            return False
    return (path / MODS_DIRNAME).is_dir()


def candidates() -> list[Path]:
    found: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except (OSError, RuntimeError):
            return
        if resolved not in found:
            found.append(resolved)

    override = os.environ.get("FOD_GAME_DIR")
    if override:
        add(Path(override))

    for library in _steam_libraries():
        add(_from_app_manifest(library))
        for name in INSTALL_DIR_NAMES:
            add(library / "steamapps" / "common" / name)

    # Beside the exporter: the shipped layout puts this tool in a subdirectory
    # of the game, so the game is usually one or two levels up.
    probe = Path(__file__).resolve().parent
    for _ in range(4):
        add(probe)
        if probe.parent == probe:
            break
        probe = probe.parent

    return found


def detect() -> Path | None:
    for candidate in candidates():
        if looks_like_install(candidate):
            return candidate
    return None


def mods_dir(install: Path) -> Path:
    return install / MODS_DIRNAME


def package_path(install: Path) -> Path:
    return mods_dir(install) / PACKAGE_NAME


def launch(install: Path | None = None) -> tuple[bool, str]:
    """Start the game, preferring Steam so it runs as an owned copy.

    steam:// rather than the executable because launching the binary directly
    bypasses Steam entirely -- no overlay, no cloud saves, no playtime, and on
    some configurations the game refuses to start at all. The direct
    executable stays as the fallback for a non-Steam build.
    """
    url = f"steam://rungameid/{STEAM_APP_ID}"
    try:
        if sys.platform == "win32":
            os.startfile(url)  # noqa: S606 - a URL handler, not a shell string
            return True, url
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
            return True, url
        if webbrowser.open(url):
            return True, url
    except OSError as error:
        last = str(error)
    else:
        last = "Steam did not respond"

    if install is not None:
        for pattern in ("FriendsOfDuty*.exe", "FriendsOfDuty*.x86_64"):
            for binary in sorted(install.glob(pattern)):
                try:
                    subprocess.Popen([str(binary)])
                    return True, str(binary)
                except OSError as error:
                    last = str(error)
    return False, last


def main() -> int:
    found = detect()
    if found is None:
        print("no Friends of Duty install found", file=sys.stderr)
        return 1
    print(found)
    print(f"mods: {mods_dir(found)}")
    print(f"package: {package_path(found)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
