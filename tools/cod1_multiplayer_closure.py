#!/usr/bin/env python3
"""Reachability policy for Friends of Duty's detached CoD1 staging trees.

The retail archives use global ``xmodel``, ``xanim`` and ``skins`` roots that
contain both campaign and multiplayer data.  Copying those roots wholesale is
therefore not a multiplayer source policy.  This module builds the two small,
explicit closures consumed by the exporter:

* weapon/player source: the selected MP WEAPONFILE roster, its alternate
  variants, view/world/projectile models, viewmodel animations, the official
  MP player rigs and every exportable binding in ``mp/playeranim.script``;
* map-model source: XModels placed by the selected official MP BSP/GSC files.

Every XModel is expanded through its compiled v14 LOD table to the matching
XModelParts, XModelSurfs and skin images.  Lookups are case-insensitive, just
like the original engine, while returned paths retain the official archive
spelling for provenance and case-sensitive hosts.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from cod1_playeranim import (
    expand_turret_reference,
    is_excluded_vehicle_animation,
    parse_active_references,
)
from cod1_mp_gsc_content import analyze_multiplayer_gsc
from cod1_script_exploder import (
    build_effect_closure,
    map_exploder_effects,
)


MANAGED_ASSET_ROOTS = (
    "skins/",
    "xanim/",
    "xmodel/",
    "xmodelalias/",
    "xmodelparts/",
    "xmodelsurfs/",
)
MANAGED_WEAPON_ROOTS = ("weapons/mp/",)
MANAGED_EXACT_FILES = ("mp/playeranim.script",)

BASE_WEAPON_FILES = (
    "bar_mp",
    "bren_mp",
    "colt_mp",
    "enfield_mp",
    "fg42_mp",
    "fraggrenade_mp",
    "kar98k_mp",
    "kar98k_sniper_mp",
    "luger_mp",
    "m1carbine_mp",
    "m1garand_mp",
    "mk1britishfrag_mp",
    "mosin_nagant_mp",
    "mosin_nagant_sniper_mp",
    "mp40_mp",
    "mp44_mp",
    "panzerfaust_mp",
    "ppsh_mp",
    "rgd-33russianfrag_mp",
    "springfield_mp",
    "sten_mp",
    "stielhandgranate_mp",
    "thompson_mp",
)
# United Offensive's own multiplayer additions. UO's pakuo04.pk3 is a
# SUPERSET of the base MP weapon set (63 files against pak0's 32), and
# everything below is a player-carried weapon with an authored viewmodel
# XModel — the vehicle and emplacement turrets are deliberately absent
# because they have no gunModel and reach the game through a map entity's
# weaponinfo instead (see import_cod_multiplayer_maps.mounted_weapon_fields).
#
# The semi-auto variants (bar_slow_mp, fg42_semi_mp, mp44_semi_mp,
# ppsh_semi_mp, thompson_semi_mp) are NOT listed: the closure already
# follows each record's alternateWeaponFile, which is how the shipped
# package grew its *__alternate records.
UO_WEAPON_FILES = (
    "bazooka_mp",
    "binoculars_artillery_mp",
    "binoculars_mp",
    "dp28_mp",
    "flamethrower_mp",
    "flashgrenade_mp",
    "gewehr43_mp",
    "mg30Cal_mp",
    "mg34_mp",
    "panzerschreck_mp",
    "satchelcharge_mp",
    "smokegrenade_mp",
    "sten_silenced_mp",
    "svt40_mp",
    "tt33_mp",
    "webley_mp",
    # The mounted emplacements. Not carried weapons and deliberately not in
    # the viewmodel roster — they have no gunModel and reach the game
    # through a misc_mg42's weaponinfo. They are staged because their
    # records are the only place the muzzle-flash effects are named, and the
    # presentation extractor discovers viewFlashEffect by walking every
    # staged weapons/mp record.
    "mg42_bipod_stand_mp",
    "mg42_bipod_duck_mp",
    "mg42_bipod_prone_mp",
)

# Exact XModels assembled by export_cod1_multiplayer_players.py.  Keeping this
# table beside the source policy avoids importing a Blender-only module merely
# to determine which retail files may be staged.
PLAYER_MODELS = (
    "playerbody_american_airborne",
    "basehead_01",
    "USAirborneHelmet",
    "gear_US_load1",
    "gear_US_bandolier",
    "gear_US_ammobelt",
    "playerbody_british_airborne",
    "basehead2_01",
    "helmet_british_airborne",
    "gear_british_air",
    "playerbody_russian_conscript",
    "basehead3_01",
    "sovietequipment_sidecap",
    "gear_russian_load_ocoat",
    "gear_russian_ppsh_ocoat",
    "playerbody_german_wehrmacht",
    "basehead_axis_a_01",
    "germanhelmet",
    "gear_german_load1_w",
    "gear_german_k98_w",
)

# CoD swaps the shared view-hands material by faction at runtime, so these
# images are a data dependency of the selected multiplayer factions even
# though they are not all named by the compiled hand XModel.
VIEW_HAND_SKINS = (
    "skins/hand.tga",
    "skins/viewhands@hand.dds",
    "skins/viewsleeves_new.tga",
    "skins/viewhands@vsleeve_whermact.tga",
    "skins/viewhands@vsleeve_British.tga",
    "skins/viewhands@vsleeve_russian.tga",
)

WEAPON_ANIMATION_FIELDS = frozenset(
    (
        "idleAnim",
        "emptyIdleAnim",
        "fireAnim",
        "holdFireAnim",
        "lastShotAnim",
        "rechamberAnim",
        "meleeAnim",
        "reloadAnim",
        "reloadEmptyAnim",
        "reloadStartAnim",
        "reloadEndAnim",
        "raiseAnim",
        "dropAnim",
        "altRaiseAnim",
        "altDropAnim",
        "adsFireAnim",
        "adsLastShotAnim",
        "adsRechamberAnim",
        "adsUpAnim",
        "adsDownAnim",
    )
)
WEAPON_MODEL_FIELDS = frozenset(
    (
        "gunModel",
        "handModel",
        "worldModel",
        "pickupModel",
        "projectileModel",
    )
)
# Entity classes whose "model" key names an XModel the map needs staged.
# Must stay in step with SCENE_PROP_CLASSES in import_cod_multiplayer_maps.py:
# this list decides what gets STAGED, that one decides what gets PLACED, and a
# class in the second but not the first places a model nothing exported.
MAP_PROP_CLASSES = frozenset((
    "misc_model",
    "script_model",
    "misc_mg42",
))

ENTITY_LUMP_INDEX = 29
SUPPORTED_BSP_VERSION = 59
HEALTH_PICKUP_MODEL = "health_medium"
MOUNTED_MG42_MODEL = "mg42_bipod"
MOUNTED_MG42_WEAPON_FILES = (
    "mg42_bipod_stand_mp",
    "mg42_bipod_prone_mp",
)


@dataclass(frozen=True)
class MountedWeaponAnimation:
    name: str
    source_weapon_file: str
    source_field: str
    stance: str
    phase: str
    vertical: str
    role: str
    looped: bool

    @property
    def frame_count(self) -> int:
        if self.phase == "aim":
            return 1
        if self.phase == "recover":
            return 10
        return 6

    @property
    def frame_rate(self) -> int:
        return 30


# The stand filenames really are misspelled ``foward`` in retail.  Recover is
# the authored continuation of each WEAPONFILE fireAnim; the prone level-fire
# variant is another compatible MP weapon-rig member retained for exact pitch
# presentation.  These seven tracks animate the nine-bone MG42 rig, never a
# player skeleton.
MOUNTED_MG42_ANIMATIONS = (
    MountedWeaponAnimation(
        "standMG42gun_aim_foward",
        "mg42_bipod_stand_mp",
        "idleAnim",
        "stand",
        "aim",
        "forward",
        "mounted:mg42/stand/aim",
        False,
    ),
    MountedWeaponAnimation(
        "standMG42gun_fire_foward",
        "mg42_bipod_stand_mp",
        "fireAnim",
        "stand",
        "fire",
        "forward",
        "mounted:mg42/stand/fire",
        True,
    ),
    MountedWeaponAnimation(
        "standMG42gun_recover_foward",
        "mg42_bipod_stand_mp",
        "derivedRecover",
        "stand",
        "recover",
        "forward",
        "mounted:mg42/stand/recover",
        False,
    ),
    MountedWeaponAnimation(
        "proneMG42gun_aim_forward_medium",
        "mg42_bipod_prone_mp",
        "idleAnim",
        "prone",
        "aim",
        "medium",
        "mounted:mg42/prone/aim_medium",
        False,
    ),
    MountedWeaponAnimation(
        "proneMG42gun_fire_forward_medium",
        "mg42_bipod_prone_mp",
        "fireAnim",
        "prone",
        "fire",
        "medium",
        "mounted:mg42/prone/fire_medium",
        True,
    ),
    MountedWeaponAnimation(
        "proneMG42gun_recover_forward_medium",
        "mg42_bipod_prone_mp",
        "derivedRecover",
        "prone",
        "recover",
        "medium",
        "mounted:mg42/prone/recover_medium",
        False,
    ),
    MountedWeaponAnimation(
        "proneMG42gun_fire_forward_level",
        "mg42_bipod_prone_mp",
        "retailAlternateFire",
        "prone",
        "fire",
        "level",
        "mounted:mg42/prone/fire_level",
        True,
    ),
)


class MultiplayerClosureError(RuntimeError):
    """The official MP dependency graph is malformed or incomplete."""


@dataclass(frozen=True)
class MultiplayerAssetClosure:
    paths: frozenset[str]
    selected_maps: tuple[str, ...] = ()
    weapon_files: tuple[str, ...] = ()
    player_animation_count: int = 0
    expanded_turret_aliases: tuple[str, ...] = ()
    excluded_turret_animations: tuple[str, ...] = ()
    mounted_weapon_files: tuple[str, ...] = ()
    mounted_weapon_animations: tuple[str, ...] = ()
    model_count: int = 0


class _Reader:
    def __init__(self, data: bytes, source: str):
        self.data = data
        self.source = source
        self.position = 0

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        if self.position + size > len(self.data):
            raise MultiplayerClosureError(
                f"{self.source}: truncated XModel v14 data"
            )
        values = struct.unpack_from("<" + fmt, self.data, self.position)
        self.position += size
        return values[0] if len(values) == 1 else values

    def skip(self, count: int) -> None:
        if count < 0 or self.position + count > len(self.data):
            raise MultiplayerClosureError(
                f"{self.source}: invalid XModel v14 offset"
            )
        self.position += count

    def cstring(self) -> str:
        try:
            end = self.data.index(0, self.position)
        except ValueError as error:
            raise MultiplayerClosureError(
                f"{self.source}: unterminated XModel string"
            ) from error
        value = self.data[self.position:end].decode("latin1")
        self.position = end + 1
        return value


def parse_xmodel_v14_dependencies(
    data: bytes,
    source: str = "<xmodel>",
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return ``(LOD asset, skin materials)`` from a compiled CoD1 XModel."""
    reader = _Reader(data, source)
    version = reader.take("H")
    if version != 14:
        raise MultiplayerClosureError(
            f"{source}: expected CoD XModel v14, got {version}"
        )
    reader.skip(24)
    lods: list[str] = []
    for _ in range(3):
        reader.take("f")
        name = reader.cstring().strip()
        if name:
            lods.append(name)
    reader.skip(4)
    padding_count = reader.take("I")
    for _ in range(padding_count):
        sub_padding_count = reader.take("I")
        reader.skip(sub_padding_count * 48 + 36)
    result: list[tuple[str, tuple[str, ...]]] = []
    for lod in lods:
        material_count = reader.take("H")
        materials = tuple(
            reader.cstring().strip()
            for _ in range(material_count)
        )
        result.append((lod, tuple(item for item in materials if item)))
    return tuple(result)


