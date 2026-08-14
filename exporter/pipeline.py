#!/usr/bin/env python3
"""Step orchestrator for the Friends of Duty exporter.

Runs the tools/ pipeline as subprocesses against the player's Call of Duty
install and assembles the fodpak content directory (Docs/CONTENT_PIPELINE.md).
Blender-based steps are invoked as
`<blender> --background --python <tool> -- <args>`.

Step CLI contracts (shared with the tool implementations):
  a extract_cod1_assets.py            <game>/Main <staging>/cod1_source
  b extract_cod_multiplayer_model_assets.py --game-root <game>
        --output <staging>/cod_multiplayer_source   (skips uo/ when absent)
  c export_cod1_demo_viewmodels.py    -- SOURCE OUTPUT IMPORTER_PY TOOLS
        --format glb --pak-root <content>
  d batch_export_cod1_models.py       -- SOURCE OUTPUT CATEGORY IMPORTER_PY
        --format glb --pak-root <content> [--roster <world,world,...>]
  e export_cod1_multiplayer_players.py -- SOURCE OUTPUT IMPORTER_PY TOOLS
        --format glb --pak-root <content>
  f extract_cod1_weapon_presentation.py <game>/Main <staging>/cod1_source
        --pak-root <content>
  g extract_cod1_footsteps.py         <game>/Main --pak-root <content>
  h extract_cod1_impacts.py           --game-root <game> --pak-root <content>
        --notes-file <staging>/notes/impacts.txt
  i extract_cod1_hud.py               <game> --pak-root <content>
        --notes-file <staging>/notes/hud.txt
  i2 extract_cod1_ordnance.py         <game> --pak-root <content>
        --staging <staging>/cod1_source   (UO smoke staging + FX + audio;
        restages the official grenade/panzerfaust WEAPONFILEs, so a rerun of
        the viewmodels step picks up the original ordnance values)
  j (in-process) weapons/data/<id>.txt from staging weapons/mp via weapons.json
  k import_cod_multiplayer_maps.py    --game-root <game> --pak-root <content>
        --maps <Friends-of-Duty shipping roster> [--force]
  l export_cod_multiplayer_props.py   -- SOURCE MODELS_JSON OUTPUT IMPORTER_PY
        --format glb --pak-root <content>
  m package.py                        <content> --game-root <game>
        --notes-dir <staging>/notes
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

EXPORTER_DIR = Path(__file__).resolve().parent
# Development layout:
#   <repo>/exporter/*.py + <repo>/tools/
# Shipped layout:
#   <build>/Exporter/*.py + <build>/Exporter/tools/
# Keep the shipped payload self-contained; resolving tools only through the
# parent directory made every packaged exporter look incomplete.
if (EXPORTER_DIR / "tools").is_dir():
    REPO_ROOT = EXPORTER_DIR
    TOOLS_DIR = EXPORTER_DIR / "tools"
    # A shipped exporter is always handed --output by the game, so nothing
    # outside the payload is ever read.
    DATA_ROOT = EXPORTER_DIR
else:
    REPO_ROOT = EXPORTER_DIR.parent
    TOOLS_DIR = REPO_ROOT / "tools"
    # The repo holds the code; the Call of Duty installs sit BESIDE it. They
    # are large and copyrighted, and deliberately not in version control, so
    # they do not move when the source does. Anything reaching for game data
    # resolves it from here, never from REPO_ROOT.
    DATA_ROOT = REPO_ROOT.parent
# The content package is a git submodule at <repo>/Content, cloned from
# github.com/anarqz/fodpak, so an export updates a tracked working tree and
# every package the machine produces is reviewable as a diff.
#
# That repo carries its own Content/ directory at its root: it is built to be
# dropped beside a game, where the runtime looks for Content/current. Mounted
# as a submodule named Content, the package therefore lands at
# <repo>/Content/Content/current. The doubled name is the submodule's mount
# point plus the repo's own layout, not a typo — and flattening that repo to
# put current/ at its root is supported here too, so this keeps working if it
# is ever restructured.
CONTENT_SUBMODULE = REPO_ROOT / "Content"


def _default_content_dir() -> Path:
    nested = CONTENT_SUBMODULE / "Content"
    if nested.is_dir() or not (CONTENT_SUBMODULE / "current").is_dir():
        return nested / "current"
    return CONTENT_SUBMODULE / "current"


DEFAULT_CONTENT_DIR = _default_content_dir()

IMPORTER_PY_ROOT = TOOLS_DIR / "cod-asset-importer" / "python"
IMPORTER_NATIVE = (
    IMPORTER_PY_ROOT
    / "cod_asset_importer"
    / (
        "cod_asset_importer.pyd"
        if sys.platform == "win32"
        else "cod_asset_importer.abi3.so"
    )
)
sys.path.insert(0, str(TOOLS_DIR))
# The exporter package's own modules. In a shipped build these sit beside the
# launcher rather than in a repo, so resolve them relative to this file.
if str(EXPORTER_DIR) not in sys.path:
    sys.path.insert(0, str(EXPORTER_DIR))

import fod_paths  # noqa: E402
import progress as fod_progress  # noqa: E402
import toolchain as fod_toolchain  # noqa: E402

from cod1_archive_policy import (  # noqa: E402
    POLICY_VERSION as SOURCE_POLICY_VERSION,
    policy_fingerprint,
    uo_installation_status,
)
from cod1_playeranim import (  # noqa: E402
    EXCLUDED_TURRET_ALIASES,
    PLAYER_ACTIVE_IDENTIFIER_COUNT,
    PLAYER_ANIMATION_COUNT,
    SCRIPT_TURRET_ALIASES,
    TURRET_EXPANSION_COUNT,
)
from cod1_multiplayer_closure import (  # noqa: E402
    MOUNTED_MG42_ANIMATIONS,
    MOUNTED_MG42_MODEL,
    MOUNTED_MG42_WEAPON_FILES,
)
from cod1_shipping_maps import (  # noqa: E402
    BASE_SHIPPING_MAP_IDS,
    UO_SHIPPING_MAP_IDS,
    selected_shipping_map_ids,
)

# 4: step signatures gained a per-step "toolchain" key, so a Blender or
# importer change now invalidates exactly the steps that toolchain produced.
# Bumping retires every pre-migration marker in one go.
#
# This is the STEP-MARKER stamp — it decides which cached steps are stale. It
# is NOT the shape of any manifest, and the two numbers are free to differ.
PIPELINE_SCHEMA_VERSION = 4

# The catalog SCHEMA version: the shape of weapons.json / players.json. Must
# equal FodContentSchemas.WeaponsVersion and .PlayersVersion on the Unity side;
# FriendsOfDutyMacBuild.ValidateExporterSchemaContract fails the build if they
# drift.
#
# Named because it used to be a bare `!= 3` at each check site while the only
# constant in this file was PIPELINE_SCHEMA_VERSION. When the step stamp went
# to 4, that build gate — which was matching against the step stamp — started
# reporting a "catalog schema mismatch" that did not exist: the exporter, the
# runtime and every shipped manifest all still agreed on 3. Bumping the runtime
# to satisfy it would have made the build pass while the game rejected its own
# content at parse.
CATALOG_SCHEMA_VERSION = 3
# The multiplayer weapon roster, IMPORTED rather than restated. This used to
# be a second copy of package.py's frozenset, and when the roster grew to the
# full CoD 1 + United Offensive set only that one was updated — so the
# validator accepted the package while the pipeline's own done-check kept
# declaring the viewmodel step's outputs "incomplete" and refused to publish
# anything. Two lists that must agree, in one file, is one list.
from package import REQUIRED_WEAPON_IDS  # noqa: E402
BASE_MAP_IDS = frozenset(BASE_SHIPPING_MAP_IDS)
UO_MAP_IDS = frozenset(UO_SHIPPING_MAP_IDS)

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str, str], None]

# A step that goes quiet is indistinguishable from a hung one, in the terminal
# and on the game's boot console alike. Whenever a child produces no output
# for this long, say so rather than letting the log stall.
SILENCE_HEARTBEAT_SECONDS = 15.0


class PipelineCancelled(Exception):
    pass


class PipelineError(Exception):
    pass


@dataclass
class PipelineConfig:
    game_dir: Path
    content_dir: Path
    blender: Path | None = None
    force: bool = False

    @property
    def staging_dir(self) -> Path:
        return self.content_dir.parent / ".fod_staging"

    @property
    def cod1_source(self) -> Path:
        return self.staging_dir / "cod1_source"

    @property
    def mp_source(self) -> Path:
        return self.staging_dir / "cod_multiplayer_source"

    @property
    def notes_dir(self) -> Path:
        return self.staging_dir / "notes"


@dataclass
class Step:
    key: str
    title: str
    build_argv: Callable[[PipelineConfig], list[str]] | None = None
    run_python: Callable[[PipelineConfig, LogFn], None] | None = None
    is_done: Callable[[PipelineConfig], bool] = lambda cfg: False
    needs_blender: bool = False
    signature_files: tuple[Path, ...] = ()
    depends_on: tuple[str, ...] = ()


def require_united_offensive(game_dir: Path) -> None:
    """Abort before any work when United Offensive is missing or incomplete.

    Friends of Duty requires Call of Duty + United Offensive. UO is not an
    optional map pack: it supplies the smoke grenade (the 24th weapon, and a
    real loadout entry), 95 additional player animations, and two of the
    seven roster maps. A package built without it fails validation and is
    refused at mount, so producing one is never useful.

    Called before the content and staging directories are created, so a player
    without UO is told what to install and is left with nothing on disk to
    clean up.
    """
    ok, message = uo_installation_status(game_dir)
    if not ok:
        raise PipelineError(message)


def _selected_map_ids(cfg: PipelineConfig) -> list[str]:
    """The fixed seven-map roster. Not derived from any caller input."""
    return list(selected_shipping_map_ids(include_uo=True))


def _host(cfg: PipelineConfig, script: Path, *args: str) -> list[str]:
    """Argv for a step that runs in our own interpreter.

    In a source checkout that is `python3 <script> ...`. In a frozen build
    there IS no separate interpreter to call — sys.executable is the bundle —
    so the exporter re-execs itself with a dispatch flag and fod_launcher
    runs the script through runpy. A fresh process per step is deliberate:
    28 of the 50 tools have no main() and execute at import with argv bound
    at module scope, so in-process invocation would silently reuse the first
    call's arguments.
    """
    if fod_paths.is_bundled():
        return [fod_paths.self_exe(), "--fod-run-tool", str(script), *args]
    return [sys.executable, str(script), *args]


def _python(cfg: PipelineConfig, tool: str, *args: str) -> list[str]:
    return _host(cfg, TOOLS_DIR / tool, *args)


def _blender(cfg: PipelineConfig, tool: str, *args: str) -> list[str]:
    if cfg.blender is None:
        raise PipelineError(f"Blender is required for {tool} but was not configured")
    return [
        # --factory-startup is not cosmetic. Without it the export loads the
        # player's own Blender preferences and every third-party add-on they
        # have installed: a BlenderMCP add-on was observed injecting its own
        # output into a live run. The export must depend on the pinned build
        # and nothing else on the machine.
        str(cfg.blender), "--background", "--factory-startup",
        "--python", str(TOOLS_DIR / tool),
        "--", *args,
    ]


def _weapons_json(cfg: PipelineConfig) -> dict:
    path = cfg.content_dir / "weapons" / "weapons.json"
    if not path.is_file():
        raise PipelineError(
            f"{path} not found — the viewmodel export step must run first")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PipelineError(f"{path} root must be a JSON object")
    return payload


def _weapon_worlds(cfg: PipelineConfig) -> list[str]:
    worlds: list[str] = []
    for weapon in _weapons_json(cfg).get("weapons", []):
        world = weapon.get("world") or weapon.get("definition", {}).get("world", "")
        world = world.strip().lower()
        if world and world not in worlds:
            worlds.append(world)
    if not worlds:
        raise PipelineError("weapons.json contains no world model names")
    return worlds


def _weapon_entry(cfg: PipelineConfig, weapon_id: str) -> dict | None:
    """weapons.json entry by id, or None — safe for is_done probes (never
    raises on a missing or partially written manifest)."""
    path = cfg.content_dir / "weapons" / "weapons.json"
    if not path.is_file():
        return None
    try:
        weapons = json.loads(path.read_text(encoding="utf-8")).get("weapons", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    for weapon in weapons:
        if isinstance(weapon, dict) and weapon.get("id") == weapon_id:
            return weapon
    return None


def _grenade_roster_done(cfg: PipelineConfig) -> bool:
    """The viewmodel export is complete only once the ordnance roster is in
    weapons.json (fraggrenade always; smokegrenade once its UO members have
    been staged by the ordnance step)."""
    if _weapon_entry(cfg, "fraggrenade") is None:
        return False
    smoke_staged = (cfg.cod1_source / "weapons" / "mp" / "smokegrenade_mp").is_file()
    return not smoke_staged or _weapon_entry(cfg, "smokegrenade") is not None


def _presentation_has_alias(cfg: PipelineConfig, alias: str) -> bool:
    path = cfg.content_dir / "audio" / "presentation.json"
    if not path.is_file():
        return False
    try:
        aliases = json.loads(path.read_text(encoding="utf-8")).get("aliases", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    return any(
        isinstance(entry, dict) and entry.get("alias") == alias
        for entry in aliases
    )


def _presentation_has_selected_maps(cfg: PipelineConfig) -> bool:
    path = cfg.content_dir / "audio" / "presentation.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("selectedMaps") == _selected_map_ids(cfg)
    )


def _mp_scripts_done(cfg: PipelineConfig) -> bool:
    path = cfg.content_dir / "scripts" / "mp" / "mp_content.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("selectedMaps") == _selected_map_ids(cfg)
    )


def _exploders_done(cfg: PipelineConfig) -> bool:
    path = cfg.content_dir / "fx" / "exploders" / "manifest.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("selectedMaps") == _selected_map_ids(cfg)
    )


def _prop_models_json(cfg: PipelineConfig) -> Path:
    candidates = (
        cfg.content_dir / "props" / "required_models.json",
        cfg.content_dir / "maps" / "prop_models.json",
        cfg.content_dir / "maps" / "OriginalMultiplayerPropModels.json",
        cfg.staging_dir / "prop_models.json",
        cfg.staging_dir / "OriginalMultiplayerPropModels.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise PipelineError(
        "prop model list not found (expected the map import step to write "
        + " or ".join(str(c) for c in candidates) + ")")


def _glb_count(root: Path, pattern: str) -> int:
    return len(list(root.glob(pattern))) if root.is_dir() else 0


def _all_weapon_paths_exist(
    cfg: PipelineConfig,
    manifest_key: str,
    *,
    require_when: Callable[[dict], bool] = lambda _entry: True,
) -> bool:
    try:
        weapons = _weapons_json(cfg).get("weapons", [])
    except (OSError, json.JSONDecodeError, PipelineError):
        return False
    if not weapons:
        return False
    for weapon in weapons:
        if not isinstance(weapon, dict) or not require_when(weapon):
            continue
        relative = weapon.get(manifest_key, "")
        if not relative or not (cfg.content_dir / relative).is_file():
            return False
    return True


def _expected_weapon_count(cfg: PipelineConfig) -> int:
    return len(REQUIRED_WEAPON_IDS)


def _expected_weapon_ids(cfg: PipelineConfig) -> set[str]:
    return set(REQUIRED_WEAPON_IDS)


def _viewmodels_done(cfg: PipelineConfig) -> bool:
    try:
        manifest = _weapons_json(cfg)
        weapons = manifest.get("weapons", [])
    except (OSError, json.JSONDecodeError, PipelineError):
        return False
    ids = {
        entry.get("id")
        for entry in weapons
        if isinstance(entry, dict)
    }
    if (
        manifest.get("version") != CATALOG_SCHEMA_VERSION
        or len(weapons) != _expected_weapon_count(cfg)
    ):
        return False
    if ids != _expected_weapon_ids(cfg):
        return False
    return _all_weapon_paths_exist(cfg, "viewmodelGlb")


def _ordnance_staging_done(cfg: PipelineConfig) -> bool:
    weapon_root = cfg.cod1_source / "weapons" / "mp"
    required = (
        "fraggrenade_mp",
        "mk1britishfrag_mp",
        "rgd-33russianfrag_mp",
        "stielhandgranate_mp",
        "panzerfaust_mp",
    )
    if not all((weapon_root / name).is_file() for name in required):
        return False
    smoke = (weapon_root / "smokegrenade_mp").is_file()
    return smoke  # the UO smoke grenade is always staged


def _players_done(cfg: PipelineConfig) -> bool:
    path = cfg.content_dir / "players" / "players.json"
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    source = manifest.get("source", {})
    players = manifest.get("players", [])
    if not isinstance(source, dict) or not isinstance(players, list):
        return False
    expected = PLAYER_ANIMATION_COUNT
    expected_active = PLAYER_ACTIVE_IDENTIFIER_COUNT
    expected_aliases = SCRIPT_TURRET_ALIASES
    return (
        manifest.get("version") == CATALOG_SCHEMA_VERSION
        and len(players) == 4
        and source.get("exportedPlayerAnimations") == expected
        and source.get("activeIdentifiers") == expected_active
        and source.get("expandedTurretAliases") == list(expected_aliases)
        and source.get("expandedTurretAnimations")
        == TURRET_EXPANSION_COUNT
        and source.get("excludedTurretAnimations")
        == list(EXCLUDED_TURRET_ALIASES)
        and all(
            isinstance(entry, dict)
            and len(entry.get("animations", [])) == expected
            and all(
                isinstance(animation, dict)
                and isinstance(animation.get("looped"), bool)
                and bool(animation.get("layers"))
                and isinstance(animation.get("actionRoles"), list)
                and isinstance(animation.get("sourceAlias"), str)
                and isinstance(animation.get("turretWeapon"), str)
                and isinstance(animation.get("turretStance"), str)
                and isinstance(animation.get("turretPhase"), str)
                and isinstance(animation.get("turretHorizontal"), str)
                and isinstance(animation.get("turretVertical"), str)
                for animation in entry.get("animations", [])
            )
            for entry in players
        )
    )


def _map_optimization_done(
    cfg: PipelineConfig,
    entry: dict[str, object],
) -> bool:
    relative = entry.get("optimizationJson")
    if not isinstance(relative, str) or not relative:
        return False
    path = cfg.content_dir / relative
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(document, dict)
        or document.get("format")
        != "FriendsOfDuty.MapOptimization"
        or document.get("version") != 2
        or document.get("sourceBspVersion") != 59
        or document.get("codUnitToMetre") != 0.0254
        or document.get("sectorStrategy") != "unity-xz-grid-v1"
        or document.get("sectorSizeMetres") != 32.0
        or document.get("fallbackWorldGlb") != entry.get("worldGlb")
    ):
        return False
    sectors = document.get("sectors")
    visibility = document.get("visibility")
    if not isinstance(visibility, dict):
        return False
    try:
        cluster_count = visibility["clusterCount"]
        row_bytes = visibility["rowBytes"]
        plane_count = visibility["planeCount"]
        node_count = visibility["nodeCount"]
        leaf_count = visibility["leafCount"]
        lengths = (
            ("pvsBase64", cluster_count * row_bytes),
            ("planesBase64", plane_count * 16),
            ("nodesBase64", node_count * 12),
            ("leavesBase64", leaf_count * 8),
        )
        visibility_complete = (
            all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                for value in (
                    cluster_count,
                    row_bytes,
                    plane_count,
                    node_count,
                    leaf_count,
                )
            )
            and row_bytes >= (cluster_count + 7) // 8
            and row_bytes % 4 == 0
            and isinstance(visibility.get("cells"), list)
            and bool(visibility["cells"])
            and all(
                isinstance(cell, dict)
                and isinstance(cell.get("minimum"), list)
                and isinstance(cell.get("maximum"), list)
                and len(cell["minimum"]) == 3
                and len(cell["maximum"]) == 3
                and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in cell["minimum"] + cell["maximum"]
                )
                and all(
                    cell["minimum"][axis] <= cell["maximum"][axis]
                    for axis in range(3)
                )
                for cell in visibility["cells"]
            )
            and all(
                len(base64.b64decode(visibility[field], validate=True))
                == expected
                for field, expected in lengths
            )
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        binascii.Error,
    ):
        return False
    if (
        not visibility_complete
        or not isinstance(sectors, list)
        or not sectors
    ):
        return False

    names: set[str] = set()
    glbs: set[str] = set()
    grids: set[tuple[int, int]] = set()
    always_visible_count = 0
    declared_triangles = 0
    actual_triangles = 0
    always_visible_triangles = 0
    for sector in sectors:
        if not isinstance(sector, dict):
            return False
        name = sector.get("name")
        relative = sector.get("glb")
        grid_x = sector.get("gridX")
        grid_z = sector.get("gridZ")
        clusters = sector.get("clusterIndices")
        always_visible = sector.get("alwaysVisible")
        triangles = sector.get("triangles")
        bounds = sector.get("boundsCodUnits")
        minimum = bounds.get("minimum") if isinstance(bounds, dict) else None
        maximum = bounds.get("maximum") if isinstance(bounds, dict) else None
        raw_relative = (
            relative.replace("\\", "/")
            if isinstance(relative, str)
            else ""
        )
        path_parts = raw_relative.split("/") if raw_relative else []
        normalized = (
            PurePosixPath(raw_relative)
            if path_parts
            else None
        )
        glb_key = (
            normalized.as_posix().casefold()
            if normalized is not None
            else ""
        )
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or normalized is None
            or normalized.is_absolute()
            or any(
                not part or part in (".", "..")
                for part in path_parts
            )
            or ".." in normalized.parts
            or not glb_key
            or glb_key in glbs
            or not isinstance(grid_x, int)
            or isinstance(grid_x, bool)
            or not isinstance(grid_z, int)
            or isinstance(grid_z, bool)
            or not isinstance(clusters, list)
            or not all(
                isinstance(cluster, int)
                and not isinstance(cluster, bool)
                and 0 <= cluster < cluster_count
                for cluster in clusters
            )
            or clusters != sorted(set(clusters))
            or not isinstance(always_visible, bool)
            or (always_visible and clusters)
            or (not always_visible and not clusters)
            or not isinstance(triangles, int)
            or isinstance(triangles, bool)
            or triangles <= 0
            or not isinstance(minimum, list)
            or not isinstance(maximum, list)
            or len(minimum) != 3
            or len(maximum) != 3
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
                for value in minimum + maximum
            )
            or any(minimum[axis] > maximum[axis] for axis in range(3))
        ):
            return False
        grid = (grid_x, grid_z)
        if always_visible:
            always_visible_count += 1
            if always_visible_count > 1:
                return False
            always_visible_triangles += triangles
        elif grid in grids:
            return False
        else:
            grids.add(grid)
        names.add(name)
        glbs.add(glb_key)
        sector_path = cfg.content_dir / normalized
        if not sector_path.is_file():
            return False
        try:
            measured = _static_glb_triangle_count(sector_path)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if measured != triangles:
            return False
        declared_triangles += triangles
        actual_triangles += measured

    counts = document.get("counts")
    if (
        not isinstance(counts, dict)
        or counts.get("sectors") != len(sectors)
        or counts.get("triangles") != declared_triangles
        or counts.get("alwaysVisibleTriangles")
        != always_visible_triangles
        or counts.get("clusters") != cluster_count
        or counts.get("cells") != len(visibility["cells"])
        or actual_triangles != declared_triangles
    ):
        return False
    fallback = cfg.content_dir / str(entry["worldGlb"])
    try:
        return (
            fallback.is_file()
            and _static_glb_triangle_count(fallback)
            == declared_triangles
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _static_glb_triangle_count(path: Path) -> int:
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError("truncated GLB")
    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(data):
        raise ValueError("invalid GLB header")
    chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A or 20 + chunk_length > len(data):
        raise ValueError("GLB has no valid JSON chunk")
    document = json.loads(
        data[20 : 20 + chunk_length]
        .rstrip(b" \t\r\n\0")
        .decode("utf-8")
    )
    accessors = document.get("accessors")
    meshes = document.get("meshes")
    if not isinstance(accessors, list) or not isinstance(meshes, list):
        raise ValueError("GLB has no mesh/accessor arrays")
    triangles = 0
    for mesh in meshes:
        primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
        if not isinstance(primitives, list):
            raise ValueError("GLB mesh has no primitives")
        for primitive in primitives:
            if not isinstance(primitive, dict) or primitive.get("mode", 4) != 4:
                raise ValueError("GLB contains non-triangle primitives")
            accessor_index = primitive.get("indices")
            if (
                not isinstance(accessor_index, int)
                or accessor_index < 0
                or accessor_index >= len(accessors)
                or not isinstance(accessors[accessor_index], dict)
            ):
                raise ValueError("GLB primitive has no index accessor")
            count = accessors[accessor_index].get("count")
            if not isinstance(count, int) or count <= 0 or count % 3:
                raise ValueError("GLB index count is not triangular")
            triangles += count // 3
    if triangles <= 0:
        raise ValueError("GLB contains no triangles")
    return triangles


def _maps_done(cfg: PipelineConfig) -> bool:
    path = cfg.content_dir / "maps" / "catalog.json"
    if not path.is_file():
        return False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    if manifest.get("version") != 2:
        return False
    maps = manifest.get("maps", [])
    if not isinstance(maps, list):
        return False
    selected = set(_selected_map_ids(cfg))
    expected = {
        (
            "uo" if map_id in UO_MAP_IDS else "cod1",
            map_id,
        )
        for map_id in selected
    }
    actual = {
        (entry.get("game"), entry.get("mapId"))
        for entry in maps
        if isinstance(entry, dict)
    }
    if actual != expected or len(maps) != len(expected):
        return False
    return all(
        isinstance(entry, dict)
        # A current (M3+) catalog entry always carries the lightmapsJson key,
        # even when empty for a map with no baked lightmaps; a pre-M3 catalog
        # omits it entirely. Requiring the key forces existing installs to
        # re-extract exactly once so the CLASSIC world lightmaps appear (the
        # per-map importer fingerprint then regenerates the geometry).
        and "lightmapsJson" in entry
        and (
            not entry.get("lightmapsJson")
            or (
                isinstance(entry.get("lightmapsJson"), str)
                and (cfg.content_dir / entry["lightmapsJson"]).is_file()
            )
        )
        and all(
            isinstance(entry.get(key), str)
            and bool(entry[key])
            and (cfg.content_dir / entry[key]).is_file()
            for key in (
                "worldGlb",
                "optimizationJson",
                "entitiesJson",
                "materialsJson",
            )
        )
        and _map_optimization_done(cfg, entry)
        for entry in maps
    )


def _prop_lods_done(
    cfg: PipelineConfig,
    prop: dict[str, object],
) -> bool:
    """Production static props require the complete API1 LOD inventory."""
    lods = prop.get("lods")
    if prop.get("animated") is True:
        return lods is None
    if lods is None:
        return False
    if not isinstance(lods, list) or not lods:
        return False
    primary = prop.get("glb")
    paths = set()
    previous = 0.0
    for index, lod in enumerate(lods):
        if not isinstance(lod, dict):
            return False
        relative = lod.get("glb")
        surface = lod.get("sourceSurface")
        distance = lod.get("distance")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.casefold() in paths
            or not (cfg.content_dir / relative).is_file()
            or not isinstance(surface, str)
            or not surface
            or isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or not math.isfinite(float(distance))
            or float(distance) < 0.0
        ):
            return False
        paths.add(relative.casefold())
        distance = float(distance)
        final = index == len(lods) - 1
        if (
            (not final and distance <= previous)
            or (final and distance > 0.0 and distance <= previous)
        ):
            return False
        if distance > 0.0:
            previous = distance
    return lods[0].get("glb") == primary


def _props_done(cfg: PipelineConfig) -> bool:
    required_path = cfg.content_dir / "props" / "required_models.json"
    manifest_path = cfg.content_dir / "props" / "props.json"
    if not required_path.is_file() or not manifest_path.is_file():
        return False
    try:
        required = json.loads(required_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    required_models = required.get("models", [])
    props = manifest.get("props", [])
    if (
        not isinstance(required_models, list)
        or not isinstance(props, list)
        or manifest.get("format") != "FriendsOfDuty.Props"
        or manifest.get("version") != 2
        or not isinstance(manifest.get("exportFingerprint"), str)
        or len(manifest["exportFingerprint"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in manifest["exportFingerprint"]
        )
        or manifest.get("requested") != len(required_models)
        or manifest.get("failures") not in (None, [])
    ):
        return False
    expected = {
        name.casefold()
        for name in required_models
        if isinstance(name, str) and name
    }
    actual = {
        str(entry.get("name", "")).casefold()
        for entry in props
        if isinstance(entry, dict)
    }
    if len(expected) != len(required_models) or actual != expected:
        return False
    if not all(
        isinstance(entry, dict)
        and isinstance(entry.get("glb"), str)
        and (cfg.content_dir / entry["glb"]).is_file()
        and _prop_lods_done(cfg, entry)
        for entry in props
    ):
        return False
    mounted = next(
        (
            entry
            for entry in props
            if str(entry.get("name", "")).casefold()
            == MOUNTED_MG42_MODEL
        ),
        None,
    )
    if MOUNTED_MG42_MODEL not in expected:
        return mounted is None
    if (
        mounted is None
        or mounted.get("animated") is not True
        or mounted.get("sourceWeaponFiles")
        != list(MOUNTED_MG42_WEAPON_FILES)
    ):
        return False
    animations = mounted.get("animations")
    if not isinstance(animations, list):
        return False
    expected_animations = {
        reference.name: reference
        for reference in MOUNTED_MG42_ANIMATIONS
    }
    if {
        entry.get("name")
        for entry in animations
        if isinstance(entry, dict)
    } != set(expected_animations):
        return False
    return all(
        isinstance(entry, dict)
        and entry.get("sourceWeaponFile")
        == expected_animations[entry["name"]].source_weapon_file
        and entry.get("sourceField")
        == expected_animations[entry["name"]].source_field
        and entry.get("stance")
        == expected_animations[entry["name"]].stance
        and entry.get("phase")
        == expected_animations[entry["name"]].phase
        and entry.get("vertical")
        == expected_animations[entry["name"]].vertical
        and entry.get("role")
        == expected_animations[entry["name"]].role
        and entry.get("looped")
        == expected_animations[entry["name"]].looped
        and entry.get("frames")
        == expected_animations[entry["name"]].frame_count
        and entry.get("fps")
        == expected_animations[entry["name"]].frame_rate
        and entry.get("animatedBones") == 9
        and entry.get("sourceBones") == 9
        for entry in animations
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_weapon_data(cfg: PipelineConfig, log: LogFn) -> None:
    weapons = _weapons_json(cfg).get("weapons", [])
    source_dir = cfg.cod1_source / "weapons" / "mp"
    data_dir = cfg.content_dir / "weapons" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    expected_files: set[str] = set()
    provenance: list[dict[str, object]] = []
    for weapon in weapons:
        weapon_id = weapon.get("id", "")
        definition = weapon.get("definition", {})
        weapon_file = (definition.get("weapon_file")
                       or weapon.get("weapon_file")
                       or f"{weapon_id}_mp")
        source = source_dir / weapon_file
        if not source.is_file():
            raise PipelineError(f"WEAPONFILE not found for {weapon_id}: {source}")
        output_name = f"{weapon_id}.txt"
        shutil.copyfile(source, data_dir / output_name)
        expected_files.add(output_name)
        provenance.append(
            {
                "weaponId": weapon_id,
                "variant": "primary",
                "weaponFile": weapon_file,
                "path": f"weapons/data/{output_name}",
                "bytes": source.stat().st_size,
                "sha256": _sha256(source),
            }
        )
        copied += 1
        alternate = weapon.get("alternateWeaponFile", "")
        if alternate:
            alternate_source = source_dir / alternate
            if not alternate_source.is_file():
                raise PipelineError(
                    f"alternate WEAPONFILE not found for {weapon_id}: "
                    f"{alternate_source}"
                )
            alternate_name = f"{weapon_id}__alternate.txt"
            shutil.copyfile(alternate_source, data_dir / alternate_name)
            expected_files.add(alternate_name)
            provenance.append(
                {
                    "weaponId": weapon_id,
                    "variant": "alternate",
                    "weaponFile": alternate,
                    "path": f"weapons/data/{alternate_name}",
                    "bytes": alternate_source.stat().st_size,
                    "sha256": _sha256(alternate_source),
                }
            )
            copied += 1
    for stale in data_dir.glob("*.txt"):
        if stale.name not in expected_files:
            stale.unlink()
    (data_dir / "provenance.json").write_text(
        json.dumps(
            {
                "format": "FriendsOfDuty.WeaponSourceProvenance",
                "version": 1,
                "files": provenance,
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    log(f"copied {copied} WEAPONFILE dumps to weapons/data/")


def build_steps(cfg: PipelineConfig) -> list[Step]:
    content = cfg.content_dir
    return [
        Step(
            key="extract_cod1",
            title="Extract official multiplayer source assets",
            build_argv=lambda c: _python(
                c, "extract_cod1_assets.py",
                str(c.game_dir), str(c.cod1_source),),
            is_done=lambda c: (c.cod1_source / "extraction_manifest.json").is_file(),
            signature_files=(
                TOOLS_DIR / "extract_cod1_assets.py",
                TOOLS_DIR / "cod1_multiplayer_closure.py",
                TOOLS_DIR / "cod1_playeranim.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
            ),
        ),
        Step(
            key="extract_mp_models",
            title="Merge official multiplayer model sources",
            build_argv=lambda c: _python(
                c, "extract_cod_multiplayer_model_assets.py",
                "--game-root", str(c.game_dir),
                "--output", str(c.mp_source),),
            is_done=lambda c: (c.mp_source / "extraction_manifest.json").is_file(),
            signature_files=(
                TOOLS_DIR / "extract_cod_multiplayer_model_assets.py",
                TOOLS_DIR / "cod1_multiplayer_closure.py",
                TOOLS_DIR / "cod1_mp_gsc_content.py",
                TOOLS_DIR / "cod1_script_exploder.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
            ),
        ),
        Step(
            key="stage_ordnance",
            title="Stage official ordnance/UO prerequisites",
            build_argv=lambda c: _python(
                c, "extract_cod1_ordnance.py",
                str(c.game_dir),
                "--pak-root", str(c.content_dir),
                "--staging", str(c.cod1_source),
                "--staging-only",),
            is_done=_ordnance_staging_done,
            signature_files=(
                TOOLS_DIR / "extract_cod1_ordnance.py",
                TOOLS_DIR / "cod1_archive_policy.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="viewmodels",
            title="Export weapon viewmodels (Blender)",
            needs_blender=True,
            build_argv=lambda c: _blender(
                c, "export_cod1_demo_viewmodels.py",
                str(c.cod1_source), str(c.staging_dir / "work" / "viewmodels"),
                str(IMPORTER_PY_ROOT), str(TOOLS_DIR),
                "--format", "glb", "--pak-root", str(c.content_dir)),
            is_done=_viewmodels_done,
            signature_files=(
                TOOLS_DIR / "export_cod1_demo_viewmodels.py",
                TOOLS_DIR / "cod1_weapon_metadata.py",
                TOOLS_DIR / "cod1_xanim.py",
                TOOLS_DIR / "fod_export_common.py",
            ),
            depends_on=("extract_cod1", "stage_ordnance"),
        ),
        Step(
            key="worldmodels",
            title="Export weapon world models (Blender)",
            needs_blender=True,
            build_argv=lambda c: _blender(
                c, "batch_export_cod1_models.py",
                str(c.cod1_source), str(c.staging_dir / "work" / "worldmodels"),
                "weapon", str(IMPORTER_PY_ROOT),
                "--format", "glb", "--pak-root", str(c.content_dir)),
            is_done=lambda c: _all_weapon_paths_exist(c, "worldGlb"),
            signature_files=(
                TOOLS_DIR / "batch_export_cod1_models.py",
                TOOLS_DIR / "fod_export_common.py",
            ),
            depends_on=("extract_cod1", "stage_ordnance", "viewmodels"),
        ),
        Step(
            key="shellcasing",
            title="Export shell casing model (Blender)",
            needs_blender=True,
            build_argv=lambda c: _blender(
                c, "batch_export_cod1_models.py",
                str(c.cod1_source), str(c.staging_dir / "work" / "effects"),
                "effect", str(IMPORTER_PY_ROOT),
                "--format", "glb", "--pak-root", str(c.content_dir)),
            is_done=lambda c: (
                content / "weapons" / "shell" / "shellcasing.glb").is_file(),
            signature_files=(
                TOOLS_DIR / "batch_export_cod1_models.py",
                TOOLS_DIR / "fod_export_common.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="projectiles",
            title="Export projectile models (Blender)",
            needs_blender=True,
            build_argv=lambda c: _blender(
                c, "batch_export_cod1_models.py",
                str(c.cod1_source), str(c.staging_dir / "work" / "projectiles"),
                "projectile", str(IMPORTER_PY_ROOT),
                "--format", "glb", "--pak-root", str(c.content_dir)),
            is_done=lambda c: _all_weapon_paths_exist(
                c,
                "projectileGlb",
                require_when=lambda entry: bool(
                    entry.get("definition", {}).get("projectile_model")
                ),
            ),
            signature_files=(
                TOOLS_DIR / "batch_export_cod1_models.py",
                TOOLS_DIR / "fod_export_common.py",
            ),
            depends_on=("extract_cod1", "stage_ordnance", "viewmodels"),
        ),
        Step(
            key="players",
            title="Export multiplayer players (Blender)",
            needs_blender=True,
            build_argv=lambda c: _blender(
                c, "export_cod1_multiplayer_players.py",
                str(c.cod1_source), str(c.staging_dir / "work" / "players"),
                str(IMPORTER_PY_ROOT), str(TOOLS_DIR),
                "--format", "glb", "--pak-root", str(c.content_dir)),
            is_done=_players_done,
            signature_files=(
                TOOLS_DIR / "export_cod1_multiplayer_players.py",
                TOOLS_DIR / "cod1_playeranim.py",
                TOOLS_DIR / "cod1_xanim.py",
                TOOLS_DIR / "fod_export_common.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="presentation",
            title="Extract multiplayer weapon/global audio + FX",
            build_argv=lambda c: _python(
                c, "extract_cod1_weapon_presentation.py",
                str(c.game_dir / "Main"), str(c.cod1_source),
                "--pak-root", str(c.content_dir),),
            is_done=lambda c: (
                _presentation_has_selected_maps(c)
                and _presentation_has_alias(c, "minefield_click")
                and _presentation_has_alias(c, "explo_mine")
                and _presentation_has_alias(c, "health_pickup_medium")
            ),
            signature_files=(
                TOOLS_DIR / "extract_cod1_weapon_presentation.py",
                TOOLS_DIR / "cod1_mp_gsc_content.py",
                TOOLS_DIR / "cod1_script_exploder.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
            ),
            depends_on=("extract_cod1", "viewmodels"),
        ),
        Step(
            key="footsteps",
            title="Extract footstep audio",
            build_argv=lambda c: _python(
                c, "extract_cod1_footsteps.py",
                str(c.game_dir / "Main"), "--pak-root", str(c.content_dir)),
            is_done=lambda c: _glb_count(
                content / "audio" / "footsteps" / "clips", "*/*.wav") > 0,
            signature_files=(
                TOOLS_DIR / "extract_cod1_footsteps.py",
                TOOLS_DIR / "cod1_archive_policy.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="impacts",
            title="Extract impacts, minefields, whizbys, fatigue, reticle",
            build_argv=lambda c: _python(
                c, "extract_cod1_impacts.py",
                "--game-root", str(c.game_dir),
                "--pak-root", str(c.content_dir),
                "--notes-file", str(c.notes_dir / "impacts.txt"),),
            is_done=lambda c: (
                (content / "ui" / "reticle_q.png").is_file()
                and _glb_count(content / "audio" / "impacts", "*/*.wav") > 0
                and (content / "fx" / "efx" / "minefield.efx").is_file()
                and (
                    content / "fx" / "textures" /
                    "dirtplume_model2.png"
                ).is_file()
                and (content / "fx" / "impacts.json").is_file()
                and _exploders_done(c)),
            signature_files=(
                TOOLS_DIR / "extract_cod1_impacts.py",
                TOOLS_DIR / "cod1_impact_table.py",
                TOOLS_DIR / "cod1_script_exploder.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="mp_scripts",
            title="Extract active multiplayer script presentation closure",
            build_argv=lambda c: _python(
                c, "extract_cod1_mp_gsc_content.py",
                "--game-root", str(c.game_dir),
                "--pak-root", str(c.content_dir),),
            is_done=_mp_scripts_done,
            signature_files=(
                TOOLS_DIR / "extract_cod1_mp_gsc_content.py",
                TOOLS_DIR / "cod1_mp_gsc_content.py",
                TOOLS_DIR / "cod1_script_exploder.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="map_previews",
            title="Extract map-picker level shots",
            # No tier or subset flag: the roster is fixed (_require_uo runs
            # before any step, and test_shipping_map_roster enforces that no
            # step negotiates it), so the tool defaults to the same
            # unconditional seven _selected_map_ids builds.
            build_argv=lambda c: _python(
                c, "extract_cod1_map_previews.py",
                "--game-root", str(c.game_dir),
                "--pak-root", str(c.content_dir),),
            is_done=lambda c: (
                content / "maps" / "previews" /
                "cod1_mp_carentan.png").is_file(),
            signature_files=(
                TOOLS_DIR / "extract_cod1_map_previews.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="hud",
            title="Extract HUD art",
            build_argv=lambda c: _python(
                c, "extract_cod1_hud.py",
                str(c.game_dir),
                "--pak-root", str(c.content_dir),
                "--notes-file", str(c.notes_dir / "hud.txt"),),
            is_done=lambda c: (
                (content / "ui" / "hud" / "compassface.png").is_file()
                and (content / "ui" / "hud" / "health_back.png").is_file()),
            signature_files=(
                TOOLS_DIR / "extract_cod1_hud.py",
                TOOLS_DIR / "cod1_archive_policy.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="ordnance",
            title="Extract ordnance staging, FX + audio",
            build_argv=lambda c: _python(
                c, "extract_cod1_ordnance.py",
                str(c.game_dir),
                "--pak-root", str(c.content_dir),
                "--staging", str(c.cod1_source),),
            is_done=lambda c: (
                (content / "fx" / "textures" / "explosion1.png").is_file()
                and _presentation_has_alias(c, "grenade_bounce_default")
                and _presentation_has_alias(c, "smokegrenade_explode")),
            signature_files=(
                TOOLS_DIR / "extract_cod1_ordnance.py",
                TOOLS_DIR / "cod1_archive_policy.py",
            ),
            depends_on=("extract_cod1", "stage_ordnance", "presentation"),
        ),
        Step(
            key="weapon_data",
            title="Copy WEAPONFILE dumps",
            run_python=_copy_weapon_data,
            is_done=lambda c: (
                (content / "weapons" / "weapons.json").is_file()
                and (content / "weapons" / "data"
                     / "provenance.json").is_file()
                and _glb_count(content / "weapons" / "data", "*.txt")
                >= len(_weapons_json(c).get("weapons", []))),
            signature_files=(
                Path(__file__).resolve(),
                TOOLS_DIR / "cod1_weapon_metadata.py",
            ),
            depends_on=("stage_ordnance", "viewmodels"),
        ),
        Step(
            key="maps",
            title="Import multiplayer maps",
            build_argv=lambda c: _python(
                c, "import_cod_multiplayer_maps.py",
                "--game-root", str(c.game_dir),
                "--pak-root", str(c.content_dir),
                *(["--force"] if c.force else [])),
            is_done=_maps_done,
            signature_files=(
                TOOLS_DIR / "import_cod_multiplayer_maps.py",
                TOOLS_DIR / "cod1_impact_table.py",
                TOOLS_DIR / "cod1_script_exploder.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_shipping_maps.py",
                TOOLS_DIR / "fod_glb_writer.py",
            ),
            depends_on=("extract_cod1",),
        ),
        Step(
            key="props",
            title="Export map props (Blender)",
            needs_blender=True,
            build_argv=lambda c: _blender(
                c, "export_cod_multiplayer_props.py",
                str(c.mp_source), str(_prop_models_json(c)),
                str(c.staging_dir / "work" / "props"), str(IMPORTER_PY_ROOT),
                "--format", "glb", "--pak-root", str(c.content_dir)),
            is_done=_props_done,
            signature_files=(
                TOOLS_DIR / "export_cod_multiplayer_props.py",
                TOOLS_DIR / "fod_export_common.py",
                TOOLS_DIR / "cod-asset-importer" / "python"
                / "cod_asset_importer" / "importer.py",
                TOOLS_DIR / "cod-asset-importer" / "python"
                / "cod_asset_importer" / "__init__.py",
                IMPORTER_NATIVE,
                TOOLS_DIR / "cod-asset-importer" / "rust"
                / "cod_asset_importer" / "Cargo.toml",
                TOOLS_DIR / "cod-asset-importer" / "rust"
                / "cod_asset_importer" / "src" / "loader.rs",
                TOOLS_DIR / "cod-asset-importer" / "rust"
                / "cod_asset_importer" / "src" / "lib.rs",
                TOOLS_DIR / "cod-asset-importer" / "rust"
                / "cod_asset_importer" / "src" / "assets"
                / "xmodel.rs",
            ),
            depends_on=("extract_mp_models", "maps"),
        ),
        Step(
            key="package",
            title="Finalize package manifest",
            build_argv=lambda c: _host(
                c, EXPORTER_DIR / "package.py",
                str(c.content_dir),
                "--game-root", str(c.game_dir),
                "--notes-dir", str(c.notes_dir),
                "--source-root", str(c.cod1_source)),
            is_done=lambda c: False,
            signature_files=(
                EXPORTER_DIR / "package.py",
                TOOLS_DIR / "cod1_archive_policy.py",
                TOOLS_DIR / "cod1_playeranim.py",
                TOOLS_DIR / "cod1_weapon_metadata.py",
            ),
            depends_on=(
                "viewmodels",
                "worldmodels",
                "projectiles",
                "players",
                "presentation",
                "mp_scripts",
                "ordnance",
                "weapon_data",
                "maps",
                "props",
            ),
        ),
    ]


def _forward_item(line: str, on_item: Callable[[int, int, str], None]) -> None:
    """Lift `@fod v1 item done=N total=M label=X` out of a child's stdout.

    The children are separate processes that cannot call back into the
    pipeline, so their per-item counts arrive the only way anything else
    does: as text. A malformed line is ignored rather than fatal — losing a
    progress tick must never lose an export.
    """
    if not line.startswith("@fod v1 item "):
        return
    fields: dict[str, str] = {}
    for token in line[len("@fod v1 item "):].split(" "):
        key, separator, value = token.partition("=")
        if separator:
            fields[key] = value
    try:
        done = int(fields["done"])
        total = int(fields["total"])
    except (KeyError, ValueError):
        return
    on_item(done, total, fields.get("label", ""))


def _run_subprocess(argv: list[str], log: LogFn,
                    cancel: threading.Event | None,
                    kind: str = "host",
                    on_item: Callable[[int, int, str], None] | None = None,
                    ) -> None:
    log("$ " + " ".join(argv))
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", bufsize=1, cwd=str(REPO_ROOT),
        env=fod_paths.child_env(kind))
    name = Path(argv[0]).name
    started = time.monotonic()
    # One-element list, not a plain float: the watchdog thread reads what the
    # reader loop below writes, and a rebound local would not be shared.
    last_output = [started]
    settled = threading.Event()

    def watch_for_silence() -> None:
        while not settled.wait(SILENCE_HEARTBEAT_SECONDS):
            silent = time.monotonic() - last_output[0]
            if silent < SILENCE_HEARTBEAT_SECONDS:
                continue
            log(
                f"  ... {name} still running - "
                f"{time.monotonic() - started:.0f}s elapsed, "
                f"{silent:.0f}s since its last output"
            )

    heartbeat = threading.Thread(
        target=watch_for_silence, name="fod-step-heartbeat", daemon=True)
    heartbeat.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            last_output[0] = time.monotonic()
            stripped = line.rstrip("\n")
            if on_item is not None:
                _forward_item(stripped, on_item)
            # Forwarded verbatim as well: the raw line is what a support log
            # shows, and a consumer that does not know the protocol still
            # sees something readable.
            log(stripped)
            if cancel is not None and cancel.is_set():
                process.terminate()
                process.wait(timeout=30)
                raise PipelineCancelled()
        returncode = process.wait()
    finally:
        settled.set()
        heartbeat.join(timeout=SILENCE_HEARTBEAT_SECONDS)
        if process.poll() is None:
            process.terminate()
    if cancel is not None and cancel.is_set():
        raise PipelineCancelled()
    if returncode != 0:
        raise PipelineError(f"{Path(argv[0]).name} exited with code {returncode}")


def _step_signature(cfg: PipelineConfig, step: Step) -> str:
    files = []
    seen: set[Path] = set()
    for path in step.signature_files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        files.append(
            {
                "path": (
                    resolved.relative_to(REPO_ROOT).as_posix()
                    if resolved.is_relative_to(REPO_ROOT)
                    else str(resolved)
                ),
                "sha256": _sha256(resolved) if resolved.is_file() else "missing",
            }
        )
    dependencies = {}
    for key in step.depends_on:
        marker = cfg.staging_dir / "done" / f"{key}.json"
        dependencies[key] = (
            hashlib.sha256(marker.read_bytes()).hexdigest()
            if marker.is_file()
            else "missing"
        )
    payload = {
        "pipelineSchema": PIPELINE_SCHEMA_VERSION,
        "sourcePolicyVersion": SOURCE_POLICY_VERSION,
        "step": step.key,
        # Only the toolchain THIS step depends on, so a Blender bump
        # re-runs the six Blender steps and leaves maps/ alone. See
        # toolchain.py for why a single global build stamp is wrong here.
        "toolchain": fod_toolchain.toolchain_for(step.key),
        "archiveFingerprint": policy_fingerprint(cfg.game_dir),
        # No tier or subset keys: the roster is fixed at seven maps and
        # United Offensive is mandatory. Dropping them also changes every
        # stored signature, which is what retires the pre-migration markers.
        "maps": {"selected": _selected_map_ids(cfg)},
        "roster": {
            "weapons": sorted(_expected_weapon_ids(cfg)),
            "playerAnimations": PLAYER_ANIMATION_COUNT,
            "maps": _selected_map_ids(cfg),
        },
        "files": files,
        "dependencies": dependencies,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _marker_path(cfg: PipelineConfig, step: Step) -> Path:
    return cfg.staging_dir / "done" / f"{step.key}.json"


def _step_complete(cfg: PipelineConfig, step: Step) -> bool:
    """A step counts as done only when BOTH its output probe passes AND its
    success marker (written after a clean exit) matches the current
    configuration. Output probes alone accept the partial results left by a
    crash or cancellation, because most tools write incrementally."""
    marker = _marker_path(cfg, step)
    if not marker.is_file():
        return False
    try:
        if marker.read_text(encoding="utf-8") != _step_signature(cfg, step):
            return False
    except OSError:
        return False
    return step.is_done(cfg)


def _mark_complete(cfg: PipelineConfig, step: Step) -> None:
    # A tool that exits 0 having written incomplete outputs would otherwise get
    # a valid completion marker, and only the NEXT run's is_done check would
    # notice — by which time the package has already been validated, zipped or
    # mounted. Re-asserting the step's own predicate here turns that into an
    # immediate, named failure.
    #
    # The 'package' step declares is_done=lambda c: False so it always re-runs;
    # asserting that would fail every time, so a step that opts out of caching
    # this way opts out of the check too.
    if step.is_done is not None and not step.is_done(cfg) and step.key != "package":
        raise PipelineError(
            f"step '{step.key}' ({step.title}) exited successfully but its "
            "outputs are incomplete; the content package was not updated"
        )
    marker = _marker_path(cfg, step)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(_step_signature(cfg, step), encoding="utf-8")


def run_pipeline(cfg: PipelineConfig,
                 log: LogFn = print,
                 progress: ProgressFn | None = None,
                 cancel: threading.Event | None = None,
                 only: list[str] | None = None) -> None:
    # First statement, above both mkdirs: a UO-less install must leave no
    # trace, not a half-seeded content root the next run has to reconcile.
    require_united_offensive(cfg.game_dir)
    cfg.content_dir.mkdir(parents=True, exist_ok=True)
    cfg.notes_dir.mkdir(parents=True, exist_ok=True)
    steps = build_steps(cfg)
    if only:
        steps = [step for step in steps if step.key in only]
        if not steps:
            raise PipelineError(f"no steps match {only}")
    total = len(steps)
    # `total` is the number of steps actually being run — --only shortens it,
    # so nothing downstream may assume 19.
    reporter = fod_progress.ExportProgress([step.key for step in steps])
    for index, step in enumerate(steps):
        if cancel is not None and cancel.is_set():
            raise PipelineCancelled()
        if not cfg.force and _step_complete(cfg, step):
            log(f"[{index + 1}/{total}] {step.title} — already done, skipping")
            if progress:
                progress(index, total, step.key, "skipped")
            reporter.skipped(index, total, step.key, step.title)
            continue
        log(f"[{index + 1}/{total}] {step.title}")
        if progress:
            progress(index, total, step.key, "running")
        reporter.begin(index, total, step.key, step.title)
        started = time.monotonic()
        _marker_path(cfg, step).unlink(missing_ok=True)
        if step.run_python is not None:
            step.run_python(cfg, log)
        else:
            assert step.build_argv is not None
            _run_subprocess(
                step.build_argv(cfg), log, cancel,
                kind="blender" if step.needs_blender else "host",
                on_item=lambda done, total, label: reporter.item(
                    step.key, done, total, label),
            )
        _mark_complete(cfg, step)
        if progress:
            progress(index, total, step.key, "done")
        reporter.end(step.key, "done", time.monotonic() - started)
    reporter.done(True, cfg.content_dir)
    log("Pipeline complete.")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="fodpak content directory (the 'current/' root)")
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", nargs="*",
                        help="run only these step keys (still respects order)")
    args = parser.parse_args()
    cfg = PipelineConfig(
        game_dir=args.game_dir.resolve(),
        content_dir=args.output.resolve(),
        blender=args.blender.resolve() if args.blender else None,
        force=args.force,
    )
    run_pipeline(cfg, only=args.only)


if __name__ == "__main__":
    main()
