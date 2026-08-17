"""Blender background script: export all CoD 1 demo viewmodels and animations."""

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
    viewmodel_anim_keys,
    write_json,
)


def args() -> tuple[Path, Path, Path, Path, str, Path | None, set[str] | None]:
    values = sys.argv[sys.argv.index("--") + 1 :]
    positionals, export_format, pak_root, models = parse_cli(
        values,
        4,
        "expected SOURCE OUTPUT IMPORTER_PY_ROOT TOOLS_ROOT "
        "[--format fbx|glb] [--pak-root DIR] [--models a,b]",
    )
    paths = tuple(Path(value).resolve() for value in positionals[:4])
    return (*paths, export_format, pak_root, models)


SOURCE, OUTPUT, IMPORTER_ROOT, TOOLS_ROOT, EXPORT_FORMAT, PAK_ROOT, MODELS = args()
sys.path.insert(0, str(IMPORTER_ROOT))
sys.path.insert(0, str(TOOLS_ROOT))
sys.path.insert(0, str(TOOLS_ROOT.parent))

from cod_asset_importer import importer
from cod_asset_importer.cod_asset_importer import GAME_VERSION
from cod1_xanim import load, sample
from cod1_weapon_metadata import (
    parse_weapon_file,
    weapon_animation_metadata,
)


ROSTER = (
    ("bar", "bar_mp", "BAR", "us"),
    ("bren", "bren_mp", "Bren", "british"),
    ("colt45", "colt_mp", "Colt .45", "us"),
    ("enfield", "enfield_mp", "Lee-Enfield", "british"),
    ("fg42", "fg42_mp", "FG 42", "german"),
    ("fraggrenade", "fraggrenade_mp", "MK2 Frag Grenade", "us"),
    ("kar98k", "kar98k_mp", "Kar98k", "german"),
    ("kar98k_scoped", "kar98k_sniper_mp", "Scoped Kar98k", "german"),
    ("luger", "luger_mp", "Luger", "german"),
    ("m1carbine", "m1carbine_mp", "M1A1 Carbine", "us"),
    ("m1garand", "m1garand_mp", "M1 Garand", "us"),
    ("mk1britishfrag", "mk1britishfrag_mp", "MK1 Frag Grenade", "british"),
    ("mosin_nagant", "mosin_nagant_mp", "Mosin-Nagant", "russian"),
    ("mosin_nagant_scoped", "mosin_nagant_sniper_mp", "Scoped Mosin-Nagant", "russian"),
    ("mp40", "mp40_mp", "MP40", "german"),
    ("mp44", "mp44_mp", "MP44", "german"),
    ("panzerfaust", "panzerfaust_mp", "Panzerfaust", "german"),
    ("ppsh", "ppsh_mp", "PPSh-41", "russian"),
    ("rgd33", "rgd-33russianfrag_mp", "RGD-33", "russian"),
    ("springfield", "springfield_mp", "Springfield", "us"),
    ("sten", "sten_mp", "Sten", "british"),
    ("stielhandgranate", "stielhandgranate_mp", "Stielhandgranate", "german"),
    ("thompson", "thompson_mp", "Thompson", "us"),
)

# UO-only weapons; their members are staged into cod1_source by
# extract_cod1_ordnance.py, so export them only once that file exists.
# Kept in step with UO_WEAPON_FILES in cod1_multiplayer_closure.py — that
# list decides what gets STAGED, this one decides what gets EXPORTED, and a
# weapon in one but not the other is either a missing model or dead weight
# in the staging tree.
UO_ROSTER = (
    ("bazooka", "bazooka_mp", "M9A1 Bazooka", "us"),
    ("binoculars", "binoculars_mp", "Binoculars", "us"),
    ("binoculars_artillery", "binoculars_artillery_mp",
     "Artillery Binoculars", "us"),
    ("dp28", "dp28_mp", "DP-28", "russian"),
    ("flamethrower", "flamethrower_mp", "M2 Flamethrower", "us"),
    ("flashgrenade", "flashgrenade_mp", "Flash Grenade", "us"),
    ("gewehr43", "gewehr43_mp", "Gewehr 43", "german"),
    ("mg30cal", "mg30Cal_mp", "M1919 .30 Cal", "us"),
    ("mg34", "mg34_mp", "MG34", "german"),
    ("panzerschreck", "panzerschreck_mp", "Panzerschreck", "german"),
    ("satchelcharge", "satchelcharge_mp", "Satchel Charge", "us"),
    ("smokegrenade", "smokegrenade_mp", "M18 Smoke Grenade", "us"),
    ("sten_silenced", "sten_silenced_mp", "Silenced Sten", "british"),
    ("svt40", "svt40_mp", "SVT-40", "russian"),
    ("tt33", "tt33_mp", "Tokarev TT-33", "russian"),
    ("webley", "webley_mp", "Webley Mk VI", "british"),
)

