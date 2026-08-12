#!/usr/bin/env python3
"""Extract CoD1/UO ordnance content: staging, explosion FX, and audio.

Four idempotent jobs against the original PK3 archives:
  1. Staging — re-copy the five grenade/panzerfaust WEAPONFILEs from the
     selected OFFICIAL archive layers (Main, then UO when selected) into
     <staging>/weapons/mp, replacing any copies a mod pak had overridden,
     and stage the UO-only M18 smoke grenade members
     (xmodel/xmodelparts/xmodelsurfs/xanim/skins + weapons/mp/smokegrenade_mp)
     when uo/*.pk3 archives are present.
  2. FX sprites — convert the Main grenade1-3.efx sprite set (plus the UO
     smoke plume sprites when present) to PNG under fx/textures/; existing
     files are kept.
  3. EFX definitions — copy the grenade/rocket/smoke .efx sets to fx/efx/.
  4. Audio — extract the ordnance WAVs and merge their aliases into
     audio/presentation.json. This tool is the single owner of the ordnance
     aliases (grenade bounce/explode, rocket explode, pin/throw, UO smoke);
     extract_cod1_weapon_presentation.py owns the per-weapon fire/reload
     aliases and every existing presentation.json entry is preserved as-is.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

from cod1_archive_policy import (
    COD1_TIER,
    UO_TIER,
    archive_record,
    archive_relative_path,
    official_archives,
    sha256_bytes,
)

# Original grenade/panzerfaust WEAPONFILEs; mod paks (e.g. w_canka_awe_v2.pk3)
# override these in a merged staging tree, so restage from the selected
# official layers. UO legitimately overrides these same paths and therefore
# must win when that tier is selected.
OFFICIAL_WEAPON_FILES = (
    "weapons/mp/fraggrenade_mp",
    "weapons/mp/mk1britishfrag_mp",
    "weapons/mp/rgd-33russianfrag_mp",
    "weapons/mp/stielhandgranate_mp",
    "weapons/mp/panzerfaust_mp",
)

# UO-only M18 smoke grenade members staged into cod1_source for the
# viewmodel/worldmodel/projectile exporters.
UO_STAGING_PREFIXES = (
    "xmodel/w_us_grn_m18_",
    "xmodelparts/w_us_grn_m18_",
    "xmodelsurfs/w_us_grn_m18_",
    "xanim/w_us_grn_m18_view_",
    "skins/viewmodel@w_us_grn_m18",
)
UO_STAGING_FILES = ("weapons/mp/smokegrenade_mp",)

# Sprites referenced by Main fx/explosions/grenade1-3.efx (+snow/water);
# explosion1b/medium_smoke/cloudflash1a already ship via the presentation step.
MAIN_FX_SPRITES = (
    "gfx/effects/explosion1.tga",
    "gfx/effects/explosion/smokeplume1.tga",
    "gfx/effects/pjsmoke.tga",
    "gfx/effects/whitesmoke.tga",
    "gfx/impact/cratered.tga",
    "gfx/effects/flash1.jpg",
    "gfx/effects/groundflash1.jpg",
    "gfx/effects/fire/firecore.tga",
    "gfx/effects/mist.tga",
    "gfx/effects/snowpuff1.tga",
    "gfx/effects/waterspay.tga",
)

# UO smoke grenade plume sprites (fx/smoke/smoke_grenade.efx and the sg_* set
# resolve to these six through fxshaders/smoke*.shader).
UO_FX_SPRITES = (
    "gfx/efx_assets/smoke/smk_p_top_wht_a.dds",
    "gfx/efx_assets/smoke/smk_p_top_wht_b.dds",
    "gfx/efx_assets/smoke/smk_p_top_wht_c.dds",
    "gfx/efx_assets/smoke/smk_p_none_wht_a.dds",
    "gfx/efx_assets/smoke/smk_p_none_wht_b.dds",
    "gfx/efx_assets/smoke/smk_p_none_wht_c.dds",
)

MAIN_EFX = (
    "fx/explosions/grenade1.efx",
    "fx/explosions/grenade2.efx",
    "fx/explosions/grenade3.efx",
    "fx/explosions/grenade_snow.efx",
    "fx/explosions/grenade_water.efx",
    "fx/explosions/fireball2_gren.efx",
    "fx/smoke/smoke_panzerfaust.efx",
    "fx/smoke/emitter_panzerfaust.efx",
)

UO_EFX = (
    "fx/smoke/smoke_grenade.efx",
    "fx/smoke/smoke_grenade_orange.efx",
    "fx/smoke/smoke_trail_panzerfaust.efx",
    "fx/weapon/explosions/_global_grenade.efx",
    "fx/weapon/explosions/_global_rocket.efx",
    "fx/weapon/explosions/grenade_dirt.efx",
    "fx/weapon/explosions/grenade_generic.efx",
    "fx/weapon/explosions/rocket_dirt.efx",
    "fx/weapon/explosions/rocket_generic.efx",
    "fx/weapon/explosions/sg_center_volume.efx",
    "fx/weapon/explosions/sg_mist_volume.efx",
    "fx/weapon/explosions/sg_smoke_linger.efx",
    "fx/weapon/explosions/sg_smoke_linger_b.efx",
    "fx/weapon/explosions/sg_smoke_shockwave.efx",
    "fx/weapon/explosions/sg_smoke_volume_initial.efx",
    "fx/weapon/impacts/grenade_bounce_generic.efx",
)

# Aliases this tool merges into presentation.json (added only when missing;
# the smokegrenade_* rows only exist in the UO soundalias CSVs).
ORDNANCE_ALIASES = (
    "grenade_bounce_default",
    "grenade_explode_default",
    "rocket_explode_default",
    "weap_fraggrenade_fire",
    "weap_fraggrenade_pin",
    "weap_rusgrenade_fire",
    "weap_germgrenade_fire",
    "weap_panzerfaust_fire",
    "smokegrenade_explode",
    "smokegrenade_smoke",
)


def canonical_sound_file(member: str) -> str:
    """Return the lowercase packaged path for an active ``sound/`` member."""
    normalized = member.replace("\\", "/")
    if not normalized.casefold().startswith("sound/"):
        raise ValueError(f"sound member is outside sound/: {member}")
    relative = PurePosixPath(normalized[len("sound/"):])
    if (
        relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise ValueError(f"unsafe sound member path: {member}")
    return relative.as_posix().casefold()


def canonical_output_path(root: Path, relative: str) -> Path:
    """Resolve an output path while case-renaming existing APFS entries.

    A direct write to ``audio/explosions`` does not rename a pre-existing
    ``audio/Explosions`` directory on a case-insensitive filesystem. Walk
    each component and use a two-step rename when only casing differs.
    """
    parsed = PurePosixPath(relative)
    if (
        parsed.is_absolute()
        or any(part in ("", ".", "..") for part in parsed.parts)
    ):
        raise ValueError(f"unsafe output path: {relative}")
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for index, part in enumerate(parsed.parts):
        matches = [
            child
            for child in current.iterdir()
            if child.name.casefold() == part.casefold()
        ]
        exact = next(
            (child for child in matches if child.name == part),
            None,
        )
        if exact is not None:
            child = exact
        elif len(matches) == 1:
            existing = matches[0]
            temporary = current / f".{existing.name}.fod-case-rename"
            suffix = 0
            while temporary.exists():
                suffix += 1
                temporary = current / (
                    f".{existing.name}.fod-case-rename-{suffix}"
                )
            existing.rename(temporary)
            child = current / part
            try:
                temporary.rename(child)
            except Exception:
                temporary.rename(existing)
                raise
        elif matches:
            raise RuntimeError(
                f"case-colliding output entries for {current / part}"
            )
        else:
            child = current / part
            if index + 1 < len(parsed.parts):
                child.mkdir()
        current = child
    return current


class MemberIndex:
    """Case-insensitive member -> (archive, member) map with the engine's
    later-archive-overrides ordering."""

    def __init__(self, archives: list[Path]):
        self.archives = archives
        self.members: dict[str, tuple[Path, str]] = {}
        for archive_path in archives:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.namelist():
                    if not member.endswith("/"):
                        self.members[member.casefold()] = (
                            archive_path,
                            member,
                        )

    def read(self, member: str) -> bytes | None:
        source = self.source(member)
        if source is None:
            return None
        archive_path, original = source
        with zipfile.ZipFile(archive_path) as archive:
            return archive.read(original)

    def source(self, member: str) -> tuple[Path, str] | None:
        return self.members.get(member.casefold())

    def matching(self, prefixes: tuple[str, ...]) -> list[str]:
        return sorted(
            member
            for member in self.members
            if member.startswith(
                tuple(prefix.casefold() for prefix in prefixes)
            )
        )


def number(row: list[str], index: int, default: float) -> float:
    if index >= len(row) or not row[index]:
        return default
    try:
        return float(row[index])
    except ValueError:
        return default


def convert_to_png(data: bytes, destination: Path) -> None:
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA")
        image.save(destination, format="PNG")


def read_sprite(index: MemberIndex, member: str) -> bytes | None:
    """Read a sprite, falling back across the .tga/.dds/.jpg variants CoD
    ships interchangeably."""
    stem = member.rsplit(".", 1)[0]
    for candidate in (member, f"{stem}.tga", f"{stem}.dds", f"{stem}.jpg"):
        data = index.read(candidate)
        if data is not None:
            return data
    return None


def sprite_source(
    index: MemberIndex,
    member: str,
) -> tuple[Path, str] | None:
    """Resolve the same explicit engine image-extension alternatives used
    by read_sprite, retaining the active layered archive/member."""
    stem = member.rsplit(".", 1)[0]
    for candidate in (
        member,
        f"{stem}.tga",
        f"{stem}.dds",
        f"{stem}.jpg",
    ):
        source = index.source(candidate)
        if source is not None:
            return source
    return None


def stage_official_weapon_files(index: MemberIndex, staging: Path) -> int:
    staged = 0
    for member in OFFICIAL_WEAPON_FILES:
        data = index.read(member)
        if data is None:
            print(f"WARNING missing official WEAPONFILE: {member}")
            continue
        destination = staging / member
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_bytes() != data:
            destination.write_bytes(data)
            staged += 1
    return staged


def stage_uo_smoke_grenade(index: MemberIndex, staging: Path) -> int:
    members = index.matching(UO_STAGING_PREFIXES)
    members += [
        member for member in UO_STAGING_FILES
        if index.members.get(member.lower()) is not None
    ]
    staged = 0
    for member in members:
        archive_path, original = index.members[member.casefold()]
        destination = staging / original
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = index.read(member)
        if not destination.is_file() or destination.read_bytes() != data:
            destination.write_bytes(data)
            staged += 1
    return staged


def extract_sprites(index: MemberIndex, sprites: tuple[str, ...],
                    texture_dir: Path) -> tuple[int, list[str]]:
    converted = 0
    extracted: list[str] = []
    for member in sprites:
        destination = texture_dir / (Path(member).stem + ".png")
        if destination.is_file():
            extracted.append(member)
            continue
        data = read_sprite(index, member)
        if data is None:
            print(f"WARNING missing FX sprite: {member}")
            continue
        convert_to_png(data, destination)
        extracted.append(member)
        converted += 1
    return converted, extracted


def extract_efx(index: MemberIndex, efx: tuple[str, ...],
                efx_dir: Path) -> tuple[int, list[str]]:
    copied = 0
    extracted: list[str] = []
    for member in efx:
        data = index.read(member)
        if data is None:
            print(f"WARNING missing efx: {member}")
            continue
        destination = efx_dir / Path(member).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        extracted.append(member)
        copied += 1
    return copied, extracted


def read_alias_rows(index: MemberIndex) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = defaultdict(list)
    for member in index.matching(("soundaliases/",)):
        if not member.endswith(".csv"):
            continue
        text = index.read(member).decode("utf-8-sig", errors="replace")
        for row in csv.reader(io.StringIO(text)):
            if len(row) < 3 or not row[0] or row[0].startswith("#"):
                continue
            sound_file = row[2].strip()
            if not sound_file or sound_file.lower() == "null.wav":
                continue
            source = index.source(
                "sound/" + sound_file.replace("\\", "/")
            )
            if source is None:
                continue
            active_member = source[1].replace("\\", "/")
            if not active_member.casefold().startswith("sound/"):
                continue
            sound_file = canonical_sound_file(active_member)
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
            if entry not in rows[alias]:
                rows[alias].append(entry)
    return rows


def merge_presentation(pak_root: Path, index: MemberIndex,
                       presentation_files: list[str],
                       archives: list[Path]) -> tuple[int, int]:
    """Add the missing ordnance aliases (and their WAVs) to
    audio/presentation.json, leaving every existing entry untouched."""
    manifest_path = pak_root / "audio" / "presentation.json"
    manifest = {"aliases": [], "presentation_files": []}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    aliases = manifest.setdefault("aliases", [])
    existing = {entry.get("alias") for entry in aliases}
    alias_rows = read_alias_rows(index)

    added_aliases = 0
    extracted_audio = 0
    for alias in ORDNANCE_ALIASES:
        if alias in existing:
            continue
        rows = alias_rows.get(alias, [])
        if not rows:
            print(f"WARNING no soundalias rows for {alias}")
            continue
        variants = []
        for row in rows:
            variants.append(dict(row))
        aliases.append({"alias": alias, "variants": variants})
        added_aliases += 1

    # Normalize both newly added rows and manifests produced by older
    # exporters. The exact archive spelling remains in provenance only.
    for entry in aliases:
        if not isinstance(entry, dict):
            continue
        for variant in entry.get("variants", []):
            if (
                not isinstance(variant, dict)
                or not isinstance(variant.get("file"), str)
                or not variant["file"]
            ):
                continue
            sound_file = canonical_sound_file(
                "sound/" + variant["file"]
            )
            variant["file"] = sound_file
            sound_member = "sound/" + sound_file
            source = index.source(sound_member)
            data = index.read(sound_member)
            if source is None or data is None:
                print(
                    f"WARNING missing sound for {entry.get('alias', '?')}: "
                    f"{sound_member}"
                )
                continue
            destination = canonical_output_path(
                pak_root,
                "audio/" + sound_file,
            )
            existed = destination.is_file()
            if not existed or destination.read_bytes() != data:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                if not existed:
                    extracted_audio += 1

    aliases.sort(key=lambda entry: entry.get("alias", ""))
    known_files = manifest.setdefault("presentation_files", [])
    for member in presentation_files:
        if member not in known_files:
            known_files.append(member)
    known_archives = {
        entry.get("path"): entry
        for entry in manifest.setdefault("sourceArchives", [])
        if isinstance(entry, dict)
    }
    for archive in archives:
        record = archive_record(archive)
        known_archives[record["path"]] = record
    manifest["sourceArchives"] = [
        known_archives[key] for key in sorted(known_archives)
    ]

    # Preserve exact per-member provenance for everything this merge can
    # reach, including aliases created by the earlier presentation step.
    # Localized archives are admitted at archive level, but this list proves
    # which individual MP-reachable members actually crossed the boundary.
    provenance_by_path = {
        entry.get("path", "").casefold(): entry
        for entry in manifest.setdefault("provenance", [])
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and entry.get("path")
    }

    def remember(
        output_path: str,
        source: tuple[Path, str],
    ) -> None:
        archive_path, member = source
        data = index.read(member)
        if data is None:
            raise RuntimeError(
                f"active presentation member disappeared: {member}"
            )
        provenance_by_path[output_path.casefold()] = {
            "path": output_path,
            "archive": archive_relative_path(archive_path),
            "member": member,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
        }

    for entry in aliases:
        if not isinstance(entry, dict):
            continue
        for variant in entry.get("variants", []):
            if (
                not isinstance(variant, dict)
                or not isinstance(variant.get("file"), str)
                or not variant["file"]
            ):
                continue
            sound_file = variant["file"].replace("\\", "/")
            source = index.source("sound/" + sound_file)
            if source is not None:
                remember("audio/" + sound_file, source)

    image_extensions = (".tga", ".dds", ".jpg", ".jpeg", ".png")
    for requested in known_files:
        if not isinstance(requested, str) or not requested:
            continue
        if requested.casefold().endswith(image_extensions):
            source = sprite_source(index, requested)
            output_path = f"fx/textures/{Path(requested).stem}.png"
        else:
            source = index.source(requested)
            output_path = f"fx/efx/{Path(requested).name}"
        if source is not None:
            remember(output_path, source)
    manifest["provenance"] = [
        provenance_by_path[key]
        for key in sorted(provenance_by_path)
    ]

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n",
                             encoding="utf-8")
    return added_aliases, extracted_audio


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("game_root", type=Path,
                        help="Call of Duty install (Main/*.pk3 [+ uo/*.pk3])")
    parser.add_argument("--pak-root", type=Path, required=True,
                        help="fodpak content root (fx/, audio/)")
    parser.add_argument("--staging", type=Path,
                        help="cod1_source staging tree; enables restaging the "
                             "official WEAPONFILEs and the UO smoke grenade")
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--staging-only",
        action="store_true",
        help="only refresh official source staging prerequisites",
    )
    args = parser.parse_args()
    # United Offensive is mandatory; the flag cannot turn it off.
    args.include_uo = True

    game_root = args.game_root.resolve()
    pak_root = args.pak_root.resolve()
    staging = args.staging.resolve() if args.staging else None

    main_archives = official_archives(game_root, COD1_TIER)
    uo_archives = (
        official_archives(game_root, UO_TIER)
        if args.include_uo else []
    )
    main_index = MemberIndex(main_archives)
    uo_index = MemberIndex(uo_archives) if uo_archives else None
    selected_index = MemberIndex(main_archives + uo_archives)

    staged = 0
    if staging is not None:
        staged += stage_official_weapon_files(selected_index, staging)
        if uo_index is not None:
            staged += stage_uo_smoke_grenade(uo_index, staging)
    if args.staging_only:
        if staging is None:
            raise SystemExit("--staging-only requires --staging")
        print(
            f"Ordnance staging ready: refreshed {staged} file(s) — "
            f"UO {'selected' if uo_index else 'not selected'}."
        )
        return

    texture_dir = pak_root / "fx" / "textures"
    efx_dir = pak_root / "fx" / "efx"
    presentation_files: list[str] = []
    converted, extracted = extract_sprites(main_index, MAIN_FX_SPRITES,
                                           texture_dir)
    presentation_files += extracted
    copied, extracted = extract_efx(main_index, MAIN_EFX, efx_dir)
    presentation_files += extracted
    if uo_index is not None:
        uo_converted, extracted = extract_sprites(uo_index, UO_FX_SPRITES,
                                                  texture_dir)
        converted += uo_converted
        presentation_files += extracted
        uo_copied, extracted = extract_efx(uo_index, UO_EFX, efx_dir)
        copied += uo_copied
        presentation_files += extracted

    # Merge aliases from Main + UO rows; UO-only aliases need the UO CSVs.
    combined_index = MemberIndex(main_archives + uo_archives)
    added, wavs = merge_presentation(
        pak_root,
        combined_index,
        presentation_files,
        main_archives + uo_archives,
    )
    print(
        f"Ordnance: staged {staged} source file(s), converted {converted} FX "
        f"sprite(s), copied {copied} efx, added {added} alias(es) "
        f"(+{wavs} wav(s)) — UO {'present' if uo_index else 'absent'}."
    )


if __name__ == "__main__":
    main()