def _official_path(index, requested: str, *, required: bool = True) -> str | None:
    reference = index.get(requested)
    if reference is None:
        if required:
            raise MultiplayerClosureError(
                f"required multiplayer source member is missing: {requested}"
            )
        return None
    return _safe_member_name(reference.name)


def _read(index, requested: str, *, required: bool = True) -> tuple[str, bytes] | None:
    reference = index.get(requested)
    if reference is None:
        if required:
            raise MultiplayerClosureError(
                f"required multiplayer source member is missing: {requested}"
            )
        return None
    return (
        _safe_member_name(reference.name),
        index.read_ref(reference),
    )


def _safe_member_name(member: str) -> str:
    normalized = member.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        raise MultiplayerClosureError(
            f"unsafe official archive member path: {member!r}"
        )
    return normalized


def parse_weapon_file(data: bytes) -> dict[str, str]:
    fields = data.decode("latin1", "replace").split("\\")
    return dict(zip(fields[1::2], fields[2::2]))


def resolve_xmodel_closure(
    index,
    model_names: Iterable[str],
) -> frozenset[str]:
    """Expand selected XModels to every official compiled/model skin member."""
    paths: set[str] = set()
    normalized_models = {
        name.replace("\\", "/").removeprefix("xmodel/").strip()
        for name in model_names
        if name and name.strip()
    }
    for model_name in sorted(normalized_models, key=str.casefold):
        loaded = _read(index, f"xmodel/{model_name}")
        assert loaded is not None
        model_path, data = loaded
        paths.add(model_path)
        for lod_name, materials in parse_xmodel_v14_dependencies(
            data,
            model_path,
        ):
            # Rigid XModels legitimately omit XModelParts.  XModelSurfs are
            # mandatory: without them the importer can produce no geometry.
            part = _official_path(
                index,
                f"xmodelparts/{lod_name}",
                required=False,
            )
            if part is not None:
                paths.add(part)
            surface = _official_path(index, f"xmodelsurfs/{lod_name}")
            assert surface is not None
            paths.add(surface)
            for material in materials:
                skin = _official_path(
                    index,
                    f"skins/{material}",
                    required=False,
                )
                # Shader-only/no-draw slots can have no image member.  Every
                # actual image that the original importer can resolve is
                # admitted, while unrelated global skins remain excluded.
                if skin is not None:
                    paths.add(skin)
    return frozenset(paths)


