"""Shared helpers for exporting fodpak GLB content from the Blender scripts.

Every model exporter supports two modes: the legacy FBX/Unity flow and the
runtime content-package ("fodpak") flow selected with --format glb --pak-root.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import bpy
import mathutils

# Material or texture names carrying these tokens render as alpha cutout.
CUTOUT_KEYWORDS = ("masked", "fence", "nosight", "transparent")

PAK_FPS = 30

# CoD XModels carry their attachment points (muzzle flash, brass ejection,
# barrel) as skeleton bones named with this prefix.
TAG_PREFIX = "tag_"

FLAGS_WITH_VALUE = ("--format", "--pak-root", "--models")


def parse_cli(values, positional_count: int, usage: str):
    """Split --format/--pak-root/--models flags out of a '--' argument list.

    Returns (positionals, export_format, pak_root, models). Positionals beyond
    positional_count are preserved (per-script selections such as player ids
    or prop names); fewer than positional_count aborts with the usage string.
    """
    positionals: list[str] = []
    export_format = "fbx"
    pak_root: Path | None = None
    models: set[str] | None = None
    index = 0
    while index < len(values):
        value = values[index]
        if value in FLAGS_WITH_VALUE:
            if index + 1 >= len(values):
                raise SystemExit(f"{value} requires a value")
            if value == "--format":
                export_format = values[index + 1].lower()
            elif value == "--pak-root":
                pak_root = Path(values[index + 1]).resolve()
            else:
                models = {
                    item.strip().casefold()
                    for item in values[index + 1].split(",")
                    if item.strip()
                }
            index += 2
        else:
            positionals.append(value)
            index += 1
    if len(positionals) < positional_count:
        raise SystemExit(usage)
    if export_format not in ("fbx", "glb"):
        raise SystemExit(f"unsupported --format: {export_format}")
    if export_format == "glb" and pak_root is None:
        raise SystemExit("--format glb requires --pak-root")
    return positionals, export_format, pak_root, models


def write_json(path: Path, payload: dict) -> None:
    """Write a manifest in the JsonUtility-safe shape (objects and arrays of
    objects only, never dictionary maps, relative paths only).

    Atomic: a killed Blender step must leave the previous manifest intact
    rather than a truncated one, because every reader treats a truncated
    manifest as a corrupt one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def convert_image_to_png(source: Path, destination: Path) -> None:
    """Convert a DDS/TGA/JPG texture to PNG through Blender's image reader."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = bpy.data.images.load(str(source), check_existing=False)
    try:
        # Blender loads pixel data lazily from image.filepath; force the read
        # before retargeting the path at the PNG destination.
        len(image.pixels)
        image.filepath_raw = str(destination)
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def is_cutout(*names: str) -> bool:
    return any(
        keyword in name.casefold()
        for name in names
        for keyword in CUTOUT_KEYWORDS
    )


def scene_mesh_materials() -> list:
    materials = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            if slot.material is not None and slot.material not in materials:
                materials.append(slot.material)
    return materials


def material_source_image(material):
    """The color texture of an importer-built material node graph."""
    if not material.use_nodes or material.node_tree is None:
        return None
    image_nodes = [
        node
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    ]
    if not image_nodes:
        return None
    for node in image_nodes:
        source = node.image.filepath or node.image.name
        if Path(source).stem.casefold() == material.name.casefold():
            return node.image
    return image_nodes[0].image


def build_material_table(texture_dir: Path, texture_rel: str) -> list[dict]:
    """Convert every texture used by scene materials to PNG and return the
    JsonUtility material table [{material, texture, cutout}] with pak-relative
    texture paths."""
    table = []
    converted: dict[Path, str] = {}
    for material in scene_mesh_materials():
        image = material_source_image(material)
        if image is None:
            continue
        source = (
            Path(bpy.path.abspath(image.filepath)) if image.filepath else None
        )
        if source is None or not source.is_file():
            continue
        png_name = converted.get(source)
        if png_name is None:
            png_name = f"{source.stem}.png"
            convert_image_to_png(source, texture_dir / png_name)
            converted[source] = png_name
        table.append(
            {
                "material": material.name,
                "texture": f"{texture_rel}/{png_name}",
                "cutout": is_cutout(material.name, source.stem),
            }
        )
    return table


# ----------------------------------------------------------------- progress

# A silent minute reads exactly like a hang, and the glTF exporter says
# nothing at all between its last "Primitives created" line and the finished
# file. Every long step is wrapped in a heartbeat so both the terminal and the
# in-game boot console keep receiving lines.
HEARTBEAT_SECONDS = 5.0


def emit(line: str) -> None:
    """Write one whole line to stdout and flush it.

    A single write keeps the heartbeat thread from interleaving a partial
    line with the main thread's output, and the flush matters because the
    game runs the exporter with its stdout on a pipe, where Python would
    otherwise block-buffer the console into 8 KB bursts. ASCII only: the
    Windows console encoding cannot take box-drawing or ellipsis characters.
    """
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


@contextlib.contextmanager
def progress_heartbeat(label: str, detail=None, interval: float = HEARTBEAT_SECONDS):
    """Print '... <label> - Ns' every interval until the block finishes.

    detail is an optional callable returning a short suffix (a running item
    count, say). It is called from the heartbeat thread, so it must only read
    plain Python state — never touch bpy, which is not thread-safe.
    """
    started = time.monotonic()
    finished = threading.Event()

    def beat() -> None:
        while not finished.wait(interval):
            suffix = ""
            if detail is not None:
                try:
                    suffix = detail() or ""
                except Exception:
                    suffix = ""
            emit(
                f"  ... {label} - {time.monotonic() - started:.0f}s"
                + (f" ({suffix})" if suffix else "")
            )

    thread = threading.Thread(target=beat, name="fod-progress", daemon=True)
    thread.start()
    emit(f"  {label} ...")
    try:
        yield
    finally:
        finished.set()
        thread.join(timeout=interval)
        emit(f"  done: {label} - {time.monotonic() - started:.0f}s")


# --------------------------------------------------------- glTF export speed

# Blender's glTF exporter turns every child-of-root property into an index
# with, in blender/exp/exporter.py:
#
#     if obj in target: return target.index(obj)
#
# which is a pair of linear scans per append. One player carries 354 clips
# over 80 joints, so the export emits ~75k accessors and ~75k buffer views and
# those scans cost billions of comparisons: minutes of 100% CPU per player,
# silent, growing quadratically with the animation count.
#
# None of the gltf2_io classes define __eq__ or __hash__, so `in` is a plain
# identity test. An id()-keyed map is exactly equivalent and O(1): it returns
# the same indices in the same order, so the glTF structure is unchanged.
# (Sample values still vary in the last float bits between runs, as they did
# before this patch — Blender's evaluation is not bit-reproducible.)
_DEDUP_HOOK = "_GlTF2Exporter__append_unique_and_get_index"


class _GltfExportProgress:
    """Speed patch and live counters for the two long, silent phases.

    An export spends its time first sampling clips and then serialising
    accessors, and reports neither. Counting both gives the heartbeat
    something real to say and makes the dedup fix observable.
    """

    def __init__(self) -> None:
        self._tables: dict[int, dict[int, int]] = {}
        # Strong references to every list we have memoised. Without them a
        # freed list could be replaced by a new one at the same address and
        # inherit its stale indices.
        self._targets: list[list] = []
        self.appended = 0
        self.clips = 0
        self.total_clips = 0
        self.dedup_patched = False
        self.clips_patched = False

    def reset(self, total_clips: int = 0) -> None:
        self._tables.clear()
        self._targets.clear()
        self.appended = 0
        self.clips = 0
        self.total_clips = total_clips

    def append_unique_and_get_index(self, target: list, obj) -> int:
        table = self._tables.get(id(target))
        if table is None:
            table = {}
            self._tables[id(target)] = table
            self._targets.append(target)
        found = table.get(id(obj))
        if found is not None:
            return found
        index = len(target)
        target.append(obj)
        # Every memoised object is referenced by target from here on, so its
        # id() cannot be recycled while the table is alive.
        table[id(obj)] = index
        self.appended += 1
        return index

    def describe(self) -> str:
        """One-line progress summary. Called from the heartbeat thread, so it
        reads only plain counters — never bpy."""
        parts = []
        if self.clips_patched:
            total = f"/{self.total_clips}" if self.total_clips else ""
            parts.append(f"{self.clips}{total} clips sampled")
        if self.dedup_patched:
            parts.append(f"{self.appended:,} accessors written")
        return ", ".join(parts)


_gltf_progress: _GltfExportProgress | None = None


def patch_gltf_exporter(total_clips: int = 0) -> _GltfExportProgress:
    """Install the speed patch and the progress counters.

    Both hooks are internals a Blender upgrade can rename out from under us.
    Neither is load-bearing — without them the stock exporter still writes the
    same file, just slowly and silently — so a miss is reported once and the
    export carries on.
    """
    global _gltf_progress
    if _gltf_progress is not None:
        _gltf_progress.reset(total_clips)
        return _gltf_progress
    progress = _GltfExportProgress()
    progress.reset(total_clips)

    try:
        from io_scene_gltf2.blender.exp.exporter import GlTF2Exporter
    except ImportError as error:
        emit(f"  note: glTF speed patch unavailable ({error})")
        GlTF2Exporter = None
    if GlTF2Exporter is not None:
        if getattr(GlTF2Exporter, _DEDUP_HOOK, None) is None:
            emit(
                "  note: glTF speed patch skipped (Blender "
                f"{bpy.app.version_string} moved {_DEDUP_HOOK}); large "
                "skinned exports will be slow but correct"
            )
        else:
            # staticmethod, not a bare function: the exporter calls this
            # through self, and a plain function would bind and eat `target`
            # as `self`.
            setattr(
                GlTF2Exporter,
                _DEDUP_HOOK,
                staticmethod(progress.append_unique_and_get_index),
            )
            progress.dedup_patched = True

    try:
        from io_scene_gltf2.io.com import gltf2_io

        original_init = gltf2_io.Animation.__init__

        def counting_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            progress.clips += 1

        gltf2_io.Animation.__init__ = counting_init
        progress.clips_patched = True
    except (ImportError, AttributeError) as error:
        emit(f"  note: glTF clip counter unavailable ({error})")

    _patch_gltf_sampling_range()
    _gltf_progress = progress
    return progress


def _patch_gltf_sampling_range() -> None:
    """Sample each clip over its own frame range instead of every clip's.

    In ACTIONS mode the exporter's get_range() returns the union of the frame
    ranges of ALL actions, so every clip is baked over the length of the
    longest one. Our longest player clip is 250 frames and the median is 16:
    that is 91,000 frame evaluations to produce 12,080 frames of animation,
    a 7.5x overshoot, and the surplus frames are discarded afterwards.

    The per-action range is already stored — it is what the NLA_TRACKS branch
    of the same function returns — so this makes ACTIONS take that same
    branch. Verified against the unpatched exporter: a viewmodel came out
    byte-identical, and the player came out structurally identical with fewer
    differing floats than two runs of the same code produce on their own.
    """
    try:
        from io_scene_gltf2.blender.exp.animation.sampled import sampling_cache
    except ImportError as error:
        emit(f"  note: glTF sampling-range patch unavailable ({error})")
        return
    if not hasattr(sampling_cache, "get_range"):
        emit("  note: glTF sampling-range patch skipped (get_range moved)")
        return

    def get_range(obj_uuid, key, export_settings):
        stored = export_settings["ranges"].get(obj_uuid, {}).get(key)
        if stored is None:
            # Materials and lights cache under keys with no stored range;
            # fall back to the exporter's own union so they behave as before.
            return _stock_union_range(export_settings)
        return stored["start"], stored["end"]

    def _stock_union_range(export_settings):
        min_ = max_ = None
        for obj in export_settings["ranges"].keys():
            for anim in export_settings["ranges"][obj].keys():
                entry = export_settings["ranges"][obj][anim]
                if min_ is None or min_ > entry["start"]:
                    min_ = entry["start"]
                if max_ is None or max_ < entry["end"]:
                    max_ = entry["end"]
        return min_, max_

    # Only called from inside its own module, as a module global, so replacing
    # the attribute is enough to redirect the single call site.
    sampling_cache.get_range = get_range


def export_skinned_glb(filepath: Path) -> None:
    """GLB with name-only materials and one glTF animation per action,
    force-sampled at 30 fps for glTFast legacy clip playback."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    bpy.context.scene.render.fps = PAK_FPS
    # Read the clip total here, on the main thread: the heartbeat runs on its
    # own thread and bpy is not thread-safe.
    progress = patch_gltf_exporter(len(bpy.data.actions))
    with progress_heartbeat(f"writing {filepath.name}", progress.describe):
        bpy.ops.export_scene.gltf(
            filepath=str(filepath),
            export_format="GLB",
            export_image_format="NONE",
            export_materials="EXPORT",
            export_yup=True,
            export_apply=False,
            export_skins=True,
            export_def_bones=False,
            export_animations=True,
            export_animation_mode="ACTIONS",
            export_force_sampling=True,
            export_frame_step=1,
            # Sampling every joint in every clip left ~92% of channels holding
            # one repeated value for the whole clip (scale was constant in
            # 100% of them, translation in 98%), which is most of a 53 MB
            # player file. Optimising collapses a constant channel to its
            # first and last keyframe. keep_anim_armature keeps the channel
            # itself, which is what the legacy-clip path needs: a joint with
            # no channel at all is left holding whatever the previous clip
            # posed it to. Clip length survives because the last keyframe does.
            export_optimize_animation_size=True,
            export_optimize_animation_keep_anim_armature=True,
            # Sampling steps the scene frame by frame, and every step
            # re-evaluated all 32 skinned meshes alongside the rig even though
            # the meshes were gathered long before and no sample reads them.
            # This hides them for the bake and restores them after. Purely a
            # cost setting: bone matrices do not depend on mesh visibility.
            export_optimize_disable_viewport=True,
            export_anim_single_armature=True,
            export_bake_animation=False,
        )