# CoD weaponClass -> the schema's weapon_class enum; a grenade WEAPONFILE with
# projExplosionType smoke becomes "smoke".
WEAPON_CLASSES = {
    "rifle": "rifle",
    # Folding smg into rifle was harmless while the class only selected
    # tracer splits (keyed on "mg"); the per-class impact tiers now need
    # the SMGs to say what they are.
    "smg": "smg",
    "spread": "rifle",
    "mg": "mg",
    # United Offensive spells its deployable machine guns "lmg". Leaving it
    # unmapped fell through to the "rifle" default, which quietly cost the
    # dp28, mg34 and mg30cal their machine-gun identity: g_tracerchancelmg
    # is selected by weaponClass == "mg", so the LMG tracer split applied to
    # nothing at all.
    "lmg": "mg",
    "pistol": "pistol",
    "grenade": "grenade",
    "rocketlauncher": "rocket",
    # Carried in a weapon slot but never fired. Naming the class is what
    # lets the runtime stop demanding ammo of it.
    "spotter": "spotter",
    "flamethrower": "flamethrower",
}

EXPLODE_SOUND_DEFAULTS = {
    "grenade": "grenade_explode_default",
    "rocket": "rocket_explode_default",
}


def number(values: dict, key: str, default=0.0):
    value = values.get(key, "")
    return float(value) if value else default


def ordnance_fields(values: dict, weapon_class: str) -> dict:
    """Optional SHARED SCHEMA definition fields (CoD units, alias names).

    Emitted only when the WEAPONFILE carries a meaningful value so the
    existing gun definitions stay unchanged; the C# mirrors fall back to
    their defaults for absent keys."""
    explosion_type = values.get("projExplosionType", "").strip()
    throwable = weapon_class in ("grenade", "smoke")
    fields = {
        # Emitted for every weapon: the runtime's default is "rifle" so the
        # explicit value is equivalent for rifles and load-bearing for the
        # per-class impact tiers everywhere else.
        "weapon_class": weapon_class,
        "fuse_time": number(values, "fuseTime"),
        "cook": values.get("cookOffHold", "0") == "1",
        "explosion_radius": number(values, "explosionRadius"),
        "explosion_inner_damage": number(values, "explosionInnerDamage"),
        "explosion_outer_damage": number(values, "explosionOuterDamage"),
        "projectile_speed": number(values, "projectileSpeed"),
        "throw_speed_up": number(values, "projectileSpeedUp"),
        "projectile_model": values.get("projectileModel", "").removeprefix("xmodel/"),
        "shared_ammo_cap": int(number(values, "sharedAmmoCap")),
        "explosion_type": explosion_type,
        "bounce_sound": "grenade_bounce_default" if throwable else "",
        "explode_sound": (values.get("projExplosionSound", "").strip()
                          or EXPLODE_SOUND_DEFAULTS.get(explosion_type, "")),
        "throw_sound": values.get("fireSound", "") if throwable else "",
        "pin_sound": values.get("pullbackSound", "") if throwable else "",
    }
    return {key: value for key, value in fields.items() if value}


