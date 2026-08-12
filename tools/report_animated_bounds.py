import bpy
from mathutils import Vector

armature = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
print(
    "RIG",
    armature.name,
    "bones",
    len(armature.data.bones),
    "meshes",
    len([obj for obj in bpy.context.scene.objects if obj.type == "MESH"]),
)
for action in bpy.data.actions:
    armature.animation_data.action = action
    first, last = action.frame_range
    for frame in sorted({int(first), round((first + last) * 0.5), int(last)}):
        bpy.context.scene.frame_set(frame)
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        mins = Vector((float("inf"),) * 3)
        maxs = Vector((float("-inf"),) * 3)
        for original in bpy.context.scene.objects:
            if original.type != "MESH":
                continue
            obj = original.evaluated_get(depsgraph)
            for corner in obj.bound_box:
                point = obj.matrix_world @ Vector(corner)
                mins = Vector(tuple(min(a, b) for a, b in zip(mins, point)))
                maxs = Vector(tuple(max(a, b) for a, b in zip(maxs, point)))
        print(action.name, frame, "size", tuple(round(v, 4) for v in maxs - mins),
              "min", tuple(round(v, 4) for v in mins), "max", tuple(round(v, 4) for v in maxs))
