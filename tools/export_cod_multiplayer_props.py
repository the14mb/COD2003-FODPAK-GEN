"""Blender exporter for XModels used by the approved multiplayer map roster.

Invoke:
  blender --background --python tools/export_cod_multiplayer_props.py -- \
    SOURCE MODELS_JSON OUTPUT IMPORTER_PY_ROOT [model_name ...]

Pak mode (runtime content package):
  ... -- SOURCE MODELS_JSON_OR_- OUTPUT IMPORTER_PY_ROOT \
      --format glb --pak-root CONTENT_DIR [model_name ...]
Reads CONTENT_DIR/props/required_models.json when present (MODELS_JSON as a
fallback, "-" for none) and writes CONTENT_DIR/props/<model>/prop.glb plus
props.json.

Model output identifiers are case-folded. CoD map entity references are
case-insensitive and several stock BSPs refer to the same XModel with different
capitalization.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Quaternion, Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fod_export_common import (
    build_material_table,
    export_skinned_glb,
    export_static_glb,
    load_json,
    parse_cli,
    prepare_static_scene,
    tag_bone_transforms,
    write_json,
)
from cod1_multiplayer_closure import (
    MOUNTED_MG42_ANIMATIONS,
    MOUNTED_MG42_MODEL,
    MOUNTED_MG42_WEAPON_FILES,
)
from cod1_xanim import load as load_xanim, sample as sample_xanim


COD_UNIT_TO_METRE = 0.0254
PROP_EXPORT_FINGERPRINT_VERSION = 1


def arguments() -> tuple[Path, str, Path, Path, str, Path | None, set[str] | None]:
    values = sys.argv[sys.argv.index("--") + 1 :]
    positionals, export_format, pak_root, models = parse_cli(
        values,
        4,
        "expected SOURCE MODELS_JSON OUTPUT IMPORTER_PY_ROOT "
        "[--format fbx|glb] [--pak-root DIR] [model_name ...]",
    )
    selected = {value.casefold() for value in positionals[4:]}
    if models:
        selected |= models
    return (
        Path(positionals[0]).resolve(),
        positionals[1],
        Path(positionals[2]).resolve(),
        Path(positionals[3]).resolve(),
        export_format,
        pak_root,
        selected or None,
    )


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for collection in (
        bpy.data.meshes,
        bpy.data.armatures,
        bpy.data.materials,
        bpy.data.images,
    ):
        for datablock in list(collection):
            collection.remove(datablock)


def file_images() -> list[tuple[object, Path]]:
    result = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        path = Path(bpy.path.abspath(image.filepath))
        if path.is_file():
            result.append((image, path))
    return result


def prepare_fbx_materials() -> list[dict[str, str]]:
    bindings = []
    for material in bpy.data.materials:
        if not material.use_nodes or material.node_tree is None:
            continue
        image_nodes = [
            node
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image is not None
        ]
        if not image_nodes:
            continue
        image = image_nodes[0].image
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (400, 0)
        principled = nodes.new("ShaderNodeBsdfPrincipled")
        principled.location = (100, 0)
        texture = nodes.new("ShaderNodeTexImage")
        texture.location = (-250, 0)
        texture.image = image
        links.new(texture.outputs["Color"], principled.inputs["Base Color"])
        links.new(texture.outputs["Alpha"], principled.inputs["Alpha"])
        links.new(principled.outputs["BSDF"], output.inputs["Surface"])
        principled.inputs["Roughness"].default_value = 0.65
        material.diffuse_color = (1.0, 1.0, 1.0, 1.0)
        bindings.append(
            {
                "material": material.name,
                "texture": Path(
                    bpy.path.abspath(image.filepath)
                ).name,
            }
        )
    return bindings


def source_model_index(source_root: Path) -> dict[str, Path]:
    return {
        path.name.casefold(): path
        for path in (source_root / "xmodel").iterdir()
        if path.is_file()
    }


def prop_export_fingerprint(importer, source_root: Path) -> str:
    """Fingerprint the compiler inputs that can change generated prop GLBs."""
    wrapper = Path(importer.__file__).resolve()
    source_manifest = source_root / "extraction_manifest.json"
    candidates = [
        Path(__file__).resolve(),
        Path(__file__).resolve().with_name("fod_export_common.py"),
        wrapper,
        wrapper.with_name("__init__.py"),
        source_manifest,
    ]
    candidates.extend(
        path
        for path in wrapper.parent.glob("cod_asset_importer.*")
        if path.suffix.lower() in (".so", ".pyd")
    )
    if not source_manifest.is_file():
        candidates.extend(
            path
            for path in sorted(
                source_root.rglob("*"),
                key=lambda item: item.as_posix().casefold(),
            )
            if path.is_file()
        )
    files = []
    seen = set()
    for path in candidates:
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        try:
            label = (
                "source/" +
                path.resolve().relative_to(source_root).as_posix()
            )
        except ValueError:
            label = path.name
        files.append(
            {
                "name": label,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    payload = {
        "version": PROP_EXPORT_FINGERPRINT_VERSION,
        "lodApi": getattr(importer, "XMODEL_LOD_API_VERSION", 0),
        "files": sorted(files, key=lambda item: item["name"].casefold()),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def merge_material_bindings(
    target: dict[str, dict[str, object]],
    bindings: list[dict[str, object]],
) -> None:
    """Merge per-LOD material tables without changing runtime lookup keys."""
    for binding in bindings:
        key = str(binding["material"]).casefold()
        existing = target.get(key)
        if existing is not None and existing != binding:
            raise RuntimeError(
                "LOD material binding conflict for "
                f"{binding['material']!r}: {existing!r} != {binding!r}"
            )
        target[key] = binding


def validate_authored_lods(
    model_id: str,
    lods: list[dict[str, object]],
) -> None:
    if not lods:
        raise RuntimeError(f"{model_id}: XModel declares no LOD surfaces")
    previous_distance = 0.0
    for index, lod in enumerate(lods):
        surface = lod.get("sourceSurface")
        distance = lod.get("distance")
        if not isinstance(surface, str) or not surface.strip():
            raise RuntimeError(
                f"{model_id}: LOD {index} has no source surface"
            )
        if (
            isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or not math.isfinite(float(distance))
            or float(distance) < 0.0
        ):
            raise RuntimeError(
                f"{model_id}: LOD {index} has invalid distance {distance!r}"
            )
        distance = float(distance)
        is_last = index == len(lods) - 1
        if not is_last and distance <= previous_distance:
            raise RuntimeError(
                f"{model_id}: LOD {index} distance {distance} does not "
                "increase"
            )
        if is_last and distance > 0.0 and distance <= previous_distance:
            raise RuntimeError(
                f"{model_id}: final cull distance {distance} does not "
                "follow the previous LOD boundary"
            )
        if distance > 0.0:
            previous_distance = distance


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
        x, y, z, w = sample_xanim(
            track.rotation,
            frame,
            quaternion=True,
        )
        matrix = Quaternion((w, x, y, z)).to_matrix().to_4x4()
    matrix.translation = bind_local.translation
    if track.translation is not None:
        matrix.translation += (
            Vector(sample_xanim(track.translation, frame))
            * COD_UNIT_TO_METRE
        )
    return matrix


def add_xanim_action(
    source_root: Path,
    armature,
    animation_name: str,
) -> dict[str, object]:
    animation = load_xanim(source_root / "xanim" / animation_name)
    action = bpy.data.actions.new(animation.name)
    action.use_fake_user = True
    armature.animation_data_create()
    armature.animation_data.action = action
    for pose_bone in armature.pose.bones:
        pose_bone.matrix_basis = Matrix.Identity(4)
        pose_bone.rotation_mode = "QUATERNION"
    tracks = {track.name: track for track in animation.bones}
    entries = [
        (
            pose_bone,
            tracks.get(pose_bone.name.casefold()),
            bind_local_matrix(pose_bone),
        )
        for pose_bone in armature.pose.bones
    ]
    animated_bones = sum(track is not None for _bone, track, _bind in entries)
    for frame in range(animation.frame_count):
        targets = {}
        for bone, track, bind_local in entries:
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
                "rotation_quaternion",
                frame=frame,
                group=bone.name,
            )
    action["source_framerate"] = animation.frame_rate
    action["source_looped"] = animation.looped
    return {
        "name": animation.name,
        "frames": animation.frame_count,
        "fps": animation.frame_rate,
        "looped": animation.looped,
        "animatedBones": animated_bones,
        "sourceBones": len(animation.bones),
    }


def prepare_animated_mg42(source_root: Path) -> list[dict[str, object]]:
    armatures = [
        obj for obj in bpy.context.scene.objects
        if obj.type == "ARMATURE"
    ]
    if len(armatures) != 1:
        raise RuntimeError(
            "mg42_bipod: expected one weapon armature, found "
            f"{len(armatures)}"
        )
    armature = armatures[0]
    scale = Matrix.Scale(COD_UNIT_TO_METRE, 4)
    armature.data.transform(scale)
    for mesh in (
        obj for obj in bpy.context.scene.objects
        if obj.type == "MESH"
    ):
        mesh.data.transform(scale)
    records = []
    for reference in MOUNTED_MG42_ANIMATIONS:
        action = add_xanim_action(
            source_root,
            armature,
            reference.name,
        )
        if (
            action["looped"] != reference.looped
            or action["frames"] != reference.frame_count
            or action["fps"] != reference.frame_rate
            or action["animatedBones"] != action["sourceBones"]
        ):
            raise RuntimeError(
                f"{reference.name}: incompatible mounted MG42 XAnim "
                f"({action['animatedBones']}/{action['sourceBones']} bones)"
            )
        records.append(
            {
                **action,
                "sourceWeaponFile": reference.source_weapon_file,
                "sourceField": reference.source_field,
                "stance": reference.stance,
                "phase": reference.phase,
                "vertical": reference.vertical,
                "role": reference.role,
            }
        )
    world = armature.matrix_world.copy()
    armature.parent = None
    armature.matrix_world = world
    for obj in list(bpy.context.scene.objects):
        if obj.type == "EMPTY":
            bpy.data.objects.remove(obj, do_unlink=True)
    return records


def export_model(
    source_root: Path,
    source_model: Path,
    output_root: Path,
    model_id: str,
    aliases: list[str],
    importer,
) -> dict[str, object]:
    clear_scene()
    importer.import_xmodel(
        asset_path=str(source_root),
        file_path=str(source_model),
        selected_version=importer.GAME_VERSION.CoD,
    )
    meshes = [
        obj for obj in bpy.context.scene.objects if obj.type == "MESH"
    ]
    if not meshes:
        raise RuntimeError("import produced no mesh objects")

    asset_dir = output_root / model_id
    texture_dir = asset_dir / "Textures"
    asset_dir.mkdir(parents=True, exist_ok=True)
    texture_dir.mkdir(parents=True, exist_ok=True)
    copied_textures = []
    for image, source in file_images():
        destination = texture_dir / source.name
        shutil.copy2(source, destination)
        image.filepath = str(destination)
        copied_textures.append(destination.name)
    material_bindings = prepare_fbx_materials()

    for obj in bpy.context.scene.objects:
        if obj.parent is None:
            obj.scale = tuple(
                component * COD_UNIT_TO_METRE for component in obj.scale
            )

    fbx_path = asset_dir / f"{model_id}.fbx"
    bpy.ops.export_scene.fbx(
        filepath=str(fbx_path),
        use_selection=False,
        apply_unit_scale=True,
        bake_space_transform=False,
        add_leaf_bones=False,
        path_mode="COPY",
        embed_textures=False,
        object_types={"EMPTY", "MESH", "ARMATURE"},
    )
    return {
        "id": model_id,
        "sourceName": source_model.name,
        "aliases": aliases,
        "fbx": fbx_path.name,
        "meshes": len(meshes),
        "vertices": sum(len(obj.data.vertices) for obj in meshes),
        "triangles": sum(len(obj.data.polygons) for obj in meshes),
        "textures": sorted(set(copied_textures), key=str.casefold),
        "materials": material_bindings,
    }


def tag_pose_records() -> list[dict[str, object]]:
    """Rest transforms of the rig's tag_* attachment points, in GLB space.

    Blender is Z-up and the GLB export is Y-up, so the same
    ``(x, z, -y)`` swizzle export_scene.gltf applies to geometry is applied
    here — a consumer reading these against the exported mesh must not have
    to know they were captured in a different basis. The runtime mirrors X
    on load exactly as it does for the glTF nodes themselves.
    """
    records: list[dict[str, object]] = []
    for name, matrix in tag_bone_transforms():
        translation = matrix.to_translation()
        records.append(
            {
                "name": name,
                "position": [
                    round(translation.x, 6),
                    round(translation.z, 6),
                    round(-translation.y, 6),
                ],
            }
        )
    records.sort(key=lambda record: record["name"])
    return records


def export_prop_pak(
    source_root: Path,
    source_model: Path,
    props_root: Path,
    model_id: str,
    importer,
    export_authored_lods: bool,
) -> dict[str, object]:
    clear_scene()
    importer.import_xmodel(
        asset_path=str(source_root),
        file_path=str(source_model),
        selected_version=importer.GAME_VERSION.CoD,
    )
    if not any(obj.type == "MESH" for obj in bpy.context.scene.objects):
        raise RuntimeError("import produced no mesh objects")
    if model_id == MOUNTED_MG42_MODEL:
        materials = build_material_table(
            props_root / model_id / "textures",
            f"props/{model_id}/textures",
        )
        animations = prepare_animated_mg42(source_root)
        # Capture the rig's attachment points BEFORE the export, in the same
        # baked space the meshes land in. The dedicated server builds no
        # visual at all, so tag_flash was the one datum it could not reach:
        # it fell back to a guessed "playerDistance straight ahead at ground
        # level", which put the authoritative muzzle 0.95 m in front of the
        # real barrel and 0.39 m below it — the server and the client
        # disagreeing about where a burst starts. Shipping the rest pose as
        # data lets both roles resolve the identical origin.
        tag_rest = tag_pose_records()
        export_skinned_glb(props_root / model_id / "prop.glb")
        return {
            "name": model_id,
            "glb": f"props/{model_id}/prop.glb",
            "materials": materials,
            "animated": True,
            "sourceWeaponFiles": list(MOUNTED_MG42_WEAPON_FILES),
            "animations": animations,
            "tags": tag_rest,
        }

    authored_lods = None
    if export_authored_lods:
        authored_lods = importer.inspect_xmodel_lods(
            asset_path=str(source_root),
            file_path=str(source_model),
            selected_version=importer.GAME_VERSION.CoD,
        )
        if authored_lods is None:
            raise RuntimeError(
                "importer advertised authored LOD support but returned no "
                "LOD inventory"
            )
        validate_authored_lods(model_id, authored_lods)
    else:
        authored_lods = [
            {
                "sourceSurface": source_model.name,
                "distance": 0.0,
            }
        ]

    asset_dir = props_root / model_id
    if export_authored_lods:
        for stale in asset_dir.glob("lod*.glb"):
            stale.unlink()

    material_bindings: dict[str, dict[str, object]] = {}
    lod_records = []
    for lod_index, authored_lod in enumerate(authored_lods):
        if lod_index:
            clear_scene()
            importer.import_xmodel(
                asset_path=str(source_root),
                file_path=str(source_model),
                selected_version=importer.GAME_VERSION.CoD,
                lod_index=lod_index,
            )
            if not any(
                obj.type == "MESH"
                for obj in bpy.context.scene.objects
            ):
                raise RuntimeError(
                    f"LOD {lod_index} import produced no mesh objects"
                )

        merge_material_bindings(
            material_bindings,
            build_material_table(
                asset_dir / "textures",
                f"props/{model_id}/textures",
            ),
        )
        for obj in bpy.context.scene.objects:
            if obj.parent is None:
                obj.scale = tuple(
                    component * COD_UNIT_TO_METRE
                    for component in obj.scale
                )
        prepare_static_scene()
        filename = "prop.glb" if lod_index == 0 else f"lod{lod_index}.glb"
        export_static_glb(asset_dir / filename)
        if export_authored_lods:
            lod_records.append(
                {
                    "glb": f"props/{model_id}/{filename}",
                    "sourceSurface": authored_lod["sourceSurface"],
                    "distance": float(authored_lod["distance"]),
                }
            )

    result = {
        "name": model_id,
        "glb": f"props/{model_id}/prop.glb",
        "materials": [
            material_bindings[key]
            for key in sorted(material_bindings)
        ],
        "animated": False,
        "sourceWeaponFiles": [],
        "animations": [],
    }
    if export_authored_lods:
        result["lods"] = lod_records
    return result


def write_props_manifest(
    manifest_path: Path,
    results: dict[str, dict],
    failures: list[dict],
    requested: int,
    export_fingerprint: str,
) -> None:
    write_json(
        manifest_path,
        {
            "format": "FriendsOfDuty.Props",
            "version": 2,
            "exportFingerprint": export_fingerprint,
            "requested": requested,
            "props": [results[key] for key in sorted(results)],
            "failures": failures,
        },
    )


def normalize_existing_prop(
    model_id: str,
    model: dict[str, object],
    require_authored_lods: bool,
) -> dict[str, object] | None:
    """Validate resumable records against the current metadata contract."""
    normalized = dict(model)
    if model_id != MOUNTED_MG42_MODEL:
        normalized["animated"] = False
        normalized["sourceWeaponFiles"] = []
        normalized["animations"] = []
        if require_authored_lods:
            lods = normalized.get("lods")
            if not isinstance(lods, list) or not lods:
                return None
            try:
                validate_authored_lods(model_id, lods)
            except RuntimeError:
                return None
            if lods[0].get("glb") != normalized.get("glb"):
                return None
        return normalized

    animations = normalized.get("animations")
    source_weapon_files = normalized.get("sourceWeaponFiles")
    if (
        normalized.get("animated") is not True
        or source_weapon_files != list(MOUNTED_MG42_WEAPON_FILES)
        or not isinstance(animations, list)
        or len(animations) != len(MOUNTED_MG42_ANIMATIONS)
    ):
        return None
    expected_names = {
        reference.name for reference in MOUNTED_MG42_ANIMATIONS
    }
    actual_names = {
        record.get("name")
        for record in animations
        if isinstance(record, dict)
    }
    return normalized if actual_names == expected_names else None


def exported_prop_files_exist(
    pak_root: Path,
    model: dict[str, object],
) -> bool:
    paths = [model.get("glb")]
    lods = model.get("lods")
    if isinstance(lods, list):
        paths.extend(
            lod.get("glb")
            for lod in lods
            if isinstance(lod, dict)
        )
    return all(
        isinstance(path, str) and (pak_root / path).is_file()
        for path in paths
    )


def pak_model_names(models_json: str, props_root: Path) -> list[str]:
    required_path = props_root / "required_models.json"
    if required_path.is_file():
        payload = load_json(required_path)
    elif models_json != "-":
        payload = load_json(Path(models_json).resolve())
    else:
        raise SystemExit(
            f"missing {required_path} and no MODELS_JSON fallback provided"
        )
    return payload if isinstance(payload, list) else payload["models"]


def run_pak_export(
    source_root: Path,
    models_json: str,
    pak_root: Path,
    selected: set[str] | None,
    importer,
) -> None:
    props_root = pak_root / "props"
    export_authored_lods = (
        getattr(importer, "XMODEL_LOD_API_VERSION", 0) >= 1
        and callable(getattr(importer, "inspect_xmodel_lods", None))
    )
    if not export_authored_lods:
        raise SystemExit(
            "cod-asset-importer v3.6+ with authored XModel LOD API 1 is "
            "required for production prop export. The installed v3.5/API0 "
            "module can only emit LOD0, so this export was stopped instead "
            "of silently producing an incomplete package. Use the exporter's "
            "'Prepare importer' action, or run build_importer.py with "
            "--require-lod from your Python interpreter, then retry."
        )
    export_fingerprint = prop_export_fingerprint(importer, source_root)
    aliases_by_id: dict[str, list[str]] = {}
    for name in pak_model_names(models_json, props_root):
        aliases_by_id.setdefault(name.casefold(), []).append(name)
    if selected is not None:
        aliases_by_id = {
            model_id: aliases
            for model_id, aliases in aliases_by_id.items()
            if model_id in selected
        }

    source_models = source_model_index(source_root)
    props_root.mkdir(parents=True, exist_ok=True)
    manifest_path = props_root / "props.json"
    existing_results = {}
    if manifest_path.is_file():
        try:
            existing_manifest = load_json(manifest_path)
            if (
                existing_manifest.get("exportFingerprint")
                == export_fingerprint
            ):
                existing_results = {
                    model["name"]: model
                    for model in existing_manifest.get("props", [])
                }
            else:
                print(
                    "prop compiler fingerprint changed; invalidating "
                    "previous GLBs"
                )
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    existing_results = {
        model_id: normalized
        for model_id, model in existing_results.items()
        if (
            normalized := normalize_existing_prop(
                model_id,
                model,
                export_authored_lods,
            )
        ) is not None
    }

    # Prune models that disappeared from the required set so stale props
    # cannot survive an incremental export or compiler-fingerprint
    # invalidation into the pak.
    asset_directories = {
        path.name.casefold(): path
        for path in props_root.iterdir()
        if path.is_dir()
    }
    stale_model_ids = sorted(
        (set(existing_results) | set(asset_directories)) -
        set(aliases_by_id)
    )
    for model_id in stale_model_ids:
        stale_directory = asset_directories.get(
            model_id,
            props_root / model_id,
        )
        if stale_directory.is_dir():
            shutil.rmtree(stale_directory)
        print(f"pruned stale model: {model_id}")
    results = {
        model_id: model
        for model_id, model in existing_results.items()
        if model_id in aliases_by_id
    }
    failures = []
    model_ids = sorted(aliases_by_id)
    for index, model_id in enumerate(model_ids, 1):
        if (
            model_id in results
            and exported_prop_files_exist(pak_root, results[model_id])
        ):
            print(f"[{index}/{len(model_ids)}] unchanged: {model_id}")
            continue
        source_model = source_models.get(model_id)
        if source_model is None:
            failures.append(
                {"name": model_id, "error": "source XModel is missing"}
            )
            print(f"[{index}/{len(model_ids)}] MISSING: {model_id}")
            continue
        print(f"[{index}/{len(model_ids)}] export: {model_id}")
        try:
            results[model_id] = export_prop_pak(
                source_root,
                source_model,
                props_root,
                model_id,
                importer,
                export_authored_lods,
            )
        except Exception as error:
            failures.append({"name": model_id, "error": str(error)})
            print(f"FAILED {model_id}: {error}")

        # Keep long conversion runs resumable even if Blender is interrupted.
        write_props_manifest(
            manifest_path,
            results,
            failures,
            len(model_ids),
            export_fingerprint,
        )

    write_props_manifest(
        manifest_path,
        results,
        failures,
        len(model_ids),
        export_fingerprint,
    )
    print(
        "PAK PROP EXPORT COMPLETE — "
        f"requested={len(model_ids)}, "
        f"available={len(results)}, failures={len(failures)}"
    )
    if failures:
        raise SystemExit(
            "One or more multiplayer props failed; see props.json"
        )


def main() -> None:
    source_root, models_json, output_root, importer_root, export_format, pak_root, selected = (
        arguments()
    )
    sys.path.insert(0, str(importer_root))
    from cod_asset_importer import importer
    from cod_asset_importer.cod_asset_importer import GAME_VERSION

    importer.GAME_VERSION = GAME_VERSION

    if export_format == "glb":
        run_pak_export(source_root, models_json, pak_root, selected, importer)
        return

    payload = json.loads(
        Path(models_json).resolve().read_text(encoding="utf-8")
    )
    aliases_by_id: dict[str, list[str]] = {}
    for name in payload["models"]:
        aliases_by_id.setdefault(name.casefold(), []).append(name)
    if selected is not None:
        aliases_by_id = {
            model_id: aliases
            for model_id, aliases in aliases_by_id.items()
            if model_id in selected
        }

    source_models = source_model_index(source_root)
    output_root.mkdir(parents=True, exist_ok=True)
    existing_manifest_path = output_root / "manifest.json"
    existing_results = {}
    if existing_manifest_path.is_file():
        try:
            existing = json.loads(
                existing_manifest_path.read_text(encoding="utf-8")
            )
            existing_results = {
                model["id"]: model
                for model in existing.get("models", [])
            }
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    # The output is a generated library for the current curated rotation.
    # Prune models that disappeared from the catalog so editor-only spawn
    # soldiers and weapon markers cannot survive an incremental export.
    stale_model_ids = sorted(set(existing_results) - set(aliases_by_id))
    for model_id in stale_model_ids:
        stale_directory = output_root / model_id
        if stale_directory.is_dir():
            shutil.rmtree(stale_directory)
        stale_meta = output_root / f"{model_id}.meta"
        if stale_meta.is_file():
            stale_meta.unlink()
        print(f"pruned stale model: {model_id}")
    results = {
        model_id: model
        for model_id, model in existing_results.items()
        if model_id in aliases_by_id
    }
    failures = []
    model_ids = sorted(aliases_by_id)
    for index, model_id in enumerate(model_ids, 1):
        aliases = sorted(
            aliases_by_id[model_id], key=str.casefold
        )
        fbx = output_root / model_id / f"{model_id}.fbx"
        if model_id in results and fbx.is_file():
            print(f"[{index}/{len(model_ids)}] unchanged: {model_id}")
            continue
        source_model = source_models.get(model_id)
        if source_model is None:
            failures.append(
                {
                    "id": model_id,
                    "aliases": aliases,
                    "error": "source XModel is missing",
                }
            )
            print(f"[{index}/{len(model_ids)}] MISSING: {model_id}")
            continue
        print(f"[{index}/{len(model_ids)}] export: {model_id}")
        try:
            results[model_id] = export_model(
                source_root,
                source_model,
                output_root,
                model_id,
                aliases,
                importer,
            )
        except Exception as error:
            failures.append(
                {
                    "id": model_id,
                    "aliases": aliases,
                    "error": str(error),
                }
            )
            print(f"FAILED {model_id}: {error}")

        # Keep long conversion runs resumable even if Blender is interrupted.
        existing_manifest_path.write_text(
            json.dumps(
                {
                    "format": (
                        "FriendsOfDuty.OriginalMultiplayerPropLibrary"
                    ),
                    "version": 1,
                    "models": [
                        results[key] for key in sorted(results)
                    ],
                    "failures": failures,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    manifest = {
        "format": "FriendsOfDuty.OriginalMultiplayerPropLibrary",
        "version": 1,
        "requested": len(model_ids),
        "models": [results[key] for key in sorted(results)],
        "failures": failures,
    }
    existing_manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "MULTIPLAYER PROP EXPORT COMPLETE — "
        f"requested={len(model_ids)}, "
        f"available={len(results)}, failures={len(failures)}"
    )
    if failures:
        raise SystemExit(
            "One or more multiplayer props failed; see manifest.json"
        )


if __name__ == "__main__":
    main()
