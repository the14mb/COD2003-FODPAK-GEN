# COD2003 fodpak generator

Turns **your own** copy of Call of Duty (2003) and United Offensive into a
`.fodpak` content package for [Friends of Duty](https://github.com/the14mb),
a mods-only FPS sandbox that ships no content of its own.

> ### You must own the game
>
> **This repository contains no Call of Duty assets, and never will.** It is
> extraction *code*. Every model, texture, sound, animation and map it produces
> comes off the installation on your machine, at the moment you run it.
>
> The package you generate is for **your personal use with your own copy**.
> Do not redistribute it. Do not ask for game data, and do not link to it —
> issues and pull requests doing either will be closed.
>
> There is no freeware fallback. Unlike Command & Conquer or Transport Tycoon,
> Activision has never released Call of Duty data for free redistribution, so
> owning CoD1 + United Offensive is the only path.

This is the same arrangement used by
[Ship of Harkinian](https://github.com/HarbourMasters/Shipwright),
[OpenRA](https://github.com/OpenRA/OpenRA),
[OpenMW](https://gitlab.com/OpenMW/openmw) and
[devilutionX](https://github.com/diasurgical/devilutionX): the tool is free and
open, the assets stay yours.

Verification is a feature, not an obstacle. The generator checks what you point
it at against known-good retail archive hashes and refuses input it does not
recognise, rather than silently producing a broken package.

---

## Minimum requirements

To run the released **standalone app**, this is the whole list. You do not need
Python, Blender, Rust, or a compiler — the app carries its own interpreter and
fetches its own Blender.

| | Minimum |
|---|---|
| **OS** | Windows 10 or 11, 64-bit. This build is Windows-only. |
| **CPU** | Any x86-64 processor. |
| **RAM** | 4 GB. A full export peaks at ~1.3 GB across the generator and Blender together, measured during the player step. |
| **Disk** | 4 GB free: ~0.9 GB Blender cache, ~0.4 GB the download it is extracted from, ~0.6 GB for the `.fodpak`, plus the unpacked package and staging. |
| **Internet** | First run only — a one-time 381 MB Blender download. Later runs are fully offline. |
| **Game** | Call of Duty **and** United Offensive, patched and installed together: all 14 retail `Main\*.pk3` and all 8 `uo\*.pk3`. A disc install without patch 1.5 / 1.51 is missing some of these and is rejected by name. |
| **Time** | About 22 minutes on an 8-thread desktop once Blender is cached, plus the one-time download on the first run. The map and player steps are three quarters of it. |

`.fodpak` packages carry nothing platform-specific, so one built on any Windows
machine works on every platform Friends of Duty runs on — including macOS and
Steam Deck, which have no exporter of their own.

Running from **source** instead of the released app additionally needs Python
3.10+ with `Pillow` and `numpy`; see [Requirements](#requirements) below.

---

## Screenshots

<!-- Screenshots to be added. Capture at 1920x1080, PNG, and drop into
     docs/screenshots/ using exactly these filenames. -->

### The exporter on first run
![The generator's GUI on first launch, before any Call of Duty install has been selected](docs/screenshots/01-first-run.png)
<!-- Should show: the GUI as it opens, with the game-directory field empty or
     auto-detected, and the Export button in its initial state. -->

### Provisioning Blender
![The generator downloading its pinned Blender build, with a progress bar](docs/screenshots/02-blender-provisioning.png)
<!-- Should show: the one-time Blender 4.5.1 download in progress, with the
     version and destination cache path visible. -->

### An export in progress
![An export running, showing the current pipeline step and overall progress](docs/screenshots/03-export-running.png)
<!-- Should show: a mid-export state with the step name visible (e.g. a map or
     player export) and the weighted progress bar partway along. -->

### A finished package
![A completed export reporting the generated fodpak and its location on disk](docs/screenshots/04-export-complete.png)
<!-- Should show: the success state, the output directory, and ideally the
     resulting .fodpak file size. -->

### The result in game
![The generated package mounted in Friends of Duty, listed on the MODS page](docs/screenshots/05-in-game.png)
<!-- Should show: the game's MODS page with the generated package listed and
     enabled — proof the round trip works. -->

---

## Quick start

### Windows — the standalone app

Download the latest release, unzip it anywhere, and run
`FriendsOfDutyExporter.exe`. Nothing else is required: the app carries its own
Python and downloads its own Blender on first use.

```powershell
# Or headless, straight to a package:
.\FriendsOfDutyExporter.exe --cli --game-dir "C:\Program Files (x86)\Steam\steamapps\common\Call of Duty" --output .\out --zip .\out\cod2003.fodpak
```

### macOS and Linux — from source

```bash
git clone --recurse-submodules git@github.com:the14mb/COD2003-FODPAK-GEN.git
cd COD2003-FODPAK-GEN
python3 -m pip install Pillow numpy

make gui                          # GUI
make run GAME="/path/to/Call of Duty" ZIP=1    # headless
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init --recursive
```

---

## Requirements

Everything the released app needs is in [Minimum
requirements](#minimum-requirements) above. This section is what a **source
checkout** needs on top of that, and how the two self-provisioning pieces work.

| | |
|---|---|
| **Python** | 3.10+, with `Pillow` and `numpy`. Source checkouts only — the frozen app bundles its own interpreter. |
| **Blender** | **Not a prerequisite.** Downloaded automatically, pinned, and verified. See below. |
| **Importer** | `tools/cod-asset-importer`, a git submodule with a compiled Rust extension. See below. |
| **Call of Duty** | CoD1 **and** United Offensive, installed. |
| **Disk** | Room for the Blender download plus the generated package. |

### Blender provisions itself

The pin lives in `packaging/blender_pin.json` — **Blender 4.5.1 LTS**, verified
against a SHA-256 copied verbatim from Blender's own published checksum file.

It is pinned, not merely minimum-versioned, on purpose: 4.5.1 is the build that
produced the reference package, and Blender 4.2 and 4.5 emit measurably
different GLBs from the same input. A Blender the generator did not pin is
refused with a reason rather than used.

It downloads into a per-user cache, never inside the application and never into
this repository:

| OS | Cache |
|---|---|
| Windows | `%LOCALAPPDATA%\Friends of Duty\Blender\<version>\` |
| macOS | `~/Library/Application Support/Friends of Duty/Blender/<version>/` |
| Linux | `$XDG_DATA_HOME/Friends of Duty/Blender/<version>/` |

```bash
python3 exporter/blender_provisioner.py --check     # is a download pending?
make blender-pin-check                             # is the pin still published?
```

Offline: if the download cannot complete, the generator prints the exact URL,
the expected SHA-256, and the directory to drop the archive into. You can also
point it at an existing 4.5.1 with `--blender /path/to/blender`.

### The asset importer

Extraction relies on `cod-asset-importer`, a GPLv3 fork vendored as a submodule
at `tools/cod-asset-importer`. It needs its compiled extension
(`cod_asset_importer.pyd` on Windows, `cod_asset_importer.abi3.so` elsewhere).

Production prop export requires the **v3.6+ authored-XModel-LOD API 1**; an
older API0/v3.5 binary is rejected rather than silently producing only LOD0.

```bash
make importer-fetch      # download CI-built extensions (preferred)
make importer-check      # is this host's importer authored-LOD capable?
```

Building from the vendored Rust source is the fallback and needs the toolchain
from [rustup.rs](https://rustup.rs):

```bash
python3 exporter/build_importer.py --from-source --require-lod
```

---

## Finding your Call of Duty install

The generator auto-detects it. To see what it found:

```bash
make detect-cod        # python3 exporter/cod_autodetect.py
```

Point at it explicitly with `--game-dir`, or set `FOD_COD_GAME_DIR`. The
directory is the one containing `Main/`, with United Offensive in `uo/`.

Typical locations:

```
C:\Program Files (x86)\Steam\steamapps\common\Call of Duty
C:\Program Files (x86)\Activision\Call of Duty
~/Library/Application Support/Steam/steamapps/common/Call of Duty
```

---

## Flag reference

### `exporter/friends_of_duty_exporter.py` — the generator

Run with no arguments for the GUI.

| Flag | Default | What it does |
|---|---|---|
| `--cli` | off | Run headless. Without it you get the GUI. |
| `--game-dir <dir>` | auto-detected | Call of Duty install (contains `Main/`, and optionally `uo/`). |
| `--output <dir>` | — | Content package directory to write. |
| `--zip <file>` | off | Also write a transportable `.fodpak` archive. CLI only. |
| `--blender <path>` | auto-provisioned | Use this Blender instead of downloading the pin. |
| `--force` | off | Re-run every step even when its outputs already exist. |
| `--only <steps>` | all | Run only these pipeline step keys. |
| `--include-uo` | off | Include United Offensive content. |
| `--all-mp` | off | Export the full multiplayer set. |
| `--maps <list>` | all | Restrict to specific maps. |
| `--game-exe <path>` | — | Game executable for the GUI's "Launch game" button. |
| `--game-callback` | off | Set by the game when it spawned the generator. Not for manual use. |

Steps are content-addressed: each records a signature over its inputs and the
tool code that produces it, so a re-run skips what has not changed. `--force`
overrides that, and `--only` narrows it.

### `exporter/build_importer.py` — importer extension

| Flag | Default | What it does |
|---|---|---|
| `--check` | off | Report extension status without building. |
| `--force` | off | Rebuild even when the extension is present. |
| `--require-lod` | off | Require importer v3.6 authored-XModel-LOD support. |
| `--from-source` | off | Build from the vendored Rust source (needs the Rust toolchain). |
| `--importer-root <dir>` | submodule | Alternate `cod-asset-importer` checkout, for testing. |

### `exporter/blender_provisioner.py` — Blender

| Flag | Default | What it does |
|---|---|---|
| `--check` | off | Report status without downloading. |
| `--blender <path>` | — | Probe this Blender instead. |

### `exporter/cod_autodetect.py` — install discovery

No flags. Prints where Call of Duty was found.

### `packaging/build_exporter.py` — freeze the standalone app

| Flag | Default | What it does |
|---|---|---|
| `--output <dir>` | — | Where to write the frozen payload. |
| `--target <name>` | host | Target triple, e.g. `windows-x64`, `macos-arm64`, `linux-x64`. |
| `--skip-selftest` | off | Skip the in-payload assertions after building. |

### `packaging/fetch_importers.py` — CI importer artifacts

| Flag | Default | What it does |
|---|---|---|
| `--run <id>` | latest | Fetch from a specific workflow run id. |
| `--target <name>` | all | Fetch only this target. Repeatable. |
| `--no-install` | off | Populate the cache without installing for this host. |

### `packaging/make_blender_pin.py` — re-pin Blender

| Flag | Default | What it does |
|---|---|---|
| `--version <v>` | — | Blender version to pin, e.g. `4.5.1`. |
| `--gltf-generator <s>` | `Khronos glTF Blender I/O v4.5.47` | Generator string this build embeds in exported GLBs. |
| `--check` | off | Verify the existing pin instead of rewriting it. |

Re-pinning is **not** a routine change: it moves the oracle every milestone is
validated against, and requires a full smoke pass.

### `packaging/tools_closure.py` — payload allow-list

| Flag | Default | What it does |
|---|---|---|
| `--write` | off | Rewrite `packaging/tools_manifest.txt`. |
| `--list-unreachable` | off | Print the `tools/` modules that never ship. |

`tools_manifest.txt` is generated, never hand-edited.
`tests/test_tools_manifest.py` fails if it disagrees with the computed closure,
so a new import cannot silently leave a module out of the payload and a new dev
tool cannot silently ship.

---

## Environment variables

| Variable | Meaning |
|---|---|
| `FOD_COD_GAME_DIR` | Call of Duty install directory. Same as `--game-dir`. |
| `FOD_REPO_ROOT` | Repository root. Set by the PyInstaller build; not for manual use. |
| `FOD_HIDDEN_IMPORTS` | Hidden-import list, generated by `build_exporter.py` for the spec. Not for manual use. |
| `XDG_DATA_HOME` | Linux cache root for the Blender download. |
| `LOCALAPPDATA` / `APPDATA` | Windows cache roots for the Blender download. |

---

## What you get, and how to use it

The generator writes a **content package**: a directory containing
`fodpak.json` plus `weapons/`, `players/`, `maps/`, `textures/`, `audio/`,
`fx/` and `ui/`. With `--zip` it also produces a single `.fodpak` archive,
which is the transportable form.

To install it into Friends of Duty, drop the `.fodpak` into the game's mods
folder:

| OS | Folder |
|---|---|
| Windows / Linux | `<game>/mods/` |
| macOS | `mods/` beside the `.app` |
| Any (per-user) | `<persistentDataPath>/mods/` |

The game's **MODS** page shows the exact folder for your install, and creates it
if it is missing. Restart the game after adding a package.

For development you can skip packaging entirely and point the game at an
unpacked directory with `-contentDir /path/to/package`, or the `FOD_CONTENT_DIR`
environment variable.

The package format contract is `docs/CONTENT_PIPELINE.md`.

---

## Building the standalone app

```bash
make payload                                # host target
make payload EXPORTER_TARGET=windows-x64    # explicit
```

**PyInstaller cannot cross-compile.** A Windows `.exe` must be built on Windows,
a macOS app on macOS. There is no way around this.

The output is `onedir`, not `onefile`, deliberately: the distribution chunks at
about 1 MB, so a one-line fix re-downloads a few hundred KB instead of the whole
payload — and the generator re-execs itself once per host step, which a onefile
build would make pay its extraction cost on every one.

Only the *shell* is frozen — interpreter, stdlib, numpy, Pillow, Tcl/Tk, and the
ssl/certifi/lzma set the Blender provisioner needs. The generator's own modules
and the whole `tools/` closure ship as **loose source** beside the binary,
because step signatures hash those files to decide what to re-run; freezing them
would make every step's signature depend on the build rather than on the code.

Nothing is signed. On Windows that costs an antivirus-reputation argument rather
than a user-visible prompt.

---

## Troubleshooting

**"Blender 4.5.1 is required"** — you passed `--blender` pointing at a different
version. Either drop the flag and let it provision, or install 4.5.1.

**Blender download fails** — the generator prints the URL, the expected
SHA-256 and the target directory. Download it yourself and drop it there.
`make blender-pin-check` tells you whether the pinned artifact is still
published at all; if it is not, that is a repository issue, not your setup.

**Importer rejected as too old** — you have an API0/v3.5 binary. Run
`make importer-fetch`, then `make importer-check`. The rejection is deliberate:
an old importer produces LOD0-only props that look wrong in game rather than
failing loudly.

**United Offensive missing** — UO content is part of the package contract.
Confirm `uo/` exists inside your install directory.

**Call of Duty not found** — run `make detect-cod` to see where it looked, then
pass `--game-dir` explicitly. Installs on drives other than C: are supported.

**An export step keeps re-running** — step signatures cover the tool code as
well as the inputs, so editing anything under `tools/` legitimately invalidates
the steps that use it.

---

## Development

```bash
make test        # python3 -m unittest discover -s tests
make selftest    # in-payload assertions
                 # SELFTEST_ARGS=--provision also exercises the real download
make closure     # regenerate packaging/tools_manifest.txt
```

Further reading in `docs/`:

- `EXPORTER_SINGLE_EXECUTABLE.md` — the frozen-app design
- `EXPORTER_PHASE_B.md` — milestones and validation
- `CONTENT_PIPELINE.md` — the `.fodpak` format contract

---

## Licensing

The extraction path depends on `cod-asset-importer`, a **GPLv3** fork vendored
as a submodule. See `tools/cod-asset-importer/LICENSE`, `MODIFICATIONS.md` and
`PROVENANCE.md`.

No Call of Duty asset is contained in, or distributed with, this repository.
