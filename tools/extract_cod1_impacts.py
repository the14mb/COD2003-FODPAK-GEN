#!/usr/bin/env python3
"""Extract CoD1 MP impact/minefield FX, related audio, and the scope mask.

Per-file provenance, verified byte-identical (sha256) against the previously
shipped Assets/Resources extraction on 2026-07-25:

  fx/textures/<name>.png        <- Main/pak5.pk3 gfx/impact/<name>.{tga,dds,jpg}
                                   The complete gfx/impact set (40 distinct names,
                                   43 members: cratered and sparkflash ship twice,
                                   .tga wins). Split below into the marks the
                                   engine projects onto geometry and the sprites
                                   it billboards, per Main/pak5.pk3
                                   fxshaders/pj_impact.shader — its "IMPACT DECALS"
                                   banner and the polygonOffset2 +
                                   'surfaceparm nonsolid/trans' stanzas mark the
                                   decal shaders — and the polygonOffset2 entries
                                   in fxshaders/pj_fx.shader (stone_singleshot1,
                                   cratered*).
  fx/efx/<name>.efx             <- Main/pak5.pk3 fx/impacts/<name>.efx (verbatim)
                                   The small-arms impact effects, kept as
                                   reference because their `Decal { ... shaders [
                                   ... ] }` block is the authority on which mark
                                   the original places on which surface.
                                   This also includes the exact three-effect
                                   closure loaded by maps/MP/_minefields.gsc:
                                   newimps/minefield -> fluff1 +
                                   dirthit_mortar.
  audio/impacts/<Surface>/*.wav <- Main/pak1.pk3 sound/weapons/impact/
                                   Impact_<Surface>_01..04.wav for Masonry, Wood,
                                   Snow, Dirt, Flesh; Metal uses the A-variant set
                                   Impact_Metal_A01..A04.wav (matches the shipped
                                   Resources/CoD1Impacts/Audio selection exactly)
  audio/whizby/*.wav            <- Main/pak1.pk3 sound/whizbys/whizby01..04.wav
  audio/fatigue/*.wav           <- uo/pakuo02.pk3 sound/misc/fatigue/
                                   fatigue_breath01..04.wav (United Offensive only;
                                   recorded as a note when uo/ is absent)
  ui/reticle_q.png              <- Main/pak0.pk3 ui/assets/reticle_q.tga (TGA->PNG;
                                   the quarter-mask ships verbatim in the original
                                   game, no crop/mirror derivation is needed)

Lookup is resilient to repacked installs: the expected member path is tried
first, then a lowercase-basename search across every Main/ and uo/ pk3 (later
archives in casefold sort order win, matching engine patch order).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from pathlib import Path

from cod1_archive_policy import (
    COD1_TIER,
    LayeredArchiveIndex,
    UO_TIER,
    official_archives,
)
from cod1_multiplayer_closure import (
    bsp_entities,
    selected_multiplayer_maps,
)
from cod1_script_exploder import (
    efx_output_path as exploder_efx_output_path,
    map_exploder_effects,
    texture_output_path as exploder_texture_output_path,
)
from cod1_shipping_maps import selected_shipping_map_ids

# gfx/impact members ship as .tga, a few also (or only) as .dds/.jpg. The
# engine references them extension-less, so the first hit in this order wins —
# .tga is the authored master wherever both forms exist (cratered, sparkflash).
IMPACT_TEXTURE_EXTENSIONS = (".tga", ".dds", ".jpg")

# Marks the engine projects onto the surface it hit: every shader in
# pj_impact.shader's "IMPACT DECALS" section plus the polygonOffset2 stanzas in
# pj_fx.shader. These are the bullet holes — NOT particle sprites.
IMPACT_DECAL_TEXTURES = (
    "bullethole1",
    "bullethole2",
    "bullethit_plaster",
    "bullethit_plaster2",
    "bullethit_wood1",
    "bullethit_wood2",
    "bullethit_sand",
    "bullethit_snow",
    "bullethit_glass",
    "bullethit_glass2",
    "stone_singleshot1",
    "cratered",
    "cratered_ground",
    "cratered_grounddetail",
)

# Billboarded debris/puff sprites — the shaders above them in pj_impact.shader
# and pj_fx.shader carry no polygonOffset2 and blend as ordinary particles
# (sparkflash/metal_spark1/sparktrail are additive GL_ONE GL_ONE and ship with
# no alpha channel at all).
IMPACT_PARTICLE_TEXTURES = (
    "bark_gib",
    "bark_gib2",
    "dustlayer1",
    "dusty",
    "dusty_puff",
    "flesh_hit1",
    "flesh_hit2",
    "flesh_hitgib",
    "foliage_gib1",
    "foliage_gib2",
    "foliage_stick",
    "grass_piece1",
    "grass_piece2",
    "gravelpuff",
    "metal_spark1",
    "snow1",
    "snowpuff",
    "sparkflash",
    "sparktrail",
    "stone_gib1",
    "stone_gib2",
    "stone_piece1",
    "stone_piece2",
    "wood_splinter1",
    "wood_splinter2",
    "woodpuff",
)

IMPACT_TEXTURES = IMPACT_DECAL_TEXTURES + IMPACT_PARTICLE_TEXTURES

# Small-arms impact effects, shipped verbatim as pipeline reference: the Decal
# block in each names the mark(s) the original picks at random for that
# surface, and its `size` (CoD units, x0.0254 for metres) and `life`
# (milliseconds) are what FodImpactContentBuilder's tuning is derived from.
IMPACT_EFX = (
    "default_hit",
    "small_plaster",
    "small_brick",
    "small_concrete",
    "small_rock",
    "small_gravel",
    "small_gravel2",
    "small_glass",
    "small_grass",
    "small_foliage",
    "woodhit_small",
    "snowhit_small",
    "metalhit_small",
    "flesh_hit",
)

# maps/MP/_minefields.gsc loads the first path. Its deathFx/playFx references
# form a closed three-file graph; none of these are SinglePlayer selections.
MINEFIELD_EFX = (
    ("minefield", "fx/impacts/newimps/minefield.efx"),
    ("fluff1", "fx/impacts/fluff1.efx"),
    ("dirthit_mortar", "fx/impacts/dirthit_mortar.efx"),
)

# Shader images referenced by that graph which are not already in
# IMPACT_TEXTURES. gfx/impact/dustlayer1 is already selected above and is the
# eighth image in the complete graph. Explicit candidate paths avoid the
# basename fallback accidentally selecting an unrelated repacked member.
MINEFIELD_TEXTURES = (
    (
        "dirtplume_model2",
        ("gfx/effects/explosion/dirtplume_model2.tga",),
    ),
    (
        "dirtplume_modelf",
        ("gfx/effects/explosion/dirtplume_modelf.tga",),
    ),
    ("cloudflash1", ("gfx/effects/cloudflash1.jpg",)),
    (
        "cloudflash_stalingradm",
        ("gfx/effects/misc/cloudflash_stalingradm.tga",),
    ),
    (
        "groundblastdark_gib",
        (
            "gfx/effects/misc/groundblastdark_gib.tga",
            "gfx/effects/misc/groundblastdark_gib.dds",
        ),
    ),
    ("explosion_1", ("gfx/effects/explosion/explosion_1.tga",)),
    ("groundflash1", ("gfx/effects/groundflash1.jpg",)),
)

SURFACE_CLIP_PREFIXES = (
    ("Masonry", "Impact_Masonry_"),
    ("Wood", "Impact_Wood_"),
    ("Metal", "Impact_Metal_A"),
    ("Snow", "Impact_Snow_"),
    ("Dirt", "Impact_Dirt_"),
    ("Flesh", "Impact_Flesh_"),
)
CLIPS_PER_SURFACE = 4

RETICLE_MEMBER = "ui/assets/reticle_q.tga"


class ArchiveIndex:
    def __init__(self, game_root: Path, include_uo: bool = False) -> None:
        self.archives = official_archives(game_root, COD1_TIER)
        if include_uo:
            self.archives += official_archives(game_root, UO_TIER)
        self.by_member: dict[str, tuple[Path, str]] = {}
        self.by_basename: dict[str, tuple[Path, str]] = {}
        for archive_path in self.archives:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.namelist():
                    if member.endswith("/"):
                        continue
                    key = member.replace("\\", "/").lower()
                    self.by_member[key] = (archive_path, member)
                    self.by_basename[Path(key).name] = (archive_path, member)

    def read(self, expected_member: str) -> tuple[bytes, str] | None:
        key = expected_member.lower()
        source = self.by_member.get(key) or self.by_basename.get(Path(key).name)
        if source is None:
            return None
        archive_path, member = source
        with zipfile.ZipFile(archive_path) as archive:
            return archive.read(member), f"{archive_path.parent.name}/{archive_path.name}:{member}"

    def read_first(self, members: tuple[str, ...]) -> tuple[bytes, str] | None:
        """First resolvable member of an ordered candidate list."""
        for member in members:
            result = self.read(member)
            if result is not None:
                return result
        return None


def convert_to_png(data: bytes, destination: Path) -> None:
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA")
        image.save(destination, format="PNG")


def write_bytes(data: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-root", type=Path, required=True,
                        help="Call of Duty installation containing Main/ and optionally uo/")
    parser.add_argument("--pak-root", type=Path, required=True,
                        help="fodpak content root (receives fx/, audio/, ui/)")
    parser.add_argument("--notes-file", type=Path,
                        help="optional file receiving one line per unresolvable asset")
    parser.add_argument("--force", action="store_true",
                        help="re-extract fx/ files that already exist in the pak "
                             "(default: skip them, so reruns stay cheap)")
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
    selected_maps = selected_shipping_map_ids(
        include_uo=args.include_uo,
        requested_maps=args.maps,
    )

    index = ArchiveIndex(
        args.game_root.resolve(),
        include_uo=args.include_uo,
    )
    pak_root = args.pak_root.resolve()
    has_uo = any(archive.parent.name == "uo" for archive in index.archives)

    notes: list[str] = []
    missing_required: list[str] = []
    extracted = 0

    def pull(expected_member: str, required: bool = True) -> tuple[bytes, str] | None:
        result = index.read(expected_member)
        if result is None:
            message = f"impacts: no pk3 member found for {expected_member}"
            print(f"WARNING {message}")
            if required:
                missing_required.append(expected_member)
            notes.append(message)
        return result

    skipped = 0

    # Exact shipping-map script_exploder graph. This is deliberately derived
    # from the selected BSP/GSC roster rather than a broad fx/ sweep.
    exploder_index = LayeredArchiveIndex(index.archives)
    exploder_maps: list[dict[str, object]] = []
    exploder_efx_sources: dict[str, str] = {}
    exploder_image_sources: dict[str, str] = {}
    exploder_models: set[str] = set()
    exploder_aliases: set[str] = set()
    for map_id, bsp_reference in selected_multiplayer_maps(
        exploder_index,
        selected_maps,
    ):
        entities = bsp_entities(
            exploder_index.read_ref(bsp_reference),
            bsp_reference.name,
        )
        script_data = exploder_index.read(f"maps/mp/{map_id}.gsc")
        script = (
            script_data.decode("latin1", "replace")
            if script_data is not None
            else None
        )
        effects = map_exploder_effects(
            exploder_index,
            map_id,
            entities,
            script,
        )
        effect_records = []
        for effect in effects:
            for source in effect.efx_files:
                exploder_efx_sources.setdefault(
                    source.casefold(),
                    source,
                )
            for source in effect.image_files:
                exploder_image_sources.setdefault(
                    source.casefold(),
                    source,
                )
            exploder_models.update(
                model.casefold()
                for model in effect.model_names
            )
            exploder_aliases.update(effect.sound_aliases)
            effect_records.append(
                {
                    "fxId": effect.fx_id,
                    "sourceEfx": effect.source_efx,
                    "efxFiles": [
                        exploder_efx_output_path(path)
                        for path in effect.efx_files
                    ],
                    "texturePngs": [
                        exploder_texture_output_path(path)
                        for path in effect.image_files
                    ],
                    "shaderTextures": [
                        {
                            "shader": shader,
                            "texturePngs": [
                                exploder_texture_output_path(path)
                                for path in image_paths
                            ],
                        }
                        for shader, image_paths in effect.shader_images
                    ],
                    "modelNames": [
                        model.casefold()
                        for model in effect.model_names
                    ],
                    "aliasNames": list(effect.sound_aliases),
                }
            )
        if effect_records:
            exploder_maps.append(
                {
                    "game": (
                        "uo"
                        if bsp_reference.archive.parent.name.casefold()
                        == "uo"
                        else "cod1"
                    ),
                    "mapId": map_id,
                    "effects": effect_records,
                }
            )

    expected_exploder_outputs: set[Path] = set()
    efx_records = []
    for source in (
        exploder_efx_sources[key]
        for key in sorted(exploder_efx_sources)
    ):
        reference = exploder_index.get(source)
        if reference is None:
            missing_required.append(source)
            continue
        data = exploder_index.read_ref(reference)
        relative = exploder_efx_output_path(source)
        destination = pak_root / relative
        write_bytes(data, destination)
        expected_exploder_outputs.add(destination)
        extracted += 1
        efx_records.append(
            {
                "source": source,
                "path": relative,
                "sourceArchive": (
                    f"{reference.archive.parent.name}/"
                    f"{reference.archive.name}:{reference.name}"
                ),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        print(
            f"{relative} <- {reference.archive.parent.name}/"
            f"{reference.archive.name}:{reference.name}"
        )

    texture_records = []
    for source in (
        exploder_image_sources[key]
        for key in sorted(exploder_image_sources)
    ):
        reference = exploder_index.get(source)
        if reference is None:
            missing_required.append(source)
            continue
        data = exploder_index.read_ref(reference)
        relative = exploder_texture_output_path(source)
        destination = pak_root / relative
        convert_to_png(data, destination)
        expected_exploder_outputs.add(destination)
        extracted += 1
        output_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
        texture_records.append(
            {
                "source": source,
                "path": relative,
                "sourceArchive": (
                    f"{reference.archive.parent.name}/"
                    f"{reference.archive.name}:{reference.name}"
                ),
                "sourceSha256": hashlib.sha256(data).hexdigest(),
                "sha256": output_sha256,
            }
        )
        print(
            f"{relative} <- {reference.archive.parent.name}/"
            f"{reference.archive.name}:{reference.name}"
        )

    exploder_root = pak_root / "fx" / "exploders"
    if exploder_root.is_dir():
        for stale in exploder_root.rglob("*"):
            if (
                stale.is_file()
                and stale.name != "manifest.json"
                and stale not in expected_exploder_outputs
            ):
                stale.unlink()
        for directory in sorted(
            (
                path
                for path in exploder_root.rglob("*")
                if path.is_dir()
            ),
            reverse=True,
        ):
            if not any(directory.iterdir()):
                directory.rmdir()
    exploder_manifest = {
        "format": "FriendsOfDuty.ScriptExploderContent",
        "version": 1,
        "tiers": ["cod1"] + (["uo"] if has_uo else []),
        "selectedMaps": list(selected_maps),
        "maps": exploder_maps,
        "efxFiles": efx_records,
        "textureFiles": texture_records,
        "modelNames": sorted(exploder_models),
        "aliasNames": sorted(exploder_aliases),
        "counts": {
            "maps": len(exploder_maps),
            "effects": sum(
                len(entry["effects"])
                for entry in exploder_maps
            ),
            "efxFiles": len(efx_records),
            "textureFiles": len(texture_records),
            "modelNames": len(exploder_models),
            "aliasNames": len(exploder_aliases),
        },
    }
    manifest_path = exploder_root / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            exploder_manifest,
            indent=2,
            sort_keys=False,
        ) + "\n",
        encoding="utf-8",
    )

    for name in IMPACT_TEXTURES:
        destination = pak_root / "fx" / "textures" / f"{name}.png"
        if destination.is_file() and not args.force:
            skipped += 1
            continue
        candidates = tuple(
            f"gfx/impact/{name}{extension}"
            for extension in IMPACT_TEXTURE_EXTENSIONS)
        result = index.read_first(candidates)
        if result is None:
            message = f"impacts: no pk3 member found for gfx/impact/{name}.*"
            print(f"WARNING {message}")
            missing_required.append(f"gfx/impact/{name}.*")
            notes.append(message)
            continue
        data, provenance = result
        convert_to_png(data, destination)
        extracted += 1
        print(f"fx/textures/{name}.png <- {provenance}")

    for name in IMPACT_EFX:
        destination = pak_root / "fx" / "efx" / f"{name}.efx"
        if destination.is_file() and not args.force:
            skipped += 1
            continue
        result = pull(f"fx/impacts/{name}.efx", required=False)
        if result is None:
            continue
        data, provenance = result
        write_bytes(data, destination)
        extracted += 1
        print(f"fx/efx/{name}.efx <- {provenance}")

    for output_name, candidates in MINEFIELD_TEXTURES:
        destination = (
            pak_root / "fx" / "textures" / f"{output_name}.png"
        )
        if destination.is_file() and not args.force:
            skipped += 1
            continue
        result = index.read_first(candidates)
        if result is None:
            expected = " or ".join(candidates)
            message = (
                "impacts: no pk3 member found for minefield shader image "
                f"{expected}"
            )
            print(f"WARNING {message}")
            missing_required.append(expected)
            notes.append(message)
            continue
        data, provenance = result
        convert_to_png(data, destination)
        extracted += 1
        print(f"fx/textures/{output_name}.png <- {provenance}")

    for output_name, expected_member in MINEFIELD_EFX:
        destination = (
            pak_root / "fx" / "efx" / f"{output_name}.efx"
        )
        if destination.is_file() and not args.force:
            skipped += 1
            continue
        result = pull(expected_member)
        if result is None:
            continue
        data, provenance = result
        write_bytes(data, destination)
        extracted += 1
        print(f"fx/efx/{output_name}.efx <- {provenance}")

    for surface, prefix in SURFACE_CLIP_PREFIXES:
        for number in range(1, CLIPS_PER_SURFACE + 1):
            filename = f"{prefix}{number:02d}.wav"
            result = pull(f"sound/weapons/impact/{filename}")
            if result is None:
                continue
            data, provenance = result
            write_bytes(data, pak_root / "audio" / "impacts" / surface / filename)
            extracted += 1
            print(f"audio/impacts/{surface}/{filename} <- {provenance}")

    for number in range(1, 5):
        filename = f"whizby{number:02d}.wav"
        result = pull(f"sound/whizbys/{filename}")
        if result is None:
            continue
        data, provenance = result
        write_bytes(data, pak_root / "audio" / "whizby" / filename)
        extracted += 1
        print(f"audio/whizby/{filename} <- {provenance}")

    if has_uo:
        for number in range(1, 5):
            filename = f"fatigue_breath{number:02d}.wav"
            result = pull(f"sound/misc/fatigue/{filename}")
            if result is None:
                continue
            data, provenance = result
            write_bytes(data, pak_root / "audio" / "fatigue" / filename)
            extracted += 1
            print(f"audio/fatigue/{filename} <- {provenance}")

        # Shellshock, pulled by member rather than by alias ON PURPOSE.
        # The alias table is a trap here: shellshock_loop and
        # shellshock_end resolve to TWO variants each, because the
        # single-player iw_sound.csv rows union with the MP ones and the
        # exporter has no loadspec layer. Retail never sees the collision --
        # soundloadspecs/mp/default.csv simply never loads iw_sound.csv --
        # but a random-variant pick here would play the SP cue half the
        # time. These two members are the MP ones by name.
        for filename in (
            "shellshock_loop_mp.wav",
            "shellshock_exit_mp.wav",
        ):
            result = pull(f"sound/misc/{filename}")
            if result is None:
                continue
            data, provenance = result
            write_bytes(data, pak_root / "audio" / "misc" / filename)
            extracted += 1
            print(f"audio/misc/{filename} <- {provenance}")

        # The concussion's authored parameters. 861 B and 828 B of
        # key/value text that ARE the retail definition of the effect --
        # every number the presentation used to invent.
        for profile in ("default_mp", "melee"):
            result = pull(f"scripts/{profile}.shock")
            if result is None:
                continue
            data, provenance = result
            write_bytes(
                data,
                pak_root / "scripts" / "shellshock" / f"{profile}.shock",
            )
            extracted += 1
            print(
                f"scripts/shellshock/{profile}.shock <- {provenance}"
            )
    else:
        fatigue_dir = pak_root / "audio" / "fatigue"
        if fatigue_dir.is_dir():
            for stale in fatigue_dir.glob("fatigue_breath*.wav"):
                stale.unlink()
        message = ("impacts: United Offensive (uo/) not installed — "
                   "audio/fatigue sprint-breathing clips are not selected")
        print(f"WARNING {message}")
        notes.append(message)

    result = pull(RETICLE_MEMBER)
    if result is not None:
        data, provenance = result
        convert_to_png(data, pak_root / "ui" / "reticle_q.png")
        extracted += 1
        print(f"ui/reticle_q.png <- {provenance}")

    if args.notes_file and notes:
        args.notes_file.parent.mkdir(parents=True, exist_ok=True)
        args.notes_file.write_text("\n".join(notes) + "\n", encoding="utf-8")

    print(f"Extracted {extracted} impact/minefield/whizby/fatigue/reticle assets "
          f"({skipped} already present, {len(notes)} note(s)).")
    if missing_required:
        raise SystemExit(
            "missing required assets: " + ", ".join(missing_required))


if __name__ == "__main__":
    main()
