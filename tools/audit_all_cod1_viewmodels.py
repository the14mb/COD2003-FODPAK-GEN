"""Blender background audit for every combined CoD 1 viewmodel export."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


root = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
manifest_data = json.loads((root / "manifest.json").read_text())
weapons = manifest_data["weapons"] if isinstance(manifest_data, dict) else manifest_data
failures = []


def has_clip(actions, token: str) -> bool:
    token = token.lower()
    return any(
        action.name.lower() == token or action.name.lower().endswith("_" + token)
        for action in actions
    )


def is_detachable_prop(mesh) -> bool:
    """CoD hides consumed clips/rounds/rockets by translating them off-camera."""
    groups = " ".join(group.name.lower() for group in mesh.vertex_groups)
    return any(token in groups for token in ("clip", "bullet", "rocket"))


for weapon in weapons:
    name = weapon["name"]
    blend_path = root / name / f"{name}.blend"
    bpy.ops.wm.open_mainfile(filepath=str(blend_path))

    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    actions = list(bpy.data.actions)
    issues = []
    if len(armatures) != 1:
        issues.append(f"{len(armatures)} armatures")
    if len(meshes) < 5:
        issues.append(f"only {len(meshes)} meshes")
    if armatures and armatures[0].data.bones.get("tag_weapon") is None:
        issues.append("missing tag_weapon")
    if len(actions) != len(weapon["animations"]):
        issues.append(
            f"{len(actions)} actions, manifest expects {len(weapon['animations'])}"
        )
    for required in ("idle", "fire", "ads_up", "ads_down"):
        if not has_clip(actions, required):
            issues.append(f"missing {required}")

    largest_dimension = 0.0
    largest_pose = ""
    largest_action = None
    largest_frame = 0
    if armatures:
        armature = armatures[0]
        armature.animation_data_create()
        for action in actions:
            armature.animation_data.action = action
            first, last = action.frame_range
            for frame in {int(first), round((first + last) * .5), int(last)}:
                bpy.context.scene.frame_set(frame)
                bpy.context.view_layer.update()
                graph = bpy.context.evaluated_depsgraph_get()
                minimum = Vector((float("inf"),) * 3)
                maximum = Vector((float("-inf"),) * 3)
                for original in meshes:
                    if is_detachable_prop(original):
                        continue
                    evaluated = original.evaluated_get(graph)
                    for corner in evaluated.bound_box:
                        point = evaluated.matrix_world @ Vector(corner)
                        minimum = Vector(
                            tuple(min(a, b) for a, b in zip(minimum, point))
                        )
                        maximum = Vector(
                            tuple(max(a, b) for a, b in zip(maximum, point))
                        )
                size = maximum - minimum
                if not all(math.isfinite(value) for value in size):
                    issues.append(f"{action.name} has non-finite bounds")
                    continue
                frame_dimension = max(size)
                if frame_dimension > largest_dimension:
                    largest_dimension = frame_dimension
                    largest_pose = (
                        f"{action.name}@{frame} "
                        f"size={tuple(round(value, 3) for value in size)}"
                    )
                    largest_action = action
                    largest_frame = frame

    if largest_dimension <= .05 or largest_dimension > 4.0:
        issues.append(
            f"implausible animated bound {largest_dimension:.3f} m ({largest_pose})"
        )
        if armatures and largest_action is not None:
            armature.animation_data.action = largest_action
            bpy.context.scene.frame_set(largest_frame)
            bpy.context.view_layer.update()
            graph = bpy.context.evaluated_depsgraph_get()
            for original in meshes:
                if is_detachable_prop(original):
                    continue
                evaluated = original.evaluated_get(graph)
                corners = [
                    evaluated.matrix_world @ Vector(corner)
                    for corner in evaluated.bound_box
                ]
                minimum = Vector(
                    tuple(min(point[axis] for point in corners) for axis in range(3))
                )
                maximum = Vector(
                    tuple(max(point[axis] for point in corners) for axis in range(3))
                )
                center = (minimum + maximum) * .5
                size = maximum - minimum
                groups = [
                    group.name for group in original.vertex_groups
                    if any(
                        membership.group == group.index
                        for vertex in original.data.vertices
                        for membership in vertex.groups
                    )
                ]
                print(
                    "MESH_POSE",
                    name,
                    original.name,
                    "center",
                    tuple(round(value, 3) for value in center),
                    "size",
                    tuple(round(value, 3) for value in size),
                    "groups",
                    groups,
                )
    if issues:
        failures.append(f"{name}: " + "; ".join(issues))
        print("AUDIT_FAIL", failures[-1])
    else:
        print(
            "AUDIT_OK",
            name,
            f"bones={len(armatures[0].data.bones)}",
            f"meshes={len(meshes)}",
            f"actions={len(actions)}",
            f"max_bound={largest_dimension:.3f}m",
            f"pose={largest_pose}",
        )

if failures:
    raise SystemExit("\n".join(failures))
print(f"AUDIT_COMPLETE {len(weapons)} combined viewmodels passed")