def make_weapon(entry) -> dict:
    name, weapon_file, display_name, skin = entry
    metadata = weapon_animation_metadata(SOURCE, weapon_file)
    values = metadata["values"]
    alternate_weapon_file = metadata["alternateWeaponFile"]
    alternate_values = (
        parse_weapon_file(
            SOURCE / "weapons" / "mp" / alternate_weapon_file
        )
        if alternate_weapon_file
        else {}
    )
    weapon_class = WEAPON_CLASSES.get(values.get("weaponClass", ""), "rifle")
    if weapon_class == "grenade" and values.get("projExplosionType") == "smoke":
        weapon_class = "smoke"
    magazine = int(number(values, "clipSize", 1))
    starting_ammo = int(number(values, "startAmmo", magazine))
    return {
        "name": name,
        "weapon_file": weapon_file,
        "display_name": display_name,
        "gun": values["gunModel"],
        "hands": values["handModel"],
        "world": values["worldModel"].removeprefix("xmodel/"),
        "skin": skin,
        "animations": metadata["animations"],
        "animation_roles": metadata["animationRoles"],
        "idle_animation": values.get("idleAnim", ""),
        "ads_up_animation": values.get("adsUpAnim", ""),
        "ads_down_animation": values.get("adsDownAnim", ""),
        "magazine": magazine,
        # CoD's startAmmo includes the rounds already loaded in the weapon.
        "reserve": max(0, starting_ammo - magazine),
        "max_ammo": int(number(values, "maxAmmo", starting_ammo)),
        # Retail MP bounds for the ammo granted by a dropped/same-ammo
        # pickup. The engine-owned clip/reserve split is not inferred here.
        "drop_ammo_min": int(number(values, "dropAmmoMin", 0)),
        "drop_ammo_max": int(number(values, "dropAmmoMax", 0)),
        "damage": number(values, "damage", 50),
        "meleeDamage": metadata["meleeDamage"],
        "meleeDelay": metadata["meleeDelay"],
        "meleeTime": metadata["meleeTime"],
        "rpm": 60.0 / max(number(values, "fireTime", 0.1), 0.001),
        "automatic": values.get("semiAuto", "0") != "1",
        "mode_name": values.get("modeName", ""),
        "alternate_weapon_file": alternate_weapon_file,
        "alternate_mode_name": alternate_values.get("modeName", ""),
        "alternate_rpm": (
            60.0 /
            max(number(alternate_values, "fireTime", 0.1), 0.001)
            if alternate_values
            else 0.0
        ),
        "alternate_automatic": (
            alternate_values.get("semiAuto", "0") != "1"
            if alternate_values
            else False
        ),
        "alternate_switch_seconds": max(
            number(values, "altDropTime", 0.0),
            number(alternate_values, "altDropTime", 0.0),
            number(values, "altRaiseTime", 0.0),
            number(alternate_values, "altRaiseTime", 0.0),
        ),
        "alternate_switch_sound": (
            values.get("altSwitchSound", "")
            or alternate_values.get("altSwitchSound", "")
        ),
        "bolt_action": values.get("boltAction", "0") == "1",
        "segmented_reload": values.get("segmentedReload", "0") == "1",
        "reload_ammo_add": int(number(values, "reloadAmmoAdd", 0)),
        "reload_start_add": int(number(values, "reloadStartAdd", 0)),
        "ads": values.get("aimDownSight", "0") == "1",
        "ads_fov": number(values, "adsZoomFov", 65),
        "ads_in": number(values, "adsTransInTime", 0.25),
        "ads_out": number(values, "adsTransOutTime", 0.35),
        "pickup_sound": values.get("pickupSound", ""),
        "fire_sound": values.get("fireSound", ""),
        "last_shot_sound": values.get("lastShotSound", ""),
        "rechamber_sound": values.get("rechamberSound", ""),
        "reload_sound": values.get("reloadSound", ""),
        "reload_empty_sound": values.get("reloadEmptySound", ""),
        "reload_start_sound": values.get("reloadStartSound", ""),
        "reload_end_sound": values.get("reloadEndSound", ""),
        "muzzle_effect": values.get("viewFlashEffect", ""),
        # The third-person flash, verbatim like muzzle_effect. Three UO
        # WEAPONFILEs spell it with a doubled extension (mf_kar98.efx.efx);
        # the presentation extractor folds that when flattening into fx/efx,
        # and consumers resolve by basename the same way.
        "world_muzzle_effect": values.get("worldFlashEffect", ""),
        "shell_effect": values.get("shellEjectEffect", ""),
        "ordnance": ordnance_fields(values, weapon_class),
    }