def collect_weapon_source(
    index,
    *,
    include_uo: bool = False,
    weapon_files: Iterable[str] | None = None,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], tuple[str, ...]]:
    """Return WEAPONFILE, XAnim and XModel seeds for the selected MP roster."""
    primary = tuple(
        weapon_files
        if weapon_files is not None
        else BASE_WEAPON_FILES + (UO_WEAPON_FILES if include_uo else ())
    )
    source_paths: set[str] = set()
    animation_paths: set[str] = set()
    model_names: set[str] = {"shellcasing"}
    resolved_weapon_files: list[str] = []
    pending = list(primary)
    visited: set[str] = set()
    while pending:
        weapon_file = pending.pop(0).strip()
        key = weapon_file.casefold()
        if key in visited:
            continue
        if (
            "/" in weapon_file
            or "\\" in weapon_file
            or not key.endswith("_mp")
        ):
            raise MultiplayerClosureError(
                f"unsafe/non-MP WEAPONFILE dependency: {weapon_file}"
            )
        visited.add(key)
        loaded = _read(index, f"weapons/mp/{weapon_file}")
        assert loaded is not None
        source_path, data = loaded
        source_paths.add(source_path)
        resolved_weapon_files.append(PurePosixPath(source_path).name)
        values = parse_weapon_file(data)
        alternate = values.get("altWeapon", "").strip()
        if alternate and alternate.casefold() not in visited:
            pending.append(alternate)
        for field in WEAPON_ANIMATION_FIELDS:
            animation = values.get(field, "").strip()
            if not animation:
                continue
            path = _official_path(index, f"xanim/{animation}")
            assert path is not None
            animation_paths.add(path)
        for field in WEAPON_MODEL_FIELDS:
            model = values.get(field, "").strip().replace("\\", "/")
            if not model:
                continue
            model_names.add(model.removeprefix("xmodel/"))
    return (
        frozenset(source_paths),
        frozenset(animation_paths),
        frozenset(model_names),
        tuple(resolved_weapon_files),
    )


