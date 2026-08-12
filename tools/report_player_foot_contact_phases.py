import sys

import bpy


TOKENS = (
    "pb_stand_shoot_walk_forward_unarmed",
    "pb_crouch_walk_forward_unarmed",
    "pb_combatrun_forward_loop_unarmed",
)
FOOT_BONES = ("bip01 l foot", "bip01 r foot")


def local_minima(values):
    found = []
    count = len(values)
    for index in range(1, count - 1):
        previous = values[index - 1][1]
        current = values[index][1]
        following = values[index + 1][1]
        if current <= previous and current < following:
            found.append(values[index])
    return found


armature = next(
    (obj for obj in bpy.data.objects if obj.type == "ARMATURE"),
    None,
)
if armature is None:
    raise RuntimeError("No armature found")

scene = bpy.context.scene
for token in TOKENS:
    action = next(
        (
            candidate
            for candidate in bpy.data.actions
            if token.lower() in candidate.name.lower()
        ),
        None,
    )
    if action is None:
        print(f"FOOT_PHASE action={token} missing=true")
        continue

    armature.animation_data_create()
    armature.animation_data.action = action
    first = int(round(action.frame_range[0]))
    last = int(round(action.frame_range[1]))
    span = max(1, last - first)
    samples = {name: [] for name in FOOT_BONES}
    for frame in range(first, last + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        normalized = (frame - first) / span
        for name in FOOT_BONES:
            bone = armature.pose.bones.get(name)
            if bone is None:
                continue
            world = armature.matrix_world @ bone.matrix
            samples[name].append((normalized, world.translation.z))

    for name in FOOT_BONES:
        values = samples[name]
        minima = local_minima(values)
        absolute = min(values, key=lambda item: item[1]) if values else None
        print(
            f"FOOT_PHASE action={action.name} bone={name} "
            f"frames={first}-{last} minima="
            f"{[(round(phase, 4), round(height, 5)) for phase, height in minima]} "
            f"absolute="
            f"{(round(absolute[0], 4), round(absolute[1], 5)) if absolute else None}"
        )

armature.animation_data.action = None
