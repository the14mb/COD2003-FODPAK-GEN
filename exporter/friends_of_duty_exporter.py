#!/usr/bin/env python3
"""Friends of Duty content exporter.

Generates a fodpak content package from the player's own Call of Duty 1
(+ United Offensive) install (Docs/CONTENT_PIPELINE.md). Runs as a tkinter
GUI by default; pass --cli for headless operation. The game launches this
app with `--output <persistentDataPath>/Content/current`.
"""

from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import threading
import webbrowser
from dataclasses import replace
from pathlib import Path

EXPORTER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXPORTER_DIR))

import blender_provisioner as fod_blender
import build_importer as fod_build_importer
import fod_install
import fod_paths
import package as fod_package
import pipeline as fod_pipeline
import progress as fod_progress
from cod1_archive_policy import (  # imported through pipeline's tools path
    COD1_TIER,
    UO_TIER,
    OfficialArchiveError,
    official_archives,
    uo_installation_status,
)

MIN_PYTHON = (3, 10)
#: Only the pinned 4.5.x line is accepted. Blender is no longer something the
#: player installs — blender_provisioner downloads exactly one build — so this
#: bound now exists for developer checkouts and for a --blender override.
#: A ceiling is not optional: blender.org serves 5.2 LTS today, and 4.2 vs 4.5
#: on identical input produce measurably different GLBs (84,628 accessors vs
#: 53,162 in players/*/player.glb), so "new enough" was never a safe test.
MIN_BLENDER = (4, 5, 0)
MAX_BLENDER = (5, 0)
APP_TITLE = "Friends of Duty — Content Exporter"
STEAM_URL = "https://store.steampowered.com/app/4480880/Friends_of_Duty/"

# The exporter's palette, taken off the store art: near-black ground, steel
# from the star, and status colours picked to stay legible on a dark panel
# rather than the ttk defaults, which are chosen for a white one.
INK = "#0b0d0f"          # the band, and what header.png fades into
PANEL = "#14171a"
FIELD = "#1c2126"
LINE = "#2b3238"
TEXT = "#d7dce0"
MUTED = "#8b949e"
DISABLED = "#5a626a"
STEEL = "#6d7982"
BUTTON = "#232a30"
BUTTON_HOVER = "#2e373f"
LINK = "#9fb4c4"
LINK_HOVER = "#d7e4ee"
STATUS_OK = "#7bd88f"
STATUS_WARN = "#ffc46b"
STATUS_FAIL = "#ff8080"
FOCUS = "#9fb4c4"
BAND_HEIGHT = 120

# Steam Deck is 1280x800, and Gaming Mode runs a window full-screen at that
# size, so the layout is built for 16:10 at exactly those numbers rather than
# scaled down from a desktop shape. The minimum keeps 16:10 as the window
# shrinks; below this the step list starts clipping.
WINDOW_SIZE = "1280x800"
WINDOW_MIN = (1120, 700)
#: Button padding. A Deck player drives the cursor with a thumbstick or
#: trackpad, where a 6px-tall target is genuinely hard to hit.
TOUCH_PAD = (18, 11)


# ---------------------------------------------------------------- requirements

def python_ok() -> tuple[bool, str]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < MIN_PYTHON:
        return False, f"Python {version} — 3.10 or newer required"
    return True, f"Python {version}"


def module_ok(name: str) -> tuple[bool, str]:
    if importlib.util.find_spec(name) is not None:
        return True, f"{name} installed"
    return False, f"{name} not installed"


def blender_candidates() -> list[Path]:
    candidates: list[Path] = []
    mac_app = Path("/Applications/Blender.app/Contents/MacOS/Blender")
    if mac_app.is_file():
        candidates.append(mac_app)
    which = shutil.which("blender")
    if which:
        candidates.append(Path(which))
    for pattern in (
        "C:/Program Files/Blender Foundation/*/blender.exe",
        str(Path.home() / "Applications/Blender.app/Contents/MacOS/Blender"),
    ):
        candidates.extend(Path(hit) for hit in glob.glob(pattern))
    for path in (Path("/usr/bin/blender"), Path("/snap/bin/blender")):
        if path.is_file():
            candidates.append(path)
    unique: list[Path] = []
    for candidate in candidates:
        if candidate.is_file() and candidate not in unique:
            unique.append(candidate)
    return unique


def blender_version(path: Path) -> tuple[int, ...] | None:
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "Blender":
            try:
                return tuple(int(piece) for piece in parts[1].split(".")[:3])
            except ValueError:
                return None
    return None


# One implementation of the wire format, in progress.py, shared by the
# provisioner's callback and the pipeline's step reporting.
emit_prepare = fod_progress.emit_prepare


def find_blender(manual: Path | None, *,
                 log=print,
                 cancel=None,
                 allow_download: bool = True) -> tuple[Path | None, str]:
    """Make the pinned Blender available, downloading it if necessary.

    Named `find_` for continuity, but under the runtime-fetch design this is
    the step that *acquires* Blender rather than hunting for one the player
    installed. There is no PATH search and no /Applications scan: adopting
    whatever Blender happens to be on the machine is exactly the failure the
    pin exists to prevent.

    Returns (path, message); path is None when Blender could not be made
    available, and the message is written to be shown to a player.
    """
    try:
        blender = fod_blender.resolve(
            manual,
            progress=emit_prepare,
            log=log,
            cancel=cancel,
            allow_download=allow_download,
        )
    except fod_blender.ProvisionCancelled:
        raise
    except fod_blender.ProvisionError as error:
        return None, str(error)
    return blender, f"Blender {fod_blender.pinned_version_label()} — {blender}"


def blender_status() -> tuple[bool, str]:
    """Non-downloading status line for the requirements screen."""
    return fod_blender.status()


def importer_addon_status() -> tuple[bool, str]:
    """Developer-checkout convenience; the shipped path uses
    blender_provisioner.importer_status(), which asks Blender instead."""
    return fod_build_importer.check(require_lod=True)