DEFINITION_KEYS = (
    "name", "weapon_file", "display_name", "world", "magazine",
    "reserve", "max_ammo", "drop_ammo_min", "drop_ammo_max",
    "damage", "rpm", "automatic", "bolt_action",
    "mode_name", "alternate_weapon_file", "alternate_mode_name",
    "alternate_rpm", "alternate_automatic",
    "alternate_switch_seconds", "alternate_switch_sound",
    "meleeDamage", "meleeDelay", "meleeTime",
    "segmented_reload", "reload_ammo_add", "reload_start_add",
    "ads", "ads_fov", "ads_in", "ads_out",
    "pickup_sound", "fire_sound", "last_shot_sound",
    "rechamber_sound", "reload_sound", "reload_empty_sound",
    "reload_start_sound", "reload_end_sound", "muzzle_effect",
    "world_muzzle_effect", "shell_effect",
)


def weapon_definition(weapon) -> dict:
    definition = {key: weapon[key] for key in DEFINITION_KEYS}
    definition.update(weapon["ordnance"])
    return definition


def roster() -> tuple:
    entries = ROSTER + tuple(
        entry for entry in UO_ROSTER
        if (SOURCE / "weapons" / "mp" / entry[1]).is_file()
    )
    return entries


WEAPONS = tuple(
    weapon
    for weapon in (make_weapon(entry) for entry in roster())
    if MODELS is None or weapon["name"].casefold() in MODELS
)


def get_mat_offs(bone):
    matrix = bone.matrix.to_4x4()
    matrix.translation = bone.head
    matrix.translation.y += bone.parent.length
    return matrix


def get_mat_rest(pose_bone, parent_pose, parent_local):
    bone = pose_bone.bone
    inherit_scale = bone.inherit_scale != "NONE"
    if pose_bone.parent:
        offset = get_mat_offs(bone)
        if not bone.use_inherit_rotation and not inherit_scale:
            rotation_scale = parent_local @ offset
        elif not bone.use_inherit_rotation:
            size = Matrix.Identity(4)
            for axis in range(3):
                size[axis][axis] = parent_pose.col[axis].magnitude
            rotation_scale = size @ parent_local @ offset
        elif not inherit_scale:
            rotation_scale = parent_pose.normalized() @ offset
        else:
            rotation_scale = parent_pose @ offset
        if not bone.use_local_location:
            a = Matrix.Translation(parent_pose @ offset.translation)
            b = parent_pose.copy()
            b.translation = Vector()
            location = a @ b
        elif not bone.use_inherit_rotation or not inherit_scale:
            location = parent_pose @ offset
        else:
            location = rotation_scale.copy()
    else:
        rotation_scale = bone.matrix_local
        location = (
            Matrix.Translation(bone.matrix_local.translation)
            if not bone.use_local_location else rotation_scale.copy()
        )
    return rotation_scale, location


def calc_basis(pose_bone, matrix, parent_pose, parent_local):
    rotation_scale, location = get_mat_rest(pose_bone, parent_pose, parent_local)
    basis = (matrix.to_3x3().inverted() @ rotation_scale.to_3x3()).transposed()
    basis.resize_4x4()
    basis.translation = location.inverted() @ matrix.translation
    return basis


def clear_scene() -> None:
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
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


def attachment_chain(model_name: str, bone_names) -> tuple:
    """Read a root-to-leaf attachment chain from the matching hands model."""
    clear_scene()
    importer.import_xmodel(
        asset_path=str(SOURCE),
        file_path=str(SOURCE / "xmodel" / model_name),
        selected_version=GAME_VERSION.CoD,
    )
    armature = imported_armature()
    result = []
    previous = None
    for name in bone_names:
        bone = armature.data.bones.get(name)
        if bone is None:
            raise RuntimeError(f"{model_name}: missing attachment bone {name}")
        if previous is not None and bone.parent != previous:
            raise RuntimeError(
                f"{model_name}: {name} is not parented to {previous.name}"
            )
        local_rest = (
            bone.matrix_local.copy()
            if previous is None
            else previous.matrix_local.inverted() @ bone.matrix_local
        )
        result.append((name, local_rest))
        previous = bone
    return tuple(result)


