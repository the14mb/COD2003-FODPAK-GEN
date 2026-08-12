"""Compare raw CoD XModel bounds with exported PAVLOV FBX bounds."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy
from mathutils import Vector


COD_UNIT_TO_METRE = 0.0254


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


def world_bounds() -> tuple[Vector, Vector]:
    points = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
    if not points:
        raise RuntimeError("scene has no mesh bounds")
    minimum = Vector(min(point[i] for point in points) for i in range(3))
    maximum = Vector(max(point[i] for point in points) for i in range(3))
    return minimum, maximum


def main() -> None:
    values = sys.argv[sys.argv.index("--") + 1 :]
    source_root, manifest_path, prop_root, importer_root = map(
        lambda value: Path(value).resolve(), values
    )
    sys.path.insert(0, str(importer_root))
    from cod_asset_importer import importer
    from cod_asset_importer.cod_asset_importer import GAME_VERSION

    payload = json.loads(manifest_path.read_text())
    for name in payload["modelAssets"]:
        clear_scene()
        importer.import_xmodel(
            asset_path=str(source_root),
            file_path=str(source_root / "xmodel" / name),
            selected_version=GAME_VERSION.CoD,
        )
        raw_minimum, raw_maximum = world_bounds()
        raw_size = raw_maximum - raw_minimum

        clear_scene()
        bpy.ops.import_scene.fbx(
            filepath=str(prop_root / name / f"{name}.fbx"),
            use_anim=False,
        )
        roots = [
            obj for obj in bpy.context.scene.objects if obj.parent is None
        ]
        material_images = []
        for material in bpy.data.materials:
            images = []
            if material.use_nodes and material.node_tree:
                images = [
                    Path(node.image.filepath).name
                    for node in material.node_tree.nodes
                    if node.type == "TEX_IMAGE" and node.image is not None
                ]
            material_images.append(
                f"{material.name}=>{','.join(sorted(set(images))) or 'NONE'}"
            )
        fbx_minimum, fbx_maximum = world_bounds()
        fbx_size = fbx_maximum - fbx_minimum
        expected = raw_size * COD_UNIT_TO_METRE
        ratios = [
            fbx_size[index] / expected[index]
            if expected[index] > 0.000001
            else 1.0
            for index in range(3)
        ]
        print(
            f"AUDIT {name}: raw_inches="
            f"({raw_size.x:.3f},{raw_size.y:.3f},{raw_size.z:.3f}), "
            f"expected_m=({expected.x:.3f},{expected.y:.3f},{expected.z:.3f}), "
            f"fbx=({fbx_size.x:.3f},{fbx_size.y:.3f},{fbx_size.z:.3f}), "
            f"ratio=({ratios[0]:.4f},{ratios[1]:.4f},{ratios[2]:.4f}), "
            f"roots=["
            + ", ".join(
                f"{root.name}:scale="
                f"({root.scale.x:.6f},{root.scale.y:.6f},{root.scale.z:.6f})"
                f":rotation="
                f"({root.rotation_euler.x:.6f},"
                f"{root.rotation_euler.y:.6f},"
                f"{root.rotation_euler.z:.6f})"
                for root in roots
            )
            + "], materials=["
            + "; ".join(material_images)
            + "]"
        )


if __name__ == "__main__":
    main()
