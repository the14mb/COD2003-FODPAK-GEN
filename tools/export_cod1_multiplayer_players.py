"""Export original CoD 1 multiplayer avatars with their shared pb_* animations.

The game assembles a player from a body, head, headwear, and optional equipment.
This exporter performs the same assembly on one skeleton and writes Unity-ready
FBX files with the original textures and multiplayer animation clips.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fod_export_common import (
    build_material_table,
    export_skinned_glb,
    load_json,
    parse_cli,
    write_json,
)


def arguments() -> tuple[Path, Path, Path, Path, str, Path | None, set[str]]:
    values = sys.argv[sys.argv.index("--") + 1 :]
    positionals, export_format, pak_root, _models = parse_cli(
        values,
        4,
        "expected SOURCE OUTPUT IMPORTER_PY_ROOT TOOLS_ROOT "
        "[--format fbx|glb] [--pak-root DIR] [PLAYER_ID...]",
    )
    paths = tuple(Path(value).resolve() for value in positionals[:4])
    return (*paths, export_format, pak_root, set(positionals[4:]))


SOURCE, OUTPUT, IMPORTER_ROOT, TOOLS_ROOT, EXPORT_FORMAT, PAK_ROOT, SELECTED_IDS = (
    arguments()
)
sys.path.insert(0, str(IMPORTER_ROOT))
sys.path.insert(0, str(TOOLS_ROOT))

from cod_asset_importer import importer
from cod_asset_importer.cod_asset_importer import GAME_VERSION
from cod1_xanim import load, sample
from cod1_playeranim import build_inventory


# Package faction ids consumed by the runtime player catalog.
FACTIONS = {
    "american_airborne": "allies",
    "british_airborne": "allies",
    "russian_conscript": "allies",
    "german_wehrmacht": "axis",
}


PROFILES = (
    {
        "id": "american_airborne",
        "display_name": "American Airborne",
        "body": "playerbody_american_airborne",
        "head": "basehead_01",
        "headwear": "USAirborneHelmet",
        "gear": ("gear_US_load1", "gear_US_bandolier", "gear_US_ammobelt"),
        "rigid_gear_bones": {},
    },
    {
        "id": "british_airborne",
        "display_name": "British Airborne",
        "body": "playerbody_british_airborne",
        "head": "basehead2_01",
        "headwear": "helmet_british_airborne",
        "gear": ("gear_british_air",),
        "rigid_gear_bones": {},
    },
    {
        "id": "russian_conscript",
        "display_name": "Russian Conscript",
        "body": "playerbody_russian_conscript",
        "head": "basehead3_01",
        "headwear": "sovietequipment_sidecap",
        "gear": ("gear_russian_load_ocoat", "gear_russian_ppsh_ocoat"),
        "rigid_gear_bones": {
            "gear_russian_load_ocoat": "bip01 spine",
        },
    },
    {
        "id": "german_wehrmacht",
        "display_name": "German Wehrmacht",
        "body": "playerbody_german_wehrmacht",
        "head": "basehead_axis_a_01",
        "headwear": "germanhelmet",
        "gear": ("gear_german_load1_w", "gear_german_k98_w"),
        "rigid_gear_bones": {},
    },
)

PLAYER_ANIMATION_INVENTORY = build_inventory(
    SOURCE / "mp" / "playeranim.script",
    SOURCE / "xanim",
)
ANIMATION_REFERENCES = PLAYER_ANIMATION_INVENTORY.animations

METRES_PER_INCH = 0.0254


def clear_scene() -> None:
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (
        bpy.data.actions,
        bpy.data.meshes,
        bpy.data.armatures,
        bpy.data.materials,
        bpy.data.images,
    ):
        for item in list(collection):
            collection.remove(item)


def import_part(name: str) -> tuple[list, list]:
    existing = set(bpy.context.scene.objects)
    importer.import_xmodel(
        asset_path=str(SOURCE),
        file_path=str(SOURCE / "xmodel" / name),
        selected_version=GAME_VERSION.CoD,
    )
    created = [obj for obj in bpy.context.scene.objects if obj not in existing]
    meshes = [obj for obj in created if obj.type == "MESH"]
    armatures = [obj for obj in created if obj.type == "ARMATURE"]
    # Convert each source into metres before any cross-model rebinding creates
    # skin inverse-bind matrices. Post-scaling a finished armature is valid
    # inside Blender but produces consumer-dependent FBX bind poses.
    scale_matrix = Matrix.Scale(METRES_PER_INCH, 4)
    for mesh in meshes:
        mesh.data.transform(scale_matrix)
    for armature in armatures:
        armature.data.transform(scale_matrix)
    return meshes, armatures


def armature_modifier(mesh):
    return next(
        (modifier for modifier in mesh.modifiers if modifier.type == "ARMATURE"),
        None,
    )


def preserve_parent(mesh, target) -> None:
    world = mesh.matrix_world.copy()
    mesh.parent = target
    mesh.matrix_world = world


def source_bone_for_group(source, group_name: str):
    bone = source.data.bones.get(group_name)
    if bone is None and group_name.endswith(".001"):
        bone = source.data.bones.get(group_name[:-4])
    return bone


def matching_bind_bones(source_bone, target):
    current = source_bone
    while current is not None:
        target_bone = target.data.bones.get(current.name)
        if target_bone is not None:
            return current, target_bone
        current = current.parent
    return None, None


def rebind_mesh(mesh, source, target) -> None:
    """Convert a component from its source bind pose to the body bind pose."""
    rebound_positions = []
    rebound_normals = []
    rebound_weights = []
    target_bones = set(target.data.bones.keys())
    for vertex in mesh.data.vertices:
        position = Vector()
        normal = Vector()
        weights = {}
        total = 0.0
        for membership in vertex.groups:
            if membership.weight <= 0.0:
                continue
            group_name = mesh.vertex_groups[membership.group].name
            source_bone = source_bone_for_group(source, group_name)
            if source_bone is None:
                continue
            source_bind_bone, target_bone = matching_bind_bones(
                source_bone,
                target,
            )
            if target_bone is None:
                continue
            transform = (
                target_bone.matrix_local
                @ source_bind_bone.matrix_local.inverted()
            )
            position += (
                transform @ vertex.co
            ) * membership.weight
            normal += (
                transform.to_3x3() @ vertex.normal
            ) * membership.weight
            weights[target_bone.name] = (
                weights.get(target_bone.name, 0.0)
                + membership.weight
            )
            total += membership.weight
        if total <= 1e-6:
            raise RuntimeError(
                f"{mesh.name}: vertex {vertex.index} has no target bind"
            )
        rebound_positions.append(position / total)
        rebound_normals.append((normal / total).normalized())
        rebound_weights.append(
            {
                name: weight / total
                for name, weight in weights.items()
                if name in target_bones
            }
        )

    for vertex, position in zip(
        mesh.data.vertices,
        rebound_positions,
    ):
        vertex.co = position
    mesh.vertex_groups.clear()
    groups = {}
    for vertex_index, weights in enumerate(rebound_weights):
        for name, weight in weights.items():
            group = groups.get(name)
            if group is None:
                group = mesh.vertex_groups.new(name=name)
                groups[name] = group
            group.add([vertex_index], weight, "REPLACE")
    mesh.data.update()
    mesh.data.normals_split_custom_set_from_vertices(
        [tuple(normal) for normal in rebound_normals]
    )


def remap_skinned_part(meshes, source_armatures, target) -> None:
    if len(source_armatures) != 1:
        raise RuntimeError(
            f"expected one source armature, found {len(source_armatures)}"
        )
    source = source_armatures[0]
    source.data.pose_position = "REST"
    for mesh in meshes:
        modifier = armature_modifier(mesh)
        rebind_mesh(mesh, source, target)
        if modifier is None:
            modifier = mesh.modifiers.new("CoD Multiplayer Rig", "ARMATURE")
        modifier.object = target
        preserve_parent(mesh, target)
    bpy.data.objects.remove(source, do_unlink=True)


def bind_rigid_part(meshes, source_armatures, target, bone_name: str) -> None:
    target_bone = target.data.bones.get(bone_name)
    if target_bone is None:
        raise RuntimeError(f"body rig has no {bone_name} attachment bone")
    for mesh in meshes:
        for modifier in list(mesh.modifiers):
            if modifier.type == "ARMATURE":
                mesh.modifiers.remove(modifier)
        # Rigid CoD attachment geometry is stored in attachment-bone local
        # space. Move it into the body's bind space before assigning weights.
        mesh.data.transform(target_bone.matrix_local)
        mesh.vertex_groups.clear()
        group = mesh.vertex_groups.new(name=bone_name)
        group.add(range(len(mesh.data.vertices)), 1.0, "REPLACE")
        modifier = mesh.modifiers.new("CoD Multiplayer Rig", "ARMATURE")
        modifier.object = target
        preserve_parent(mesh, target)
    for source_armature in source_armatures:
        bpy.data.objects.remove(source_armature, do_unlink=True)


def bind_local_matrix(pose_bone) -> Matrix:
    if pose_bone.parent is None:
        return pose_bone.bone.matrix_local.copy()
    return (
        pose_bone.parent.bone.matrix_local.inverted()
        @ pose_bone.bone.matrix_local
    )


def animated_local_matrix(track, bind_local: Matrix, frame: int) -> Matrix:
    matrix = bind_local.copy()
    if track.rotation is not None:
        x, y, z, w = sample(
            track.rotation,
            frame,
            quaternion=True,
        )
        matrix = Quaternion((w, x, y, z)).to_matrix().to_4x4()
    matrix.translation = bind_local.translation
    if track.translation is not None:
        # CoD1 translations are deltas from the model's bind-local offset.
        # Treating an omitted channel as zero collapses every child onto its
        # parent—the exact failure the deformation audit is designed to catch.
        matrix.translation += (
            Vector(sample(track.translation, frame)) * METRES_PER_INCH
        )
    return matrix


def add_action(armature, name: str) -> dict:
    print(f"  ANIMATION {name}", flush=True)
    animation = load(SOURCE / "xanim" / name)
    action = bpy.data.actions.new(animation.name)
    action.use_fake_user = True
    armature.animation_data_create()
    armature.animation_data.action = action
    for pose_bone in armature.pose.bones:
        pose_bone.matrix_basis = Matrix.Identity(4)
        pose_bone.rotation_mode = "QUATERNION"
    tracks = {track.name: track for track in animation.bones}
    entries = []
    animated_bones = 0
    for pose_bone in armature.pose.bones:
        source = tracks.get(pose_bone.name.lower())
        if source is not None:
            animated_bones += 1
        entries.append(
            {
                "bone": pose_bone,
                "source": source,
                "bind_local": bind_local_matrix(pose_bone),
            }
        )

    for frame in range(animation.frame_count):
        targets = {}
        for entry in entries:
            bone = entry["bone"]
            track = entry["source"]
            bind_local = entry["bind_local"]
            local = (
                animated_local_matrix(track, bind_local, frame)
                if track is not None
                else bind_local
            )
            parent_target = (
                targets[bone.parent.name]
                if bone.parent is not None
                else Matrix.Identity(4)
            )
            target = parent_target @ local
            targets[bone.name] = target
            bone.matrix_basis = (
                bind_local.inverted()
                @ parent_target.inverted()
                @ target
            )
            if track is None:
                continue
            bone.keyframe_insert("location", frame=frame, group=bone.name)
            bone.keyframe_insert(
                "rotation_quaternion", frame=frame, group=bone.name
            )

    action["source_framerate"] = animation.frame_rate
    action["source_looped"] = animation.looped
    return {
        "name": name,
        "frames": animation.frame_count,
        "fps": animation.frame_rate,
        "looped": animation.looped,
        "animated_bones": animated_bones,
    }


def copy_textures(output_dir: Path) -> list[str]:
    texture_dir = output_dir / "Textures"
    texture_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        path = Path(bpy.path.abspath(image.filepath))
        if not path.is_file():
            continue
        destination = texture_dir / path.name
        if not destination.exists():
            shutil.copy2(path, destination)
        copied.append(path.name)
    return sorted(set(copied))


def prepare_export_hierarchy(rig) -> None:
    """Remove importer-only XModel roots from the metre-native hierarchy."""
    # The importer creates one organizational Empty per source XModel. They
    # carry no gameplay data and add another opportunity for unit conversion
    # differences, so export one identity-scale armature hierarchy instead.
    world = rig.matrix_world.copy()
    rig.parent = None
    rig.matrix_world = world
    for obj in list(bpy.context.scene.objects):
        if obj.type == "EMPTY":
            bpy.data.objects.remove(obj, do_unlink=True)


def export_profile(profile: dict) -> dict:
    clear_scene()
    body_meshes, body_armatures = import_part(profile["body"])
    if len(body_armatures) != 1:
        raise RuntimeError(
            f"{profile['body']}: expected one body armature, "
            f"found {len(body_armatures)}"
        )
    rig = body_armatures[0]
    rig.name = "CoD1 Multiplayer Rig"
    rig.data.name = "CoD1 Multiplayer Skeleton"

    head_meshes, head_armatures = import_part(profile["head"])
    remap_skinned_part(head_meshes, head_armatures, rig)
    hat_meshes, hat_armatures = import_part(profile["headwear"])
    if hat_armatures:
        # British airborne headwear has both `bip01 head` and `tag_helmet`
        # and must retain that skinning. The other CoD 1 hats have a single
        # `bip01 head` root, which the importer intentionally omits.
        remap_skinned_part(hat_meshes, hat_armatures, rig)
    else:
        bind_rigid_part(hat_meshes, hat_armatures, rig, "bip01 head")
    for gear_name in profile["gear"]:
        gear_meshes, gear_armatures = import_part(gear_name)
        if gear_armatures:
            remap_skinned_part(gear_meshes, gear_armatures, rig)
        else:
            rigid_bone = profile["rigid_gear_bones"].get(gear_name)
            if rigid_bone is None:
                raise RuntimeError(
                    f"{gear_name}: rigid equipment has no declared bind bone"
                )
            bind_rigid_part(gear_meshes, gear_armatures, rig, rigid_bone)

    animation_records = [
        (reference, add_action(rig, reference.name))
        for reference in ANIMATION_REFERENCES
    ]
    animations = [animation for _reference, animation in animation_records]
    prepare_export_hierarchy(rig)

    if EXPORT_FORMAT == "glb":
        # Actions keep their exact pb_*/pt_* names — the runtime resolves
        # clips by substring over those names.
        pak_dir = PAK_ROOT / "players" / profile["id"]
        materials = build_material_table(
            pak_dir / "textures",
            f"players/{profile['id']}/textures",
        )
        export_skinned_glb(pak_dir / "player.glb")
        return {
            "id": profile["id"],
            "displayName": profile["display_name"],
            "faction": FACTIONS[profile["id"]],
            "glb": f"players/{profile['id']}/player.glb",
            "materials": materials,
            "animations": [
                {
                    "name": animation["name"],
                    "looped": animation["looped"],
                    "layers": sorted(reference.layers),
                    "actionRoles": sorted(reference.action_roles),
                    "scriptOccurrences": reference.occurrences,
                    "sourceAlias": reference.source_alias,
                    "turretWeapon": reference.turret_weapon,
                    "turretStance": reference.turret_stance,
                    "turretPhase": reference.turret_phase,
                    "turretHorizontal": reference.turret_horizontal,
                    "turretVertical": reference.turret_vertical,
                }
                for reference, animation in animation_records
            ],
        }

    output_dir = OUTPUT / profile["id"]
    output_dir.mkdir(parents=True, exist_ok=True)
    textures = copy_textures(output_dir)
    fbx_path = output_dir / f"{profile['id']}.fbx"
    bpy.context.scene.render.fps = 30
    bpy.ops.export_scene.fbx(
        filepath=str(fbx_path),
        use_selection=False,
        apply_unit_scale=True,
        bake_space_transform=False,
        add_leaf_bones=False,
        path_mode="COPY",
        embed_textures=False,
        object_types={"EMPTY", "MESH", "ARMATURE"},
        bake_anim=True,
        bake_anim_use_all_bones=True,
        bake_anim_use_all_actions=True,
        bake_anim_force_startend_keying=True,
        bake_anim_simplify_factor=0.0,
    )
    bpy.ops.wm.save_as_mainfile(
        filepath=str(output_dir / f"{profile['id']}.blend")
    )
    return {
        **profile,
        "fbx": fbx_path.name,
        "meshes": len(
            [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
        ),
        "textures": textures,
        "animations": animations,
    }


def write_players_manifest(results: list[dict]) -> None:
    """Merge into players.json so exporting a subset of ids keeps the rest."""
    manifest_path = PAK_ROOT / "players" / "players.json"
    by_id = {}
    if manifest_path.is_file():
        try:
            by_id = {
                entry["id"]: entry
                for entry in load_json(manifest_path).get("players", [])
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            by_id = {}
    for entry in results:
        by_id[entry["id"]] = entry
    write_json(
        manifest_path,
        {
            "format": "FriendsOfDuty.Players",
            # Catalog SCHEMA version — the manifest's shape, which has not
            # changed. Locked to FodContentSchemas.PlayersVersion and
            # WeaponsVersion by the build's schema contract. Data changes go
            # to fodpak.json's gameContentVersion instead.
            "version": 3,
            "source": {
                "script": "mp/playeranim.script",
                "scriptSha256": PLAYER_ANIMATION_INVENTORY.script_sha256,
                "activeIdentifiers":
                    PLAYER_ANIMATION_INVENTORY.active_identifier_count,
                "exportedPlayerAnimations": len(ANIMATION_REFERENCES),
                "expandedTurretAliases": list(
                    PLAYER_ANIMATION_INVENTORY.expanded_turret_aliases
                ),
                "expandedTurretAnimations":
                    PLAYER_ANIMATION_INVENTORY
                    .expanded_turret_animation_count,
                "excludedTurretAnimations": list(
                    PLAYER_ANIMATION_INVENTORY.excluded_turret_animations
                ),
            },
            "players": [by_id[key] for key in sorted(by_id)],
        },
    )


def main() -> None:
    results = []
    failures = []
    for profile in PROFILES:
        if SELECTED_IDS and profile["id"] not in SELECTED_IDS:
            continue
        print(f"EXPORTING {profile['display_name']}")
        try:
            results.append(export_profile(profile))
        except Exception as error:
            failures.append({"id": profile["id"], "error": str(error)})
            print(f"FAILED {profile['id']}: {error}")
    if EXPORT_FORMAT == "glb":
        write_players_manifest(results)
        print(f"PAK PLAYER EXPORT COMPLETE — {len(results)} player(s)")
    else:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        manifest = {"players": results, "failures": failures}
        (OUTPUT / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
    if failures:
        raise SystemExit(
            f"{len(failures)} multiplayer player export(s) failed"
        )


if __name__ == "__main__":
    main()
