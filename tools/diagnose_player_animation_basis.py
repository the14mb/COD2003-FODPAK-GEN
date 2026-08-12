"""Compare CoD animation targets with Blender rest and evaluated bone poses."""

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


def track_matrix(track, frame: int) -> Matrix:
    x, y, z, w = sample(track.rotation, frame, quaternion=True)
    matrix = Quaternion((w, x, y, z)).to_matrix().to_4x4()
    matrix.translation = Vector(sample(track.translation, frame))
    return matrix


rig = next(
    obj for obj in bpy.context.scene.objects
    if obj.type == "ARMATURE"
)
animation = load(source / "xanim" / "pb_stand_alert")
tracks = {track.name: track for track in animation.bones}
targets = {}
for pose_bone in rig.pose.bones:
    track = tracks.get(pose_bone.name.lower())
    if track is None:
        continue
    local = track_matrix(track, 0)
    parent_target = (
        targets.get(pose_bone.parent.name, pose_bone.parent.matrix)
        if pose_bone.parent
        else Matrix.Identity(4)
    )
    targets[pose_bone.name] = parent_target @ local

action = next(
    item for item in bpy.data.actions
    if item.name == "pb_stand_alert"
)
rig.animation_data.action = action
bpy.context.scene.frame_set(0)
bpy.context.view_layer.update()

names = (
    "bip01",
    "bip01 pelvis",
    "bip01 spine",
    "bip01 spine2",
    "bip01 head",
    "bip01 l upperarm",
    "bip01 l hand",
    "bip01 r hand",
    "bip01 l thigh",
    "bip01 l foot",
)
for name in names:
    pose_bone = rig.pose.bones.get(name)
    if pose_bone is None:
        continue
    target = targets.get(name)
    print(
        "BONE",
        name,
        "rest",
        tuple(round(value, 5) for value in pose_bone.bone.matrix_local.translation),
        "target",
        tuple(round(value, 5) for value in target.translation)
        if target is not None else "NONE",
        "evaluated",
        tuple(round(value, 5) for value in pose_bone.matrix.translation),
        "basis",
        tuple(round(value, 5) for value in pose_bone.matrix_basis.translation),
    )
