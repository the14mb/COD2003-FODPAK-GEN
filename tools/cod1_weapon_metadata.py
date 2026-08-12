#!/usr/bin/env python3
"""Pure-Python WEAPONFILE animation-role and melee metadata helpers."""

from __future__ import annotations

from pathlib import Path


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
    primary_path = weapon_root / primary_weapon_file
    if not primary_path.is_file():
        raise FileNotFoundError(primary_path)
    primary = parse_weapon_file(primary_path)

    variants: list[tuple[str, str, dict[str, str]]] = [
        ("primary", primary_weapon_file, primary)
    ]
    alternate_name = _safe_variant_name(primary.get("altWeapon", ""))
    if alternate_name and alternate_name.casefold() != primary_weapon_file.casefold():
        alternate_path = weapon_root / alternate_name
        if not alternate_path.is_file():
            raise FileNotFoundError(
                f"{primary_weapon_file}: alternate WEAPONFILE missing: "
                f"{alternate_path}"
            )
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
            if not (xanim_root / animation).is_file():
                missing.append(
                    f"{weapon_file}:{field}={animation}"
                )
                continue
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
