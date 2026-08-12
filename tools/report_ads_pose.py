"""Report viewmodel root transforms and bounds for idle/ADS endpoints."""

import bpy
from mathutils import Vector


armature = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
armature.animation_data_create()
meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]


def bounds():
    graph = bpy.context.evaluated_depsgraph_get()
    minimum = Vector((float("inf"),) * 3)
    maximum = Vector((float("-inf"),) * 3)
    for original in meshes:
        evaluated = original.evaluated_get(graph)
        for corner in evaluated.bound_box:
            point = evaluated.matrix_world @ Vector(corner)
            minimum = Vector(tuple(min(a, b) for a, b in zip(minimum, point)))
            maximum = Vector(tuple(max(a, b) for a, b in zip(maximum, point)))
    return minimum, maximum


for action in bpy.data.actions:
    lowered = action.name.lower()
    if not any(token in lowered for token in ("idle", "ads_up", "ads_down")):
        continue
    armature.animation_data.action = action
    first, last = action.frame_range
    for label, frame in (("FIRST", int(first)), ("LAST", int(last))):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        minimum, maximum = bounds()
        print(
            "ADS_POSE",
            action.name,
            label,
            frame,
            "tag_view_basis",
            tuple(round(value, 5) for row in armature.pose.bones["tag_view"].matrix_basis for value in row),
            "tag_weapon",
            tuple(round(value, 5) for row in armature.pose.bones["tag_weapon"].matrix for value in row),
            "bounds_center",
            tuple(round(value, 5) for value in (minimum + maximum) * .5),
            "bounds_size",
            tuple(round(value, 5) for value in maximum - minimum),
        )
