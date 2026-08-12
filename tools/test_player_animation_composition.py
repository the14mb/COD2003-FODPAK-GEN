"""Evaluate candidate CoD XAnim-to-bind-pose composition rules."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector


values = sys.argv[sys.argv.index("--") + 1 :]
source = Path(values[0]).resolve()
tools_root = Path(values[1]).resolve()
sys.path.insert(0, str(tools_root))

from cod1_xanim import load, sample


rig = next(
    obj for obj in bpy.context.scene.objects
    if obj.type == "ARMATURE"
)
animation = load(source / "xanim" / "pb_stand_alert")
tracks = {track.name: track for track in animation.bones}
if rig.animation_data is not None:
    rig.animation_data.action = None
rig.data.pose_position = "POSE"


def local_rest(bone) -> Matrix:
    if bone.parent is None:
        return bone.matrix_local.copy()
    return bone.parent.matrix_local.inverted() @ bone.matrix_local


def rotation_matrix(track, frame: int) -> Matrix:
    x, y, z, w = sample(track.rotation, frame, quaternion=True)
    return Quaternion((w, x, y, z)).to_matrix().to_4x4()


def apply_candidate(
    space_mode: str,
    translation_mode: str,
    rotation_mode: str,
    assignment_mode: str = "matrix",
) -> None:
    targets = {}
    for pose_bone in rig.pose.bones:
        rest = (
            pose_bone.bone.matrix_local.copy()
            if space_mode == "global"
            else local_rest(pose_bone.bone)
        )
        track = tracks.get(pose_bone.name.lower())
        local = rest.copy()
        if track is not None:
            if track.rotation is not None:
                source_rotation = rotation_matrix(track, 0).to_3x3()
                rest_rotation = rest.to_3x3()
                rotation = {
                    "absolute": source_rotation,
                    "pre_delta": source_rotation @ rest_rotation,
                    "post_delta": rest_rotation @ source_rotation,
                }[rotation_mode]
                local = rotation.to_4x4()
            if track.translation is not None:
                source_translation = Vector(
                    sample(track.translation, 0)
                )
                local.translation = (
                    source_translation
                    if translation_mode == "absolute"
                    else rest.translation + source_translation
                )
            else:
                local.translation = rest.translation
        parent_target = (
            targets[pose_bone.parent.name]
            if space_mode == "local" and pose_bone.parent is not None
            else Matrix.Identity(4)
        )
        target = parent_target @ local
        targets[pose_bone.name] = target
        if assignment_mode == "basis":
            bind_local = local_rest(pose_bone.bone)
            pose_bone.matrix_basis = (
                bind_local.inverted()
                @ parent_target.inverted()
                @ target
            )
        else:
            pose_bone.matrix = target
            # Matrix assignment derives matrix_basis from the parent's
            # evaluated pose. Refresh before assigning a child so later parent
            # changes do not invalidate the intended object-space target.
            bpy.context.view_layer.update()

    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    minimum = Vector((float("inf"),) * 3)
    maximum = Vector((float("-inf"),) * 3)
    centers = []
    for original in bpy.context.scene.objects:
        if original.type != "MESH":
            continue
        evaluated = original.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            points = [
                evaluated.matrix_world @ vertex.co
                for vertex in mesh.vertices
            ]
        finally:
            evaluated.to_mesh_clear()
        mesh_minimum = Vector(
            tuple(min(point[i] for point in points) for i in range(3))
        )
        mesh_maximum = Vector(
            tuple(max(point[i] for point in points) for i in range(3))
        )
        centers.append((mesh_minimum + mesh_maximum) * 0.5)
        minimum = Vector(
            tuple(min(a, b) for a, b in zip(minimum, mesh_minimum))
        )
        maximum = Vector(
            tuple(max(a, b) for a, b in zip(maximum, mesh_maximum))
        )
    centroid = sum(centers, Vector()) / len(centers)
    spread = max((center - centroid).length for center in centers)
    print(
        "CANDIDATE",
        assignment_mode,
        space_mode,
        translation_mode,
        rotation_mode,
        "size",
        tuple(round(value, 4) for value in maximum - minimum),
        "center",
        tuple(round(value, 4) for value in (minimum + maximum) * 0.5),
        "mesh_center_spread",
        round(spread, 4),
    )


for space in ("local", "global"):
    for translation in ("absolute", "delta"):
        for rotation in ("absolute", "pre_delta", "post_delta"):
            apply_candidate(space, translation, rotation)
apply_candidate("local", "delta", "absolute", "basis")
