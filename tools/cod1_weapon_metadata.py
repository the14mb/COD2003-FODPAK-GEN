#!/usr/bin/env python3
"""Pure-Python WEAPONFILE animation-role and melee metadata helpers."""

from __future__ import annotations

import functools
from pathlib import Path


@functools.lru_cache(maxsize=16)
def _dir_index(root: Path) -> dict[str, str]:
    """Case-folded name -> actual on-disk name, for one directory.

    Cached because it is consulted once per animation field per weapon, and
    the xanim directory alone holds ~688 entries.
    """
    try:
        return {
            entry.name.casefold(): entry.name
            for entry in root.iterdir()
            if entry.is_file()
        }
    except OSError:
        return {}


def resolve_file(root: Path, name: str) -> str | None:
    """The on-disk spelling of a file, or None when it truly is absent.

    Call of Duty's own data disagrees with itself about case: the luger's
    WEAPONFILE asks for `viewmodel_luger_ads_up` and the pk3 stores
    `viewmodel_luger_ADS_up`. That went unnoticed for twenty years because
    Windows and macOS filesystems are case-insensitive, so an exact-case
    lookup finds the file anyway. Linux is case-sensitive, so on a Steam Deck
    the viewmodels step aborted with "active viewmodel XAnim source(s)
    missing" for a file sitting right there.

    The exact spelling is tried first, so on a case-insensitive filesystem
    this returns the WEAPONFILE's own spelling exactly as before and the
    emitted metadata is unchanged. Only a case-sensitive filesystem, where
    the alternative is failing outright, sees the on-disk name instead --
    which is also the name every consumer needs, since these strings are used
    directly as paths under xanim/.
    """
    if (root / name).is_file():
        return name
    return _dir_index(root).get(name.casefold())


# Ordered exactly as the source fields should be evaluated. The role names are
# stable package API; ``field`` retains the original WEAPONFILE authority.
ANIMATION_FIELD_ROLES = (
    ("idleAnim", "idle"),
    ("emptyIdleAnim", "empty_idle"),
    ("fireAnim", "fire"),
    ("holdFireAnim", "hold_fire"),
    ("lastShotAnim", "last_fire"),
    ("rechamberAnim", "rechamber"),
    ("meleeAnim", "melee"),
    ("reloadAnim", "reload"),
    ("reloadEmptyAnim", "reload_empty"),
    ("reloadStartAnim", "reload_start"),
    ("reloadEndAnim", "reload_end"),
    ("raiseAnim", "raise"),
    ("dropAnim", "drop"),
    ("altRaiseAnim", "alt_raise"),
    ("altDropAnim", "alt_drop"),
    ("adsFireAnim", "ads_fire"),
    ("adsLastShotAnim", "ads_last_fire"),
    ("adsRechamberAnim", "ads_rechamber"),
    ("adsUpAnim", "ads_up"),
    ("adsDownAnim", "ads_down"),
)


def parse_weapon_file(path: Path) -> dict[str, str]:
    fields = path.read_text(errors="replace").split("\\")
    return dict(zip(fields[1::2], fields[2::2]))


def number(values: dict[str, str], key: str, default: float = 0.0) -> float:
    value = values.get(key, "")
    return float(value) if value else default


def _safe_variant_name(value: str) -> str | None:
    name = value.strip()
    if not name:
        return None
    if "/" in name or "\\" in name or not name.casefold().endswith("_mp"):
        raise ValueError(f"unsafe/non-MP alternate WEAPONFILE reference: {name}")
    return name


def weapon_animation_metadata(
    source_root: Path,
    primary_weapon_file: str,
) -> dict[str, object]:
    """Return primary+alternate animation roles and exact melee values.

    CoD1's ``altWeapon`` records (BAR slow fire and semi-auto variants) can
    introduce a unique animation even though the inventory owns only the
    primary weapon. Those roles are unioned into the primary package entry.
    """
    weapon_root = source_root / "weapons" / "mp"
    xanim_root = source_root / "xanim"
    # Resolved for the same reason as the XAnims below: these names come out
    # of CoD's own data, which is not consistent about case.
    primary_on_disk = resolve_file(weapon_root, primary_weapon_file)
    if primary_on_disk is None:
        raise FileNotFoundError(weapon_root / primary_weapon_file)
    primary_path = weapon_root / primary_on_disk
    primary = parse_weapon_file(primary_path)

    variants: list[tuple[str, str, dict[str, str]]] = [
        ("primary", primary_weapon_file, primary)
    ]
    alternate_name = _safe_variant_name(primary.get("altWeapon", ""))
    if alternate_name and alternate_name.casefold() != primary_weapon_file.casefold():
        alternate_on_disk = resolve_file(weapon_root, alternate_name)
        if alternate_on_disk is None:
            raise FileNotFoundError(
                f"{primary_weapon_file}: alternate WEAPONFILE missing: "
                f"{weapon_root / alternate_name}"
            )
        alternate_path = weapon_root / alternate_on_disk
        variants.append(
            ("alternate", alternate_name, parse_weapon_file(alternate_path))
        )

    roles: list[dict[str, str]] = []
    animations: list[str] = []
    missing: list[str] = []
    for variant, weapon_file, values in variants:
        for field, role in ANIMATION_FIELD_ROLES:
            animation = values.get(field, "").strip()
            if not animation:
                continue
            resolved = resolve_file(xanim_root, animation)
            if resolved is None:
                missing.append(
                    f"{weapon_file}:{field}={animation}"
                )
                continue
            # Everything downstream treats this as a path under xanim/, so it
            # has to be the name the filesystem will actually open.
            animation = resolved
            if animation not in animations:
                animations.append(animation)
            record = {
                "role": role,
                "field": field,
                "sourceAnimation": animation,
                "weaponFile": weapon_file,
                "variant": variant,
            }
            if record not in roles:
                roles.append(record)
    if missing:
        raise FileNotFoundError(
            f"{primary_weapon_file}: active viewmodel XAnim source(s) missing: "
            + ", ".join(missing)
        )

    melee_animation = primary.get("meleeAnim", "").strip()
    if not melee_animation:
        raise ValueError(f"{primary_weapon_file}: meleeAnim is missing")
    missing_melee = [
        key for key in ("meleeDamage", "meleeDelay", "meleeTime")
        if not primary.get(key, "").strip()
    ]
    if missing_melee:
        raise ValueError(
            f"{primary_weapon_file}: missing melee field(s): "
            + ", ".join(missing_melee)
        )

    return {
        "values": primary,
        "animations": tuple(animations),
        "animationRoles": roles,
        "alternateWeaponFile": alternate_name or "",
        "meleeDamage": number(primary, "meleeDamage"),
        "meleeDelay": number(primary, "meleeDelay"),
        "meleeTime": number(primary, "meleeTime"),
    }
