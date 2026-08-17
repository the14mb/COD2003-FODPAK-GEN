#!/usr/bin/env python3
"""Finalize a fodpak content directory: validate rosters, write fodpak.json,
optionally zip the directory into a transportable .fodpak file.

Contract: Docs/CONTENT_PIPELINE.md §1.2/§1.3. All paths inside the manifest
are relative to the content root; fodpak.json is JsonUtility-compatible
(flat fields, arrays of objects, no dictionary maps).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import io
import json
import math
import re
import struct
import sys
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

EXPORTER_DIR = Path(__file__).resolve().parent
if (EXPORTER_DIR / "tools").is_dir():
    REPO_ROOT = EXPORTER_DIR
    TOOLS_DIR = EXPORTER_DIR / "tools"
else:
    REPO_ROOT = EXPORTER_DIR.parent
    TOOLS_DIR = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS_DIR))

from cod1_archive_policy import (  # noqa: E402
    COD1_ARCHIVES,
    LayeredArchiveIndex,
    UO_ARCHIVES,
    POLICY_FORMAT,
    POLICY_VERSION,
    selected_archives,
    source_policy_manifest,
    uo_installation_status,
    validate_policy_manifest,
    write_policy_manifest,
)
from cod1_playeranim import (  # noqa: E402
    EXCLUDED_TURRET_ALIASES,
    PLAYER_ACTIVE_IDENTIFIER_COUNT,
    PLAYER_ANIMATION_COUNT,
    SCRIPT_TURRET_ALIASES,
    TURRET_ACTION_ROLES,
    TURRET_EXPANSION_COUNT,
    build_inventory,
)
from cod1_xanim import load as load_xanim  # noqa: E402
from cod1_multiplayer_closure import (  # noqa: E402
    BASE_WEAPON_FILES,
    MOUNTED_MG42_ANIMATIONS,
    MOUNTED_MG42_MODEL,
    MOUNTED_MG42_WEAPON_FILES,
    UO_WEAPON_FILES,
    is_managed_source_path,
)
from cod1_impact_table import (  # noqa: E402
    IMPACTS_FORMAT,
    IMPACTS_VERSION,
    is_closure_impact_type,
)
from cod1_script_exploder import (  # noqa: E402
    ScriptExploderClosureError,
    efx_shader_names,
    resolve_shader_images,
)
from cod1_shipping_maps import (  # noqa: E402
    BASE_SHIPPING_MAP_IDS,
    UO_SHIPPING_MAP_IDS,
    selected_shipping_map_ids,
    shipping_map_specs,
)

FORMAT_NAME = "FriendsOfDuty.ContentPackage"
PACKAGE_VERSION = 1
# 2: Call of Duty + United Offensive became mandatory. Kept in lockstep with
# FodPakManifest.ExpectedGameContentVersion so an already-installed base-only
# package is refused at mount and routed to the erase-and-regenerate screen.
# 4: the player animation inventory changed shape-compatibly — MG42 gunner
# clips no longer claim the guarded movement:combat/idle their turretanim
# alias is bound under, and the three torso clips bound only on the retail
# script's jeep-passenger lines are gone, so the count is 351 and not 354.
# The manifests' own schema versions could not carry this: they describe the
# SHAPE of a catalog, that shape did not change, and the build's
# ValidateExporterSchemaContract requires weapons, players and
# PIPELINE_SCHEMA_VERSION to move as one. A v3 build meeting this package
# would otherwise have failed deep inside ValidateMg42TurretAnimation
# instead of at the mount gate.
GAME_CONTENT_VERSION = 4
EXPORTER_VERSION = "2.0.0"
# The experience contract this exporter's packages claim. Mirrors
# Cod1ExperienceProfile.ProfileId in Assets/Scripts/Content: the runtime
# resolves the profile named here and validates the package against it, so
# this string and that one must agree.
PROFILE_ID = "cod1"
PROFILE_DISPLAY_NAME = "Call of Duty + United Offensive"
PROFILE_DESCRIPTION = (
    "Weapons, maps, characters and effects imported from your own "
    "installed copy of Call of Duty and United Offensive."
)
PROFILE_CREATOR = "Generated locally by the Friends of Duty importer"
EXPECTED_PLAYERS = 4
AUDIO_EXTENSIONS = (".wav", ".mp3")

ORDNANCE_WEAPONS = ("fraggrenade", "mk1britishfrag", "rgd33", "stielhandgranate")
ORDNANCE_ALIASES = (
    "grenade_bounce_default", "grenade_explode_default", "rocket_explode_default")
ORDNANCE_FX_TEXTURE = "fx/textures/explosion1.png"
MINEFIELD_ALIAS_VARIANTS = {
    "minefield_click": [{
        "file": "misc/metal_click.wav",
        "volume_min": 0.8,
        "volume_max": 0.94,
        "pitch_min": 1.0,
        "pitch_max": 1.0,
        "distance_min_inches": 500.0,
        "distance_max_inches": 0.0,
    }],
    "explo_mine": [{
        "file": "explosions/explo_mine01.wav",
        "volume_min": 1.5,
        "volume_max": 1.5,
        "pitch_min": 0.8,
        "pitch_max": 1.1,
        "distance_min_inches": 750.0,
        "distance_max_inches": 6000.0,
    }],
}
HEALTH_PICKUP_ALIAS_VARIANTS = {
    "health_pickup_medium": [{
        "file": "misc/pu_health01.wav",
        "volume_min": 0.67,
        "volume_max": 0.8,
        "pitch_min": 0.9,
        "pitch_max": 1.1,
        "distance_min_inches": 60.0,
        "distance_max_inches": 120.0,
    }],
}
UO_SCRIPT_EXPLODER_ALIAS_VARIANTS: dict[
    str,
    list[dict[str, object]],
] = {}
MINEFIELD_FX_TEXTURES = (
    "dirtplume_model2",
    "dirtplume_modelf",
    "cloudflash1",
    "cloudflash_stalingradm",
    "dustlayer1",
    "groundblastdark_gib",
    "explosion_1",
    "groundflash1",
)
MINEFIELD_EFX = (
    "minefield",
    "fluff1",
    "dirthit_mortar",
)
HEALTH_PICKUP_MODEL = "health_medium"
# The exact script_exploder fxId roster of the seven shipping maps. Only
# three carry exploders at all; the other four must stay empty.
BASE_SCRIPT_EXPLODER_EFFECT_IDS = {
    "mp_pavlov": ("antitankgunexplosion",),
    "mp_railyard": ("tigertankexplosion",),
    "mp_rocket": ("fueltank", "v2rocketexplosion"),
}
UO_SCRIPT_EXPLODER_EFFECT_IDS: dict[str, tuple[str, ...]] = {}
# United Offensive always ships, so only the full-tier row is reachable.
SCRIPT_EXPLODER_CLOSURE_COUNTS = {
    'maps': 3,
    'effects': 4,
    'efxFiles': 2,
    'textureFiles': 6,
    'modelNames': 0,
    'aliasNames': 0,
}
MP_GSC_CONTENT_FORMAT = "FriendsOfDuty.MultiplayerGscRuntimeContent"
MP_GSC_CONTENT_VERSION = 1
MP_GSC_ANALYSIS_FORMAT = "FriendsOfDuty.MultiplayerGscContentClosure"
MP_GSC_ANALYSIS_VERSION = 1
# United Offensive always ships, so only the full-tier row is reachable.
MP_GSC_CONTENT_COUNTS = {
    'scripts': 39,
    'soundAliases': 184,
    'modelNames': 2,
    'shaderNames': 56,
    'effectPaths': 12,
    'unresolvedSinks': 11,
    'excludedScriptCalls': 16,
    'resolvedEffects': 11,
    'recursiveEfxFiles': 13,
    'resolvedShaderHandles': 48,
    'textureFiles': 88,
    'runtimeSoundAliases': 184,
    'runtimeModelNames': 2,
    'missingRetailEffects': 1,
    'unresolvedShaders': 8,
}
# United Offensive always ships, so only the full-tier row is reachable.
MP_GSC_MISSING_RETAIL_EFFECTS = ('fx/map_mp/mp_exp_generic_complete.efx',)
MP_GSC_MISSING_RETAIL_EFFECT_REASON = (
    "active maps/mp loadFx registration is absent from the selected "
    "retail archives"
)
# The one active maps/mp alias the retail soundalias CSVs never define.
# mp_bomb1_defuse and radio_neutral left this set with the Search & Destroy
# and Headquarters scripts that were their only callers.
MP_GSC_SILENT_SOUND_ALIASES = frozenset(('mp_demotion',))
MP_GSC_ENGINE_BUILTIN_SHADERS = frozenset((
    "black",
    "default",
    "hudscoreboard_mp",
    "white",
))
MP_GSC_UNRESOLVED_SHADER_REASON = (
    "active maps/mp shader handle has no retail image/declaration"
)

# The 24 multiplayer weapons: 19 firearms/launcher, four nation grenades, and
# United Offensive's M18 smoke grenade. UO is mandatory, so the smoke grenade
# is not an optional extra — it is a regular loadout entry.
REQUIRED_WEAPON_IDS = frozenset((
    # Base Call of Duty multiplayer.
    "bar", "bren", "colt45", "enfield", "fg42", "fraggrenade",
    "kar98k", "kar98k_scoped", "luger", "m1carbine", "m1garand",
    "mk1britishfrag", "mosin_nagant", "mosin_nagant_scoped", "mp40",
    "mp44", "panzerfaust", "ppsh", "rgd33", "smokegrenade", "springfield",
    "sten", "stielhandgranate", "thompson",
    # United Offensive's multiplayer additions. UO always mounts, so these
    # are as mandatory as the base set.
    "bazooka", "binoculars", "binoculars_artillery", "dp28",
    "flamethrower", "flashgrenade", "gewehr43", "mg30cal", "mg34",
    "panzerschreck", "satchelcharge", "sten_silenced", "svt40", "tt33",
    "webley",
))
# Equipment that is carried in a weapon slot but never fires. The
# animation-role contract below demands a fire clip of every weapon, which
# these cannot satisfy: retail's own binocular "ADSfire" XAnim is a
# degenerate placeholder with no bone-name table at all.
NON_FIRING_WEAPON_IDS = frozenset((
    "binoculars",
    "binoculars_artillery",
))
# Validator expectations: the extracted value must equal the stock retail
# MP WEAPONFILE. United Offensive's archives override the base WEAPONFILEs
# wherever both define a weapon, and UO always mounts, so these are the only
# values a correct package can carry.
MULTIPLAYER_DROP_AMMO_BOUNDS = {
    'bar': (20, 40),
    'bren': (30, 60),
    'colt45': (14, 21),
    'enfield': (20, 30),
    'fg42': (20, 40),
    'fraggrenade': (0, 0),
    'kar98k': (15, 30),
    'kar98k_scoped': (15, 20),
    'luger': (16, 24),
    'm1carbine': (30, 45),
    'm1garand': (16, 24),
    'mk1britishfrag': (0, 0),
    'mosin_nagant': (20, 30),
    'mosin_nagant_scoped': (15, 20),
    'mp40': (32, 64),
    'mp44': (30, 60),
    'panzerfaust': (0, 0),
    'ppsh': (35, 71),
    'rgd33': (0, 0),
    'smokegrenade': (0, 0),
    'springfield': (15, 20),
    'sten': (32, 64),
    'stielhandgranate': (0, 0),
    'thompson': (30, 60),
    # United Offensive's own MP additions. Read out of
    # uo/pakuo04.pk3 weapons/mp/* the same way the base rows were.
    'bazooka': (1, 3),
    'binoculars': (0, 0),
    'binoculars_artillery': (10, 10),
    'dp28': (75, 150),
    'flamethrower': (100, 150),
    'flashgrenade': (0, 0),
    'gewehr43': (20, 30),
    'mg30cal': (75, 150),
    'mg34': (75, 150),
    'panzerschreck': (1, 3),
    'satchelcharge': (0, 0),
    'sten_silenced': (32, 64),
    'svt40': (20, 30),
    'tt33': (14, 21),
    'webley': (14, 21),
}
# Validator expectations: the extracted value must equal the stock retail
# MP WEAPONFILE. United Offensive's archives override the base WEAPONFILEs
# wherever both define a weapon, and UO always mounts, so these are the only
# values a correct package can carry.
MULTIPLAYER_MAX_AMMO = {
    'bar': 300,
    'bren': 300,
    'colt45': 56,
    'enfield': 160,
    'fg42': 320,
    'fraggrenade': 3,
    'kar98k': 125,
    'kar98k_scoped': 150,
    'luger': 64,
    'm1carbine': 400,
    'm1garand': 240,
    'mk1britishfrag': 3,
    'mosin_nagant': 150,
    'mosin_nagant_scoped': 150,
    'mp40': 320,
    'mp44': 240,
    'panzerfaust': 1,
    'ppsh': 355,
    'rgd33': 3,
    'smokegrenade': 3,
    'springfield': 200,
    'sten': 320,
    'stielhandgranate': 3,
    'thompson': 360,
    # United Offensive's own MP additions. binoculars carry no ammo at all;
    # the artillery spotter's 10 is its call count.
    'bazooka': 3,
    'binoculars': 0,
    'binoculars_artillery': 10,
    'dp28': 375,
    'flamethrower': 500,
    'flashgrenade': 3,
    'gewehr43': 250,
    'mg30cal': 375,
    'mg34': 375,
    'panzerschreck': 3,
    'satchelcharge': 1,
    'sten_silenced': 320,
    'svt40': 250,
    'tt33': 56,
    'webley': 56,
}
# Backwards-compatible aliases for package callers; the authoritative values
# live in the dependency-light shared shipping-roster module.
BASE_MAP_IDS = frozenset(BASE_SHIPPING_MAP_IDS)
UO_MAP_IDS = frozenset(UO_SHIPPING_MAP_IDS)
REQUIRED_WEAPON_ROLES = frozenset(("idle", "fire", "melee"))
ALTERNATE_WEAPON_FILES = frozenset((
    "bar_slow_mp",
    "fg42_semi_mp",
    "mp44_semi_mp",
    "ppsh_semi_mp",
    "thompson_semi_mp",
))
# United Offensive always ships, so only the full-tier row is reachable.
# Both counts grew when the roster took on UO's own multiplayer weapons
# (see UO_WEAPON_FILES in tools/cod1_multiplayer_closure.py): 1095 -> 1347
# files and 78 -> 109 XModels. The file count is thirteen lower than the
# weapon roster alone implies: the six jeep driver/gunner XAnims and the four
# jeep passenger clips left with the vehicles, and so did the three torso
# clips the retail script binds only on those same jeep-passenger lines
# (1347 -> 1344; see EXCLUDED_VEHICLE_TORSO_ANIMATIONS in
# tools/cod1_playeranim.py).
SOURCE_CLOSURE_FILE_COUNT = 1344
# United Offensive always ships, so only the full-tier row is reachable.
SOURCE_CLOSURE_MODEL_COUNT = 109

# Bullet-impact art is warn-only: FodImpactContentBuilder drops any mark or
# sprite it cannot load and still builds a usable profile from the rest.
# Marks = the polygonOffset2 shaders in pak5 fxshaders/pj_impact.shader +
# pj_fx.shader; sprites = the billboards the fx/impacts/*.efx emitters name.
IMPACT_MARK_TEXTURES = (
    "bullethole1", "bullethole2",
    "bullethit_plaster", "bullethit_plaster2",
    "bullethit_wood1", "bullethit_wood2",
    "bullethit_sand", "bullethit_snow",
    "bullethit_glass", "bullethit_glass2",
    "stone_singleshot1",
)
IMPACT_SPRITE_TEXTURES = (
    "stone_piece1", "stone_gib1", "dustlayer1",
    "wood_splinter1", "woodpuff",
    "metal_spark1", "sparktrail",
    "snowpuff", "snow1",
    "gravelpuff",
    "grass_piece1", "grass_piece2", "foliage_gib1", "foliage_gib2",
    "flesh_hit1",
)
# The Decal blocks in these are the authority on per-surface mark selection.
IMPACT_REFERENCE_EFX = (
    "default_hit", "small_plaster", "small_gravel", "small_glass",
    "woodhit_small", "snowhit_small", "metalhit_small",
)

# fx/shaders.json: shader-name -> packaged texture, for every shader named by
# an efx under fx/efx/. Warn-only and additive — the runtime guessed texture
# basenames before this manifest existed and still can.
FX_SHADERS_FORMAT = "FriendsOfDuty.FxShaders"
FX_SHADERS_VERSION = 1


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def selected_map_roster() -> frozenset[tuple[str, str]]:
    """The exact map/game pairs a shipped package must contain — all seven."""
    return frozenset(
        (game, map_id)
        for game, _source_tier, map_id in shipping_map_specs(include_uo=True)
    )


def validate_map_catalog_roster(
    maps: object,
    errors: list[str],
) -> set[tuple[str, str]]:
    """Validate exact membership, tier ownership, and multiplicity."""
    if not isinstance(maps, list):
        errors.append("maps/catalog.json maps member is not an array")
        return set()

    records: list[tuple[str, str]] = []
    malformed = False
    for entry in maps:
        if not isinstance(entry, dict):
            malformed = True
            continue
        game = entry.get("game")
        map_id = entry.get("mapId")
        if (
            not isinstance(game, str)
            or not game
            or not isinstance(map_id, str)
            or not map_id
        ):
            malformed = True
            continue
        records.append((game, map_id))
    if malformed:
        errors.append("map catalog roster contains a malformed entry")

    actual = set(records)
    duplicates = sorted(
        record for record in actual if records.count(record) > 1
    )
    if duplicates:
        errors.append(
            "map catalog roster contains duplicate map/game pairs"
            f"; duplicates={duplicates}"
        )

    expected = set(selected_map_roster())
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        errors.append(
            "map catalog does not contain exactly the approved shipping maps"
            + (f"; missing={missing}" if missing else "")
            + (f"; extra={extra}" if extra else "")
        )
    return actual


def validate_shared_map_texture_closure(
    content: Path,
    maps: object,
    errors: list[str],
) -> set[str]:
    """Require maps/shared to equal approved material texture reachability."""
    expected_roster = set(selected_map_roster())
    referenced: set[str] = set()
    entries = maps if isinstance(maps, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        game = entry.get("game")
        map_id = entry.get("mapId")
        if (game, map_id) not in expected_roster:
            continue
        label = f"map {map_id} materials"
        materials_path = content_path(
            content,
            entry.get("materialsJson"),
            label,
            errors,
        )
        if materials_path is None or not materials_path.is_file():
            continue
        materials = load_json(materials_path)
        if not isinstance(materials, list):
            errors.append(f"{label} is not an array")
            continue
        for index, material in enumerate(materials):
            texture_label = f"{label}[{index}] texture"
            if not isinstance(material, dict):
                errors.append(f"{label}[{index}] is malformed")
                continue
            texture = material.get("texture")
            if texture in (None, ""):
                continue
            normalized = _mp_gsc_path(
                texture,
                texture_label,
                errors,
            )
            if normalized is None:
                continue
            expected_prefix = f"maps/shared/{game}/textures/"
            if (
                not normalized.casefold().startswith(
                    expected_prefix.casefold()
                )
                or not normalized.casefold().endswith(".png")
            ):
                errors.append(
                    f"{texture_label} is outside the selected tier's shared "
                    f"PNG root: {normalized}"
                )
                continue
            referenced.add(normalized)
            if not (content / PurePosixPath(normalized)).is_file():
                errors.append(
                    f"{texture_label} references missing file '{normalized}'"
                )

    shared_root = content / "maps" / "shared"
    actual = {
        path.relative_to(content).as_posix()
        for path in shared_root.rglob("*")
        if path.is_file()
    } if shared_root.is_dir() else set()
    missing = sorted(referenced - actual, key=str.casefold)
    unreferenced = sorted(actual - referenced, key=str.casefold)
    if missing or unreferenced:
        def preview(paths: list[str]) -> str:
            visible = paths[:8]
            suffix = (
                f", ... (+{len(paths) - len(visible)})"
                if len(paths) > len(visible)
                else ""
            )
            return "[" + ", ".join(visible) + suffix + "]"

        details = []
        if missing:
            details.append(f"missing={preview(missing)}")
        if unreferenced:
            details.append(f"unreferenced={preview(unreferenced)}")
        errors.append(
            "maps/shared files differ from approved map material reachability"
            f"; {'; '.join(details)}"
        )
    return referenced


def count_files(root: Path, extensions: tuple[str, ...] | None = None) -> int:
    if not root.is_dir():
        return 0
    return sum(
        1 for path in root.rglob("*")
        if path.is_file()
        and (extensions is None or path.suffix.lower() in extensions))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_path(
    content: Path,
    relative: object,
    label: str,
    errors: list[str],
) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        errors.append(f"{label}: empty content path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        errors.append(f"{label}: unsafe content path '{relative}'")
        return None
    current = content
    actual_parts: list[str] = []
    case_mismatch = False
    for part in candidate.parts:
        if not current.is_dir():
            break
        try:
            children = list(current.iterdir())
        except OSError:
            break
        exact = next(
            (child for child in children if child.name == part),
            None,
        )
        if exact is not None:
            selected = exact
        else:
            folded = [
                child
                for child in children
                if child.name.casefold() == part.casefold()
            ]
            if not folded:
                break
            selected = sorted(folded, key=lambda child: child.name)[0]
            case_mismatch = True
        actual_parts.append(selected.name)
        current = selected
    if case_mismatch:
        actual = PurePosixPath(*actual_parts).as_posix()
        errors.append(
            f"{label}: content path casing differs from disk: "
            f"'{relative}' (actual '{actual}')"
        )
    return content / candidate


def validate_prop_lods(
    content: Path,
    prop: dict[str, object],
    errors: list[str],
    required: bool = False,
) -> None:
    """Validate the optional authored XModel LOD inventory."""
    lods = prop.get("lods")
    if lods is None:
        if required:
            errors.append(
                f"prop {prop.get('name', '?')}: authored lods are missing"
            )
        return

    label = f"prop {prop.get('name', '?')}"
    if not isinstance(lods, list) or not lods:
        errors.append(f"{label}: lods must be a non-empty array")
        return
    if (
        not isinstance(lods[0], dict)
        or lods[0].get("glb") != prop.get("glb")
    ):
        errors.append(f"{label}: LOD0 glb differs from the primary glb")

    paths: set[str] = set()
    previous_distance = 0.0
    for index, lod in enumerate(lods):
        lod_label = f"{label} LOD{index}"
        if not isinstance(lod, dict):
            errors.append(f"{lod_label}: malformed LOD metadata")
            continue

        relative = lod.get("glb")
        path = content_path(content, relative, f"{lod_label} glb", errors)
        if path is not None and not path.is_file():
            errors.append(f"{lod_label}: missing glb '{relative}'")
        if isinstance(relative, str) and relative:
            normalized = relative.casefold()
            if normalized in paths:
                errors.append(f"{lod_label}: duplicate glb '{relative}'")
            paths.add(normalized)

        surface = lod.get("sourceSurface")
        if not isinstance(surface, str) or not surface.strip():
            errors.append(f"{lod_label}: sourceSurface is empty")

        distance = lod.get("distance")
        if (
            isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or not math.isfinite(float(distance))
            or float(distance) < 0.0
        ):
            errors.append(f"{lod_label}: distance is invalid")
            continue
        distance = float(distance)
        final = index == len(lods) - 1
        if not final and distance <= previous_distance:
            errors.append(
                f"{lod_label}: distance must be positive and strictly "
                "increasing"
            )
        elif final and distance > 0.0 and distance <= previous_distance:
            errors.append(
                f"{lod_label}: cull distance must exceed the prior LOD"
            )
        if distance > 0.0:
            previous_distance = distance


def validate_map_optimization(
    content: Path,
    relative: object,
    map_id: object,
    world_glb: object,
    errors: list[str],
) -> None:
    """Validate one versioned offline sector/PVS document and its closure."""
    label = f"map {map_id} optimization"
    path = content_path(content, relative, label, errors)
    if path is None:
        return
    document = load_json(path)
    if not isinstance(document, dict):
        errors.append(f"{label}: missing or invalid JSON")
        return
    if (
        document.get("format") != "FriendsOfDuty.MapOptimization"
        or document.get("version") != 2
        or document.get("sourceBspVersion") != 59
    ):
        errors.append(f"{label}: unsupported format/version")
        return
    if document.get("fallbackWorldGlb") != world_glb:
        errors.append(f"{label}: fallbackWorldGlb differs from catalog")
    scale = document.get("codUnitToMetre")
    if (
        isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or not math.isfinite(float(scale))
        or abs(float(scale) - 0.0254) > 1e-9
    ):
        errors.append(f"{label}: codUnitToMetre must be canonical 0.0254")
    if (
        document.get("sectorStrategy") != "unity-xz-grid-v1"
        or document.get("sectorSizeMetres") != 32.0
    ):
        errors.append(f"{label}: sector grid contract is invalid")

    visibility = document.get("visibility")
    if not isinstance(visibility, dict):
        errors.append(f"{label}: visibility member is malformed")
        return
    integer_fields = (
        "clusterCount",
        "rowBytes",
        "planeCount",
        "nodeCount",
        "leafCount",
    )
    if not all(
        isinstance(visibility.get(field), int)
        and not isinstance(visibility[field], bool)
        and visibility[field] > 0
        for field in integer_fields
    ):
        errors.append(f"{label}: visibility counts are invalid")
        return
    cluster_count = visibility["clusterCount"]
    row_bytes = visibility["rowBytes"]
    plane_count = visibility["planeCount"]
    node_count = visibility["nodeCount"]
    leaf_count = visibility["leafCount"]
    if row_bytes < (cluster_count + 7) // 8 or row_bytes % 4:
        errors.append(f"{label}: PVS row width is invalid")
        return

    def decode(field: str, expected: int) -> bytes | None:
        encoded = visibility.get(field)
        if not isinstance(encoded, str) or not encoded:
            errors.append(f"{label}: {field} is missing")
            return None
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            errors.append(f"{label}: {field} is not canonical base64")
            return None
        if len(payload) != expected:
            errors.append(
                f"{label}: {field} length {len(payload)} != {expected}"
            )
            return None
        return payload

    pvs = decode("pvsBase64", cluster_count * row_bytes)
    planes = decode("planesBase64", plane_count * 16)
    nodes = decode("nodesBase64", node_count * 12)
    leaves = decode("leavesBase64", leaf_count * 8)
    cells = visibility.get("cells")
    if not isinstance(cells, list) or not cells:
        errors.append(f"{label}: cells are missing")
        return
    for cell_index, cell in enumerate(cells):
        minimum = cell.get("minimum") if isinstance(cell, dict) else None
        maximum = cell.get("maximum") if isinstance(cell, dict) else None
        if (
            not isinstance(minimum, list)
            or not isinstance(maximum, list)
            or len(minimum) != 3
            or len(maximum) != 3
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in minimum + maximum
            )
            or any(minimum[axis] > maximum[axis] for axis in range(3))
        ):
            errors.append(f"{label}: cell {cell_index} bounds are invalid")
            return

    if pvs is not None:
        for cluster in range(cluster_count):
            if not (
                pvs[cluster * row_bytes + cluster // 8]
                & (1 << (cluster & 7))
            ):
                errors.append(
                    f"{label}: cluster {cluster} cannot see itself"
                )
                break
    if planes is not None:
        for offset in range(0, len(planes), 16):
            if not all(
                math.isfinite(value)
                for value in struct.unpack_from("<4f", planes, offset)
            ):
                errors.append(f"{label}: plane data is non-finite")
                break
    if nodes is not None:
        decoded_nodes: list[tuple[int, int, int]] = []
        for node_index in range(node_count):
            plane, front, back = struct.unpack_from(
                "<3i",
                nodes,
                node_index * 12,
            )
            decoded_nodes.append((plane, front, back))
            if plane < 0 or plane >= plane_count:
                errors.append(
                    f"{label}: node {node_index} has invalid plane"
                )
                return
            for child in (front, back):
                if child >= node_count or (
                    child < 0 and -1 - child >= leaf_count
                ):
                    errors.append(
                        f"{label}: node {node_index} has invalid child"
                    )
                    return
        state: dict[int, int] = {}
        stack: list[tuple[int, bool]] = [(0, False)]
        reached_leaf = False
        while stack:
            node_index, leaving = stack.pop()
            if node_index < 0:
                reached_leaf = True
                continue
            if leaving:
                state[node_index] = 2
                continue
            status = state.get(node_index, 0)
            if status == 1:
                errors.append(f"{label}: BSP root contains a cycle")
                return
            if status == 2:
                continue
            state[node_index] = 1
            stack.append((node_index, True))
            _plane, front, back = decoded_nodes[node_index]
            stack.append((back, False))
            stack.append((front, False))
        if not reached_leaf:
            errors.append(f"{label}: BSP root reaches no leaves")
            return
    cluster_cells: list[int | None] = [None] * cluster_count
    if leaves is not None:
        for leaf_index in range(leaf_count):
            cluster, cell = struct.unpack_from(
                "<2i",
                leaves,
                leaf_index * 8,
            )
            if (
                cluster < -1
                or cluster >= cluster_count
                or cell < -1
                or cell >= len(cells)
                or (cluster < 0) != (cell < 0)
            ):
                errors.append(f"{label}: leaf {leaf_index} is invalid")
                return
            if cluster >= 0:
                previous = cluster_cells[cluster]
                if previous is not None and previous != cell:
                    errors.append(
                        f"{label}: cluster {cluster} spans cells"
                    )
                    return
                cluster_cells[cluster] = cell
        if any(cell is None for cell in cluster_cells):
            errors.append(f"{label}: not every cluster maps to a cell")

    sectors = document.get("sectors")
    if not isinstance(sectors, list) or not sectors:
        errors.append(f"{label}: sectors are missing")
        return
    names: set[str] = set()
    glb_paths: set[str] = set()
    grid_sectors: set[tuple[int, int]] = set()
    always_visible_sectors = 0
    triangle_total = 0
    actual_sector_triangle_total = 0
    all_sector_glbs_valid = True
    always_visible_total = 0
    for index, sector in enumerate(sectors):
        sector_label = f"{label} sector[{index}]"
        if not isinstance(sector, dict):
            errors.append(f"{sector_label}: malformed")
            all_sector_glbs_valid = False
            continue
        name = sector.get("name")
        glb_relative = sector.get("glb")
        glb_parts = (
            glb_relative.replace("\\", "/").split("/")
            if isinstance(glb_relative, str)
            else []
        )
        glb_key = (
            "/".join(glb_parts).casefold()
            if glb_parts
            else ""
        )
        grid_x = sector.get("gridX")
        grid_z = sector.get("gridZ")
        cluster_indices = sector.get("clusterIndices")
        always = sector.get("alwaysVisible")
        triangles = sector.get("triangles")
        bounds = sector.get("boundsCodUnits")
        minimum = bounds.get("minimum") if isinstance(bounds, dict) else None
        maximum = bounds.get("maximum") if isinstance(bounds, dict) else None
        grid = (grid_x, grid_z)
        if (
            not isinstance(name, str)
            or not name
            or name in names
            or not glb_key
            or any(
                not part or part in (".", "..")
                for part in glb_parts
            )
            or glb_key in glb_paths
            or isinstance(grid_x, bool)
            or not isinstance(grid_x, int)
            or isinstance(grid_z, bool)
            or not isinstance(grid_z, int)
            or not isinstance(cluster_indices, list)
            or not all(
                isinstance(cluster, int)
                and not isinstance(cluster, bool)
                and 0 <= cluster < cluster_count
                for cluster in cluster_indices
            )
            or cluster_indices != sorted(set(cluster_indices))
            or not isinstance(always, bool)
            or not isinstance(triangles, int)
            or isinstance(triangles, bool)
            or triangles <= 0
            or (always and cluster_indices)
            or (not always and not cluster_indices)
            or (not always and grid in grid_sectors)
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
            errors.append(f"{sector_label}: invalid identity/counts")
            all_sector_glbs_valid = False
            continue
        names.add(name)
        glb_paths.add(glb_key)
        if always:
            always_visible_sectors += 1
            if always_visible_sectors > 1:
                errors.append(
                    f"{sector_label}: duplicate always-visible sector"
                )
        else:
            grid_sectors.add(grid)
        triangle_total += triangles
        if always:
            always_visible_total += triangles
        glb = content_path(
            content,
            glb_relative,
            f"{sector_label} glb",
            errors,
        )
        if glb is not None:
            if not glb.is_file():
                errors.append(f"{sector_label}: GLB is missing")
                all_sector_glbs_valid = False
            else:
                try:
                    actual_triangles = glb_triangle_count(glb)
                except (OSError, ValueError, json.JSONDecodeError) as exception:
                    errors.append(
                        f"{sector_label}: invalid GLB ({exception})"
                    )
                    all_sector_glbs_valid = False
                else:
                    actual_sector_triangle_total += actual_triangles
                    if actual_triangles != triangles:
                        errors.append(
                            f"{sector_label}: GLB contains "
                            f"{actual_triangles} triangles, declares "
                            f"{triangles}"
                        )
                        all_sector_glbs_valid = False
        else:
            all_sector_glbs_valid = False
    counts = document.get("counts")
    if (
        not isinstance(counts, dict)
        or counts.get("sectors") != len(sectors)
        or counts.get("triangles") != triangle_total
        or counts.get("alwaysVisibleTriangles")
        != always_visible_total
        or counts.get("clusters") != cluster_count
        or counts.get("cells") != len(cells)
    ):
        errors.append(f"{label}: aggregate counts disagree")

    fallback = content_path(
        content,
        world_glb,
        f"{label} fallback world GLB",
        errors,
    )
    if fallback is not None:
        if not fallback.is_file():
            errors.append(f"{label}: fallback world GLB is missing")
        else:
            try:
                fallback_triangles = glb_triangle_count(fallback)
            except (OSError, ValueError, json.JSONDecodeError) as exception:
                errors.append(
                    f"{label}: fallback world GLB is invalid ({exception})"
                )
            else:
                if fallback_triangles != triangle_total:
                    errors.append(
                        f"{label}: fallback world has "
                        f"{fallback_triangles} triangles, sectors declare "
                        f"{triangle_total}"
                    )
                if (
                    all_sector_glbs_valid
                    and actual_sector_triangle_total != triangle_total
                ):
                    errors.append(
                        f"{label}: sector GLBs contain "
                        f"{actual_sector_triangle_total} triangles, declare "
                        f"{triangle_total}"
                    )


def validate_map_lightmaps(
    content: Path,
    relative: object,
    map_id: object,
    game: object,
    errors: list[str],
) -> None:
    """Validate one CLASSIC world-lightmap manifest and its pages (M3).

    Only reached when a catalog entry declares a non-empty lightmapsJson. Keeps
    a broken bake (missing/duplicate/misnamed pages, wrong count) out of a
    shipped package; the runtime otherwise silently falls back to the M2 grid.
    """
    label = f"map {map_id} lightmaps"
    path = content_path(content, relative, label, errors)
    if path is None:
        return
    if not path.is_file():
        errors.append(f"{label}: manifest is missing '{relative}'")
        return
    document = load_json(path)
    if not isinstance(document, dict):
        errors.append(f"{label}: missing or invalid JSON")
        return
    if (
        document.get("format") != "FriendsOfDuty.MapLightmaps"
        or document.get("version") != 1
    ):
        errors.append(f"{label}: unsupported format/version")
        return
    if not all(
        isinstance(document.get(field), int)
        and not isinstance(document[field], bool)
        and document[field] > 0
        for field in ("pageWidth", "pageHeight")
    ):
        errors.append(f"{label}: page dimensions are invalid")
    multiplier = document.get("decodeMultiplier")
    if (
        isinstance(multiplier, bool)
        or not isinstance(multiplier, (int, float))
        or not math.isfinite(float(multiplier))
        or float(multiplier) <= 0.0
    ):
        errors.append(f"{label}: decodeMultiplier is invalid")
    pages = document.get("pages")
    if not isinstance(pages, list) or not pages:
        errors.append(f"{label}: pages array is missing")
        return
    if document.get("pageCount") != len(pages):
        errors.append(
            f"{label}: pageCount {document.get('pageCount')} "
            f"!= {len(pages)} pages"
        )
    expected_prefix = f"maps/{game}/{map_id}/lightmaps/".casefold()
    seen: set[str] = set()
    for index, page in enumerate(pages):
        page_label = f"{label} page[{index}]"
        if not isinstance(page, str) or not page:
            errors.append(f"{page_label}: is not a path string")
            continue
        folded = page.casefold()
        if folded in seen:
            errors.append(f"{label}: duplicate page '{page}'")
        seen.add(folded)
        if (
            not folded.startswith(expected_prefix)
            or not folded.endswith(".png")
        ):
            errors.append(
                f"{page_label}: outside the map's lightmaps PNG root: {page}"
            )
            continue
        page_path = content_path(content, page, page_label, errors)
        if page_path is not None and not page_path.is_file():
            errors.append(f"{page_label}: missing page file '{page}'")


def glb_animation_names(path: Path) -> list[str]:
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError("truncated GLB")
    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(data):
        raise ValueError("invalid GLB header")
    chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A or 20 + chunk_length > len(data):
        raise ValueError("GLB has no valid JSON chunk")
    payload = json.loads(
        data[20 : 20 + chunk_length].rstrip(b" \t\r\n\0").decode("utf-8")
    )
    animations = payload.get("animations", [])
    if not isinstance(animations, list):
        raise ValueError("GLB animations member is not an array")
    names = []
    for animation in animations:
        name = animation.get("name") if isinstance(animation, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("GLB animation has no name")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("GLB contains duplicate animation names")
    return names


def glb_triangle_count(path: Path) -> int:
    """Return indexed triangle count for the exact static GLB subset."""
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
                raise ValueError("GLB contains a non-triangle primitive")
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


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _exact_string_array(
    value: object,
    label: str,
    errors: list[str],
) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item for item in value)
        or len(value) != len(set(value))
    ):
        errors.append(f"{label} is not a unique nonempty string array")
        return []
    return value


def _mp_gsc_schema(
    value: object,
    keys: frozenset[str],
    label: str,
    errors: list[str],
) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{label} is not an object")
        return False
    actual = frozenset(value)
    if actual != keys:
        missing = ",".join(sorted(keys - actual))
        extra = ",".join(sorted(actual - keys))
        details = []
        if missing:
            details.append(f"missing={missing}")
        if extra:
            details.append(f"extra={extra}")
        errors.append(
            f"{label} schema is unsupported"
            + (f" ({'; '.join(details)})" if details else "")
        )
        return False
    return True


def _mp_gsc_path(
    value: object,
    label: str,
    errors: list[str],
) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        errors.append(f"{label} is not a nonempty canonical path")
        return None
    if "\\" in value or value.startswith("/"):
        errors.append(f"{label} is unsafe: '{value}'")
        return None
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != value
        or any(part in ("", ".", "..") for part in parsed.parts)
        or any(":" in part for part in parsed.parts)
        or any(ord(character) < 32 for character in value)
    ):
        errors.append(f"{label} is unsafe: '{value}'")
        return None
    return value


def _mp_gsc_strings(
    value: object,
    label: str,
    errors: list[str],
    *,
    canonical_order: bool = True,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} is not an array")
        return []
    result: list[str] = []
    seen: set[str] = set()
    valid = True
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or any(ord(character) < 32 for character in item)
        ):
            valid = False
            continue
        folded = item.casefold()
        if folded in seen:
            valid = False
            continue
        seen.add(folded)
        result.append(item)
    if not valid or len(result) != len(value):
        errors.append(
            f"{label} is not a case-insensitively unique nonempty "
            "string array"
        )
    if (
        canonical_order
        and result
        and result != sorted(result, key=str.casefold)
    ):
        errors.append(f"{label} is not in canonical order")
    return result


def _mp_gsc_efx_output(source: str) -> str:
    path = PurePosixPath(source)
    parts = (
        path.parts[1:]
        if path.parts and path.parts[0].casefold() == "fx"
        else path.parts
    )
    return PurePosixPath("scripts", "mp", "efx", *parts).as_posix()


def _mp_gsc_texture_output(source: str) -> str:
    return PurePosixPath(
        "scripts",
        "mp",
        "textures",
        *PurePosixPath(source).with_suffix(".png").parts,
    ).as_posix()


def _mp_gsc_provenance(
    source: str,
    source_sha256: object,
    value: object,
    label: str,
    errors: list[str],
    *,
    retail_index: object | None,
) -> None:
    keys = frozenset((
        "tier",
        "archive",
        "member",
        "bytes",
        "crc32",
        "sha256",
    ))
    if not _mp_gsc_schema(value, keys, f"{label} provenance", errors):
        return
    assert isinstance(value, dict)
    archive = value.get("archive")
    member = value.get("member")
    tier = value.get("tier")
    allowed: dict[str, str] = {
        f"Main/{name}".casefold(): "cod1"
        for name in COD1_ARCHIVES
    }
    allowed.update({
        f"uo/{name}".casefold(): "uo"
        for name in UO_ARCHIVES
    })
    archive_key = archive.casefold() if isinstance(archive, str) else ""
    expected_tier = allowed.get(archive_key)
    if expected_tier is None:
        errors.append(
            f"{label} provenance names a nonselected/nonofficial archive: "
            f"{archive or '?'}"
        )
    elif tier != expected_tier:
        errors.append(f"{label} provenance archive/tier mismatch")
    if (
        _mp_gsc_path(member, f"{label} provenance member", errors)
        is not None
        and isinstance(member, str)
        and member.casefold() != source.casefold()
    ):
        errors.append(f"{label} provenance member differs from source")
    if (
        not _valid_sha256(source_sha256)
        or value.get("sha256") != source_sha256
    ):
        errors.append(f"{label} source hash/provenance mismatch")
    byte_count = value.get("bytes")
    if (
        not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
    ):
        errors.append(f"{label} provenance byte count is invalid")
    crc32 = value.get("crc32")
    if (
        not isinstance(crc32, str)
        or len(crc32) != 8
        or any(character not in "0123456789abcdef" for character in crc32)
    ):
        errors.append(f"{label} provenance CRC-32 is invalid")

    if retail_index is None:
        return
    try:
        reference = retail_index.get(source)
    except (AttributeError, OSError, KeyError, ValueError) as error:
        errors.append(f"{label} retail lookup failed: {error}")
        return
    if reference is None:
        errors.append(f"{label} source is absent from selected retail data")
        return
    actual_archive = (
        f"{reference.archive.parent.name}/{reference.archive.name}"
    )
    if (
        not isinstance(member, str)
        or reference.name.casefold() != member.casefold()
        or actual_archive.casefold() != archive_key
        or reference.tier != tier
    ):
        errors.append(f"{label} does not name the active layered member")
    if (
        byte_count != reference.size
        or crc32 != f"{reference.crc32:08x}"
    ):
        errors.append(f"{label} retail size/CRC provenance mismatch")
    try:
        raw = retail_index.read_ref(reference)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as error:
        errors.append(f"{label} retail member could not be read: {error}")
        return
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        errors.append(f"{label} retail member hash mismatch")


def _mp_gsc_shader_mappings(
    value: object,
    label: str,
    errors: list[str],
) -> tuple[list[dict[str, object]], set[str], set[str]]:
    if not isinstance(value, list):
        errors.append(f"{label} is not an array")
        return [], set(), set()
    mappings: list[dict[str, object]] = []
    shaders: set[str] = set()
    textures: set[str] = set()
    for index, mapping in enumerate(value):
        entry_label = f"{label}[{index}]"
        if not _mp_gsc_schema(
            mapping,
            frozenset(("shader", "texturePngs")),
            entry_label,
            errors,
        ):
            continue
        assert isinstance(mapping, dict)
        shader = mapping.get("shader")
        if (
            not isinstance(shader, str)
            or not shader
            or shader != shader.strip()
            or any(ord(character) < 32 for character in shader)
        ):
            errors.append(f"{entry_label} shader is invalid")
            continue
        folded_shader = shader.casefold()
        if folded_shader in shaders:
            errors.append(f"{label} duplicates shader '{shader}'")
            continue
        shaders.add(folded_shader)
        texture_paths = _mp_gsc_strings(
            mapping.get("texturePngs"),
            f"{entry_label} texturePngs",
            errors,
            canonical_order=False,
        )
        if not texture_paths:
            errors.append(f"{entry_label} has no ordered texture frames")
        for texture in texture_paths:
            if (
                _mp_gsc_path(texture, f"{entry_label} texture", errors)
                is not None
                and not texture.casefold().startswith(
                    "scripts/mp/textures/"
                )
            ):
                errors.append(
                    f"{entry_label} texture is outside scripts/mp/textures"
                )
            textures.add(texture.casefold())
        mappings.append(
            {"shader": shader, "texturePngs": texture_paths}
        )
    return mappings, shaders, textures


def _mp_gsc_retail_alias_files(
    game_root: Path,
    *,
    retail_index: object,
) -> dict[str, list[dict[str, object]]]:
    """Rebuild ordered, non-null soundalias variants from retail."""
    rows: dict[str, list[dict[str, object]]] = {}

    def number(row: list[str], index: int, default: float) -> float:
        if index >= len(row) or not row[index]:
            return default
        try:
            return float(row[index])
        except ValueError:
            return default

    for archive_path in selected_archives(game_root):
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.namelist():
                folded = member.casefold()
                if (
                    not folded.startswith("soundaliases/")
                    or not folded.endswith(".csv")
                ):
                    continue
                text = archive.read(member).decode(
                    "utf-8-sig",
                    errors="replace",
                )
                for row in csv.reader(io.StringIO(text)):
                    if (
                        len(row) < 3
                        or not row[0]
                        or row[0].startswith("#")
                    ):
                        continue
                    sound_file = row[2].strip().replace("\\", "/")
                    reference = retail_index.get(
                        f"sound/{sound_file}"
                    )
                    if (
                        not sound_file
                        or sound_file.casefold() == "null.wav"
                        or reference is None
                    ):
                        continue
                    active_member = reference.name.replace("\\", "/")
                    if not active_member.casefold().startswith("sound/"):
                        continue
                    sound_file = (
                        active_member[len("sound/"):].casefold()
                    )
                    volume_min = number(row, 3, 1.0)
                    pitch_min = number(row, 5, 1.0)
                    entry = {
                        "file": sound_file,
                        "volume_min": volume_min,
                        "volume_max": number(row, 4, volume_min),
                        "pitch_min": pitch_min,
                        "pitch_max": number(row, 6, pitch_min),
                        "distance_min_inches": number(row, 7, 120.0),
                        "distance_max_inches": number(row, 8, 0.0),
                    }
                    alias = row[0].strip().casefold()
                    aliases = rows.setdefault(alias, [])
                    if entry not in aliases:
                        aliases.append(entry)
    return rows


def _mp_gsc_validate_presentation(
    content: Path,
    presentation: object,
    runtime_aliases: set[str],
    errors: list[str],
    *,
    game_root: Path | None,
    retail_index: object | None,
) -> None:
    """Bind every active MP alias variant to exact packaged retail bytes."""
    if not isinstance(presentation, dict):
        errors.append(
            "audio/presentation.json cannot prove MP GSC alias provenance"
        )
        return
    expected_selected_maps = list(
        selected_shipping_map_ids(include_uo=True)
    )
    selected_maps = _mp_gsc_strings(
        presentation.get("selectedMaps"),
        "audio/presentation.json selectedMaps",
        errors,
    )
    if selected_maps != expected_selected_maps:
        errors.append(
            "audio/presentation.json selectedMaps differs from the exact "
            "shipping map roster"
        )
    aliases = presentation.get("aliases")
    provenance = presentation.get("provenance")
    if not isinstance(aliases, list):
        errors.append(
            "audio/presentation.json aliases cannot prove MP GSC content"
        )
        aliases = []
    if not isinstance(provenance, list):
        errors.append(
            "audio/presentation.json provenance is not an array"
        )
        provenance = []

    aliases_by_name: dict[str, dict[str, object]] = {}
    for entry in aliases:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("alias"), str)
        ):
            continue
        aliases_by_name.setdefault(entry["alias"].casefold(), entry)

    provenance_by_path: dict[str, dict[str, object]] = {}
    for index, record in enumerate(provenance):
        label = f"presentation provenance[{index}]"
        if not _mp_gsc_schema(
            record,
            frozenset((
                "path",
                "archive",
                "member",
                "bytes",
                "sha256",
            )),
            label,
            errors,
        ):
            continue
        assert isinstance(record, dict)
        path = record.get("path")
        if (
            _mp_gsc_path(path, f"{label} path", errors) is None
            or not isinstance(path, str)
        ):
            continue
        folded = path.casefold()
        if folded in provenance_by_path:
            errors.append(
                f"audio/presentation.json duplicates provenance for {path}"
            )
            continue
        provenance_by_path[folded] = record

    expected_alias_files = None
    if game_root is not None and retail_index is not None:
        try:
            expected_alias_files = _mp_gsc_retail_alias_files(
                game_root,
                retail_index=retail_index,
            )
        except (
            OSError,
            ValueError,
            KeyError,
            zipfile.BadZipFile,
        ) as error:
            errors.append(
                f"retail soundalias closure could not be rebuilt: {error}"
            )

    allowed_archives = {
        f"Main/{name}".casefold()
        for name in COD1_ARCHIVES
    }
    allowed_archives.update(
        f"uo/{name}".casefold()
        for name in UO_ARCHIVES
    )
    silent = MP_GSC_SILENT_SOUND_ALIASES
    for alias in sorted(runtime_aliases - silent):
        entry = aliases_by_name.get(alias)
        if entry is None:
            # The roster join reports the missing alias separately.
            continue
        variants = entry.get("variants")
        if not isinstance(variants, list) or not variants:
            errors.append(
                f"audio alias '{alias}' cannot prove any retail variants"
            )
            continue
        actual_variants: list[dict[str, object]] = []
        for index, variant in enumerate(variants):
            label = f"audio alias '{alias}' variant[{index}]"
            if not isinstance(variant, dict):
                errors.append(f"{label} is malformed")
                continue
            sound_file = variant.get("file")
            if (
                _mp_gsc_path(sound_file, f"{label} file", errors) is None
                or not isinstance(sound_file, str)
            ):
                continue
            if sound_file != sound_file.casefold():
                errors.append(
                    f"{label} file is not canonical lowercase"
                )
            expected_variant_keys = frozenset((
                "file",
                "volume_min",
                "volume_max",
                "pitch_min",
                "pitch_max",
                "distance_min_inches",
                "distance_max_inches",
            ))
            if frozenset(variant) != expected_variant_keys:
                errors.append(
                    f"{label} schema differs from the retail soundalias "
                    "variant"
                )
            actual_variants.append(variant)
            relative = f"audio/{sound_file}"
            audio_path = content_path(
                content,
                relative,
                label,
                errors,
            )
            record = provenance_by_path.get(relative.casefold())
            if record is None:
                errors.append(
                    f"{label} has no exact per-file source provenance"
                )
                continue
            if record.get("path") != relative:
                errors.append(
                    f"{label} provenance path differs from its canonical "
                    "packaged path"
                )
            archive = record.get("archive")
            member = record.get("member")
            digest = record.get("sha256")
            byte_count = record.get("bytes")
            expected_member = f"sound/{sound_file}"
            if (
                not isinstance(archive, str)
                or archive.casefold() not in allowed_archives
            ):
                errors.append(
                    f"{label} names a nonselected/nonofficial source archive"
                )
            if (
                not isinstance(member, str)
                or member.casefold() != expected_member.casefold()
            ):
                errors.append(
                    f"{label} provenance member differs from its exact "
                    "sound file"
                )
            member_folded = (
                member.casefold() if isinstance(member, str) else ""
            )
            if (
                member_folded.startswith("sound/voiceovers/")
                and not member_folded.startswith(
                    "sound/voiceovers/mp/"
                )
            ):
                errors.append(
                    f"{label} admits a SinglePlayer voiceover member"
                )
            if (
                audio_path is None
                or not audio_path.is_file()
                or not _valid_sha256(digest)
                or (
                    audio_path.is_file()
                    and sha256_file(audio_path) != digest
                )
                or (
                    audio_path.is_file()
                    and (
                        not isinstance(byte_count, int)
                        or isinstance(byte_count, bool)
                        or audio_path.stat().st_size != byte_count
                    )
                )
            ):
                errors.append(
                    f"{label} packaged bytes/hash differ from provenance"
                )
            if retail_index is None:
                continue
            reference = retail_index.get(expected_member)
            if reference is None:
                errors.append(
                    f"{label} source is absent from selected retail data"
                )
                continue
            actual_archive = (
                f"{reference.archive.parent.name}/"
                f"{reference.archive.name}"
            )
            if (
                not isinstance(member, str)
                or reference.name.casefold() != member.casefold()
                or not isinstance(archive, str)
                or actual_archive.casefold() != archive.casefold()
                or reference.size != byte_count
            ):
                errors.append(
                    f"{label} does not name the active layered retail member"
                )
            try:
                raw = retail_index.read_ref(reference)
            except (
                OSError,
                KeyError,
                ValueError,
                zipfile.BadZipFile,
            ) as error:
                errors.append(f"{label} retail bytes could not be read: {error}")
            else:
                if hashlib.sha256(raw).hexdigest() != digest:
                    errors.append(f"{label} retail byte hash mismatch")
        if expected_alias_files is not None:
            expected_variants = expected_alias_files.get(alias, [])
            if actual_variants != expected_variants:
                errors.append(
                    f"audio alias '{alias}' variants differ from exact "
                    "selected retail soundalias rows"
                )


def validate_multiplayer_gsc_content(
    content: Path,
    errors: list[str],
    *,
    presentation_aliases: set[str] | None = None,
    presentation: dict[str, object] | None = None,
    prop_names: set[str] | None = None,
    game_root: Path | None = None,
) -> dict[str, set[str]]:
    """Validate the complete active ``maps/mp`` runtime asset closure.

    The detached manifest must be internally closed even without the retail
    install.  When ``game_root`` is available, its canonical GSC analysis and
    every resolved EFX/shader relationship are rebuilt from the active
    layered archives; a manifest cannot satisfy validation by swapping in a
    similarly named global or campaign asset.
    """
    result = {"soundAliases": set(), "modelNames": set()}
    payload = load_json(
        content / "scripts" / "mp" / "mp_content.json"
    )
    if payload is None:
        errors.append(
            "scripts/mp/mp_content.json missing or unparseable"
        )
        return result
    top_keys = frozenset((
        "format",
        "version",
        "tiers",
        "selectedMaps",
        "analysis",
        "effects",
        "shaderTextures",
        "soundAliases",
        "modelNames",
        "missingRetailEffects",
        "unresolvedShaders",
        "scriptFiles",
        "efxFiles",
        "textureFiles",
        "counts",
    ))
    if not _mp_gsc_schema(
        payload,
        top_keys,
        "MP GSC content",
        errors,
    ):
        if not isinstance(payload, dict):
            return result
    expected_tiers = ["cod1", "uo"]
    if (
        payload.get("format") != MP_GSC_CONTENT_FORMAT
        or payload.get("version") != MP_GSC_CONTENT_VERSION
        or payload.get("tiers") != expected_tiers
    ):
        errors.append("MP GSC content format/version/tier is unsupported")
    expected_selected_maps = list(
        selected_shipping_map_ids(include_uo=True)
    )
    selected_maps = _mp_gsc_strings(
        payload.get("selectedMaps"),
        "MP GSC selectedMaps",
        errors,
    )
    if selected_maps != expected_selected_maps:
        errors.append(
            "MP GSC selectedMaps differs from the exact shipping map roster"
        )
    expected_counts = MP_GSC_CONTENT_COUNTS
    if payload.get("counts") != expected_counts:
        errors.append(
            "MP GSC content counts differ from the exact selected retail "
            "multiplayer tier"
        )

    retail_index = None
    expected_analysis = None
    if game_root is not None:
        try:
            from cod1_mp_gsc_content import analyze_multiplayer_gsc

            retail_index = LayeredArchiveIndex(
                selected_archives(game_root)
            )
            expected_analysis = analyze_multiplayer_gsc(
                retail_index,
                selected_maps=expected_selected_maps,
            ).to_manifest()
        except Exception as error:
            errors.append(
                f"MP GSC retail closure could not be rebuilt: {error}"
            )
            retail_index = None

    analysis = payload.get("analysis")
    analysis_keys = frozenset((
        "format",
        "version",
        "scriptFiles",
        "soundAliases",
        "modelNames",
        "shaderNames",
        "effectPaths",
        "scripts",
        "unresolvedSinks",
        "excludedScriptCalls",
        "counts",
    ))
    if not _mp_gsc_schema(
        analysis,
        analysis_keys,
        "MP GSC analysis",
        errors,
    ):
        analysis = {}
    assert isinstance(analysis, dict)
    if (
        analysis.get("format") != MP_GSC_ANALYSIS_FORMAT
        or analysis.get("version") != MP_GSC_ANALYSIS_VERSION
    ):
        errors.append("MP GSC analysis format/version is unsupported")
    if expected_analysis is not None and analysis != expected_analysis:
        errors.append(
            "MP GSC analysis differs from active selected retail maps/mp "
            "scripts"
        )

    script_sources = _mp_gsc_strings(
        analysis.get("scriptFiles"),
        "MP GSC analysis scriptFiles",
        errors,
    )
    for source in script_sources:
        if (
            _mp_gsc_path(source, "MP GSC analysis script", errors)
            is not None
            and (
                not source.casefold().startswith("maps/mp/")
                or not source.casefold().endswith(".gsc")
            )
        ):
            errors.append(
                f"MP GSC analysis admits a non-multiplayer script: {source}"
            )
    analysis_aliases = _mp_gsc_strings(
        analysis.get("soundAliases"),
        "MP GSC analysis soundAliases",
        errors,
    )
    analysis_models = _mp_gsc_strings(
        analysis.get("modelNames"),
        "MP GSC analysis modelNames",
        errors,
    )
    if any("/" in model or "\\" in model for model in analysis_models):
        errors.append("MP GSC analysis contains a noncanonical model name")
    analysis_shaders = _mp_gsc_strings(
        analysis.get("shaderNames"),
        "MP GSC analysis shaderNames",
        errors,
    )
    analysis_effects = _mp_gsc_strings(
        analysis.get("effectPaths"),
        "MP GSC analysis effectPaths",
        errors,
    )
    for source in analysis_effects:
        if (
            _mp_gsc_path(source, "MP GSC analysis effect", errors)
            is not None
            and (
                not source.casefold().startswith("fx/")
                or not source.casefold().endswith(".efx")
            )
        ):
            errors.append(
                f"MP GSC analysis effect is outside exact fx/*.efx: "
                f"{source}"
            )

    excluded_calls = _mp_gsc_strings(
        analysis.get("excludedScriptCalls"),
        "MP GSC analysis excludedScriptCalls",
        errors,
    )
    admitted_scripts = {path.casefold() for path in script_sources}
    for source in excluded_calls:
        normalized = _mp_gsc_path(
            source,
            "MP GSC excluded script call",
            errors,
        )
        if normalized is None:
            continue
        if (
            not source.casefold().endswith(".gsc")
            or source.casefold().startswith("maps/mp/")
            or source.casefold() in admitted_scripts
        ):
            errors.append(
                f"MP GSC excluded script call was admitted: {source}"
            )

    unresolved_sinks = analysis.get("unresolvedSinks")
    if not isinstance(unresolved_sinks, list):
        errors.append("MP GSC analysis unresolvedSinks is not an array")
        unresolved_sinks = []
    for index, sink in enumerate(unresolved_sinks):
        label = f"MP GSC unresolved sink[{index}]"
        if not _mp_gsc_schema(
            sink,
            frozenset(("script", "function", "sink", "expression")),
            label,
            errors,
        ):
            continue
        assert isinstance(sink, dict)
        if (
            not all(
                isinstance(sink.get(key), str) and sink.get(key)
                for key in ("script", "function", "sink", "expression")
            )
            or str(sink.get("script", "")).casefold()
            not in admitted_scripts
        ):
            errors.append(f"{label} is malformed or names an inactive script")

    analysis_scripts = analysis.get("scripts")
    if not isinstance(analysis_scripts, list):
        errors.append("MP GSC analysis scripts is not an array")
        analysis_scripts = []
    per_script_sources: list[str] = []
    per_script_unions = {
        "soundAliases": set(),
        "modelNames": set(),
        "shaderNames": set(),
        "effectPaths": set(),
    }
    script_record_keys = frozenset((
        "source",
        "soundAliases",
        "modelNames",
        "shaderNames",
        "effectPaths",
    ))
    for index, entry in enumerate(analysis_scripts):
        label = f"MP GSC analysis scripts[{index}]"
        if not _mp_gsc_schema(
            entry,
            script_record_keys,
            label,
            errors,
        ):
            continue
        assert isinstance(entry, dict)
        source = entry.get("source")
        if isinstance(source, str):
            per_script_sources.append(source)
        else:
            errors.append(f"{label} source is malformed")
        for key in per_script_unions:
            values = _mp_gsc_strings(
                entry.get(key),
                f"{label} {key}",
                errors,
            )
            per_script_unions[key].update(
                value.casefold() for value in values
            )
    if per_script_sources != script_sources:
        errors.append(
            "MP GSC per-script analysis does not exactly cover scriptFiles"
        )
    outer_analysis_rosters = {
        "soundAliases": analysis_aliases,
        "modelNames": analysis_models,
        "shaderNames": analysis_shaders,
        "effectPaths": analysis_effects,
    }
    for key, values in outer_analysis_rosters.items():
        if per_script_unions[key] != {
            value.casefold() for value in values
        }:
            errors.append(
                f"MP GSC per-script {key} closure differs from its global "
                "roster"
            )
    computed_analysis_counts = {
        "scripts": len(script_sources),
        "soundAliases": len(analysis_aliases),
        "modelNames": len(analysis_models),
        "shaderNames": len(analysis_shaders),
        "effectPaths": len(analysis_effects),
        "unresolvedSinks": len(unresolved_sinks),
        "excludedScriptCalls": len(excluded_calls),
    }
    expected_analysis_counts = {
        key: expected_counts[key]
        for key in computed_analysis_counts
    }
    if (
        analysis.get("counts") != expected_analysis_counts
        or computed_analysis_counts != expected_analysis_counts
    ):
        errors.append(
            "MP GSC analysis counts differ from exact selected retail "
            "reachability"
        )

    runtime_aliases = _mp_gsc_strings(
        payload.get("soundAliases"),
        "MP GSC runtime soundAliases",
        errors,
    )
    runtime_models = _mp_gsc_strings(
        payload.get("modelNames"),
        "MP GSC runtime modelNames",
        errors,
    )
    if any("/" in model or "\\" in model for model in runtime_models):
        errors.append("MP GSC runtime contains a noncanonical model name")
    result["soundAliases"] = {
        alias.casefold() for alias in runtime_aliases
    }
    result["modelNames"] = {
        model.casefold() for model in runtime_models
    }

    missing_records = payload.get("missingRetailEffects")
    if not isinstance(missing_records, list):
        errors.append("MP GSC missingRetailEffects is not an array")
        missing_records = []
    missing_paths: list[str] = []
    for index, record in enumerate(missing_records):
        label = f"MP GSC missing retail effect[{index}]"
        if not _mp_gsc_schema(
            record,
            frozenset(("path", "reason")),
            label,
            errors,
        ):
            continue
        assert isinstance(record, dict)
        path = record.get("path")
        reason = record.get("reason")
        if isinstance(path, str):
            missing_paths.append(path)
        if reason != MP_GSC_MISSING_RETAIL_EFFECT_REASON:
            errors.append(f"{label} reason is not the exact retail gap")
    expected_missing = list(
        MP_GSC_MISSING_RETAIL_EFFECTS
    )
    if missing_paths != expected_missing:
        errors.append(
            "MP GSC missing retail effect roster differs from exact "
            "selected tier"
        )

    effect_records = payload.get("effects")
    if not isinstance(effect_records, list):
        errors.append("MP GSC effects is not an array")
        effect_records = []
    effect_keys = frozenset((
        "fxId",
        "sourceEfx",
        "efxFiles",
        "texturePngs",
        "shaderTextures",
        "aliasNames",
        "modelNames",
    ))
    effect_sources: list[str] = []
    effect_ids: set[str] = set()
    effect_efx_paths: set[str] = set()
    effect_texture_paths: set[str] = set()
    effect_aliases: set[str] = set()
    effect_models: set[str] = set()
    for index, effect in enumerate(effect_records):
        label = f"MP GSC effect[{index}]"
        if not _mp_gsc_schema(
            effect,
            effect_keys,
            label,
            errors,
        ):
            continue
        assert isinstance(effect, dict)
        source = effect.get("sourceEfx")
        fx_id = effect.get("fxId")
        if (
            not isinstance(source, str)
            or _mp_gsc_path(source, f"{label} sourceEfx", errors) is None
            or not source.casefold().startswith("fx/")
            or not source.casefold().endswith(".efx")
        ):
            errors.append(f"{label} sourceEfx is invalid")
            continue
        effect_sources.append(source)
        if (
            not isinstance(fx_id, str)
            or fx_id != source.casefold()
            or fx_id in effect_ids
        ):
            errors.append(
                f"{label} fxId is not the unique exact loadFx path"
            )
        else:
            effect_ids.add(fx_id)
        efx_paths = _mp_gsc_strings(
            effect.get("efxFiles"),
            f"{label} efxFiles",
            errors,
            canonical_order=False,
        )
        texture_paths = _mp_gsc_strings(
            effect.get("texturePngs"),
            f"{label} texturePngs",
            errors,
            canonical_order=False,
        )
        aliases = _mp_gsc_strings(
            effect.get("aliasNames"),
            f"{label} aliasNames",
            errors,
        )
        models = _mp_gsc_strings(
            effect.get("modelNames"),
            f"{label} modelNames",
            errors,
        )
        if not efx_paths or not texture_paths:
            errors.append(f"{label} has an empty EFX/texture closure")
        expected_root = _mp_gsc_efx_output(source)
        if expected_root.casefold() not in {
            path.casefold() for path in efx_paths
        }:
            errors.append(
                f"{label} does not contain its exact root EFX output"
            )
        for path in (*efx_paths, *texture_paths):
            normalized = _mp_gsc_path(path, f"{label} asset", errors)
            expected_prefix = (
                "scripts/mp/efx/"
                if path in efx_paths
                else "scripts/mp/textures/"
            )
            if (
                normalized is not None
                and not path.casefold().startswith(expected_prefix)
            ):
                errors.append(
                    f"{label} asset is outside {expected_prefix.rstrip('/')}"
                )
        _mappings, _shaders, mapped_textures = (
            _mp_gsc_shader_mappings(
                effect.get("shaderTextures"),
                f"{label} shaderTextures",
                errors,
            )
        )
        if mapped_textures != {
            path.casefold() for path in texture_paths
        }:
            errors.append(
                f"{label} shader mapping does not exactly cover "
                "texturePngs"
            )
        effect_efx_paths.update(path.casefold() for path in efx_paths)
        effect_texture_paths.update(
            path.casefold() for path in texture_paths
        )
        effect_aliases.update(alias.casefold() for alias in aliases)
        effect_models.update(model.casefold() for model in models)

    expected_effect_sources = [
        source
        for source in analysis_effects
        if source.casefold()
        not in {path.casefold() for path in expected_missing}
    ]
    if [
        source.casefold() for source in effect_sources
    ] != [
        source.casefold() for source in expected_effect_sources
    ]:
        errors.append(
            "MP GSC resolved effects do not exactly equal active loadFx "
            "reachability minus the declared retail gap"
        )
    if (
        {alias.casefold() for alias in analysis_aliases}
        | effect_aliases
    ) != result["soundAliases"]:
        errors.append(
            "MP GSC runtime sound aliases differ from script/EFX closure"
        )
    if (
        {model.casefold() for model in analysis_models}
        | effect_models
    ) != result["modelNames"]:
        errors.append(
            "MP GSC runtime models differ from script/EFX closure"
        )

    direct_mappings, resolved_shaders, direct_texture_paths = (
        _mp_gsc_shader_mappings(
            payload.get("shaderTextures"),
            "MP GSC shaderTextures",
            errors,
        )
    )
    unresolved_records = payload.get("unresolvedShaders")
    if not isinstance(unresolved_records, list):
        errors.append("MP GSC unresolvedShaders is not an array")
        unresolved_records = []
    unresolved_shaders: set[str] = set()
    for index, record in enumerate(unresolved_records):
        label = f"MP GSC unresolved shader[{index}]"
        if not _mp_gsc_schema(
            record,
            frozenset(("shader", "reason")),
            label,
            errors,
        ):
            continue
        assert isinstance(record, dict)
        shader = record.get("shader")
        reason = record.get("reason")
        if not isinstance(shader, str) or not shader:
            errors.append(f"{label} shader is malformed")
            continue
        folded = shader.casefold()
        if folded in unresolved_shaders:
            errors.append(f"MP GSC unresolvedShaders duplicates '{shader}'")
        unresolved_shaders.add(folded)
        expected_reason = (
            "engine builtin"
            if folded in MP_GSC_ENGINE_BUILTIN_SHADERS
            else MP_GSC_UNRESOLVED_SHADER_REASON
        )
        if reason != expected_reason:
            errors.append(
                f"{label} reason does not match the exact unresolved class"
            )
    analysis_shader_set = {
        shader.casefold() for shader in analysis_shaders
    }
    if (
        resolved_shaders & unresolved_shaders
        or resolved_shaders | unresolved_shaders
        != analysis_shader_set
    ):
        errors.append(
            "MP GSC resolved/unresolved shaders do not exactly partition "
            "active shader reachability"
        )

    file_records_by_kind = (
        (
            "script",
            payload.get("scriptFiles"),
            frozenset(("source", "sourceProvenance", "sha256")),
        ),
        (
            "EFX",
            payload.get("efxFiles"),
            frozenset((
                "source",
                "path",
                "sourceProvenance",
                "sha256",
            )),
        ),
        (
            "texture",
            payload.get("textureFiles"),
            frozenset((
                "source",
                "path",
                "sourceProvenance",
                "sourceSha256",
                "sha256",
            )),
        ),
    )
    parsed_file_records: dict[str, list[dict[str, object]]] = {}
    for kind, records, keys in file_records_by_kind:
        if not isinstance(records, list):
            errors.append(f"MP GSC {kind}Files is not an array")
            records = []
        parsed: list[dict[str, object]] = []
        seen_sources: set[str] = set()
        seen_paths: set[str] = set()
        ordered_sources: list[str] = []
        for index, record in enumerate(records):
            label = f"MP GSC {kind} file[{index}]"
            if not _mp_gsc_schema(record, keys, label, errors):
                continue
            assert isinstance(record, dict)
            source = record.get("source")
            if (
                not isinstance(source, str)
                or _mp_gsc_path(source, f"{label} source", errors) is None
            ):
                errors.append(f"{label} source is malformed")
                continue
            folded_source = source.casefold()
            if folded_source in seen_sources:
                errors.append(f"MP GSC {kind} files duplicate source {source}")
            seen_sources.add(folded_source)
            ordered_sources.append(source)
            if kind == "script":
                if (
                    not source.casefold().startswith("maps/mp/")
                    or not source.casefold().endswith(".gsc")
                ):
                    errors.append(
                        f"{label} is not an active multiplayer GSC source"
                    )
                source_sha = record.get("sha256")
            else:
                if kind == "EFX" and (
                    not source.casefold().startswith("fx/")
                    or not source.casefold().endswith(".efx")
                ):
                    errors.append(
                        f"{label} source is outside the exact fx/*.efx "
                        "namespace"
                    )
                if kind == "texture" and (
                    PurePosixPath(source).suffix.casefold()
                    not in (".dds", ".jpg", ".jpeg", ".png", ".tga")
                ):
                    errors.append(
                        f"{label} source is not a supported retail image"
                    )
                path = record.get("path")
                if (
                    not isinstance(path, str)
                    or _mp_gsc_path(path, f"{label} path", errors) is None
                ):
                    errors.append(f"{label} path is malformed")
                    path = ""
                folded_path = path.casefold()
                if folded_path in seen_paths:
                    errors.append(
                        f"MP GSC {kind} files duplicate output path {path}"
                    )
                seen_paths.add(folded_path)
                expected_path = (
                    _mp_gsc_efx_output(source)
                    if kind == "EFX"
                    else _mp_gsc_texture_output(source)
                )
                if path != expected_path:
                    errors.append(
                        f"{label} output is not the exact source-derived path"
                    )
                resolved = content_path(content, path, label, errors)
                digest = record.get("sha256")
                if (
                    resolved is None
                    or not resolved.is_file()
                    or not _valid_sha256(digest)
                    or (
                        resolved.is_file()
                        and sha256_file(resolved) != digest
                    )
                ):
                    errors.append(f"{label} output hash/path mismatch")
                source_sha = (
                    record.get("sha256")
                    if kind == "EFX"
                    else record.get("sourceSha256")
                )
            _mp_gsc_provenance(
                source,
                source_sha,
                record.get("sourceProvenance"),
                label,
                errors,
                retail_index=retail_index,
            )
            parsed.append(record)
        if ordered_sources != sorted(ordered_sources, key=str.casefold):
            errors.append(f"MP GSC {kind} files are not in canonical order")
        parsed_file_records[kind] = parsed

    script_file_sources = {
        str(record.get("source", "")).casefold()
        for record in parsed_file_records["script"]
    }
    if script_file_sources != admitted_scripts:
        errors.append(
            "MP GSC script provenance does not exactly cover active scripts"
        )
    efx_file_paths = {
        str(record.get("path", "")).casefold()
        for record in parsed_file_records["EFX"]
    }
    if efx_file_paths != effect_efx_paths:
        errors.append(
            "MP GSC recursive EFX files differ from effect reachability"
        )
    texture_file_paths = {
        str(record.get("path", "")).casefold()
        for record in parsed_file_records["texture"]
    }
    if (
        effect_texture_paths | direct_texture_paths
        != texture_file_paths
    ):
        errors.append(
            "MP GSC texture files differ from effect/direct shader "
            "reachability"
        )

    computed_runtime_counts = {
        **computed_analysis_counts,
        "resolvedEffects": len(effect_records),
        "recursiveEfxFiles": len(parsed_file_records["EFX"]),
        "resolvedShaderHandles": len(direct_mappings),
        "textureFiles": len(parsed_file_records["texture"]),
        "runtimeSoundAliases": len(runtime_aliases),
        "runtimeModelNames": len(runtime_models),
        "missingRetailEffects": len(missing_records),
        "unresolvedShaders": len(unresolved_records),
    }
    if computed_runtime_counts != expected_counts:
        errors.append(
            "MP GSC manifest members do not satisfy exact selected retail "
            "counts"
        )

    if presentation_aliases is not None:
        packaged_aliases = {
            alias.casefold() for alias in presentation_aliases
        }
        silent = MP_GSC_SILENT_SOUND_ALIASES
        missing_audio = (
            result["soundAliases"] - silent - packaged_aliases
        )
        guessed_silence = silent & packaged_aliases
        if missing_audio:
            errors.append(
                "audio/presentation.json omits active MP GSC aliases: "
                + ", ".join(sorted(missing_audio))
            )
        if guessed_silence:
            errors.append(
                "audio/presentation.json guesses unresolved stock MP "
                "aliases: "
                + ", ".join(sorted(guessed_silence))
            )
    if prop_names is not None:
        packaged_models = {
            model.casefold() for model in prop_names
        }
        absent_models = result["modelNames"] - packaged_models
        if absent_models:
            errors.append(
                "props.json omits active MP GSC/EFX models: "
                + ", ".join(sorted(absent_models))
            )

    if retail_index is not None:
        try:
            from cod1_script_exploder import (
                ScriptExploderClosureError,
                build_effect_closure,
            )
            from extract_cod1_mp_gsc_content import (
                _resolve_shader_images,
                efx_output_path,
                texture_output_path,
            )

            expected_effect_records: list[dict[str, object]] = []
            expected_efx_sources: set[str] = set()
            expected_texture_sources: set[str] = set()
            expected_missing_records: list[dict[str, str]] = []
            for effect_index, source in enumerate(analysis_effects):
                if retail_index.get(source) is None:
                    expected_missing_records.append({
                        "path": source,
                        "reason": MP_GSC_MISSING_RETAIL_EFFECT_REASON,
                    })
                    continue
                closure = build_effect_closure(
                    retail_index,
                    f"mp_gsc_{effect_index}",
                    source,
                )
                expected_efx_sources.update(
                    path.casefold() for path in closure.efx_files
                )
                expected_texture_sources.update(
                    path.casefold() for path in closure.image_files
                )
                expected_effect_records.append({
                    "fxId": source.casefold(),
                    "sourceEfx": source,
                    "efxFiles": [
                        efx_output_path(path)
                        for path in closure.efx_files
                    ],
                    "texturePngs": [
                        texture_output_path(path)
                        for path in closure.image_files
                    ],
                    "shaderTextures": [
                        {
                            "shader": shader,
                            "texturePngs": [
                                texture_output_path(path)
                                for path in images
                            ],
                        }
                        for shader, images in closure.shader_images
                    ],
                    "aliasNames": list(closure.sound_aliases),
                    "modelNames": [
                        model.casefold()
                        for model in closure.model_names
                    ],
                })
            expected_direct_mappings = []
            expected_unresolved = []
            for shader in analysis_shaders:
                try:
                    images = _resolve_shader_images(
                        retail_index,
                        shader,
                    )
                except ScriptExploderClosureError:
                    expected_unresolved.append({
                        "shader": shader,
                        "reason": (
                            "engine builtin"
                            if shader.casefold()
                            in MP_GSC_ENGINE_BUILTIN_SHADERS
                            else MP_GSC_UNRESOLVED_SHADER_REASON
                        ),
                    })
                    continue
                ordered_images: list[str] = []
                seen_images: set[str] = set()
                for image in images:
                    expected_texture_sources.add(image.casefold())
                    if image.casefold() not in seen_images:
                        seen_images.add(image.casefold())
                        ordered_images.append(image)
                expected_direct_mappings.append({
                    "shader": shader,
                    "texturePngs": [
                        texture_output_path(path)
                        for path in ordered_images
                    ],
                })
            if effect_records != expected_effect_records:
                errors.append(
                    "MP GSC effect closure differs from exact active retail "
                    "EFX graphs"
                )
            if missing_records != expected_missing_records:
                errors.append(
                    "MP GSC missing effect records differ from retail"
                )
            if direct_mappings != expected_direct_mappings:
                errors.append(
                    "MP GSC shader/image closure differs from exact active "
                    "retail declarations"
                )
            if unresolved_records != expected_unresolved:
                errors.append(
                    "MP GSC unresolved shader roster differs from retail"
                )
            actual_efx_sources = {
                str(record.get("source", "")).casefold()
                for record in parsed_file_records["EFX"]
            }
            actual_texture_sources = {
                str(record.get("source", "")).casefold()
                for record in parsed_file_records["texture"]
            }
            if actual_efx_sources != expected_efx_sources:
                errors.append(
                    "MP GSC packaged EFX sources differ from retail closure"
                )
            if actual_texture_sources != expected_texture_sources:
                errors.append(
                    "MP GSC packaged image sources differ from retail "
                    "closure"
                )
        except Exception as error:
            errors.append(
                f"MP GSC retail EFX/shader closure could not be rebuilt: "
                f"{error}"
            )
    if presentation is not None:
        _mp_gsc_validate_presentation(
            content,
            presentation,
            result["soundAliases"],
            errors,
            game_root=game_root,
            retail_index=retail_index,
        )
    return result


def validate_map_script_exploders(
    content: Path,
    map_id: str,
    payload: dict[str, object],
    errors: list[str],
) -> dict[str, object]:
    result = {
        "effects": [],
        "efxFiles": set(),
        "texturePngs": set(),
        "aliasNames": set(),
        "modelNames": set(),
    }
    if (
        payload.get("format")
        != "FriendsOfDuty.OriginalMultiplayerMapEntities"
        or payload.get("version") != 2
    ):
        errors.append(
            f"map {map_id}: entities format/version is unsupported"
        )

    effects = payload.get("exploderEffects")
    if not isinstance(effects, list):
        errors.append(f"map {map_id}: exploderEffects is not an array")
        effects = []
    seen_ids: set[str] = set()
    normalized_effects: list[dict[str, object]] = []
    for effect in effects:
        if not isinstance(effect, dict):
            errors.append(
                f"map {map_id}: malformed script_exploder effect"
            )
            continue
        fx_id = effect.get("fxId")
        source_efx = effect.get("sourceEfx")
        if (
            not isinstance(fx_id, str)
            or not fx_id
            or fx_id != fx_id.casefold()
            or fx_id in seen_ids
        ):
            errors.append(
                f"map {map_id}: invalid/duplicate exploder fxId "
                f"'{fx_id}'"
            )
            continue
        seen_ids.add(fx_id)
        if (
            not isinstance(source_efx, str)
            or not source_efx.casefold().startswith("fx/")
            or not source_efx.casefold().endswith(".efx")
            or ".." in Path(source_efx).parts
        ):
            errors.append(
                f"map {map_id}: fxId '{fx_id}' has invalid sourceEfx"
            )
        efx_files = _exact_string_array(
            effect.get("efxFiles"),
            f"map {map_id} fxId {fx_id} efxFiles",
            errors,
        )
        textures = _exact_string_array(
            effect.get("texturePngs"),
            f"map {map_id} fxId {fx_id} texturePngs",
            errors,
        )
        aliases = _exact_string_array(
            effect.get("aliasNames"),
            f"map {map_id} fxId {fx_id} aliasNames",
            errors,
        ) if effect.get("aliasNames") else []
        models = _exact_string_array(
            effect.get("modelNames"),
            f"map {map_id} fxId {fx_id} modelNames",
            errors,
        ) if effect.get("modelNames") else []
        for path in (*efx_files, *textures):
            resolved = content_path(
                content,
                path,
                f"map {map_id} fxId {fx_id}",
                errors,
            )
            if resolved is not None and not resolved.is_file():
                errors.append(
                    f"map {map_id}: fxId '{fx_id}' references missing "
                    f"content '{path}'"
                )
        shader_textures = effect.get("shaderTextures")
        if not isinstance(shader_textures, list) or not shader_textures:
            errors.append(
                f"map {map_id}: fxId '{fx_id}' has no shaderTextures"
            )
            shader_textures = []
        shader_names: set[str] = set()
        mapped_textures: set[str] = set()
        normalized_shader_textures = []
        for mapping in shader_textures:
            if not isinstance(mapping, dict):
                errors.append(
                    f"map {map_id}: fxId '{fx_id}' has malformed "
                    "shaderTextures entry"
                )
                continue
            shader = mapping.get("shader")
            if (
                not isinstance(shader, str)
                or not shader
                or shader.casefold() in shader_names
            ):
                errors.append(
                    f"map {map_id}: fxId '{fx_id}' has invalid/duplicate "
                    f"shader '{shader}'"
                )
                continue
            shader_names.add(shader.casefold())
            mapped = _exact_string_array(
                mapping.get("texturePngs"),
                f"map {map_id} fxId {fx_id} shader {shader}",
                errors,
            )
            mapped_textures.update(mapped)
            normalized_shader_textures.append(
                {"shader": shader, "texturePngs": mapped}
            )
        if mapped_textures != set(textures):
            errors.append(
                f"map {map_id}: fxId '{fx_id}' shader texture mapping "
                "does not exactly cover texturePngs"
            )
        result["efxFiles"].update(efx_files)
        result["texturePngs"].update(textures)
        result["aliasNames"].update(aliases)
        result["modelNames"].update(models)
        normalized_effects.append(
            {
                "fxId": fx_id,
                "sourceEfx": source_efx,
                "efxFiles": efx_files,
                "texturePngs": textures,
                "shaderTextures": normalized_shader_textures,
                "aliasNames": aliases,
                "modelNames": models,
            }
        )
    result["effects"] = normalized_effects

    raw_entities = payload.get("entities")
    gameplay = payload.get("gameplayEntities")
    if not isinstance(raw_entities, list) or not isinstance(gameplay, list):
        errors.append(
            f"map {map_id}: raw/gameplay entity arrays are malformed"
        )
        return result
    gameplay_by_source = {
        entry.get("sourceEntityIndex"): entry
        for entry in gameplay
        if isinstance(entry, dict)
        and isinstance(entry.get("sourceEntityIndex"), int)
    }
    raw_exploder_indices = []
    for source_index, raw in enumerate(raw_entities):
        if not isinstance(raw, dict) or not raw.get("script_exploder"):
            continue
        descriptor = gameplay_by_source.get(source_index)
        if not isinstance(descriptor, dict):
            errors.append(
                f"map {map_id}: gameplay descriptor missing for source "
                f"entity {source_index}"
            )
            continue
        raw_exploder_indices.append(source_index)
        try:
            expected_id = int(str(raw["script_exploder"]))
        except ValueError:
            expected_id = -1
        if (
            expected_id <= 0
            or descriptor.get("scriptExploderId") != expected_id
            or descriptor.get("scriptFxId", "")
            != str(raw.get("script_fxid", "")).casefold()
        ):
            errors.append(
                f"map {map_id}: exploder entity {source_index} lost its "
                "exact group/fx id"
            )
        if (
            descriptor.get("hasScriptDelayMin")
            is not ("script_delay_min" in raw)
            or descriptor.get("hasScriptDelayMax")
            is not ("script_delay_max" in raw)
        ):
            errors.append(
                f"map {map_id}: exploder entity {source_index} lost "
                "script_delay_min/max key presence"
            )
        brush_glb = descriptor.get("exploderBrushGlb", "")
        brush_materials = descriptor.get("exploderBrushMaterials", [])
        if brush_glb:
            resolved = content_path(
                content,
                brush_glb,
                f"map {map_id} exploder brush {source_index}",
                errors,
            )
            if resolved is not None and not resolved.is_file():
                errors.append(
                    f"map {map_id}: exploder brush {source_index} GLB "
                    f"is missing '{brush_glb}'"
                )
            if not isinstance(brush_materials, list) or not brush_materials:
                errors.append(
                    f"map {map_id}: exploder brush {source_index} has a "
                    "GLB but no material bindings"
                )
        elif brush_materials:
            errors.append(
                f"map {map_id}: exploder brush {source_index} has material "
                "bindings but no GLB"
            )

    # Ordinary marker entities are part of the stock exploder command graph.
    # They are not recognizable by classname, so prove that the complete
    # targetname/target closure rooted at an exploder is retained verbatim.
    raw_by_target_name: dict[str, list[int]] = {}
    target_frontier: list[str] = []
    for source_index, raw in enumerate(raw_entities):
        if not isinstance(raw, dict):
            continue
        target_name = str(raw.get("targetname", "")).strip().casefold()
        if target_name:
            raw_by_target_name.setdefault(target_name, []).append(
                source_index
            )
        if raw.get("script_exploder"):
            target = str(raw.get("target", "")).strip().casefold()
            if target:
                target_frontier.append(target)
    visited_targets: set[str] = set()
    target_sources: set[int] = set()
    while target_frontier:
        target_name = target_frontier.pop()
        if target_name in visited_targets:
            continue
        visited_targets.add(target_name)
        for source_index in raw_by_target_name.get(target_name, []):
            target_sources.add(source_index)
            raw = raw_entities[source_index]
            assert isinstance(raw, dict)
            next_target = str(raw.get("target", "")).strip().casefold()
            if next_target and next_target not in visited_targets:
                target_frontier.append(next_target)
    for source_index in sorted(target_sources):
        raw = raw_entities[source_index]
        descriptor = gameplay_by_source.get(source_index)
        if (
            not isinstance(raw, dict)
            or not isinstance(descriptor, dict)
            or descriptor.get("targetName", "")
            != raw.get("targetname", "")
            or descriptor.get("target", "") != raw.get("target", "")
        ):
            errors.append(
                f"map {map_id}: exploder target entity {source_index} "
                "is missing from the exact target graph"
            )

    model_assets = payload.get("modelAssets")
    if isinstance(model_assets, list):
        available_models = {
            str(model).casefold()
            for model in model_assets
            if isinstance(model, str)
        }
        missing_effect_models = (
            set(result["modelNames"]) - available_models
        )
        if missing_effect_models:
            errors.append(
                f"map {map_id}: exploder effect models are absent from "
                "modelAssets: "
                f"{', '.join(sorted(missing_effect_models))}"
            )
    counts = payload.get("counts")
    if not isinstance(counts, dict) or (
        counts.get("scriptExploderEntities")
        != len(raw_exploder_indices)
        or counts.get("exploderEffects") != len(effects)
        or counts.get("exploderBrushes")
        != sum(
            bool(
                isinstance(raw, dict)
                and raw.get("script_exploder")
                and str(raw.get("classname", "")).casefold()
                == "script_brushmodel"
            )
            for raw in raw_entities
        )
    ):
        errors.append(
            f"map {map_id}: script_exploder counts do not match entities"
        )
    return result


def _pak_file_index(
    content: Path,
) -> tuple[set[str], dict[str, str], list[str]]:
    """Index every packaged file by exact and casefolded relative path.

    The exact set is what makes this check work at all on the machines that
    cause the bug. Asking the filesystem whether a reference resolves is
    useless on Windows and macOS — they answer yes whatever its case, which is
    precisely why a broken package gets built and shipped there. Comparing
    against the spellings rglob actually returned tests case on every host.

    Returns the exact-path set, the casefolded->exact map, and the casefolded
    paths that more than one real file collapses onto.
    """
    exact_paths: set[str] = set()
    exact_by_folded: dict[str, str] = {}
    collisions: list[str] = []
    for path in content.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(content).as_posix()
        exact_paths.add(relative)
        folded = relative.casefold()
        if folded in exact_by_folded and exact_by_folded[folded] != relative:
            collisions.append(folded)
            continue
        exact_by_folded[folded] = relative
    return exact_paths, exact_by_folded, collisions


PAK_ASSET_SUFFIXES = (
    ".wav", ".mp3", ".png", ".glb", ".json", ".txt", ".efx", ".csv",
)
# Deliberately strict: manifests also carry base64 payloads and free text,
# and a blob full of '/' would otherwise be probed as a path — which on a
# long enough string raises ENAMETOOLONG rather than returning False.
PAK_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9_./\\-]{1,200}$")


def _looks_like_pak_reference(value: str) -> bool:
    if not value.lower().endswith(PAK_ASSET_SUFFIXES):
        return False
    if not PAK_REFERENCE_PATTERN.match(value):
        return False
    return value.count("/") + value.count("\\") <= 12


def _iter_reference_strings(payload: object) -> Iterator[str]:
    """Every string in a decoded manifest that could name a packaged file."""
    if isinstance(payload, str):
        if _looks_like_pak_reference(payload):
            yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _iter_reference_strings(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_reference_strings(value)


def _glb_uri_references(path: Path) -> Iterator[str]:
    """URIs a .glb points at, read out of its JSON chunk.

    glTF stores texture paths as plain strings inside the binary, so a model
    whose PNG was written with different casing than the URI recorded for it
    breaks on a case-sensitive filesystem exactly like a manifest entry does.
    """
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or header[:4] != b"glTF":
                return
            chunk = handle.read(8)
            if len(chunk) < 8:
                return
            length = int.from_bytes(chunk[:4], "little")
            if chunk[4:8] != b"JSON" or length <= 0 or length > (1 << 26):
                return
            document = json.loads(
                handle.read(length).decode("utf-8", errors="replace")
            )
    except (OSError, ValueError):
        return
    for image in document.get("images", []) or []:
        if isinstance(image, dict) and isinstance(image.get("uri"), str):
            yield image["uri"]
    for buffer in document.get("buffers", []) or []:
        if isinstance(buffer, dict) and isinstance(buffer.get("uri"), str):
            yield buffer["uri"]


def validate_path_case_consistency(
    content: Path,
    errors: list[str],
) -> None:
    """Reject a package whose own references disagree with disk about case.

    Windows and macOS resolve a path whatever its case, so an export produced
    there happily references audio/Explosions/x.wav while having written
    audio/explosions/x.wav. Linux and SteamOS do not: the file is simply
    missing, the catalog build fails, and the package is unusable on the one
    platform the exporter never ran on. Nothing else in this validator catches
    it, because every other check runs on the machine that built the package.

    A reference is only reported when its exact spelling is absent AND some
    other casing of it exists. That pairing is always this bug and never a
    false positive from a string that merely looks like a path.
    """
    exact_paths, exact_by_folded, collisions = _pak_file_index(content)
    for folded in sorted(set(collisions))[:20]:
        errors.append(
            "path case collision: two packaged files differ only by case "
            f"('{folded}'), which cannot survive a case-insensitive install"
        )

    referrers: dict[str, str] = {}
    for manifest in sorted(content.rglob("*.json")):
        payload = load_json(manifest)
        if payload is None:
            continue
        origin = manifest.relative_to(content).as_posix()
        for reference in _iter_reference_strings(payload):
            referrers.setdefault(reference, origin)
    for model in sorted(content.rglob("*.glb")):
        origin = model.relative_to(content).as_posix()
        parent = model.parent.relative_to(content).as_posix()
        for uri in _glb_uri_references(model):
            referrers.setdefault(f"{parent}/{uri}".lstrip("/"), origin)

    mismatches: list[str] = []
    for reference, origin in referrers.items():
        candidate = reference.replace("\\", "/").lstrip("/")
        if not candidate or ".." in candidate.split("/"):
            continue
        # Manifests address files both from the package root and relative to
        # their own directory. Every spelling has to be ruled out before a
        # reference counts as broken, or a correct relative path gets reported
        # as a mismatch against the identical absolute one.
        forms = [candidate]
        origin_dir = PurePosixPath(origin).parent.as_posix()
        if origin_dir and origin_dir != ".":
            forms.append(f"{origin_dir}/{candidate}")
        if any(form in exact_paths for form in forms):
            continue
        actual = next(
            (
                exact_by_folded[form.casefold()]
                for form in forms
                if form.casefold() in exact_by_folded
            ),
            None,
        )
        if actual is None:
            continue
        mismatches.append(f"{origin} -> '{reference}' (on disk: '{actual}')")

    for message in sorted(mismatches)[:20]:
        errors.append(f"path case mismatch: {message}")
    if len(mismatches) > 20:
        errors.append(
            f"path case mismatch: {len(mismatches) - 20} further reference(s) "
            "differ from disk only by case"
        )


def validate_source_policy(
    content: Path,
    errors: list[str],
    *,
    game_root: Path | None,
) -> None:
    payload = load_json(content / "provenance" / "source_archives.json")
    if payload is None:
        errors.append("provenance/source_archives.json missing or unparseable")
        return
    errors.extend(
        f"source policy: {message}"
        for message in validate_policy_manifest(payload)
    )
    if game_root is None:
        return
    expected = source_policy_manifest(game_root)
    expected_records = {
        record["path"]: record
        for record in expected["archives"]
    }
    actual_records = {
        record.get("path"): record
        for record in payload.get("archives", [])
        if isinstance(record, dict)
    }
    if set(actual_records) != set(expected_records):
        errors.append("source policy archive roster differs from selected install")
        return
    for path_key, expected_record in expected_records.items():
        actual_record = actual_records[path_key]
        if (
            actual_record.get("sha256") != expected_record.get("sha256")
            or actual_record.get("bytes") != expected_record.get("bytes")
        ):
            errors.append(
                f"source policy archive hash/size mismatch: {path_key}"
            )


def validate_staging_provenance(
    content: Path,
    source_root: Path | None,
    errors: list[str],
) -> None:
    if source_root is None:
        return
    manifest = load_json(source_root / "extraction_manifest.json")
    if manifest is None:
        errors.append("source staging extraction_manifest.json missing/unparseable")
        return
    if (
        manifest.get("format") != "FriendsOfDuty.OfficialMultiplayerSource"
        or manifest.get("version") != 2
    ):
        errors.append("source staging was not produced by the official MP extractor")
        return
    expected_tiers = ["cod1", "uo"]
    if manifest.get("tiers") != expected_tiers:
        errors.append("source staging tiers do not match package selection")
    closure = manifest.get("closure")
    expected_weapon_files = {
        *BASE_WEAPON_FILES,
        *ALTERNATE_WEAPON_FILES,
        *UO_WEAPON_FILES,
    }
    if (
        not isinstance(closure, dict)
        or closure.get("format")
        != "FriendsOfDuty.MultiplayerReachabilityClosure"
        or closure.get("version") != 1
        or closure.get("kind") != "weapon-player"
    ):
        errors.append(
            "source staging has no supported multiplayer reachability closure"
        )
        closure = {}
    if closure.get("requestedMaps") != list(
        selected_shipping_map_ids(include_uo=True)
    ):
        errors.append(
            "source staging requestedMaps differs from the exact shipping "
            "map roster"
        )
    closure_weapon_files = closure.get("weaponFiles")
    if (
        not isinstance(closure_weapon_files, list)
        or not all(
            isinstance(item, str)
            for item in closure_weapon_files
        )
        or set(closure_weapon_files) != expected_weapon_files
    ):
        errors.append(
            "source staging weapon closure differs from selected MP tier"
        )
    if (
        closure.get("playerAnimations") != PLAYER_ANIMATION_COUNT
    ):
        errors.append(
            "source staging player-animation closure differs from the "
            "required Call of Duty + United Offensive MP content"
        )
    expected_turret_aliases = SCRIPT_TURRET_ALIASES
    if closure.get("expandedTurretAliases") != list(
        expected_turret_aliases
    ):
        errors.append(
            "source staging turret-alias expansion differs from selected MP tier"
        )
    if closure.get("excludedTurretAnimations") != list(
        EXCLUDED_TURRET_ALIASES
    ):
        errors.append(
            "source staging turret-alias exclusions differ from the vehicle "
            "families this product cut"
        )
    if closure.get("models") != SOURCE_CLOSURE_MODEL_COUNT:
        errors.append(
            "source staging XModel closure differs from selected MP tier"
        )
    policy_errors = validate_policy_manifest(
        {
            "format": POLICY_FORMAT,
            "version": manifest.get("sourcePolicyVersion"),
            "tiers": manifest.get("tiers"),
            "archives": manifest.get("archives"),
        },
    )
    errors.extend(f"source staging policy: {message}" for message in policy_errors)
    archive_fingerprint = manifest.get("archiveFingerprint")
    if not _valid_sha256(archive_fingerprint):
        errors.append("source staging archive fingerprint is invalid")
    packaged_policy = load_json(
        content / "provenance" / "source_archives.json"
    )
    if (
        packaged_policy is not None
        and packaged_policy.get("fingerprint") != archive_fingerprint
    ):
        errors.append(
            "source staging archive fingerprint differs from packaged provenance"
        )
    allowed_archives = {
        f"Main/{name}".casefold() for name in COD1_ARCHIVES
    }
    allowed_archives |= {
        f"uo/{name}".casefold() for name in UO_ARCHIVES
    }
    provenance = manifest.get("provenance", {})
    if not isinstance(provenance, dict) or not provenance:
        errors.append("source staging contains no per-member provenance")
        return
    if manifest.get("fileCount") != len(provenance):
        errors.append("source staging fileCount differs from provenance records")
    if manifest.get("fileCount") != SOURCE_CLOSURE_FILE_COUNT:
        errors.append(
            "source staging file count differs from the complete selected MP closure"
        )
    actual_managed_paths = {
        path.relative_to(source_root).as_posix().casefold()
        for path in source_root.rglob("*")
        if path.is_file()
        and is_managed_source_path(
            path.relative_to(source_root).as_posix()
        )
    }
    declared_paths = {
        relative.casefold()
        for relative in provenance
        if isinstance(relative, str)
    }
    if actual_managed_paths != declared_paths:
        errors.append(
            "source staging contains untracked or missing managed MP members"
        )
    for relative, record in provenance.items():
        if not isinstance(relative, str) or not isinstance(record, dict):
            errors.append("source staging contains malformed provenance")
            continue
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"unsafe staged source path: {relative}")
            continue
        folded = relative.casefold()
        if folded.startswith("weapons/") and not folded.startswith("weapons/mp/"):
            errors.append(f"non-MP WEAPONFILE staged: {relative}")
        archive = str(record.get("archive", ""))
        if archive.casefold() not in allowed_archives:
            errors.append(
                f"nonofficial archive in source provenance: {archive or '?'}"
            )
        expected_tier = (
            "uo" if archive.casefold().startswith("uo/") else "cod1"
        )
        if record.get("tier") != expected_tier:
            errors.append(f"staged member tier mismatch: {relative}")
        member = str(record.get("member", "")).replace("\\", "/")
        if member.casefold() != folded:
            errors.append(f"staged member path mismatch: {relative}")
        digest = record.get("sha256")
        if not _valid_sha256(digest):
            errors.append(f"invalid staged member SHA-256: {relative}")
        crc32 = record.get("crc32")
        if (
            not isinstance(crc32, str)
            or len(crc32) != 8
            or any(character not in "0123456789abcdef" for character in crc32)
        ):
            errors.append(f"invalid staged member CRC-32: {relative}")
        staged_path = source_root / relative_path
        if not staged_path.is_file():
            errors.append(f"staged source file missing: {relative}")
            continue
        if record.get("bytes") != staged_path.stat().st_size:
            errors.append(f"staged member size mismatch: {relative}")
        if _valid_sha256(digest) and sha256_file(staged_path) != digest:
            errors.append(f"staged member hash mismatch: {relative}")


def validate(
    content: Path,
    errors: list[str],
    warnings: list[str],
    *,
    source_root: Path | None = None,
    game_root: Path | None = None,
) -> dict[str, int]:
    counts = {"weapons": 0, "players": 0, "maps": 0, "props": 0}
    validate_path_case_consistency(content, errors)
    validate_source_policy(
        content,
        errors,
        game_root=game_root,
    )
    validate_staging_provenance(
        content,
        source_root,
        errors,
    )

    weapons_data = load_json(content / "weapons" / "weapons.json")
    if weapons_data is None:
        errors.append("weapons/weapons.json missing or unparseable")
    else:
        if (
            weapons_data.get("format") != "FriendsOfDuty.Weapons"
            or weapons_data.get("version") != 3
        ):
            errors.append("weapons.json format/version is unsupported")
        weapons = weapons_data.get("weapons", [])
        if not isinstance(weapons, list):
            weapons = []
            errors.append("weapons.json weapons member is not an array")
        counts["weapons"] = len(weapons)
        expected_weapon_ids = set(REQUIRED_WEAPON_IDS)
        weapon_ids = {
            weapon.get("id")
            for weapon in weapons
            if isinstance(weapon, dict)
        }
        if weapon_ids != expected_weapon_ids:
            missing = sorted(expected_weapon_ids - weapon_ids)
            extra = sorted(weapon_ids - expected_weapon_ids)
            errors.append(
                "weapons.json roster differs from selected MP tier"
                + (f"; missing={','.join(missing)}" if missing else "")
                + (f"; extra={','.join(extra)}" if extra else "")
            )
        for weapon in weapons:
            if not isinstance(weapon, dict):
                errors.append("weapons.json contains a malformed weapon entry")
                continue
            weapon_id = weapon.get("id", "?")
            viewmodel = content_path(
                content,
                weapon.get("viewmodelGlb"),
                f"weapon {weapon_id} viewmodelGlb",
                errors,
            )
            world = content_path(
                content,
                weapon.get("worldGlb"),
                f"weapon {weapon_id} worldGlb",
                errors,
            )
            for key, path in (("viewmodelGlb", viewmodel), ("worldGlb", world)):
                if path is not None and not path.is_file():
                    errors.append(
                        f"weapon {weapon_id}: missing {key} "
                        f"'{weapon.get(key, '')}'"
                    )

            declared_animations = weapon.get("animations", [])
            if (
                not isinstance(declared_animations, list)
                or not all(
                    isinstance(name, str) and name
                    for name in declared_animations
                )
                or len(declared_animations) != len(set(declared_animations))
            ):
                errors.append(
                    f"weapon {weapon_id}: animations must be unique names"
                )
                declared_animations = []
            if viewmodel is not None and viewmodel.is_file():
                try:
                    glb_animations = glb_animation_names(viewmodel)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    errors.append(
                        f"weapon {weapon_id}: invalid viewmodel GLB: {error}"
                    )
                else:
                    if set(glb_animations) != set(declared_animations):
                        errors.append(
                            f"weapon {weapon_id}: manifest/GLB animation "
                            "name sets differ"
                        )

            roles = weapon.get("animationRoles", [])
            if not isinstance(roles, list) or not roles:
                errors.append(f"weapon {weapon_id}: animationRoles missing")
                roles = []
            role_names: set[str] = set()
            role_clips: set[str] = set()
            for role in roles:
                if not isinstance(role, dict):
                    errors.append(
                        f"weapon {weapon_id}: malformed animation role"
                    )
                    continue
                required_fields = (
                    "role", "field", "sourceAnimation",
                    "weaponFile", "variant", "clip",
                )
                if any(
                    not isinstance(role.get(field), str)
                    or not role.get(field)
                    for field in required_fields
                ):
                    errors.append(
                        f"weapon {weapon_id}: incomplete animation role"
                    )
                    continue
                weapon_file = role["weaponFile"]
                if (
                    "/" in weapon_file
                    or "\\" in weapon_file
                    or not weapon_file.casefold().endswith("_mp")
                ):
                    errors.append(
                        f"weapon {weapon_id}: non-MP role source "
                        f"'{weapon_file}'"
                    )
                role_names.add(role["role"])
                role_clips.add(role["clip"])
                if role["clip"] not in declared_animations:
                    errors.append(
                        f"weapon {weapon_id}: role {role['role']} names "
                        f"unknown clip '{role['clip']}'"
                    )
            missing_roles = REQUIRED_WEAPON_ROLES - role_names
            if weapon_id in NON_FIRING_WEAPON_IDS:
                missing_roles -= {"fire"}
            if missing_roles:
                errors.append(
                    f"weapon {weapon_id}: missing animation role(s): "
                    + ", ".join(sorted(missing_roles))
                )
            if set(declared_animations) != role_clips:
                errors.append(
                    f"weapon {weapon_id}: animationRoles do not cover every "
                    "declared GLB clip"
                )

            definition = weapon.get("definition", {})
            if not isinstance(definition, dict):
                definition = {}
                errors.append(f"weapon {weapon_id}: definition missing")
            source_weapon_file = definition.get("weapon_file", "")
            if (
                not isinstance(source_weapon_file, str)
                or "/" in source_weapon_file
                or "\\" in source_weapon_file
                or not source_weapon_file.casefold().endswith("_mp")
            ):
                errors.append(
                    f"weapon {weapon_id}: non-MP WEAPONFILE source "
                    f"'{source_weapon_file}'"
                )
            expected_drop_ammo = MULTIPLAYER_DROP_AMMO_BOUNDS.get(
                weapon_id
            )
            actual_drop_ammo = (
                definition.get("drop_ammo_min"),
                definition.get("drop_ammo_max"),
            )
            if (
                expected_drop_ammo is None
                or actual_drop_ammo != expected_drop_ammo
            ):
                errors.append(
                    f"weapon {weapon_id}: drop ammo bounds "
                    f"{actual_drop_ammo!r} differ from the stock MP "
                    f"WEAPONFILE {expected_drop_ammo!r}"
                )
            expected_max_ammo = MULTIPLAYER_MAX_AMMO.get(weapon_id)
            if definition.get("max_ammo") != expected_max_ammo:
                errors.append(
                    f"weapon {weapon_id}: max_ammo "
                    f"{definition.get('max_ammo')!r} differs from the stock "
                    f"MP WEAPONFILE {expected_max_ammo!r}"
                )
            for key, positive in (
                ("meleeDamage", True),
                ("meleeDelay", False),
                ("meleeTime", True),
            ):
                value = definition.get(key)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value < 0
                    or (positive and value <= 0)
                ):
                    errors.append(
                        f"weapon {weapon_id}: invalid/missing {key}"
                    )
            alternate = weapon.get("alternateWeaponFile", "")
            if alternate:
                if (
                    not isinstance(alternate, str)
                    or "/" in alternate
                    or "\\" in alternate
                    or not alternate.casefold().endswith("_mp")
                ):
                    errors.append(
                        f"weapon {weapon_id}: invalid alternateWeaponFile"
                    )
                if not any(
                    isinstance(role, dict)
                    and role.get("variant") == "alternate"
                    and role.get("weaponFile") == alternate
                    for role in roles
                ):
                    errors.append(
                        f"weapon {weapon_id}: alternate roles are missing"
                    )
            if definition.get("projectile_model", ""):
                projectile = content_path(
                    content,
                    weapon.get("projectileGlb"),
                    f"weapon {weapon_id} projectileGlb",
                    errors,
                )
                if projectile is not None and not projectile.is_file():
                    errors.append(
                        f"weapon {weapon_id}: missing projectileGlb "
                        f"'{weapon.get('projectileGlb', '')}'"
                    )
        for weapon_id in ORDNANCE_WEAPONS:
            if weapon_id not in weapon_ids:
                errors.append(
                    f"ordnance weapon '{weapon_id}' missing from weapons.json"
                )
        if "smokegrenade" not in weapon_ids:
            errors.append(
                "smokegrenade is missing; United Offensive is required"
            )
        data_dir = content / "weapons" / "data"
        data_provenance = load_json(data_dir / "provenance.json")
        if data_provenance is None:
            errors.append("weapons/data/provenance.json missing/unparseable")
        else:
            records = data_provenance.get("files", [])
            if not isinstance(records, list) or not records:
                errors.append("weapons/data provenance has no file records")
                records = []
            for record in records:
                if not isinstance(record, dict):
                    errors.append("weapons/data provenance record is malformed")
                    continue
                weapon_file = str(record.get("weaponFile", ""))
                if (
                    "/" in weapon_file
                    or "\\" in weapon_file
                    or not weapon_file.casefold().endswith("_mp")
                ):
                    errors.append(
                        f"weapons/data provenance names non-MP source "
                        f"'{weapon_file}'"
                    )
                path = content_path(
                    content,
                    record.get("path"),
                    "weapons/data provenance",
                    errors,
                )
                if path is None or not path.is_file():
                    errors.append(
                        f"weapons/data provenance file missing: "
                        f"{record.get('path', '')}"
                    )
                elif sha256_file(path) != record.get("sha256"):
                    errors.append(
                        f"weapons/data hash mismatch: {record.get('path', '')}"
                    )
        shell = content / "weapons" / "shell" / "shellcasing.glb"
        if not shell.is_file():
            errors.append("weapons/shell/shellcasing.glb missing")

    players_data = load_json(content / "players" / "players.json")
    if players_data is None:
        errors.append("players/players.json missing or unparseable")
    else:
        if (
            players_data.get("format") != "FriendsOfDuty.Players"
            or players_data.get("version") != 3
        ):
            errors.append("players.json format/version is unsupported")
        players = players_data.get("players", [])
        if not isinstance(players, list):
            players = []
            errors.append("players.json players member is not an array")
        counts["players"] = len(players)
        if len(players) != EXPECTED_PLAYERS:
            errors.append(
                f"players.json lists {len(players)} players, expected {EXPECTED_PLAYERS}")
        expected_player_count = PLAYER_ANIMATION_COUNT
        expected_active_count = PLAYER_ACTIVE_IDENTIFIER_COUNT
        expected_turret_aliases = SCRIPT_TURRET_ALIASES
        source = players_data.get("source", {})
        if not isinstance(source, dict):
            source = {}
        if source.get("script") != "mp/playeranim.script":
            errors.append("players.json source script is not mp/playeranim.script")
        if not _valid_sha256(source.get("scriptSha256")):
            errors.append("players.json source script SHA-256 is invalid")
        if source.get("activeIdentifiers") != expected_active_count:
            errors.append(
                "players.json active playeranim identifier count differs "
                "from selected source tier"
            )
        if source.get("exportedPlayerAnimations") != expected_player_count:
            errors.append(
                "players.json exported animation count differs from selected "
                "source tier"
            )
        expanded_turret_aliases = source.get("expandedTurretAliases", [])
        if (
            expanded_turret_aliases
            != list(expected_turret_aliases)
        ):
            errors.append(
                "players.json expanded turret aliases differ from the "
                "selected multiplayer tier"
            )
        if (
            source.get("expandedTurretAnimations")
            != TURRET_EXPANSION_COUNT
        ):
            errors.append(
                "players.json expanded turret animation count differs from "
                "the selected multiplayer tier"
            )
        excluded_turrets = source.get("excludedTurretAnimations")
        if excluded_turrets != list(EXCLUDED_TURRET_ALIASES):
            errors.append(
                "players.json turret-alias exclusions differ from the "
                "vehicle families this product cut"
            )

        source_inventory = None
        source_looped: dict[str, bool] = {}
        if source_root is not None:
            try:
                source_inventory = build_inventory(
                    source_root / "mp" / "playeranim.script",
                    source_root / "xanim",
                )
                source_looped = {
                    reference.name: load_xanim(
                        source_root / "xanim" / reference.name
                    ).looped
                    for reference in source_inventory.animations
                }
            except (OSError, ValueError) as error:
                errors.append(f"cannot validate source player animations: {error}")
            else:
                if source.get("scriptSha256") != source_inventory.script_sha256:
                    errors.append("players.json playeranim script hash mismatch")
                if (
                    source_inventory.active_identifier_count
                    != expected_active_count
                    or len(source_inventory.animations) != expected_player_count
                    or source_inventory.expanded_turret_animation_count
                    != TURRET_EXPANSION_COUNT
                ):
                    errors.append(
                        "staged playeranim inventory differs from selected tier"
                    )
                if expanded_turret_aliases != list(
                    source_inventory.expanded_turret_aliases
                ):
                    errors.append(
                        "players.json expanded turret aliases differ from "
                        "playeranim.script"
                    )
                if excluded_turrets != list(
                    source_inventory.excluded_turret_animations
                ):
                    errors.append(
                        "players.json excluded turret animations differ from "
                        "playeranim.script"
                    )

        canonical_names: list[str] | None = None
        for player in players:
            if not isinstance(player, dict):
                errors.append("players.json contains a malformed player entry")
                continue
            player_id = player.get("id", "?")
            glb = content_path(
                content,
                player.get("glb"),
                f"player {player_id} glb",
                errors,
            )
            if glb is not None and not glb.is_file():
                errors.append(
                    f"player {player_id}: missing glb '{player.get('glb', '')}'"
                )
            animation_entries = player.get("animations", [])
            if not isinstance(animation_entries, list):
                animation_entries = []
                errors.append(
                    f"player {player_id}: animations member is not an array"
                )
            names: list[str] = []
            looped_by_name: dict[str, bool] = {}
            layers_by_name: dict[str, list[str]] = {}
            actions_by_name: dict[str, list[str]] = {}
            occurrences_by_name: dict[str, int] = {}
            turret_metadata_by_name: dict[
                str,
                tuple[str, str, str, str, str, str],
            ] = {}
            player_turret_roles: set[str] = set()
            for animation in animation_entries:
                if not isinstance(animation, dict):
                    errors.append(
                        f"player {player_id}: malformed animation metadata"
                    )
                    continue
                name = animation.get("name")
                looped = animation.get("looped")
                layers = animation.get("layers")
                action_roles = animation.get("actionRoles")
                occurrences = animation.get("scriptOccurrences")
                turret_metadata = (
                    animation.get("sourceAlias"),
                    animation.get("turretWeapon"),
                    animation.get("turretStance"),
                    animation.get("turretPhase"),
                    animation.get("turretHorizontal"),
                    animation.get("turretVertical"),
                )
                if not isinstance(name, str) or not name:
                    errors.append(
                        f"player {player_id}: animation has no name"
                    )
                    continue
                if not isinstance(looped, bool):
                    errors.append(
                        f"player {player_id}: {name} has no loop metadata"
                    )
                if (
                    not isinstance(layers, list)
                    or not layers
                    or not all(
                        layer in ("both", "legs", "torso", "turret")
                        for layer in layers
                    )
                ):
                    errors.append(
                        f"player {player_id}: {name} has invalid script layers"
                    )
                if (
                    not isinstance(action_roles, list)
                    or not action_roles
                    or not all(
                        isinstance(role, str)
                        and (
                            role.startswith("event:")
                            or role.startswith("movement:")
                            or role.startswith("turret:")
                        )
                        for role in action_roles
                    )
                    or len(action_roles) != len(set(action_roles))
                ):
                    errors.append(
                        f"player {player_id}: {name} has invalid actionRoles"
                    )
                elif isinstance(action_roles, list):
                    player_turret_roles.update(
                        role
                        for role in action_roles
                        if isinstance(role, str)
                        and role.startswith("turret:")
                    )
                if not all(
                    isinstance(value, str)
                    for value in turret_metadata
                ):
                    errors.append(
                        f"player {player_id}: {name} has invalid turret "
                        "provenance metadata"
                    )
                    turret_metadata = ("", "", "", "", "", "")
                source_alias = turret_metadata[0]
                has_turret_role = (
                    isinstance(action_roles, list)
                    and any(
                        isinstance(role, str)
                        and role.startswith("turret:")
                        for role in action_roles
                    )
                )
                if (
                    bool(source_alias) != has_turret_role
                    or (
                        not source_alias
                        and any(turret_metadata[1:])
                    )
                ):
                    errors.append(
                        f"player {player_id}: {name} has inconsistent "
                        "turret-alias provenance"
                    )
                folded_name = name.casefold()
                if (
                    folded_name.startswith("standmg42gun_")
                    or folded_name.startswith("pronemg42gun_")
                    or folded_name.startswith("pb_standmg42gunner_")
                    or folded_name.startswith("pb_pronemg42gunner_")
                ):
                    errors.append(
                        f"player {player_id}: {name} is not an allowed "
                        "multiplayer turret-player clip"
                    )
                if (
                    not isinstance(occurrences, int)
                    or isinstance(occurrences, bool)
                    or occurrences <= 0
                ):
                    errors.append(
                        f"player {player_id}: {name} has invalid "
                        "scriptOccurrences"
                    )
                names.append(name)
                looped_by_name[name] = bool(looped)
                layers_by_name[name] = (
                    layers if isinstance(layers, list) else []
                )
                actions_by_name[name] = (
                    action_roles if isinstance(action_roles, list) else []
                )
                occurrences_by_name[name] = (
                    occurrences if isinstance(occurrences, int) else 0
                )
                turret_metadata_by_name[name] = turret_metadata
            if len(names) != expected_player_count or len(names) != len(set(names)):
                errors.append(
                    f"player {player_id}: expected {expected_player_count} "
                    "unique source-derived animations"
                )
            expected_turret_roles = set(TURRET_ACTION_ROLES)
            if player_turret_roles != expected_turret_roles:
                errors.append(
                    f"player {player_id}: turret action-role inventory "
                    "differs from the selected multiplayer tier"
                )
            if canonical_names is None:
                canonical_names = names
            elif names != canonical_names:
                errors.append(
                    f"player {player_id}: animation inventory/order differs "
                    "from other player rigs"
                )
            if glb is not None and glb.is_file():
                try:
                    glb_names = glb_animation_names(glb)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    errors.append(
                        f"player {player_id}: invalid GLB: {error}"
                    )
                else:
                    if set(glb_names) != set(names):
                        errors.append(
                            f"player {player_id}: manifest/GLB animation "
                            "name sets differ"
                        )
            if source_inventory is not None:
                expected_names = [
                    reference.name
                    for reference in source_inventory.animations
                ]
                if names != expected_names:
                    errors.append(
                        f"player {player_id}: GLB inventory does not cover the "
                        "active MP playeranim script exactly"
                    )
                for name in set(names) & set(source_looped):
                    if looped_by_name.get(name) != source_looped[name]:
                        errors.append(
                            f"player {player_id}: {name} loop metadata differs "
                            "from XAnim"
                        )
                expected_by_name = {
                    reference.name: reference
                    for reference in source_inventory.animations
                }
                for name in set(names) & set(expected_by_name):
                    reference = expected_by_name[name]
                    if layers_by_name.get(name) != sorted(reference.layers):
                        errors.append(
                            f"player {player_id}: {name} layer metadata differs "
                            "from playeranim.script"
                        )
                    if actions_by_name.get(name) != sorted(reference.action_roles):
                        errors.append(
                            f"player {player_id}: {name} actionRoles differ "
                            "from playeranim.script"
                        )
                    if occurrences_by_name.get(name) != reference.occurrences:
                        errors.append(
                            f"player {player_id}: {name} scriptOccurrences "
                            "differs from playeranim.script"
                        )
                    expected_turret_metadata = (
                        reference.source_alias,
                        reference.turret_weapon,
                        reference.turret_stance,
                        reference.turret_phase,
                        reference.turret_horizontal,
                        reference.turret_vertical,
                    )
                    if (
                        turret_metadata_by_name.get(name)
                        != expected_turret_metadata
                    ):
                        errors.append(
                            f"player {player_id}: {name} turret provenance "
                            "differs from playeranim.script expansion"
                        )

    expected_prop_models: set[str] | None = None
    catalog = load_json(content / "maps" / "catalog.json")
    if catalog is None:
        errors.append("maps/catalog.json missing or unparseable")
    else:
        if (
            catalog.get("format") != "FriendsOfDuty.MapCatalog"
            or catalog.get("version") != 2
        ):
            errors.append("maps/catalog.json format/version is unsupported")
        maps = catalog.get("maps", [])
        if not isinstance(maps, list):
            maps = []
        counts["maps"] = len(maps)
        actual_maps = validate_map_catalog_roster(
            catalog.get("maps"),
            errors,
        )
        for game in ("cod1", "uo"):
            game_dir = content / "maps" / game
            if not game_dir.is_dir():
                continue
            directory_ids = {
                path.name.casefold()
                for path in game_dir.iterdir()
                if path.is_dir()
            }
            catalog_ids = {
                map_id.casefold()
                for entry_game, map_id in actual_maps
                if entry_game == game
            }
            if directory_ids != catalog_ids:
                errors.append(
                    f"maps/{game} directories differ from the selected "
                    "multiplayer catalog"
                )
        validate_shared_map_texture_closure(
            content,
            maps,
            errors,
        )
        expected_prop_models = set()
        mp_script_manifest = load_json(
            content / "scripts" / "mp" / "mp_content.json"
        )
        if isinstance(mp_script_manifest, dict):
            script_models = mp_script_manifest.get("modelNames", [])
            if isinstance(script_models, list):
                expected_prop_models.update(
                    str(model).casefold()
                    for model in script_models
                    if isinstance(model, str) and model
                )
        expected_effect_ids = {
            **BASE_SCRIPT_EXPLODER_EFFECT_IDS,
            **UO_SCRIPT_EXPLODER_EFFECT_IDS,
        }
        exploder_maps_from_entities: dict[
            tuple[str, str],
            list[dict[str, object]],
        ] = {}
        exploder_efx_from_entities: set[str] = set()
        exploder_textures_from_entities: set[str] = set()
        exploder_aliases_from_entities: set[str] = set()
        exploder_models_from_entities: set[str] = set()
        for entry in maps:
            if not isinstance(entry, dict):
                errors.append("map catalog contains a malformed entry")
                continue
            map_id = entry.get("mapId", "?")
            map_paths: dict[str, Path] = {}
            for key in ("worldGlb", "entitiesJson", "materialsJson"):
                path = content_path(
                    content,
                    entry.get(key),
                    f"map {map_id} {key}",
                    errors,
                )
                if path is not None and not path.is_file():
                    errors.append(
                        f"map {map_id}: missing {key} '{entry.get(key, '')}'"
                    )
                elif path is not None:
                    map_paths[key] = path
            # Optional in the catalog (a map imported before the clip pass, or
            # one with no tool brushes at all, writes ""), but a path that IS
            # declared has to resolve — the alternative is a map that silently
            # ships without its invisible collision.
            clip_glb = entry.get("clipGlb")
            if clip_glb not in (None, ""):
                clip_path = content_path(
                    content,
                    clip_glb,
                    f"map {map_id} clipGlb",
                    errors,
                )
                if clip_path is not None and not clip_path.is_file():
                    errors.append(
                        f"map {map_id}: missing clipGlb '{clip_glb}'"
                    )
            optimization_json = entry.get("optimizationJson")
            if optimization_json not in (None, ""):
                validate_map_optimization(
                    content,
                    optimization_json,
                    map_id,
                    entry.get("worldGlb"),
                    errors,
                )
            lightmaps_json = entry.get("lightmapsJson")
            if lightmaps_json not in (None, ""):
                validate_map_lightmaps(
                    content,
                    lightmaps_json,
                    map_id,
                    entry.get("game"),
                    errors,
                )
            entities_path = map_paths.get("entitiesJson")
            if entities_path is not None and entities_path.is_file():
                entities = load_json(entities_path)
                model_assets = (
                    entities.get("modelAssets")
                    if isinstance(entities, dict)
                    else None
                )
                if (
                    not isinstance(model_assets, list)
                    or not all(
                        isinstance(model, str)
                        and model
                        and "/" not in model
                        and "\\" not in model
                        for model in model_assets
                    )
                ):
                    errors.append(
                        f"map {map_id}: invalid multiplayer modelAssets"
                    )
                else:
                    expected_prop_models.update(
                        model.casefold() for model in model_assets
                    )
                    supported = (
                        entities.get("supportedGameTypes", [])
                        if isinstance(entities, dict)
                        else []
                    )
                    if (
                        isinstance(supported, list)
                        and ("dm" in supported or "tdm" in supported)
                        and HEALTH_PICKUP_MODEL not in {
                            model.casefold()
                            for model in model_assets
                        }
                    ):
                        errors.append(
                            f"map {map_id}: DM/TDM manifest omits "
                            f"'{HEALTH_PICKUP_MODEL}'"
                        )
                if isinstance(entities, dict):
                    exploder = validate_map_script_exploders(
                        content,
                        str(map_id),
                        entities,
                        errors,
                    )
                    actual_effect_ids = tuple(
                        sorted(
                            effect["fxId"]
                            for effect in exploder["effects"]
                        )
                    )
                    expected_ids = tuple(
                        sorted(
                            expected_effect_ids.get(
                                str(map_id),
                                (),
                            )
                        )
                    )
                    if actual_effect_ids != expected_ids:
                        errors.append(
                            f"map {map_id}: exploder fxId roster differs "
                            "from the exact stock MP map"
                        )
                    game_key = str(entry.get("game", ""))
                    if exploder["effects"]:
                        exploder_maps_from_entities[
                            (game_key, str(map_id))
                        ] = exploder["effects"]
                    exploder_efx_from_entities.update(
                        exploder["efxFiles"]
                    )
                    exploder_textures_from_entities.update(
                        exploder["texturePngs"]
                    )
                    exploder_aliases_from_entities.update(
                        exploder["aliasNames"]
                    )
                    exploder_models_from_entities.update(
                        exploder["modelNames"]
                    )
            game = entry.get("game")
            source = str(entry.get("source", ""))
            try:
                archive_name, member = source.split(":", 1)
            except ValueError:
                errors.append(f"map {map_id}: malformed source provenance")
            else:
                allowed_names = COD1_ARCHIVES if game == "cod1" else UO_ARCHIVES
                if archive_name.casefold() not in {
                    name.casefold() for name in allowed_names
                }:
                    errors.append(
                        f"map {map_id}: nonofficial source archive "
                        f"'{archive_name}'"
                    )
                if (
                    not member.casefold().startswith("maps/mp/")
                    or not member.casefold().endswith(".bsp")
                ):
                    errors.append(
                        f"map {map_id}: non-MP BSP source '{member}'"
                    )
            if not _valid_sha256(entry.get("sourceFingerprint")):
                errors.append(f"map {map_id}: invalid source fingerprint")

        exploder_manifest = load_json(
            content / "fx" / "exploders" / "manifest.json"
        )
        if exploder_manifest is None:
            errors.append(
                "fx/exploders/manifest.json missing or unparseable"
            )
        else:
            if (
                exploder_manifest.get("format")
                != "FriendsOfDuty.ScriptExploderContent"
                or exploder_manifest.get("version") != 1
                or exploder_manifest.get("tiers")
                != ["cod1", "uo"]
            ):
                errors.append(
                    "script_exploder content format/tier is unsupported"
                )
            selected_exploder_maps = _mp_gsc_strings(
                exploder_manifest.get("selectedMaps"),
                "script_exploder selectedMaps",
                errors,
            )
            if selected_exploder_maps != list(
                selected_shipping_map_ids(include_uo=True)
            ):
                errors.append(
                    "script_exploder selectedMaps differs from the exact "
                    "shipping map roster"
                )
            manifest_maps = exploder_manifest.get("maps")
            normalized_manifest_maps = {
                (
                    str(map_entry.get("game", "")),
                    str(map_entry.get("mapId", "")),
                ): map_entry.get("effects")
                for map_entry in (
                    manifest_maps
                    if isinstance(manifest_maps, list)
                    else []
                )
                if isinstance(map_entry, dict)
            }
            if normalized_manifest_maps != exploder_maps_from_entities:
                errors.append(
                    "script_exploder global/map effect manifests differ"
                )
            efx_records = exploder_manifest.get("efxFiles")
            texture_records = exploder_manifest.get("textureFiles")
            efx_paths = {
                record.get("path")
                for record in (
                    efx_records if isinstance(efx_records, list) else []
                )
                if isinstance(record, dict)
            }
            texture_paths = {
                record.get("path")
                for record in (
                    texture_records
                    if isinstance(texture_records, list) else []
                )
                if isinstance(record, dict)
            }
            if efx_paths != exploder_efx_from_entities:
                errors.append(
                    "script_exploder EFX files differ from map reachability"
                )
            if texture_paths != exploder_textures_from_entities:
                errors.append(
                    "script_exploder textures differ from map reachability"
                )
            if set(exploder_manifest.get("aliasNames", [])) != (
                exploder_aliases_from_entities
            ):
                errors.append(
                    "script_exploder aliases differ from map reachability"
                )
            if set(exploder_manifest.get("modelNames", [])) != (
                exploder_models_from_entities
            ):
                errors.append(
                    "script_exploder models differ from map reachability"
                )
            if exploder_manifest.get("counts") != (
                SCRIPT_EXPLODER_CLOSURE_COUNTS
            ):
                errors.append(
                    "script_exploder closure counts differ from exact "
                    "selected MP tier"
                )
            for record in (
                list(efx_records)
                if isinstance(efx_records, list)
                else []
            ) + (
                list(texture_records)
                if isinstance(texture_records, list)
                else []
            ):
                if not isinstance(record, dict):
                    errors.append(
                        "script_exploder file provenance is malformed"
                    )
                    continue
                path = content_path(
                    content,
                    record.get("path"),
                    "script_exploder content",
                    errors,
                )
                if (
                    path is None
                    or not path.is_file()
                    or not _valid_sha256(record.get("sha256"))
                    or (
                        path.is_file()
                        and sha256_file(path) != record.get("sha256")
                    )
                ):
                    errors.append(
                        "script_exploder content hash/path mismatch: "
                        f"{record.get('path', '')}"
                    )

    required_props = load_json(content / "props" / "required_models.json")
    required_prop_models: set[str] = set()
    if required_props is None:
        errors.append("props/required_models.json missing or unparseable")
    else:
        if (
            required_props.get("format")
            != "FriendsOfDuty.OriginalMultiplayerPropModels"
            or required_props.get("version") != 1
        ):
            errors.append(
                "props/required_models.json format/version is unsupported"
            )
        required_models = required_props.get("models", [])
        if (
            not isinstance(required_models, list)
            or not all(
                isinstance(model, str)
                and model
                and "/" not in model
                and "\\" not in model
                for model in required_models
            )
            or len(required_models) != len(set(required_models))
        ):
            errors.append("props/required_models.json has an invalid model roster")
            required_models = []
        required_prop_models = {
            model.casefold() for model in required_models
        }
        if required_props.get("count") != len(required_models):
            errors.append("props/required_models.json count is incorrect")
        if (
            expected_prop_models is not None
            and required_prop_models != expected_prop_models
        ):
            errors.append(
                "required prop roster is not exactly derived from selected "
                "multiplayer maps"
            )

    packaged_prop_names: set[str] | None = None
    props_data = load_json(content / "props" / "props.json")
    if props_data is None:
        errors.append("props/props.json missing or unparseable")
    else:
        if (
            props_data.get("format") != "FriendsOfDuty.Props"
            or props_data.get("version") != 2
        ):
            errors.append("props/props.json format/version is unsupported")
        if not _valid_sha256(props_data.get("exportFingerprint")):
            errors.append(
                "props/props.json has no valid prop compiler fingerprint"
            )
        props = props_data.get("props", [])
        if not isinstance(props, list):
            props = []
            errors.append("props/props.json props member is not an array")
        counts["props"] = len(props)
        if not props:
            errors.append("props/props.json lists no props")
        prop_names = {
            str(prop.get("name", "")).casefold()
            for prop in props
            if isinstance(prop, dict)
        }
        packaged_prop_names = prop_names
        if prop_names != required_prop_models:
            errors.append(
                "props.json roster does not exactly match required MP models"
            )
        if props_data.get("requested") != len(required_prop_models):
            errors.append("props.json requested count is incorrect")
        if props_data.get("failures") not in ([], None):
            errors.append("props.json records failed multiplayer prop exports")
        for prop in props:
            if not isinstance(prop, dict):
                errors.append("props.json contains a malformed entry")
                continue
            path = content_path(
                content,
                prop.get("glb"),
                f"prop {prop.get('name', '?')} glb",
                errors,
            )
            if path is not None and not path.is_file():
                errors.append(
                    f"prop {prop.get('name', '?')}: missing glb "
                    f"'{prop.get('glb', '')}'"
                )
            validate_prop_lods(
                content,
                prop,
                errors,
                required=prop.get("animated") is not True,
            )
            prop_name = str(prop.get("name", "")).casefold()
            animations = prop.get("animations")
            source_weapon_files = prop.get("sourceWeaponFiles")
            if prop_name == MOUNTED_MG42_MODEL:
                if (
                    prop.get("animated") is not True
                    or source_weapon_files
                    != list(MOUNTED_MG42_WEAPON_FILES)
                    or not isinstance(animations, list)
                ):
                    errors.append(
                        "props.json mounted MG42 animation provenance is "
                        "incomplete"
                    )
                    animations = []
                expected_animations = {
                    reference.name: reference
                    for reference in MOUNTED_MG42_ANIMATIONS
                }
                actual_names = {
                    animation.get("name")
                    for animation in animations
                    if isinstance(animation, dict)
                }
                if (
                    len(animations) != len(expected_animations)
                    or actual_names != set(expected_animations)
                ):
                    errors.append(
                        "props.json mounted MG42 animation roster is not "
                        "the exact MP weapon-rig closure"
                    )
                for animation in animations:
                    if not isinstance(animation, dict):
                        errors.append(
                            "props.json mounted MG42 contains malformed "
                            "animation metadata"
                        )
                        continue
                    reference = expected_animations.get(
                        animation.get("name")
                    )
                    if reference is None:
                        continue
                    expected_metadata = {
                        "sourceWeaponFile":
                            reference.source_weapon_file,
                        "sourceField": reference.source_field,
                        "stance": reference.stance,
                        "phase": reference.phase,
                        "vertical": reference.vertical,
                        "role": reference.role,
                        "looped": reference.looped,
                        "frames": reference.frame_count,
                        "fps": reference.frame_rate,
                        "animatedBones": 9,
                        "sourceBones": 9,
                    }
                    if any(
                        animation.get(key) != value
                        for key, value in expected_metadata.items()
                    ):
                        errors.append(
                            "props.json mounted MG42 animation metadata "
                            f"differs from retail for {reference.name}"
                        )
                if path is not None and path.is_file():
                    try:
                        action_names = glb_animation_names(path)
                    except (
                        OSError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as error:
                        errors.append(
                            f"prop {prop_name}: invalid animated GLB: {error}"
                        )
                    else:
                        if (
                            len(action_names) != len(expected_animations)
                            or set(action_names) != set(expected_animations)
                        ):
                            errors.append(
                                "mounted MG42 manifest/GLB animation sets "
                                "differ"
                            )
            elif (
                prop.get("animated") is not False
                or source_weapon_files != []
                or animations != []
            ):
                errors.append(
                    f"prop {prop.get('name', '?')}: unexpected animated "
                    "prop metadata"
                )
            materials = prop.get("materials", [])
            if not isinstance(materials, list):
                errors.append(
                    f"prop {prop.get('name', '?')}: materials is not an array"
                )
                continue
            for material in materials:
                if not isinstance(material, dict):
                    errors.append(
                        f"prop {prop.get('name', '?')}: malformed material"
                    )
                    continue
                texture = content_path(
                    content,
                    material.get("texture"),
                    f"prop {prop.get('name', '?')} texture",
                    errors,
                )
                if texture is not None and not texture.is_file():
                    errors.append(
                        f"prop {prop.get('name', '?')}: missing texture "
                        f"'{material.get('texture', '')}'"
                    )

    presentation_alias_names: set[str] | None = None
    presentation = load_json(content / "audio" / "presentation.json")
    if presentation is None:
        errors.append("audio/presentation.json missing or unparseable")
    elif not presentation.get("aliases"):
        errors.append("audio/presentation.json has no aliases")
    else:
        aliases = presentation.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
            errors.append("audio/presentation.json aliases is not an array")
        alias_names: set[str] = set()
        for entry in aliases:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("alias"), str)
                or not entry["alias"]
            ):
                errors.append("audio/presentation.json has a malformed alias")
                continue
            alias = entry["alias"]
            if alias in alias_names:
                errors.append(f"audio/presentation.json duplicates alias '{alias}'")
            alias_names.add(alias)
            variants = entry.get("variants", [])
            if not isinstance(variants, list) or not variants:
                errors.append(f"audio alias '{alias}' has no variants")
                continue
            for variant in variants:
                if not isinstance(variant, dict):
                    errors.append(f"audio alias '{alias}' has malformed variant")
                    continue
                sound_file = variant.get("file")
                if (
                    isinstance(sound_file, str)
                    and sound_file != sound_file.casefold()
                ):
                    errors.append(
                        f"audio alias '{alias}' file is not canonical "
                        "lowercase"
                    )
                audio_path = content_path(
                    content / "audio",
                    sound_file,
                    f"audio alias {alias}",
                    errors,
                )
                if audio_path is not None and not audio_path.is_file():
                    errors.append(
                        f"audio alias '{alias}' references missing file "
                        f"'{variant.get('file', '')}'"
                    )
        presentation_alias_names = alias_names
        required_aliases = (
            list(ORDNANCE_ALIASES) +
            list(MINEFIELD_ALIAS_VARIANTS) +
            list(HEALTH_PICKUP_ALIAS_VARIANTS)
        )
        required_aliases += [
            "smokegrenade_explode",
            "smokegrenade_smoke",
            *UO_SCRIPT_EXPLODER_ALIAS_VARIANTS,
        ]
        for alias in required_aliases:
            if alias not in alias_names:
                errors.append(
                    f"audio/presentation.json lacks required multiplayer "
                    f"alias '{alias}'"
                )
        aliases_by_name = {
            entry.get("alias"): entry
            for entry in aliases
            if isinstance(entry, dict)
        }
        exact_global_variants = {
            **MINEFIELD_ALIAS_VARIANTS,
            **HEALTH_PICKUP_ALIAS_VARIANTS,
            **UO_SCRIPT_EXPLODER_ALIAS_VARIANTS,
        }
        for alias, expected_variants in exact_global_variants.items():
            entry = aliases_by_name.get(alias)
            if entry is None:
                continue
            if entry.get("variants") != expected_variants:
                errors.append(
                    f"audio alias '{alias}' does not match the exact stock "
                    "multiplayer global-script variant"
                )
        source_archives = presentation.get("sourceArchives", [])
        if not isinstance(source_archives, list) or not source_archives:
            errors.append(
                "audio/presentation.json lacks official source archive records"
            )
        else:
            allowed_paths = {
                f"Main/{name}".casefold() for name in COD1_ARCHIVES
            } | {f"uo/{name}".casefold() for name in UO_ARCHIVES}
            for record in source_archives:
                if not isinstance(record, dict):
                    errors.append("presentation source archive record malformed")
                    continue
                path = str(record.get("path", ""))
                if path.casefold() not in allowed_paths:
                    errors.append(
                        f"presentation names nonselected/nonofficial source: "
                        f"{path or '?'}"
                    )
                if not _valid_sha256(record.get("sha256")):
                    errors.append(
                        f"presentation source archive hash invalid: "
                        f"{path or '?'}"
                    )

    validate_multiplayer_gsc_content(
        content,
        errors,
        presentation_aliases=presentation_alias_names,
        presentation=presentation,
        prop_names=packaged_prop_names,
        game_root=game_root,
    )

    if not (content / ORDNANCE_FX_TEXTURE).is_file():
        errors.append(
            f"{ORDNANCE_FX_TEXTURE} missing (ordnance explosion sprites;"
            " run the ordnance step)")

    fx_textures = content / "fx" / "textures"
    absent_minefield_textures = [
        name for name in MINEFIELD_FX_TEXTURES
        if not (fx_textures / f"{name}.png").is_file()
    ]
    if absent_minefield_textures:
        errors.append(
            "fx/textures missing required multiplayer minefield sprites: "
            f"{', '.join(absent_minefield_textures)} "
            "(run the impacts step)"
        )
    absent_minefield_efx = [
        name for name in MINEFIELD_EFX
        if not (content / "fx" / "efx" / f"{name}.efx").is_file()
    ]
    if absent_minefield_efx:
        errors.append(
            "fx/efx missing required multiplayer minefield effects: "
            f"{', '.join(absent_minefield_efx)} "
            "(run the impacts step)"
        )
    for label, names in (
        ("bullet-impact marks", IMPACT_MARK_TEXTURES),
        ("impact particle sprites", IMPACT_SPRITE_TEXTURES),
    ):
        absent = [
            name for name in names
            if not (fx_textures / f"{name}.png").is_file()]
        if absent:
            warnings.append(
                f"fx/textures missing {len(absent)} {label}: "
                f"{', '.join(absent)} (run the impacts step)")
    absent_efx = [
        name for name in IMPACT_REFERENCE_EFX
        if not (content / "fx" / "efx" / f"{name}.efx").is_file()]
    if absent_efx:
        warnings.append(
            "fx/efx missing impact reference effects: "
            f"{', '.join(absent_efx)} (run the impacts step)")

    # Weapon flash/eject presence + the fx shader manifest. Warn-only: both
    # are additive presentation features, and a package from an older
    # exporter without them still mounts (the runtime falls back to its
    # basename guessing).
    validate_weapon_effect_presence(content, warnings)
    validate_fx_shader_manifest(content, warnings)
    # The per-surface impact table + its bullet effect closure. Warn-only
    # for the same reason: absent on older packages, and the runtime keeps
    # its transcribed nine-bucket profiles as the fallback.
    validate_impacts_manifest(content, warnings)

    clips_root = content / "audio" / "footsteps" / "clips"
    families = [
        family for family in clips_root.iterdir()
        if family.is_dir() and count_files(family, (".wav",)) > 0
    ] if clips_root.is_dir() else []
    if not families:
        errors.append("audio/footsteps/clips has no clip families")
    footsteps = load_json(
        content / "audio" / "footsteps" / "footstep_manifest.json"
    )
    if footsteps is None:
        errors.append("audio/footsteps/footstep_manifest.json missing/unparseable")
    else:
        footstep_source = footsteps.get("source", {})
        for key in ("audio_archive", "alias_archive"):
            record = (
                footstep_source.get(key)
                if isinstance(footstep_source, dict)
                else None
            )
            if not isinstance(record, dict):
                errors.append(f"footstep source {key} record missing")
                continue
            path = str(record.get("path", ""))
            if path.casefold() not in {
                f"Main/{name}".casefold() for name in COD1_ARCHIVES
            }:
                errors.append(f"footsteps name nonofficial source: {path or '?'}")
            if not _valid_sha256(record.get("sha256")):
                errors.append(f"footstep source archive hash invalid: {path or '?'}")
        clips = footsteps.get("clips", {})
        if not isinstance(clips, dict) or not clips:
            errors.append("footstep manifest contains no clip provenance")
        else:
            for details in clips.values():
                if not isinstance(details, dict):
                    errors.append("footstep clip provenance is malformed")
                    continue
                path = content_path(
                    content / "audio" / "footsteps",
                    details.get("path"),
                    "footstep clip",
                    errors,
                )
                if path is None or not path.is_file():
                    errors.append(
                        f"footstep clip missing: {details.get('path', '')}"
                    )
                elif sha256_file(path) != details.get("sha256"):
                    errors.append(
                        f"footstep clip hash mismatch: {details.get('path', '')}"
                    )

    if not (content / "ui" / "reticle_q.png").is_file():
        errors.append("ui/reticle_q.png missing")

    hud_dir = content / "ui" / "hud"
    for name in ("compassface.png", "compass_arrow.png", "health_back.png",
                 "health_bar.png", "health_cross.png"):
        if not (hud_dir / name).is_file():
            errors.append(f"ui/hud/{name} missing")
    if not (hud_dir / "textback.png").is_file():
        warnings.append("ui/hud/textback.png missing (chat/kill-feed text backing)")
    if count_files(hud_dir / "death", (".png",)) == 0:
        warnings.append("ui/hud/death has no kill-feed weapon icons")
    for name, hint in (
        ("stance_stand.png", "stance indicator"),
        ("stance_crouch.png", "stance indicator"),
        ("stance_prone.png", "stance indicator"),
        ("ammo_bullet.png", "ammo readout bullets glyph"),
        ("ammo_back.png", "ammo readout backdrop"),
        ("grenade_frag.png", "grenade HUD icon"),
    ):
        if not (hud_dir / name).is_file():
            warnings.append(f"ui/hud/{name} missing ({hint})")
    # The M18 smoke grenade is a regular loadout weapon now, so its HUD icon
    # is required rather than tier-dependent.
    if not (hud_dir / "grenade_smoke.png").is_file():
        errors.append("ui/hud/grenade_smoke.png missing")
    return counts


def collect_notes(notes_dir: Path | None) -> list[str]:
    notes: list[str] = []
    if notes_dir and notes_dir.is_dir():
        for path in sorted(notes_dir.glob("*.txt")):
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and line not in notes:
                    notes.append(line)
    return notes


def source_summary(
    game_root: Path | None,
    content: Path,
) -> tuple[str, bool]:
    if game_root and game_root.is_dir():
        archives = selected_archives(game_root)
        main_count = sum(path.parent.name == "Main" for path in archives)
        uo_count = sum(path.parent.name == "uo" for path in archives)
        return (
            f"CoD1 MP (Main, {main_count} official pk3)"
            f" + UO MP (uo, {uo_count} official pk3)",
            True,
        )
    policy = load_json(content / "provenance" / "source_archives.json") or {}
    tiers = policy.get("tiers", [])
    if "uo" not in tiers:
        raise SystemExit(
            "provenance/source_archives.json does not declare United "
            "Offensive; Friends of Duty requires Call of Duty + United "
            "Offensive. Re-run the exporter with both installed."
        )
    return "Official multiplayer source (detached install)", True


def flattened_efx_name(reference: str) -> str:
    """fx/efx basename for a WEAPONFILE/entities .efx reference.

    Retail UO WEAPONFILEs spell three worldFlashEffect values with a doubled
    extension (``mf_kar98.efx.efx``) while the archived member has one; the
    engine strips and re-appends the extension, so fold the duplication —
    and add the extension for references authored without one — before
    flattening to the packaged basename.
    """
    name = PurePosixPath(reference.replace("\\", "/").strip()).name
    while name.casefold().endswith(".efx.efx"):
        name = name[: -len(".efx")]
    if name and not name.casefold().endswith(".efx"):
        name += ".efx"
    return name


def validate_weapon_effect_presence(
    content: Path,
    warnings: list[str],
) -> None:
    """Warn when a weapons.json definition names an efx that is not packaged.

    The presentation step flattens every WEAPONFILE-referenced efx into
    fx/efx/<basename>; a definition whose muzzle/world/shell effect has no
    flattened file would leave the runtime with a name it cannot resolve.
    """
    efx_dir = content / "fx" / "efx"
    packaged_efx_names = (
        {entry.name.casefold() for entry in efx_dir.glob("*.efx")}
        if efx_dir.is_dir()
        else set()
    )
    weapons_manifest = load_json(content / "weapons" / "weapons.json") or {}
    manifest_weapons = weapons_manifest.get("weapons", [])
    if not isinstance(manifest_weapons, list):
        return
    for weapon in manifest_weapons:
        if not isinstance(weapon, dict):
            continue
        definition = weapon.get("definition")
        if not isinstance(definition, dict):
            continue
        for key in ("muzzle_effect", "world_muzzle_effect", "shell_effect"):
            reference = str(definition.get(key, "") or "").strip()
            if not reference or reference.lower() == "none":
                continue
            flattened = flattened_efx_name(reference).casefold()
            if flattened not in packaged_efx_names:
                warnings.append(
                    f"weapon {weapon.get('id', '?')} {key} '{reference}' "
                    "has no flattened fx/efx file (run the presentation "
                    "step)"
                )


def validate_fx_shader_manifest(
    content: Path,
    warnings: list[str],
) -> None:
    """Warn when fx/shaders.json is absent, dangling, or incomplete."""
    efx_dir = content / "fx" / "efx"
    shaders_manifest = load_json(content / "fx" / "shaders.json")
    if shaders_manifest is None:
        warnings.append(
            "fx/shaders.json missing/unparseable (run the package step "
            "with --game-root to regenerate it)"
        )
        return
    if (
        shaders_manifest.get("format") != FX_SHADERS_FORMAT
        or shaders_manifest.get("version") != FX_SHADERS_VERSION
    ):
        warnings.append("fx/shaders.json format/version is unsupported")
    shader_entries = shaders_manifest.get("shaders", [])
    if not isinstance(shader_entries, list):
        shader_entries = []
        warnings.append("fx/shaders.json shaders member is not an array")
    mapped_shaders: set[str] = set()
    for entry in shader_entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("shader"), str)
            or not entry.get("shader")
            or not isinstance(entry.get("texture"), str)
        ):
            warnings.append("fx/shaders.json contains a malformed entry")
            continue
        mapped_shaders.add(entry["shader"].casefold())
        texture_errors: list[str] = []
        texture_path = content_path(
            content,
            entry["texture"],
            f"fx shader '{entry['shader']}' texture",
            texture_errors,
        )
        if (
            texture_errors
            or texture_path is None
            or not texture_path.is_file()
        ):
            warnings.append(
                f"fx shader '{entry['shader']}' references missing "
                f"texture '{entry['texture']}'"
            )
    referenced_shaders: dict[str, str] = {}
    if efx_dir.is_dir():
        for efx_path in sorted(efx_dir.glob("*.efx")):
            try:
                text = efx_path.read_text(encoding="latin1", errors="replace")
            except OSError:
                continue
            try:
                names = efx_shader_names(text)
            except ScriptExploderClosureError as error:
                warnings.append(
                    f"fx/efx/{efx_path.name} shader block is "
                    f"malformed: {error}"
                )
                continue
            for shader in names:
                referenced_shaders.setdefault(shader.casefold(), shader)
    absent_shaders = sorted(set(referenced_shaders) - mapped_shaders)
    if absent_shaders:
        preview = ", ".join(
            referenced_shaders[key] for key in absent_shaders[:6]
        ) + (", ..." if len(absent_shaders) > 6 else "")
        warnings.append(
            f"fx/shaders.json lacks mappings for {len(absent_shaders)} "
            f"shader(s) referenced by fx/efx: {preview}"
        )


def validate_impacts_manifest(
    content: Path,
    warnings: list[str],
) -> None:
    """Warn when fx/impacts.json is absent/malformed, when a closure-type
    row names an efx with no packaged fx/efx file, or when a materials.json
    ``surface`` token is outside the table's surface vocabulary.

    The efx-presence check covers exactly the rows the impacts step ships
    closures for, derived from the SAME rule
    (cod1_impact_table.is_closure_impact_type: every bullet_* type — CoD1
    small/large and UO's gmi per-class rows — plus grenade_bounce/
    grenade_explode) so validator and closure can never disagree. The heavy
    ordnance no shipping weapon fires (molotov_*, mortar/tank/artillery/
    b17_explode, smoke_grenade_explode) is table data the runtime filters,
    so its efx are deliberately not packaged and must not warn.
    """
    manifest = load_json(content / "fx" / "impacts.json")
    if manifest is None:
        warnings.append(
            "fx/impacts.json missing/unparseable (run the impacts step)"
        )
        return
    if (
        manifest.get("format") != IMPACTS_FORMAT
        or manifest.get("version") != IMPACTS_VERSION
    ):
        warnings.append("fx/impacts.json format/version is unsupported")
    rows = manifest.get("rows", [])
    if not isinstance(rows, list) or not rows:
        warnings.append("fx/impacts.json contains no rows")
        rows = []
    efx_dir = content / "fx" / "efx"
    packaged_efx_names = (
        {entry.name.casefold() for entry in efx_dir.glob("*.efx")}
        if efx_dir.is_dir()
        else set()
    )
    vocabulary: set[str] = set()
    absent_efx: dict[str, str] = {}
    malformed = False
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("impact"), str)
            or not row.get("impact")
            or not isinstance(row.get("surface"), str)
            or not row.get("surface")
            or not isinstance(row.get("efx"), str)
        ):
            malformed = True
            continue
        if row["surface"] != "default":
            vocabulary.add(row["surface"])
        efx = row["efx"]
        if is_closure_impact_type(row["impact"]) and efx:
            flattened = flattened_efx_name(efx)
            if flattened.casefold() not in packaged_efx_names:
                absent_efx.setdefault(flattened.casefold(), efx)
    if malformed:
        warnings.append("fx/impacts.json contains a malformed row")
    if absent_efx:
        references = sorted(absent_efx.values(), key=str.casefold)
        preview = ", ".join(references[:6]) + (
            ", ..." if len(references) > 6 else ""
        )
        warnings.append(
            f"fx/efx missing {len(references)} impact effect(s) "
            f"named by fx/impacts.json: {preview} (run the impacts step)"
        )
    unknown_surfaces: dict[str, str] = {}
    maps_root = content / "maps"
    if maps_root.is_dir():
        for materials_path in sorted(maps_root.glob("*/*/materials.json")):
            materials = load_json(materials_path)
            if not isinstance(materials, list):
                continue
            for material in materials:
                if not isinstance(material, dict):
                    continue
                surface = material.get("surface")
                if not isinstance(surface, str) or not surface:
                    continue
                if surface not in vocabulary:
                    unknown_surfaces.setdefault(
                        surface,
                        materials_path.relative_to(content).as_posix(),
                    )
    if unknown_surfaces:
        details = ", ".join(
            f"'{token}' ({unknown_surfaces[token]})"
            for token in sorted(unknown_surfaces)[:6]
        ) + (", ..." if len(unknown_surfaces) > 6 else "")
        warnings.append(
            f"materials.json declares {len(unknown_surfaces)} surface "
            f"token(s) outside the impact table vocabulary: {details}"
        )


def write_fx_shader_manifest(content: Path, game_root: Path) -> None:
    """Emit fx/shaders.json: every fx shader name referenced by an efx under
    fx/efx/, mapped to its packaged fx/textures PNG.

    Written at package time because this is the first point where every step
    that ships into fx/efx (presentation, impacts, ordnance) has run, so the
    manifest covers the whole directory rather than one owner's slice. The
    shader names come from the shipped efx text; the shader->image hop
    resolves through the retail archives exactly the way the exploder
    closure does (declared *.shader stanzas first, then the engine's
    same-named-texture fallback that serves the ``*_sn`` muzzle-flash
    names). A shader whose resolved texture is not packaged is omitted — an
    entry's texture must exist — and reported so the owning step can be
    extended.
    """
    efx_dir = content / "fx" / "efx"
    if not efx_dir.is_dir():
        return
    index = LayeredArchiveIndex(selected_archives(game_root))
    texture_names: dict[str, str] = {}
    textures_dir = content / "fx" / "textures"
    if textures_dir.is_dir():
        for entry in sorted(textures_dir.glob("*.png")):
            texture_names.setdefault(entry.stem.casefold(), entry.name)
    shaders: dict[str, str] = {}
    for efx_path in sorted(
        efx_dir.glob("*.efx"),
        key=lambda path: path.name.casefold(),
    ):
        text = efx_path.read_text(encoding="latin1", errors="replace")
        try:
            names = efx_shader_names(text)
        except ScriptExploderClosureError as error:
            print(
                f"WARNING fx/efx/{efx_path.name} shader block is "
                f"malformed: {error}"
            )
            continue
        for shader in names:
            shaders.setdefault(shader.casefold(), shader)
    entries: list[dict[str, str]] = []
    omitted: list[str] = []
    for key in sorted(shaders):
        shader = shaders[key]
        try:
            images = resolve_shader_images(index, shader)
        except ScriptExploderClosureError:
            omitted.append(shader)
            continue
        # Animated materials list one image per frame; the manifest maps the
        # material to its first frame, which is also what the flattened
        # textures directory serves a basename-guessing consumer.
        texture = texture_names.get(PurePosixPath(images[0]).stem.casefold())
        if texture is None:
            omitted.append(shader)
            continue
        entries.append(
            {"shader": shader, "texture": f"fx/textures/{texture}"}
        )
    payload = {
        "format": FX_SHADERS_FORMAT,
        "version": FX_SHADERS_VERSION,
        "shaders": entries,
    }
    (content / "fx" / "shaders.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"fx/shaders.json written: {len(entries)} shader mapping(s)"
        + (f", {len(omitted)} omitted" if omitted else "")
    )
    if omitted:
        preview = ", ".join(omitted[:6]) + (
            ", ..." if len(omitted) > 6 else ""
        )
        print(
            f"WARNING fx/shaders.json omits {len(omitted)} shader(s) whose "
            f"textures are not packaged: {preview}"
        )


FACTIONS_FORMAT = "FriendsOfDuty.Factions"
FACTIONS_VERSION = 1

# The retail multiplayer factions, mirroring Cod1ExperienceProfile's defaults
# in Assets/Scripts/Content. Teams are allies/axis and exist only in the team
# modes; the faction supplies the character and the SPAWN KIT in every mode,
# which is what lets one team field several factions at once.
#
# Retail pinned the allied nationality per map and gave the player no choice.
# That rule is gone by decision, so all three allied factions are selectable
# on every map and a team may mix them.
#
# primaryWeapons/sidearm/grenade are the STARTING kit only. Map pickups and
# dropped weapons are not governed here, which is why the panzerfaust appears
# in no faction.
COD1_FACTIONS = [
    {
        "id": "american",
        "displayName": "American",
        "teams": ["allies"],
        "characterProfile": "american_airborne",
        "primaryWeapons": [
            "m1carbine", "m1garand", "thompson", "bar", "springfield",
        ],
        "sidearm": "colt45",
        "grenade": "fraggrenade",
    },
    {
        "id": "british",
        "displayName": "British",
        "teams": ["allies"],
        "characterProfile": "british_airborne",
        "primaryWeapons": ["enfield", "sten", "bren"],
        "sidearm": "colt45",
        "grenade": "mk1britishfrag",
    },
    {
        "id": "russian",
        "displayName": "Russian",
        "teams": ["allies"],
        "characterProfile": "russian_conscript",
        "primaryWeapons": ["mosin_nagant", "mosin_nagant_scoped", "ppsh"],
        "sidearm": "colt45",
        "grenade": "rgd33",
    },
    {
        "id": "german",
        "displayName": "German",
        "teams": ["axis"],
        "characterProfile": "german_wehrmacht",
        "primaryWeapons": ["kar98k", "kar98k_scoped", "mp40", "mp44", "fg42"],
        "sidearm": "luger",
        "grenade": "stielhandgranate",
    },
]


def write_factions(content: Path) -> None:
    """Emit factions.json.

    The runtime falls back to the CoD1 profile's compiled defaults when this
    file is absent, so writing it is not what makes an existing package work
    — it is what makes the package SELF-DESCRIBING, so the same code path
    serves a retail package and an original one.
    """
    document = {
        "format": FACTIONS_FORMAT,
        "version": FACTIONS_VERSION,
        "factions": COD1_FACTIONS,
    }
    (content / "factions.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8")


def validate_factions(content: Path) -> list[str]:
    """Check every faction reference resolves inside this package.

    The runtime performs the same check (FodContentSchemas.
    ValidateFactionReferences) and throws on a miss, so catching it here
    turns "the game refused your package at mount" into a build error naming
    the id. A dangling reference is how a player ends up with no model or
    empty hands.
    """
    errors: list[str] = []
    factions_path = content / "factions.json"
    if not factions_path.exists():
        return errors

    document = load_json(factions_path)
    weapons = {
        str(entry.get("id", "")).lower()
        for entry in load_json(
            content / "weapons" / "weapons.json").get("weapons", [])
    }
    players = {
        str(entry.get("id", "")).lower()
        for entry in load_json(
            content / "players" / "players.json").get("players", [])
    }
    for faction in document.get("factions", []):
        label = f"factions.json faction '{faction.get('id')}'"
        profile = str(faction.get("characterProfile", ""))
        if profile.lower() not in players:
            errors.append(
                f"{label} names character profile '{profile}', which "
                "players.json does not declare")
        kit = list(faction.get("primaryWeapons", []))
        kit.append(faction.get("sidearm", ""))
        kit.append(faction.get("grenade", ""))
        for weapon_id in kit:
            if str(weapon_id).lower() not in weapons:
                errors.append(
                    f"{label} names weapon '{weapon_id}', which weapons.json "
                    "does not declare")
    return errors


# The mods page shows this as a card. 16:9 at 512x288 is the recommended
# shape: large enough to read at card size and in a detail panel, small
# enough that a package is not carrying a megabyte of chrome. It is a
# RECOMMENDATION, not a validated requirement — the page fits whatever it is
# given, and rejecting a community pak over its thumbnail would be hostile.
PREVIEW_MAX_WIDTH = 512
PREVIEW_MAX_HEIGHT = 288
AUTHORED_PREVIEW_PATH = "ui/preview.jpg"


def pick_preview_image(content: Path) -> str:
    """Pak-relative preview for the mods page, or "" when there is none.

    Prefers an authored ui/preview.(jpg|png). Falls back to a map preview,
    which is the only other screenshot a package contains — those are square
    1024s rather than 16:9, so they read as a stopgap, which is what they
    are.

    Chosen from files the package actually contains rather than declared as a
    fixed path: an earlier version named "ui/preview.png", which the exporter
    never produced, so every package advertised an image that was not there.
    The runtime tolerates that (it resolves to null), but a manifest should
    not describe files that do not exist.

    The lowest name wins among map previews so the choice is stable across
    rebuilds rather than varying with directory order.
    """
    for authored in ("ui/preview.jpg", "ui/preview.png"):
        if (content / authored).is_file():
            return authored
    previews = content / "maps" / "previews"
    if not previews.is_dir():
        return ""
    candidates = sorted(
        entry.name for entry in previews.iterdir()
        if entry.is_file() and entry.suffix.lower() == ".png"
    )
    return f"maps/previews/{candidates[0]}" if candidates else ""


def count_factions(content: Path) -> int:
    """How many factions the written factions.json declares, or 0."""
    path = content / "factions.json"
    if not path.exists():
        return 0
    try:
        return len(load_json(path).get("factions", []))
    except Exception:
        return 0


def write_manifest(content: Path, counts: dict[str, int], summary: str,
                   has_uo: bool, notes: list[str]) -> None:
    manifest = {
        "format": FORMAT_NAME,
        "version": PACKAGE_VERSION,
        "gameContentVersion": GAME_CONTENT_VERSION,
        "exporterVersion": EXPORTER_VERSION,
        "createdUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Pak identity and the experience contract this package claims. Both
        # are additive: the runtime reads an absent "profile" as PROFILE_ID
        # (ExperienceProfiles.Default), so packages built before these fields
        # existed keep mounting and no gameContentVersion bump is owed. An
        # explicit "cod1" validates through exactly the same path.
        "id": PROFILE_ID,
        "displayName": PROFILE_DISPLAY_NAME,
        "profile": PROFILE_ID,
        # Shown on the in-game mods page. All optional and all display-only.
        "description": PROFILE_DESCRIPTION,
        "creator": PROFILE_CREATOR,
        "previewImage": pick_preview_image(content),
        # Distinct from createdUtc so a rebuild of the same package reads as
        # updated rather than as a different package.
        "updatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sourceSummary": summary,
        "hasUnitedOffensive": has_uo,
        "contentTier": "cod1+uo-mp" if has_uo else "cod1-mp",
        "sourcePolicyFormat": POLICY_FORMAT,
        "sourcePolicyVersion": POLICY_VERSION,
        "categories": [
            {"name": "weapons", "count": counts["weapons"]},
            {"name": "players", "count": counts["players"]},
            {"name": "maps", "count": counts["maps"]},
            # Counted from the document just written rather than from
            # len(COD1_FACTIONS), so the manifest reports what the package
            # actually carries even if the two ever disagree.
            {"name": "factions", "count": count_factions(content)},
            {"name": "props", "count": counts["props"]},
            {"name": "audio", "count": count_files(content / "audio", AUDIO_EXTENSIONS)},
            {"name": "fx", "count": count_files(content / "fx")},
        ],
        "orientation": {
            "viewmodelYawOffsetDegrees": 90.0,
            "playerVisualYawOffsetDegrees": 90.0,
            "playerAuthoredForward": "-X",
        },
        "notes": notes,
    }
    (content / "fodpak.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def write_zip(content: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = sorted(
        path for path in content.rglob("*") if path.is_file())
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(content).as_posix())
    print(f"Wrote {destination} ({destination.stat().st_size} bytes, {len(files)} files)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("content_root", type=Path,
                        help="fodpak content directory (the mounted 'current/' form)")
    parser.add_argument("--game-root", type=Path,
                        help="Call of Duty install used for sourceSummary/hasUnitedOffensive")
    parser.add_argument(
        "--source-root",
        type=Path,
        help="official extracted multiplayer staging root used for semantic validation",
    )
    # Retired: United Offensive is mandatory, so there is no tier to select.
    # Accepted and ignored so an in-flight pipeline or a user's saved command
    # line keeps working.
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--notes-dir", type=Path,
                        help="directory of *.txt note files folded into fodpak.json notes")
    parser.add_argument("--zip", type=Path, dest="zip_path", metavar="FILE.fodpak",
                        help="also write a transportable zip of the content directory")
    parser.add_argument("--skip-validate", action="store_true",
                        help="write fodpak.json/zip even when roster validation fails")
    args = parser.parse_args()

    content = args.content_root.resolve()
    if not content.is_dir():
        raise SystemExit(f"content root does not exist: {content}")

    game_root = args.game_root.resolve() if args.game_root else None
    source_root = args.source_root.resolve() if args.source_root else None
    # Refuse before writing the provenance manifest: a package stamped from a
    # UO-less install could never validate, and stamping it first would leave
    # the content root claiming a tier it does not have.
    if game_root is not None:
        ok, message = uo_installation_status(game_root)
        if not ok:
            raise SystemExit(message)
        write_policy_manifest(
            content / "provenance" / "source_archives.json",
            game_root,
        )
        # Written before validation, like the policy manifest, so the same
        # run that produced it also proves every mapped texture exists.
        write_fx_shader_manifest(content, game_root)

    errors: list[str] = []
    warnings: list[str] = []
    counts = validate(
        content,
        errors,
        warnings,
        source_root=source_root,
        game_root=game_root,
    )
    for warning in warnings:
        print(f"WARNING {warning}")
    for error in errors:
        print(f"ERROR {error}")
    # --skip-validate is a developer convenience for cosmetic warnings; it
    # must not be able to stamp a package that is missing United Offensive or
    # part of the seven-map roster, because the runtime refuses those.
    fatal = [
        error for error in errors
        if "United Offensive" in error
        or "multiplayer map" in error
        or "map catalog" in error
    ]
    if fatal:
        for error in fatal:
            print(f"FATAL {error}")
        raise SystemExit(
            f"package validation failed with {len(fatal)} unskippable error(s)"
        )
    if errors and not args.skip_validate:
        raise SystemExit(f"package validation failed with {len(errors)} error(s)")

    # Written before validation so a dangling faction reference fails the
    # build here, naming the id, rather than at mount inside the game.
    write_factions(content)
    faction_errors = validate_factions(content)
    if faction_errors:
        for error in faction_errors:
            print(f"FATAL {error}")
        raise SystemExit(
            f"factions validation failed with {len(faction_errors)} error(s)")

    notes = collect_notes(args.notes_dir)
    summary, has_uo = source_summary(
        game_root,
        content,
    )
    write_manifest(content, counts, summary, has_uo, notes)
    print(
        f"fodpak.json written: weapons={counts['weapons']} players={counts['players']} "
        f"maps={counts['maps']} props={counts['props']} uo={has_uo} notes={len(notes)}")

    if args.zip_path:
        write_zip(content, args.zip_path.resolve())


if __name__ == "__main__":
    main()