def imported_armature():
    armatures = [obj for obj in bpy.context.scene.objects if obj.type == "ARMATURE"]
    if len(armatures) != 1:
        raise RuntimeError(f"expected one armature, found {len(armatures)}")
    return armatures[0]


def import_model(model_name: str):
    existing = set(bpy.context.scene.objects)
    importer.import_xmodel(
        asset_path=str(SOURCE),
        file_path=str(SOURCE / "xmodel" / model_name),
        selected_version=GAME_VERSION.CoD,
    )
    armatures = [
        obj for obj in bpy.context.scene.objects
        if obj not in existing and obj.type == "ARMATURE"
    ]
    if len(armatures) != 1:
        raise RuntimeError(
            f"{model_name}: expected one newly imported armature, found {len(armatures)}"
        )
    return armatures[0]


def make_textured_material(name: str, texture_path: Path):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    image = bpy.data.images.load(str(texture_path), check_existing=True)
    texture = material.node_tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    shader = material.node_tree.nodes.get("Principled BSDF")
    material.node_tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
    return material


def apply_hand_skin(skin: str) -> None:
    """The engine replaces viewhands@default at runtime based on the team."""
    if skin == "german":
        hand_path = SOURCE / "skins/hand.tga"
        sleeve_path = SOURCE / "skins/viewhands@vsleeve_whermact.tga"
    elif skin == "us":
        hand_path = SOURCE / "skins/viewhands@hand.dds"
        sleeve_path = SOURCE / "skins/viewsleeves_new.tga"
    elif skin == "british":
        hand_path = SOURCE / "skins/hand.tga"
        sleeve_path = SOURCE / "skins/viewhands@vsleeve_British.tga"
    elif skin == "russian":
        hand_path = SOURCE / "skins/hand.tga"
        sleeve_path = SOURCE / "skins/viewhands@vsleeve_russian.tga"
    else:
        raise ValueError(f"unsupported hand skin: {skin}")
    hand = make_textured_material(hand_path.stem, hand_path)
    sleeve = make_textured_material(sleeve_path.stem, sleeve_path)
    meshes = sorted(
        (obj for obj in bpy.context.scene.objects if obj.type == "MESH"),
        key=lambda obj: obj.name,
    )
    if len(meshes) != 4:
        raise RuntimeError(f"expected four hand surfaces, found {len(meshes)}")
    # CoD stores left hand, right hand, left sleeve, right sleeve.
    for index, obj in enumerate(meshes):
        obj.data.materials.clear()
        obj.data.materials.append(hand if index < 2 else sleeve)


def track_matrix(track, frame: int) -> Matrix:
    x, y, z, w = sample(track.rotation, frame, quaternion=True)
    matrix = Quaternion((w, x, y, z)).to_matrix().to_4x4()
    matrix.translation = Vector(sample(track.translation, frame))
    return matrix


