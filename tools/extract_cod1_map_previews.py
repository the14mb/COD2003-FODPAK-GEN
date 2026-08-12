#!/usr/bin/env python3
"""Extract retail multiplayer level shots as the map-picker previews.

The create-server lobby's MAP section draws a preview for the highlighted
map through ``MenuSkin.GetMapPreview``, which reads
``maps/previews/{game}_{map_id}.png`` out of the mounted package. Nothing
wrote that path, so every map rendered the "[no preview in pak]" fallback
even though retail ships a 1024x1024 level shot for all of them.

Retail keeps them in the LOCALIZED archives -- ``levelshots/<map_id>.dds``
in ``Main/localized_english_pak1.pk3`` for the base game and
``uo/localized_english_pakuo00.pk3`` for United Offensive -- so they are
resolved through the same layered index and load order as every other
asset, and the higher tier wins if both define one.

The output name carries the game tier because the catalog key does:
``cod1``/``mp_carentan`` and ``uo``/``mp_arnhem`` are distinct entries and
the runtime looks them up as ``{game}_{map_id}``.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from PIL import Image

from cod1_archive_policy import (
    LayeredArchiveIndex,
    selected_archives,
)
from cod1_shipping_maps import shipping_map_specs

OUTPUT_ROOT = "maps/previews"


def preview_output_path(game: str, map_id: str) -> str:
    """The pak-relative path MenuSkin.GetMapPreview reads."""
    return f"{OUTPUT_ROOT}/{game}_{map_id}.png"


def _convert(data: bytes, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        # Level shots are opaque; the alpha channel a DXT decode hands back
        # is dead weight in a menu texture that is always drawn filled.
        converted = image.convert("RGB")
        buffer = io.BytesIO()
        converted.save(buffer, format="PNG", optimize=True)
    encoded = buffer.getvalue()
    destination.write_bytes(encoded)
    return len(encoded)


def extract_map_previews(
    game_root: Path,
    pak_root: Path,
    *,
    include_uo: bool,
    selected_maps: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Write every shipping map's preview; return a provenance manifest."""
    index = LayeredArchiveIndex(selected_archives(game_root))
    specs = shipping_map_specs(include_uo=include_uo)
    if selected_maps is not None:
        wanted = set(selected_maps)
        specs = [spec for spec in specs if spec[2] in wanted]

    # The lobby lists exactly the shipping roster, so a preview for anything
    # else is a leftover from a wider roster and would ship as dead weight.
    # Pruned against the FULL roster, never the optional --maps subset, so a
    # narrowed manual run cannot delete the other maps' art.
    previews_dir = pak_root / OUTPUT_ROOT
    if previews_dir.is_dir():
        shipping_previews = {
            preview_output_path(game, map_id).rsplit("/", 1)[-1].casefold()
            for game, _source_tier, map_id in shipping_map_specs(
                include_uo=True,
            )
        }
        for stale in previews_dir.glob("*.png"):
            if stale.name.casefold() not in shipping_previews:
                stale.unlink()

    written: list[dict[str, object]] = []
    missing: list[str] = []
    for game, _source_tier, map_id in specs:
        member = index.get(f"levelshots/{map_id}.dds")
        if member is None:
            # Not fatal: a map with no authored level shot simply keeps the
            # lobby's text fallback rather than failing the export.
            missing.append(f"{game}/{map_id}")
            continue
        data = index.read_ref(member)
        relative = preview_output_path(game, map_id)
        size = _convert(data, pak_root / relative)
        written.append(
            {
                "game": game,
                "mapId": map_id,
                "path": relative,
                "bytes": size,
                "source": index.provenance(member, data),
            }
        )
    return {
        "format": "FriendsOfDuty.MapPreviews",
        "version": 1,
        "previews": written,
        "missing": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-root", required=True, type=Path)
    parser.add_argument("--pak-root", required=True, type=Path)
    # The shipping roster is fixed and UO is mandatory, so the pipeline
    # deliberately passes no tier flag. This stays only for running the tool
    # by hand against a base-game-only tree.
    parser.add_argument(
        "--base-game-only",
        action="store_true",
        help="Skip the United Offensive maps (manual runs only).",
    )
    parser.add_argument(
        "--maps",
        default=None,
        help="Comma-separated map ids; default is the shipping roster.",
    )
    parser.add_argument("--manifest", default=None, type=Path)
    args = parser.parse_args()

    selected = (
        tuple(part.strip() for part in args.maps.split(",") if part.strip())
        if args.maps
        else None
    )
    manifest = extract_map_previews(
        args.game_root,
        args.pak_root,
        include_uo=not args.base_game_only,
        selected_maps=selected,
    )
    if args.manifest is not None:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
    for entry in manifest["previews"]:
        print(f"{entry['path']}  ({entry['bytes']:,} bytes)")
    for key in manifest["missing"]:
        print(f"no authored level shot for {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
