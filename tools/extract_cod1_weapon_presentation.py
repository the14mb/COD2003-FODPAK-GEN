#!/usr/bin/env python3
"""Extract CoD1 multiplayer audio and weapon presentation assets.

Legacy mode (positional OUTPUT) keeps the historical Unity staging layout
(Audio/, SourceFx/, Textures/, manifest.json with baked Assets/ paths).
Pak mode (--pak-root) targets the fodpak content layout instead: WAVs under
audio/<original relpath>, audio/presentation.json (alias variants keep file/
volume/pitch, no 'asset' field), fx textures converted to PNG under
fx/textures/, and .efx definitions under fx/efx/.

This tool owns the per-weapon SOUND_FIELDS aliases plus the small set of
global aliases called directly by stock multiplayer scripts or by the
engine itself (melee swing, out-of-ammo click). The ordnance
aliases (grenade bounce/explode, rocket explode, pin/throw, UO smoke) are
owned by extract_cod1_ordnance.py, which runs later in the pipeline and
merges them into audio/presentation.json without touching the aliases written
here.
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
    LayeredArchiveIndex,
    UO_TIER,
    archive_record,
    archive_relative_path,
    official_archives,
    sha256_bytes,
)
from cod1_mp_gsc_content import analyze_multiplayer_gsc
from cod1_script_exploder import build_effect_closure
from cod1_shipping_maps import selected_shipping_map_ids
from extract_cod1_ordnance import (
    MAIN_EFX as ORDNANCE_MAIN_EFX,
    MAIN_FX_SPRITES as ORDNANCE_MAIN_FX_SPRITES,
    UO_EFX as ORDNANCE_UO_EFX,
    UO_FX_SPRITES as ORDNANCE_UO_FX_SPRITES,
    canonical_output_path,
    canonical_sound_file,
)

SOUND_FIELDS = (
    "pickupSound",
    "fireSound",
    "lastShotSound",
    "rechamberSound",
    "reloadSound",
    "reloadEmptySound",
    "reloadStartSound",
    "reloadEndSound",
    "altSwitchSound",
)

# maps/MP/_minefields.gsc calls the first two aliases directly. The stock
# DM/TDM dropHealth queue calls health_pickup_medium when item_health is
# collected. The melee swing pair and the out-of-ammo click are called by
# the engine itself (cgame melee swing, empty trigger pull) — reachable
# from no script and no WEAPONFILE field — so they are pinned here.
GLOBAL_MULTIPLAYER_SOUND_ALIASES = (
    "minefield_click",
    "explo_mine",
    "health_pickup_medium",
    "melee_swing_small",
    "melee_swing_large",
    "player_out_of_ammo",
    # Victim-side flesh hit reports. Engine-called like the swing pair:
    # iw_sound.csv ships loadspec all_mp rows for both (dist 180..1400)
    # and the compiled caliber split sends pistols small, everything
    # else large. No WEAPONFILE field names them, so they must be
    # pinned globally.
    "bullet_small_flesh",
    "bullet_large_flesh",
    # UO barrel overheat. Engine-played on EV_OVERHEATING (uo_cgamex86.dll
    # carries the alias name next to that event) rather than named by any
    # WEAPONFILE key, so like the pair above it has to be pinned globally.
    # generic_mp_sound.csv authors one variant: volume 1.5, pitch
    # 0.95-1.05, distance 200..600 inches.
    "mg_overheat",
    # The match-result announcer. Stock DM and TDM never played these --
    # only the round-based modes did, and every one of those scripts is out
    # of this product's closure -- but Friends of Duty's Team Deathmatch
    # announces its winner (AuthoritativeMatchServer.PublishGameModeResult
    # Audio), so the three lines are pinned here instead of being reached
    # through a gametype script that no longer ships.
    "mp_announcer_allies_win",
    "mp_announcer_axis_win",
    "mp_announcer_round_draw",
)

# extract_cod1_ordnance.py merges these aliases and their provenance into this
# tool's manifest later in the pipeline. They are a separate output owner and
# must survive presentation-only stale-file pruning.
ORDNANCE_MANAGED_ALIASES = frozenset((
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
))
ORDNANCE_MANAGED_MEMBERS = frozenset(
    member.casefold()
    for member in (
        *ORDNANCE_MAIN_EFX,
        *ORDNANCE_MAIN_FX_SPRITES,
        *ORDNANCE_UO_EFX,
        *ORDNANCE_UO_FX_SPRITES,
    )
)

PRESENTATION_BASE_FILES = (
    # The bullet tracer. scripts/sfx.shader defines gfx/misc/tracer as a
    # two-sided additive stage over textures/sfx/tracer.tga; no .tga ships,
    # and the engine falls back across image extensions to the .jpg. UO's
    # 64x64 copy (a horizontally elongated comet) overrides CoD1's 16x16
    # dot, and layering CoD1-then-UO is what picks it. Retail draws tracers
    # from EV_BULLET_TRACER with this material directly, so no .efx is
    # involved.
    "textures/sfx/tracer.jpg",
    "gfx/effects/muzflash2.tga",
    "gfx/effects/muzflash2a.tga",
    "gfx/effects/medium_smoke.tga",
    "gfx/effects/cloudflash1a.jpg",
    "gfx/effects/explosion1b.tga",
    "fx/shellejects/pistol.efx",
    "fx/shellejects/rifle.efx",
    "fx/shellejects/shelleject_pistolsound.efx",
    "fx/shellejects/shelleject_riflesound.efx",
    "fx/muzzleflashes/standardflashviewmp.efx",
    "fx/muzzleflashes/german_mg_viewmp.efx",
    "fx/muzzleflashes/heavy_viewmp.efx",
    "fx/muzzleflashes/russian_mg_viewmp.efx",
    "fx/muzzleflashes/thompson_viewmp.efx",
)


def weapon_values(path: Path) -> dict[str, str]:
    fields = path.read_text().split("\\")
    return dict(zip(fields[1::2], fields[2::2]))


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


def _alias_output_paths(
    manifest: dict[str, object],
    *,
    aliases: frozenset[str],
    pak_mode: bool,
) -> set[str]:
    paths: set[str] = set()
    entries = manifest.get("aliases", [])
    if not isinstance(entries, list):
        return paths
    prefix = "audio" if pak_mode else "Audio"
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or str(entry.get("alias", "")).casefold() not in aliases
            or not isinstance(entry.get("variants"), list)
        ):
            continue
        for variant in entry["variants"]:
            relative = (
                variant.get("file")
                if isinstance(variant, dict)
                else None
            )
            if isinstance(relative, str) and relative:
                paths.add(
                    PurePosixPath(
                        prefix,
                        relative.replace("\\", "/"),
                    ).as_posix()
                )
    return paths


def _prior_presentation_owned_paths(
    manifest: dict[str, object] | None,
    *,
    pak_mode: bool,
) -> set[str]:
    if not isinstance(manifest, dict):
        return set()
    explicit = manifest.get("presentationOwnedPaths")
    if isinstance(explicit, list):
        return {
            item
            for item in explicit
            if isinstance(item, str) and item
        }
    # Migration from manifests written before ownership was explicit. This
    # manifest is shared only with the ordnance merger; subtract its paths so
    # impacts, footsteps and unrelated audio owners are never considered.
    ordnance_paths = _alias_output_paths(
        manifest,
        aliases=ORDNANCE_MANAGED_ALIASES,
        pak_mode=pak_mode,
    )
    ordnance_path_keys = {path.casefold() for path in ordnance_paths}
    provenance = manifest.get("provenance", [])
    return {
        str(entry["path"])
        for entry in provenance
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and entry["path"].casefold() not in ordnance_path_keys
            and str(entry.get("member", "")).replace(
                "\\",
                "/",
            ).casefold() not in ORDNANCE_MANAGED_MEMBERS
        )
    }


def _prune_owned_paths(
    output_root: Path,
    stale_paths: set[str],
) -> int:
    removed = 0
    touched_parents: set[Path] = set()
    for relative in sorted(stale_paths):
        normalized = PurePosixPath(relative.replace("\\", "/"))
        if (
            normalized.is_absolute()
            or any(part in ("", ".", "..") for part in normalized.parts)
        ):
            raise ValueError(
                f"unsafe prior presentation output path: {relative!r}"
            )
        destination = output_root.joinpath(*normalized.parts)
        if destination.is_file():
            destination.unlink()
            touched_parents.add(destination.parent)
            removed += 1
    for directory in sorted(touched_parents, reverse=True):
        current = directory
        while current != output_root and current.is_dir():
            if any(current.iterdir()):
                break
            current.rmdir()
            current = current.parent
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pk3_root", type=Path)
    parser.add_argument("cod1_source", type=Path)
    parser.add_argument("output", type=Path, nargs="?",
                        help="legacy Unity staging output (Audio/, SourceFx/, Textures/)")
    parser.add_argument("--pak-root", type=Path,
                        help="fodpak content root: audio/ tree + audio/presentation.json, "
                        "fx textures converted to PNG in fx/textures/, efx in fx/efx/")
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--maps",
        nargs="*",
        help="optional subset of the Friends of Duty shipping rotation",
    )
    args = parser.parse_args()
    # United Offensive is mandatory; the flag cannot turn it off.
    args.include_uo = True
    if (args.output is None) == (args.pak_root is None):
        parser.error("expected exactly one of OUTPUT or --pak-root")
    pk3_root = args.pk3_root.resolve()
    game_root = pk3_root.parent if pk3_root.name.casefold() == "main" else pk3_root
    source_root = args.cod1_source.resolve()
    pak_root = args.pak_root.resolve() if args.pak_root else None
    output_root = pak_root if pak_root else args.output.resolve()
    manifest_path = (
        pak_root / "audio" / "presentation.json"
        if pak_root
        else output_root / "manifest.json"
    )
    try:
        prior_manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        prior_manifest = None
    prior_owned_paths = _prior_presentation_owned_paths(
        prior_manifest,
        pak_mode=pak_root is not None,
    )
    archives = official_archives(game_root, COD1_TIER)
    if args.include_uo:
        archives += official_archives(game_root, UO_TIER)
    selected_maps = selected_shipping_map_ids(
        include_uo=args.include_uo,
        requested_maps=args.maps,
    )

    members: dict[str, tuple[Path, str]] = {}
    alias_rows: dict[str, list[dict]] = defaultdict(list)
    for archive_path in archives:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.namelist():
                members[member.casefold()] = (archive_path, member)
            for member in archive.namelist():
                if (
                    not member.casefold().startswith("soundaliases/")
                    or not member.casefold().endswith(".csv")
                ):
                    continue
                text = archive.read(member).decode("utf-8-sig", errors="replace")
                for row in csv.reader(io.StringIO(text)):
                    if len(row) < 3 or not row[0] or row[0].startswith("#"):
                        continue
                    sound_file = row[2].strip()
                    if not sound_file or sound_file.lower() == "null.wav":
                        continue
                    volume_min = number(row, 3, 1.0)
                    pitch_min = number(row, 5, 1.0)
                    entry = {
                        "file": sound_file.replace("\\", "/"),
                        "volume_min": volume_min,
                        "volume_max": number(row, 4, volume_min),
                        "pitch_min": pitch_min,
                        "pitch_max": number(row, 6, pitch_min),
                        # Preserve the original spatial fields in CoD inches.
                        # A blank dist_max is deliberately zero: the retail
                        # alias format says that means "start at any distance".
                        "distance_min_inches": number(row, 7, 120.0),
                        "distance_max_inches": number(row, 8, 0.0),
                    }
                    alias_key = row[0].strip().casefold()
                    if entry not in alias_rows[alias_key]:
                        alias_rows[alias_key].append(entry)

    required_aliases = {
        "shell_eject_pistol",
        "shell_eject_rifle",
        *GLOBAL_MULTIPLAYER_SOUND_ALIASES,
    }
    # Exact selected maps/mp GSC reachability, including shared team command
    # wrappers and game[...] indirection. Campaign and unshipped map scripts
    # are outside the analyzer boundary.
    mp_script_content = analyze_multiplayer_gsc(
        LayeredArchiveIndex(archives),
        selected_maps=selected_maps,
    )
    required_aliases.update(mp_script_content.sound_aliases)
    mp_index = LayeredArchiveIndex(archives)
    for effect_index, effect_path in enumerate(
        mp_script_content.effect_paths
    ):
        # One active UO registration names an EFX that is absent from the
        # retail archives. It remains recorded by the MP content manifest;
        # never substitute a similarly named file here.
        if mp_index.get(effect_path) is None:
            continue
        effect = build_effect_closure(
            mp_index,
            f"mp_gsc_{effect_index}",
            effect_path,
        )
        required_aliases.update(effect.sound_aliases)
    required_presentation = set(PRESENTATION_BASE_FILES)
    for weapon_file in (source_root / "weapons/mp").iterdir():
        if not weapon_file.is_file():
            continue
        values = weapon_values(weapon_file)
        for field in SOUND_FIELDS:
            alias = values.get(field, "").strip().lower()
            if alias:
                required_aliases.add(alias)
        for field in ("viewFlashEffect", "shellEjectEffect"):
            effect = values.get(field, "").strip()
            if effect and effect.lower() != "none":
                required_presentation.add(effect)

    audio_prefix = "audio" if pak_root else "Audio"
    extracted_audio: set[str] = set()
    provenance: dict[str, dict[str, object]] = {}
    manifest_aliases = []
    for alias in sorted(required_aliases, key=str.casefold):
        files = []
        # GSC preserves the authored spelling (for example MP_bomb_plant)
        # while soundalias CSV lookup is case-insensitive in the engine.
        # The archive table is keyed casefolded, so never let stock mixed
        # case turn a reachable multiplayer alias into a false omission.
        for row in alias_rows.get(alias.casefold(), []):
            member_key = ("sound/" + row["file"]).casefold()
            source = members.get(member_key)
            if source is None:
                print(f"WARNING missing sound for {alias}: {member_key}")
                continue
            archive_path, member = source
            normalized_member = member.replace("\\", "/")
            if not normalized_member.casefold().startswith("sound/"):
                raise RuntimeError(
                    f"soundalias resolved outside sound/: {member}"
                )
            # Package a single lowercase spelling on every platform. Keep
            # the active retail member's exact spelling in provenance.
            canonical_file = canonical_sound_file(normalized_member)
            destination = canonical_output_path(
                output_root,
                f"{audio_prefix}/{canonical_file}",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination_key = str(destination).casefold()
            if destination_key not in extracted_audio:
                with zipfile.ZipFile(archive_path) as archive:
                    data = archive.read(member)
                destination.write_bytes(data)
                extracted_audio.add(destination_key)
                relative = destination.relative_to(output_root).as_posix()
                provenance[relative] = {
                    "archive": archive_relative_path(archive_path),
                    "member": member,
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
            if pak_root:
                files.append({**row, "file": canonical_file})
            else:
                files.append({
                    **row,
                    "file": canonical_file,
                    "asset":
                        "Assets/Presentation/CoD1/Audio/" + canonical_file,
                })
        # CoD deliberately assigns some weapon fields to aliases backed by
        # `null.wav` (and a few UO definitions reference aliases that are not
        # shipped at all).  Those fields mean "no sound"; emitting an empty
        # alias would turn that intentional silence into a corrupt manifest.
        if files:
            manifest_aliases.append(
                {"alias": alias.casefold(), "variants": files}
            )
        else:
            print(f"INFO silent/unresolved weapon alias omitted: {alias}")

    if pak_root:
        source_fx_output = pak_root / "fx" / "efx"
        texture_output = pak_root / "fx" / "textures"
    else:
        source_fx_output = output_root / "SourceFx"
        texture_output = output_root / "Textures"
    extracted_presentation = []
    for requested in sorted(required_presentation):
        source = members.get(requested.lower())
        if source is None:
            print(f"WARNING missing presentation asset: {requested}")
            continue
        archive_path, member = source
        is_texture = requested.lower().endswith((".tga", ".dds", ".jpg", ".jpeg", ".png"))
        destination_root = texture_output if is_texture else source_fx_output
        with zipfile.ZipFile(archive_path) as archive:
            data = archive.read(member)
        if pak_root and is_texture:
            destination = destination_root / (Path(requested).stem + ".png")
            convert_to_png(data, destination)
        else:
            destination = destination_root / Path(requested).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        relative = destination.relative_to(output_root).as_posix()
        provenance[relative] = {
            "archive": archive_relative_path(archive_path),
            "member": member,
            "bytes": len(data),
            "sha256": sha256_bytes(data),
        }
        extracted_presentation.append(requested)

    output_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "selectedMaps": list(selected_maps),
        "aliases": manifest_aliases,
        "presentation_files": extracted_presentation,
        "sourceArchives": [archive_record(path) for path in archives],
        "provenance": [
            {"path": path, **record}
            for path, record in sorted(provenance.items())
        ],
        "presentationOwnedPaths": sorted(provenance),
    }
    current_owned = {path.casefold() for path in provenance}
    removed = _prune_owned_paths(
        output_root,
        {
            path
            for path in prior_owned_paths
            if path.casefold() not in current_owned
        },
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        f"Extracted {len(extracted_audio)} multiplayer sounds across "
        f"{len(manifest_aliases)} aliases and "
        f"{len(extracted_presentation)} FX assets; "
        f"pruned {removed} stale presentation file(s)."
    )


if __name__ == "__main__":
    main()