def add_action(
    armature,
    animation_path: Path,
    external_parent_chain=(),
    base_animation_path: Path | None = None,
) -> dict:
    animation = load(animation_path)
    base_animation = load(base_animation_path) if base_animation_path else None
    action = bpy.data.actions.new(animation.name)
    action.use_fake_user = True
    armature.animation_data_create()
    armature.animation_data.action = action

    # CoD ADS-up/down files are sparse overlays containing only tag_torso.
    # Compose them over the weapon's idle tracks before exporting to FBX,
    # because Unity animation clips do not reproduce CoD's runtime overlay.
    tracks = {}
    if base_animation is not None:
        tracks.update(
            {
                track.name: (track, base_animation.frame_count)
                for track in base_animation.bones
            }
        )
    tracks.update(
        {
            track.name: (track, animation.frame_count)
            for track in animation.bones
        }
    )
    external_parents = [
        (name, tracks.get(name), rest_matrix)
        for name, rest_matrix in external_parent_chain
    ]
    if external_parents and external_parents[-1][1] is None:
        raise RuntimeError(
            f"{animation.name}: missing attachment track {external_parents[-1][0]}"
        )
    mapped = []
    for pose_bone in armature.pose.bones:
        pose_bone.matrix_basis = Matrix.Identity(4)
        pose_bone.rotation_mode = "QUATERNION"
        source = tracks.get(pose_bone.name.lower())
        if source is None:
            continue
        parent = next(
            (entry for entry in reversed(mapped)
             if entry["bone"] == pose_bone.parent or entry["bone"] in pose_bone.parent_recursive),
            None,
        )
        mapped.append({
            "bone": pose_bone,
            "source": source,
            "parent": parent,
            "matrix": Matrix.Identity(4),
        })

    def matrix_at(source, frame):
        track, frame_count = source
        return track_matrix(track, frame % max(frame_count, 1))

    for frame in range(animation.frame_count):
        external_matrix = Matrix.Identity(4)
        # tag_weapon is not a world-space track. Its full CoD hierarchy is
        # tag_view -> tag_torso -> tag_weapon. Bake that complete attachment
        # transform into the gun root so it receives the same camera/torso
        # motion as the separately rendered hands.
        for _name, external_parent, rest_matrix in external_parents:
            external_matrix @= (
                matrix_at(external_parent, frame)
                if external_parent is not None else rest_matrix
            )
        for entry in mapped:
            matrix = matrix_at(entry["source"], frame)

            parent = entry["parent"]
            parent_matrix = parent["matrix"] if parent else external_matrix
            parent_local = parent["bone"].bone.matrix_local if parent else Matrix.Identity(4)
            # CoD's compiled XAnim tracks are local to the animated parent.
            # BetterBlender's basis conversion consumes global pose matrices,
            # so accumulate the hierarchy before converting into Blender space.
            matrix = parent_matrix @ matrix
            entry["matrix"] = matrix
            bone = entry["bone"]
            bone.matrix_basis = calc_basis(bone, matrix, parent_matrix, parent_local)
            bone.keyframe_insert("location", frame=frame, group=bone.name)
            bone.keyframe_insert("rotation_quaternion", frame=frame, group=bone.name)

    action["source_framerate"] = animation.frame_rate
    action["source_looped"] = animation.looped
    for note_name, note_frame in animation.notes:
        action.pose_markers.new(note_name).frame = note_frame
    return {
        "name": animation.name,
        "frames": animation.frame_count,
        "fps": animation.frame_rate,
        "animated_bones": len(mapped),
    }


def export_model(export_name: str, model_name: str, animation_names,
                 hand_skin=None, external_parent_chain=()) -> dict:
    clear_scene()
    importer.import_xmodel(
        asset_path=str(SOURCE),
        file_path=str(SOURCE / "xmodel" / model_name),
        selected_version=GAME_VERSION.CoD,
    )
    if hand_skin:
        apply_hand_skin(hand_skin)
    armature = imported_armature()
    animations = [
        add_action(
            armature,
            SOURCE / "xanim" / name,
            external_parent_chain=external_parent_chain,
        )
        for name in animation_names
    ]

    # One top-level conversion from the engine's inches to Unity metres.
    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.scale = tuple(value * 0.0254 for value in obj.scale)

    model_dir = OUTPUT / export_name
    texture_dir = model_dir / "Textures"
    texture_dir.mkdir(parents=True, exist_ok=True)
    textures = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        source_path = Path(bpy.path.abspath(image.filepath))
        if source_path.is_file():
            shutil.copy2(source_path, texture_dir / source_path.name)
            textures.append(source_path.name)

    bpy.context.scene.render.fps = 30
    fbx_path = model_dir / f"{export_name}.fbx"
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
    bpy.ops.wm.save_as_mainfile(filepath=str(model_dir / f"{export_name}.blend"))
    return {
        "name": export_name,
        "source_model": model_name,
        "fbx": str(fbx_path),
        "textures": sorted(set(textures)),
        "animations": animations,
    }