def tag_bone_transforms(
    prefix: str = TAG_PREFIX,
) -> list[tuple[str, mathutils.Matrix]]:
    """World-space rest transforms of every tag_* bone in the scene armatures.

    The rig is the only place the attachment points survive an XModel import,
    so they have to be read before prepare_static_scene() removes it.
    """
    bpy.context.view_layer.update()
    transforms: list[tuple[str, mathutils.Matrix]] = []
    seen: set[str] = set()
    for obj in bpy.context.scene.objects:
        if obj.type != "ARMATURE":
            continue
        for bone in obj.data.bones:
            name = bone.name
            if not name.casefold().startswith(prefix) or name in seen:
                continue
            seen.add(name)
            transforms.append((name, obj.matrix_world @ bone.matrix_local))
    return transforms


def add_tag_empties(transforms) -> list[str]:
    """Re-create captured tag transforms as parentless empties.

    glTF writes an object without mesh data as a node without a mesh, which is
    exactly what FodGlbStaticLoader turns into a plain named GameObject. The
    bone scale (the 0.0254 inch conversion) is dropped so the node carries a
    clean rotation and a metre-space translation.
    """
    names = []
    for name, matrix in transforms:
        location, rotation, _ = matrix.decompose()
        empty = bpy.data.objects.new(name, None)
        empty.empty_display_type = "ARROWS"
        empty.empty_display_size = 0.05
        bpy.context.scene.collection.objects.link(empty)
        empty.matrix_world = (
            mathutils.Matrix.Translation(location)
            @ rotation.to_matrix().to_4x4()
        )
        # bpy.data.objects.new uniquifies on collision; the contract is a
        # literal node name, so a clash has to fail loudly instead.
        if empty.name != name:
            raise RuntimeError(f"tag name {name!r} collides with an existing object")
        names.append(name)
    return names


