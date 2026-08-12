"""Report armature and mesh compatibility for CoD 1 multiplayer player parts."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
importer_root = Path(sys.argv[sys.argv.index("--") + 2]).resolve()
names = sys.argv[sys.argv.index("--") + 3 :]
sys.path.insert(0, str(importer_root))

from cod_asset_importer import importer
from cod_asset_importer.cod_asset_importer import GAME_VERSION


for name in names:
    before = set(bpy.context.scene.objects)
    importer.import_xmodel(
        asset_path=str(source),
        file_path=str(source / "xmodel" / name),
        selected_version=GAME_VERSION.CoD,
    )
    objects = [obj for obj in bpy.context.scene.objects if obj not in before]
    armatures = [obj for obj in objects if obj.type == "ARMATURE"]
    meshes = [obj for obj in objects if obj.type == "MESH"]
    print(f"PART {name}: armatures={len(armatures)} meshes={len(meshes)}")
    for armature in armatures:
        bones = [bone.name for bone in armature.data.bones]
        print(f"  ARMATURE {armature.name}: bones={len(bones)} {bones}")
    for mesh in meshes:
        groups = [group.name for group in mesh.vertex_groups]
        modifiers = [
            modifier.object.name
            for modifier in mesh.modifiers
            if modifier.type == "ARMATURE" and modifier.object is not None
        ]
        print(
            f"  MESH {mesh.name}: vertices={len(mesh.data.vertices)} "
            f"groups={groups} modifiers={modifiers}"
        )
