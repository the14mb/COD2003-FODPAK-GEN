"""Blender diagnostic for CoD 1 viewmodel animation coordinate conventions."""
import sys
from pathlib import Path

import bpy

args = sys.argv[sys.argv.index("--") + 1:]
source, importer_root, tools_root = map(lambda value: Path(value).resolve(), args)
sys.path.insert(0, str(importer_root))
sys.path.insert(0, str(tools_root))

from cod_asset_importer import importer
from cod_asset_importer.cod_asset_importer import GAME_VERSION
from cod1_xanim import load, sample

importer.import_xmodel(
    asset_path=str(source),
    file_path=str(source / "xmodel/viewmodel_hands_sten"),
    selected_version=GAME_VERSION.CoD,
)
armature = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
animation = load(source / "xanim/viewmodel_mp40_idle")
tracks = {track.name: track for track in animation.bones}

print("BONE_DIAGNOSTIC_BEGIN")
for bone in armature.data.bones:
    track = tracks.get(bone.name.lower())
    if track is None:
        continue
    translation = sample(track.translation, 0)
    rotation = sample(track.rotation, 0, True)
    parent = bone.parent.name if bone.parent else "-"
    print(
        bone.name, "parent=", parent,
        "rest_head=", tuple(round(v, 4) for v in bone.head_local),
        "anim_pos=", tuple(round(v, 4) for v in translation),
        "quat_xyzw=", tuple(round(v, 5) for v in rotation),
    )
print("BONE_DIAGNOSTIC_END")
for obj in bpy.context.scene.objects:
    if obj.type == "MESH":
        print("MESH", obj.name, len(obj.data.vertices),
              [material.name for material in obj.data.materials])