def export_combined_viewmodel(weapon) -> dict:
    """Build one rig so hands and gun are evaluated by one Unity Animation."""
    clear_scene()
    hands_armature = import_model(weapon["hands"])
    apply_hand_skin(weapon["skin"])
    gun_armature = import_model(weapon["gun"])

    gun_meshes = [
        obj for obj in bpy.context.scene.objects
        if obj.type == "MESH" and obj.find_armature() == gun_armature
    ]
    gun_roots = [bone for bone in gun_armature.data.bones if bone.parent is None]
    if len(gun_roots) != 1:
        # The UO m18 smoke grenade ships a stray helper root (tape01.target)
        # beside tag_weapon. Attach through tag_weapon; other roots stay free
        # roots driven directly by their own animation tracks, as in CoD.
        named = [bone for bone in gun_roots if bone.name.lower() == "tag_weapon"]
        if len(named) != 1:
            raise RuntimeError(
                f"{weapon['name']}: expected one gun root, found {len(gun_roots)}"
            )
        gun_roots = named
    gun_root_name = gun_roots[0].name
    if hands_armature.data.bones.get(gun_root_name) is not None:
        # Garand calls both the hands attachment and gun root "tag_weapon".
        # Keep the hands bone as the animated attachment and turn the gun copy
        # into an unanimated identity bridge beneath it.
        renamed_root = "gun_attachment_root"
        gun_roots[0].name = renamed_root
        for mesh in gun_meshes:
            group = mesh.vertex_groups.get(gun_root_name)
            if group is not None:
                group.name = renamed_root
        gun_root_name = renamed_root

    bpy.ops.object.select_all(action="DESELECT")
    hands_armature.select_set(True)
    gun_armature.select_set(True)
    bpy.context.view_layer.objects.active = hands_armature
    bpy.ops.object.join()

    # Blender normally retargets these during armature join; setting them
    # explicitly also makes the relationship deterministic across versions.
    for mesh in gun_meshes:
        for modifier in mesh.modifiers:
            if modifier.type == "ARMATURE":
                modifier.object = hands_armature

    bpy.context.view_layer.objects.active = hands_armature
    bpy.ops.object.mode_set(mode="EDIT")
    gun_root = hands_armature.data.edit_bones.get(gun_root_name)
    weapon_tag = hands_armature.data.edit_bones.get("tag_weapon")
    if gun_root is None or weapon_tag is None:
        raise RuntimeError(
            f"{weapon['name']}: combined rig lacks {gun_root_name} or tag_weapon"
        )
    gun_root.parent = weapon_tag
    gun_root.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")

    overlay_animations = {
        weapon["ads_up_animation"],
        weapon["ads_down_animation"],
    }
    animations = []
    unreadable_animations: list[str] = []
    for name in weapon["animations"]:
        base_path = (
            SOURCE / "xanim" / weapon["idle_animation"]
            if name in overlay_animations and weapon["idle_animation"]
            else None
        )
        try:
            animations.append(
                add_action(
                    hands_armature,
                    SOURCE / "xanim" / name,
                    base_animation_path=base_path,
                )
            )
        except ValueError as error:
            # A clip the XAnim reader cannot make sense of must not cost the
            # whole export. Retail ships at least one degenerate placeholder
            # -- w_us_spw_binocular_view_ADSfire, an "ADS fire" clip for a
            # weapon that does not fire -- and aborting on it took all 39
            # weapons down with it. Drop the clip from the weapon's roster
            # too, so the packer does not then demand a baked action for it,
            # and say so loudly enough to be noticed in the log.
            print(
                f"  WARNING: skipping unreadable animation {name} on "
                f"{weapon['name']}: {error}"
            )
            unreadable_animations.append(name)
    if unreadable_animations:
        weapon["animations"] = [
            name
            for name in weapon["animations"]
            if name not in unreadable_animations
        ]

    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.scale = tuple(value * 0.0254 for value in obj.scale)

    if EXPORT_FORMAT == "glb":
        return export_pak_viewmodel(weapon)

    export_name = f"{weapon['name']}_combined"
    model_dir = OUTPUT / export_name
    texture_dir = model_dir / "Textures"
    texture_dir.mkdir(parents=True, exist_ok=True)
    textures = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        source_path = Path(bpy.path.abspath(image.filepath))
        if source_path.is_file():
            shutil.copy2(source_path, texture_dir / source_path.name)
            textures.append(source_path.name)

    bpy.context.scene.render.fps = 30
    fbx_path = model_dir / f"{export_name}.fbx"
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
    bpy.ops.wm.save_as_mainfile(filepath=str(model_dir / f"{export_name}.blend"))
    return {
        "name": export_name,
        "source_model": f"{weapon['hands']} + {weapon['gun']}",
        "fbx": str(fbx_path),
        "textures": sorted(set(textures)),
        "animations": animations,
        "definition": weapon_definition(weapon),
    }


