import sys
from mathutils import Vector
import bpy

mins = Vector((float("inf"),) * 3)
maxs = Vector((float("-inf"),) * 3)
for obj in bpy.context.scene.objects:
    if obj.type != "MESH":
        continue
    for corner in obj.bound_box:
        point = obj.matrix_world @ Vector(corner)
        mins.x, mins.y, mins.z = min(mins.x, point.x), min(mins.y, point.y), min(mins.z, point.z)
        maxs.x, maxs.y, maxs.z = max(maxs.x, point.x), max(maxs.y, point.y), max(maxs.z, point.z)
print(f"MODEL_BOUNDS min={tuple(mins)} max={tuple(maxs)} size={tuple(maxs-mins)}")
