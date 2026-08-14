#!/usr/bin/env python3
"""Find the player's Call of Duty install without asking them.

The game already does a version of this in
Assets/Scripts/Content/ExporterLauncher.cs, and the two must not drift: if
the game says it found an install and the exporter then opens a folder
picker, nobody can tell which one is wrong. `--fod-detect-cod` exposes this
module so the game can call it instead of keeping a second implementation,
and the exporter's own Paths screen pre-fills from the same answer.

"Requires nothing from the player" is only true to the extent this works —
every miss falls back to a folder picker, which on Steam Deck Gaming Mode is
not reachable at all. So the search is deliberately broad: Steam's own
library index first (it knows where the player actually put their games),
then the conventional install locations, then the directories beside the
game itself, which is what a dev checkout and a portable install look like.

A candidate only counts if it validates as a real install, so a leftover
empty "Call of Duty" folder never shadows the real one.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

#: Steam's own name for the app, as it appears under steamapps/common.
STEAM_DIR_NAMES = (
    "Call of Duty",
    "Call of Duty 1",
    "Call of Duty Game of the Year Edition",
)

#: How far up from the exporter to look for an install sitting beside the
#: repo or the game. Mirrors ExporterLauncher.DevSearchDepth.
NEARBY_SEARCH_DEPTH = 6


def _steam_root_from_registry() -> Path | None:
    """Where Steam actually is, according to Windows.

    The guesses below only cover Steam installed somewhere conventional. This
    is the authoritative answer, and it matters most for exactly the player
    this tool is hardest for: someone who put Steam on a second drive, whose
    library index we then never read, whose games we then never find, and who
    is then handed a folder picker -- on Steam Deck Gaming Mode, a folder
    picker is barely usable with a controller.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive, key in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                for value in ("SteamPath", "InstallPath"):
                    try:
                        raw, _kind = winreg.QueryValueEx(handle, value)
                    except OSError:
                        continue
                    path = Path(str(raw).replace("/", "\\"))
                    if path.is_dir():
                        return path
        except OSError:
            continue
    return None


def _steam_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()
    if sys.platform == "win32":
        registered = _steam_root_from_registry()
        if registered is not None:
            roots.append(registered)
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(variable)
            if base:
                roots.append(Path(base) / "Steam")
        for letter in "CDEFG":
            roots.append(Path(f"{letter}:/Steam"))
            roots.append(Path(f"{letter}:/SteamLibrary"))
    elif sys.platform == "darwin":
        roots.append(home / "Library" / "Application Support" / "Steam")
    else:
        roots.append(home / ".steam" / "steam")
        roots.append(home / ".local" / "share" / "Steam")
        # Flatpak Steam, which is how a lot of Linux players install it.
        roots.append(
            home / ".var" / "app" / "com.valvesoftware.Steam" / ".local"
            / "share" / "Steam")
    return [root for root in roots if root.is_dir()]


def _library_folders(steam_root: Path) -> list[Path]:
    """Every library Steam knows about, from libraryfolders.vdf.

    Parsed with a regex rather than a real VDF parser on purpose: the only
    thing wanted is the quoted "path" values, the file is small, and adding
    a dependency to read four lines is not worth it. An unparseable file
    yields nothing and the conventional locations still get searched.
    """
    libraries = [steam_root]
    for relative in ("steamapps/libraryfolders.vdf",
                     "config/libraryfolders.vdf"):
        index = steam_root / relative
        try:
            text = index.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            candidate = Path(match.group(1).replace("\\\\", "/"))
            if candidate.is_dir():
                libraries.append(candidate)
    return libraries


def candidates() -> list[Path]:
    """Every place worth looking, most-likely first, de-duplicated."""
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

    override = os.environ.get("FOD_COD_GAME_DIR")
    if override:
        add(Path(override))

    for steam_root in _steam_roots():
        for library in _library_folders(steam_root):
            for name in STEAM_DIR_NAMES:
                add(library / "steamapps" / "common" / name)

    if sys.platform == "win32":
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(variable)
            if not base:
                continue
            # Every retail folder name, not just "Call of Duty": the disc
            # installer writes "Call of Duty Game of the Year Edition", which
            # was previously reached only by the bounded drive scan below and
            # so depended on that scan's ordering and budget. Naming the exact
            # paths makes the common case deterministic and instant.
            for name in STEAM_DIR_NAMES:
                add(Path(base) / name)
                add(Path(base) / "Steam" / "steamapps" / "common" / name)
            add(Path(base) / "Activision" / "Call of Duty")
        for candidate in _windows_drive_candidates():
            add(candidate)
    elif sys.platform == "darwin":
        add(Path("/Applications/Call of Duty"))
        add(Path.home() / "Applications" / "Call of Duty")
    else:
        add(Path.home() / "CallOfDuty")
        add(Path.home() / "Games" / "Call of Duty")

    # Beside the exporter, then walking up: a dev checkout keeps the install
    # one directory above the repo, and a portable install sits next to the
    # game. Same shape as ExporterLauncher.AddNearbyGameDirectories.
    probe = Path(__file__).resolve().parent
    for _ in range(NEARBY_SEARCH_DEPTH):
        add(probe)
        add(probe / "Call of Duty")
        if probe.parent == probe:
            break
        probe = probe.parent

    return found