def validate_game_dir(path: Path) -> tuple[bool, str, bool]:
    """Pre-flight check for the chosen Call of Duty folder.

    Both tiers are required: United Offensive supplies the smoke grenade and
    two of the seven maps, so an install without it cannot produce a package
    the game will mount. Reported here so the Start button stays disabled
    with a reason, rather than letting the run begin and fail.
    """
    try:
        main_pk3 = official_archives(path, COD1_TIER)
    except OfficialArchiveError as error:
        return False, str(error), False
    ok, uo_message = uo_installation_status(path)
    if not ok:
        return False, uo_message.replace("\n", " "), False
    uo_pk3 = official_archives(path, UO_TIER, required=False)
    return (
        True,
        f"Main: {len(main_pk3)} official pk3, UO: {len(uo_pk3)} official pk3",
        True,
    )


def pip_install_argv() -> list[str]:
    return [sys.executable, "-m", "pip", "install", "Pillow", "numpy"]


# ----------------------------------------------------------- atomic publishing

def export_working_directory(final_directory: Path) -> Path:
    """Hidden sibling used while a package is being generated.

    The playable `current` directory is never modified until the final
    package validator succeeds, so cancelling Blender cannot strand the game
    with a half-upgraded schema.
    """
    return (
        final_directory.parent
        / f".{final_directory.name}.exporting"
    )


def seed_working_directory(
    final_directory: Path,
    working_directory: Path,
    log=print,
) -> None:
    if working_directory.exists() or not final_directory.is_dir():
        return
    working_directory.parent.mkdir(parents=True, exist_ok=True)
    log(
        "Preparing a safe resumable export without changing the currently "
        "playable package…"
    )
    copy_commands: list[list[str]] = []
    if sys.platform == "darwin" and Path("/bin/cp").is_file():
        # APFS clone: copy-on-write, normally near-instant even for maps.
        copy_commands.append([
            "/bin/cp", "-cR",
            str(final_directory), str(working_directory),
        ])
    elif sys.platform.startswith("linux"):
        cp = shutil.which("cp")
        if cp:
            copy_commands.append([
                cp, "--reflink=auto", "-a",
                str(final_directory), str(working_directory),
            ])
    for command in copy_commands:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and working_directory.is_dir():
            return
        if working_directory.exists():
            shutil.rmtree(working_directory)
    shutil.copytree(final_directory, working_directory)


def promote_working_directory(
    working_directory: Path,
    final_directory: Path,
) -> None:
    if not (working_directory / "fodpak.json").is_file():
        raise fod_pipeline.PipelineError(
            "validated export has no fodpak.json; refusing to replace the "
            "playable package"
        )
    backup = (
        final_directory.parent
        / f".{final_directory.name}.previous-{os.getpid()}"
    )
    if backup.exists():
        raise fod_pipeline.PipelineError(
            f"safe-publish backup already exists: {backup}"
        )
    moved_previous = False
    try:
        if final_directory.exists():
            final_directory.rename(backup)
            moved_previous = True
        working_directory.rename(final_directory)
    except Exception:
        if (
            moved_previous
            and backup.exists()
            and not final_directory.exists()
        ):
            backup.rename(final_directory)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def run_export_pipeline(
    cfg: fod_pipeline.PipelineConfig,
    *,
    log=print,
    progress=None,
    cancel=None,
    only: list[str] | None = None,
) -> bool:
    """Run against a hidden working tree and publish after validation.

    Returns True when a complete package was promoted. Developer `--only`
    runs that omit the package step remain resumable in the working tree and
    deliberately do not alter the mounted package.
    """
    # Ahead of seed_working_directory: run_pipeline enforces this too, but by
    # then the hidden .current.exporting tree exists and a UO-less run would
    # strand a multi-gigabyte copy the user never asked for.
    fod_pipeline.require_united_offensive(cfg.game_dir)
    final_directory = cfg.content_dir.resolve()
    working_directory = export_working_directory(final_directory)
    seed_working_directory(
        final_directory,
        working_directory,
        log=log,
    )
    working_cfg = replace(cfg, content_dir=working_directory)
    fod_pipeline.run_pipeline(
        working_cfg,
        log=log,
        progress=progress,
        cancel=cancel,
        only=only,
    )
    should_promote = only is None or "package" in only
    if not should_promote:
        log(
            f"Partial export staged at {working_directory}; the playable "
            "package was not changed."
        )
        return False
    promote_working_directory(working_directory, final_directory)
    log(f"Published validated package atomically: {final_directory}")
    return True


# ------------------------------------------------------------------------ CLI

def run_cli(args: argparse.Namespace) -> int:
    core_failed = False
    for ok, message in (python_ok(), module_ok("PIL"), module_ok("numpy")):
        print(("OK   " if ok else "FAIL ") + message)
        if not ok:
            core_failed = True
            print("     fix: " + " ".join(pip_install_argv()))
    if core_failed:
        return 1
    # Blender is acquired, not required: this may download ~300-400 MB the
    # first time. It runs before the game directory is validated so that a
    # player on a broken connection learns that here rather than after
    # choosing a folder — and before anything has been written to disk.
    try:
        blender, blender_message = find_blender(args.blender)
    except fod_blender.ProvisionCancelled:
        print("Cancelled.")
        return 130
    print(("OK   " if blender else "FAIL ") + blender_message)
    if blender is None:
        return 1
    # Asked of Blender, which is the process that actually loads the
    # extension. The exporter never imports it: under PyInstaller
    # sys.executable is this binary, so the old `sys.executable -c` probe fed
    # its snippet to our own argparse, and importing the GPL extension here
    # would breach the process boundary in Docs/EXPORTER_SINGLE_EXECUTABLE.md
    # section 9.1. A shipped payload arrives with the extension in place;
    # there is nothing to prepare, only something to verify.
    addon_ok, addon_message = fod_blender.importer_status(
        blender, fod_pipeline.IMPORTER_PY_ROOT)
    print(("OK   " if addon_ok else "FAIL ") + addon_message)
    if not addon_ok:
        print(
            "     Authored XModel LOD API 1 is required; the export was not "
            "started, because producing a silent LOD0-only package is not "
            "supported. In a source checkout, run `make importer-fetch`."
        )
        return 1

    if not args.game_dir:
        print("FAIL --game-dir is required in --cli mode")
        return 1
    game_dir = args.game_dir.resolve()
    valid, message, _ = validate_game_dir(game_dir)
    print(("OK   " if valid else "FAIL ") + f"game dir: {message}")
    if not valid:
        return 1

    content_dir = args.output.resolve()
    cfg = fod_pipeline.PipelineConfig(
        game_dir=game_dir,
        content_dir=content_dir,
        blender=blender,
        force=args.force,
    )
    try:
        promoted = run_export_pipeline(cfg, only=args.only)
    except fod_pipeline.PipelineError as error:
        print(f"FAIL {error}")
        return 1
    except fod_pipeline.PipelineCancelled:
        return 130
    if promoted:
        # An explicit --zip wins. Otherwise install into the game's mods
        # folder when the game is on this machine, so a headless run started
        # from the launcher finishes as playable as a GUI run does; a player
        # who never sees this window should not end up with a package they
        # then have to move by hand.
        destination = args.zip.resolve() if args.zip else None
        if destination is None and not args.no_install:
            install = fod_install.detect()
            if install is not None:
                destination = fod_install.package_path(install)
        if destination is not None:
            fod_package.write_zip(content_dir, destination)
            print(f"Installed package: {destination}")
        print(f"Content package ready: {content_dir}")
    return 0


