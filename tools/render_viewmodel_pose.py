"""Render idle and ADS endpoint poses from a combined CoD1 viewmodel.

Run after opening a generated ``*_combined.blend`` file:
    blender --background model.blend --python tools/render_viewmodel_pose.py -- output_prefix
"""

import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector


arguments = sys.argv[sys.argv.index("--") + 1 :]
if len(arguments) != 1:
    raise SystemExit("expected output prefix")
output_prefix = Path(arguments[0]).resolve()

armature = next(obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE")
armature.animation_data_create()

camera_data = bpy.data.cameras.new("CoD View Camera")
camera_data.type = "PERSP"
camera_data.lens_unit = "FOV"
camera_data.sensor_fit = "HORIZONTAL"
camera_data.angle = math.radians(80.0)
camera_data.clip_start = 0.001
camera_data.clip_end = 10.0
camera = bpy.data.objects.new("CoD View Camera", camera_data)
bpy.context.scene.collection.objects.link(camera)
camera.location = Vector((0.0, 0.0, 0.0))
# CoD uses +X forward, -Y screen-right and +Z up.
camera.rotation_mode = "XYZ"
camera.rotation_euler = (math.radians(90.0), 0.0, math.radians(-90.0))

scene = bpy.context.scene
scene.camera = camera
scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "MATERIAL"
scene.display.shading.show_shadows = True
scene.display.shading.show_cavity = True
scene.display.shading.cavity_type = "WORLD"
scene.render.resolution_x = 1280
scene.render.resolution_y = 720
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.film_transparent = False


def find_action(token):
    token = token.lower()
    return next(
        action for action in bpy.data.actions
        if token in action.name.lower()
    )


for label, action, use_last_frame in (
    ("hip", find_action("ads_down"), True),
    ("ads", find_action("ads_up"), True),
):
    armature.animation_data.action = action
    frame = int(action.frame_range[1] if use_last_frame else action.frame_range[0])
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    scene.render.filepath = str(output_prefix.with_name(
        f"{output_prefix.name}_{label}.png"
    ))
    bpy.ops.render.render(write_still=True)
    print("VIEWMODEL_RENDER", label, action.name, frame, scene.render.filepath)
