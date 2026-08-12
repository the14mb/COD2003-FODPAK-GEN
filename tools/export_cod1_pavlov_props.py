"""Blender background exporter for the XModels referenced by mp_pavlov.bsp.

Invoke:
  blender --background --python tools/export_cod1_pavlov_props.py -- \
    SOURCE ENTITIES_JSON OUTPUT IMPORTER_PY_ROOT
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import bpy


COD_UNIT_TO_METRE = 0.0254


def arguments() -> tuple[Path, Path, Path, Path]:
    values = sys.argv[sys.argv.index("--") + 1 :]
    if len(values) != 4:
        raise SystemExit(
            "expected SOURCE ENTITIES_JSON OUTPUT IMPORTER_PY_ROOT"
        )
    return tuple(Path(value).resolve() for value in values)


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (
        bpy.data.meshes,
        bpy.data.armatures,
        bpy.data.materials,
        bpy.data.images,
    ):
        for datablock in list(collection):
            collection.remove(datablock)


def file_images() -> list[tuple[object, Path]]:
    result = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        path = Path(bpy.path.abspath(image.filepath))
        if path.is_file():
            result.append((image, path))
    return result


def prepare_fbx_materials() -> list[dict[str, str]]:
    """Reduce CoD's preview shader graph to FBX's portable PBR subset.

    The source importer builds an alpha Mix Shader for Blender previewing.
    Blender's FBX exporter does not serialize the image binding from that
    graph, which made Unity import every material without its diffuse texture.
    """
    bindings = []
    for material in bpy.data.materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        image_nodes = [
            node
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image is not None
        ]
        if not image_nodes:
            continue

        image = image_nodes[0].image
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()

        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (400, 0)
        principled = nodes.new("ShaderNodeBsdfPrincipled")
        principled.location = (100, 0)
        texture = nodes.new("ShaderNodeTexImage")
        texture.location = (-250, 0)
        texture.image = image

        links.new(texture.outputs["Color"], principled.inputs["Base Color"])
        links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
        links.new(principled.outputs["BSDF"], output.inputs["Surface"])
        principled.inputs["Roughness"].default_value = 0.65
        material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
        bindings.append(
            {
                "material": material.name,
                "texture": Path(bpy.path.abspath(image.filepath)).name,
            }
        )
    return bindings


def export_model(
    source_root: Path,
    output_root: Path,
    model_name: str,
    importer,
) -> dict[str, object]:
    clear_scene()
    source_model = source_root / "xmodel" / model_name
    if not source_model.is_file():
        raise FileNotFoundError(source_model)
    importer.import_xmodel(
        asset_path=str(source_root),
        file_path=str(source_model),
        selected_version=importer.GAME_VERSION.CoD,
    )

    meshes = [
        obj for obj in bpy.context.scene.objects if obj.type == "MESH"
    ]
    if not meshes:
        raise RuntimeError("import produced no mesh objects")

    asset_dir = output_root / model_name
    texture_dir = asset_dir / "Textures"
    asset_dir.mkdir(parents=True, exist_ok=True)
    texture_dir.mkdir(parents=True, exist_ok=True)

    copied_textures = []
    for image, source in file_images():
        destination = texture_dir / source.name
        shutil.copy2(source, destination)
        image.filepath = str(destination)
        copied_textures.append(destination.name)

    material_bindings = prepare_fbx_materials()

    # The source XModel and BSP entity origins are authored in CoD inches.
    # Keep the model prefab metre-native so only entity positions need the
    # shared 0.0254 conversion in Unity.
    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.scale = tuple(
                component * COD_UNIT_TO_METRE for component in obj.scale
            )

    fbx_path = asset_dir / f"{model_name}.fbx"
    bpy.ops.export_scene.fbx(
        filepath=str(fbx_path),
        use_selection=False,
        apply_unit_scale=True,
        bake_space_transform=False,
        add_leaf_bones=False,
        path_mode="COPY",
        embed_textures=False,
        object_types={"EMPTY", "MESH", "ARMATURE"},
    )
    return {
        "name": model_name,
        "fbx": fbx_path.name,
        "meshes": len(meshes),
        "vertices": sum(len(obj.data.vertices) for obj in meshes),
        "triangles": sum(len(obj.data.polygons) for obj in meshes),
        "textures": sorted(set(copied_textures)),
        "materials": material_bindings,
    }


def main() -> None:
    source_root, entity_path, output_root, importer_root = arguments()
    sys.path.insert(0, str(importer_root))
    from cod_asset_importer import importer
    from cod_asset_importer.cod_asset_importer import GAME_VERSION

    importer.GAME_VERSION = GAME_VERSION
    payload = json.loads(entity_path.read_text(encoding="utf-8"))
    models = payload["modelAssets"]
    output_root.mkdir(parents=True, exist_ok=True)

    results = []
    failures = []
    for index, model_name in enumerate(models, 1):
        print(f"[{index}/{len(models)}] PAVLOV prop: {model_name}")
        try:
            results.append(
                export_model(
                    source_root,
                    output_root,
                    model_name,
                    importer,
                )
            )
        except Exception as error:
            failures.append({"name": model_name, "error": str(error)})
            print(f"FAILED {model_name}: {error}")

    manifest = {
        "category": "pavlov_props",
        "sourceEntityManifest": entity_path.name,
        "models": results,
        "failures": failures,
    }
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"PAVLOV prop export: {len(results)} succeeded, "
        f"{len(failures)} failed"
    )
    if failures:
        raise SystemExit("PAVLOV prop export failed; see manifest.json")


if __name__ == "__main__":
    main()