# ------------------------------------------------------------------------ GUI

def run_gui(args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    def asset(name: str) -> Path:
        """Brand art, rendered by packaging/make_brand_assets.py."""
        return fod_paths.payload_root() / "assets" / name

    def install_theme(root: tk.Tk) -> None:
        """Dress the default grey ttk widgets in the game's palette.

        'clam' first, because the native Windows theme ignores most colour
        options -- its widgets are drawn by the OS -- so the usual result of
        theming without it is half a window turning dark.
        """
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:  # a stripped Tk; keep the default rather than fail
            return
        root.configure(bg=INK)
        style.configure(".", background=PANEL, foreground=TEXT,
                        fieldbackground=FIELD, bordercolor=LINE,
                        lightcolor=PANEL, darkcolor=PANEL,
                        focuscolor=STEEL, insertcolor=TEXT)
        style.configure("TFrame", background=PANEL)
        style.configure("Band.TFrame", background=INK)
        style.configure("TLabel", background=PANEL, foreground=TEXT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Heading.TLabel", background=PANEL, foreground=TEXT,
                        font=("Segoe UI Semibold", 15))
        style.configure("Band.TLabel", background=INK, foreground=MUTED)
        # Sized for a Steam Deck held at arm's length and driven by a stick,
        # not for a mouse pointer at a desk: TOUCH_PAD gives every button a
        # hit target a trackpad-emulated cursor can actually land on, and
        # focusthickness draws a visible ring, which is the only way a player
        # tabbing with a controller can tell where they are.
        style.configure("TButton", background=BUTTON, foreground=TEXT,
                        bordercolor=LINE, focusthickness=2,
                        focuscolor=FOCUS, padding=TOUCH_PAD,
                        font=("Segoe UI", 11))
        style.map("TButton",
                  background=[("active", BUTTON_HOVER),
                              ("focus", BUTTON_HOVER),
                              ("disabled", PANEL)],
                  foreground=[("disabled", DISABLED)])
        style.configure("Primary.TButton", background=STEEL,
                        foreground="#0b0d0f", font=("Segoe UI Semibold", 11))
        style.map("Primary.TButton",
                  background=[("active", "#7d8a94"), ("focus", "#7d8a94"),
                              ("disabled", PANEL)],
                  foreground=[("disabled", DISABLED)])
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                        bordercolor=LINE, insertcolor=TEXT, padding=7)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT,
                        focuscolor=FOCUS, padding=4)
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TProgressbar", background=STEEL, troughcolor=FIELD,
                        bordercolor=LINE, lightcolor=STEEL, darkcolor=STEEL,
                        thickness=18)
        # Treeview ignores the "." defaults entirely -- it is drawn from its
        # own element options -- so without this the step list on the export
        # screen stays a white slab in the middle of a dark window.
        style.configure("Treeview", background=FIELD, fieldbackground=FIELD,
                        foreground=TEXT, bordercolor=LINE, borderwidth=0,
                        rowheight=30, font=("Segoe UI", 10))
        style.map("Treeview",
                  background=[("selected", STEEL)],
                  foreground=[("selected", INK)])
        style.configure("Treeview.Heading", background=BUTTON, foreground=TEXT,
                        relief="flat", padding=6,
                        font=("Segoe UI Semibold", 10))
        style.map("Treeview.Heading",
                  background=[("active", BUTTON_HOVER)])
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=BUTTON,
                            troughcolor=PANEL, bordercolor=LINE,
                            arrowcolor=MUTED, lightcolor=BUTTON,
                            darkcolor=BUTTON, width=16)
            style.map(f"{orient}.TScrollbar",
                      background=[("active", BUTTON_HOVER)])

    def install_controller_navigation(root: tk.Tk) -> None:
        """Make the window usable without a mouse.

        Steam Input presents a controller to the desktop as keyboard and
        mouse, so "joystick friendly" in practice means "keyboard friendly":
        the d-pad arrives as arrow keys and A as Return or space. Tk gives Tab
        traversal and space-activates-a-button for free; what it does not give
        is arrow-key traversal or Return, which is what a player will press
        first. Bound on the toplevel so every screen inherits it.
        """
        def move(delta: int):
            def handler(event):
                widget = root.focus_get()
                if widget is None or isinstance(widget, tk.Text):
                    return None
                try:
                    nxt = widget.tk_focusNext() if delta > 0 else \
                        widget.tk_focusPrev()
                except tk.TclError:
                    return None
                if nxt is not None:
                    nxt.focus_set()
                return "break"
            return handler

        def activate(event):
            widget = root.focus_get()
            if widget is None or isinstance(widget, tk.Text):
                return None
            try:
                if str(widget.cget("state")) == "disabled":
                    return "break"
                widget.invoke()
            except (AttributeError, tk.TclError):
                return None
            return "break"

        for key in ("<Down>", "<Right>"):
            root.bind_all(key, move(1))
        for key in ("<Up>", "<Left>"):
            root.bind_all(key, move(-1))
        root.bind_all("<Return>", activate)
        root.bind_all("<KP_Enter>", activate)

    def focus_default(widget) -> None:
        """Put the caret somewhere useful when a screen appears.

        Without this a controller player lands on a window with no focus at
        all, and the first d-pad press does nothing visible.
        """
        try:
            if str(widget.cget("state")) != "disabled":
                widget.focus_set()
        except (AttributeError, tk.TclError):
            pass

    def install_icon(root: tk.Tk) -> None:
        """Taskbar and title-bar icon. Never fatal: it is decoration."""
        icon = asset("fod.ico")
        if icon.is_file():
            try:
                root.iconbitmap(default=str(icon))
                return
            except tk.TclError:
                pass
        photo = asset("fod_icon.png")
        if photo.is_file():
            try:
                root._icon_image = tk.PhotoImage(file=str(photo))
                root.iconphoto(True, root._icon_image)
            except tk.TclError:
                pass

    def build_band(root: tk.Tk) -> tk.Frame:
        """The title band: wordmark over the ruins, fading into flat INK.

        The image is left-anchored and the frame paints the same INK the image
        fades to, so widening the window extends the band with no visible seam
        instead of stretching or tiling the art.
        """
        band = tk.Frame(root, bg=INK, height=BAND_HEIGHT)
        band.pack(fill="x", side="top")
        band.pack_propagate(False)
        header = asset("header.png")
        if header.is_file():
            try:
                root._band_image = tk.PhotoImage(file=str(header))
            except tk.TclError:
                root._band_image = None
            if root._band_image is not None:
                tk.Label(band, image=root._band_image, bg=INK,
                         bd=0).place(x=0, y=0)
        return band

    def build_footer(root: tk.Tk) -> tk.Frame:
        foot = tk.Frame(root, bg=INK, height=30)
        foot.pack(fill="x", side="bottom")
        foot.pack_propagate(False)
        tk.Label(foot, text="Your own Call of Duty install stays on your machine.",
                 bg=INK, fg=MUTED).pack(side="left", padx=14)
        link = tk.Label(foot, text="Friends of Duty on Steam  ↗", bg=INK,
                        fg=LINK, cursor="hand2")
        link.pack(side="right", padx=14)
        link.bind("<Button-1>", lambda _event: webbrowser.open(STEAM_URL))
        link.bind("<Enter>", lambda _e: link.configure(fg=LINK_HOVER))
        link.bind("<Leave>", lambda _e: link.configure(fg=LINK))
        return foot

    class ExporterApp(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title(APP_TITLE)
            self.geometry(WINDOW_SIZE)
            self.minsize(*WINDOW_MIN)
            install_theme(self)
            install_controller_navigation(self)
            install_icon(self)
            build_band(self)
            build_footer(self)
            self.launch_args = args
            self.game_dir_var = tk.StringVar(
                value=str(args.game_dir) if args.game_dir else "")
            self.output_var = tk.StringVar(value=str(args.output))
            self.force_var = tk.BooleanVar(value=args.force)
            # Where the finished pak is installed. Detected rather than asked
            # for: the exporter ships beside the game, so the answer is almost
            # always known and making the player supply it is pure friction.
            self.fod_install: Path | None = fod_install.detect()
            self.package_var = tk.StringVar(
                value=str(args.zip) if args.zip
                else (str(fod_install.package_path(self.fod_install))
                      if self.fod_install else ""))
            self.blender_path: Path | None = None
            #: Set by ExportFrame.install_package once the run succeeds, and
            #: read by the done screen to say where the pak actually landed.
            self.installed_package: Path | None = None
            self.install_error: str | None = None

            container = ttk.Frame(self, padding=12)
            container.pack(fill="both", expand=True)
            self.frames = {
                "requirements": RequirementsFrame(container, self),
                "paths": PathsFrame(container, self),
                "export": ExportFrame(container, self),
                "done": DoneFrame(container, self),
            }
            for frame in self.frames.values():
                frame.grid(row=0, column=0, sticky="nsew")
            container.rowconfigure(0, weight=1)
            container.columnconfigure(0, weight=1)
            self.show("requirements")

        def show(self, name: str) -> None:
            frame = self.frames[name]
            frame.tkraise()
            refresh = getattr(frame, "on_show", None)
            if refresh:
                refresh()

        def build_config(self) -> fod_pipeline.PipelineConfig:
            return fod_pipeline.PipelineConfig(
                game_dir=Path(self.game_dir_var.get()).resolve(),
                content_dir=Path(self.output_var.get()).resolve(),
                blender=self.blender_path,
                force=self.force_var.get(),
            )

    class RequirementsFrame(ttk.Frame):
        def __init__(self, parent, app: "ExporterApp") -> None:
            super().__init__(parent)
            self.app = app
            ttk.Label(self, text="Step 1 — Requirements",
                      font=("", 16, "bold")).pack(anchor="w")
            ttk.Label(self, text=(
                "The exporter reads your own Call of Duty install and "
                "generates the game's content package. No Call of Duty "
                "content is ever downloaded — the only download is Blender "
                "itself, fetched once from blender.org when you start the "
                "export."),
                wraplength=760).pack(anchor="w", pady=(2, 10))

            self.rows = ttk.Frame(self)
            self.rows.pack(fill="x")
            self.status_labels: dict[str, ttk.Label] = {}
            for key, title in (
                ("python", "Python 3.10+"),
                ("pillow", "Pillow"),
                ("numpy", "numpy"),
                ("blender", "Blender 4.2+"),
                ("addon", "cod-asset-importer LOD API 1"),
            ):
                row = ttk.Frame(self.rows)
                row.pack(fill="x", pady=2)
                ttk.Label(row, text=title, width=26).pack(side="left")
                label = ttk.Label(row, text="…", wraplength=580, justify="left")
                label.pack(side="left", fill="x", expand=True)
                self.status_labels[key] = label

            # No Blender picker. The exporter provisions the pinned 4.5.1 build
            # itself, and offering a browse box next to a line that already says
            # it will be downloaded automatically reads as an unmet requirement:
            # players went looking for a Blender to select. Worse, any build a
            # player found would be the wrong one -- 4.2 and 4.5 emit measurably
            # different GLBs from identical input, which is the whole reason the
            # version is pinned rather than minimum-versioned. Developers who
            # need to aim at a specific build still have --blender.
            buttons = ttk.Frame(self)
            buttons.pack(fill="x", pady=12)
            # Both of these repair a source checkout, and a shipped payload
            # carries what they would install, so in a bundle they are dead
            # controls that can only ever appear greyed out beside a green
            # requirement. Build them either way so refresh() stays uniform,
            # but only show them where they can actually do something.
            self.pip_button = ttk.Button(
                buttons, text="Install Python packages (pip)",
                command=self.install_packages)
            self.build_button = ttk.Button(
                buttons, text="Prepare importer",
                command=self.build_importer)
            if not fod_paths.is_bundled():
                self.pip_button.pack(side="left")
                self.build_button.pack(side="left", padx=6)
            ttk.Button(buttons, text="Re-check",
                       command=self.refresh).pack(side="left")
            self.next_button = ttk.Button(
                buttons, text="Continue →", style="Primary.TButton",
                command=lambda: app.show("paths"))
            self.next_button.pack(side="right")
            self.pip_status = ttk.Label(self, text="", wraplength=760)
            self.pip_status.pack(anchor="w")

            self.build_running = False
            # This pane only ever shows output from the two source-checkout
            # repair buttons above, which a bundle does not offer. Packing it
            # anyway left the shipped requirements screen two thirds empty
            # black rectangle under five green lines.
            log_frame = ttk.Frame(self)
            if not fod_paths.is_bundled():
                log_frame.pack(fill="both", expand=True, pady=(6, 0))
            # Consolas rather than Menlo: Menlo does not exist on Windows, and
            # Tk silently substitutes a proportional face, which turns a log of
            # aligned paths into ragged prose.
            self.build_log = tk.Text(log_frame, height=8, state="disabled",
                                     wrap="none", font=("Consolas", 10),
                                     bg=FIELD, fg=TEXT, insertbackground=TEXT,
                                     relief="flat", highlightthickness=1,
                                     highlightbackground=LINE,
                                     selectbackground=STEEL)
            scroll = ttk.Scrollbar(log_frame, command=self.build_log.yview)
            self.build_log.configure(yscrollcommand=scroll.set)
            self.build_log.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

        def on_show(self) -> None:
            self.refresh()
            focus_default(self.next_button)

        def set_status(self, key: str, ok: bool, message: str,
                       warn_only: bool = False) -> None:
            color = STATUS_OK if ok else (STATUS_WARN if warn_only else STATUS_FAIL)
            prefix = "OK — " if ok else ("Warning — " if warn_only else "Missing — ")
            self.status_labels[key].configure(text=prefix + message, foreground=color)

        def refresh(self) -> None:
            ok_python, message = python_ok()
            self.set_status("python", ok_python, message)
            ok_pillow, message = module_ok("PIL")
            self.set_status("pillow", ok_pillow, message)
            ok_numpy, message = module_ok("numpy")
            self.set_status("numpy", ok_numpy, message)
            # Status only — never a download. This runs on every refresh, and a
            # UI refresh must not start pulling 300 MB. The acquisition happens
            # once, inside the export worker, where it has a progress bar and a
            # Cancel button.
            blender_ok, message = blender_status()
            self.set_status("blender", blender_ok, message, warn_only=False)
            addon_ok, message = importer_addon_status()
            self.set_status("addon", addon_ok, message)
            self.pip_button.configure(
                state="normal" if not (ok_pillow and ok_numpy) else "disabled")
            self.build_button.configure(
                state="normal" if not addon_ok and not self.build_running
                else "disabled")
            # blender_ok means "a pinned build exists for this platform",
            # not "it is already on disk" — the only case that blocks the
            # export is a platform with no Blender build at all (Linux ARM).
            core_ok = (
                ok_python and
                ok_pillow and
                ok_numpy and
                blender_ok and
                addon_ok
            )
            self.next_button.configure(state="normal" if core_ok else "disabled")

        def install_packages(self) -> None:
            self.pip_button.configure(state="disabled")
            self.pip_status.configure(text="Installing Pillow + numpy…")

            def worker() -> None:
                result = subprocess.run(
                    pip_install_argv(), capture_output=True, text=True)
                tail = (result.stdout + result.stderr).strip().splitlines()
                summary = tail[-1] if tail else ""
                self.after(0, lambda: self.finish_install(result.returncode, summary))

            threading.Thread(target=worker, daemon=True).start()

        def finish_install(self, returncode: int, summary: str) -> None:
            self.pip_status.configure(
                text=("pip finished: " if returncode == 0 else "pip FAILED: ") + summary)
            self.refresh()

        def append_build_log(self, line: str) -> None:
            self.build_log.configure(state="normal")
            self.build_log.insert("end", line + "\n")
            self.build_log.see("end")
            self.build_log.configure(state="disabled")

        def build_importer(self) -> None:
            self.build_running = True
            self.build_button.configure(state="disabled")
            self.append_build_log(
                "Preparing authored XModel LOD API 1. A capable bundled "
                "platform binary is used when available; the bundled v3.5 "
                "LOD0-only binaries are skipped and the vendored Rust source "
                "is built once. Local compilation needs https://rustup.rs.")

            def emit(line: str) -> None:
                self.after(0, lambda text=line: self.append_build_log(text))

            def worker() -> None:
                try:
                    fod_build_importer.build(
                        log=emit,
                        require_lod=True,
                    )
                except Exception as error:  # surfaced in the log pane
                    self.after(0, lambda: self.finish_build(str(error)))
                else:
                    self.after(0, lambda: self.finish_build(None))

            threading.Thread(target=worker, daemon=True).start()

        def finish_build(self, error: str | None) -> None:
            self.build_running = False
            if error:
                self.append_build_log("Build FAILED: " + error)
            else:
                self.append_build_log("Build finished.")
            self.refresh()

    class PathsFrame(ttk.Frame):
        def __init__(self, parent, app: "ExporterApp") -> None:
            super().__init__(parent)
            self.app = app
            ttk.Label(self, text="Step 2 — Locations",
                      font=("", 16, "bold")).pack(anchor="w")
            ttk.Label(self, text=(
                "Select your Call of Duty installation (the folder containing "
                "Main/ and, if installed, uo/) and where the content package "
                "should be written."), wraplength=760).pack(anchor="w", pady=(2, 10))

            game_row = ttk.Frame(self)
            game_row.pack(fill="x", pady=2)
            ttk.Label(game_row, text="Call of Duty folder", width=26).pack(side="left")
            ttk.Entry(game_row, textvariable=app.game_dir_var).pack(
                side="left", fill="x", expand=True)
            ttk.Button(game_row, text="Browse…",
                       command=self.browse_game).pack(side="left", padx=4)
            self.game_status = ttk.Label(self, text="", wraplength=760)
            self.game_status.pack(anchor="w", padx=(0, 0), pady=(0, 8))

            install_row = ttk.Frame(self)
            install_row.pack(fill="x", pady=2)
            ttk.Label(install_row, text="Install package to",
                      width=26).pack(side="left")
            ttk.Entry(install_row, textvariable=app.package_var).pack(
                side="left", fill="x", expand=True)
            ttk.Button(install_row, text="Browse…",
                       command=self.browse_package).pack(side="left", padx=4)
            self.install_status = ttk.Label(self, text="", wraplength=900)
            self.install_status.pack(anchor="w", pady=(0, 8))

            out_row = ttk.Frame(self)
            out_row.pack(fill="x", pady=2)
            ttk.Label(out_row, text="Working folder", width=26).pack(side="left")
            ttk.Entry(out_row, textvariable=app.output_var).pack(
                side="left", fill="x", expand=True)
            ttk.Button(out_row, text="Browse…",
                       command=self.browse_output).pack(side="left", padx=4)
            ttk.Label(self, text=(
                "The working folder holds the unpacked package while it is "
                "built. Only the file above is needed to play."),
                style="Muted.TLabel", wraplength=900).pack(anchor="w",
                                                           pady=(0, 4))

            options = ttk.Frame(self)
            options.pack(fill="x", pady=10)
            ttk.Checkbutton(options, text="Force full re-export (ignore existing outputs)",
                            variable=app.force_var).pack(anchor="w")

            buttons = ttk.Frame(self)
            buttons.pack(fill="x", pady=12)
            ttk.Button(buttons, text="← Back",
                       command=lambda: app.show("requirements")).pack(side="left")
            self.start_button = ttk.Button(
                buttons, text="Start Export →", style="Primary.TButton",
                command=self.start)
            self.start_button.pack(side="right")
            app.game_dir_var.trace_add("write", lambda *_: self.refresh())

        def on_show(self) -> None:
            self.refresh()
            focus_default(self.start_button)

        def refresh(self) -> None:
            raw = self.app.game_dir_var.get().strip()
            if not raw:
                self.game_status.configure(text="Select the install folder.",
                                           foreground=STATUS_WARN)
                self.start_button.configure(state="disabled")
                return
            valid, message, has_uo = validate_game_dir(Path(raw))
            self.app.has_uo = has_uo
            self.game_status.configure(
                text=message, foreground=STATUS_OK if valid else STATUS_FAIL)
            self.refresh_install_status()
            self.start_button.configure(state="normal" if valid else "disabled")

        def browse_game(self) -> None:
            selected = filedialog.askdirectory(title="Call of Duty installation folder")
            if selected:
                self.app.game_dir_var.set(selected)

        def refresh_install_status(self) -> None:
            install = self.app.fod_install
            if install is not None:
                self.install_status.configure(
                    text=f"Friends of Duty found at {install} — the package "
                         "will be installed for you.",
                    foreground=STATUS_OK)
            elif self.app.package_var.get().strip():
                self.install_status.configure(
                    text="Friends of Duty was not found. The package will be "
                         "written to the path above; copy it into the game's "
                         "mods folder yourself.",
                    foreground=STATUS_WARN)
            else:
                self.install_status.configure(
                    text="Friends of Duty was not found. Choose where to save "
                         "the package, then copy it into the game's mods "
                         "folder.",
                    foreground=STATUS_WARN)

        def browse_output(self) -> None:
            selected = filedialog.askdirectory(title="Content output folder")
            if selected:
                self.app.output_var.set(selected)

        def browse_package(self) -> None:
            selected = filedialog.asksaveasfilename(
                title="Install the package as",
                defaultextension=".fodpak",
                initialfile=fod_install.PACKAGE_NAME,
                filetypes=[("Friends of Duty package", "*.fodpak")])
            if selected:
                self.app.package_var.set(selected)

        def start(self) -> None:
            addon_ok, message = importer_addon_status()
            if not addon_ok:
                messagebox.showerror(
                    APP_TITLE,
                    "Authored XModel LOD support is required before export.\n\n"
                    + message,
                )
                self.app.show("requirements")
                return
            self.app.show("export")
            self.app.frames["export"].start(self.app.build_config())

    class ExportFrame(ttk.Frame):
        def __init__(self, parent, app: "ExporterApp") -> None:
            super().__init__(parent)
            self.app = app
            self.queue: queue.Queue = queue.Queue()
            self.cancel_event = threading.Event()
            self.worker: threading.Thread | None = None

            ttk.Label(self, text="Step 3 — Exporting",
                      font=("", 16, "bold")).pack(anchor="w")
            self.tree = ttk.Treeview(
                self, columns=("status",), show="tree headings", height=13)
            self.tree.heading("#0", text="Step")
            self.tree.heading("status", text="Status")
            self.tree.column("#0", width=420)
            self.tree.column("status", width=140)
            self.tree.pack(fill="x", pady=(6, 6))

            self.progress = ttk.Progressbar(self, mode="determinate")
            self.progress.pack(fill="x", pady=(0, 6))

            # Buttons first, anchored to the bottom, because the log pane below
            # expands: packed after it they were squeezed off the bottom edge
            # of the window and Cancel became unreachable mid-export.
            buttons = ttk.Frame(self)
            buttons.pack(side="bottom", fill="x", pady=(10, 0))
            self.cancel_button = ttk.Button(buttons, text="Cancel",
                                            command=self.cancel)
            self.cancel_button.pack(side="left")
            self.back_button = ttk.Button(
                buttons, text="← Back", state="disabled",
                command=lambda: app.show("paths"))
            self.back_button.pack(side="right")

            log_frame = ttk.Frame(self)
            log_frame.pack(fill="both", expand=True)
            self.log_text = tk.Text(log_frame, height=10, state="disabled",
                                    wrap="none", font=("Consolas", 10),
                                    bg=FIELD, fg=TEXT, insertbackground=TEXT,
                                    relief="flat", highlightthickness=1,
                                    highlightbackground=LINE,
                                    selectbackground=STEEL)
            scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
            self.log_text.configure(yscrollcommand=scroll.set)
            self.log_text.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

        def start(self, cfg: fod_pipeline.PipelineConfig) -> None:
            self.cancel_event.clear()
            self.cancel_button.configure(state="normal")
            self.back_button.configure(state="disabled")
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
            self.tree.delete(*self.tree.get_children())
            steps = fod_pipeline.build_steps(cfg)
            for step in steps:
                self.tree.insert("", "end", iid=step.key, text=step.title,
                                 values=("pending",))
            self.progress.configure(maximum=len(steps), value=0)

            def worker() -> None:
                emit = lambda line: self.queue.put(("log", line))
                try:
                    # Acquire Blender first, on this thread, so the download
                    # gets the log pane and the Cancel button rather than
                    # freezing the UI from a refresh handler.
                    blender, message = find_blender(
                        self.app.launch_args.blender,
                        log=emit,
                        cancel=self.cancel_event,
                    )
                    if blender is None:
                        self.queue.put(("log", message))
                        self.queue.put(("finished", message))
                        return
                    self.app.blender_path = blender
                    run_export_pipeline(
                        replace(cfg, blender=blender),
                        log=emit,
                        progress=lambda i, n, key, status: self.queue.put(
                            ("progress", i, n, key, status)),
                        cancel=self.cancel_event)
                except (fod_pipeline.PipelineCancelled,
                        fod_blender.ProvisionCancelled):
                    self.queue.put(("finished", "cancelled"))
                except Exception as error:  # surfaced in the log pane
                    self.queue.put(("log", f"ERROR {error}"))
                    self.queue.put(("finished", str(error)))
                else:
                    self.queue.put(("finished", None))

            self.worker = threading.Thread(target=worker, daemon=True)
            self.worker.start()
            self.after(100, self.poll)

        def poll(self) -> None:
            finished: tuple | None = None
            try:
                while True:
                    message = self.queue.get_nowait()
                    if message[0] == "log":
                        self.append_log(message[1])
                    elif message[0] == "progress":
                        _, index, total, key, status = message
                        if self.tree.exists(key):
                            self.tree.set(key, "status", status)
                        completed = index + (1 if status in ("done", "skipped") else 0)
                        self.progress.configure(value=completed)
                    elif message[0] == "finished":
                        finished = message
            except queue.Empty:
                pass
            if finished is not None:
                self.finish(finished[1])
            else:
                self.after(100, self.poll)

        def append_log(self, line: str) -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def cancel(self) -> None:
            self.cancel_event.set()
            self.append_log("Cancelling… current step will be terminated.")
            self.cancel_button.configure(state="disabled")

        def install_package(self) -> None:
            """Write the .fodpak where the game will mount it.

            Done here rather than offered as a button on the next screen: a
            player who has just waited twenty minutes should not then have to
            understand what a mods folder is. Failure is recorded on the app
            and reported by the done screen, never raised -- the package in
            the working folder is still perfectly good, and a full export is
            far too expensive to discard over a copy.
            """
            self.app.installed_package = None
            self.app.install_error = None
            target = self.app.package_var.get().strip()
            if not target:
                return
            destination = Path(target)
            self.append_log(f"Installing package to {destination}")
            try:
                fod_package.write_zip(
                    Path(self.app.output_var.get()).resolve(), destination)
            except Exception as error:  # noqa: BLE001 - reported, never fatal
                self.app.install_error = str(error)
                self.append_log(f"Install failed: {error}")
            else:
                self.app.installed_package = destination
                self.append_log("Installed.")

        def finish(self, error: str | None) -> None:
            self.cancel_button.configure(state="disabled")
            self.back_button.configure(state="normal")
            if error is None:
                self.progress.configure(value=self.progress["maximum"])
                self.install_package()
                self.app.show("done")
            elif error == "cancelled":
                self.append_log("Export cancelled. Progress is kept; "
                                "run again to resume.")
            else:
                self.append_log("Export failed. Fix the issue above and go "
                                "back to retry — finished steps are skipped.")

    class DoneFrame(ttk.Frame):
        def __init__(self, parent, app: "ExporterApp") -> None:
            super().__init__(parent)
            self.app = app
            ttk.Label(self, text="GET READY TO BATTLE",
                      font=("Segoe UI Semibold", 30)).pack(anchor="w",
                                                           pady=(10, 0))
            self.summary = ttk.Label(self, text="", wraplength=900,
                                     justify="left")
            self.summary.pack(anchor="w", pady=(8, 4))
            self.install_line = ttk.Label(self, text="", wraplength=900,
                                          justify="left")
            self.install_line.pack(anchor="w", pady=(0, 18))

            buttons = ttk.Frame(self)
            buttons.pack(fill="x")
            # The one thing a player wants next, sized and styled to say so.
            self.launch_button = ttk.Button(
                buttons, text="▶  Launch Friends of Duty",
                style="Primary.TButton", command=self.launch_game)
            self.launch_button.pack(side="left")
            ttk.Button(buttons, text="Save a copy…",
                       command=self.save_zip).pack(side="left", padx=10)
            ttk.Button(buttons, text="Close",
                       command=self.app.destroy).pack(side="right")
            self.zip_status = ttk.Label(self, text="", wraplength=900)
            self.zip_status.pack(anchor="w", pady=12)

        def on_show(self) -> None:
            installed = getattr(self.app, "installed_package", None)
            error = getattr(self.app, "install_error", None)
            self.summary.configure(
                text="Your Call of Duty content is ready. It never left this "
                     "machine, and the package is yours alone.",
                foreground=TEXT)
            if installed is not None:
                self.install_line.configure(
                    text=f"Installed to {installed}  —  the game will mount it "
                         "on next start.",
                    foreground=STATUS_OK)
            elif error is not None:
                self.install_line.configure(
                    text=f"The package was built, but installing it failed: "
                         f"{error}\nUse Save a copy… and place the file in the "
                         "game's mods folder.",
                    foreground=STATUS_FAIL)
            else:
                self.install_line.configure(
                    text="Use Save a copy… and place the file in the game's "
                         "mods folder.",
                    foreground=STATUS_WARN)
            # Steam can start the game whether or not we located the install,
            # so this is only disabled when there is nothing to launch at all.
            launchable = (self.app.fod_install is not None
                          or sys.platform in ("win32", "darwin"))
            self.launch_button.configure(
                state="normal" if launchable else "disabled")
            focus_default(self.launch_button)

        def save_zip(self) -> None:
            destination = filedialog.asksaveasfilename(
                title="Save content package",
                defaultextension=".fodpak",
                initialfile="friends_of_duty_content.fodpak",
                filetypes=[("Friends of Duty package", "*.fodpak")])
            if not destination:
                return
            self.zip_status.configure(text="Writing zip…")

            def worker() -> None:
                try:
                    fod_package.write_zip(
                        Path(self.app.output_var.get()).resolve(), Path(destination))
                except Exception as error:
                    self.after(0, lambda: self.zip_status.configure(
                        text=f"Zip failed: {error}"))
                else:
                    self.after(0, lambda: self.zip_status.configure(
                        text=f"Saved {destination}"))

            threading.Thread(target=worker, daemon=True).start()

        def launch_game(self) -> None:
            """Hand off to the game, through Steam where possible.

            A --game-exe passed by the launcher wins, because that is the
            build the player actually started this from. Otherwise ask Steam,
            which starts the owned copy with its overlay and cloud saves
            rather than a bare process.
            """
            game_exe = self.app.launch_args.game_exe
            if game_exe and Path(game_exe).exists():
                try:
                    if sys.platform == "darwin" and str(game_exe).endswith(".app"):
                        subprocess.Popen(["open", str(game_exe)])
                    else:
                        subprocess.Popen([str(game_exe)])
                except OSError as error:
                    messagebox.showerror(
                        APP_TITLE, f"Could not launch the game: {error}")
                    return
                self.app.destroy()
                return

            ok, detail = fod_install.launch(self.app.fod_install)
            if not ok:
                messagebox.showerror(
                    APP_TITLE,
                    "Could not launch Friends of Duty.\n\n"
                    f"{detail}\n\nStart it from Steam; the package is already "
                    "installed.")
                return
            self.app.destroy()

    ExporterApp().mainloop()
    return 0


# ----------------------------------------------------------------------- main

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cli", action="store_true", help="run headless")
    parser.add_argument("--game-dir", type=Path,
                        help="Call of Duty install (contains Main/ and optionally uo/)")
    parser.add_argument("--output", type=Path,
                        default=fod_pipeline.DEFAULT_CONTENT_DIR,
                        help="content package directory (the game passes its "
                             "persistentDataPath/Content/current)")
    parser.add_argument("--blender", type=Path, help="Blender executable override")
    parser.add_argument("--force", action="store_true",
                        help="re-run every step even when outputs exist")
    # Retired selection flags. United Offensive is mandatory and the roster is
    # fixed at seven maps, so none of these can change what is exported.
    #
    # --include-uo and --all-mp are ACCEPTED AND IGNORED rather than removed:
    # an already-installed game spawns this exporter and still passes
    # --include-uo (ExporterLauncher.cs), so erroring on it would break
    # updating from any older build. --maps is human-typed only, and silently
    # ignoring a requested subset would hand back a package the user did not
    # ask for, so that one fails loudly.
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--all-mp", action="store_true", dest="all_mp",
                        help=argparse.SUPPRESS)
    parser.add_argument(
        "--maps",
        nargs="*",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--only", nargs="*", help="run only these pipeline step keys")
    parser.add_argument("--zip", type=Path,
                        help="(cli) also write a transportable .fodpak zip")
    parser.add_argument(
        "--no-install", action="store_true",
        help="do not place the package in the detected game's mods folder")
    parser.add_argument("--game-exe", type=Path,
                        help="game executable for the 'Launch game' button")
    parser.add_argument("--game-callback", action="store_true",
                        help="passed by the game when it spawned the exporter")
    args = parser.parse_args()
    if args.maps:
        parser.error(
            "the Friends of Duty roster is fixed at seven maps "
            "(Carentan, Pavlov, Chateau, Railyard, Rocket, Arnhem, Cassino); "
            "--maps is no longer supported"
        )
    return args


def stream_stdout_live() -> None:
    """Line-buffer stdout/stderr.

    The game launches the exporter with its pipes redirected, and Python
    block-buffers a pipe: the boot-screen console would sit empty through a
    whole step and then jump 8 KB at once, which is indistinguishable from a
    hang. Line buffering costs nothing here and makes every step visible as
    it happens. Older streams without .reconfigure() just keep their default.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass


def main() -> int:
    stream_stdout_live()
    args = parse_args()
    if args.game_callback:
        # --game-callback has been accepted and ignored since the launcher
        # first passed it. It now means exactly one thing: a program, not a
        # person, is reading this stdout, so the machine-readable `@fod`
        # lines are switched on. Setting it in the environment rather than
        # threading a flag means every child step inherits it through
        # fod_paths.child_env() without touching 19 call sites.
        os.environ[fod_paths.PROGRESS_ENV] = "1"
    if args.cli:
        return run_cli(args)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter is not available; falling back to --cli mode")
        return run_cli(args)
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
