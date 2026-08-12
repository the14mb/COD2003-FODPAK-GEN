"""Validate every exported CoD 1 multiplayer avatar and animation pose."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


# Every direct animation actively referenced by the official multiplayer
# playeranim.script is exported, and its mounted-MG42 ``turretanim`` aliases
# are expanded to the real directional gunner clips, which resolves 354
# XAnims. Campaign, duplicate pb_* MG42, and weapon-rig ``*MG42gun_*`` clips
# are deliberately outside this player inventory, as is every jeep clip the
# script binds -- turret family or passenger: this product ships no vehicle.
EXPECTED_ACTION_COUNTS = frozenset((354,))
MAX_CHARACTER_EXTENT = 3.0
MAX_HEADWEAR_GAP = 0.45


def clear_scene() -> None:
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (
        bpy.data.actions,
        bpy.data.meshes,
        bpy.data.armatures,
        bpy.data.materials,
        bpy.data.images,
    ):
        for item in list(collection):
            collection.remove(item)


def evaluated_bounds(obj, depsgraph) -> tuple[Vector, Vector]:
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        points = [
            evaluated.matrix_world @ vertex.co
            for vertex in mesh.vertices
        ]
    finally:
        evaluated.to_mesh_clear()
    if not points:
        raise RuntimeError(f"{obj.name}: empty evaluated mesh")
    return (
        Vector(tuple(min(point[axis] for point in points) for axis in range(3))),
        Vector(tuple(max(point[axis] for point in points) for axis in range(3))),
    )


def pose_metrics(meshes) -> tuple[Vector, dict[str, Vector]]:
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    minimum = Vector((float("inf"),) * 3)
    maximum = Vector((float("-inf"),) * 3)
    centers = {}
    for mesh in meshes:
        mesh_minimum, mesh_maximum = evaluated_bounds(mesh, depsgraph)
        centers[mesh.name] = (mesh_minimum + mesh_maximum) * 0.5
        minimum = Vector(
            tuple(min(a, b) for a, b in zip(minimum, mesh_minimum))
        )
        maximum = Vector(
            tuple(max(a, b) for a, b in zip(maximum, mesh_maximum))
        )
    return maximum - minimum, centers


def validate_pose(
    player_id: str,
    label: str,
    meshes,
    head_names: list[str],
    headwear_names: list[str],
) -> Vector:
    size, centers = pose_metrics(meshes)
    if not all(math.isfinite(value) for value in size):
        raise RuntimeError(f"{player_id} {label}: non-finite bounds {tuple(size)}")
    if max(size) > MAX_CHARACTER_EXTENT or min(size) <= 0.05:
        raise RuntimeError(
            f"{player_id} {label}: invalid character bounds "
            f"{tuple(round(value, 4) for value in size)}"
        )
    if head_names and headwear_names:
        gap = min(
            (centers[head] - centers[headwear]).length
            for head in head_names
            for headwear in headwear_names
        )
        if gap > MAX_HEADWEAR_GAP:
            raise RuntimeError(
                f"{player_id} {label}: detached headwear gap {gap:.4f}m"
            )
    return size


def validate_player(path: Path) -> None:
    clear_scene()
    bpy.ops.import_scene.fbx(filepath=str(path), use_anim=True)
    rigs = [
        obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"
    ]
    meshes = [
        obj for obj in bpy.context.scene.objects if obj.type == "MESH"
    ]
    actions = list(bpy.data.actions)
    if len(rigs) != 1:
        raise RuntimeError(f"{path.stem}: expected one armature, found {len(rigs)}")
    if len(actions) not in EXPECTED_ACTION_COUNTS:
        raise RuntimeError(
            f"{path.stem}: expected one complete MP action tier "
            f"{sorted(EXPECTED_ACTION_COUNTS)}, "
            f"found {len(actions)}"
        )
    if not meshes:
        raise RuntimeError(f"{path.stem}: no meshes")

    player_id = path.stem
    head_names = [
        mesh.name for mesh in meshes if mesh.name.lower().startswith("basehead")
    ]
    headwear_names = [
        mesh.name
        for mesh in meshes
        if any(
            token in mesh.name.lower()
            for token in ("helmet", "sidecap")
        )
    ]
    if not head_names or not headwear_names:
        raise RuntimeError(
            f"{player_id}: missing head/headwear meshes "
            f"({len(head_names)}/{len(headwear_names)})"
        )

    rig = rigs[0]
    rig.data.pose_position = "REST"
    rig.animation_data_create()
    rig.animation_data.action = None
    rest_size = validate_pose(
        player_id,
        "REST",
        meshes,
        head_names,
        headwear_names,
    )

    sampled = 0
    max_extent = max(rest_size)
    for action in actions:
        rig.data.pose_position = "POSE"
        rig.animation_data.action = action
        first = int(action.frame_range[0])
        last = int(action.frame_range[1])
        frames = sorted({first, (first + last) // 2, last})
        for frame in frames:
            bpy.context.scene.frame_set(frame)
            size = validate_pose(
                player_id,
                f"{action.name}@{frame}",
                meshes,
                head_names,
                headwear_names,
            )
            max_extent = max(max_extent, max(size))
            sampled += 1

    print(
        f"PASS {player_id}: meshes={len(meshes)} actions={len(actions)} "
        f"poses={sampled + 1} max_extent={max_extent:.4f}m"
    )


def main() -> None:
    root = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    paths = sorted(root.glob("*/*.fbx"))
    if not paths:
        raise SystemExit(f"no player FBXs found under {root}")
    for path in paths:
        validate_player(path)
    print(f"PASS all multiplayer players: {len(paths)}")


if __name__ == "__main__":
    main()