def prepare_static_scene(keep_tags: bool = False) -> list[str]:
    """Bake world transforms onto mesh objects and drop rigs/empties so the
    static GLB carries plain TRS mesh nodes (FodGlbStaticLoader contract:
    no skins, no animations).

    With keep_tags the rig's tag_* bones are re-emitted as empties in the same
    baked space as the meshes; returns the tag names written.
    """
    tags = tag_bone_transforms() if keep_tags else []
    # Propagate the freshly applied root scale into child world matrices.
    bpy.context.view_layer.update()
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    for mesh in meshes:
        world = mesh.matrix_world.copy()
        mesh.parent = None
        mesh.matrix_world = world
        for modifier in list(mesh.modifiers):
            mesh.modifiers.remove(modifier)
    for obj in list(bpy.context.scene.objects):
        if obj.type != "MESH":
            bpy.data.objects.remove(obj, do_unlink=True)
    return add_tag_empties(tags)


def export_static_glb(filepath: Path) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    progress = patch_gltf_exporter()
    with progress_heartbeat(f"writing {filepath.name}", progress.describe):
        bpy.ops.export_scene.gltf(
            filepath=str(filepath),
            export_format="GLB",
            export_image_format="NONE",
            export_materials="EXPORT",
            export_yup=True,
            export_apply=False,
            export_skins=False,
            export_animations=False,
        )


def viewmodel_anim_keys(names) -> list[str]:
    """Strip the shared xanim prefix (e.g. viewmodel_mp40_) so pak clips can
    be renamed <id>_combined_<key> while preserving the exact suffix tokens
    the runtime matches (fire, ADS_up, reload_empty, ...)."""
    names = list(names)
    if len(names) > 1:
        prefix = os.path.commonprefix(names)
        prefix = prefix[: prefix.rfind("_") + 1]
    elif names and names[0].startswith("viewmodel_"):
        prefix = "viewmodel_"
    else:
        prefix = ""
    keys = []
    for name in names:
        key = name[len(prefix):] if prefix and name.startswith(prefix) else name
        keys.append(key or name)
    return keys
