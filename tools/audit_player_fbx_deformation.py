"""Audit evaluated per-mesh bounds after importing a multiplayer player FBX."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy
from mathutils import Vector


def arguments() -> Path:
    values = sys.argv[sys.argv.index("--") + 1 :]
    if len(values) != 1:
        raise SystemExit("expected PLAYER_FBX")
    return Path(values[0]).resolve()


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (
        bpy.data.actions,
        bpy.data.meshes,
        bpy.data.armatures,
        bpy.data.materials,
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
        zero = Vector()
        return zero, zero
    return (
        Vector(tuple(min(point[i] for point in points) for i in range(3))),
        Vector(tuple(max(point[i] for point in points) for i in range(3))),
    )


def report_pose(label: str) -> None:
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    global_min = Vector((float("inf"),) * 3)
    global_max = Vector((float("-inf"),) * 3)
    print(f"POSE {label}")
    for obj in sorted(
        (item for item in bpy.context.scene.objects if item.type == "MESH"),
        key=lambda item: item.name,
    ):
        minimum, maximum = evaluated_bounds(obj, depsgraph)
        raw_points = [
            obj.matrix_world @ vertex.co
            for vertex in obj.data.vertices
        ]
        raw_minimum = Vector(
            tuple(min(point[i] for point in raw_points) for i in range(3))
        )
        raw_maximum = Vector(
            tuple(max(point[i] for point in raw_points) for i in range(3))
        )
        raw_center = (raw_minimum + raw_maximum) * 0.5
        center = (minimum + maximum) * 0.5
        size = maximum - minimum
        global_min = Vector(
            tuple(min(a, b) for a, b in zip(global_min, minimum))
        )
        global_max = Vector(
            tuple(max(a, b) for a, b in zip(global_max, maximum))
        )
        modifiers = [
            modifier.object.name if modifier.object else "NONE"
            for modifier in obj.modifiers
            if modifier.type == "ARMATURE"
        ]
        print(
            "MESH",
            obj.name,
            "center",
            tuple(round(value, 4) for value in center),
            "size",
            tuple(round(value, 4) for value in size),
            "raw_center",
            tuple(round(value, 4) for value in raw_center),
            "parent",
            obj.parent.name if obj.parent else "NONE",
            "armatures",
            modifiers,
        )
    print(
        "GLOBAL",
        "center",
        tuple(round(value, 4) for value in (global_min + global_max) * 0.5),
        "size",
        tuple(round(value, 4) for value in global_max - global_min),
        "min",
        tuple(round(value, 4) for value in global_min),
        "max",
        tuple(round(value, 4) for value in global_max),
    )


def main() -> None:
    path = arguments()
    clear_scene()
    bpy.ops.import_scene.fbx(filepath=str(path), use_anim=True)
    armatures = [
        obj for obj in bpy.context.scene.objects
        if obj.type == "ARMATURE"
    ]
    print(
        "IMPORT",
        path,
        "armatures",
        len(armatures),
        "meshes",
        len([
            obj for obj in bpy.context.scene.objects
            if obj.type == "MESH"
        ]),
        "actions",
        len(bpy.data.actions),
    )
    for rig in armatures:
        rig.data.pose_position = "REST"
        if rig.animation_data is not None:
            rig.animation_data.action = None
    report_pose("REST")
    if armatures and bpy.data.actions:
        rig = armatures[0]
        rig.data.pose_position = "POSE"
        rig.animation_data_create()
        action = next(
            (
                item for item in bpy.data.actions
                if "pb_stand_alert" in item.name
                and "pistol" not in item.name
            ),
            bpy.data.actions[0],
        )
        rig.animation_data.action = action
        bpy.context.scene.frame_set(int(action.frame_range[0]))
        report_pose(f"{action.name}@{int(action.frame_range[0])}")


if __name__ == "__main__":
    main()
