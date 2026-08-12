"""Render an imported multiplayer FBX pose for deformation inspection."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy
from mathutils import Vector


values = sys.argv[sys.argv.index("--") + 1 :]
fbx_path = Path(values[0]).resolve()
output_path = Path(values[1]).resolve()
close_head = len(values) > 2 and values[2].lower() == "head"

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.fbx(filepath=str(fbx_path), use_anim=True)

rig = next(
    obj for obj in bpy.context.scene.objects
    if obj.type == "ARMATURE"
)
action = next(
    item for item in bpy.data.actions
    if "pb_stand_alert" in item.name and "pistol" not in item.name
)
rig.animation_data_create()
rig.animation_data.action = action
bpy.context.scene.frame_set(int(action.frame_range[0]))

bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, -0.015))
ground = bpy.context.object
ground.name = "Audit Ground"
material = bpy.data.materials.new("Audit Ground")
material.diffuse_color = (0.11, 0.12, 0.14, 1.0)
ground.data.materials.append(material)

bpy.ops.object.light_add(type="AREA", location=(2.5, -3.0, 5.0))
key = bpy.context.object
key.data.energy = 900
key.data.shape = "DISK"
key.data.size = 4
bpy.ops.object.light_add(type="AREA", location=(-3.0, 1.0, 2.5))
fill = bpy.context.object
fill.data.energy = 500
fill.data.size = 3

bpy.ops.object.camera_add(
    location=(1.0, -1.45, 1.72)
    if close_head
    else (3.4, -5.5, 2.35)
)
camera = bpy.context.object
target = Vector((0.0, 0.0, 1.48 if close_head else 0.9))
camera.rotation_euler = (target - camera.location).to_track_quat(
    "-Z",
    "Y",
).to_euler()
camera.data.lens = 72 if close_head else 62

scene = bpy.context.scene
scene.camera = camera
scene.render.engine = "BLENDER_EEVEE_NEXT"
scene.render.resolution_x = 800
scene.render.resolution_y = 800
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = str(output_path)
scene.render.film_transparent = False
scene.world.color = (0.025, 0.03, 0.04)
bpy.ops.render.render(write_still=True)
print("RENDERED", output_path)
