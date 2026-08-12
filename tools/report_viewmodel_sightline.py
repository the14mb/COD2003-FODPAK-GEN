"""Report camera-space attachment positions at authored hip/ADS endpoints."""

import bpy


armature = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
armature.animation_data_create()

for token in ("ads_down", "ads_up"):
    action = next(action for action in bpy.data.actions if token in action.name.lower())
    armature.animation_data.action = action
    frame = int(action.frame_range[1])
    bpy.context.scene.frame_set(frame)
    bpy.context.view_layer.update()
    print("SIGHTLINE", token, action.name, frame)
    for name in ("tag_torso", "tag_weapon", "tag_flash"):
        bone = armature.pose.bones.get(name)
        if bone is None:
            continue
        matrix = armature.matrix_world @ bone.matrix
        position = matrix.translation
        print(name, tuple(round(value, 6) for value in position))
        if name == "tag_flash":
            for axis_name, axis in zip("xyz", matrix.to_3x3().col):
                direction = axis.normalized()
                closest = position - direction * position.dot(direction)
                print(
                    "axis_" + axis_name,
                    tuple(round(value, 6) for value in direction),
                    "line_origin_distance",
                    round(closest.length, 6),
                    "closest",
                    tuple(round(value, 6) for value in closest),
                )
