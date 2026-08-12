#!/usr/bin/env python3
"""Parse the active animation bindings in CoD1 ``mp/playeranim.script``.

The script can place more than one ``legs``/``torso`` binding on one line and
contains both ``//`` and ``/* ... */`` comments. A line-start regex therefore
silently drops valid multiplayer actions; this parser scans every binding and
deduplicates identifiers case-insensitively, matching the game filesystem.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


LAYERS = frozenset(("both", "legs", "torso", "turret"))
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# ``turretanim`` bindings in the retail script are aliases, not XAnim member
# names.  The engine expands them over the authored mounted-player families.
# These are deliberately exact: nearby ``*MG42gun_*`` files animate the weapon
# rig, ``pb_*MG42gunner_*`` are a second player-body family not selected by the
# MP script, and the three novelty standing idles are campaign presentation.
MG42_HORIZONTAL = {
    "stand": (
        "forward",
        "15left",
        "15right",
        "30left",
        "30right",
        "45left",
        "45right",
    ),
    "prone": (
        "forward",
        "20left",
        "20right",
        "40left",
        "40right",
    ),
}
MG42_VERTICAL = ("15down", "level", "15up")
MG42_ALIAS_PHASES = {
    "standmg42_aim": ("aim",),
    "standmg42_fire": ("fire", "recover"),
    "pronemg42_aim": ("aim",),
    "pronemg42_fire": ("fire", "recover"),
}

# The ``turretanim`` aliases this exporter expands. Order is load-bearing:
# the package validators compare it as an ordered list, not a set, and the
# order is the retail script's own declaration order.
SCRIPT_TURRET_ALIASES = (
    "standMG42_aim",
    "standMG42_fire",
    "proneMG42_fire",
    "proneMG42_aim",
)

# The script also binds United Offensive's jeep driver/gunner turret
# families. Friends of Duty ships no vehicle, so they are deliberately not
# expanded: the exporter records them here and the package validators demand
# exactly this set, which is what keeps "cut from the product" distinguishable
# from "went missing from the install". Order is the script's, as above.
EXCLUDED_TURRET_ALIASES = (
    "vehicleJeepDriverIdle",
    "vehicleJeepDriverDriving",
    "vehicleJeepGunnerFire",
    "vehicleJeepGunnerAim",
)

# Direct (non-alias) bindings the retail script makes for a jeep passenger.
# They are the last vehicle animations in the active inventory and nothing in
# this product can ever play them, so they leave with the vehicles.
EXCLUDED_VEHICLE_ANIMATION_PREFIX = "c_mp_jeep_"

# The torso halves of those same jeep-passenger lines. This list used to be
# absent on the stated grounds that "the torso clips they pair with are shared
# with ordinary on-foot states and stay" — which is not true of these three.
# Every binding they have in the script is a jeep line:
#
#   STATE COMBAT > idle > mounted vehPassenger1, vehicle jeep, weaponclass
#       pistol, weapon_position ads
#           legs c_mp_jeep_passenger_idle torso pt_stand_ads_pistol
#   ... , weaponclass pistol
#           legs c_mp_jeep_passenger_idle torso pt_stand_alert_pistol
#   ...
#           legs c_mp_jeep_passenger_idle torso c_mp_torso_stand_alert
#
# so they are unreachable once the vehicles are cut, and the only action role
# they could inherit is the enclosing movement:combat/idle — which would make
# three clips no player can select claim to be the standing combat idle, and
# sort them to the front of it. The on-foot standing idles are the separate
# pb_stand_alert / pb_stand_ads_pistol / pb_stand_alert_pistol family, which
# is bound under `weapons`/`weaponclass` guards and is untouched by this.
EXCLUDED_VEHICLE_TORSO_ANIMATIONS = frozenset(
    (
        "pt_stand_ads_pistol",
        "pt_stand_alert_pistol",
        "c_mp_torso_stand_alert",
    )
)
_EXCLUDED_VEHICLE_TORSO_KEYS = frozenset(
    name.casefold() for name in EXCLUDED_VEHICLE_TORSO_ANIMATIONS
)


def is_excluded_vehicle_animation(name: str) -> bool:
    """Whether an animation leaves with the vehicles this product cut.

    The exported player inventory and the source-staging closure must answer
    this identically — exporter/package.py compares the two counts against
    each other — so the rule lives here once rather than being restated at
    each call site, which is how they drifted apart before.
    """
    key = name.casefold()
    return (
        key.startswith(EXCLUDED_VEHICLE_ANIMATION_PREFIX)
        or key in _EXCLUDED_VEHICLE_TORSO_KEYS
    )

# The six mounted MG42 roles, which are the only turret roles a player can
# reach in this product.
TURRET_ACTION_ROLES = tuple(
    f"turret:{stance}mg42_{phase}"
    for stance in ("stand", "prone")
    for phase in ("aim", "fire", "recover")
)

# Scalars, not tier-keyed dicts: the base-only figures (269 / 165 / 108)
# describe a package that can no longer be produced.
PLAYER_ANIMATION_COUNT = 351
PLAYER_ACTIVE_IDENTIFIER_COUNT = 258
TURRET_EXPANSION_COUNT = 108


@dataclass
class PlayerAnimationReference:
    name: str
    layers: set[str] = field(default_factory=set)
    action_roles: set[str] = field(default_factory=set)
    occurrences: int = 0
    turret_animation: bool = False
    source_alias: str = ""
    turret_weapon: str = ""
    turret_stance: str = ""
    turret_phase: str = ""
    turret_horizontal: str = ""
    turret_vertical: str = ""


@dataclass(frozen=True)
class PlayerAnimationInventory:
    script_path: Path
    script_sha256: str
    active_identifier_count: int
    animations: tuple[PlayerAnimationReference, ...]
    expanded_turret_aliases: tuple[str, ...]
    excluded_turret_animations: tuple[str, ...]

    @property
    def expanded_turret_animation_count(self) -> int:
        return sum(bool(reference.source_alias) for reference in self.animations)


def _without_comments(text: str) -> str:
    # Preserve newline count for diagnostics while blanking block comments.
    text = BLOCK_COMMENT.sub(
        lambda match: "\n" * match.group(0).count("\n"),
        text,
    )
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


def _action_role(section: str, blocks: list[str]) -> str | None:
    if section == "events" and blocks:
        return f"event:{blocks[0].casefold()}"
    if section != "animations":
        return None
    for index, block in enumerate(blocks):
        if block.casefold().startswith("state ") and index + 1 < len(blocks):
            state = block.split(None, 1)[1].casefold()
            movement = blocks[index + 1].casefold()
            return f"movement:{state}/{movement}"
    return None


def parse_active_references(text: str) -> tuple[PlayerAnimationReference, ...]:
    ordered: dict[str, PlayerAnimationReference] = {}
    section = ""
    blocks: list[str] = []
    pending_block = ""
    for line in _without_comments(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        marker = stripped.casefold()
        if marker in ("animations", "events"):
            section = marker
            blocks.clear()
            pending_block = ""
            continue
        if stripped == "{":
            blocks.append(pending_block)
            pending_block = ""
            continue
        if stripped == "}":
            if blocks:
                blocks.pop()
            pending_block = ""
            continue

        tokens = stripped.split()
        if not tokens:
            continue
        binding_indexes = [
            index
            for index, token in enumerate(tokens[:-1])
            if token.casefold() in LAYERS
        ]
        action_role = _action_role(section, blocks)
        for binding_position, index in enumerate(binding_indexes):
            layer = tokens[index].casefold()
            name = tokens[index + 1]
            key = name.casefold()
            reference = ordered.get(key)
            if reference is None:
                reference = PlayerAnimationReference(name=name)
                ordered[key] = reference
            reference.layers.add(layer)
            if action_role:
                reference.action_roles.add(action_role)
            reference.occurrences += 1
            next_binding = (
                binding_indexes[binding_position + 1]
                if binding_position + 1 < len(binding_indexes)
                else len(tokens)
            )
            if any(
                value.casefold() == "turretanim"
                for value in tokens[index + 2 : next_binding]
            ):
                reference.turret_animation = True
        if not binding_indexes:
            pending_block = stripped
    return tuple(ordered.values())


def _expanded_reference(
    alias: PlayerAnimationReference,
    name: str,
    *,
    weapon: str,
    stance: str,
    phase: str,
    role: str,
    horizontal: str = "",
    vertical: str = "",
) -> PlayerAnimationReference:
    return PlayerAnimationReference(
        name=name,
        layers=set(alias.layers),
        # The alias keeps its ``movement:`` bindings; the members it expands
        # to must not inherit them.
        #
        # In the retail script a turretanim alias is always bound behind a
        # guard — ``mounted mg42 { both standMG42_aim turretanim }`` sits
        # inside ``STATE COMBAT { idle { ... } }`` alongside
        # ``weapons none { both pb_stand_alert }``. The guard is what makes
        # the binding true: standMG42_aim is the combat idle *while manning
        # an MG42*, not the combat idle. Copying ``movement:combat/idle``
        # onto all 21 concrete yaw/pitch members claimed each of them was a
        # general standing idle, and because the expansion runs first they
        # also sorted to the FRONT of that role — so resolving the role
        # without a name preference returned standMG42gunner_aim_forward_15down:
        # a full-body gunner pose, hands on spade grips, head up, handed to a
        # player standing in the open.
        #
        # A member is selected by aim direction, never by movement state, so
        # the ``turret:`` role is its whole truth. The alias-level movement
        # bindings stay recorded on the alias itself, which is where the
        # script put them.
        action_roles={
            *(
                inherited
                for inherited in alias.action_roles
                if not inherited.startswith("movement:")
            ),
            role,
        },
        occurrences=alias.occurrences,
        source_alias=alias.name,
        turret_weapon=weapon,
        turret_stance=stance,
        turret_phase=phase,
        turret_horizontal=horizontal,
        turret_vertical=vertical,
    )


def expand_turret_reference(
    reference: PlayerAnimationReference,
    available_names: Mapping[str, str],
) -> tuple[PlayerAnimationReference, ...] | None:
    """Resolve one active ``turretanim`` alias to official MP XAnim members.

    ``available_names`` maps case-folded basenames to the actual archive or
    staged filename. Returning ``None`` means that the alias is not one this
    product ships — the jeep families in EXCLUDED_TURRET_ALIASES, or anything
    a mod added. A recognized family is all-or-nothing so a damaged or
    partially staged install cannot silently ship incomplete directional
    coverage.
    """
    alias_key = reference.name.casefold()
    requested: list[
        tuple[str, str, str, str, str, str, str]
    ] = []
    mg42_phases = MG42_ALIAS_PHASES.get(alias_key)
    if mg42_phases is None:
        return None
    stance = "stand" if alias_key.startswith("stand") else "prone"
    for phase in mg42_phases:
        role = f"turret:{stance}mg42_{phase}"
        for horizontal in MG42_HORIZONTAL[stance]:
            for vertical in MG42_VERTICAL:
                requested.append(
                    (
                        f"{stance}MG42gunner_{phase}_"
                        f"{horizontal}_{vertical}",
                        "mg42",
                        stance,
                        phase,
                        role,
                        horizontal,
                        vertical,
                    )
                )

    missing = [
        name
        for name, *_metadata in requested
        if name.casefold() not in available_names
    ]
    if missing:
        raise FileNotFoundError(
            f"multiplayer turret alias '{reference.name}' is missing "
            f"{len(missing)} official XAnim member(s): "
            + ", ".join(missing)
        )
    return tuple(
        _expanded_reference(
            reference,
            available_names[name.casefold()],
            weapon=weapon,
            stance=stance,
            phase=phase,
            role=role,
            horizontal=horizontal,
            vertical=vertical,
        )
        for (
            name,
            weapon,
            stance,
            phase,
            role,
            horizontal,
            vertical,
        ) in requested
    )


def build_inventory(
    script_path: Path,
    xanim_root: Path,
) -> PlayerAnimationInventory:
    raw = script_path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    references = parse_active_references(text)
    files = {
        path.name.casefold(): path
        for path in xanim_root.iterdir()
        if path.is_file()
    }
    animations: list[PlayerAnimationReference] = []
    expanded_aliases: list[str] = []
    excluded: list[str] = []
    missing: list[str] = []
    for reference in references:
        if reference.turret_animation:
            expanded = expand_turret_reference(
                reference,
                {
                    key: path.name
                    for key, path in files.items()
                },
            )
            if expanded is None:
                excluded.append(reference.name)
                continue
            expanded_aliases.append(reference.name)
            animations.extend(expanded)
            continue
        if is_excluded_vehicle_animation(reference.name):
            continue
        source = files.get(reference.name.casefold())
        if source is None:
            missing.append(reference.name)
            continue
        # Use the actual staged filename so case-sensitive hosts open the same
        # source the game's case-insensitive lookup selected.
        reference.name = source.name
        animations.append(reference)
    if missing:
        raise FileNotFoundError(
            "active multiplayer player animations missing from official "
            f"staging ({len(missing)}): {', '.join(sorted(missing))}"
        )
    return PlayerAnimationInventory(
        script_path=script_path,
        script_sha256=hashlib.sha256(raw).hexdigest(),
        active_identifier_count=len(references),
        animations=tuple(animations),
        expanded_turret_aliases=tuple(expanded_aliases),
        excluded_turret_animations=tuple(excluded),
    )
