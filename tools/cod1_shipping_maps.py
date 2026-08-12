"""Authoritative multiplayer map roster for Friends of Duty exports.

Friends of Duty intentionally supports a small rotation from the official
retail multiplayer data.  Keeping the roster in one dependency-light module
prevents the map importer, map-model closure and script presentation closure
from silently drifting back to "every BSP in the mounted archives".

The ``game`` field is the runtime/catalog namespace.  The ``source_tier``
field identifies the official archive tier which owns the BSP.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable


BASE_SHIPPING_MAP_IDS = (
    "mp_carentan",
    "mp_chateau",
    "mp_pavlov",
    "mp_railyard",
    "mp_rocket",
)

UO_SHIPPING_MAP_IDS = (
    "mp_arnhem",
    "mp_cassino",
)

SHIPPING_MAP_SPECS = (
    ("uo", "uo", "mp_arnhem"),
    ("uo", "uo", "mp_cassino"),
    ("cod1", "cod1", "mp_carentan"),
    ("cod1", "cod1", "mp_pavlov"),
    ("cod1", "cod1", "mp_chateau"),
    ("cod1", "cod1", "mp_railyard"),
    ("cod1", "cod1", "mp_rocket"),
)

ALL_SHIPPING_MAP_IDS = frozenset(
    (*BASE_SHIPPING_MAP_IDS, *UO_SHIPPING_MAP_IDS)
)

# Retail ``maps/mp`` scripts belonging to systems this product cut. They are
# not roster-filtered — every map mounts them — so they have to be named.
#
# The gametype scripts are the seven modes Friends of Duty does not ship;
# leaving them in the closure is what pulled the S&D bomb, the HQ radios, the
# CTF flags and the Base Assault bunkers into props/required_models.json.
# The four ``*_gmi`` helpers are United Offensive's drivable jeep and tank,
# whose damage-swap XModels arrive the same way. Deathmatch and Team
# Deathmatch (dm.gsc, tdm.gsc) and every shared helper stay.
EXCLUDED_MULTIPLAYER_SCRIPTS = frozenset(
    (
        "gametypes/bas.gsc",
        "gametypes/bel.gsc",
        "gametypes/ctf.gsc",
        "gametypes/dom.gsc",
        "gametypes/hq.gsc",
        "gametypes/re.gsc",
        "gametypes/sd.gsc",
        "_jeepdrive_gmi.gsc",
        "_jeepeffects_gmi.gsc",
        "_tankdrive_gmi.gsc",
        "_tankeffects_gmi.gsc",
        # A shared United Offensive helper whose ONLY content sink is the
        # Base Assault bunker table (it otherwise builds HUD elements, which
        # a Unity runtime never reads): excluding it removes exactly one
        # model, mp_bunker_foy_dmg, and no alias, effect or shader.
        "_util_mp_gmi.gsc",
    )
)


def shipping_map_specs(
    *,
    include_uo: bool,
) -> tuple[tuple[str, str, str], ...]:
    """Return the allowed roster for the selected official content tier."""
    return tuple(
        spec
        for spec in SHIPPING_MAP_SPECS
        if include_uo or spec[0] == "cod1"
    )


def normalize_requested_maps(
    values: Iterable[str] | None,
) -> frozenset[str] | None:
    """Normalize comma/space-compatible map arguments.

    ``None`` and an empty iterable select the complete shipping roster.
    Unknown ids are rejected here, before any archive data is extracted.
    """
    if values is None:
        return None
    names = frozenset(
        name.strip().casefold()
        for value in values
        for name in str(value).split(",")
        if name.strip()
    )
    if not names:
        return None
    unknown = sorted(names - ALL_SHIPPING_MAP_IDS)
    if unknown:
        raise ValueError(
            "map(s) are not in the Friends of Duty shipping rotation: "
            + ", ".join(unknown)
        )
    return names


def selected_shipping_map_ids(
    *,
    include_uo: bool,
    requested_maps: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Resolve an optional subset against the selected retail tier."""
    requested = normalize_requested_maps(requested_maps)
    available = frozenset(
        map_id
        for _game, _source_tier, map_id in shipping_map_specs(
            include_uo=include_uo
        )
    )
    if requested is None:
        selected = available
    else:
        unavailable = sorted(requested - available)
        if unavailable:
            raise ValueError(
                "United Offensive is required for map(s): "
                + ", ".join(unavailable)
            )
        selected = requested
    return tuple(sorted(selected))


def is_selected_multiplayer_script(
    path: str,
    selected_maps: Iterable[str],
) -> bool:
    """Whether a retail ``maps/mp`` GSC belongs to the shipping product.

    Shared helpers and the Deathmatch/Team Deathmatch gametype scripts remain
    in the analysis graph.  Top-level map scripts (``mp_<name>.gsc`` and
    ``mp_<name>_fx.gsc``) are roster-filtered, and the scripts owned by a cut
    system are refused outright (EXCLUDED_MULTIPLAYER_SCRIPTS).  This
    preserves required MP-only shared behavior while excluding presentation
    assets owned solely by an unshipped map or an unshipped mode.
    """
    normalized = path.strip().replace("\\", "/")
    parsed = PurePosixPath(normalized)
    parts = tuple(part.casefold() for part in parsed.parts)
    if (
        len(parts) < 3
        or parts[0] != "maps"
        or parts[1] != "mp"
        or parsed.suffix.casefold() != ".gsc"
    ):
        return False
    relative_parts = parts[2:]
    if "/".join(relative_parts) in EXCLUDED_MULTIPLAYER_SCRIPTS:
        return False
    if len(relative_parts) != 1:
        return True
    stem = PurePosixPath(relative_parts[0]).stem
    if not stem.startswith("mp_"):
        return True
    map_id = stem.removesuffix("_fx")
    selected = {item.strip().casefold() for item in selected_maps}
    return map_id in selected
