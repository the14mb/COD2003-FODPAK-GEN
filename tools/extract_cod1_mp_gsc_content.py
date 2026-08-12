#!/usr/bin/env python3
"""Extract the selected multiplayer GSC presentation closure.

Shared/gametype ``maps/mp`` scripts plus the approved map scripts are analyzed.
Resolved loadFx graphs, ordered shader images, model emitters and soundaliases
are copied into the locally generated fodpak; unselected map scripts,
SinglePlayer data and basename guesses are excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath

from PIL import Image

from cod1_archive_policy import (
    LayeredArchiveIndex,
    selected_archives,
)
from cod1_mp_gsc_content import analyze_multiplayer_gsc
from cod1_shipping_maps import selected_shipping_map_ids
from cod1_script_exploder import (
    ScriptExploderClosureError,
    _resolve_image,
    _shader_image_tokens,
    build_effect_closure,
)


FORMAT = "FriendsOfDuty.MultiplayerGscRuntimeContent"
VERSION = 1
OUTPUT_ROOT = PurePosixPath("scripts", "mp")
ENGINE_BUILTIN_SHADERS = frozenset(
    ("black", "default", "hudscoreboard_mp", "white")
)


def _normal(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    parsed = PurePosixPath(normalized)
    if (
        not normalized
        or parsed.is_absolute()
        or any(part in ("", ".", "..") for part in parsed.parts)
        or any(":" in part for part in parsed.parts)
    ):
        raise ValueError(f"unsafe MP GSC output path: {path!r}")
    return parsed.as_posix()


def efx_output_path(source: str) -> str:
    path = PurePosixPath(_normal(source))
    parts = (
        path.parts[1:]
        if path.parts and path.parts[0].casefold() == "fx"
        else path.parts
    )
    return PurePosixPath(OUTPUT_ROOT, "efx", *parts).as_posix()


def texture_output_path(source: str) -> str:
    path = PurePosixPath(_normal(source)).with_suffix(".png")
    return PurePosixPath(
        OUTPUT_ROOT,
        "textures",
        *path.parts,
    ).as_posix()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _convert_png(data: bytes, destination: Path) -> bytes:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA")
        output = io.BytesIO()
        image.save(output, format="PNG")
    encoded = output.getvalue()
    destination.write_bytes(encoded)
    return encoded


def _resolve_shader_images(
    index: LayeredArchiveIndex,
    shader: str,
) -> tuple[str, ...]:
    tokens = _shader_image_tokens(index, shader) or (shader,)
    try:
        return tuple(_resolve_image(index, token) for token in tokens)
    except ScriptExploderClosureError:
        # UO creates virtual levelshot shader handles such as mp_arnhem from
        # levelshots/mp_arnhem.dds.  This is a named engine convention, not a
        # basename scan.
        if "/" not in shader:
            return (_resolve_image(index, f"levelshots/{shader}"),)
        raise


def build_manifest(
    game_root: Path,
    pak_root: Path,
    *,
    include_uo: bool,
    selected_maps: tuple[str, ...] | None = None,
) -> dict[str, object]:
    archives = selected_archives(game_root)
    index = LayeredArchiveIndex(archives)
    map_ids = (
        selected_maps
        if selected_maps is not None
        else selected_shipping_map_ids(include_uo=include_uo)
    )
    analysis = analyze_multiplayer_gsc(
        index,
        selected_maps=map_ids,
    )

    effect_records: list[dict[str, object]] = []
    all_efx: dict[str, str] = {}
    all_images: dict[str, str] = {}
    effect_models: set[str] = set()
    effect_aliases: set[str] = set()
    missing_effects: list[dict[str, str]] = []
    for effect_index, source in enumerate(analysis.effect_paths):
        if index.get(source) is None:
            missing_effects.append(
                {
                    "path": source,
                    "reason":
                        "active maps/mp loadFx registration is absent "
                        "from the selected retail archives",
                }
            )
            continue
        effect = build_effect_closure(
            index,
            f"mp_gsc_{effect_index}",
            source,
        )
        for path in effect.efx_files:
            all_efx.setdefault(path.casefold(), path)
        for path in effect.image_files:
            all_images.setdefault(path.casefold(), path)
        effect_models.update(
            model.casefold() for model in effect.model_names
        )
        effect_aliases.update(effect.sound_aliases)
        effect_records.append(
            {
                # Runtime lookups use the exact loadFx path. It is distinct
                # from map exploder ids and therefore cannot collide.
                "fxId": source.casefold(),
                "sourceEfx": source,
                "efxFiles": [
                    efx_output_path(path)
                    for path in effect.efx_files
                ],
                "texturePngs": [
                    texture_output_path(path)
                    for path in effect.image_files
                ],
                "shaderTextures": [
                    {
                        "shader": shader,
                        "texturePngs": [
                            texture_output_path(path)
                            for path in images
                        ],
                    }
                    for shader, images in effect.shader_images
                ],
                "aliasNames": list(effect.sound_aliases),
                "modelNames": [
                    model.casefold()
                    for model in effect.model_names
                ],
            }
        )

    shader_records: list[dict[str, object]] = []
    unresolved_shaders: list[dict[str, str]] = []
    for shader in analysis.shader_names:
        try:
            images = _resolve_shader_images(index, shader)
        except ScriptExploderClosureError:
            unresolved_shaders.append(
                {
                    "shader": shader,
                    "reason": (
                        "engine builtin"
                        if shader.casefold() in ENGINE_BUILTIN_SHADERS
                        else "active maps/mp shader handle has no retail "
                        "image/declaration"
                    ),
                }
            )
            continue
        ordered: list[str] = []
        seen: set[str] = set()
        for image in images:
            all_images.setdefault(image.casefold(), image)
            if image.casefold() not in seen:
                seen.add(image.casefold())
                ordered.append(image)
        shader_records.append(
            {
                "shader": shader,
                "texturePngs": [
                    texture_output_path(path)
                    for path in ordered
                ],
            }
        )

    root = pak_root / OUTPUT_ROOT
    expected_outputs: set[Path] = set()
    efx_files: list[dict[str, object]] = []
    for source in (
        all_efx[key] for key in sorted(all_efx)
    ):
        reference = index.get(source)
        if reference is None:
            raise RuntimeError(
                f"recursive MP GSC EFX disappeared: {source}"
            )
        raw = index.read_ref(reference)
        relative = efx_output_path(source)
        destination = pak_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        expected_outputs.add(destination)
        efx_files.append(
            {
                "source": source,
                "path": relative,
                "sourceProvenance": index.provenance(reference, raw),
                "sha256": _sha256(raw),
            }
        )

    texture_files: list[dict[str, object]] = []
    for source in (
        all_images[key] for key in sorted(all_images)
    ):
        reference = index.get(source)
        if reference is None:
            raise RuntimeError(
                f"resolved MP GSC image disappeared: {source}"
            )
        raw = index.read_ref(reference)
        relative = texture_output_path(source)
        destination = pak_root / relative
        encoded = _convert_png(raw, destination)
        expected_outputs.add(destination)
        texture_files.append(
            {
                "source": source,
                "path": relative,
                "sourceProvenance": index.provenance(reference, raw),
                "sourceSha256": _sha256(raw),
                "sha256": _sha256(encoded),
            }
        )

    script_files: list[dict[str, object]] = []
    for source in analysis.script_files:
        reference = index.get(source)
        if reference is None:
            raise RuntimeError(
                f"analyzed MP GSC disappeared: {source}"
            )
        raw = index.read_ref(reference)
        script_files.append(
            {
                "source": source,
                "sourceProvenance": index.provenance(reference, raw),
                "sha256": _sha256(raw),
            }
        )

    manifest: dict[str, object] = {
        "format": FORMAT,
        "version": VERSION,
        "tiers": ["cod1"] + (["uo"] if include_uo else []),
        "selectedMaps": list(map_ids),
        "analysis": analysis.to_manifest(),
        "effects": effect_records,
        "shaderTextures": shader_records,
        "soundAliases": sorted(
            set(analysis.sound_aliases) | effect_aliases,
            key=str.casefold,
        ),
        "modelNames": sorted(
            set(analysis.model_names) | effect_models,
            key=str.casefold,
        ),
        "missingRetailEffects": missing_effects,
        "unresolvedShaders": unresolved_shaders,
        "scriptFiles": script_files,
        "efxFiles": efx_files,
        "textureFiles": texture_files,
        "counts": {
            **analysis.to_manifest()["counts"],
            "resolvedEffects": len(effect_records),
            "recursiveEfxFiles": len(efx_files),
            "resolvedShaderHandles": len(shader_records),
            "textureFiles": len(texture_files),
            "runtimeSoundAliases": (
                len(set(analysis.sound_aliases) | effect_aliases)
            ),
            "runtimeModelNames": (
                len(set(analysis.model_names) | effect_models)
            ),
            "missingRetailEffects": len(missing_effects),
            "unresolvedShaders": len(unresolved_shaders),
        },
    }

    manifest_path = root / "mp_content.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    expected_outputs.add(manifest_path)
    if root.is_dir():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() and path not in expected_outputs:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--pak-root", type=Path, required=True)
    parser.add_argument(
        "--include-uo", action="store_true", help=argparse.SUPPRESS
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
    manifest = build_manifest(
        args.game_root.resolve(),
        args.pak_root.resolve(),
        include_uo=args.include_uo,
        selected_maps=selected_maps,
    )
    print(
        "MP GSC runtime content: "
        f"{manifest['counts']['scripts']} scripts, "
        f"{manifest['counts']['runtimeSoundAliases']} aliases, "
        f"{manifest['counts']['runtimeModelNames']} models, "
        f"{manifest['counts']['recursiveEfxFiles']} EFX, "
        f"{manifest['counts']['textureFiles']} textures."
    )


if __name__ == "__main__":
    main()