#: Parents worth looking under on each fixed drive. A player who keeps games
#: off C: almost always uses one of these, and the alternative -- walking whole
#: drives -- is unacceptably slow on a spinning disk.
WINDOWS_GAME_PARENTS = (
    "",
    "Games",
    "Program Files",
    "Program Files (x86)",
    "SteamLibrary/steamapps/common",
    "steamapps/common",
    "GOG Games",
    "GOG Galaxy/Games",
)

#: How far below each parent to look. Two, because retail Call of Duty is
#: routinely unpacked into a wrapper directory: the install verified on the
#: Windows build machine is E:\Games\Call Of Duty 1 & UO\Call of Duty,
#: which is a grandchild of the parent, not a child.
WINDOWS_SEARCH_DEPTH = 2

#: Bound on directories examined, so a drive with thousands of game folders
#: cannot turn detection into a visible stall.
WINDOWS_SCAN_LIMIT = 4000


def _windows_drive_roots() -> list[Path]:
    """Every fixed drive letter that currently exists."""
    roots = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:/")
        try:
            if root.is_dir():
                roots.append(root)
        except OSError:
            continue
    return roots


def _windows_drive_candidates() -> list[Path]:
    """Bounded scan of the usual game locations on every fixed drive.

    Without this, a second-drive install is simply not found and the player
    falls through to a folder picker -- which on a Steam Deck-style
    controller-only session may not be reachable at all, and which is friction
    for everyone else. Detection is best-effort by nature, so every filesystem
    error here is skipped rather than raised.
    """
    found: list[Path] = []
    scanned = 0
    for root in _windows_drive_roots():
        for parent in WINDOWS_GAME_PARENTS:
            base = root / parent if parent else root
            frontier = [base]
            for _ in range(WINDOWS_SEARCH_DEPTH):
                nxt: list[Path] = []
                for directory in frontier:
                    if scanned >= WINDOWS_SCAN_LIMIT:
                        return found
                    try:
                        children = [c for c in directory.iterdir() if c.is_dir()]
                    except OSError:
                        continue
                    scanned += len(children)
                    for child in children:
                        if looks_like_install(child):
                            found.append(child)
                        else:
                            nxt.append(child)
                frontier = nxt
    return found


def _main_directory(path: Path) -> Path | None:
    """`Main/` however this install happened to spell it.

    Case-insensitively, because cod1_archive_policy._tier_directory already
    resolves the same directory that way and the two must agree. They did not:
    an exact-case lookup here skipped installs the policy would then happily
    accept, so on a case-sensitive filesystem -- every Linux box, so every
    Steam Deck -- a `main/` install was never detected and the player was sent
    to a folder picker. In Gaming Mode a folder picker is barely usable with a
    controller, which is the exact outcome this module exists to avoid.
    """
    exact = path / "Main"
    if exact.is_dir():
        return exact
    try:
        for child in sorted(path.iterdir()):
            if child.is_dir() and child.name.casefold() == "main":
                return child
    except OSError:
        return None
    return None


def looks_like_install(path: Path) -> bool:
    """Cheap structural test, no policy check.

    Only asks whether `Main/` holds any pk3 at all. The authoritative
    verdict — official archives present, United Offensive installed — lives
    in cod1_archive_policy and is applied by validate_game_dir; duplicating
    it here would mean two answers to the same question.
    """
    main = _main_directory(path)
    if main is None:
        return False
    try:
        return any(main.glob("*.pk3"))
    except OSError:
        return False


def detect() -> Path | None:
    """The first candidate that structurally looks like an install."""
    for candidate in candidates():
        if looks_like_install(candidate):
            return candidate
    return None


def main() -> int:
    found = detect()
    if found is None:
        print("no Call of Duty install found", file=sys.stderr)
        return 1
    print(found)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
