"""Open a hand blend, import its matching gun FBX, and audit synchronized poses."""
import sys
from pathlib import Path

import bpy
from mathutils import Vector

gun_fbx = Path(sys.argv[sys.argv.index("--") + 1]).resolve()

# The hands and gun intentionally use the same source animation names. Prefix the
# actions already present in the hands .blend so FBX import cannot resolve the
# gun takes back to those existing datablocks.
hand_actions = set(bpy.data.actions)
for action in hand_actions:
    action.name = f"hands__{action.name}"

bpy.ops.import_scene.fbx(filepath=str(gun_fbx), use_anim=True)
gun_actions = [action for action in bpy.data.actions if action not in hand_actions]

# Blender imports FBX takes starting at frame 1. Move the gun curves back to the
# source frame origin so one scene frame evaluates the matching hands/gun pose.
for action in gun_actions:
    offset = action.frame_range[0]
    if offset:
        for fcurve in action.fcurves:
            for point in fcurve.keyframe_points:
                point.co.x -= offset
                point.handle_left.x -= offset
                point.handle_right.x -= offset

armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
hand = next(obj for obj in armatures if "hands" in obj.name.lower())
gun = next(obj for obj in armatures if "hands" not in obj.name.lower())


def action_for(armature, token, matching_span=None):
    token_actions = [
        action for action in bpy.data.actions
        if token in action.name.lower()
    ]
    candidates = [
        action for action in token_actions
        if any(group.name in armature.pose.bones for group in action.groups)
    ]
    if not candidates and matching_span is not None:
        candidates = [
            action for action in gun_actions
            if abs((action.frame_range[1] - action.frame_range[0]) - matching_span) < 0.01
            and any(group.name in armature.pose.bones for group in action.groups)
        ]
    if not candidates:
        details = [
            f"{action.name} groups={[group.name for group in action.groups]}"
            for action in token_actions
        ]
        raise RuntimeError(
            f"{armature.name}: no {token} action; pose bones="
            f"{list(armature.pose.bones.keys())}; token actions={details}; "
            f"all actions={[(action.name, tuple(action.frame_range)) for action in bpy.data.actions]}"
        )
    return candidates[0]


for token in ("idle", "fire", "reload", "pullout"):
    hand_action = action_for(hand, token)
    hand_span = hand_action.frame_range[1] - hand_action.frame_range[0]
    gun_action = action_for(gun, token, matching_span=hand_span)
    hand.animation_data.action = hand_action
    gun.animation_data.action = gun_action
    duration = min(
        hand_action.frame_range[1] - hand_action.frame_range[0],
        gun_action.frame_range[1] - gun_action.frame_range[0],
    )
    for frame in (0, round(duration * 0.5), round(duration)):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        groups = {}
        for label, armature in (("hands", hand), ("gun", gun)):
            mins = Vector((float("inf"),) * 3)
            maxs = Vector((float("-inf"),) * 3)
            for original in bpy.context.scene.objects:
                if original.type != "MESH" or original.find_armature() != armature:
                    continue
                obj = original.evaluated_get(depsgraph)
                for corner in obj.bound_box:
                    point = obj.matrix_world @ Vector(corner)
                    mins = Vector(tuple(min(a, b) for a, b in zip(mins, point)))
                    maxs = Vector(tuple(max(a, b) for a, b in zip(maxs, point)))
            groups[label] = (mins, maxs)
        all_min = Vector(tuple(min(groups["hands"][0][i], groups["gun"][0][i]) for i in range(3)))
        all_max = Vector(tuple(max(groups["hands"][1][i], groups["gun"][1][i]) for i in range(3)))
        separation = ((groups["hands"][0] + groups["hands"][1]) * 0.5 -
                      (groups["gun"][0] + groups["gun"][1]) * 0.5).length
        print(
            "PAIR_AUDIT", token, frame,
            "union_size=", tuple(round(v, 4) for v in all_max - all_min),
            "center_separation=", round(separation, 4),
        )
