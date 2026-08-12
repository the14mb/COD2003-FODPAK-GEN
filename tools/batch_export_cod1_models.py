"""Blender background script: convert selected CoD 1 XModels to FBX.

Invoke with:
  blender --background --python this_file.py -- SOURCE OUTPUT weapon

Pak mode (runtime content package):
  ... -- SOURCE OUTPUT {weapon|effect|projectile} IMPORTER_PY_ROOT \
      --format glb --pak-root CONTENT_DIR [--models a,b]
Category weapon reads CONTENT_DIR/weapons/weapons.json for the roster world
models and appends worldMaterials back into it; category effect exports the
shared shellcasing; category projectile exports every definition
projectile_model (grenades, panzerfaust rocket) to
weapons/projectiles/<model>/projectile.glb and wires projectileGlb +
projectileMaterials back into weapons.json.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fod_export_common import (
    build_material_table,
    export_static_glb,
    load_json,
    parse_cli,
    prepare_static_scene,
    write_json,
)


def arguments() -> tuple[Path, Path, str, Path, str, Path | None, set[str] | None]:
    args = sys.argv[sys.argv.index("--") + 1 :]
    positionals, export_format, pak_root, models = parse_cli(
        args,
        4,
        "expected SOURCE OUTPUT {weapon|player|effect|projectile} IMPORTER_PY_ROOT "
        "[--format fbx|glb] [--pak-root DIR] [--models a,b]",
    )
    return (
        Path(positionals[0]),
        Path(positionals[1]),
        positionals[2],
        Path(positionals[3]),
        export_format,
        pak_root,
        models,
    )


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for datablocks in (bpy.data.meshes, bpy.data.armatures, bpy.data.materials, bpy.data.images):
        for datablock in list(datablocks):
            datablocks.remove(datablock)


def texture_files() -> list[Path]:
    files: list[Path] = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        path = Path(bpy.path.abspath(image.filepath))
        if path.is_file() and path not in files:
            files.append(path)
    return files


def export_one(source_root: Path, output_root: Path, xmodel: Path, importer) -> dict:
    clear_scene()
    importer.import_xmodel(
        asset_path=str(source_root),
        file_path=str(xmodel),
        selected_version=importer.GAME_VERSION.CoD,
    )

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("import produced no mesh objects")

    name = xmodel.name
    asset_dir = output_root / name
    texture_dir = asset_dir / "Textures"
    asset_dir.mkdir(parents=True, exist_ok=True)
    texture_dir.mkdir(parents=True, exist_ok=True)

    copied_textures = []
    for source in texture_files():
        destination = texture_dir / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
        copied_textures.append(destination.name)

    # CoD coordinates are inches; scale the model root once to convert to metres.
    # Scale only top-level roots. Applying the same factor to children as well
    # compounds it through the armature hierarchy.
    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.scale = tuple(value * 0.0254 for value in obj.scale)

    fbx_path = asset_dir / f"{name}.fbx"
    bpy.ops.export_scene.fbx(
        filepath=str(fbx_path),
        use_selection=False,
        apply_unit_scale=True,
        bake_space_transform=False,
        add_leaf_bones=False,
        path_mode="COPY",
        embed_textures=True,
        object_types={"EMPTY", "MESH", "ARMATURE"},
    )
    bpy.ops.wm.save_as_mainfile(filepath=str(asset_dir / f"{name}.blend"))

    return {
        "name": name,
        "fbx": fbx_path.name,
        "meshes": len(meshes),
        "vertices": sum(len(obj.data.vertices) for obj in meshes),
        # CoD XModel surfaces are already triangulated.
        "triangles": sum(len(obj.data.polygons) for obj in meshes),
        "textures": sorted(set(copied_textures)),
    }


def import_source_model(source_root: Path, xmodel: Path, importer) -> None:
    clear_scene()
    importer.import_xmodel(
        asset_path=str(source_root),
        file_path=str(xmodel),
        selected_version=importer.GAME_VERSION.CoD,
    )
    if not any(obj.type == "MESH" for obj in bpy.context.scene.objects):
        raise RuntimeError("import produced no mesh objects")


def export_static_pak_model(
    source_root: Path,
    xmodel: Path,
    glb_path: Path,
    texture_rel: str,
    importer,
    keep_tags: bool = False,
) -> tuple[list[dict], list[str]]:
    """Import one XModel and write a static pak GLB plus PNG textures.

    Returns the [{material, texture, cutout}] table for the model and the
    tag_* attachment nodes carried over from the source rig.
    """
    import_source_model(source_root, xmodel, importer)
    materials = build_material_table(glb_path.parent / "textures", texture_rel)
    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.scale = tuple(value * 0.0254 for value in obj.scale)
    tags = prepare_static_scene(keep_tags=keep_tags)
    export_static_glb(glb_path)
    return materials, tags


def source_model_index(source_root: Path) -> dict[str, Path]:
    return {
        path.name.casefold(): path
        for path in (source_root / "xmodel").iterdir()
        if path.is_file()
    }


def run_pak_weapons(
    source_root: Path,
    pak_root: Path,
    selected: set[str] | None,
    importer,
) -> None:
    manifest_path = pak_root / "weapons" / "weapons.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"missing {manifest_path}; run the viewmodel exporter first"
        )
    manifest = load_json(manifest_path)
    weapons = manifest.get("weapons", [])
    source_models = source_model_index(source_root)
    failures = []
    # Roster entries may share one world model; export each world once.
    exported: dict[str, list[dict]] = {}
    for index, entry in enumerate(weapons, 1):
        world_lower = entry["world"]
        if selected is not None and not (
            {entry["id"].casefold(), world_lower.casefold()} & selected
        ):
            continue
        world_glb = f"weapons/worldmodels/{world_lower}/world.glb"
        if world_lower in exported:
            entry["worldMaterials"] = exported[world_lower]
            entry["worldGlb"] = world_glb
            continue
        source_model = source_models.get(entry["definition"]["world"].casefold())
        if source_model is None:
            failures.append(
                {"name": world_lower, "error": "source XModel is missing"}
            )
            print(f"[{index}/{len(weapons)}] MISSING: {world_lower}")
            continue
        print(f"[{index}/{len(weapons)}] worldmodel: {world_lower}")
        try:
            # World models keep their tag_* nodes: the runtime spawns muzzle
            # flash and brass at tag_flash/tag_brass on the third-person model.
            materials, tags = export_static_pak_model(
                source_root,
                source_model,
                pak_root / "weapons" / "worldmodels" / world_lower / "world.glb",
                f"weapons/worldmodels/{world_lower}/textures",
                importer,
                keep_tags=True,
            )
        except Exception as error:
            failures.append({"name": world_lower, "error": str(error)})
            print(f"FAILED {world_lower}: {error}")
            continue
        print(f"    tags: {', '.join(tags) if tags else '(none)'}")
        entry["worldMaterials"] = materials
        entry["worldGlb"] = world_glb
        exported[world_lower] = materials
    write_json(manifest_path, manifest)
    if failures:
        raise SystemExit(f"{len(failures)} world model(s) failed")


def run_pak_projectiles(
    source_root: Path,
    pak_root: Path,
    selected: set[str] | None,
    importer,
) -> None:
    """Export every definition projectile_model referenced by weapons.json as
    a static GLB under weapons/projectiles/ and wire projectileGlb +
    projectileMaterials into the owning weapon entries."""
    manifest_path = pak_root / "weapons" / "weapons.json"
    if not manifest_path.is_file():
        raise SystemExit(
            f"missing {manifest_path}; run the viewmodel exporter first"
        )
    manifest = load_json(manifest_path)
    weapons = manifest.get("weapons", [])
    source_models = source_model_index(source_root)
    failures = []
    # A projectile model may be shared between weapons; export each once.
    exported: dict[str, list[dict]] = {}
    for index, entry in enumerate(weapons, 1):
        model_name = entry.get("definition", {}).get("projectile_model", "")
        if not model_name:
            continue
        model_lower = model_name.lower()
        if selected is not None and not (
            {entry["id"].casefold(), model_lower.casefold()} & selected
        ):
            continue
        projectile_glb = f"weapons/projectiles/{model_lower}/projectile.glb"
        if model_lower in exported:
            entry["projectileMaterials"] = exported[model_lower]
            entry["projectileGlb"] = projectile_glb
            continue
        source_model = source_models.get(model_name.casefold())
        if source_model is None:
            failures.append(
                {"name": model_lower, "error": "source XModel is missing"}
            )
            print(f"[{index}/{len(weapons)}] MISSING: {model_lower}")
            continue
        print(f"[{index}/{len(weapons)}] projectile: {model_lower}")
        try:
            materials, _ = export_static_pak_model(
                source_root,
                source_model,
                pak_root / "weapons" / "projectiles" / model_lower
                / "projectile.glb",
                f"weapons/projectiles/{model_lower}/textures",
                importer,
            )
        except Exception as error:
            failures.append({"name": model_lower, "error": str(error)})
            print(f"FAILED {model_lower}: {error}")
            continue
        entry["projectileMaterials"] = materials
        entry["projectileGlb"] = projectile_glb
        exported[model_lower] = materials
    write_json(manifest_path, manifest)
    if failures:
        raise SystemExit(f"{len(failures)} projectile model(s) failed")


def run_pak_shellcasing(source_root: Path, pak_root: Path, importer) -> None:
    print("effect: shellcasing")
    export_static_pak_model(
        source_root,
        source_root / "xmodel/shellcasing",
        pak_root / "weapons" / "shell" / "shellcasing.glb",
        "weapons/shell/textures",
        importer,
    )


def main() -> None:
    source_root, output_root, category, importer_root, export_format, pak_root, selected = (
        arguments()
    )
    sys.path.insert(0, str(importer_root))
    from cod_asset_importer import importer
    from cod_asset_importer.cod_asset_importer import GAME_VERSION

    # Expose the enum alongside the importer module for export_one.
    importer.GAME_VERSION = GAME_VERSION

    if export_format == "glb":
        if category == "weapon":
            run_pak_weapons(source_root, pak_root, selected, importer)
        elif category == "projectile":
            run_pak_projectiles(source_root, pak_root, selected, importer)
        elif category == "effect":
            run_pak_shellcasing(source_root, pak_root, importer)
        else:
            raise SystemExit(f"category {category} is not part of the pak flow")
        return

    if category == "weapon":
        models = sorted((source_root / "xmodel").glob("weapon_*"))
    elif category == "player":
        models = sorted((source_root / "xmodel").glob("character_*"))
    elif category == "effect":
        models = [source_root / "xmodel/shellcasing"]
    else:
        raise SystemExit(f"unsupported category: {category}")

    output_root.mkdir(parents=True, exist_ok=True)
    results = []
    failures = []
    for index, model in enumerate(models, 1):
        print(f"[{index}/{len(models)}] {category}: {model.name}")
        try:
            results.append(export_one(source_root, output_root, model, importer))
        except Exception as error:
            failures.append({"name": model.name, "error": str(error)})
            print(f"FAILED {model.name}: {error}")

    manifest = {"category": category, "models": results, "failures": failures}
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if failures:
        raise SystemExit(f"{len(failures)} model(s) failed; see manifest.json")


if __name__ == "__main__":
    main()