def rename_pak_actions(weapon) -> list[str]:
    """Rename baked actions to <id>_combined_<key> so the GLB animation names
    keep the lowercase '_token' suffixes the runtime matches today."""
    keys = viewmodel_anim_keys(weapon["animations"])
    clip_names = []
    source_to_clip = {}
    for source_name, key in zip(weapon["animations"], keys):
        action = bpy.data.actions.get(source_name)
        if action is None:
            raise RuntimeError(
                f"{weapon['name']}: baked action missing for {source_name}"
            )
        action.name = f"{weapon['name']}_combined_{key}"
        clip_names.append(action.name)
        source_to_clip[source_name] = action.name
    # A role whose clip was dropped as unreadable simply has no clip to
    # point at. Emitting the role anyway would put a dangling clip name in
    # the manifest, and raising would undo the whole point of skipping it.
    weapon["resolved_animation_roles"] = [
        {
            **record,
            "clip": source_to_clip[record["sourceAnimation"]],
        }
        for record in weapon["animation_roles"]
        if record["sourceAnimation"] in source_to_clip
    ]
    return clip_names


def export_pak_viewmodel(weapon) -> dict:
    clip_names = rename_pak_actions(weapon)
    pak_dir = PAK_ROOT / "weapons" / "viewmodels" / weapon["name"]
    materials = build_material_table(
        pak_dir / "textures",
        f"weapons/viewmodels/{weapon['name']}/textures",
    )
    export_skinned_glb(pak_dir / "viewmodel.glb")
    world_lower = weapon["world"].lower()
    return {
        "id": weapon["name"],
        "displayName": weapon["display_name"],
        "world": world_lower,
        "viewmodelGlb": f"weapons/viewmodels/{weapon['name']}/viewmodel.glb",
        "worldGlb": f"weapons/worldmodels/{world_lower}/world.glb",
        "viewmodelMaterials": materials,
        "worldMaterials": [],
        "projectileGlb": "",
        "projectileMaterials": [],
        "animations": clip_names,
        "animationRoles": weapon["resolved_animation_roles"],
        "alternateWeaponFile": weapon["alternate_weapon_file"],
        "definition": weapon_definition(weapon),
    }


def write_weapons_manifest(entries: list[dict]) -> None:
    """Merge into weapons.json so worldMaterials appended by the world-model
    exporter and previously exported weapons survive partial re-runs."""
    manifest_path = PAK_ROOT / "weapons" / "weapons.json"
    previous = {}
    if manifest_path.is_file():
        try:
            previous = {
                entry["id"]: entry
                for entry in load_json(manifest_path).get("weapons", [])
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            previous = {}
    merged = []
    exported_ids = set()
    for entry in entries:
        exported_ids.add(entry["id"])
        old = previous.get(entry["id"])
        if old and old.get("worldMaterials"):
            entry["worldMaterials"] = old["worldMaterials"]
        if old and old.get("projectileGlb"):
            entry["projectileGlb"] = old["projectileGlb"]
            entry["projectileMaterials"] = old.get("projectileMaterials", [])
        merged.append(entry)
    # A complete run is authoritative for the selected content tier and must
    # drop stale UO entries when switching back to base-only. Explicit
    # --models subset runs retain untouched entries for developer iteration.
    if MODELS is not None:
        for weapon_id, old in previous.items():
            if weapon_id not in exported_ids:
                merged.append(old)
    merged.sort(key=lambda entry: entry["id"])
    write_json(
        manifest_path,
        {"format": "FriendsOfDuty.Weapons", "version": 3, "weapons": merged},
    )


if EXPORT_FORMAT == "glb":
    entries = [export_combined_viewmodel(weapon) for weapon in WEAPONS]
    write_weapons_manifest(entries)
    print(f"PAK VIEWMODEL EXPORT COMPLETE — {len(entries)} weapon(s)")
else:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results = [export_combined_viewmodel(weapon) for weapon in WEAPONS]
    manifest = {"weapons": results}
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