def collect_mounted_mg42_source(
    index,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return the exact MP WEAPONFILE/XAnim graph for ``misc_mg42`` visuals."""
    weapon_paths: set[str] = set()
    values_by_file: dict[str, dict[str, str]] = {}
    for weapon_file in MOUNTED_MG42_WEAPON_FILES:
        loaded = _read(index, f"weapons/mp/{weapon_file}")
        assert loaded is not None
        source_path, data = loaded
        weapon_paths.add(source_path)
        values_by_file[weapon_file] = parse_weapon_file(data)

    expected_fields = {
        ("mg42_bipod_stand_mp", "idleAnim"):
            "standMG42gun_aim_foward",
        ("mg42_bipod_stand_mp", "fireAnim"):
            "standMG42gun_fire_foward",
        ("mg42_bipod_prone_mp", "idleAnim"):
            "proneMG42gun_aim_forward_medium",
        ("mg42_bipod_prone_mp", "fireAnim"):
            "proneMG42gun_fire_forward_medium",
    }
    for (weapon_file, field), expected in expected_fields.items():
        actual = values_by_file[weapon_file].get(field, "")
        if actual.casefold() != expected.casefold():
            raise MultiplayerClosureError(
                f"weapons/mp/{weapon_file}: {field} is {actual!r}, "
                f"expected official MP member {expected!r}"
            )

    animations: set[str] = set()
    for animation in MOUNTED_MG42_ANIMATIONS:
        path = _official_path(index, f"xanim/{animation.name}")
        assert path is not None
        animations.add(path)
    return frozenset(weapon_paths), frozenset(animations)


def collect_player_animation_source(
    index,
) -> tuple[
    str,
    frozenset[str],
    tuple[str, ...],
    tuple[str, ...],
]:
    loaded = _read(index, "mp/playeranim.script")
    assert loaded is not None
    script_path, raw = loaded
    references = parse_active_references(
        raw.decode("utf-8-sig", errors="replace")
    )
    available_names = {
        PurePosixPath(reference.name).name.casefold():
            PurePosixPath(reference.name).name
        for key, reference in index.members.items()
        if key.startswith("xanim/") and "/" not in key.removeprefix("xanim/")
    }
    animations: set[str] = set()
    expanded_aliases: list[str] = []
    excluded: list[str] = []
    for reference in references:
        if reference.turret_animation:
            try:
                expanded = expand_turret_reference(
                    reference,
                    available_names,
                )
            except FileNotFoundError as error:
                raise MultiplayerClosureError(str(error)) from error
            if expanded is None:
                excluded.append(reference.name)
                continue
            expanded_aliases.append(reference.name)
            for member in expanded:
                path = _official_path(index, f"xanim/{member.name}")
                assert path is not None
                animations.add(path)
            continue
        # Same refusal build_inventory makes, from the same constants: the
        # four jeep passenger clips are the last vehicle animations the
        # script binds directly, and the three torso clips they pair with are
        # bound on those same jeep lines and nowhere else. Staging either
        # would put XAnims in the source closure that no exported player
        # carries — and package.py checks this count against
        # PLAYER_ANIMATION_COUNT, so the two lists have to agree.
        if is_excluded_vehicle_animation(reference.name):
            continue
        path = _official_path(index, f"xanim/{reference.name}")
        assert path is not None
        animations.add(path)
    return (
        script_path,
        frozenset(animations),
        tuple(expanded_aliases),
        tuple(excluded),
    )


def build_weapon_player_closure(
    index,
    *,
    include_uo: bool = False,
) -> MultiplayerAssetClosure:
    weapon_paths, weapon_animations, weapon_models, weapon_files = (
        collect_weapon_source(index, include_uo=include_uo)
    )
    (
        player_script,
        player_animations,
        expanded_aliases,
        excluded,
    ) = collect_player_animation_source(index)
    model_names = set(weapon_models)
    model_names.update(PLAYER_MODELS)
    paths = set(weapon_paths)
    paths.update(weapon_animations)
    paths.update(player_animations)
    paths.add(player_script)
    for skin in VIEW_HAND_SKINS:
        path = _official_path(index, skin)
        assert path is not None
        paths.add(path)
    paths.update(resolve_xmodel_closure(index, model_names))
    return MultiplayerAssetClosure(
        paths=frozenset(paths),
        weapon_files=weapon_files,
        player_animation_count=len(player_animations),
        expanded_turret_aliases=expanded_aliases,
        excluded_turret_animations=excluded,
        model_count=len(model_names),
    )


def _bsp_lump(data: bytes, lump_index: int, source: str) -> bytes:
    header_size = 8 + (lump_index + 1) * 8
    if len(data) < header_size or data[:4] != b"IBSP":
        raise MultiplayerClosureError(f"{source}: not a CoD IBSP")
    version = struct.unpack_from("<i", data, 4)[0]
    if version != SUPPORTED_BSP_VERSION:
        raise MultiplayerClosureError(
            f"{source}: expected CoD IBSP v{SUPPORTED_BSP_VERSION}, got {version}"
        )
    length, offset = struct.unpack_from("<ii", data, 8 + lump_index * 8)
    if length < 0 or offset < 0 or offset + length > len(data):
        raise MultiplayerClosureError(
            f"{source}: invalid BSP lump {lump_index}"
        )
    return data[offset : offset + length]


def bsp_entities(data: bytes, source: str) -> tuple[dict[str, str], ...]:
    text = _bsp_lump(data, ENTITY_LUMP_INDEX, source).decode(
        "latin1",
        "replace",
    ).rstrip("\0")
    entities = tuple(
        dict(re.findall(r'"([^"]*)"\s+"([^"]*)"', body))
        for body in re.findall(r"\{([^}]*)\}", text, re.DOTALL)
    )
    if not entities or entities[0].get("classname") != "worldspawn":
        raise MultiplayerClosureError(
            f"{source}: BSP entity lump has no worldspawn"
        )
    return entities


def selected_multiplayer_maps(
    index,
    requested_maps: Iterable[str] | None = None,
) -> tuple[tuple[str, object], ...]:
    available = {
        PurePosixPath(reference.name).stem.casefold(): reference
        for key, reference in index.members.items()
        if key.startswith("maps/mp/") and key.endswith(".bsp")
    }
    requested = (
        {item.casefold() for item in requested_maps if item.strip()}
        if requested_maps is not None
        else set(available)
    )
    missing = sorted(requested - set(available))
    if missing:
        raise MultiplayerClosureError(
            "unknown official multiplayer map(s): " + ", ".join(missing)
        )
    if not requested:
        raise MultiplayerClosureError("no official multiplayer maps selected")
    return tuple((map_id, available[map_id]) for map_id in sorted(requested))


def collect_map_model_names(
    index,
    requested_maps: Iterable[str] | None = None,
) -> tuple[frozenset[str], tuple[str, ...]]:
    models: dict[str, str] = {}
    map_filter = (
        tuple(requested_maps)
        if requested_maps is not None
        else None
    )

    def add_model(model: str) -> None:
        models.setdefault(model.casefold(), model)

    selected: list[str] = []
    # item_health is spawned by the stock DM/TDM game scripts rather than
    # serialized into a BSP. Every official map selected by this exporter
    # supports at least one of those modes; the per-map manifest performs the
    # exact dm/tdm check before advertising the model at runtime.
    add_model(HEALTH_PICKUP_MODEL)
    # Global gametype/map helper scripts create models outside the BSP
    # placement loop. Analyze shared MP scripts plus only the selected map GSC
    # files, and close every resolvable loadFx model emitter without admitting
    # excluded-map or campaign assets.
    script_content = analyze_multiplayer_gsc(
        index,
        selected_maps=map_filter,
    )
    for model in script_content.model_names:
        add_model(model)
    for effect_index, effect_path in enumerate(
        script_content.effect_paths
    ):
        if index.get(effect_path) is None:
            continue
        effect = build_effect_closure(
            index,
            f"mp_gsc_{effect_index}",
            effect_path,
        )
        for model in effect.model_names:
            add_model(model)
    for map_id, reference in selected_multiplayer_maps(
        index,
        map_filter,
    ):
        selected.append(map_id)
        entities = bsp_entities(
            index.read_ref(reference),
            reference.name,
        )
        for entity in entities:
            damage_model = entity.get(
                "script_damagemodel",
                "",
            ).replace("\\", "/")
            if damage_model:
                add_model(
                    damage_model[len("xmodel/") :]
                    if damage_model.casefold().startswith("xmodel/")
                    else damage_model
                )
            if entity.get("classname", "").casefold() not in MAP_PROP_CLASSES:
                continue
            model = entity.get("model", "").replace("\\", "/")
            if (
                not model.casefold().startswith("xmodel/")
                or model.casefold() == "xmodel/fx"
            ):
                continue
            add_model(model.removeprefix("xmodel/"))

        script_ref = index.get(f"maps/mp/{map_id}.gsc")
        script = (
            index.read_ref(script_ref).decode("latin1", "replace")
            if script_ref is not None
            else None
        )

        # EFX model emitters are reachable only through script_exploder.
        # Include precisely those XModels so the prop exporter can
        # materialize every authored multiplayer presentation dependency.
        for effect in map_exploder_effects(
            index,
            map_id,
            entities,
            script,
        ):
            for model in effect.model_names:
                add_model(model)
    return frozenset(models.values()), tuple(selected)


def build_map_model_closure(
    index,
    requested_maps: Iterable[str] | None = None,
) -> MultiplayerAssetClosure:
    models, selected = collect_map_model_names(index, requested_maps)
    mounted_weapon_paths: frozenset[str] = frozenset()
    mounted_animation_paths: frozenset[str] = frozenset()
    if any(
        model.casefold() == MOUNTED_MG42_MODEL
        for model in models
    ):
        mounted_weapon_paths, mounted_animation_paths = (
            collect_mounted_mg42_source(index)
        )
    paths = set(resolve_xmodel_closure(index, models))
    paths.update(mounted_weapon_paths)
    paths.update(mounted_animation_paths)
    return MultiplayerAssetClosure(
        paths=frozenset(paths),
        selected_maps=selected,
        mounted_weapon_files=tuple(MOUNTED_MG42_WEAPON_FILES)
        if mounted_weapon_paths
        else (),
        mounted_weapon_animations=tuple(
            animation.name
            for animation in MOUNTED_MG42_ANIMATIONS
        )
        if mounted_animation_paths
        else (),
        model_count=len(models),
    )


def is_managed_source_path(member: str) -> bool:
    key = member.replace("\\", "/").casefold()
    return (
        key.startswith(MANAGED_ASSET_ROOTS)
        or key.startswith(MANAGED_WEAPON_ROOTS)
        or key in MANAGED_EXACT_FILES
    )
