"""Print the parent chain of selected bones in the currently open Blender file."""
import sys

import bpy


targets = {value.lower() for value in sys.argv[sys.argv.index("--") + 1:]}
for armature in (obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"):
    print("ARMATURE", armature.name)
    for pose_bone in armature.pose.bones:
        if targets and pose_bone.name.lower() not in targets:
            continue
        chain = []
        current = pose_bone
        while current is not None:
            chain.append(current.name)
            current = current.parent
        print("BONE_CHAIN", " <- ".join(chain))
