# Friends of Duty — Content Exporter

Generates the game's runtime content package ("fodpak") from **your own**
Call of Duty 1 (+ United Offensive) installation. The shipped game contains
no Call of Duty assets; this tool extracts and converts them on your machine.
Packages are for personal use with a lawfully owned copy and must not be
publicly redistributed. See `Docs/CONTENT_PIPELINE.md` for the format contract.

## Requirements

- Python 3.10+ with `Pillow` and `numpy` (`python3 -m pip install Pillow numpy`;
  the GUI offers a one-click install). A shipped build freezes its own
  interpreter, so this applies to a source checkout only.
- **Blender is NOT a requirement.** The exporter downloads the pinned build
  itself on first use — Blender 4.5.1 LTS, verified against a SHA-256 that is
  part of this build, into a per-user cache
  (`%LOCALAPPDATA%` / `~/Library/Application Support` / `$XDG_DATA_HOME`,
  under `Friends of Duty/Blender/<version>/`). It is never written inside the
  application and never enters a Steam depot.
  - The pin lives in `packaging/blender_pin.json`. It is not a free choice:
    4.5.1 is the build that produced the reference package, and Blender 4.2
    and 4.5 emit measurably different GLBs from the same input. A Blender the
    exporter did not pin is refused with a reason rather than used.
  - `--blender <path>` (or `FOD_BLENDER`) skips the download for an existing
    4.5.1. This is also the offline path: if the download cannot complete,
    the exporter prints the exact URL, the expected SHA-256 and the directory
    to drop the archive into.
  - `python3 exporter/blender_provisioner.py --check` reports whether a
    download is pending; `--blender <path>` probes a specific build.
- `tools/cod-asset-importer` with its compiled Rust extension
  (`cod_asset_importer.abi3.so`, or `cod_asset_importer.pyd` on Windows).
  Production prop export requires the v3.6+ authored-XModel-LOD API 1; the
  exporter rejects an older API0/v3.5 binary instead of silently producing
  only LOD0. The GUI's **Prepare importer** action and CLI startup first try
  a capable bundled artifact, then build the vendored Rust source when the
  matching bundled artifact is absent or too old. That one-time fallback
  needs the Rust toolchain from <https://rustup.rs>. It can also be run by
  hand with `python3 exporter/build_importer.py --require-lod`
  (`--check --require-lod` reports production readiness; `--force
  --require-lod` rebuilds). The fork is published under GPLv3 at
  <https://github.com/anarqz/cod-asset-importer> (tag `v3.6.0`) and is a
  submodule of this repo — clone with `--recurse-submodules`. See
  `tools/cod-asset-importer/MODIFICATIONS.md`.
- A Call of Duty install directory containing `Main/*.pk3`, plus United
  Offensive. `python3 exporter/cod_autodetect.py` prints the one it finds.

## Running

- macOS: double-click `run_exporter.command` (or `./run_exporter.sh`)
- Windows: `run_exporter.bat`
- Linux: `./run_exporter.sh`

Headless:

```
python3 friends_of_duty_exporter.py --cli \
    --game-dir "/path/to/Call of Duty" \
    --output "/path/to/Content/current" \
    [--blender /path/to/blender] [--force] [--include-uo] \
    [--maps mp_carentan ...] \
    [--zip my_package.fodpak] [--only impacts footsteps ...]
```

The game launches the exporter with `--output <persistentDataPath>/Content/current`
and `--game-exe <game binary>` (enables the final "Launch game" button).
Without `--output` the default is `<repo>/fod_content/current` (the dev
search path).

The exporter has a hard multiplayer-map allowlist. The complete package
contains only Arnhem, Cassino, Carentan, Pavlov, Chateau, Railyard and Rocket.
`--maps` may select a smaller development subset of those names; it cannot
admit another retail or custom map. The legacy `--all-mp` argument remains a
no-op for older launchers and cannot expand the allowlist.

## Pipeline steps

Intermediate files go to `<output parent>/.fod_staging/`. Steps whose outputs
already exist are skipped, so a cancelled or failed run resumes where it
stopped ("Force full re-export" ignores that).

1. `extract_cod1` — unpack CoD1 `Main/*.pk3` model/weapon/anim roots
2. `extract_mp_models` — merge CoD1+UO model roots for props
3. `viewmodels` — Blender: skinned viewmodel GLBs + `weapons/weapons.json`
4. `worldmodels` / `shellcasing` — Blender: static weapon GLBs
5. `players` — Blender: skinned player GLBs + `players/players.json`
6. `presentation` — weapon audio + `audio/presentation.json` + fx PNG/efx
7. `footsteps` — `audio/footsteps/clips/<family>/`
8. `impacts` — impact decals/sounds, whizbys, fatigue, scope reticle
9. `weapon_data` — raw WEAPONFILE dumps to `weapons/data/<id>.txt`
10. `maps` — the approved BSP roster → collision/fallback world GLB, offline
    32 m render sectors, CoD v59 cluster PVS, textures, entities, catalog
11. `props` — Blender: authored map-prop LOD GLBs + CoD distance boundaries
    in `props/props.json`
12. `package` — validate rosters, write `fodpak.json`

## Output

A mountable content directory (`fodpak.json` at its root). "Save transportable
.fodpak" zips that directory (plain zip, contents at zip root) so you can move
it between your own machines.
