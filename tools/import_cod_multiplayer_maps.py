#!/usr/bin/env python3
"""Import the approved Friends of Duty CoD1/UO multiplayer map rotation.

The importer deliberately mirrors the proven PAVLOV pipeline:

* merge official PK3 archives in game load order;
* export IBSP v59 render geometry as a metre-scaled-on-import OBJ;
* resolve the original DDS/TGA/JPG diffuse textures;
* preserve all authored entities in JSON and expose props, lights and spawns;
* write one catalog consumed by the Unity scene builder.

Only Arnhem, Cassino, Carentan, Pavlov, Chateau, Railyard and Rocket can pass
the shipping allowlist. Shared presentation and map assets remain MP-only.

Two output modes:

* legacy Unity mode (default): writes OBJ/MTL/JSON into the Unity project
  for the editor scene builders.  PAVLOV is catalogued but not regenerated
  because the project already contains its hand-tuned source.
* pak mode (--pak-root): writes the runtime content-package layout from
  Docs/CONTENT_PIPELINE.md §1.2 — per map world.glb (fod_glb_writer),
  pre-partitioned sectors/ plus optimization.json (validated v59 BSP/PVS),
  entities.json, materials.json, sky.png and ambience/, shared PNG textures,
  maps/catalog.json (format v2) and props/required_models.json. There is no
  PAVLOV special case in pak mode; mp_pavlov regenerates from pak5.pk3 like
  every other map.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from build_pavlov_sky_panorama import build as build_sky_panorama
from fod_decal_alpha import build_alpha as build_decal_alpha
from fod_glb_writer import write_glb
from cod1_archive_policy import (
    COD1_TIER,
    UO_TIER,
    official_archives as policy_official_archives,
)
from cod1_impact_table import (
    parse_impact_table,
    surface_vocabulary as impact_table_surface_vocabulary,
)
from cod1_multiplayer_closure import HEALTH_PICKUP_MODEL
from cod1_shipping_maps import (
    ALL_SHIPPING_MAP_IDS,
    SHIPPING_MAP_SPECS,
    normalize_requested_maps,
    shipping_map_specs,
)
from cod1_script_exploder import (
    ExploderEffectClosure,
    efx_output_path as exploder_efx_output_path,
    map_exploder_effects,
    texture_output_path as exploder_texture_output_path,
)


COD_UNIT_TO_METRE = 0.0254
ENTITY_LUMP_INDEX = 29
MODEL_LUMP_INDEX = 27
MODEL_LUMP_STRIDE = 48
MATERIAL_LUMP_INDEX = 0
MATERIAL_LUMP_STRIDE = 72
LIGHTMAP_LUMP_INDEX = 1
# CoD1/UO IBSP v59 surface lightmaps are fixed 512x512x3 8-bit RGB pages stored
# in dLDR overbright form (decode = clamp(stored * 2)); see M3 spec.
LIGHTMAP_PAGE_DIM = 512
LIGHTMAP_PAGE_BYTES = LIGHTMAP_PAGE_DIM * LIGHTMAP_PAGE_DIM * 3
LIGHTMAP_DECODE_MULTIPLIER = 2.0
# A draw-surface's lightmap-page field is a u16; 0xFFFF marks an unlit surface.
LIGHTMAP_UNLIT_PAGE = 0xFFFF
BRUSH_SIDE_LUMP_INDEX = 3
BRUSH_SIDE_LUMP_STRIDE = 8
BRUSH_LUMP_INDEX = 4
BRUSH_LUMP_STRIDE = 4
PLANE_LUMP_INDEX = 2
PLANE_LUMP_STRIDE = 16
CELL_LUMP_INDEX = 17
CELL_LUMP_STRIDE = 52
NODE_LUMP_INDEX = 20
NODE_LUMP_STRIDE = 36
LEAF_LUMP_INDEX = 21
LEAF_LUMP_STRIDE = 36
LEAF_SURFACE_LUMP_INDEX = 23
LEAF_SURFACE_LUMP_STRIDE = 4
VISIBILITY_LUMP_INDEX = 28
LIGHT_LUMP_INDEX = 30
LIGHT_LUMP_STRIDE = 72
DIRECTIONAL_LIGHT_TYPE = 1
SUPPORTED_VERSION = 59
MAP_PIPELINE_VERSION = 20
MAP_OPTIMIZATION_FORMAT = "FriendsOfDuty.MapOptimization"
MAP_OPTIMIZATION_VERSION = 2
RENDER_SECTOR_SIZE_METRES = 32.0
# The proven Pavlov build_assets.py resolver only ever considered the two
# native image containers; JPG/PNG variants are a last-resort tier reached
# when no candidate name ships a DDS/TGA at all.
PRIMARY_TEXTURE_EXTENSIONS = (".dds", ".tga")
FALLBACK_TEXTURE_EXTENSIONS = (".jpg", ".jpeg", ".png")
TEXTURE_EXTENSIONS = PRIMARY_TEXTURE_EXTENSIONS + FALLBACK_TEXTURE_EXTENSIONS
# The proven Pavlov build_assets.py converter capped every world texture at
# 512px (LANCZOS): all 14 of the baked scene's 1024x1024 DDS sources shipped
# as 512x512 PNGs that a 512-cap LANCZOS resize reproduces pixel-exactly
# (83/85 textures byte-identical; the 2 decals differ only by their separate
# alpha-reconstruction pass). The pak textures must match that output or
# close-up surfaces read differently than the proven PavlovFpsDemo scene.
MAX_WORLD_TEXTURE_SIZE = 512
# Manual close-variant substitutions ported from the proven Pavlov
# build_assets.py resolver: these trim images never shipped standalone, and
# the substitute must win over the material's own JPG-only variant.
TEXTURE_SUBSTITUTIONS = {
    "textures/normandy/trim/wood@trim_beam1":
        "textures/normandy/trim/wood@trim_beam2",
    "textures/belgium/trim/wood@trim_beam1":
        "textures/normandy/trim/wood@trim_beam2",
}
# Friends of Duty ships Deathmatch and Team Deathmatch only, so no map
# entity belonging to another game type is exported. These are the exact
# script_gameobjectname tokens maps\\mp\\gametypes\\_gameobjects reads as its
# per-mode allow list; the key is SPACE-SEPARATED, so an entity shared by two
# modes carries something like "dom retrieval" and any one token disqualifies
# it. Exploders outrank this: mp_pavlov's destroyable set pieces are named
# "bombzone" and carry a script_exploder id, and those stay.
OBJECTIVE_GAMEOBJECT_TOKENS = frozenset(
    ("bombzone", "re", "hq", "ctf", "dom", "flag_cap")
)
# Entity classes whose whole purpose is another game type's objective.
OBJECTIVE_CLASSES = frozenset(("mp_retrieval_objective", "mp_gmi_ctf_flag"))
# targetnames the stock gametype scripts address objectives by.
OBJECTIVE_TARGET_NAMES = frozenset(("retrieval_objective", "hqradio"))
# The multiplayer game types this product ships. maps/catalog.json advertises
# the intersection of these with the map's own .arena declaration, so the
# runtime never sees a mode it cannot run.
SHIPPED_GAME_TYPES = ("dm", "tdm")
SCENE_PROP_CLASSES = {
    "misc_model",
    "script_model",
    "misc_mg42",
}

# Runtime ids emitted by export_cod1_demo_viewmodels.py. Radiant stores an
# entity classname in each WEAPONFILE (for example mpweapon_fg42); resolving
# through the official file instead of slicing the classname also covers the
# historical spelling differences in sniper and grenade names.
WEAPON_FILE_RUNTIME_IDS = {
    "bar_mp": "bar",
    "bren_mp": "bren",
    "colt_mp": "colt45",
    "enfield_mp": "enfield",
    "fg42_mp": "fg42",
    "fraggrenade_mp": "fraggrenade",
    "kar98k_mp": "kar98k",
    "kar98k_sniper_mp": "kar98k_scoped",
    "luger_mp": "luger",
    "m1carbine_mp": "m1carbine",
    "m1garand_mp": "m1garand",
    "mk1britishfrag_mp": "mk1britishfrag",
    "mosin_nagant_mp": "mosin_nagant",
    "mosin_nagant_sniper_mp": "mosin_nagant_scoped",
    "mp40_mp": "mp40",
    "mp44_mp": "mp44",
    "panzerfaust_mp": "panzerfaust",
    "ppsh_mp": "ppsh",
    "rgd-33russianfrag_mp": "rgd33",
    "smokegrenade_mp": "smokegrenade",
    "springfield_mp": "springfield",
    "sten_mp": "sten",
    "stielhandgranate_mp": "stielhandgranate",
    "thompson_mp": "thompson",
}

# Backwards-compatible public name used by editor tooling.  The authoritative
# roster lives in cod1_shipping_maps and includes five base maps plus Arnhem
# and Cassino from United Offensive.
CURATED_MAPS = SHIPPING_MAP_SPECS


@dataclass(frozen=True)
class ArchiveEntry:
    archive: Path
    name: str
    size: int


@dataclass(frozen=True)
class BspVisibility:
    """Validated CoD1 v59 render-visibility data.

    CoD's PVS rows are indexed by *clusters*, not by the much smaller
    graphics-cell array. A leaf provides the exact cluster -> cell bridge,
    while nodes and planes locate the camera's leaf. Keeping these concepts
    separate is important: mp_pavlov, for example, has 666 clusters but only
    20 graphics cells.
    """

    planes: tuple[tuple[float, float, float, float], ...]
    nodes: tuple[tuple[int, int, int], ...]
    leaf_clusters: tuple[int, ...]
    leaf_cells: tuple[int, ...]
    cluster_cells: tuple[int, ...]
    cells: tuple[tuple[float, float, float, float, float, float], ...]
    cluster_count: int
    row_bytes: int
    pvs: bytes


@dataclass(frozen=True)
class RenderSectorPartition:
    grid_x: int
    grid_z: int
    always_visible: bool
    cluster_indices: tuple[int, ...]
    # Keyed by (material_index, lightmap_page) so each GLB primitive maps to
    # exactly one lightmap page; 0xFFFF page = unlit surface.
    faces: dict[tuple[int, int], list[np.ndarray]]


def arguments() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--game-root",
        type=Path,
        default=root / "Call of Duty",
        help="Call of Duty installation containing Main/ and uo/",
    )
    parser.add_argument(
        "--unity-root",
        type=Path,
        default=root / "FriendsOfDutyUnity",
    )
    parser.add_argument(
        "--pak-root",
        type=Path,
        default=None,
        help="Content-package root; switches output to the fodpak layout",
    )
    # Retired selection flags. The Friends of Duty rotation is exactly seven
    # maps and United Offensive is mandatory, so neither the roster nor the
    # tier is the caller's to choose. Still accepted so an in-flight pipeline
    # or a saved command line keeps working; the values are forced below.
    parser.add_argument("--maps", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--all-mp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--include-uo", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate outputs even when their source fingerprint matches",
    )
    args = parser.parse_args()
    # A narrowed roster produces a catalog the runtime refuses to mount, and
    # a base-only tier produces a package that fails validation. Neither is a
    # useful outcome, so both are simply overridden.
    args.maps = None
    args.all_mp = False
    args.include_uo = True
    return args


def parse_requested_maps(values: list[str] | None) -> set[str] | None:
    requested = normalize_requested_maps(values)
    return set(requested) if requested is not None else None


def official_archives(root: Path, game: str) -> list[Path]:
    tier = COD1_TIER if game == "cod1" else UO_TIER
    return policy_official_archives(root, tier)


_INDEX_CACHE: dict[tuple[Path, ...], dict[str, ArchiveEntry]] = {}


def layered_index(archives: list[Path]) -> dict[str, ArchiveEntry]:
    key = tuple(archives)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    result: dict[str, ArchiveEntry] = {}
    for archive in archives:
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                if info.is_dir():
                    continue
                normalized = info.filename.replace("\\", "/")
                result[normalized.casefold()] = ArchiveEntry(
                    archive,
                    normalized,
                    info.file_size,
                )
    _INDEX_CACHE[key] = result
    return result


def read_entry(entry: ArchiveEntry) -> bytes:
    with zipfile.ZipFile(entry.archive) as archive:
        return archive.read(entry.name)


def multiplayer_maps(
    archives: list[Path],
) -> dict[str, ArchiveEntry]:
    index = layered_index(archives)
    return {
        key: value
        for key, value in index.items()
        if key.startswith("maps/mp/") and key.endswith(".bsp")
    }


def lump(data: bytes, index: int) -> bytes:
    length, offset = struct.unpack_from("<ii", data, 8 + index * 8)
    if length < 0 or offset < 0 or offset + length > len(data):
        raise ValueError(
            f"invalid BSP lump {index}: offset={offset}, length={length}"
        )
    return data[offset : offset + length]


def validate_bsp(data: bytes, source: str) -> None:
    if len(data) < 8 or data[:4] != b"IBSP":
        raise ValueError(f"{source}: not an IBSP")
    version = struct.unpack_from("<i", data, 4)[0]
    if version != SUPPORTED_VERSION:
        raise ValueError(
            f"{source}: expected CoD IBSP v{SUPPORTED_VERSION}, got v{version}"
        )


def decode_lightmap_pages(data: bytes) -> list[np.ndarray]:
    """Split BSP lump 1 into fixed 512x512 RGB lightmap pages.

    CoD1/UO IBSP v59 bakes surface lighting into an array of 512x512x3 8-bit
    RGB pages in dLDR overbright form (the runtime multiplies by two on
    decode). A map without baked lightmaps has an empty lump; a lump whose
    length is not a whole number of pages is treated as absent so the map
    falls back to the M2 Classic light grid instead of aborting the batch.
    Returns one contiguous (512, 512, 3) uint8 array per page, in page order.
    """
    try:
        blob = lump(data, LIGHTMAP_LUMP_INDEX)
    except (ValueError, struct.error):
        return []
    if not blob:
        return []
    if len(blob) % LIGHTMAP_PAGE_BYTES:
        print(
            f"  warning: lightmap lump is {len(blob)} bytes, not a multiple "
            f"of {LIGHTMAP_PAGE_BYTES} (512x512 RGB); skipping map lightmaps"
        )
        return []
    page_count = len(blob) // LIGHTMAP_PAGE_BYTES
    pages = np.frombuffer(blob, np.uint8).reshape(
        page_count, LIGHTMAP_PAGE_DIM, LIGHTMAP_PAGE_DIM, 3
    )
    return [np.ascontiguousarray(pages[index]) for index in range(page_count)]


def parse_bsp_visibility(data: bytes) -> BspVisibility:
    """Decode and fail-closed validate CoD1/CoDUO IBSP v59 PVS data.

    The on-disk layout was verified against every map in the product roster:

    * lump 2: ``<4f`` planes (normal, distance);
    * lump 17: ``<6f7i`` graphics cells (the first six floats are bounds);
    * lump 20: ``<3i6i`` nodes (plane, two children, integer bounds);
    * lump 21: nine ints per leaf (cluster at 0, cell at byte 24);
    * lump 28: ``<clusterCount,rowBytes>`` followed by uncompressed,
      least-significant-bit-first PVS rows.

    The leaf-surface list is bounds-checked as part of the contract, but is
    deliberately not used for render ownership. CoDUO encodes some of those
    references outside the draw-soup index range, so triangle ownership is
    instead classified by exact BSP point traversal below.
    """
    validate_bsp(data, "map visibility")

    plane_data = lump(data, PLANE_LUMP_INDEX)
    cell_data = lump(data, CELL_LUMP_INDEX)
    node_data = lump(data, NODE_LUMP_INDEX)
    leaf_data = lump(data, LEAF_LUMP_INDEX)
    leaf_surface_data = lump(data, LEAF_SURFACE_LUMP_INDEX)
    visibility_data = lump(data, VISIBILITY_LUMP_INDEX)
    for label, payload, stride in (
        ("plane", plane_data, PLANE_LUMP_STRIDE),
        ("cell", cell_data, CELL_LUMP_STRIDE),
        ("node", node_data, NODE_LUMP_STRIDE),
        ("leaf", leaf_data, LEAF_LUMP_STRIDE),
        (
            "leaf-surface",
            leaf_surface_data,
            LEAF_SURFACE_LUMP_STRIDE,
        ),
    ):
        if len(payload) == 0 or len(payload) % stride:
            raise ValueError(
                f"{label} lump has invalid {stride}-byte stride"
            )
    if len(visibility_data) < 8:
        raise ValueError("visibility lump has no cluster header")

    planes = tuple(
        struct.unpack_from("<4f", plane_data, offset)
        for offset in range(0, len(plane_data), PLANE_LUMP_STRIDE)
    )
    if not all(math.isfinite(value) for plane in planes for value in plane):
        raise ValueError("plane lump contains a non-finite coefficient")

    cells = tuple(
        struct.unpack_from("<6f", cell_data, offset)
        for offset in range(0, len(cell_data), CELL_LUMP_STRIDE)
    )
    for index, bounds in enumerate(cells):
        if (
            not all(math.isfinite(value) for value in bounds)
            or any(bounds[axis] > bounds[axis + 3] for axis in range(3))
        ):
            raise ValueError(f"cell {index} has invalid bounds")

    raw_nodes = tuple(
        struct.unpack_from("<3i6i", node_data, offset)
        for offset in range(0, len(node_data), NODE_LUMP_STRIDE)
    )
    nodes = tuple(record[:3] for record in raw_nodes)
    leaf_records = tuple(
        struct.unpack_from("<9i", leaf_data, offset)
        for offset in range(0, len(leaf_data), LEAF_LUMP_STRIDE)
    )
    leaf_clusters = tuple(record[0] for record in leaf_records)
    leaf_cells = tuple(record[6] for record in leaf_records)
    leaf_surface_count = (
        len(leaf_surface_data) // LEAF_SURFACE_LUMP_STRIDE
    )
    for index, record in enumerate(leaf_records):
        first_surface, surface_count = record[2], record[3]
        if (
            first_surface < 0
            or surface_count < 0
            or first_surface + surface_count > leaf_surface_count
        ):
            raise ValueError(
                f"leaf {index} has an invalid leaf-surface range"
            )

    cluster_count, row_bytes = struct.unpack_from(
        "<ii",
        visibility_data,
    )
    minimum_row_bytes = (cluster_count + 7) // 8
    if (
        cluster_count <= 0
        or row_bytes < minimum_row_bytes
        or row_bytes % 4
    ):
        raise ValueError(
            "visibility header has an invalid cluster count or row width"
        )
    pvs = visibility_data[8:]
    if len(pvs) != cluster_count * row_bytes:
        raise ValueError(
            "visibility rows do not match clusterCount * rowBytes"
        )
    for cluster in range(cluster_count):
        if not (
            pvs[cluster * row_bytes + cluster // 8]
            & (1 << (cluster & 7))
        ):
            raise ValueError(
                f"visibility cluster {cluster} cannot see itself"
            )

    cluster_cells: list[int | None] = [None] * cluster_count
    for leaf_index, (cluster, cell) in enumerate(
        zip(leaf_clusters, leaf_cells)
    ):
        if cluster < -1 or cluster >= cluster_count:
            raise ValueError(
                f"leaf {leaf_index} references cluster {cluster}"
            )
        if cell < -1 or cell >= len(cells):
            raise ValueError(f"leaf {leaf_index} references cell {cell}")
        if cluster < 0:
            if cell >= 0:
                raise ValueError(
                    f"solid leaf {leaf_index} unexpectedly maps to cell {cell}"
                )
            continue
        if cell < 0:
            raise ValueError(
                f"visible leaf {leaf_index} has no graphics cell"
            )
        previous = cluster_cells[cluster]
        if previous is not None and previous != cell:
            raise ValueError(
                f"cluster {cluster} spans graphics cells "
                f"{previous} and {cell}"
            )
        cluster_cells[cluster] = cell
    missing_clusters = [
        index
        for index, cell in enumerate(cluster_cells)
        if cell is None
    ]
    if missing_clusters:
        raise ValueError(
            "visibility clusters have no leaf/cell mapping: "
            + ", ".join(map(str, missing_clusters[:8]))
        )

    for node_index, (plane, front, back) in enumerate(nodes):
        if plane < 0 or plane >= len(planes):
            raise ValueError(
                f"node {node_index} references plane {plane}"
            )
        for child in (front, back):
            if child >= len(nodes):
                raise ValueError(
                    f"node {node_index} references node {child}"
                )
            if child < 0 and -1 - child >= len(leaf_records):
                raise ValueError(
                    f"node {node_index} references leaf {-1 - child}"
                )

    # Validate the world root specifically. Additional nodes may be roots for
    # inline brush models and need not be reachable from node zero.
    state: dict[int, int] = {}
    stack: list[tuple[int, bool]] = [(0, False)]
    reached_leaf = False
    while stack:
        node_index, leaving = stack.pop()
        if node_index < 0:
            reached_leaf = True
            continue
        if leaving:
            state[node_index] = 2
            continue
        status = state.get(node_index, 0)
        if status == 1:
            raise ValueError(
                f"world BSP tree contains a cycle at node {node_index}"
            )
        if status == 2:
            continue
        state[node_index] = 1
        stack.append((node_index, True))
        _plane, front, back = nodes[node_index]
        stack.append((back, False))
        stack.append((front, False))
    if not reached_leaf:
        raise ValueError("world BSP root reaches no leaves")

    return BspVisibility(
        planes=planes,
        nodes=nodes,
        leaf_clusters=leaf_clusters,
        leaf_cells=leaf_cells,
        cluster_cells=tuple(
            int(cell)
            for cell in cluster_cells
            if cell is not None
        ),
        cells=cells,
        cluster_count=cluster_count,
        row_bytes=row_bytes,
        pvs=pvs,
    )


def locate_bsp_leaf(
    visibility: BspVisibility,
    point: np.ndarray | tuple[float, float, float],
) -> int:
    """Return the exact v59 leaf containing one point in CoD coordinates."""
    node_index = 0
    visited: set[int] = set()
    while node_index >= 0:
        if node_index in visited:
            raise ValueError(f"BSP traversal cycled at node {node_index}")
        visited.add(node_index)
        plane_index, front, back = visibility.nodes[node_index]
        nx, ny, nz, distance = visibility.planes[plane_index]
        side = (
            nx * float(point[0])
            + ny * float(point[1])
            + nz * float(point[2])
            - distance
        )
        node_index = front if side >= 0.0 else back
    leaf_index = -1 - node_index
    if leaf_index < 0 or leaf_index >= len(visibility.leaf_clusters):
        raise ValueError(f"BSP traversal produced leaf {leaf_index}")
    return leaf_index


def triangle_visibility_clusters(
    visibility: BspVisibility,
    positions: np.ndarray,
    triangle: np.ndarray,
) -> tuple[int, ...] | None:
    """Conservatively classify one render triangle.

    The complete triangle polygon is clipped through the BSP, rather than
    merely sampling its vertices or centroid. This matters because one
    graphics cell can be a non-convex union of leaves. Near-coplanar polygons
    visit both children, and a complexity guard fails open. Cluster ownership
    is the exact union of every intersected non-solid leaf.
    Only numerical/complexity failures and all-solid triangles return None.
    """
    epsilon = 1e-4
    fragment_limit = 256
    initial = positions[np.asarray(triangle, dtype=np.int64)].astype(
        np.float64,
        copy=True,
    )
    stack: list[tuple[int, np.ndarray]] = [(0, initial)]
    clusters: set[int] = set()
    fragments = 0
    while stack:
        node_index, polygon = stack.pop()
        fragments += 1
        if fragments > fragment_limit or len(polygon) < 3:
            return None
        if node_index < 0:
            leaf = -1 - node_index
            cluster = visibility.leaf_clusters[leaf]
            if cluster >= 0:
                clusters.add(cluster)
            continue

        plane_index, front_child, back_child = (
            visibility.nodes[node_index]
        )
        nx, ny, nz, distance = visibility.planes[plane_index]
        normal = np.asarray((nx, ny, nz), dtype=np.float64)
        signed = polygon @ normal - distance

        def clipped(keep_front: bool) -> np.ndarray:
            result: list[np.ndarray] = []
            for index, current in enumerate(polygon):
                following = polygon[(index + 1) % len(polygon)]
                current_distance = float(signed[index])
                following_distance = float(
                    signed[(index + 1) % len(polygon)]
                )
                current_inside = (
                    current_distance >= -epsilon
                    if keep_front
                    else current_distance <= epsilon
                )
                following_inside = (
                    following_distance >= -epsilon
                    if keep_front
                    else following_distance <= epsilon
                )
                if current_inside:
                    result.append(current)
                if current_inside != following_inside:
                    denominator = current_distance - following_distance
                    if abs(denominator) <= 1e-12:
                        # Numerically parallel at the tolerance boundary:
                        # ownership is uncertain, so trigger fail-open.
                        return np.empty((0, 3), dtype=np.float64)
                    amount = current_distance / denominator
                    result.append(
                        current + amount * (following - current)
                    )
            if len(result) < 3:
                return np.empty((0, 3), dtype=np.float64)
            return np.asarray(result, dtype=np.float64)

        front = clipped(True)
        back = clipped(False)
        # A polygon that should straddle but failed to produce one side is a
        # numerical ambiguity, never permission to hide it.
        strictly_front = bool(np.all(signed > epsilon))
        strictly_back = bool(np.all(signed < -epsilon))
        if strictly_front:
            stack.append((front_child, polygon))
        elif strictly_back:
            stack.append((back_child, polygon))
        else:
            if len(front) < 3 or len(back) < 3:
                return None
            stack.append((front_child, front))
            stack.append((back_child, back))
    return tuple(sorted(clusters)) if clusters else None


def triangle_visibility_cell(
    visibility: BspVisibility,
    positions: np.ndarray,
    triangle: np.ndarray,
) -> int | None:
    """Compatibility helper: return one cell only for a single-cell face."""
    clusters = triangle_visibility_clusters(
        visibility,
        positions,
        triangle,
    )
    if not clusters:
        return None
    cells = {visibility.cluster_cells[cluster] for cluster in clusters}
    return next(iter(cells)) if len(cells) == 1 else None


def parse_entities(data: bytes) -> list[dict[str, str]]:
    text = lump(data, ENTITY_LUMP_INDEX).decode(
        "latin1", "replace"
    ).rstrip("\0")
    entities = [
        dict(re.findall(r'"([^"]*)"\s+"([^"]*)"', body))
        for body in re.findall(r"\{([^}]*)\}", text, re.DOTALL)
    ]
    if not entities or entities[0].get("classname") != "worldspawn":
        raise ValueError("BSP entity lump has no worldspawn")
    return entities


def parse_authored_sun(
    data: bytes,
    worldspawn: dict[str, str],
) -> dict[str, object]:
    """Decode CoD's compiled directional light into the Unity sun contract.

    Lump 30 contains 72-byte compiled light records. Type 1 is the single
    global directional light used when baking the map. Its vector points from
    the world towards the sun in CoD coordinates. Map geometry is converted
    to Unity as ``(-x, z, -y)``, so the light vector must use the same basis.

    The time-of-day course is anchored at synthetic noon. CoD stores no clock,
    only a direction; at noon PavlovSunCourse places the sun opposite its
    configurable heading, hence ``heading = bearing + 180``.
    """
    light_data = lump(data, LIGHT_LUMP_INDEX)
    if len(light_data) % LIGHT_LUMP_STRIDE:
        raise ValueError(
            "light lump has invalid "
            f"{LIGHT_LUMP_STRIDE}-byte stride"
        )

    directional_offsets = [
        offset
        for offset in range(0, len(light_data), LIGHT_LUMP_STRIDE)
        if struct.unpack_from("<i", light_data, offset)[0]
        == DIRECTIONAL_LIGHT_TYPE
    ]
    if len(directional_offsets) != 1:
        raise ValueError(
            "light lump must contain exactly one type-1 directional light; "
            f"found {len(directional_offsets)}"
        )

    offset = directional_offsets[0]
    compiled_color = struct.unpack_from("<3f", light_data, offset + 4)
    direction_cod = struct.unpack_from("<3f", light_data, offset + 28)
    if not all(
        math.isfinite(component)
        for component in (*compiled_color, *direction_cod)
    ):
        raise ValueError(
            "compiled directional light contains a non-finite value"
        )

    direction_length = math.sqrt(
        sum(component * component for component in direction_cod)
    )
    if not math.isclose(
        direction_length,
        1.0,
        rel_tol=0.0,
        abs_tol=1e-4,
    ):
        raise ValueError(
            "compiled directional-light vector is not normalized: "
            f"length={direction_length:.9g}"
        )

    raw_angles = worldspawn.get("sundirection")
    if raw_angles is None:
        raise ValueError("worldspawn has no sundirection")
    fields = raw_angles.split()
    if len(fields) != 3:
        raise ValueError(
            f"worldspawn sundirection is malformed: {raw_angles!r}"
        )
    try:
        pitch, yaw, _roll = (float(field) for field in fields)
    except ValueError as error:
        raise ValueError(
            f"worldspawn sundirection is malformed: {raw_angles!r}"
        ) from error
    if not all(math.isfinite(angle) for angle in (pitch, yaw, _roll)):
        raise ValueError(
            f"worldspawn sundirection is non-finite: {raw_angles!r}"
        )

    pitch_radians = math.radians(pitch)
    yaw_radians = math.radians(yaw)
    horizontal = math.cos(pitch_radians)
    worldspawn_direction = (
        horizontal * math.cos(yaw_radians),
        horizontal * math.sin(yaw_radians),
        -math.sin(pitch_radians),
    )
    agreement = sum(
        compiled * authored
        for compiled, authored in zip(
            direction_cod,
            worldspawn_direction,
        )
    )
    agreement = max(-1.0, min(1.0, agreement / direction_length))
    angular_error = math.degrees(math.acos(agreement))
    if angular_error > 1e-3:
        raise ValueError(
            "compiled directional light disagrees with worldspawn "
            f"sundirection by {angular_error:.6g} degrees"
        )

    direction_unity = (
        -direction_cod[0],
        direction_cod[2],
        -direction_cod[1],
    )
    normalized_direction_unity = tuple(
        component / direction_length
        for component in direction_unity
    )
    bearing = math.degrees(
        math.atan2(
            normalized_direction_unity[0],
            normalized_direction_unity[2],
        )
    ) % 360.0
    elevation = math.degrees(
        math.asin(
            max(-1.0, min(1.0, normalized_direction_unity[1]))
        )
    )
    return {
        "source": "bsp-lump-30",
        "directionUnity": list(direction_unity),
        "compiledColor": list(compiled_color),
        "startHour": 12.0,
        "heading": (bearing + 180.0) % 360.0,
        "arcPeak": elevation,
    }


def parse_brush_models(data: bytes) -> list[dict[str, object]]:
    """Decode CoD1 lump 27.

    A model number in an entity (``model "*17"``) indexes this array. The
    first six floats are the model's authored world-space AABB; the remaining
    six ints index triangle soups, leaf faces and collision brushes. Only the
    bounds are needed to rebuild trigger volumes in Unity, but retaining the
    indices makes malformed data detectable instead of silently inventing a
    collider.
    """
    model_data = lump(data, MODEL_LUMP_INDEX)
    if len(model_data) % MODEL_LUMP_STRIDE:
        raise ValueError(
            "model lump stride is not "
            f"{MODEL_LUMP_STRIDE} bytes"
        )
    models = []
    for offset in range(0, len(model_data), MODEL_LUMP_STRIDE):
        values = struct.unpack_from("<6f6i", model_data, offset)
        models.append(
            {
                "minimum": list(values[0:3]),
                "maximum": list(values[3:6]),
                "firstTriangleSoup": values[6],
                "triangleSoupCount": values[7],
                "firstLeafFace": values[8],
                "leafFaceCount": values[9],
                "firstBrush": values[10],
                "brushCount": values[11],
            }
        )
    return models


def brush_model_bounds(
    entity: dict[str, str],
    models: list[dict[str, object]],
) -> tuple[list[float], list[float]] | None:
    model = entity.get("model", "")
    if not model.startswith("*") or not model[1:].isdigit():
        return None
    model_index = int(model[1:])
    if model_index <= 0 or model_index >= len(models):
        raise ValueError(
            f"entity references invalid brush model {model!r}; "
            f"model count={len(models)}"
        )
    record = models[model_index]
    minimum = record["minimum"]
    maximum = record["maximum"]
    assert isinstance(minimum, list) and isinstance(maximum, list)
    center = [
        (minimum[axis] + maximum[axis]) * 0.5
        for axis in range(3)
    ]
    size = [
        max(0.0, maximum[axis] - minimum[axis])
        for axis in range(3)
    ]
    return center, size


def entity_brush_volume_planes(
    entity: dict[str, str],
    models: list[dict[str, object]],
    brushes: list[tuple[int, int]],
    brush_first_side: list[int],
    sides: list[tuple[int, int]],
    planes: "np.ndarray",
) -> tuple[list[float], list[int]]:
    """Flat per-brush half-space rows for a ``*N`` brush-model entity.

    brush_model_bounds above ships only the model lump's union AABB, and
    that lie is lethal for minefields: 19 of mp_pavlov's 26 minefield
    trigger brushes are non-axial — 1-4 diagonal cutting planes slice each
    volume flush along the warning-sign line — and the AABB protrudes up to
    15 m past the authored boundary into retail-safe ground (1,484 m² of
    falsely lethal ground across the map).  These rows are the authored
    hull itself: every brush's half-spaces as [nx, ny, nz, distance] in
    source coordinates with inside = n·x <= d, flattened because
    JsonUtility reads no nested arrays; brushVolumePlaneCounts says how
    many rows belong to each brush.
    """
    model = entity.get("model", "")
    if not model.startswith("*") or not model[1:].isdigit():
        return [], []
    model_index = int(model[1:])
    if model_index <= 0 or model_index >= len(models):
        return [], []
    record = models[model_index]
    first_brush = int(record["firstBrush"])
    brush_count = int(record["brushCount"])
    plane_rows: list[float] = []
    plane_counts: list[int] = []
    for brush_index in range(first_brush, first_brush + brush_count):
        if not 0 <= brush_index < len(brushes):
            continue
        side_count = brushes[brush_index][0]
        start = brush_first_side[brush_index]
        brush_sides = sides[start : start + side_count]
        if side_count < 6 or len(brush_sides) != side_count:
            continue
        half_spaces = brush_half_spaces(brush_sides, planes)
        for normal, distance in half_spaces:
            plane_rows.extend(
                (
                    float(normal[0]),
                    float(normal[1]),
                    float(normal[2]),
                    float(distance),
                )
            )
        plane_counts.append(len(half_spaces))
    return plane_rows, plane_counts


def _positive_script_exploder_id(
    entity: dict[str, str],
    source_index: int,
) -> int:
    raw = entity.get("script_exploder", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(
            f"entity {source_index} has non-integer script_exploder "
            f"{raw!r}"
        ) from error
    if value <= 0:
        raise ValueError(
            f"entity {source_index} has non-positive script_exploder "
            f"{raw!r}"
        )
    return value


def exploder_effect_record(
    effect: ExploderEffectClosure,
) -> dict[str, object]:
    return {
        "fxId": effect.fx_id,
        "sourceEfx": effect.source_efx,
        "efxFiles": [
            exploder_efx_output_path(path)
            for path in effect.efx_files
        ],
        "texturePngs": [
            exploder_texture_output_path(path)
            for path in effect.image_files
        ],
        "shaderTextures": [
            {
                "shader": shader,
                "texturePngs": [
                    exploder_texture_output_path(path)
                    for path in image_paths
                ],
            }
            for shader, image_paths in effect.shader_images
        ],
        "aliasNames": list(effect.sound_aliases),
        "modelNames": [
            model.casefold()
            for model in effect.model_names
        ],
    }


def exploder_brush_assets(
    bsp: bytes,
    entities: list[dict[str, str]],
    models: list[dict[str, object]],
    game: str,
    map_id: str,
) -> dict[int, dict[str, object]]:
    """Pak-local GLB contracts for each authored exploder brush model.

    Lump-27 ``firstTriangleSoup/triangleSoupCount`` is the exact ownership
    link between a ``script_brushmodel`` and render soups in lump 6. Keeping
    those ranges separate prevents a destructible brush from also remaining
    permanently baked into world.glb.
    """
    material_data = lump(bsp, MATERIAL_LUMP_INDEX)
    soup_data = lump(bsp, 6)
    if len(material_data) % MATERIAL_LUMP_STRIDE:
        raise ValueError("material lump stride is not 72 bytes")
    if len(soup_data) % 16:
        raise ValueError("draw-soup lump stride is not 16 bytes")
    material_names = [
        material_data[offset : offset + 64]
        .split(b"\0", 1)[0]
        .decode("latin1", "replace")
        for offset in range(
            0,
            len(material_data),
            MATERIAL_LUMP_STRIDE,
        )
    ]
    groups = material_groups(material_names)
    soup_materials = [
        struct.unpack_from("<H", soup_data, offset)[0]
        for offset in range(0, len(soup_data), 16)
    ]
    result: dict[int, dict[str, object]] = {}
    claimed: dict[int, int] = {}
    for source_index, entity in enumerate(entities):
        if (
            entity.get("classname", "").casefold()
            != "script_brushmodel"
            or not entity.get("script_exploder")
        ):
            continue
        _positive_script_exploder_id(entity, source_index)
        model = entity.get("model", "")
        if not model.startswith("*") or not model[1:].isdigit():
            raise ValueError(
                f"script_exploder brush entity {source_index} has invalid "
                f"model {model!r}"
            )
        model_index = int(model[1:])
        if model_index <= 0 or model_index >= len(models):
            raise ValueError(
                f"script_exploder brush entity {source_index} references "
                f"invalid model {model!r}"
            )
        source_model = models[model_index]
        first = int(source_model["firstTriangleSoup"])
        count = int(source_model["triangleSoupCount"])
        if first < 0 or count < 0 or first + count > len(soup_materials):
            raise ValueError(
                f"script_exploder brush entity {source_index} has invalid "
                f"draw-soup range {first}+{count}"
            )
        soup_indices = list(range(first, first + count))
        for soup_index in soup_indices:
            previous = claimed.get(soup_index)
            if previous is not None:
                raise ValueError(
                    "script_exploder brush draw soup "
                    f"{soup_index} is shared by entities "
                    f"{previous} and {source_index}"
                )
            claimed[soup_index] = source_index
        bounds = brush_model_bounds(entity, models)
        if bounds is None:
            raise ValueError(
                f"script_exploder brush entity {source_index} has no bounds"
            )
        brush_material_groups = sorted(
            {
                groups[material]
                for material in (
                    soup_materials[soup_index]
                    for soup_index in soup_indices
                )
                if 0 <= material < len(material_names)
            },
            key=str.casefold,
        )
        has_render_geometry = bool(soup_indices)
        result[source_index] = {
            "glb": (
                f"maps/{game}/{map_id}/exploders/"
                f"entity_{source_index}.glb"
                if has_render_geometry
                else ""
            ),
            "materials": brush_material_groups,
            "_soupIndices": soup_indices,
            "_center": bounds[0],
        }
    return result


def _uint_bits_as_float(value: int) -> float:
    """Decode CoD1's axial brush-plane representation.

    The first six BrushSide ``plane`` fields store a float distance in the
    uint32 field's bits; later, non-axial sides store a real plane-lump
    index.  Every official MP ladder has the six axial bounds, including
    the two UO ladders that add diagonal clipping planes afterwards.
    """
    return struct.unpack("<f", struct.pack("<I", value))[0]


def ladder_brush_volumes(data: bytes) -> list[dict[str, object]]:
    """Return official ``textures/common/ladder*`` collision brushes.

    Ladder shaders are collision-only and therefore never enter the draw
    soup used to build ``world.glb``.  Keeping their exact axial brush bounds
    in the material manifest lets Unity rebuild the gameplay surface without
    importing any SinglePlayer data or inventing volumes from visible art.
    Bounds remain in authored CoD coordinates; RuntimeMapBuilder performs the
    same axis/unit conversion as spawns and gameplay brush entities.
    """
    material_data = lump(data, MATERIAL_LUMP_INDEX)
    side_data = lump(data, BRUSH_SIDE_LUMP_INDEX)
    brush_data = lump(data, BRUSH_LUMP_INDEX)
    if len(material_data) % MATERIAL_LUMP_STRIDE:
        raise ValueError("material lump stride is not 72 bytes")
    if len(side_data) % BRUSH_SIDE_LUMP_STRIDE:
        raise ValueError("brush-side lump stride is not 8 bytes")
    if len(brush_data) % BRUSH_LUMP_STRIDE:
        raise ValueError("brush lump stride is not 4 bytes")

    materials = [
        material_data[offset : offset + 64]
        .split(b"\0", 1)[0]
        .decode("latin1", "replace")
        for offset in range(
            0,
            len(material_data),
            MATERIAL_LUMP_STRIDE,
        )
    ]
    sides = [
        struct.unpack_from("<II", side_data, offset)
        for offset in range(
            0,
            len(side_data),
            BRUSH_SIDE_LUMP_STRIDE,
        )
    ]
    brushes = [
        struct.unpack_from("<HH", brush_data, offset)
        for offset in range(
            0,
            len(brush_data),
            BRUSH_LUMP_STRIDE,
        )
    ]

    result: list[dict[str, object]] = []
    first_side = 0
    for brush_index, (side_count, brush_material) in enumerate(brushes):
        brush_sides = sides[first_side : first_side + side_count]
        first_side += side_count
        if len(brush_sides) != side_count:
            raise ValueError(
                f"brush {brush_index} references missing brush sides"
            )
        material_indices = [brush_material] + [
            material for _plane, material in brush_sides
        ]
        ladder_material = next(
            (
                materials[index]
                for index in material_indices
                if 0 <= index < len(materials)
                and normalized_material_path(materials[index]).startswith(
                    "common/ladder"
                )
            ),
            None,
        )
        if ladder_material is None:
            continue
        if side_count < 6:
            raise ValueError(
                f"ladder brush {brush_index} has only {side_count} sides"
            )

        axial = [
            _uint_bits_as_float(plane)
            for plane, _material in brush_sides[:6]
        ]
        minimum = [
            min(axial[0], axial[1]),
            min(axial[2], axial[3]),
            min(axial[4], axial[5]),
        ]
        maximum = [
            max(axial[0], axial[1]),
            max(axial[2], axial[3]),
            max(axial[4], axial[5]),
        ]
        size = [
            maximum[axis] - minimum[axis]
            for axis in range(3)
        ]
        if any(not 0.0 < component < 100000.0 for component in size):
            raise ValueError(
                f"ladder brush {brush_index} has invalid bounds "
                f"{minimum}..{maximum}"
            )
        result.append(
            {
                "sourceBrushIndex": brush_index,
                "sourceMaterial": ladder_material,
                "center": [
                    (minimum[axis] + maximum[axis]) * 0.5
                    for axis in range(3)
                ],
                "size": size,
            }
        )
    if first_side != len(sides):
        raise ValueError(
            "brush-side ownership does not cover the brush-side lump"
        )
    return result


# --------------------------------------------------------------------------
# Collision-only clip brushes
#
# The BSP compiler keeps tool-textured brushes out of the draw soups, so
# world.glb — and therefore the MeshColliders the runtime stamps onto it —
# carries only collision that happens to be visible. Everything the level
# authors added as invisible movement collision (clip over a railing, a
# smoothed stair run, a blocked scenery gap, the ceiling that keeps players
# off a roof) lived in `textures/common/clip*` and was silently dropped.
# ladder_brush_volumes above already reads the same lump; this rebuilds the
# rest as real geometry instead of axial bounds, because most clip brushes
# are not axis-aligned (mp_pavlov: 272 of 358) and their AABB would seal off
# space the player is supposed to reach.
# --------------------------------------------------------------------------

# Q3-derived content flag. Every `common/clip*`, and the PLAYERCLIP-flagged
# `common/nodraw` variants, set it; `caulk` (SOLID, the hidden back faces of
# ordinary world brushes, already covered by the visible mesh), `trigger`,
# `hint` and `clipfoliage` do not and stay dropped.
CONTENTS_PLAYERCLIP = 0x10000
# Half the axial extent of a base winding, in CoD units. Two orders of
# magnitude past the largest shipping map bound (mp_pavlov spans ~17k units),
# so a face's winding always starts larger than the brush that chops it.
CLIP_WINDING_EXTENT = float(1 << 17)
CLIP_WINDING_EPSILON = 1e-3


def brush_half_spaces(
    brush_sides: list[tuple[int, int]],
    planes: np.ndarray,
) -> list[tuple[np.ndarray, float]]:
    """Half-spaces (normal, distance) bounding one brush; inside is n·x <= d.

    The first six sides carry the axial bounds in the bit-packed form
    _uint_bits_as_float decodes (see ladder_brush_volumes); any further side
    indexes the plane lump and is what makes a brush non-axial.
    """
    axial = [
        _uint_bits_as_float(plane)
        for plane, _material in brush_sides[:6]
    ]
    minimum = [
        min(axial[0], axial[1]),
        min(axial[2], axial[3]),
        min(axial[4], axial[5]),
    ]
    maximum = [
        max(axial[0], axial[1]),
        max(axial[2], axial[3]),
        max(axial[4], axial[5]),
    ]
    result: list[tuple[np.ndarray, float]] = []
    for axis in range(3):
        low = np.zeros(3)
        low[axis] = -1.0
        result.append((low, -minimum[axis]))
        high = np.zeros(3)
        high[axis] = 1.0
        result.append((high, maximum[axis]))
    for plane_index, _material in brush_sides[6:]:
        if 0 <= plane_index < len(planes):
            plane = planes[plane_index]
            result.append(
                (
                    np.asarray(plane[:3], dtype=np.float64),
                    float(plane[3]),
                )
            )
    return result


def base_winding(
    normal: np.ndarray,
    distance: float,
) -> list[np.ndarray] | None:
    """A quad on the plane, large enough to enclose any brush face.

    Wound counter-clockwise seen from outside (its cross product is +normal),
    which is the same handedness parse_world_lumps hands the GLB writer.
    """
    axis = int(np.argmax(np.abs(normal)))
    up = np.array([0.0, 0.0, 1.0]) if axis != 2 else np.array([1.0, 0.0, 0.0])
    up = up - normal * float(np.dot(up, normal))
    length = float(np.linalg.norm(up))
    if length < 1e-9:
        return None
    up /= length
    right = np.cross(up, normal)
    origin = normal * distance
    extent = CLIP_WINDING_EXTENT
    return [
        origin + (up + right) * extent,
        origin + (up - right) * extent,
        origin + (-up - right) * extent,
        origin + (-up + right) * extent,
    ]


def chop_winding(
    winding: list[np.ndarray],
    normal: np.ndarray,
    distance: float,
) -> list[np.ndarray] | None:
    """Keep the n·x <= d side of a winding; None once nothing survives."""
    distances = [float(np.dot(normal, point) - distance) for point in winding]
    epsilon = CLIP_WINDING_EPSILON
    if all(value < -epsilon for value in distances):
        return winding
    if all(value > epsilon for value in distances):
        return None
    result: list[np.ndarray] = []
    for index, point in enumerate(winding):
        following = winding[(index + 1) % len(winding)]
        here = distances[index]
        there = distances[(index + 1) % len(winding)]
        if here <= epsilon:
            result.append(point)
        if (here > epsilon) != (there > epsilon):
            result.append(point + (here / (here - there)) * (following - point))
    return result if len(result) >= 3 else None


def clip_brush_primitives(data: bytes) -> tuple[list[dict[str, object]], int]:
    """GLB primitives for every player-blocking collision-only brush.

    One primitive per clip material so the runtime (and anyone reading the
    file) can tell a hand-placed `common/clip` from the bulk `clip_nosight`.
    Ladders are excluded — ladder_brush_volumes already ships those as
    gameplay volumes and a solid collider over one would break climbing.
    """
    material_data = lump(data, MATERIAL_LUMP_INDEX)
    side_data = lump(data, BRUSH_SIDE_LUMP_INDEX)
    brush_data = lump(data, BRUSH_LUMP_INDEX)
    plane_data = lump(data, PLANE_LUMP_INDEX)
    if len(material_data) % MATERIAL_LUMP_STRIDE:
        raise ValueError("material lump stride is not 72 bytes")
    if len(plane_data) % PLANE_LUMP_STRIDE:
        raise ValueError("plane lump stride is not 16 bytes")

    materials: list[tuple[str, int]] = []
    for offset in range(0, len(material_data), MATERIAL_LUMP_STRIDE):
        name = (
            material_data[offset : offset + 64]
            .split(b"\0", 1)[0]
            .decode("latin1", "replace")
        )
        _surface_flags, content_flags = struct.unpack_from(
            "<iI",
            material_data,
            offset + 64,
        )
        materials.append((name, content_flags))
    planes = np.frombuffer(plane_data, dtype="<f4").reshape(-1, 4)
    sides = [
        struct.unpack_from("<II", side_data, offset)
        for offset in range(0, len(side_data), BRUSH_SIDE_LUMP_STRIDE)
    ]
    brushes = [
        struct.unpack_from("<HH", brush_data, offset)
        for offset in range(0, len(brush_data), BRUSH_LUMP_STRIDE)
    ]

    # One entry PER BRUSH, not per material. Merging brushes into shared
    # material-group triangle soups let Unity's capsule controller step up
    # onto a car's clip hull and wedge INSIDE it — a triangle soup has no
    # interior, so a capsule that crosses a face is simply trapped behind
    # it. Per-brush primitives let the runtime cook each brush as a CONVEX
    # collider: a solid volume nothing can be "inside", whose authored
    # bevel sides are exactly what lets a capsule slide across abutting
    # brushes the way the original trace code did.
    brush_geometries: list[tuple[str, list[np.ndarray]]] = []
    brush_count = 0
    first_side = 0
    for brush_index, (side_count, brush_material) in enumerate(brushes):
        brush_sides = sides[first_side : first_side + side_count]
        first_side += side_count
        if len(brush_sides) != side_count:
            raise ValueError(
                f"brush {brush_index} references missing brush sides"
            )
        if side_count < 6:
            continue
        clip_material = next(
            (
                materials[index]
                for index in [brush_material]
                + [material for _plane, material in brush_sides]
                if 0 <= index < len(materials)
                and normalized_material_path(materials[index][0]).startswith(
                    "common/"
                )
            ),
            None,
        )
        if clip_material is None:
            continue
        name, content_flags = clip_material
        normalized = normalized_material_path(name)
        if not content_flags & CONTENTS_PLAYERCLIP:
            continue
        if normalized.startswith("common/ladder"):
            continue

        half_spaces = brush_half_spaces(brush_sides, planes)
        faces: list[np.ndarray] = []
        for index, (normal, distance) in enumerate(half_spaces):
            winding = base_winding(normal, distance)
            if winding is None:
                continue
            for other, (cut_normal, cut_distance) in enumerate(half_spaces):
                if other == index:
                    continue
                winding = chop_winding(winding, cut_normal, cut_distance)
                if winding is None:
                    break
            if winding is not None and len(winding) >= 3:
                faces.append(np.asarray(winding, dtype=np.float64))
        # Fewer than four surviving faces is not a closed solid; the brush
        # would contribute stray sheets rather than a volume.
        if len(faces) < 4:
            continue
        brush_count += 1
        brush_geometries.append((safe_name(normalized), faces))

    primitives: list[dict[str, object]] = []
    triangle_total = 0
    for ordinal, (group, brush_faces) in enumerate(brush_geometries):
        positions: list[np.ndarray] = []
        indices: list[int] = []
        for winding in brush_faces:
            base = len(positions)
            positions.extend(winding)
            # Fan the convex face, then swap b/c to the writer's (a, c, b)
            # order so the runtime's winding reversal lands outward-facing,
            # exactly as world.glb's draw soups do.
            for corner in range(1, len(winding) - 1):
                indices.extend(
                    (base, base + corner + 1, base + corner)
                )
        source = np.asarray(positions, dtype=np.float64)
        # BSP (x, y, z) Z-up inches -> GLB (x, z, -y) metres; see
        # fod_glb_writer for the full convention chain.
        glb_positions = (
            np.column_stack((source[:, 0], source[:, 2], -source[:, 1]))
            * COD_UNIT_TO_METRE
        ).astype(np.float32)
        # Face fans are emitted independently, so neighbouring faces of the
        # same brush repeat their shared corners. Welding them is what keeps
        # clip.glb a fraction of world.glb instead of rivalling it, and a
        # welded manifold is also what PhysX wants to cook.
        unique_positions, inverse = np.unique(
            glb_positions,
            axis=0,
            return_inverse=True,
        )
        clip_indices = inverse[np.asarray(indices, dtype=np.int64)]
        # Welding can collapse a sliver face onto a line; drop the triangles
        # that lost a corner rather than cook degenerate collision.
        triangles = clip_indices.reshape(-1, 3)
        keep = (
            (triangles[:, 0] != triangles[:, 1])
            & (triangles[:, 1] != triangles[:, 2])
            & (triangles[:, 2] != triangles[:, 0])
        )
        triangles = triangles[keep]
        if not len(triangles):
            continue
        triangle_total += len(triangles)
        primitives.append(
            {
                "material": f"{group}__b{ordinal}",
                "positions": unique_positions,
                "indices": triangles.reshape(-1).astype(np.uint32),
            }
        )
    return primitives, brush_count


def parse_backslash_values(data: bytes) -> dict[str, str]:
    fields = data.decode("latin1", "replace").split("\\")
    return dict(zip(fields[1::2], fields[2::2]))


def runtime_weapon_ids(
    asset_index: dict[str, ArchiveEntry],
) -> dict[str, str]:
    result: dict[str, str] = {}
    prefix = "weapons/mp/"
    for key, entry in asset_index.items():
        if not key.startswith(prefix):
            continue
        file_name = key[len(prefix):]
        runtime_id = WEAPON_FILE_RUNTIME_IDS.get(file_name.casefold())
        if runtime_id is None:
            continue
        values = parse_backslash_values(read_entry(entry))
        radiant_name = values.get("radiantName", "").strip().casefold()
        if radiant_name:
            result[radiant_name] = runtime_id
    return result


def parse_arena_game_types(
    asset_index: dict[str, ArchiveEntry],
) -> dict[str, list[str]]:
    """Return exact MP gametype tokens authored in official .arena files."""
    result: dict[str, list[str]] = {}
    for key, entry in asset_index.items():
        if not key.endswith(".arena"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        # Strip line comments first so a disabled record cannot leak into the
        # runtime catalog. (No official file has any, but mods do.)
        text = re.sub(r"//[^\r\n]*", "", text)
        for body in re.findall(r"\{([^}]*)\}", text, re.DOTALL):
            # Retail authors the KEY BARE and quotes only the value:
            #
            #     {
            #     <TAB>map     "mp_carentan"
            #     <TAB>longname"mp_carentan"
            #     <TAB>gametype"dm tdm sd re bel hq"
            #     }
            #
            # This used to demand Quake 3's fully-quoted `"key" "value"`
            # form, which matches ZERO records in any shipped .arena — the
            # bare key between two quoted values is exactly what \s+ cannot
            # cross. Every map therefore fell through to the entity-inferred
            # fallback below, which reads the roster's own spawn classes
            # rather than the file the game itself reads.
            values = dict(
                re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*"([^"]*)"', body)
            )
            map_name = values.get("map", "").strip().casefold()
            if not map_name:
                continue
            tokens = [
                token.casefold()
                for token in re.split(
                    r"[\s,]+",
                    values.get("gametype", "").strip(),
                )
                if token
            ]
            if tokens:
                result[map_name] = list(dict.fromkeys(tokens))
    return result


def supported_game_types(
    map_id: str,
    entities: list[dict[str, str]],
    asset_index: dict[str, ArchiveEntry],
) -> list[str]:
    """The map's own .arena declaration, narrowed to the shipped modes.

    Friends of Duty ships Deathmatch and Team Deathmatch only, so the catalog
    advertises the intersection rather than retail's full token list: every
    other token names a mode the runtime cannot run, and the map roster is
    fixed precisely so nothing has to discover that at match start. The
    SHIPPED_GAME_TYPES order is kept, not the .arena's, so every map reads
    the same way.
    """
    official = parse_arena_game_types(asset_index).get(map_id.casefold())
    if official:
        declared = set(official)
        return [token for token in SHIPPED_GAME_TYPES if token in declared]

    # Some user-installed patch combinations omit their arena file. Preserve
    # a deterministic, entity-backed fallback rather than losing all mode
    # metadata from the generated package.
    class_names = {
        entity.get("classname", "").casefold()
        for entity in entities
    }
    inferred = []
    if "mp_deathmatch_spawn" in class_names:
        inferred.append("dm")
    if "mp_teamdeathmatch_spawn" in class_names:
        inferred.append("tdm")
    return inferred


def is_objective_entity(entity: dict[str, str]) -> bool:
    """Whether an authored entity belongs to a game type this product cut.

    Search & Destroy bombzones and their look-at triggers, Retrieval
    objectives and their candidates, Headquarters radios, Capture the Flag
    flags/stands/triggers and Domination capture points are all identified
    the way maps\\mp\\gametypes\\_gameobjects identifies them: by class, by
    the targetname the mode's script getent()s, or by a token in the
    SPACE-SEPARATED script_gameobjectname allow list (an entity shared by two
    modes carries something like "dom retrieval", and either token is
    enough).
    """
    if entity.get("classname", "").casefold() in OBJECTIVE_CLASSES:
        return True
    if entity.get("targetname", "").strip().casefold() in (
        OBJECTIVE_TARGET_NAMES
    ):
        return True
    tokens = entity.get("script_gameobjectname", "").casefold().split()
    return any(token in OBJECTIVE_GAMEOBJECT_TOKENS for token in tokens)


def vector(
    value: str | None,
    default: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> list[float]:
    if not value:
        return list(default)
    fields = value.split()
    if len(fields) != 3:
        return list(default)
    try:
        return [float(field) for field in fields]
    except ValueError:
        return list(default)


def color(value: str | None) -> list[float]:
    result = vector(value, (1.0, 1.0, 1.0))
    if max(result) > 1.0:
        result = [component / 255.0 for component in result]
    return [max(0.0, min(1.0, component)) for component in result]


def number(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return float(value.split()[0])
    except (ValueError, IndexError):
        return default


def model_scale(entity: dict[str, str]) -> tuple[float, list[float]]:
    raw_vector = entity.get("modelscale_vec")
    raw_scalar = entity.get("modelscale")
    if raw_vector:
        scale_vector = vector(raw_vector, (1.0, 1.0, 1.0))
        return scale_vector[0], scale_vector
    if raw_scalar and len(raw_scalar.split()) == 3:
        scale_vector = vector(raw_scalar, (1.0, 1.0, 1.0))
        return scale_vector[0], scale_vector
    scalar = number(raw_scalar, 1.0)
    return scalar, [scalar, scalar, scalar]


def safe_name(value: str) -> str:
    value = value.replace("\\", "/")
    if value.startswith("textures/"):
        value = value[len("textures/") :]
    result = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")
    return result or "unnamed"


def fallback_color(material: str) -> list[float]:
    name = material.casefold()
    if any(word in name for word in ("sky", "cloud")):
        return [0.55, 0.62, 0.68]
    if any(word in name for word in ("snow", "ice")):
        return [0.82, 0.84, 0.85]
    if any(word in name for word in ("grass", "foliage", "leaf")):
        return [0.28, 0.34, 0.20]
    if any(word in name for word in ("wood", "board", "door")):
        return [0.35, 0.25, 0.16]
    if any(word in name for word in ("metal", "iron", "steel")):
        return [0.33, 0.34, 0.35]
    if any(word in name for word in ("brick", "rubble")):
        return [0.42, 0.31, 0.26]
    if any(word in name for word in ("sand", "dirt", "ground")):
        return [0.42, 0.38, 0.29]
    return [0.5, 0.5, 0.5]


def alpha_material(material: str) -> bool:
    name = material.casefold()
    return any(
        marker in name
        for marker in (
            "masked",
            "transparent",
            "fence",
            "foliage",
            "leaf",
            "nosight",
            "grate",
            "lattice",
        )
    )


def normalized_material_path(material: str) -> str:
    """BSP material name as a lowercase '/'-separated path without the
    leading 'textures/'. BSP names keep the archive's '@' separator in the
    final segment (textures/belgium/decals/decal@lightfootpath)."""
    name = material.replace("\\", "/").casefold().lstrip("/")
    if name.startswith("textures/"):
        name = name[len("textures/") :]
    return name


def polygon_offset_materials(
    asset_index: dict[str, ArchiveEntry],
) -> set[str]:
    """Materials whose pak shader stanza declares ``polygonOffset``.

    CoD1 draws these with a depth bias because the level authors laid them
    exactly coplanar over the surface beneath: terrain blend layers (grass
    and dirt over the base ground, snow fill over rubble), roads, and the
    unique overlays. The BSP keeps no trace of that bias, and decal_material
    below cannot infer it — its path rule only matches art filed under a
    ``decals/`` directory, while ``textures/normandy/ground/a_grass1b`` and
    its ~50 siblings are ordinary-looking world materials.

    Left unmarked they land on an unbiased shader and z-fight over hundreds
    of square metres of the exact ground players walk on (worst on Chateau
    and Carentan). Returning the set here lets the material manifest carry a
    ``polygonOffset`` flag that the runtime turns back into a depth bias.

    Parsed from the shader files rather than hard-coded: the same archives
    already answer skyParms (resolve_sky_environment) and the impact-mark
    polygonOffset2 stanzas, and a hand-written list would silently rot
    against a different pak set.
    """
    result: set[str] = set()
    for key, entry in asset_index.items():
        if not key.endswith(".shader"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        # Stanza = a name at column zero followed by a brace block. Tracking
        # depth keeps a nested `{ ... }` stage from being read as a new one.
        name: str | None = None
        depth = 0
        has_offset = False
        for line in text.splitlines():
            stripped = line.split("//", 1)[0].strip()
            if not stripped:
                continue
            if depth == 0:
                if stripped == "{":
                    depth = 1
                    has_offset = False
                elif not stripped.startswith("}"):
                    name = stripped
                continue
            if "polygonoffset" in stripped.casefold():
                has_offset = True
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                if has_offset and name:
                    result.add(normalized_material_path(name))
                depth = 0
                name = None
    return result


def shader_diffuse_images(
    asset_index: dict[str, ArchiveEntry],
) -> dict[str, str]:
    """Diffuse image each pak shader stanza names, keyed by material path.

    Some BSP materials exist only as shader-script names with image
    indirection: no pk3 ships an image called
    ``textures/normandy/ground/a_grass1b`` — its stanza in scripts/
    terrain.shader declares ``map textures/normandy/ground/grass@lp_brecort1b``
    instead. resolve_texture's filename candidates all miss for these, the
    manifest ships texture:"" and the runtime paints fallbackColor: that is
    Carentan's floors rendering the plain-green 'grass' fallback, drawn in
    front of the correctly textured base ground because the very same
    stanzas also declare polygonOffset. Same mechanism on Chateau
    (dirt_earthbase) and the Arnhem/Cassino ``*_blend`` terrain groups.

    The image is the first stage ``map <path>`` whose path is not a builtin
    ($lightmap/$whiteimage/$dlight), skipping the optional ``clamp`` wrap
    token (``map clamp textures/sfx/facade_tiler2``); ``qer_editorimage``
    stands in when no stage names a real image. Same column-zero/brace-depth
    stanza walker as polygon_offset_materials.
    """
    result: dict[str, str] = {}
    for key, entry in asset_index.items():
        if not key.endswith(".shader"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        name: str | None = None
        depth = 0
        stage_image: str | None = None
        editor_image: str | None = None
        for line in text.splitlines():
            stripped = line.split("//", 1)[0].strip()
            if not stripped:
                continue
            if depth == 0:
                if stripped == "{":
                    depth = 1
                    stage_image = None
                    editor_image = None
                elif not stripped.startswith("}"):
                    name = stripped
                continue
            fields = stripped.split()
            token = fields[0].casefold()
            if token == "map" and len(fields) > 1 and stage_image is None:
                image = fields[1]
                if image.casefold() == "clamp" and len(fields) > 2:
                    image = fields[2]
                if not image.startswith("$"):
                    stage_image = image
            elif (
                token == "qer_editorimage"
                and len(fields) > 1
                and editor_image is None
            ):
                editor_image = fields[1]
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                image = stage_image or editor_image
                if name and image:
                    result[normalized_material_path(name)] = image
                depth = 0
                name = None
    return result


def alpha_tested_materials(
    asset_index: dict[str, ArchiveEntry],
) -> set[str]:
    """Materials whose pak shader stanza alpha-tests the diffuse texture:
    ``alphaFunc GE128`` without ``alphaGen vertex``.

    The alpha_material() name markers cannot see a stanza-declared alpha
    test, so a marker-less material CoD1 clips by texture alpha ships
    alphaCutout=false and renders its keyed-out texels opaque. The stanza is
    authoritative — but only when the tested alpha actually comes from the
    texture. A stanza that also declares ``alphaGen vertex`` tested VERTEX
    alpha, which the export does not carry (world.glb has no COLOR_0);
    clipping those by texture alpha instead would punch wrong holes through
    Railyard's painted sfx/facade_* backdrops (27% of facade_tiler2's
    texels sit below the 128 threshold).
    """
    result: set[str] = set()
    for key, entry in asset_index.items():
        if not key.endswith(".shader"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        name: str | None = None
        depth = 0
        texture_alpha_test = False
        vertex_alpha = False
        for line in text.splitlines():
            stripped = line.split("//", 1)[0].strip()
            if not stripped:
                continue
            if depth == 0:
                if stripped == "{":
                    depth = 1
                    texture_alpha_test = False
                    vertex_alpha = False
                elif not stripped.startswith("}"):
                    name = stripped
                continue
            fields = stripped.casefold().split()
            if fields[:2] == ["alphafunc", "ge128"]:
                texture_alpha_test = True
            elif fields[:2] == ["alphagen", "vertex"]:
                vertex_alpha = True
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                if name and texture_alpha_test and not vertex_alpha:
                    result.add(normalized_material_path(name))
                depth = 0
                name = None
    return result


def impact_surface_vocabulary(
    asset_index: dict[str, ArchiveEntry],
) -> frozenset[str]:
    """Surface tokens the layered impact tables (``fx/*.csv``) speak.

    Derived from the same archives the map is built from rather than
    hard-coded, so the ``surface`` harvest below can never invent a token
    retail's fx/iw_impacts.csv (and UO's gmi_impacts.csv) cannot key an
    effect on. The union across tables is the vocabulary; the per-row
    override merge is irrelevant here because overriding a row never
    changes which surface columns exist.
    """
    tokens: set[str] = set()
    for key, entry in asset_index.items():
        if not key.startswith("fx/") or not key.endswith(".csv"):
            continue
        if "/" in key[len("fx/"):]:
            continue
        rows = parse_impact_table(
            read_entry(entry).decode("latin1", "replace")
        )
        if rows is not None:
            tokens |= impact_table_surface_vocabulary(rows)
    return frozenset(tokens)


def surfaceparm_materials(
    asset_index: dict[str, ArchiveEntry],
    vocabulary: frozenset[str],
) -> dict[str, str]:
    """Impact surface type each pak shader stanza declares, keyed by
    material path: the ``surfaceparm <token>`` whose token is one of the
    impact table's surface words (grass, brick, metal, ...).

    This is what retail keys the per-surface bullet impact effects on, and
    materials.json carries it as an additive ``surface`` field. Only an
    explicit surface-vocabulary token qualifies — the many non-surface
    parms (nomarks, trans, nolightmap, noimpact, playerclip, nonsolid, ...)
    share the keyword but describe behaviour, not material, and must never
    land in the field. When a stanza declares several vocabulary tokens the
    last one wins, matching the engine's parm-by-parm application order.
    Same column-zero/brace-depth stanza walker as polygon_offset_materials.
    """
    result: dict[str, str] = {}
    for key, entry in asset_index.items():
        if not key.endswith(".shader"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        name: str | None = None
        depth = 0
        surface: str | None = None
        for line in text.splitlines():
            stripped = line.split("//", 1)[0].strip()
            if not stripped:
                continue
            if depth == 0:
                if stripped == "{":
                    depth = 1
                    surface = None
                elif not stripped.startswith("}"):
                    name = stripped
                continue
            fields = stripped.casefold().split()
            if (
                len(fields) >= 2
                and fields[0] == "surfaceparm"
                and fields[1] in vocabulary
            ):
                surface = fields[1]
            depth += stripped.count("{") - stripped.count("}")
            if depth <= 0:
                if name and surface:
                    result[normalized_material_path(name)] = surface
                depth = 0
                name = None
    return result


def decal_material(material: str) -> bool:
    """A decal surface: renders blended on top of the wall/ground it is
    stuck to instead of as its own opaque brush face.

    CoD 1 has no single decal directory. The Pavlov-era rule only matched
    'textures/decals/*', which missed every map-themed decal set
    (textures/belgium/decals/*, textures/normandy/decals/*,
    textures/german/decal_metalnomask@*, textures/belgium/ground/decal@*)
    and left those materials opaque, so their keyed-out background rendered
    as a black box — the crater below Rocket's V2. Both authored conventions
    are now honoured:

    * a 'decals' path segment anywhere above the file name; or
    * a final segment named 'decal' + separator ('_' or the archive's '@').

    Verified against every BSP material of the four shipped maps (320
    unique names): these two rules together catch exactly the 26 names that
    contain 'decal' and nothing else — belgium/ground/decal@1024alpharoad
    and german/decal_metalnomask@exit are only reachable by the second
    rule, normandy/decals/128windowborder01 only by the first, and
    denmark/floors/wood@hotel_floor1 (and the other 294) match neither.
    """
    name = normalized_material_path(material)
    segments = name.split("/")
    if "decals" in segments[:-1]:
        return True
    leaf = segments[-1]
    return leaf.startswith("decal_") or leaf.startswith("decal@")


def black_key_decal_material(material: str) -> bool:
    """The retired BuildPavlovScene black-key crater family: the root
    'textures/decals/*' bomb craters and mortar scars whose shipped alpha
    the fod_decal_alpha rebuild was tuned against (see decal_alpha_plan)."""
    return normalized_material_path(material).startswith("decals/")


def alpha_channel_state(image: Image.Image) -> str:
    """Classify a decoded texture's alpha channel.

    'none'   - no alpha band at all (24-bit TGA/JPG);
    'opaque' - an alpha band that is solid white apart from stray texels;
    'binary' - punch-through (two distinct levels);
    'graded' - a real authored gradient.
    """
    if "A" not in image.getbands():
        return "none"
    alpha = np.asarray(image.convert("RGBA"))[..., 3]
    if float((alpha < 250).mean()) < 0.002:
        return "opaque"
    return "binary" if len(np.unique(alpha)) <= 2 else "graded"


def decal_alpha_plan(material: str, alpha_state: str) -> tuple[bool, str]:
    """Decide whether a decal texture ships as-is or gets its alpha rebuilt,
    and return (rebuild, reason) for the run log.

    The fod_decal_alpha pass is NOT a generic 'add the missing alpha' fix:
    it is the crater reconstruction ported from build_pavlov_decal_alpha.py.
    It keeps only the largest silhouette component, forces a central blast
    bowl and paints a soil floor into it — assumptions that hold for the
    'textures/decals/*' craters it was tuned on and for nothing else. It
    therefore stays scoped to that family, and every other decal ships the
    texture the pk3 authored:

    * belgium/normandy decals are DXT3/DXT5 with real graded alpha (road,
      footpath, window-border, poster); rebuilding them would replace an
      authored gradient with a synthetic crater silhouette;
    * german/decal_metalnomask@* are 24-bit TGA signage with no alpha at
      all ('nomask' is the author's own note) and must stay solid — running
      the crater rebuild on them would carve a blast bowl through the text.
    """
    if black_key_decal_material(material):
        return True, f"rebuild black-key crater alpha (source: {alpha_state})"
    if alpha_state == "none":
        return False, "ship as-is (no alpha channel — opaque decal)"
    return False, f"ship as-is (authored {alpha_state} alpha)"


def decal_tint(group: str, rebuilt: bool) -> list[float]:
    """Decal shader tint written into materials.json's fallbackColor as
    [brightness r, g, b, opacity].

    fallbackColor only ever stood in for a missing texture, and the importer
    never marks a material as a decal unless its texture resolved, so the
    field is free to carry the decal shader parameters instead of hard-coding
    a material name in FodMaterialFactory.

    Rebuilt craters keep the retired BuildPavlovScene constants the
    reconstruction was tuned against (_Brightness .55 for the cratered
    ground, .68 for the other craters, _Opacity .95). Decals that ship their
    authored alpha render untinted: the pk3 already stores the colour the
    game draws, and the .68 crater brightness would grey out Rocket's snow
    road and footpaths.
    """
    if not rebuilt:
        return [1.0, 1.0, 1.0, 1.0]
    if group.casefold() == "decals_decal_cratered_ground":
        return [0.55, 0.55, 0.55, 0.95]
    return [0.68, 0.68, 0.68, 0.95]


def sky_material(material: str) -> bool:
    name = material.casefold()
    return (
        name.startswith("sky")
        or "/sky" in name
        or name.startswith("textures/sky")
        or name.startswith("textures/skies")
    )


def resolve_sky_environment(
    material: str,
    asset_index: dict[str, ArchiveEntry],
) -> str | None:
    target = material.replace("\\", "/").casefold()
    for key, entry in reversed(list(asset_index.items())):
        if not key.endswith(".shader"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        lower = text.casefold()
        position = lower.find(target)
        while position >= 0:
            segment = text[position : position + 2400]
            match = re.search(
                r"\bskyParms\s+([^\s]+)",
                segment,
                re.IGNORECASE,
            )
            if match is not None:
                return match.group(1).replace("\\", "/")
            position = lower.find(target, position + len(target))
    return None


def extract_sky(
    sky_material_names: list[str],
    asset_index: dict[str, ArchiveEntry],
    output_dir: Path,
    map_id: str,
) -> dict[str, object] | None:
    face_names = ("ft", "bk", "lf", "rt", "up", "dn")
    for material in sky_material_names:
        environment = resolve_sky_environment(material, asset_index)
        if not environment or environment == "-":
            continue
        entries = {}
        for face in face_names:
            entry = None
            for extension in TEXTURE_EXTENSIONS:
                entry = asset_index.get(
                    f"{environment}_{face}{extension}".casefold()
                )
                if entry is not None:
                    break
            if entry is None:
                entries = {}
                break
            entries[face] = entry
        if len(entries) != len(face_names):
            continue

        sky_dir = output_dir / "sky"
        sky_dir.mkdir(parents=True, exist_ok=True)
        sources = {}
        for face, entry in entries.items():
            image = Image.open(io.BytesIO(read_entry(entry))).convert("RGB")
            destination = sky_dir / f"{face}.png"
            image.save(destination, optimize=True)
            sources[face] = (
                f"{entry.archive.name}:{entry.name}"
            )
        panorama = sky_dir / f"{map_id}_equirect.png"
        build_sky_panorama(sky_dir, panorama, 2048, 1024)
        return {
            "material": material,
            "environment": environment,
            "panorama": f"sky/{panorama.name}",
            "faces": sources,
        }
    return None


def resolve_texture(
    material: str,
    asset_index: dict[str, ArchiveEntry],
    shader_images: dict[str, str] | None = None,
) -> ArchiveEntry | None:
    """Map a BSP material name to its pk3 diffuse image.

    Faithful port of the proven Pavlov build_assets.py find_image():
    candidate names are tried in the order manual-substitution, exact
    name, then first-underscore->'@' swap in the final path segment
    (materials such as belgium/ground/snow_1024lightfill store the
    archive's '@' as '_'), with DDS preferred over TGA per candidate.
    Only when no candidate ships either native container does the
    JPG/JPEG/PNG fallback tier run.

    When every filename candidate misses and shader_images
    (shader_diffuse_images) carries the material's stanza, the stanza's
    referenced diffuse resolves through this same machinery — Carentan's
    terrain blend layers (a_grass1b -> grass@lp_brecort1b) exist only that
    way. An explicit image extension on the reference is dropped first so
    the DDS-preferred candidate order still applies (gmi_terrain.shader
    writes ``map .../rubble2b.tga`` for an image that ships as
    rubble2b.dds).
    """
    normalized = material.replace("\\", "/").lstrip("/")
    names = [normalized]
    if not normalized.casefold().startswith("textures/"):
        names.append("textures/" + normalized)
    candidates: list[str] = []
    for name in names:
        substitute = TEXTURE_SUBSTITUTIONS.get(name.casefold())
        if substitute is not None:
            candidates.append(substitute)
        candidates.append(name)
        directory, _, stem = name.rpartition("/")
        if directory and "@" not in stem and "_" in stem:
            candidates.append(f"{directory}/{stem.replace('_', '@', 1)}")
    for extensions in (
        PRIMARY_TEXTURE_EXTENSIONS,
        FALLBACK_TEXTURE_EXTENSIONS,
    ):
        for candidate in candidates:
            for extension in extensions:
                match = asset_index.get(
                    (candidate + extension).casefold()
                )
                if match is not None:
                    return match
    if shader_images:
        referenced = shader_images.get(normalized_material_path(material))
        if referenced:
            suffix = PurePosixPath(referenced).suffix.casefold()
            if suffix in TEXTURE_EXTENSIONS:
                referenced = str(PurePosixPath(referenced).with_suffix(""))
            return resolve_texture(referenced, asset_index)
    return None


def texture_destination_name(entry: ArchiveEntry) -> str:
    logical = entry.name.replace("\\", "/").casefold()
    digest = hashlib.sha1(logical.encode("utf-8")).hexdigest()[:10]
    basename = safe_name(str(PurePosixPath(entry.name).with_suffix("")))
    suffix = PurePosixPath(entry.name).suffix.casefold()
    return f"{basename}__{digest}{suffix}"


def png_texture_name(entry: ArchiveEntry) -> str:
    return str(
        PurePosixPath(texture_destination_name(entry)).with_suffix(".png")
    )


def convert_texture_to_png(entry: ArchiveEntry, destination: Path) -> None:
    if destination.is_file():
        return
    image = Image.open(io.BytesIO(read_entry(entry)))
    if image.mode == "P":
        image = image.convert(
            "RGBA" if "transparency" in image.info else "RGB"
        )
    elif image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.mode else "RGB")
    largest = max(image.size)
    if largest > MAX_WORLD_TEXTURE_SIZE:
        scale = MAX_WORLD_TEXTURE_SIZE / largest
        image = image.resize(
            (round(image.size[0] * scale), round(image.size[1] * scale)),
            Image.LANCZOS,
        )
    image.save(destination, optimize=True)


def build_decal_rgba(texture_dir: Path, png_name: str) -> str:
    """Reconstruct black-key decal alpha (fod_decal_alpha) for a converted
    shared texture and return the *_rgba.png companion's file name."""
    rgba_name = f"{PurePosixPath(png_name).stem}_rgba.png"
    destination = texture_dir / rgba_name
    if not destination.is_file():
        build_decal_alpha(texture_dir / png_name, destination)
    return rgba_name


def material_groups(material_names: list[str]) -> dict[int, str]:
    used: dict[str, str] = {}
    result: dict[int, str] = {}
    for index, material in enumerate(material_names):
        candidate = safe_name(material)
        key = candidate.casefold()
        if key in used and used[key] != material:
            digest = hashlib.sha1(
                material.encode("latin1", "replace")
            ).hexdigest()[:8]
            candidate = f"{candidate}_{digest}"
        used[candidate.casefold()] = material
        result[index] = candidate
    return result


def parse_world_lumps(data: bytes) -> tuple[
    list[str],
    dict[int, str],
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    dict[int, list[np.ndarray]],
    list[tuple[int, int, np.ndarray]],
    int,
]:
    material_data = lump(data, 0)
    soup_data = lump(data, 6)
    vertex_data = lump(data, 7)
    index_data = lump(data, 8)
    if len(material_data) % 72:
        raise ValueError("material lump stride is not 72 bytes")
    if len(soup_data) % 16:
        raise ValueError("draw-soup lump stride is not 16 bytes")
    if len(vertex_data) % 44:
        raise ValueError("vertex lump stride is not 44 bytes")
    if len(index_data) % 2:
        raise ValueError("index lump stride is not 2 bytes")

    material_names = [
        material_data[offset : offset + 64]
        .split(b"\0", 1)[0]
        .decode("latin1", "replace")
        for offset in range(0, len(material_data), 72)
    ]
    groups = material_groups(material_names)
    vertex_count = len(vertex_data) // 44
    raw = np.frombuffer(vertex_data, np.uint8).reshape(vertex_count, 44)
    source_positions = (
        raw[:, 0:12].copy().view(np.float32).reshape(vertex_count, 3)
    )
    source_uvs = (
        raw[:, 12:20].copy().view(np.float32).reshape(vertex_count, 2)
    )
    # Per-vertex lightmap UV (bytes 20:28), page-normalized 0..1; used as the
    # GLB TEXCOORD_1 channel. Skipped by the legacy readers before M3.
    source_lightmap_uvs = (
        raw[:, 20:28].copy().view(np.float32).reshape(vertex_count, 2)
    )
    source_normals = (
        raw[:, 28:40].copy().view(np.float32).reshape(vertex_count, 3)
    )
    source_indices = np.frombuffer(index_data, np.uint16)
    faces: dict[int, list[np.ndarray]] = defaultdict(list)
    soup_faces: list[tuple[int, int, np.ndarray]] = []
    for offset in range(0, len(soup_data), 16):
        material, lightmap_page, first_vertex, _vertex_length, count, first = (
            struct.unpack_from("<HHIHHI", soup_data, offset)
        )
        if material >= len(material_names):
            raise ValueError(f"surface references material {material}")
        local = (
            source_indices[first : first + count].astype(np.int64)
            + first_vertex
        )
        if len(local) % 3:
            raise ValueError("surface index count is not divisible by three")
        triangles = local.reshape(-1, 3)
        faces[material].append(triangles)
        soup_faces.append((material, lightmap_page, triangles))
    return (
        material_names,
        groups,
        source_positions,
        source_uvs,
        source_lightmap_uvs,
        source_normals,
        faces,
        soup_faces,
        vertex_count,
    )


def partition_world_render_sectors(
    visibility: BspVisibility,
    material_names: list[str],
    soup_faces: list[tuple[int, int, np.ndarray]],
    source_positions: np.ndarray,
    excluded_soups: set[int] | None = None,
) -> dict[tuple[int, int] | None, RenderSectorPartition]:
    """Partition static triangles into offline 32 m Unity-XZ grid sectors.

    ``None`` is the explicit always-visible sector. It receives sky,
    all-solid faces and anything whose exact cluster ownership cannot be
    established. Cross-cluster triangles remain in one spatial sector and
    contribute every intersected cluster to its conservative visibility
    union. Each source triangle is emitted exactly once.
    """
    excluded = excluded_soups or set()
    pending: dict[
        tuple[int, int] | None,
        dict[tuple[int, int], list[np.ndarray]],
    ] = defaultdict(lambda: defaultdict(list))
    pending_clusters: dict[
        tuple[int, int],
        set[int],
    ] = defaultdict(set)
    source_triangle_count = 0
    for soup_index, (material_index, lightmap_page, triangles) in enumerate(
        soup_faces
    ):
        if soup_index in excluded:
            continue
        force_visible = sky_material(material_names[material_index])
        group_key = (material_index, lightmap_page)
        for triangle in triangles:
            source_triangle_count += 1
            clusters = (
                None
                if force_visible
                else triangle_visibility_clusters(
                    visibility,
                    source_positions,
                    triangle,
                )
            )
            if not clusters:
                key = None
            else:
                centroid = source_positions[
                    np.asarray(triangle, dtype=np.int64)
                ].mean(axis=0)
                # FodGlbStaticLoader maps CoD (x,y,z) to Unity
                # (-x,z,-y)*scale. Match the retired runtime XZ grid exactly.
                key = (
                    math.floor(
                        -float(centroid[0])
                        * COD_UNIT_TO_METRE
                        / RENDER_SECTOR_SIZE_METRES
                    ),
                    math.floor(
                        -float(centroid[1])
                        * COD_UNIT_TO_METRE
                        / RENDER_SECTOR_SIZE_METRES
                    ),
                )
                pending_clusters[key].update(clusters)
            pending[key][group_key].append(triangle)

    result: dict[
        tuple[int, int] | None,
        RenderSectorPartition,
    ] = {}
    emitted_triangle_count = 0
    for key, groups_in_sector in pending.items():
        packed_faces: dict[tuple[int, int], list[np.ndarray]] = {}
        for group_key, triangles in groups_in_sector.items():
            packed = np.asarray(triangles, dtype=np.int64)
            if packed.ndim != 2 or packed.shape[1] != 3:
                raise ValueError(
                    f"sector {key} group {group_key} has malformed faces"
                )
            packed_faces[group_key] = [packed]
            emitted_triangle_count += len(packed)
        result[key] = RenderSectorPartition(
            grid_x=0 if key is None else key[0],
            grid_z=0 if key is None else key[1],
            always_visible=key is None,
            cluster_indices=(
                ()
                if key is None
                else tuple(sorted(pending_clusters[key]))
            ),
            faces=packed_faces,
        )
    if emitted_triangle_count != source_triangle_count:
        raise ValueError(
            "render-sector partition lost or duplicated triangles: "
            f"{source_triangle_count} source, "
            f"{emitted_triangle_count} emitted"
        )
    return result


def extract_world(
    data: bytes,
    output_dir: Path,
    map_id: str,
    game: str,
    asset_index: dict[str, ArchiveEntry],
    shared_texture_dir: Path,
) -> dict[str, object]:
    (
        material_names,
        groups,
        source_positions,
        source_uvs,
        _source_lightmap_uvs,
        source_normals,
        faces,
        _soup_faces,
        vertex_count,
    ) = parse_world_lumps(data)
    positions = np.column_stack(
        (
            source_positions[:, 0],
            source_positions[:, 2],
            -source_positions[:, 1],
        )
    )
    uvs = np.column_stack((source_uvs[:, 0], 1.0 - source_uvs[:, 1]))
    normals = np.column_stack(
        (
            source_normals[:, 0],
            source_normals[:, 2],
            -source_normals[:, 1],
        )
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    shared_texture_dir.mkdir(parents=True, exist_ok=True)
    material_manifest = []
    shader_images = shader_diffuse_images(asset_index)
    texture_alpha_tested = alpha_tested_materials(asset_index)
    texture_rel_root = Path("..") / ".." / ".." / "Shared" / game / "Textures"
    for material_index in sorted(faces, key=lambda item: material_names[item]):
        source_name = material_names[material_index]
        group = groups[material_index]
        texture = resolve_texture(source_name, asset_index, shader_images)
        texture_relative = None
        texture_source = None
        if texture is not None:
            destination_name = texture_destination_name(texture)
            destination = shared_texture_dir / destination_name
            if not destination.exists() or destination.stat().st_size != texture.size:
                destination.write_bytes(read_entry(texture))
            texture_relative = (
                texture_rel_root / destination_name
            ).as_posix()
            texture_source = (
                f"{texture.archive.name}:{texture.name}"
            )
        material_manifest.append(
            {
                "source": source_name,
                "group": group,
                "texture": texture_relative,
                "textureSource": texture_source,
                "alphaCutout": (
                    alpha_material(source_name)
                    or normalized_material_path(source_name)
                    in texture_alpha_tested
                ),
                "sky": sky_material(source_name),
                "fallbackColor": fallback_color(source_name),
            }
        )

    sky = extract_sky(
        [
            item["source"]
            for item in material_manifest
            if item["sky"]
        ],
        asset_index,
        output_dir,
        map_id,
    )

    obj_path = output_dir / f"{map_id}.obj"
    with obj_path.open("w", encoding="utf-8", newline="\n") as output:
        triangle_count = sum(
            sum(len(chunk) for chunk in chunks)
            for chunks in faces.values()
        )
        output.write(
            f"# {game} multiplayer map '{map_id}' render geometry\n"
            "# extracted from CoD IBSP v59 by "
            "tools/import_cod_multiplayer_maps.py\n"
            "# Y-up: (x,z,-y); CoD inches, imported by Unity at 0.0254\n"
            f"# {vertex_count} vertices, {triangle_count} triangles, "
            f"{len(faces)} material groups\n"
            f"mtllib {map_id}.mtl\n"
        )
        for x, y, z in positions:
            output.write(f"v {x:.4f} {y:.4f} {z:.4f}\n")
        for u, v in uvs:
            output.write(f"vt {u:.5f} {v:.5f}\n")
        for x, y, z in normals:
            output.write(f"vn {x:.4f} {y:.4f} {z:.4f}\n")
        for material_index in sorted(
            faces, key=lambda item: material_names[item]
        ):
            group = groups[material_index]
            output.write(f"g {group}\nusemtl {group}\n")
            for chunk in faces[material_index]:
                for a, b, c in chunk + 1:
                    # CoD draw indices are clockwise. Reversing B/C produces
                    # Unity-facing triangles while preserving authored normals.
                    output.write(
                        f"f {a}/{a}/{a} {c}/{c}/{c} {b}/{b}/{b}\n"
                    )

    mtl_path = output_dir / f"{map_id}.mtl"
    by_group = {item["group"]: item for item in material_manifest}
    with mtl_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(
            f"# Original diffuse materials for {game}:{map_id}\n\n"
        )
        for group in sorted(by_group):
            item = by_group[group]
            red, green, blue = item["fallbackColor"]
            output.write(
                f"newmtl {group}\n"
                "Ka 0.0 0.0 0.0\n"
                f"Kd {red:.4f} {green:.4f} {blue:.4f}\n"
            )
            if item["texture"]:
                output.write(f"map_Kd {item['texture']}\n")
                if item["alphaCutout"]:
                    output.write(f"map_d {item['texture']}\n")
            output.write("d 1.0\nillum 1\n\n")

    minimum = positions.min(axis=0).tolist()
    maximum = positions.max(axis=0).tolist()
    return {
        "vertices": vertex_count,
        "triangles": triangle_count,
        "materials": len(faces),
        "texturedMaterials": sum(
            item["texture"] is not None for item in material_manifest
        ),
        "alphaMaterials": sum(
            item["alphaCutout"] for item in material_manifest
        ),
        "boundsCodUnits": {
            "minimum": minimum,
            "maximum": maximum,
            "size": [
                maximum[index] - minimum[index] for index in range(3)
            ],
        },
        "sky": sky,
        "materialManifest": material_manifest,
    }


def extract_world_pak(
    data: bytes,
    output_dir: Path,
    map_id: str,
    game: str,
    asset_index: dict[str, ArchiveEntry],
    shared_texture_dir: Path,
    entities: list[dict[str, str]],
) -> dict[str, object]:
    (
        material_names,
        groups,
        source_positions,
        source_uvs,
        source_lightmap_uvs,
        source_normals,
        _faces,
        soup_faces,
        vertex_count,
    ) = parse_world_lumps(data)
    # Y-up CoD-unit frame, identical to the OBJ path; see fod_glb_writer for
    # the full OBJ/GLB convention chain.
    yup_positions = np.column_stack(
        (
            source_positions[:, 0],
            source_positions[:, 2],
            -source_positions[:, 1],
        )
    )
    glb_positions = (
        yup_positions * np.float32(COD_UNIT_TO_METRE)
    ).astype(np.float32)
    glb_normals = np.column_stack(
        (
            source_normals[:, 0],
            source_normals[:, 2],
            -source_normals[:, 1],
        )
    )
    lengths = np.linalg.norm(glb_normals, axis=1, keepdims=True)
    glb_normals = (
        glb_normals / np.maximum(lengths, np.float32(1e-6))
    ).astype(np.float32)
    glb_uvs = source_uvs.astype(np.float32)
    # Lightmap channel (TEXCOORD_1) and the decoded page atlas. Stored raw
    # (page-normalized 0..1, no v-flip) exactly like glb_uvs, so the runtime's
    # shared TEXCOORD flip applies to both identically. Empty when the map has
    # no baked lightmaps -> primitives emit no uvs2 and lightmapIndex = -1.
    glb_uvs2 = source_lightmap_uvs.astype(np.float32)
    lightmap_pages = decode_lightmap_pages(data)

    brush_assets = exploder_brush_assets(
        data,
        entities,
        parse_brush_models(data),
        game,
        map_id,
    )
    soup_owner = {
        soup_index: source_index
        for source_index, asset in brush_assets.items()
        for soup_index in asset["_soupIndices"]
    }
    # Group by (material, lightmap_page) so each GLB primitive references one
    # lightmap page. The material-manifest/texture code below projects the key
    # back to its material index; only the GLB primitive split cares about page.
    static_faces: dict[tuple[int, int], list[np.ndarray]] = defaultdict(list)
    brush_faces: dict[int, dict[tuple[int, int], list[np.ndarray]]] = {
        source_index: defaultdict(list)
        for source_index in brush_assets
    }
    for soup_index, (material, lightmap_page, triangles) in enumerate(
        soup_faces
    ):
        owner = soup_owner.get(soup_index)
        group_key = (material, lightmap_page)
        if owner is None:
            static_faces[group_key].append(triangles)
        else:
            brush_faces[owner][group_key].append(triangles)

    output_dir.mkdir(parents=True, exist_ok=True)
    shared_texture_dir.mkdir(parents=True, exist_ok=True)
    # Project the (material, page) buckets back to per-material triangle counts;
    # the material manifest and texture staging are per material, page-agnostic.
    material_triangle_counts: dict[int, int] = defaultdict(int)
    for (material, _page), chunks in static_faces.items():
        material_triangle_counts[material] += sum(
            len(chunk) for chunk in chunks
        )
    for per_entity in brush_faces.values():
        for (material, _page), chunks in per_entity.items():
            material_triangle_counts[material] += sum(
                len(chunk) for chunk in chunks
            )
    all_render_materials = [
        material
        for material in sorted(
            material_triangle_counts,
            key=lambda item: material_names[item],
        )
        if material_triangle_counts[material]
    ]
    material_manifest = []
    rebuilt_decals = 0
    offset_materials = polygon_offset_materials(asset_index)
    shader_images = shader_diffuse_images(asset_index)
    texture_alpha_tested = alpha_tested_materials(asset_index)
    material_surfaces = surfaceparm_materials(
        asset_index,
        impact_surface_vocabulary(asset_index),
    )
    biased_materials = 0
    for material_index in all_render_materials:
        source_name = material_names[material_index]
        group_name = groups[material_index]
        looks_like_decal = decal_material(source_name)
        texture = resolve_texture(source_name, asset_index, shader_images)
        texture_relative = None
        texture_source = None
        is_decal = False
        rebuilt = False
        if texture is not None:
            destination_name = png_texture_name(texture)
            convert_texture_to_png(
                texture, shared_texture_dir / destination_name
            )
            if looks_like_decal:
                # A decal is only meaningful with a texture to blend, so the
                # flag is set here rather than from the name alone: a decal
                # entry with an empty texture would route the runtime at the
                # decal shader's default map and paint a black quad.
                is_decal = True
                with Image.open(
                    shared_texture_dir / destination_name
                ) as converted:
                    alpha_state = alpha_channel_state(converted)
                rebuilt, reason = decal_alpha_plan(source_name, alpha_state)
                if rebuilt:
                    destination_name = build_decal_rgba(
                        shared_texture_dir, destination_name
                    )
                    rebuilt_decals += 1
                print(f"  decal {group_name}: {reason}")
            texture_relative = (
                f"maps/shared/{game}/textures/{destination_name}"
            )
            texture_source = f"{texture.archive.name}:{texture.name}"
        elif looks_like_decal:
            print(
                f"  decal {group_name}: no texture in any archive — "
                "left opaque with its fallback colour"
            )
        # The decal shader already carries its own Offset, so flagging a
        # decal here as well would double the bias for no gain.
        polygon_offset = (
            not is_decal
            and normalized_material_path(source_name) in offset_materials
        )
        if polygon_offset:
            biased_materials += 1
        material_entry = {
            "source": source_name,
            "group": group_name,
            "texture": texture_relative,
            "textureSource": texture_source,
            "alphaCutout": (
                alpha_material(source_name)
                or normalized_material_path(source_name)
                in texture_alpha_tested
            ),
            "decal": is_decal,
            "polygonOffset": polygon_offset,
            "sky": sky_material(source_name),
            "fallbackColor": (
                decal_tint(group_name, rebuilt)
                if is_decal
                else fallback_color(source_name)
            ),
        }
        # Additive: present only when the shader stanza declared an explicit
        # impact-vocabulary surfaceparm; the runtime's name heuristics stay
        # in charge for every other material.
        surface = material_surfaces.get(
            normalized_material_path(source_name)
        )
        if surface:
            material_entry["surface"] = surface
        material_manifest.append(material_entry)

    # ``textures/common/ladder*`` brushes do not have draw-soup faces and
    # therefore cannot appear in the loop above.  Add one collision-only
    # material group whose exact brush volumes survive into materials.json.
    # A single group is enough even when a map mixes ladder and ladder_wood:
    # runtime traversal depends on the authored volumes, not diffuse art.
    ladder_volumes = ladder_brush_volumes(data)
    if ladder_volumes:
        material_manifest.append(
            {
                "source": "textures/common/ladder",
                "group": "common_ladder_collision",
                "texture": None,
                "textureSource": None,
                "alphaCutout": False,
                "decal": False,
                "sky": False,
                "ladder": True,
                "ladderVolumes": ladder_volumes,
                "fallbackColor": [0.0, 0.0, 0.0],
            }
        )

    sky = extract_sky(
        [
            item["source"]
            for item in material_manifest
            if item["sky"]
        ],
        asset_index,
        output_dir,
        map_id,
    )
    if sky is not None:
        sky_dir = output_dir / "sky"
        (sky_dir / f"{map_id}_equirect.png").replace(output_dir / "sky.png")
        shutil.rmtree(sky_dir)
        sky["panorama"] = "sky.png"

    def build_primitives(
        source_faces: dict[tuple[int, int], list[np.ndarray]],
        positions: np.ndarray,
    ) -> tuple[list[dict[str, object]], int]:
        primitives: list[dict[str, object]] = []
        triangle_total = 0
        for group_key in sorted(
            source_faces,
            key=lambda item: (material_names[item[0]], item[1]),
        ):
            material_index, lightmap_page = group_key
            chunks = source_faces[group_key]
            if not chunks:
                continue
            triangles = np.concatenate(chunks)
            triangle_total += len(triangles)
            # Store (a, c, b): the runtime loader's winding reversal restores
            # the proven Unity-facing (a, b, c) order. Do not store unflipped
            # BSP order; that renders the geometry inside-out.
            flat = triangles[:, [0, 2, 1]].ravel()
            unique, inverse = np.unique(flat, return_inverse=True)
            # A surface is lit only when it names a real decoded page. The
            # 0xFFFF unlit sentinel and any out-of-range page (or a map with no
            # lightmaps at all) fall through to lightmapIndex = -1 / no uvs2.
            is_lit = 0 <= lightmap_page < len(lightmap_pages)
            primitive: dict[str, object] = {
                "material": groups[material_index],
                "positions": positions[unique],
                "normals": glb_normals[unique],
                "uvs": glb_uvs[unique],
                "indices": inverse.reshape(-1).astype(np.uint32),
                "lightmapIndex": lightmap_page if is_lit else -1,
            }
            if is_lit:
                primitive["uvs2"] = glb_uvs2[unique]
            primitives.append(primitive)
        return primitives, triangle_total

    primitives, triangle_count = build_primitives(
        static_faces,
        glb_positions,
    )
    write_glb(output_dir / "world.glb", primitives)

    # Invisible movement collision. Kept in its own GLB rather than merged
    # into world.glb so it never reaches a renderer, a lightmap or the
    # sector culler — it is collision and nothing else.
    clip_primitives, clip_brush_count = clip_brush_primitives(data)
    clip_glb_relative = ""
    clip_triangle_count = 0
    clip_path = output_dir / "clip.glb"
    if clip_primitives:
        write_glb(clip_path, clip_primitives, node_name="clip")
        clip_glb_relative = f"maps/{game}/{map_id}/clip.glb"
        clip_triangle_count = sum(
            len(primitive["indices"]) // 3 for primitive in clip_primitives
        )
        print(
            f"  clip: {clip_brush_count} collision brushes -> "
            f"{clip_triangle_count} triangles across "
            f"{len(clip_primitives)} groups"
        )
    elif clip_path.exists():
        # A re-import that now resolves no clip brushes must not leave the
        # previous run's file behind for the catalog to point at.
        clip_path.unlink()

    # Build the game-ready render topology once, beside the immutable
    # compatibility world.glb. Runtime can load these small 32 m grid sectors
    # directly and therefore skips its former per-launch rebucketing. Each
    # spatial sector owns the exact union of PVS clusters intersected by its
    # triangles; ambiguous/all-solid/sky faces fail open into one global
    # always-visible sector.
    visibility = parse_bsp_visibility(data)
    sector_faces = partition_world_render_sectors(
        visibility,
        material_names,
        soup_faces,
        source_positions,
        excluded_soups=set(soup_owner),
    )
    sectors_dir = output_dir / "sectors"
    sectors_dir.mkdir(parents=True, exist_ok=True)
    sector_manifest: list[dict[str, object]] = []
    expected_sector_files: set[str] = set()
    sector_triangle_total = 0
    always_visible_triangles = 0
    ordered_sectors = sorted(
        sector_faces,
        key=lambda key: (
            key is None,
            0 if key is None else key[0],
            0 if key is None else key[1],
        ),
    )

    def grid_token(value: int) -> str:
        return f"m{abs(value):03d}" if value < 0 else f"p{value:03d}"

    for key in ordered_sectors:
        partition = sector_faces[key]
        sector_name = (
            "always_visible"
            if partition.always_visible
            else (
                f"grid_{grid_token(partition.grid_x)}_"
                f"{grid_token(partition.grid_z)}"
            )
        )
        file_name = f"{sector_name}.glb"
        expected_sector_files.add(file_name)
        sector_primitives, sector_triangles = build_primitives(
            partition.faces,
            glb_positions,
        )
        write_glb(
            sectors_dir / file_name,
            sector_primitives,
            node_name=sector_name,
        )
        source_indices = np.unique(
            np.concatenate(
                [
                    chunk
                    for material_chunks in partition.faces.values()
                    for chunk in material_chunks
                ]
            )
        )
        source_bounds = source_positions[source_indices]
        sector_manifest.append(
            {
                "name": sector_name,
                "glb": (
                    f"maps/{game}/{map_id}/sectors/{file_name}"
                ),
                "gridX": partition.grid_x,
                "gridZ": partition.grid_z,
                "clusterIndices": list(partition.cluster_indices),
                "alwaysVisible": partition.always_visible,
                "triangles": sector_triangles,
                "boundsCodUnits": {
                    "minimum": source_bounds.min(axis=0).tolist(),
                    "maximum": source_bounds.max(axis=0).tolist(),
                },
            }
        )
        sector_triangle_total += sector_triangles
        if partition.always_visible:
            always_visible_triangles += sector_triangles
    for stale in sectors_dir.glob("*.glb"):
        if stale.name not in expected_sector_files:
            stale.unlink()
    if sector_triangle_total != triangle_count:
        raise ValueError(
            "optimized render sectors disagree with fallback world: "
            f"{sector_triangle_total} != {triangle_count} triangles"
        )

    plane_blob = b"".join(
        struct.pack("<4f", *plane)
        for plane in visibility.planes
    )
    node_blob = b"".join(
        struct.pack("<3i", *node)
        for node in visibility.nodes
    )
    leaf_blob = b"".join(
        struct.pack("<2i", cluster, cell)
        for cluster, cell in zip(
            visibility.leaf_clusters,
            visibility.leaf_cells,
        )
    )
    optimization_path = output_dir / "optimization.json"
    write_json(
        optimization_path,
        {
            "format": MAP_OPTIMIZATION_FORMAT,
            "version": MAP_OPTIMIZATION_VERSION,
            "sourceBspVersion": SUPPORTED_VERSION,
            "codUnitToMetre": COD_UNIT_TO_METRE,
            "sectorStrategy": "unity-xz-grid-v1",
            "sectorSizeMetres": RENDER_SECTOR_SIZE_METRES,
            "fallbackWorldGlb":
                f"maps/{game}/{map_id}/world.glb",
            "sectors": sector_manifest,
            "visibility": {
                "clusterCount": visibility.cluster_count,
                "rowBytes": visibility.row_bytes,
                "pvsBase64": base64.b64encode(
                    visibility.pvs
                ).decode("ascii"),
                "planeCount": len(visibility.planes),
                "planesBase64": base64.b64encode(
                    plane_blob
                ).decode("ascii"),
                "nodeCount": len(visibility.nodes),
                "nodesBase64": base64.b64encode(
                    node_blob
                ).decode("ascii"),
                "leafCount": len(visibility.leaf_clusters),
                "leavesBase64": base64.b64encode(
                    leaf_blob
                ).decode("ascii"),
                "cells": [
                    {
                        "minimum": list(bounds[:3]),
                        "maximum": list(bounds[3:]),
                    }
                    for bounds in visibility.cells
                ],
            },
            "counts": {
                "sectors": len(sector_manifest),
                "triangles": sector_triangle_total,
                "alwaysVisibleTriangles": always_visible_triangles,
                "clusters": visibility.cluster_count,
                "cells": len(visibility.cells),
            },
        },
    )

    exploder_dir = output_dir / "exploders"
    expected_exploder_glbs: set[str] = set()
    exploder_triangle_count = 0
    for source_index, asset in sorted(brush_assets.items()):
        relative = str(asset["glb"])
        if not relative:
            continue
        center = asset["_center"]
        local_yup = yup_positions - np.asarray(
            [center[0], center[2], -center[1]],
            dtype=np.float32,
        )
        local_positions = (
            local_yup * np.float32(COD_UNIT_TO_METRE)
        ).astype(np.float32)
        brush_primitives, brush_triangles = build_primitives(
            brush_faces[source_index],
            local_positions,
        )
        if not brush_primitives:
            raise ValueError(
                f"script_exploder brush entity {source_index} declares "
                "render soups but produced no GLB primitives"
            )
        destination = output_dir / "exploders" / (
            f"entity_{source_index}.glb"
        )
        write_glb(
            destination,
            brush_primitives,
            node_name=f"exploder_entity_{source_index}",
        )
        expected_exploder_glbs.add(destination.name)
        exploder_triangle_count += brush_triangles
    if exploder_dir.is_dir():
        for stale in exploder_dir.glob("*.glb"):
            if stale.name not in expected_exploder_glbs:
                stale.unlink()
        if not any(exploder_dir.iterdir()):
            exploder_dir.rmdir()

    # CLASSIC world lightmaps (M3). Decoded pages are written verbatim (raw
    # dLDR bytes; the runtime shader applies the x2 overbright decode). Absent
    # lump / no pages -> "" so the runtime falls back to the M2 Classic grid.
    lightmaps_json_rel = ""
    if lightmap_pages:
        lightmaps_dir = output_dir / "lightmaps"
        lightmaps_dir.mkdir(parents=True, exist_ok=True)
        page_names = [
            f"page_{index}.png" for index in range(len(lightmap_pages))
        ]
        for name, page in zip(page_names, lightmap_pages):
            Image.fromarray(page, "RGB").save(
                lightmaps_dir / name, optimize=True
            )
        expected_pages = set(page_names)
        for stale in lightmaps_dir.glob("page_*.png"):
            if stale.name not in expected_pages:
                stale.unlink()
        write_json(
            lightmaps_dir / "lightmaps.json",
            {
                "format": "FriendsOfDuty.MapLightmaps",
                "version": 1,
                "encoding": "dLDR",
                "decodeMultiplier": LIGHTMAP_DECODE_MULTIPLIER,
                "pageWidth": LIGHTMAP_PAGE_DIM,
                "pageHeight": LIGHTMAP_PAGE_DIM,
                "pageCount": len(lightmap_pages),
                "pages": [
                    f"maps/{game}/{map_id}/lightmaps/{name}"
                    for name in page_names
                ],
            },
        )
        lightmaps_json_rel = f"maps/{game}/{map_id}/lightmaps/lightmaps.json"
    else:
        # A map that previously baked lightmaps but now has none (empty lump 1)
        # must not keep a stale lightmaps/ dir the catalog no longer references.
        stale_lightmaps_dir = output_dir / "lightmaps"
        if stale_lightmaps_dir.is_dir():
            shutil.rmtree(stale_lightmaps_dir)

    minimum = yup_positions.min(axis=0).tolist()
    maximum = yup_positions.max(axis=0).tolist()
    return {
        "vertices": vertex_count,
        "triangles": triangle_count,
        "materials": len(all_render_materials),
        "texturedMaterials": sum(
            item["texture"] is not None for item in material_manifest
        ),
        "alphaMaterials": sum(
            item["alphaCutout"] for item in material_manifest
        ),
        "decalMaterials": sum(
            item["decal"] for item in material_manifest
        ),
        "rebuiltDecalTextures": rebuilt_decals,
        "polygonOffsetMaterials": biased_materials,
        "exploderBrushes": len(brush_assets),
        "exploderBrushTriangles": exploder_triangle_count,
        "clipBrushes": clip_brush_count,
        "clipBrushTriangles": clip_triangle_count,
        "clipGlb": clip_glb_relative,
        "optimization": {
            "format": MAP_OPTIMIZATION_FORMAT,
            "version": MAP_OPTIMIZATION_VERSION,
            "path": f"maps/{game}/{map_id}/optimization.json",
            "sectors": len(sector_manifest),
            "alwaysVisibleTriangles": always_visible_triangles,
        },
        "boundsCodUnits": {
            "minimum": minimum,
            "maximum": maximum,
            "size": [
                maximum[index] - minimum[index] for index in range(3)
            ],
        },
        "sky": sky,
        "lightmapsJson": lightmaps_json_rel,
        "materialManifest": material_manifest,
    }


def find_map_script(
    map_id: str,
    asset_index: dict[str, ArchiveEntry],
) -> ArchiveEntry | None:
    return asset_index.get(f"maps/mp/{map_id}.gsc".casefold())


def ambient_alias_from_script(script: str | None) -> str | None:
    if not script:
        return None
    match = re.search(
        r'ambientPlay\s*\(\s*"([^"]+)"\s*\)',
        script,
        re.IGNORECASE,
    )
    return match.group(1) if match is not None else None


def _alias_columns(text: str) -> dict[str, int]:
    """Column name -> index, from a sound-alias CSV's own key row.

    The two tiers do NOT share a layout: CoD1's iw_sound.csv has 16 columns
    and no LOD pair, UO's generic_mp_sound.csv has 18 with lod_min/lod_max
    inserted after the distance pair. Reading dist/lod/sequence positionally
    therefore cannot serve both, and reading them by name costs one pass.
    """
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].strip() or row[0].lstrip().startswith("#"):
            continue
        return {
            name.strip().casefold(): index
            for index, name in enumerate(row)
            if name.strip()
        }
    return {}


def _alias_field(row: list[str], columns: dict[str, int], name: str) -> str:
    index = columns.get(name)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


#: `loadspec` values that name a LOAD SET rather than a map. A row carrying
#: one of these applies to everything in that set, so it must not be filtered
#: out the way a map-specific row is.
#:
#: Only `all_mp` is listed. It is the set every map this exporter ships
#: belongs to. `game_main` and `game_uo` are deliberately absent: they are
#: campaign-wide sets, and admitting them would let single-player tuning
#: reach multiplayer output -- the same class of mistake, in the other
#: direction, as the bug this constant fixes.
GENERAL_LOADSPECS = frozenset({"all_mp"})


def resolve_sound_alias(
    alias: str,
    asset_index: dict[str, ArchiveEntry],
) -> dict[str, object] | None:
    """The NEAR bank of a retail sound alias, with its authored envelope.

    Retail splits a loud weapon across distance banks: UO authors
    weap_mg42_loop twice, sequence 1 over LOD 0..1800 units and sequence 2
    over 1800..8500. This used to keep reassigning `resolved` and return
    whatever matched last, which on a UO map is the FAR bank — mp_arnhem's
    emplacements shipped mg42_cooldown_distant.wav as the sound a gunner
    hears with his hands on the gun. Candidates are now collected and the
    nearest bank chosen (lowest lod_min, then lowest sequence).

    Rows whose loadspec names a specific map without a leading '!' are
    skipped: `pavlov` means "only on Pavlov", while `! Pavlov` means
    "everywhere except", and taking the former as a general row is how the
    CoD1 maps ended up on Pavlov's own tuning.

    But `loadspec` mixes two vocabularies, and treating them alike silently
    dropped content. Besides map names it carries LOAD SETS -- `all_mp`,
    `game_main`, `game_uo`, `credits`, `menu` -- which are not maps at all.
    `all_mp` means "every multiplayer map", i.e. exactly what this exporter
    produces, so skipping it discarded every CoD1 ambient bed: the five
    `ambient_mp_*` aliases the shipping map scripts call via ambientPlay()
    all carry `loadspec=all_mp`, and mp_carentan, mp_chateau, mp_pavlov,
    mp_railyard and mp_rocket therefore exported with no ambient audio.
    (mp_arnhem and mp_cassino survived only because UO re-authors those rows
    with an empty loadspec.)
    """
    candidates: list[tuple[float, float, dict[str, object]]] = []
    for key, entry in asset_index.items():
        if not key.startswith("soundaliases/") or not key.endswith(".csv"):
            continue
        text = read_entry(entry).decode("latin1", "replace")
        columns = _alias_columns(text)
        for row in csv.reader(io.StringIO(text)):
            if not row or row[0].strip().casefold() != alias.casefold():
                continue
            loadspec = _alias_field(row, columns, "loadspec")
            if (
                loadspec
                and not loadspec.startswith("!")
                and loadspec.casefold() not in GENERAL_LOADSPECS
            ):
                continue
            sound_name = row[2].strip().replace("\\", "/")
            if not sound_name:
                continue
            clip = asset_index.get(
                f"sound/{sound_name}".casefold()
            )
            if clip is None:
                continue
            lod_min = number(_alias_field(row, columns, "lod_min"), 0.0)
            sequence = number(row[1].strip() if len(row) > 1 else None, 0.0)
            candidates.append(
                (
                    lod_min,
                    sequence,
                    {
                        "alias": alias,
                        "soundName": sound_name,
                        "volume": number(
                            row[3].strip() if len(row) > 3 else None,
                            1.0,
                        ),
                        "loop": any(
                            field.strip().casefold() == "looping"
                            for field in row
                        ),
                        # The authored audible envelope, in CoD units. The
                        # runtime used to hardcode 45 m, which is not a
                        # retail number at all — it is the UO LOD crossover
                        # (1800 units), i.e. where retail SWITCHES bank, not
                        # where the gun stops being heard.
                        "distanceMinInches": number(
                            _alias_field(row, columns, "dist_min"),
                            120.0,
                        ),
                        "distanceMaxInches": number(
                            _alias_field(row, columns, "dist_max"),
                            0.0,
                        ),
                        "source": f"{entry.archive.name}:{entry.name}",
                        "clipSource": f"{clip.archive.name}:{clip.name}",
                        "_clip": clip,
                    },
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    near = candidates[0][2]
    # Every other authored bank travels with the near one. The runtime plays
    # only `near` today and attenuates it with its own falloff, so shipping
    # the rest changes nothing now -- but the far clip is genuinely different
    # audio, not a quieter copy (mg42_cooldown_distant.wav has its own tail),
    # and it cannot be recovered later without the player's Call of Duty
    # install. Extracting it now is what lets distance banks be implemented
    # from a package built today.
    # One entry per distinct clip, nearest first. Several CSVs re-author the
    # same alias (generic_mp_sound, gmi_credits, wep_gmi_sound all carry the
    # MG42 rows), so without this the list repeats the same file three times.
    banks: list[dict[str, object]] = []
    seen: set[str] = set()
    for lod_min, sequence, cue in candidates:
        if cue["soundName"] in seen:
            continue
        seen.add(cue["soundName"])
        banks.append(
            {
                "soundName": cue["soundName"],
                "lodMinInches": lod_min,
                "sequence": sequence,
                "volume": cue["volume"],
                "distanceMinInches": cue["distanceMinInches"],
                "distanceMaxInches": cue["distanceMaxInches"],
                "clipSource": cue["clipSource"],
                "_clip": cue["_clip"],
            }
        )
    near["banks"] = banks
    return near


def extract_audio_cue(
    cue: dict[str, object],
    destination_dir: Path,
    asset_path_prefix: str,
) -> dict[str, object]:
    def write(entry: ArchiveEntry) -> str:
        filename = PurePosixPath(entry.name).name
        destination = destination_dir / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        if (
            not destination.is_file()
            or destination.stat().st_size != entry.size
        ):
            destination.write_bytes(read_entry(entry))
        return f"{asset_path_prefix}/{filename}"

    clip = cue.pop("_clip")
    assert isinstance(clip, ArchiveEntry)
    cue["assetPath"] = write(clip)
    # Every authored distance bank is written, not just the one the runtime
    # currently plays. `assetPath` still names the near bank, so the mount
    # contract is unchanged and an existing reader sees exactly what it saw
    # before; `banks` is additive, and JsonUtility ignores fields it does not
    # know. Extracting them now is the only chance: a player's Call of Duty
    # install is not available again later.
    banks = cue.get("banks")
    if isinstance(banks, list):
        for bank in banks:
            entry = bank.pop("_clip", None)
            if isinstance(entry, ArchiveEntry):
                bank["assetPath"] = write(entry)
    return cue


def map_audio_manifest(
    map_id: str,
    script: str | None,
    entities: list[dict[str, str]],
    asset_index: dict[str, ArchiveEntry],
    destination_dir: Path,
    asset_path_prefix: str,
) -> dict[str, object]:
    background = None
    ambient_alias = ambient_alias_from_script(script)
    if ambient_alias:
        resolved = resolve_sound_alias(ambient_alias, asset_index)
        if resolved is not None:
            background = extract_audio_cue(
                resolved,
                destination_dir,
                asset_path_prefix,
            )

    emitters = []
    sound_keys = {
        "noise",
        "sound",
        "soundalias",
        "loopsound",
    }
    for source_index, entity in enumerate(entities):
        if not entity.get("origin"):
            continue
        for key, value in entity.items():
            if key.casefold() not in sound_keys or not value:
                continue
            resolved = resolve_sound_alias(value, asset_index)
            if resolved is None:
                continue
            cue = extract_audio_cue(
                resolved,
                destination_dir,
                asset_path_prefix,
            )
            cue["sourceEntityIndex"] = source_index
            cue["origin"] = vector(entity.get("origin"))
            cue["sourceKey"] = key
            emitters.append(cue)

    return {
        "background": background,
        "emitters": emitters,
    }


def parse_fog(script: str | None) -> dict[str, object] | None:
    if not script:
        return None
    match = re.search(
        r"setCullFog\s*\(\s*"
        r"([-+.\d]+)\s*,\s*([-+.\d]+)\s*,\s*"
        r"([-+.\d]+)\s*,\s*([-+.\d]+)\s*,\s*"
        r"([-+.\d]+)\s*,\s*([-+.\d]+)\s*\)",
        script,
        re.IGNORECASE,
    )
    if match is None:
        return None
    values = [float(value) for value in match.groups()]
    return {
        "startCodUnits": values[0],
        "endCodUnits": values[1],
        "startMetres": values[0] * COD_UNIT_TO_METRE,
        "endMetres": values[1] * COD_UNIT_TO_METRE,
        "color": values[2:5],
        "transitionSeconds": values[5],
        "sourceCall": match.group(0),
    }


def mounted_weapon_fields(
    entity: dict[str, str],
    asset_index: dict[str, ArchiveEntry],
    destination_dir: Path,
    asset_path_prefix: str,
) -> dict[str, object]:
    weapon_info = entity.get("weaponinfo", "").strip()
    entry = asset_index.get(
        f"weapons/mp/{weapon_info}".casefold()
    )
    if not weapon_info or entry is None:
        return {"weaponInfo": weapon_info}

    values = parse_backslash_values(read_entry(entry))
    result: dict[str, object] = {
        "weaponInfo": weapon_info,
        "turretDamage": number(values.get("damage"), 60.0),
        "turretFireTime": number(values.get("fireTime"), 0.05),
        "turretAccuracy": number(values.get("accuracy"), 0.5),
        "turretLeftArc": number(values.get("leftArc"), 45.0),
        "turretRightArc": number(values.get("rightArc"), 45.0),
        "turretTopArc": number(values.get("topArc"), 40.0),
        "turretBottomArc": number(values.get("bottomArc"), 40.0),
        "turretHorizontalTurnSpeed": number(
            values.get("horTurnSpeed"), 40.0
        ),
        "turretVerticalTurnSpeed": number(
            values.get("vertTurnSpeed"), 40.0
        ),
        "turretConvergenceTime": number(
            values.get("convergenceTime"), 1.5
        ),
        "turretPlayerDistance": number(
            values.get("playerPositionDist"), 46.0
        ),
        # UO barrel heat. Normalized 0-1: fireHeat is added per round and
        # cooldownRate bleeds off, so 1/fireHeat is rounds-to-overheat
        # (UO's own maps/mp/_turret_gmi.gsc draws the gauge as
        # (1.0 - getturretheat()), which is what pins the normalisation).
        #
        # Defaulting to ZERO is load-bearing, not defensive: base CoD 1 has
        # no barrel heat at ALL — not one weapons/mp/* file in any Main
        # pak carries a fireHeat key — and the emplacement records are
        # resolved through each map's own tier. A CoD1 map therefore
        # imports 0 here and must never get an overheat lock retail never
        # had, while mp_arnhem picks up UO's authored 0.02 / 0.01.
        "turretFireHeat": number(values.get("fireHeat"), 0.0),
        "turretCooldownRate": number(values.get("cooldownRate"), 0.0),
        # Retail narrows the view while manning the gun (UO authors 55
        # against the 80-degree hip FOV). Zero means "no authored mounted
        # FOV", which keeps the free-look value.
        "turretFov": number(values.get("turret_fov"), 0.0),
        "turretLoopFireSound": values.get("loopFireSound", ""),
        "turretStopFireSound": values.get("stopFireSound", ""),
        "turretViewFlashEffect": values.get("viewFlashEffect", ""),
        "turretWorldFlashEffect": values.get("worldFlashEffect", ""),
        "turretReticle": values.get("reticleCenter", ""),
        # Retail draws the reticle at reticleCenterSize on the 640x480
        # virtual canvas. The name was carried without the size, which is
        # half a contract.
        "turretReticleSize": number(values.get("reticleCenterSize"), 32.0),
        "turretIdleAnimation": values.get("idleAnim", ""),
        "turretFireAnimation": values.get("fireAnim", ""),
        "useHintString": values.get("useHintString", ""),
    }
    for alias_key, path_key, envelope_prefix in (
        ("turretLoopFireSound", "turretLoopFireAudioPath",
         "turretLoopFire"),
        ("turretStopFireSound", "turretStopFireAudioPath",
         "turretStopFire"),
    ):
        alias = str(result[alias_key])
        resolved = (
            resolve_sound_alias(alias, asset_index)
            if alias
            else None
        )
        if resolved is None:
            result[path_key] = ""
            result[f"{envelope_prefix}DistanceMinInches"] = 0.0
            result[f"{envelope_prefix}DistanceMaxInches"] = 0.0
            continue
        cue = extract_audio_cue(
            resolved,
            destination_dir,
            asset_path_prefix,
        )
        result[path_key] = cue.get("assetPath", "")
        # Carried so the runtime plays the gun over the distance retail
        # authored for it rather than a constant. Already numeric here —
        # resolve_sound_alias ran them through number() off the CSV.
        result[f"{envelope_prefix}DistanceMinInches"] = float(
            resolved.get("distanceMinInches") or 0.0
        )
        result[f"{envelope_prefix}DistanceMaxInches"] = float(
            resolved.get("distanceMaxInches") or 0.0
        )
    return result


def gameplay_entity_manifest(
    entities: list[dict[str, str]],
    models: list[dict[str, object]],
    asset_index: dict[str, ArchiveEntry],
    destination_dir: Path,
    asset_path_prefix: str,
    exploder_brushes: dict[int, dict[str, object]] | None = None,
    bsp: bytes | None = None,
) -> list[dict[str, object]]:
    weapon_ids = runtime_weapon_ids(asset_index)
    # Brush-side/plane lumps back the per-brush hull export
    # (entity_brush_volumes); without the bsp the manifest still builds,
    # minus the hulls, and the runtime falls back to the AABB.
    brushes: list[tuple[int, int]] = []
    sides: list[tuple[int, int]] = []
    brush_first_side: list[int] = []
    planes = np.zeros((0, 4), dtype="<f4")
    if bsp is not None:
        side_data = lump(bsp, BRUSH_SIDE_LUMP_INDEX)
        brush_data = lump(bsp, BRUSH_LUMP_INDEX)
        plane_data = lump(bsp, PLANE_LUMP_INDEX)
        if (
            len(side_data) % BRUSH_SIDE_LUMP_STRIDE
            or len(brush_data) % BRUSH_LUMP_STRIDE
            or len(plane_data) % PLANE_LUMP_STRIDE
        ):
            raise ValueError(
                "brush, brush-side, or plane lump stride mismatch"
            )
        planes = np.frombuffer(plane_data, dtype="<f4").reshape(-1, 4)
        sides = [
            struct.unpack_from("<II", side_data, offset)
            for offset in range(
                0,
                len(side_data),
                BRUSH_SIDE_LUMP_STRIDE,
            )
        ]
        brushes = [
            struct.unpack_from("<HH", brush_data, offset)
            for offset in range(
                0,
                len(brush_data),
                BRUSH_LUMP_STRIDE,
            )
        ]
        side_offset = 0
        for side_count, _material in brushes:
            brush_first_side.append(side_offset)
            side_offset += side_count
    exploder_brushes = exploder_brushes or {}
    # Stock map scripts can drive a script_exploder toward ordinary target
    # marker entities (and, in turn, through another target hop).  Those
    # entities do not have an otherwise gameplay-specific classname, so retain
    # the exact transitive target graph rooted at every exploder.
    entities_by_target_name: dict[str, list[int]] = {}
    target_frontier: list[str] = []
    for source_index, entity in enumerate(entities):
        target_name = entity.get("targetname", "").strip().casefold()
        if target_name:
            entities_by_target_name.setdefault(
                target_name,
                [],
            ).append(source_index)
        if entity.get("script_exploder"):
            target = entity.get("target", "").strip().casefold()
            if target:
                target_frontier.append(target)
    exploder_target_closure: set[int] = set()
    visited_targets: set[str] = set()
    while target_frontier:
        target_name = target_frontier.pop()
        if target_name in visited_targets:
            continue
        visited_targets.add(target_name)
        for source_index in entities_by_target_name.get(target_name, []):
            exploder_target_closure.add(source_index)
            next_target = entities[source_index].get(
                "target",
                "",
            ).strip().casefold()
            if next_target and next_target not in visited_targets:
                target_frontier.append(next_target)

    gameplay = []
    for source_index, entity in enumerate(entities):
        class_name = entity.get("classname", "")
        normalized = class_name.casefold()
        # The exploder graph outranks the objective filter: mp_pavlov's
        # destroyable weapon_antitankrifle set pieces are script_models the
        # mapper named "bombzone", and they are Deathmatch scenery.
        exploder_member = (
            bool(entity.get("script_exploder"))
            or source_index in exploder_target_closure
        )
        relevant = exploder_member or (
            not is_objective_entity(entity)
            and (
                normalized.startswith("trigger_")
                or normalized.startswith("mpweapon_")
                or normalized == "misc_mg42"
            )
        )
        if not relevant:
            continue

        bounds = brush_model_bounds(entity, models)
        volume_planes, volume_plane_counts = entity_brush_volume_planes(
            entity,
            models,
            brushes,
            brush_first_side,
            sides,
            planes,
        )
        record: dict[str, object] = {
            "sourceEntityIndex": source_index,
            "className": class_name,
            "origin": vector(entity.get("origin")),
            "angles": vector(entity.get("angles")),
            "model": entity.get("model", ""),
            "hasBrushBounds": bounds is not None,
            "boundsCenter": bounds[0] if bounds else [0.0, 0.0, 0.0],
            "boundsSize": bounds[1] if bounds else [0.0, 0.0, 0.0],
            "brushVolumePlanes": volume_planes,
            "brushVolumePlaneCounts": volume_plane_counts,
            "targetName": entity.get("targetname", ""),
            "target": entity.get("target", ""),
            "scriptGameObjectName": entity.get(
                "script_gameobjectname", ""
            ),
            "scriptNoteworthy": entity.get(
                "script_noteworthy", ""
            ),
            "spawnFlags": int(number(entity.get("spawnflags"), 0.0)),
            "damage": number(
                entity.get("dmg") or entity.get("damage"),
                0.0,
            ),
            "wait": number(entity.get("wait"), 0.0),
            "weaponId": weapon_ids.get(normalized, ""),
            "scriptExploderId": _positive_script_exploder_id(
                entity,
                source_index,
            ),
            "scriptFxId": entity.get("script_fxid", "").casefold(),
            "scriptDelay": number(entity.get("script_delay"), 0.0),
            "hasScriptDelayMin": "script_delay_min" in entity,
            "scriptDelayMin": number(
                entity.get("script_delay_min"),
                0.0,
            ),
            "hasScriptDelayMax": "script_delay_max" in entity,
            "scriptDelayMax": number(
                entity.get("script_delay_max"),
                0.0,
            ),
            "scriptFxStart": entity.get("script_fxstart", ""),
            "scriptFxStop": entity.get("script_fxstop", ""),
            "scriptFxCommand": entity.get("script_fxcommand", ""),
            "exploderBrushGlb": str(
                exploder_brushes.get(
                    source_index,
                    {},
                ).get("glb", "")
            ),
            "exploderBrushMaterials": list(
                exploder_brushes.get(
                    source_index,
                    {},
                ).get("materials", [])
            ),
        }
        if normalized == "misc_mg42":
            record.update(
                mounted_weapon_fields(
                    entity,
                    asset_index,
                    destination_dir,
                    asset_path_prefix,
                )
            )
        gameplay.append(record)
    return gameplay


def entity_manifest(
    game: str,
    map_id: str,
    source: ArchiveEntry,
    bsp: bytes,
    entities: list[dict[str, str]],
    script: str | None,
    asset_index: dict[str, ArchiveEntry],
    audio_destination_dir: Path,
    audio_asset_path_prefix: str,
) -> dict[str, object]:
    models = parse_brush_models(bsp)
    game_types = supported_game_types(
        map_id,
        entities,
        asset_index,
    )
    exploder_effects = map_exploder_effects(
        asset_index,
        map_id,
        entities,
        script,
    )
    exploder_brushes = exploder_brush_assets(
        bsp,
        entities,
        models,
        game,
        map_id,
    )
    gameplay_entities = gameplay_entity_manifest(
        entities,
        models,
        asset_index,
        audio_destination_dir,
        audio_asset_path_prefix,
        exploder_brushes,
        bsp,
    )
    placements = []
    lights = []
    spawns = []
    for source_index, entity in enumerate(entities):
        class_name = entity.get("classname", "")
        model_path = entity.get("model", "")
        if (
            class_name.casefold() in SCENE_PROP_CLASSES
            and model_path.startswith("xmodel/")
            and model_path != "xmodel/fx"
            # The objective props of a cut mode are not set dressing: retail
            # deletes them outright in every mode that does not own them, so
            # drawing a CTF flag or an HQ radio in Deathmatch was never the
            # authored picture. Exploder members outrank the test, because
            # mp_pavlov's destroyable set pieces are named "bombzone".
            and not (
                is_objective_entity(entity)
                and not entity.get("script_exploder")
            )
        ):
            scalar_scale, scale_vector = model_scale(entity)
            placements.append(
                {
                    "sourceEntityIndex": source_index,
                    "className": class_name,
                    "model": model_path.removeprefix("xmodel/"),
                    "sourceModel": model_path,
                    "origin": vector(entity.get("origin")),
                    "angles": vector(entity.get("angles")),
                    "scale": scalar_scale,
                    "scaleVector": scale_vector,
                    "lighting": color(entity.get("lightingPrecalc")),
                }
            )
        if class_name == "light":
            lights.append(
                {
                    "sourceEntityIndex": source_index,
                    "origin": vector(entity.get("origin")),
                    "angles": vector(entity.get("angles")),
                    "color": color(entity.get("_color")),
                    "intensity": number(entity.get("light"), 300.0),
                    "overbrightShift": number(
                        entity.get("overbrightShift"), 1.0
                    ),
                }
            )
        if "spawn" in class_name.casefold() and entity.get("origin"):
            spawns.append(
                {
                    "sourceEntityIndex": source_index,
                    "className": class_name,
                    "origin": vector(entity.get("origin")),
                    "angles": vector(entity.get("angles")),
                    "targetname": entity.get("targetname"),
                }
            )
    model_names = sorted(
        {placement["model"] for placement in placements},
        key=str.casefold,
    )
    if "dm" in game_types or "tdm" in game_types:
        model_names.append(HEALTH_PICKUP_MODEL)
    model_names.extend(
        model.casefold()
        for effect in exploder_effects
        for model in effect.model_names
    )
    model_names = sorted(set(model_names), key=str.casefold)
    return {
        "format": "FriendsOfDuty.OriginalMultiplayerMapEntities",
        "version": 2,
        "game": game,
        "mapId": map_id,
        "source": f"{source.archive.name}:{source.name}",
        "supportedGameTypes": game_types,
        "worldspawn": entities[0],
        "authoredSun": parse_authored_sun(bsp, entities[0]),
        "fog": parse_fog(script),
        "modelAssets": model_names,
        "placements": placements,
        "lights": lights,
        "spawns": spawns,
        "gameplayEntities": gameplay_entities,
        "exploderEffects": [
            exploder_effect_record(effect)
            for effect in exploder_effects
        ],
        "entities": entities,
        "counts": {
            "sourceEntities": len(entities),
            "placements": len(placements),
            "uniqueModels": len(model_names),
            "lights": len(lights),
            "spawns": len(spawns),
            "gameplayEntities": len(gameplay_entities),
            "scriptExploderEntities": sum(
                bool(entity.get("script_exploder"))
                for entity in entities
            ),
            "exploderEffects": len(exploder_effects),
            "exploderBrushes": len(exploder_brushes),
        },
    }


def title_for(map_id: str) -> str:
    special = {
        "mp_carentan": "Carentan",
        "mp_pavlov": "Pavlov",
        "mp_railyard": "Railyard",
        "mp_rocket": "Rocket",
    }
    return special.get(
        map_id,
        map_id.removeprefix("mp_").replace("_", " ").title(),
    )


def source_fingerprint(
    entry: ArchiveEntry,
    bsp: bytes,
    script: str | None,
) -> str:
    digest = hashlib.sha256()
    # v20: materials.json entries gain an additive "surface" field — the
    # impact-vocabulary surfaceparm token the material's shader stanza
    # declares (grass, brick, metal, ...), the key retail's fx/iw_impacts.csv
    # maps bullet impact effects on. Unchanged BSPs must regenerate
    # materials.json to carry it; the field is absent when no stanza
    # declares one, so nothing else changes.
    # v19: clip.glb ships one primitive PER BRUSH instead of per clip
    # material, so the runtime can cook each playerclip brush as a CONVEX
    # collider. The merged triangle-soup groups let the capsule controller
    # wedge INSIDE a car's clip hull (a soup has no interior); convex
    # volumes cannot be entered and their authored bevel sides restore the
    # original engine's smooth brush-to-brush sliding.
    # v18: gameplay-entity records carry the exact per-brush half-space
    # hulls (brushVolumePlanes/brushVolumePlaneCounts) behind their union
    # AABB. The AABB alone made non-axial minefield trigger brushes lethal
    # metres past their authored diagonal boundary (mp_pavlov: 1,484 m² of
    # falsely lethal ground, warning signs standing inside the box);
    # unchanged BSPs must regenerate entities.json to ship the hulls.
    # v17: world materials resolve shader-script image indirection
    # (resolve_texture follows the stanza's `map` when every filename
    # candidate misses — Carentan's plain-green a_grass1b/dirt_earthbase
    # blend layers gain their real diffuse) and alphaCutout is additionally
    # derived from texture-alpha `alphaFunc GE128` stanzas; unchanged BSPs
    # must regenerate materials.json and the newly referenced shared PNGs.
    # v16: extracts CoD1 BSP lump-1 surface lightmaps — per-(material,page) GLB
    # primitives carrying a TEXCOORD_1 lightmap UV + extras.lightmapIndex, plus
    # maps/{game}/{map_id}/lightmaps/page_N.png + lightmaps.json and the catalog
    # lightmapsJson field (M3, CLASSIC static world lighting). The uv2 lives in
    # world.glb/sectors, so this must bump the key or v15 paks keep their
    # lightmap-less geometry on an unchanged-fingerprint rerun. v15: exports the
    # compiled BSP directional light as the exact authored
    # sun anchor. v14 emitted offline 32 m Unity-XZ sectors with exact
    # per-triangle BSP
    # cluster unions; ambiguous/all-solid and sky triangles fail open into an
    # always-visible sector. v13 introduced the validated v59 node/leaf/PVS
    # payload and coarse graphics-cell sectors. v12: script_exploder members
    # leave the immutable
    # world mesh and receive
    # per-entity brush GLBs plus exact FX/content descriptors; DM/TDM maps
    # advertise their script-spawned health_medium dependency. v11 preserved
    # collision-only common/ladder brush bounds in the runtime
    # material manifest. v10 bound the official MP map GSC as well as the BSP.
    # HQ radios are
    # script-spawned on most stock maps, so a BSP-only cache key could retain
    # a stale gameplay-entity manifest after their declarations change.
    # Gameplay entities also carry brush-model bounds, official weapon/turret
    # metadata and arena gametype support for runtime materialization.
    # (belgium/normandy/german) instead of only textures/decals/*, the
    # fod_decal_alpha rebuild is scoped to the black-key crater family it was
    # tuned for, and decal entries carry their shader tint in fallbackColor.
    digest.update(
        f"FriendsOfDuty-MP-map-pipeline-v{MAP_PIPELINE_VERSION}".encode()
    )
    digest.update(entry.archive.name.encode("utf-8"))
    digest.update(entry.name.encode("utf-8"))
    digest.update(bsp)
    digest.update(b"\0MP-GSC\0")
    digest.update((script or "").encode("latin1", "replace"))
    return digest.hexdigest()


def preferred_spawn(spawns: list[dict[str, object]]) -> dict[str, object] | None:
    return next(
        (
            spawn
            for spawn in spawns
            if spawn["className"] == "mp_teamdeathmatch_spawn"
        ),
        spawns[0] if spawns else None,
    )


def map_environment(
    worldspawn: dict[str, str],
    fog: dict[str, object] | None,
    authored_sun: dict[str, object],
) -> dict[str, object]:
    return {
        "sunDirection": vector(
            worldspawn.get("sundirection"),
            (-30.0, 40.0, 0.0),
        ),
        "sunColor": color(
            worldspawn.get("suncolor")
            or worldspawn.get("_color")
        ),
        "sunlight": number(
            worldspawn.get("sunlight"), 1.0
        ),
        "ambient": number(
            worldspawn.get("ambient"), 0.18
        ),
        "ambientColor": color(worldspawn.get("_color")),
        "authoredSun": authored_sun,
        "fog": fog,
    }


def map_catalog_entry(
    game: str,
    map_id: str,
    source: ArchiveEntry,
    manifest: dict[str, object],
    world: dict[str, object] | None,
    unity_source_root: str,
    fingerprint: str,
    existing_pavlov: bool,
    audio: dict[str, object],
) -> dict[str, object]:
    spawns = manifest["spawns"]
    preferred = preferred_spawn(spawns)
    worldspawn = manifest["worldspawn"]
    fog = manifest["fog"]
    return {
        "game": game,
        "mapId": map_id,
        "title": title_for(map_id),
        "source": f"{source.archive.name}:{source.name}",
        "sourceFingerprint": fingerprint,
        "supportedGameTypes": manifest["supportedGameTypes"],
        "existingPavlov": existing_pavlov,
        "modelAssetPath": (
            "Assets/Maps/Pavlov/Source/mp_pavlov.obj"
            if existing_pavlov
            else f"{unity_source_root}/{map_id}.obj"
        ),
        "entityAssetPath": (
            "Assets/Resources/PavlovOriginalEntities.json"
            if existing_pavlov
            else f"{unity_source_root}/{map_id}_entities.json"
        ),
        "skyPanoramaAssetPath": (
            "Assets/Maps/Pavlov/Source/textures/sky/"
            "pavlov_equirect.png"
            if existing_pavlov
            else (
                f"{unity_source_root}/"
                f"{world['sky']['panorama']}"
                if world is not None and world.get("sky")
                else None
            )
        ),
        "sceneAssetPath": (
            "Assets/Scenes/PavlovFpsDemo.unity"
            if existing_pavlov
            else f"Assets/Scenes/Multiplayer/{game}/{map_id}.unity"
        ),
        "recommendedSpawn": preferred,
        "spawns": spawns,
        "audio": audio,
        "environment": map_environment(
            worldspawn,
            fog,
            manifest["authoredSun"],
        ),
        "world": (
            {
                key: value
                for key, value in world.items()
                if key != "materialManifest"
            }
            if world is not None
            else None
        ),
        "counts": manifest["counts"],
    }


LIGHT_GRID_MAGIC = b"FODLGRD1"
LIGHT_GRID_VERSION = 1
# 64 CoD units = 1.6256 m per cell, uniform. Doubled and rebaked if a map's
# padded grid would exceed the cell cap.
LIGHT_GRID_CELL_COD = 64.0
LIGHT_GRID_MAX_CELLS = 262_144
LIGHT_GRID_FLAG_SUN_INCLUDED = 1
# ReadColor's fallback (RuntimeMapBuilder.cs) for a lamp entity with no _color.
# color() already defaults an uncoloured lamp to white before it reaches here,
# so this only guards a genuinely absent/malformed colour field.
LIGHT_GRID_FALLBACK_COLOR = (1.0, 1.0, 0.824)
# CLASSIC (M2) sun-match scalar: baked sun radiance = sunColor *
# clamp(sunlight,.15,2.5) * this. In Classic the realtime sun is masked off the
# viewmodel, so the baked grid is the weapon's ONLY sun and this scalar sets how
# bright that baked sun reads (Open Question #1 — an in-engine pass may retune).
LIGHT_GRID_CLASSIC_SUN_SCALE = 1.0
# RuntimeMapBuilder.CreateEnvironment clamps for the Classic sun + ambient, so
# the baked terms match the authored world sun and RenderSettings.ambientLight.
LIGHT_GRID_SUN_INTENSITY_RANGE = (0.15, 2.5)
LIGHT_GRID_AMBIENT_RANGE = (0.03, 0.65)
# FodMaterialFactory.ReadColor fallbacks for a null sun / ambient colour.
LIGHT_GRID_SUN_FALLBACK_COLOR = (1.0, 0.97, 0.9)
LIGHT_GRID_AMBIENT_FALLBACK_COLOR = (1.0, 1.0, 1.0)


def _light_grid_raw_bounds(
    bounds_cod: dict[str, object] | None,
) -> tuple[
    tuple[float, float, float], tuple[float, float, float]
] | None:
    """world['boundsCodUnits'] is emitted in the Y-up (x, z, -y) frame, but the
    lamp origins and RuntimeMapBuilder.ConvertPosition both work in the raw
    z-up (x, y, z) BSP frame the grid must be authored in. Convert the Y-up AABB
    back to raw z-up (an axis permutation plus one negation — exact for a box)
    so the bake, the lamps, and the runtime eye->CoD inverse all agree."""
    if not isinstance(bounds_cod, dict):
        return None
    yup_min = bounds_cod.get("minimum")
    yup_max = bounds_cod.get("maximum")
    if (
        not isinstance(yup_min, (list, tuple))
        or not isinstance(yup_max, (list, tuple))
        or len(yup_min) < 3
        or len(yup_max) < 3
    ):
        return None
    try:
        xmin, ymin, zmin = (float(yup_min[i]) for i in range(3))
        xmax, ymax, zmax = (float(yup_max[i]) for i in range(3))
    except (TypeError, ValueError):
        return None
    # Reject non-finite bounds (e.g. a corrupted source.json on the unchanged
    # path): a NaN would serialize a NaN origin the runtime cannot detect, and
    # an inf would crash the whole export at math.ceil. Degrade to no grid.
    if not all(
        math.isfinite(v)
        for v in (xmin, ymin, zmin, xmax, ymax, zmax)
    ):
        return None
    # raw_x = X, raw_y = -Z, raw_z = Y  (the negation flips min/max on that axis)
    raw_min = (min(xmin, xmax), -max(zmin, zmax), min(ymin, ymax))
    raw_max = (max(xmin, xmax), -min(zmin, zmax), max(ymin, ymax))
    return raw_min, raw_max


def resolve_runtime_lamps(
    lights: list[dict[str, object]],
    map_id: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Project the authored lamp entities to the exact runtime ForceVertex lamp
    parameters (RuntimeMapBuilder.cs): intensity_u = clamp(I/50 * 2^(shift-.8),
    .3, 9) with I floored to 1, range_m = clamp(sqrt(I)*.9, 3, cap) where the
    cap is 22 m on mp_pavlov and 24 m everywhere else, colour clamped to [0,1]
    with ReadColor's fallback, lamps without a usable origin dropped. One
    definition shared by every analytic bake (M1/M2 viewmodel grids and the
    stage-2 lamps-only world lightmap) so a lamp-constant change in the runtime
    updates all Python replicas in lockstep (LAMP_LIGHTING_REWORK_SPEC.md (E)).
    Returns (positions (L,3) raw z-up CoD units, intensities (L,),
    ranges_m (L,), colors (L,3)) float32 arrays; L may be zero."""
    range_cap = 22.0 if map_id.casefold() == "mp_pavlov" else 24.0
    positions: list[list[float]] = []
    intensities: list[float] = []
    ranges_m: list[float] = []
    colors: list[list[float]] = []
    for light in lights:
        origin = light.get("origin")
        if not isinstance(origin, (list, tuple)) or len(origin) < 3:
            continue
        # Replicate RuntimeMapBuilder's lamp math exactly.
        source_intensity = max(1.0, float(light.get("intensity", 300.0)))
        overbright = 2.0 ** (float(light.get("overbrightShift", 1.0)) - 0.8)
        intensity_u = min(9.0, max(0.3, source_intensity / 50.0 * overbright))
        range_m = min(range_cap, max(3.0, math.sqrt(source_intensity) * 0.9))
        col = light.get("color")
        if not isinstance(col, (list, tuple)) or len(col) < 3:
            col = LIGHT_GRID_FALLBACK_COLOR
        positions.append(
            [float(origin[0]), float(origin[1]), float(origin[2])]
        )
        intensities.append(intensity_u)
        ranges_m.append(range_m)
        colors.append([min(1.0, max(0.0, float(col[i]))) for i in range(3)])
    return (
        np.asarray(positions, dtype=np.float32).reshape(len(positions), 3),
        np.asarray(intensities, dtype=np.float32),
        np.asarray(ranges_m, dtype=np.float32),
        np.asarray(colors, dtype=np.float32).reshape(len(colors), 3),
    )


def lamp_attenuation(d_m: np.ndarray, range_m: float) -> np.ndarray:
    """The runtime lamps' distance falloff: 1/(1 + 25*(d/R)^2) inside range,
    hard zero at and beyond it, distances in METRES. Shared by the viewmodel
    grids and the lamp-lightmap bake so every baked artifact agrees with the
    realtime ForceVertex lamps by construction."""
    ratio = np.asarray(d_m, dtype=np.float32) / np.float32(range_m)
    return np.where(
        ratio < np.float32(1.0),
        np.float32(1.0)
        / (np.float32(1.0) + np.float32(25.0) * ratio * ratio),
        np.float32(0.0),
    ).astype(np.float32)


def bake_viewmodel_light_grid(
    lights: list[dict[str, object]],
    bounds_cod: dict[str, object] | None,
    map_id: str,
    sun_dir_cod: tuple[float, float, float] | None = None,
    sun_radiance: tuple[float, float, float] | None = None,
    ambient_floor: tuple[float, float, float] | None = None,
    sun_included: bool = False,
) -> tuple[dict[str, object], np.ndarray] | None:
    """Bake a per-cell SH-L1 RGB radiance grid for the first-person viewmodel /
    character fill. Analytic, gamma space, NO occlusion and NO bounce. Each
    lamp's intensity, range and colour replicate the runtime ForceVertex lamps
    exactly (RuntimeMapBuilder.cs) and the falloff matches their vertex
    attenuation; everything is authored in the raw z-up CoD frame the runtime
    eye->CoD inverse reconstructs.

    REIMAGINED (M1): lamps only, ``sun_included=False`` -> header flag 0; the
    realtime dynamic sun lights the weapon directly and the runtime adds live sky
    ambient, so neither is baked. CLASSIC (M2): CoD's real lightgrid lump is
    undocumented, so we synthesise the same lighting analytically from the
    authored sun we already extract accurately per map — pass the sun toward-sun
    direction ``sun_dir_cod`` (unit, raw z-up CoD) and ``sun_radiance`` (gamma,
    sunColor x clamp(sunlight)) plus the static ``ambient_floor`` (gamma), with
    ``sun_included=True`` -> header flag 1. The sun is a constant directional
    SH-L1 term on every cell (global, no occlusion in M2), ambient a flat c0
    floor. In Classic the realtime authored sun is masked off the viewmodel
    layer (PavlovEnvironmentController), so this baked grid is the weapon's sole
    lighting — no double-count.

    Returns (header, body_f32) or None when there is nothing to bake (no lamps,
    no sun, no ambient) or no usable bounds — the caller then writes no file."""
    has_sun = (
        sun_dir_cod is not None
        and sun_radiance is not None
        and any(abs(float(c)) > 0.0 for c in sun_radiance)
    )
    has_ambient = ambient_floor is not None and any(
        abs(float(c)) > 0.0 for c in ambient_floor
    )
    if not lights and not has_sun and not has_ambient:
        return None
    bounds = _light_grid_raw_bounds(bounds_cod)
    if bounds is None:
        return None
    raw_min, raw_max = bounds

    # Lamp parameters come from the shared runtime-contract helper so this
    # grid, the Classic grid and the stage-2 lamp-lightmap bake can never
    # drift apart on the lamp formulas.
    lamp_positions, lamp_intensities, lamp_ranges, lamp_colors = (
        resolve_runtime_lamps(lights, map_id)
    )
    # A sun/ambient-only Classic grid legitimately has no lamps; only bail when
    # there is genuinely nothing to accumulate (mirrors the M1 lamp-only bail).
    if not len(lamp_positions) and not has_sun and not has_ambient:
        return None

    # Grow the cell until the padded grid fits the cap, then place cell (0,0,0)
    # one cell below the min so a full ring of zero-lamp cells surrounds the
    # world on every side (dims +2). x-fastest index i = x + nx*(y + ny*z).
    cell = LIGHT_GRID_CELL_COD
    while True:
        nx = int(math.ceil(max(0.0, raw_max[0] - raw_min[0]) / cell)) + 1 + 2
        ny = int(math.ceil(max(0.0, raw_max[1] - raw_min[1]) / cell)) + 1 + 2
        nz = int(math.ceil(max(0.0, raw_max[2] - raw_min[2]) / cell)) + 1 + 2
        if nx * ny * nz <= LIGHT_GRID_MAX_CELLS:
            break
        cell *= 2.0
    origin_cod = (raw_min[0] - cell, raw_min[1] - cell, raw_min[2] - cell)

    # Cell-center coordinates (raw z-up CoD), shape (nz, ny, nx, 3). meshgrid
    # 'ij' + C-order flatten yields x-fastest with the coeffs contiguous.
    xs = np.float32(origin_cod[0]) + np.arange(nx, dtype=np.float32) * np.float32(cell)
    ys = np.float32(origin_cod[1]) + np.arange(ny, dtype=np.float32) * np.float32(cell)
    zs = np.float32(origin_cod[2]) + np.arange(nz, dtype=np.float32) * np.float32(cell)
    grid_z, grid_y, grid_x = np.meshgrid(zs, ys, xs, indexing="ij")
    centers = np.stack((grid_x, grid_y, grid_z), axis=-1).astype(np.float32)

    coeffs = np.zeros((nz, ny, nx, 12), dtype=np.float32)
    scale = np.float32(COD_UNIT_TO_METRE)
    for index in range(len(lamp_positions)):
        lamp = lamp_positions[index]
        # delta points from each cell toward the lamp (the omega direction).
        delta = lamp - centers  # (nz, ny, nx, 3)
        d_cod = np.sqrt(np.sum(delta * delta, axis=-1))  # (nz, ny, nx)
        d_m = d_cod * scale
        # Shared falloff: 1/(1 + 25*(d/R)^2), hard 0 beyond range — matching
        # the ForceVertex lamp attenuation the runtime shows on the weapon.
        atten = lamp_attenuation(d_m, float(lamp_ranges[index]))
        if not np.any(atten > np.float32(0.0)):
            continue
        col = lamp_colors[index]
        # E = intensity_u * atten * colour  (radiance, per channel).
        E = (lamp_intensities[index] * atten)[..., np.newaxis] * col
        # omega = delta / d (unit, toward lamp); zero at a coincident cell.
        finite = d_cod > np.float32(1e-6)
        safe_d = np.where(finite, d_cod, np.float32(1.0))
        omega = (delta / safe_d[..., np.newaxis]).astype(np.float32)
        omega = np.where(finite[..., np.newaxis], omega, np.float32(0.0))
        # Accumulate radiance SH-L1 in CoD axes: c0 += E; c1 += E * omega.
        coeffs[..., 0:3] += E
        coeffs[..., 3:6] += E * omega[..., 0:1]
        coeffs[..., 6:9] += E * omega[..., 1:2]
        coeffs[..., 9:12] += E * omega[..., 2:3]

    # Constant Classic terms added to every cell: a flat ambient floor in c0 and
    # the authored static sun as a directional SH-L1 term (c0 += sunE, c1 +=
    # sunE * omega_sun). No occlusion in M2 — the sun reaches every cell.
    if has_ambient:
        coeffs[..., 0:3] += np.asarray(ambient_floor, dtype=np.float32)
    if has_sun:
        sun_rad = np.asarray(sun_radiance, dtype=np.float32)
        sun_dir = np.asarray(sun_dir_cod, dtype=np.float32)
        sun_len = float(np.sqrt(np.sum(sun_dir * sun_dir)))
        if sun_len > 1e-6:
            sun_dir = sun_dir / sun_len
            coeffs[..., 0:3] += sun_rad
            coeffs[..., 3:6] += sun_rad * sun_dir[0]
            coeffs[..., 6:9] += sun_rad * sun_dir[1]
            coeffs[..., 9:12] += sun_rad * sun_dir[2]

    header = {
        "magic": LIGHT_GRID_MAGIC,
        "version": LIGHT_GRID_VERSION,
        "flags": LIGHT_GRID_FLAG_SUN_INCLUDED if sun_included else 0,
        "cellCount": nx * ny * nz,
        "origin": origin_cod,
        "cellSize": cell,
        "nx": nx,
        "ny": ny,
        "nz": nz,
    }
    return header, coeffs.astype(np.float32)


def write_light_grid(
    path: Path,
    header: dict[str, object],
    body: np.ndarray,
) -> None:
    """Serialize a baked grid: 48-byte little-endian header + cellCount x 12
    float16, x-fastest, coeff order [c0.rgb, c1x.rgb, c1y.rgb, c1z.rgb]. The
    body's C-contiguous (nz, ny, nx, 12) layout already flattens x-fastest with
    the 12 coefficients contiguous per cell; endianness is pinned via '<f2'."""
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = struct.pack(
        "<8sIIIffffIII",
        header["magic"],
        int(header["version"]),
        int(header["flags"]),
        int(header["cellCount"]),
        float(header["origin"][0]),
        float(header["origin"][1]),
        float(header["origin"][2]),
        float(header["cellSize"]),
        int(header["nx"]),
        int(header["ny"]),
        int(header["nz"]),
    )
    path.write_bytes(packed + body.astype("<f2").tobytes())


def _classic_sun_ambient(
    environment: dict[str, object],
) -> tuple[
    tuple[float, float, float] | None,
    tuple[float, float, float] | None,
    tuple[float, float, float],
]:
    """Derive the Classic sun + ambient terms the authored world applies
    (RuntimeMapBuilder.CreateEnvironment) so the synthesised grid reproduces
    CoD's baked lighting from the sun we already extract accurately per map:
    sun radiance = sunColor x clamp(sunlight, .15, 2.5) x scale toward the sun,
    ambient floor = ambientColor x clamp(ambient, .03, .65). Gamma throughout;
    the same clamps and ReadColor fallbacks as the runtime. Returns
    (sun_dir_cod, sun_radiance, ambient_floor); the sun pair is None when a map
    has no authored sun (ambient floor is still returned)."""

    def _rgb(value: object, fallback: tuple[float, float, float]):
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            value = fallback
        return tuple(min(1.0, max(0.0, float(value[i]))) for i in range(3))

    ambient_scale = min(
        LIGHT_GRID_AMBIENT_RANGE[1],
        max(LIGHT_GRID_AMBIENT_RANGE[0], float(environment.get("ambient", 0.18))),
    )
    ambient_rgb = _rgb(
        environment.get("ambientColor"),
        LIGHT_GRID_AMBIENT_FALLBACK_COLOR,
    )
    ambient_floor = tuple(component * ambient_scale for component in ambient_rgb)

    sun_dir_cod = None
    sun_radiance = None
    authored = environment.get("authoredSun")
    direction_unity = (
        authored.get("directionUnity") if isinstance(authored, dict) else None
    )
    if (
        isinstance(direction_unity, (list, tuple))
        and len(direction_unity) >= 3
    ):
        du = [float(direction_unity[i]) for i in range(3)]
        # Unity toward-sun -> raw z-up CoD toward-sun (inverse of (-x, z, -y)).
        sun_dir_cod = (-du[0], -du[2], du[1])
        sun_scale = min(
            LIGHT_GRID_SUN_INTENSITY_RANGE[1],
            max(
                LIGHT_GRID_SUN_INTENSITY_RANGE[0],
                float(environment.get("sunlight", 1.0)),
            ),
        )
        sun_rgb = _rgb(
            environment.get("sunColor"),
            LIGHT_GRID_SUN_FALLBACK_COLOR,
        )
        sun_radiance = tuple(
            component * sun_scale * LIGHT_GRID_CLASSIC_SUN_SCALE
            for component in sun_rgb
        )
    return sun_dir_cod, sun_radiance, ambient_floor


def _write_one_grid(
    path: Path,
    catalog_path: str,
    regenerate: bool,
    bake,
) -> str:
    """Bake+write one grid, or reuse/backfill/unlink exactly as M1 did. Returns
    the pak-relative catalog path, or "" when the grid has no content."""
    if regenerate or not path.is_file():
        grid = bake()
        if grid is not None:
            write_light_grid(path, grid[0], grid[1])
            return catalog_path
        # Nothing to bake: drop any stale file so the catalog stays honest.
        if path.is_file():
            path.unlink()
        return ""
    return catalog_path


def write_map_sun_profile(
    map_dir: Path,
    game: str,
    map_id: str,
    asset_index: dict[str, ArchiveEntry],
) -> str:
    """Copy the map's authored sun profile out of the paks.

    ``scripts/<mapname>.sun`` is retail's own parameter file for the sun
    sprite, the lens flare and — the part that is a GAMEPLAY mechanic — the
    blind/glare pair that darkens and washes the screen when the player looks
    into the sun. The engine loads it by BSP name and falls back to its
    built-in cvar defaults when the file is absent, which is the case for most
    stock multiplayer maps; ``FodSunProfile`` carries that fallback.

    Half a kilobyte of key/value text, pulled verbatim like the ``.shock``
    profiles. Returns the pak-relative catalog path, or "" when the map has
    no authored profile.
    """
    destination = map_dir / "sun.txt"
    catalog_path = f"maps/{game}/{map_id}/sun.txt"
    entry = asset_index.get(f"scripts/{map_id}.sun".casefold())
    if entry is None:
        # Not an error: retail ships a .sun for only a handful of maps.
        # Drop any stale copy so the catalog never points at a file that the
        # current source data no longer justifies.
        if destination.is_file():
            destination.unlink()
        return ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(read_entry(entry))
    print(
        f"  sun profile {game}:{map_id} <- "
        f"{entry.archive.name}:{entry.name}"
    )
    return catalog_path


def write_map_light_grids(
    map_dir: Path,
    game: str,
    map_id: str,
    lights: list[dict[str, object]],
    bounds_cod: dict[str, object] | None,
    environment: dict[str, object],
    regenerate: bool,
) -> tuple[str, str]:
    """Bake+write both viewmodel/character grids for a map and return
    (lightGridBin, lightGridClassicBin) catalog paths ("" when absent):
    lightgrid.bin (REIMAGINED, lamps only) and lightgrid_classic.bin (CLASSIC,
    lamps + authored sun + ambient). Each file is independently reused on the
    unchanged path and backfilled when missing, so an M1-era extraction still
    gains the Classic grid on the next run without a full re-extract."""
    reimagined = _write_one_grid(
        map_dir / "lightgrid.bin",
        f"maps/{game}/{map_id}/lightgrid.bin",
        regenerate,
        lambda: bake_viewmodel_light_grid(lights, bounds_cod, map_id),
    )
    sun_dir_cod, sun_radiance, ambient_floor = _classic_sun_ambient(environment)
    classic = _write_one_grid(
        map_dir / "lightgrid_classic.bin",
        f"maps/{game}/{map_id}/lightgrid_classic.bin",
        regenerate,
        lambda: bake_viewmodel_light_grid(
            lights,
            bounds_cod,
            map_id,
            sun_dir_cod=sun_dir_cod,
            sun_radiance=sun_radiance,
            ambient_floor=ambient_floor,
            sun_included=True,
        ),
    )
    return reimagined, classic


# ---------------------------------------------------------------------------
# Lamps-only world lightmaps (Lamp Lighting Rework stage 2).
#
# The CLASSIC pages fuse sun+lamps+ambient+occlusion per texel (BSP lump 1),
# so a lamps-only term the REIMAGINED world can fade with the day/night cycle
# must be computed analytically. These pages mirror the CLASSIC set exactly —
# same page count, same indexing, same page-normalized UVs — so a surface's
# existing classic pageIndex addresses the matching lamp page, and they use
# the same dLDR encoding so the runtime decode path is shared.
LAMP_LIGHTMAP_FORMAT = "FriendsOfDuty.MapLampLightmaps"
LAMP_LIGHTMAP_VERSION = 1
# Ray origins step off the surface along the interpolated normal before the
# any-hit query so a texel's own triangle (and coplanar decals inside the
# offset) can never shadow it. 2 CoD units = 5.08 cm.
LAMP_LIGHTMAP_SURFACE_OFFSET_COD = 2.0
# Segments stop this short of the lamp origin so fixture polygons hugging the
# emitter do not occlude the lamp's own light.
LAMP_LIGHTMAP_RAY_EPSILON_COD = 2.0
# Occluder uniform-grid cell edge: 128 CoD units = 3.25 m keeps the cell count
# in the tens of thousands on the largest shipping maps while keeping the
# per-cell triangle lists short enough for the vectorized any-hit tests.
LAMP_LIGHTMAP_GRID_CELL_COD = 128.0
# Ceiling on the dense cell count, because that count is allocated twice as
# int64 (the bincount and the CSR offset array) and the nominal cell size above
# only keeps it small for maps whose geometry stays near the play area.
# mp_cassino's draw soup spans 182528 x 133888 x 45358 CoD units -- about ten
# times Carentan per axis -- which is 535,150,497 cells and 7.97 GiB of offsets
# alone. That completes on a workstation with most of its memory free and fails
# outright on a machine a player is likely to own. 2**24 cells is 256 MB for
# the pair. Only the two maps whose geometry strays far from the play area are
# affected: Cassino drops to 512-unit cells (8,624,616) and Chateau to 256-unit
# (7,115,724, down from 55,539,918). Arnhem, Carentan, Pavlov, Railyard and
# Rocket all sit under 500k cells and keep the nominal size untouched.
LAMP_LIGHTMAP_GRID_MAX_CELLS = 16777216
# Contributions below half a stored-byte quantum cannot change the encoded
# dLDR pixel (pixel = clamp(E/2)*255), so their rays are never cast. This is
# what keeps the bake at minutes per map: only texels a lamp meaningfully
# reaches are shaded at all.
LAMP_LIGHTMAP_MIN_CONTRIBUTION = 0.5 / 255.0 * LIGHTMAP_DECODE_MULTIPLIER
# Gutter growth passes around each chart so the runtime's bilinear fetch at
# chart borders reads neighbouring lamp light instead of black. The CLASSIC
# pages carry CoD's own compiled padding; the analytic set must grow its own.
LAMP_LIGHTMAP_DILATE_PASSES = 4
# Rays per lockstep-DDA batch. Every ray of a lamp converges on the lamp's own
# grid cell at the end of traversal, so the transient (ray x triangle) pair
# arrays scale with chunk size times that cell's triangle count; 8192 bounds
# the spike to a few hundred MB on the densest shipping map.
LAMP_LIGHTMAP_RAY_CHUNK = 8192


def rasterize_lightmap_texels(
    uv_px: np.ndarray,
    world_positions: np.ndarray,
    world_normals: np.ndarray,
    triangles: np.ndarray,
    covered: np.ndarray,
    texel_positions: np.ndarray,
    texel_normals: np.ndarray,
    page_dim: int = LIGHTMAP_PAGE_DIM,
) -> int:
    """Rasterize one surface's triangles in lightmap texel space.

    A texel belongs to a triangle when its center (x+.5, y+.5) lies inside the
    triangle's page UVs scaled to texels; the covered texel receives the
    barycentric-interpolated world position and normal — exactly the surface
    point the runtime's bilinear lightmap fetch reconstructs for that texel.
    Charts butt against each other on shared edges, so a seam texel may be
    written by more than one triangle; the last write wins, and because the
    writers interpolate to (near-)identical surface points the difference is
    below the dLDR quantum. Writes into the caller's flat per-page
    accumulators (x-fastest, index = y*page_dim + x) and returns the number of
    texel writes performed."""
    dim = int(page_dim)
    uv = np.asarray(uv_px, dtype=np.float64)
    tris = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    written = 0
    for tri in tris:
        p0 = uv[tri[0]]
        p1 = uv[tri[1]]
        p2 = uv[tri[2]]
        denom = (
            (p1[0] - p0[0]) * (p2[1] - p0[1])
            - (p1[1] - p0[1]) * (p2[0] - p0[0])
        )
        if abs(denom) < 1e-9:
            # Zero-area chart triangle: no texel center can fall inside.
            continue
        lo_x = max(0, int(math.floor(min(p0[0], p1[0], p2[0]) - 0.5)))
        hi_x = min(dim - 1, int(math.ceil(max(p0[0], p1[0], p2[0]) - 0.5)))
        lo_y = max(0, int(math.floor(min(p0[1], p1[1], p2[1]) - 0.5)))
        hi_y = min(dim - 1, int(math.ceil(max(p0[1], p1[1], p2[1]) - 0.5)))
        if hi_x < lo_x or hi_y < lo_y:
            continue
        centers_x = np.arange(lo_x, hi_x + 1, dtype=np.float64) + 0.5
        centers_y = np.arange(lo_y, hi_y + 1, dtype=np.float64) + 0.5
        cx, cy = np.meshgrid(centers_x, centers_y)
        # Barycentric solve of (c - p0) = b1*(p1-p0) + b2*(p2-p0); the small
        # negative tolerance keeps texel centers sitting exactly on a shared
        # chart edge covered by both neighbours instead of neither.
        b1 = (
            (cx - p0[0]) * (p2[1] - p0[1]) - (cy - p0[1]) * (p2[0] - p0[0])
        ) / denom
        b2 = (
            (p1[0] - p0[0]) * (cy - p0[1]) - (p1[1] - p0[1]) * (cx - p0[0])
        ) / denom
        b0 = 1.0 - b1 - b2
        inside = (b0 >= -1e-9) & (b1 >= -1e-9) & (b2 >= -1e-9)
        if not inside.any():
            continue
        iy, ix = np.nonzero(inside)
        texel = (iy + lo_y) * dim + (ix + lo_x)
        w0 = b0[inside][:, np.newaxis]
        w1 = b1[inside][:, np.newaxis]
        w2 = b2[inside][:, np.newaxis]
        texel_positions[texel] = (
            w0 * world_positions[tri[0]]
            + w1 * world_positions[tri[1]]
            + w2 * world_positions[tri[2]]
        ).astype(np.float32)
        texel_normals[texel] = (
            w0 * world_normals[tri[0]]
            + w1 * world_normals[tri[1]]
            + w2 * world_normals[tri[2]]
        ).astype(np.float32)
        covered[texel] = True
        written += len(texel)
    return written


class TriangleOcclusionGrid:
    """Any-hit uniform-grid accelerator over the raw z-up CoD triangle soup.

    Built once per map for the lamp-lightmap bake: every draw-soup triangle
    (including alpha-cutout foliage — CoD's own compiled bake treats those as
    solid shadow casters too) is binned into fixed-size cells by bounding box
    and stored CSR-style. ``occluded`` answers "does anything intersect the
    texel->lamp segment" for whole batches: a lockstep 3D-DDA advances every
    active ray one cell per iteration, and Möller-Trumbore runs vectorized
    over the (ray, triangle) pairs each step gathers. Only the boolean answer
    matters, so hit order is irrelevant and a triangle straddling several
    cells may be tested more than once without affecting the result."""

    def __init__(
        self,
        positions: np.ndarray,
        triangles: np.ndarray,
        cell_size: float = LAMP_LIGHTMAP_GRID_CELL_COD,
    ) -> None:
        tris = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
        verts = np.asarray(positions, dtype=np.float64)
        corners = (
            verts[tris] if len(tris) else np.zeros((0, 3, 3), dtype=np.float64)
        )
        self._v0 = np.ascontiguousarray(corners[:, 0, :])
        self._e1 = np.ascontiguousarray(corners[:, 1, :] - corners[:, 0, :])
        self._e2 = np.ascontiguousarray(corners[:, 2, :] - corners[:, 0, :])
        self._cell = float(cell_size)
        if not len(tris):
            self._origin = np.zeros(3, dtype=np.float64)
            self._dims = np.ones(3, dtype=np.int64)
            self._cell_start = np.zeros(2, dtype=np.int64)
            self._cell_tris = np.zeros(0, dtype=np.int64)
            return
        lo = corners.min(axis=1)
        hi = corners.max(axis=1)
        # One-cell padding on every side so ray origins nudged off a hull
        # surface (and lamps hovering just outside it) still start in-grid.
        #
        # Widen the cells until the dense arrays fit LAMP_LIGHTMAP_GRID_MAX_CELLS.
        # This cannot move the bake's output: the grid only nominates candidate
        # triangles for the exact Moller-Trumbore test in _intersects, the query
        # is any-hit so testing a triangle more than once is harmless, and the
        # DDA's per-ray step bound is derived from _dims, which shrinks with the
        # grid. A coarser grid therefore costs traversal work and returns the
        # identical answer. Maps already inside the budget take the first branch
        # and keep both their nominal cell size and their byte-identical output.
        nominal_cells = None
        while True:
            origin = lo.min(axis=0) - self._cell
            dims = (
                np.floor((hi.max(axis=0) - origin) / self._cell).astype(
                    np.int64
                )
                + 2
            )
            cells = int(dims.prod())
            if cells <= LAMP_LIGHTMAP_GRID_MAX_CELLS:
                break
            if nominal_cells is None:
                nominal_cells = cells
            self._cell *= 2.0
        if nominal_cells is not None:
            print(
                "  lamp occlusion grid widened to %.0f-unit cells: %d cells "
                "in place of %d at the nominal %.0f"
                % (self._cell, cells, nominal_cells, float(cell_size))
            )
        self._origin = origin
        self._dims = dims
        lo_cell = np.clip(
            np.floor((lo - self._origin) / self._cell).astype(np.int64),
            0,
            self._dims - 1,
        )
        hi_cell = np.clip(
            np.floor((hi - self._origin) / self._cell).astype(np.int64),
            0,
            self._dims - 1,
        )
        # Enumerate every (triangle, overlapped cell) pair without a Python
        # loop: expand each triangle's cell-range box row-major.
        span = hi_cell - lo_cell + 1
        counts = span.prod(axis=1)
        tri_ids = np.repeat(np.arange(len(tris), dtype=np.int64), counts)
        starts = np.cumsum(counts) - counts
        local = (
            np.arange(int(counts.sum()), dtype=np.int64)
            - np.repeat(starts, counts)
        )
        span_x = np.repeat(span[:, 0], counts)
        span_y = np.repeat(span[:, 1], counts)
        cell_x = np.repeat(lo_cell[:, 0], counts) + local % span_x
        rest = local // span_x
        cell_y = np.repeat(lo_cell[:, 1], counts) + rest % span_y
        cell_z = np.repeat(lo_cell[:, 2], counts) + rest // span_y
        linear = (
            (cell_z * self._dims[1] + cell_y) * self._dims[0] + cell_x
        )
        order = np.argsort(linear, kind="stable")
        self._cell_tris = tri_ids[order]
        cell_count = int(self._dims.prod())
        bins = np.bincount(linear, minlength=cell_count)
        self._cell_start = np.zeros(cell_count + 1, dtype=np.int64)
        np.cumsum(bins, out=self._cell_start[1:])

    def occluded(
        self,
        origins: np.ndarray,
        targets: np.ndarray,
        stop_short_cod: float = LAMP_LIGHTMAP_RAY_EPSILON_COD,
    ) -> np.ndarray:
        """True per ray where any triangle intersects the origin->target
        segment, stopping ``stop_short_cod`` CoD units before the target."""
        origins = np.asarray(origins, dtype=np.float64).reshape(-1, 3)
        targets = np.asarray(targets, dtype=np.float64).reshape(-1, 3)
        result = np.zeros(len(origins), dtype=bool)
        if not len(self._cell_tris) or not len(origins):
            return result
        for begin in range(0, len(origins), LAMP_LIGHTMAP_RAY_CHUNK):
            end = min(begin + LAMP_LIGHTMAP_RAY_CHUNK, len(origins))
            result[begin:end] = self._occluded_chunk(
                origins[begin:end],
                targets[begin:end],
                float(stop_short_cod),
            )
        return result

    def _occluded_chunk(
        self,
        origins: np.ndarray,
        targets: np.ndarray,
        stop_short_cod: float,
    ) -> np.ndarray:
        count = len(origins)
        direction = targets - origins
        distance = np.linalg.norm(direction, axis=1)
        # Segments are parametrized O + t*D with t in (0, t_stop]; degenerate
        # rays (a texel sitting on the lamp) are trivially unoccluded.
        usable = distance > 1e-6
        t_stop = np.zeros(count, dtype=np.float64)
        t_stop[usable] = np.maximum(
            0.0, 1.0 - stop_short_cod / distance[usable]
        )
        occluded = np.zeros(count, dtype=bool)

        with np.errstate(divide="ignore"):
            inverse = np.where(direction != 0.0, 1.0 / direction, np.inf)
        step = np.sign(direction).astype(np.int64)
        cell = np.clip(
            np.floor((origins - self._origin) / self._cell).astype(np.int64),
            0,
            self._dims - 1,
        )
        # Parametric t of the next cell-boundary crossing per axis; +inf on
        # axes the ray never crosses (0 * inf NaNs land only on the discarded
        # where-branch, hence the suppressed invalid warning).
        boundary = self._origin + (cell + (step > 0)) * self._cell
        with np.errstate(invalid="ignore"):
            t_next = np.where(
                step != 0, (boundary - origins) * inverse, np.inf
            )
        t_delta = np.where(step != 0, self._cell * np.abs(inverse), np.inf)

        active = np.nonzero(usable & (t_stop > 0.0))[0]
        # A segment crosses each axis at most dims[axis] times; the slack
        # absorbs boundary jitter. Hitting the bound just under-occludes.
        for _ in range(int(self._dims.sum()) + 8):
            if not len(active):
                break
            linear = (
                (cell[active, 2] * self._dims[1] + cell[active, 1])
                * self._dims[0]
                + cell[active, 0]
            )
            start = self._cell_start[linear]
            amount = self._cell_start[linear + 1] - start
            total = int(amount.sum())
            if total:
                pair_row = np.repeat(
                    np.arange(len(active), dtype=np.int64), amount
                )
                pair_flat = (
                    np.arange(total, dtype=np.int64)
                    - np.repeat(np.cumsum(amount) - amount, amount)
                    + np.repeat(start, amount)
                )
                tri = self._cell_tris[pair_flat]
                ray = active[pair_row]
                hit = self._intersects(
                    origins[ray], direction[ray], t_stop[ray], tri
                )
                if hit.any():
                    occluded[ray[hit]] = True
            # Retire occluded rays and rays whose segment ends inside the
            # cell just tested, then advance survivors along the smallest t.
            t_here = t_next[active]
            axis = np.argmin(t_here, axis=1)
            crossing = t_here[np.arange(len(active)), axis]
            keep = ~occluded[active] & (crossing < t_stop[active])
            moving = active[keep]
            moving_axis = axis[keep]
            cell[moving, moving_axis] += step[moving, moving_axis]
            t_next[moving, moving_axis] += t_delta[moving, moving_axis]
            inside = (
                (cell[moving] >= 0) & (cell[moving] < self._dims)
            ).all(axis=1)
            active = moving[inside]
        return occluded

    def _intersects(
        self,
        ray_origins: np.ndarray,
        ray_directions: np.ndarray,
        ray_t_stop: np.ndarray,
        tri: np.ndarray,
    ) -> np.ndarray:
        """Vectorized Möller-Trumbore over (ray, triangle) pairs; double-sided,
        with small relative tolerances so shared-edge crossings never slip
        between two adjacent triangles."""
        v0 = self._v0[tri]
        e1 = self._e1[tri]
        e2 = self._e2[tri]
        pvec = np.cross(ray_directions, e2)
        det = np.einsum("ij,ij->i", e1, pvec)
        with np.errstate(divide="ignore", invalid="ignore"):
            inverse_det = np.where(np.abs(det) > 1e-12, 1.0 / det, 0.0)
        tvec = ray_origins - v0
        u = np.einsum("ij,ij->i", tvec, pvec) * inverse_det
        qvec = np.cross(tvec, e1)
        v = np.einsum("ij,ij->i", ray_directions, qvec) * inverse_det
        t = np.einsum("ij,ij->i", e2, qvec) * inverse_det
        return (
            (inverse_det != 0.0)
            & (u >= -1e-6)
            & (v >= -1e-6)
            & (u + v <= 1.0 + 1e-6)
            & (t > 1e-6)
            & (t <= ray_t_stop)
        )


def dilate_lamp_page(
    pixels: np.ndarray,
    covered: np.ndarray,
    passes: int = LAMP_LIGHTMAP_DILATE_PASSES,
) -> None:
    """Grow each chart's border texels into the uncovered gutter, in place.

    The runtime's bilinear fetch at a chart edge reads the gutter texels; the
    CLASSIC pages ship CoD's compiled padding there, but the analytic lamp
    pages start black outside coverage, which would darken every chart edge.
    Each pass fills an uncovered texel adjacent to coverage with the mean of
    its covered 8-neighbours and marks it covered, growing charts outward one
    texel per pass. ``covered`` is consumed (grown) alongside ``pixels``."""

    def _shifted(array: np.ndarray, dy: int, dx: int) -> np.ndarray:
        out = np.zeros_like(array)
        height, width = array.shape[:2]
        out[
            max(dy, 0) : height + min(dy, 0),
            max(dx, 0) : width + min(dx, 0),
        ] = array[
            max(-dy, 0) : height + min(-dy, 0),
            max(-dx, 0) : width + min(-dx, 0),
        ]
        return out

    for _ in range(passes):
        if covered.all():
            break
        summed = np.zeros_like(pixels)
        neighbours = np.zeros(covered.shape, dtype=np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                shifted_covered = _shifted(covered, dy, dx)
                summed += np.where(
                    shifted_covered[..., np.newaxis],
                    _shifted(pixels, dy, dx),
                    np.float32(0.0),
                )
                neighbours += shifted_covered
        fill = ~covered & (neighbours > 0)
        if not fill.any():
            break
        pixels[fill] = summed[fill] / neighbours[fill][:, np.newaxis]
        covered |= fill


def bake_lamp_lightmap_pages(
    bsp: bytes,
    lights: list[dict[str, object]],
    map_id: str,
) -> tuple[list[np.ndarray], dict[str, int]] | None:
    """Bake the analytic, raycast-occluded, lamps-only world lightmap pages.

    Per lit surface (lightmap page != 0xFFFF) the triangles are rasterized in
    page-texel space; each covered texel accumulates, over every lamp whose
    range reaches it, E += intensity_u * atten(d) * max(0, N.L) * lampColour —
    the exact runtime lamp contract (resolve_runtime_lamps/lamp_attenuation)
    plus the N.L term the isotropic viewmodel grid deliberately omits: these
    are oriented world surfaces. A lamp's contribution is zeroed when the
    texel->lamp segment hits any world triangle (TriangleOcclusionGrid) —
    unlike the grid precedent this bake IS occluded, because a leak through a
    wall is far more visible on a lightmap than on the coarse grid. Encoding
    is dLDR to mirror the CLASSIC pages: stored = clamp(E/2)*255, gamma space
    like everything else in the pipeline.

    Returns ([(512,512,3) uint8 per classic page], stats) or None when there
    is nothing to bake (no classic pages to mirror, or no usable lamps)."""
    page_count = len(decode_lightmap_pages(bsp))
    if not page_count:
        return None
    lamp_positions, lamp_intensities, lamp_ranges, lamp_colors = (
        resolve_runtime_lamps(lights, map_id)
    )
    if not len(lamp_positions):
        return None
    (
        _material_names,
        _groups,
        source_positions,
        _source_uvs,
        source_lightmap_uvs,
        source_normals,
        _faces,
        soup_faces,
        _vertex_count,
    ) = parse_world_lumps(bsp)
    occluder = TriangleOcclusionGrid(
        source_positions,
        np.concatenate([tris for _, _, tris in soup_faces])
        if soup_faces
        else np.zeros((0, 3), dtype=np.int64),
    )
    # Group surfaces by page so each page's accumulators live only while that
    # page bakes (a page's worth of texel positions/normals is ~6 MB).
    page_surfaces: dict[int, list[np.ndarray]] = defaultdict(list)
    for _material, page, triangles in soup_faces:
        # 0xFFFF marks an unlit surface (no UV2 to bake into); a page index at
        # or above the classic page count could not be addressed through the
        # classic pageIndex either, so it is skipped the same way.
        if page == LIGHTMAP_UNLIT_PAGE or page >= page_count:
            continue
        page_surfaces[page].append(triangles)

    uv_px = source_lightmap_uvs.astype(np.float64) * LIGHTMAP_PAGE_DIM
    lamp_positions_f64 = lamp_positions.astype(np.float64)
    lamp_ranges_cod = lamp_ranges.astype(np.float64) / COD_UNIT_TO_METRE
    texel_count = LIGHTMAP_PAGE_DIM * LIGHTMAP_PAGE_DIM
    pages: list[np.ndarray] = []
    texels_covered = 0
    texels_shaded = 0
    rays_cast = 0
    for page_index in range(page_count):
        stored = np.zeros(
            (LIGHTMAP_PAGE_DIM, LIGHTMAP_PAGE_DIM, 3), dtype=np.float32
        )
        covered = np.zeros(texel_count, dtype=bool)
        chunks = page_surfaces.get(page_index)
        if chunks:
            texel_positions = np.zeros((texel_count, 3), dtype=np.float32)
            texel_normals = np.zeros((texel_count, 3), dtype=np.float32)
            for triangles in chunks:
                rasterize_lightmap_texels(
                    uv_px,
                    source_positions,
                    source_normals,
                    triangles,
                    covered,
                    texel_positions,
                    texel_normals,
                )
            index = np.nonzero(covered)[0]
            texels_covered += len(index)
            if len(index):
                surface_points = texel_positions[index].astype(np.float64)
                normals = texel_normals[index].astype(np.float64)
                lengths = np.linalg.norm(normals, axis=1, keepdims=True)
                normals /= np.maximum(lengths, 1e-6)
                page_lo = surface_points.min(axis=0)
                page_hi = surface_points.max(axis=0)
                irradiance = np.zeros((len(index), 3), dtype=np.float32)
                shaded = np.zeros(len(index), dtype=bool)
                for lamp in range(len(lamp_positions_f64)):
                    lamp_point = lamp_positions_f64[lamp]
                    reach = float(lamp_ranges_cod[lamp])
                    # AABB-vs-range-sphere reject: most lamps cannot reach
                    # most pages at all.
                    nearest = np.clip(lamp_point, page_lo, page_hi)
                    if np.linalg.norm(lamp_point - nearest) >= reach:
                        continue
                    delta = lamp_point - surface_points
                    d_cod = np.linalg.norm(delta, axis=1)
                    d_m = d_cod * COD_UNIT_TO_METRE
                    atten = lamp_attenuation(d_m, float(lamp_ranges[lamp]))
                    n_dot_l = np.einsum("ij,ij->i", normals, delta) / (
                        np.maximum(d_cod, 1e-6)
                    )
                    weight = (
                        float(lamp_intensities[lamp])
                        * atten
                        * np.maximum(n_dot_l, 0.0)
                    )
                    select = weight * float(lamp_colors[lamp].max()) >= (
                        LAMP_LIGHTMAP_MIN_CONTRIBUTION
                    )
                    if not select.any():
                        continue
                    rows = np.nonzero(select)[0]
                    ray_origins = (
                        surface_points[rows]
                        + normals[rows] * LAMP_LIGHTMAP_SURFACE_OFFSET_COD
                    )
                    blocked = occluder.occluded(
                        ray_origins,
                        np.broadcast_to(lamp_point, (len(rows), 3)),
                    )
                    rays_cast += len(rows)
                    shaded[rows] = True
                    lit = rows[~blocked]
                    irradiance[lit] += (
                        weight[lit][:, np.newaxis]
                        * lamp_colors[lamp][np.newaxis, :]
                    ).astype(np.float32)
                texels_shaded += int(shaded.sum())
                # dLDR: stored = clamp(E / decodeMultiplier); the runtime
                # multiplies back by 2 on decode, same as the classic pages.
                stored.reshape(texel_count, 3)[index] = np.clip(
                    irradiance / np.float32(LIGHTMAP_DECODE_MULTIPLIER),
                    0.0,
                    1.0,
                )
        dilate_lamp_page(stored, covered.reshape(stored.shape[:2]))
        pages.append(
            np.round(stored * np.float32(255.0)).astype(np.uint8)
        )
    stats = {
        "texelsCovered": texels_covered,
        "texelsShaded": texels_shaded,
        "raysCast": rays_cast,
    }
    return pages, stats


def write_map_lamp_lightmaps(
    map_dir: Path,
    game: str,
    map_id: str,
    bsp: bytes,
    lights: list[dict[str, object]],
    regenerate: bool,
) -> str:
    """Bake+write the lamps-only page set, or reuse/backfill/prune exactly as
    _write_one_grid does for the light grids: (re)baked when --force or when
    the manifest is missing, so an existing extraction gains lamp pages on the
    next run without a world re-extract (the BSP bytes are already read on the
    unchanged-fingerprint path). Returns the pak-relative catalog path, or ""
    when there is nothing to bake."""
    lamps_dir = map_dir / "lightmaps_lamps"
    manifest_path = lamps_dir / "lightmaps_lamps.json"
    catalog_path = (
        f"maps/{game}/{map_id}/lightmaps_lamps/lightmaps_lamps.json"
    )
    if not regenerate and manifest_path.is_file():
        return catalog_path
    started = time.perf_counter()
    baked = bake_lamp_lightmap_pages(bsp, lights, map_id)
    if baked is None:
        # Nothing to bake: drop any stale page set so the catalog stays honest.
        if lamps_dir.is_dir():
            shutil.rmtree(lamps_dir)
        return ""
    pages, stats = baked
    lamps_dir.mkdir(parents=True, exist_ok=True)
    page_names = [f"page_{index}.png" for index in range(len(pages))]
    for name, page in zip(page_names, pages):
        Image.fromarray(page, "RGB").save(lamps_dir / name, optimize=True)
    expected_pages = set(page_names)
    for stale in lamps_dir.glob("page_*.png"):
        if stale.name not in expected_pages:
            stale.unlink()
    write_json(
        manifest_path,
        {
            "format": LAMP_LIGHTMAP_FORMAT,
            "version": LAMP_LIGHTMAP_VERSION,
            "encoding": "dLDR",
            "decodeMultiplier": LIGHTMAP_DECODE_MULTIPLIER,
            "pageWidth": LIGHTMAP_PAGE_DIM,
            "pageHeight": LIGHTMAP_PAGE_DIM,
            "pageCount": len(pages),
            "pages": [
                f"maps/{game}/{map_id}/lightmaps_lamps/{name}"
                for name in page_names
            ],
        },
    )
    print(
        f"  lamp lightmaps {game}:{map_id}: pages={len(pages)} "
        f"texelsCovered={stats['texelsCovered']} "
        f"texelsShaded={stats['texelsShaded']} "
        f"raysCast={stats['raysCast']} "
        f"{time.perf_counter() - started:.1f}s"
    )
    return catalog_path


def map_catalog_entry_v2(
    game: str,
    map_id: str,
    source: ArchiveEntry,
    manifest: dict[str, object],
    world: dict[str, object],
    fingerprint: str,
    audio: dict[str, object],
    light_grid_bin: str = "",
    light_grid_classic_bin: str = "",
    lamp_lightmaps_json: str = "",
    sun_profile: str = "",
) -> dict[str, object]:
    spawns = manifest["spawns"]
    base = f"maps/{game}/{map_id}"
    return {
        "game": game,
        "mapId": map_id,
        "title": title_for(map_id),
        "source": f"{source.archive.name}:{source.name}",
        "sourceFingerprint": fingerprint,
        "supportedGameTypes": manifest["supportedGameTypes"],
        "worldGlb": f"{base}/world.glb",
        "clipGlb": world.get("clipGlb", ""),
        "optimizationJson": f"{base}/optimization.json",
        "entitiesJson": f"{base}/entities.json",
        "materialsJson": f"{base}/materials.json",
        "skyPng": f"{base}/sky.png" if world.get("sky") else "",
        "lightmapsJson": world.get("lightmapsJson", ""),
        "lightGridBin": light_grid_bin,
        "lightGridClassicBin": light_grid_classic_bin,
        # Stage-2 lamps-only world lightmaps. Additive field + files: an old
        # build's JsonUtility ignores unknown catalog fields and never opens
        # lightmaps_lamps/, so GAME_CONTENT_VERSION (exporter/package.py)
        # deliberately stays put — any bump batches into the planned 3->4
        # (ENGINE_SCALING_ROADMAP.md).
        "lampLightmapsJson": lamp_lightmaps_json,
        # Retail's authored sun/flare/blind parameters. Additive field like
        # the lamp pages above: an old build's JsonUtility ignores it and the
        # runtime falls back to the engine-default profile, so no content
        # version bump is owed.
        "sunProfile": sun_profile,
        "environment": map_environment(
            manifest["worldspawn"],
            manifest["fog"],
            manifest["authoredSun"],
        ),
        "audio": audio,
        "recommendedSpawn": preferred_spawn(spawns),
        "spawns": spawns,
    }


def write_json(path: Path, payload: object) -> None:
    """Write a manifest atomically.

    An interrupted run must never leave a half-written catalog.json or
    materials.json behind: the readers downstream treat a truncated file the
    same as a corrupt one, and the recovery path for a corrupt catalog used to
    be "assume no maps exist", which prunes real map trees. Writing to a
    sibling temp file and renaming makes the target either the old content or
    the new one, never a prefix of the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def optimization_outputs_exist(
    pak_root: Path,
    map_dir: Path,
) -> bool:
    path = map_dir / "optimization.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != MAP_OPTIMIZATION_FORMAT
        or manifest.get("version") != MAP_OPTIMIZATION_VERSION
        or manifest.get("sourceBspVersion") != SUPPORTED_VERSION
        or manifest.get("codUnitToMetre") != COD_UNIT_TO_METRE
        or manifest.get("sectorStrategy") != "unity-xz-grid-v1"
        or manifest.get("sectorSizeMetres")
        != RENDER_SECTOR_SIZE_METRES
    ):
        return False
    sectors = manifest.get("sectors")
    if not isinstance(sectors, list) or not sectors:
        return False
    visibility = manifest.get("visibility")
    if not isinstance(visibility, dict):
        return False
    try:
        cluster_count = int(visibility["clusterCount"])
        row_bytes = int(visibility["rowBytes"])
        plane_count = int(visibility["planeCount"])
        node_count = int(visibility["nodeCount"])
        leaf_count = int(visibility["leafCount"])
        encoded_lengths = (
            ("pvsBase64", cluster_count * row_bytes),
            ("planesBase64", plane_count * 16),
            ("nodesBase64", node_count * 12),
            ("leavesBase64", leaf_count * 8),
        )
        if (
            cluster_count <= 0
            or row_bytes < (cluster_count + 7) // 8
            or row_bytes % 4
            or plane_count <= 0
            or node_count <= 0
            or leaf_count <= 0
            or not isinstance(visibility.get("cells"), list)
            or not visibility["cells"]
            or any(
                len(base64.b64decode(visibility[field], validate=True))
                != expected
                for field, expected in encoded_lengths
            )
        ):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    for sector in sectors:
        if not isinstance(sector, dict):
            return False
        clusters = sector.get("clusterIndices")
        always_visible = sector.get("alwaysVisible")
        if (
            not isinstance(sector.get("name"), str)
            or not sector["name"]
            or not isinstance(sector.get("gridX"), int)
            or isinstance(sector["gridX"], bool)
            or not isinstance(sector.get("gridZ"), int)
            or isinstance(sector["gridZ"], bool)
            or not isinstance(clusters, list)
            or not all(
                isinstance(cluster, int)
                and not isinstance(cluster, bool)
                and 0 <= cluster < cluster_count
                for cluster in clusters
            )
            or clusters != sorted(set(clusters))
            or not isinstance(always_visible, bool)
            or (always_visible and clusters)
            or (not always_visible and not clusters)
            or not isinstance(sector.get("triangles"), int)
            or isinstance(sector["triangles"], bool)
            or sector["triangles"] <= 0
        ):
            return False
        relative = (
            sector.get("glb")
        )
        if not isinstance(relative, str) or not relative:
            return False
        normalized = PurePosixPath(relative.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            return False
        if not (pak_root / normalized).is_file():
            return False
    return True


def run_unity_mode(args: argparse.Namespace) -> None:
    unity_assets = args.unity_root.resolve() / "Assets"
    imported_root = unity_assets / "Maps" / "Imported"
    source_root = imported_root / "Source"
    shared_root = imported_root / "Shared"
    audio_root = unity_assets / "Audio" / "CoD1" / "Maps"
    requested = parse_requested_maps(args.maps)
    allowed_specs = shipping_map_specs(include_uo=args.include_uo)
    allowed = {spec[2] for spec in allowed_specs}
    if requested is not None:
        unavailable = sorted(requested - allowed)
        if unavailable:
            raise ValueError(
                "United Offensive is required for map(s): "
                + ", ".join(unavailable)
            )

    base_archives = official_archives(args.game_root, "cod1")
    catalog_entries = []
    all_model_assets: set[str] = set()
    extraction_stats = {
        "generated": 0,
        "unchanged": 0,
        "existingPavlov": 0,
    }

    specs = [
        spec
        for spec in allowed_specs
        if requested is None or spec[2] in requested
    ]

    for game, source_game, map_id in specs:
        game_archives = official_archives(args.game_root, source_game)
        maps = multiplayer_maps(game_archives)
        source = next(
            (
                entry
                for entry in maps.values()
                if PurePosixPath(entry.name).stem.casefold() == map_id
            ),
            None,
        )
        if source is None:
            raise FileNotFoundError(
                f"{source_game} multiplayer BSP is missing: {map_id}"
            )
        asset_archives = (
            base_archives
            if game == "cod1"
            else base_archives +
                 official_archives(args.game_root, "uo")
        )
        asset_index = layered_index(asset_archives)
        bsp = read_entry(source)
        validate_bsp(bsp, f"{source.archive.name}:{source.name}")
        script_entry = find_map_script(map_id, asset_index)
        script = (
            read_entry(script_entry).decode("latin1", "replace")
            if script_entry is not None
            else None
        )
        fingerprint = source_fingerprint(source, bsp, script)
        entities = parse_entities(bsp)
        manifest = entity_manifest(
            game,
            map_id,
            source,
            bsp,
            entities,
            script,
            asset_index,
            audio_root / map_id,
            f"Assets/Audio/CoD1/Maps/{map_id}",
        )
        audio = map_audio_manifest(
            map_id,
            script,
            entities,
            asset_index,
            audio_root / map_id,
            f"Assets/Audio/CoD1/Maps/{map_id}",
        )
        all_model_assets.update(manifest["modelAssets"])
        existing_pavlov = game == "cod1" and map_id == "mp_pavlov"
        if existing_pavlov:
            catalog_entries.append(
                map_catalog_entry(
                    game,
                    map_id,
                    source,
                    manifest,
                    None,
                    "Assets/Maps/Pavlov/Source",
                    fingerprint,
                    True,
                    audio,
                )
            )
            extraction_stats["existingPavlov"] += 1
            print("catalogued existing cod1:mp_pavlov")
            continue

        output_dir = source_root / game / map_id
        unity_source = (
            f"Assets/Maps/Imported/Source/{game}/{map_id}"
        )
        metadata_path = output_dir / f"{map_id}_source.json"
        existing_metadata = None
        if metadata_path.is_file():
            try:
                existing_metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                pass
        outputs_exist = (
            (output_dir / f"{map_id}.obj").is_file()
            and (output_dir / f"{map_id}.mtl").is_file()
            and (output_dir / f"{map_id}_entities.json").is_file()
        )
        if (
            not args.force
            and outputs_exist
            and existing_metadata
            and existing_metadata.get("sourceFingerprint") == fingerprint
        ):
            world = existing_metadata["world"]
            extraction_stats["unchanged"] += 1
            print(f"unchanged {game}:{map_id}")
        else:
            print(f"extracting {game}:{map_id}")
            world = extract_world(
                bsp,
                output_dir,
                map_id,
                game,
                asset_index,
                shared_root / game / "Textures",
            )
            write_json(
                output_dir / f"{map_id}_entities.json",
                manifest,
            )
            write_json(
                metadata_path,
                {
                    "format": "FriendsOfDuty.OriginalMapSource",
                    "version": 1,
                    "game": game,
                    "mapId": map_id,
                    "sourceFingerprint": fingerprint,
                    "source": (
                        f"{source.archive.name}:{source.name}"
                    ),
                    "world": world,
                },
            )
            extraction_stats["generated"] += 1

        catalog_entries.append(
            map_catalog_entry(
                game,
                map_id,
                source,
                manifest,
                world,
                unity_source,
                fingerprint,
                False,
                audio,
            )
        )

    expected = len(requested) if requested is not None else len(allowed_specs)
    if len(catalog_entries) != expected:
        raise RuntimeError(
            f"expected {expected} curated multiplayer maps, "
            f"found {len(catalog_entries)}"
        )

    unique_model_ids = sorted(
        {name.casefold() for name in all_model_assets}
    )
    catalog = {
        "format": "FriendsOfDuty.OriginalMultiplayerMapCatalog",
        "version": 1,
        "codUnitToMetre": COD_UNIT_TO_METRE,
        "maps": catalog_entries,
        "counts": {
            "maps": len(catalog_entries),
            "cod1": sum(
                item["game"] == "cod1" for item in catalog_entries
            ),
            "unitedOffensive": sum(
                item["game"] == "uo" for item in catalog_entries
            ),
            "newWorldMeshes": sum(
                not item["existingPavlov"] for item in catalog_entries
            ),
            "uniquePropModels": len(unique_model_ids),
        },
        "extraction": extraction_stats,
    }
    write_json(imported_root / "OriginalMultiplayerMapCatalog.json", catalog)
    write_json(
        unity_assets / "Resources" /
        "OriginalMultiplayerMapCatalog.json",
        catalog,
    )
    write_json(
        imported_root / "OriginalMultiplayerPropModels.json",
        {
            "format": "FriendsOfDuty.OriginalMultiplayerPropModels",
            "version": 1,
            "models": unique_model_ids,
            "count": len(unique_model_ids),
        },
    )
    print(
        "MULTIPLAYER MAP IMPORT COMPLETE — "
        f"maps={len(catalog_entries)}, "
        f"generated={extraction_stats['generated']}, "
        f"unchanged={extraction_stats['unchanged']}, "
        f"newWorldMeshes={catalog['counts']['newWorldMeshes']}, "
        f"uniqueProps={len(unique_model_ids)}"
    )
    print(imported_root / "OriginalMultiplayerMapCatalog.json")


def pak_map_specs(
    game_root: Path,
    base_archives: list[Path],
    requested: set[str] | None,
    all_mp: bool,
    has_uo: bool,
) -> list[tuple[str, str, str]]:
    # ``all_mp`` remains accepted for older launchers but can never expand
    # the shipping allowlist.
    del all_mp
    allowed_specs = shipping_map_specs(include_uo=has_uo)
    allowed = {spec[2] for spec in allowed_specs}
    if requested is not None:
        unknown = sorted(requested - ALL_SHIPPING_MAP_IDS)
        if unknown:
            raise ValueError(
                "map(s) are not in the Friends of Duty shipping rotation: "
                + ", ".join(unknown)
            )
        unavailable = sorted(requested - allowed)
        if unavailable:
            raise ValueError(
                "United Offensive is required for map(s): "
                + ", ".join(unavailable)
            )
        specs = [
            spec for spec in allowed_specs if spec[2] in requested
        ]
    else:
        specs = list(allowed_specs)

    available_by_tier = {
        "cod1": {
            PurePosixPath(entry.name).stem.casefold()
            for entry in multiplayer_maps(base_archives).values()
        }
    }
    if has_uo:
        available_by_tier["uo"] = {
            PurePosixPath(entry.name).stem.casefold()
            for entry in multiplayer_maps(
                official_archives(game_root, "uo")
            ).values()
        }
    missing = sorted(
        map_id
        for _game, source_tier, map_id in specs
        if map_id not in available_by_tier.get(source_tier, set())
    )
    if missing:
        raise FileNotFoundError(
            "shipping multiplayer BSP(s) are missing: "
            + ", ".join(missing)
        )
    return specs


def merge_catalog_maps(
    catalog_path: Path,
    catalog_entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merge this invocation's entries into any existing catalog by
    (game, mapId) so regenerating a subset never drops the other maps."""
    existing: list[dict[str, object]] = []
    if catalog_path.is_file():
        try:
            payload = json.loads(
                catalog_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and isinstance(
            payload.get("maps"), list
        ):
            existing = [
                item
                for item in payload["maps"]
                if isinstance(item, dict)
            ]
    merged = {
        (str(item.get("game")), str(item.get("mapId"))): item
        for item in existing
    }
    for entry in catalog_entries:
        merged[(str(entry["game"]), str(entry["mapId"]))] = entry
    return [merged[key] for key in sorted(merged)]


def selected_catalog_maps(
    catalog_path: Path,
    catalog_entries: list[dict[str, object]],
    *,
    allowed_keys: set[tuple[str, str]],
    authoritative: bool,
) -> list[dict[str, object]]:
    """Build a catalog which can never retain an unshipped map."""
    candidates = (
        catalog_entries
        if authoritative
        else merge_catalog_maps(catalog_path, catalog_entries)
    )
    return sorted(
        (
            entry
            for entry in candidates
            if (
                str(entry.get("game")),
                str(entry.get("mapId")).casefold(),
            )
            in allowed_keys
        ),
        key=lambda item: (str(item["game"]), str(item["mapId"])),
    )


def collect_pak_model_assets(
    pak_root: Path,
    catalog_entries: list[dict[str, object]],
) -> set[str]:
    """Union props from maps that are actually present in the catalog.

    Old map directories can remain on disk after switching from UO to the base
    tier. Walking every directory would silently carry their expansion props
    into a base-only package.
    """
    models: set[str] = set()
    for entry in catalog_entries:
        relative = entry.get("entitiesJson")
        if not isinstance(relative, str):
            continue
        entities_path = pak_root / relative
        try:
            manifest = json.loads(
                entities_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        assets = manifest.get("modelAssets")
        if isinstance(assets, list):
            models.update(
                item for item in assets if isinstance(item, str)
            )
    return models


def referenced_shared_map_textures(
    pak_root: Path,
    catalog_entries: list[dict[str, object]],
) -> set[str]:
    """Return package-relative shared textures used by catalogued maps."""
    referenced: set[str] = set()
    for entry in catalog_entries:
        relative = entry.get("materialsJson")
        if not isinstance(relative, str):
            continue
        try:
            materials = json.loads(
                (pak_root / relative).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(materials, list):
            continue
        for material in materials:
            texture = (
                material.get("texture")
                if isinstance(material, dict)
                else None
            )
            if not isinstance(texture, str) or not texture:
                continue
            normalized = PurePosixPath(texture.replace("\\", "/"))
            if (
                normalized.is_absolute()
                or ".." in normalized.parts
                or normalized.parts[:2] != ("maps", "shared")
            ):
                raise ValueError(
                    f"unsafe shared map texture path: {texture!r}"
                )
            referenced.add(normalized.as_posix().casefold())
    return referenced


def prune_unreferenced_shared_map_textures(
    pak_root: Path,
    catalog_entries: list[dict[str, object]],
) -> int:
    """Remove map-owned shared textures not used by the selected catalog."""
    shared_root = pak_root / "maps" / "shared"
    if not shared_root.is_dir():
        return 0
    referenced = referenced_shared_map_textures(
        pak_root,
        catalog_entries,
    )
    removed = 0
    for path in sorted(shared_root.rglob("*"), reverse=True):
        if path.is_file():
            relative = path.relative_to(pak_root).as_posix().casefold()
            if relative not in referenced:
                path.unlink()
                removed += 1
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    return removed


def prune_stale_map_outputs(
    pak_root: Path,
    catalog_entries: list[dict[str, object]],
) -> tuple[list[tuple[str, str]], int]:
    """Prune map directories and shared textures outside the catalog."""
    maps_root = pak_root / "maps"
    selected_directories = {
        (str(entry["game"]), str(entry["mapId"]).casefold())
        for entry in catalog_entries
    }
    # Last-line backstop against deleting the product. Every shipping map is
    # in the catalog on any correct run, so a shipping map directory that is
    # NOT in the catalog means the catalog is wrong — a narrowed run, a
    # truncated file, a bug upstream. Removing seven maps' worth of exported
    # geometry because a manifest went missing is not a recoverable mistake,
    # so refuse rather than rmtree and let the caller re-import.
    shipping = {
        (game, map_id.casefold())
        for game, _source_tier, map_id in SHIPPING_MAP_SPECS
    }
    removed_maps: list[tuple[str, str]] = []
    for game in ("cod1", "uo"):
        game_dir = maps_root / game
        if not game_dir.is_dir():
            continue
        for map_dir in game_dir.iterdir():
            if not map_dir.is_dir():
                continue
            key = (game, map_dir.name.casefold())
            if key in selected_directories:
                continue
            if key in shipping:
                raise ValueError(
                    f"refusing to delete shipping map {game}:{map_dir.name} — "
                    "it is missing from the catalog this run produced. The "
                    "catalog is wrong, not the export; re-run a full import."
                )
            removed_maps.append((game, map_dir.name))
            shutil.rmtree(map_dir)
    removed_textures = prune_unreferenced_shared_map_textures(
        pak_root,
        catalog_entries,
    )
    return removed_maps, removed_textures


def run_pak_mode(args: argparse.Namespace) -> None:
    pak_root = args.pak_root.resolve()
    maps_root = pak_root / "maps"
    requested = parse_requested_maps(args.maps)
    base_archives = official_archives(args.game_root, "cod1")
    has_uo = args.include_uo
    if has_uo:
        # Fail early on a partial/missing optional install rather than silently
        # producing a package that claims UO coverage.
        official_archives(args.game_root, "uo")
    specs = pak_map_specs(
        args.game_root,
        base_archives,
        requested,
        args.all_mp,
        has_uo,
    )
    if not specs:
        raise ValueError("no multiplayer maps selected")

    catalog_entries = []
    all_model_assets: set[str] = set()
    extraction_stats = {"generated": 0, "unchanged": 0}

    for game, source_game, map_id in specs:
        game_archives = official_archives(args.game_root, source_game)
        maps = multiplayer_maps(game_archives)
        source = next(
            (
                entry
                for entry in maps.values()
                if PurePosixPath(entry.name).stem.casefold() == map_id
            ),
            None,
        )
        if source is None:
            raise FileNotFoundError(
                f"{source_game} multiplayer BSP is missing: {map_id}"
            )
        asset_archives = (
            base_archives
            if game == "cod1"
            else base_archives +
                 official_archives(args.game_root, "uo")
        )
        asset_index = layered_index(asset_archives)
        bsp = read_entry(source)
        validate_bsp(bsp, f"{source.archive.name}:{source.name}")
        script_entry = find_map_script(map_id, asset_index)
        script = (
            read_entry(script_entry).decode("latin1", "replace")
            if script_entry is not None
            else None
        )
        fingerprint = source_fingerprint(source, bsp, script)
        entities = parse_entities(bsp)
        map_dir = maps_root / game / map_id
        manifest = entity_manifest(
            game,
            map_id,
            source,
            bsp,
            entities,
            script,
            asset_index,
            map_dir / "ambience",
            f"maps/{game}/{map_id}/ambience",
        )
        audio = map_audio_manifest(
            map_id,
            script,
            entities,
            asset_index,
            map_dir / "ambience",
            f"maps/{game}/{map_id}/ambience",
        )
        all_model_assets.update(manifest["modelAssets"])

        metadata_path = map_dir / "source.json"
        existing_metadata = None
        if metadata_path.is_file():
            try:
                existing_metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                pass
        outputs_exist = (
            (map_dir / "world.glb").is_file()
            and (map_dir / "entities.json").is_file()
            and (map_dir / "materials.json").is_file()
            and optimization_outputs_exist(pak_root, map_dir)
        )
        regenerated = False
        if (
            not args.force
            and outputs_exist
            and existing_metadata
            and existing_metadata.get("sourceFingerprint") == fingerprint
        ):
            world = existing_metadata["world"]
            extraction_stats["unchanged"] += 1
            print(f"unchanged {game}:{map_id}")
        else:
            regenerated = True
            print(f"extracting {game}:{map_id}")
            world = extract_world_pak(
                bsp,
                map_dir,
                map_id,
                game,
                asset_index,
                maps_root / "shared" / game / "textures",
                entities,
            )
            write_json(map_dir / "entities.json", manifest)
            write_json(
                map_dir / "materials.json",
                [
                    {
                        "group": item["group"],
                        "texture": item["texture"] or "",
                        "alphaCutout": item["alphaCutout"],
                        "decal": item.get("decal", False),
                        "polygonOffset": item.get("polygonOffset", False),
                        "sky": item["sky"],
                        "ladder": item.get("ladder", False),
                        "ladderVolumes": item.get(
                            "ladderVolumes",
                            [],
                        ),
                        "fallbackColor": item["fallbackColor"],
                        # Additive impact surface type; only present when
                        # the source shader declared one (JsonUtility
                        # defaults it to "" when absent).
                        **(
                            {"surface": item["surface"]}
                            if item.get("surface")
                            else {}
                        ),
                    }
                    for item in world["materialManifest"]
                ],
            )
            write_json(
                metadata_path,
                {
                    "format": "FriendsOfDuty.OriginalMapSource",
                    "version": 1,
                    "game": game,
                    "mapId": map_id,
                    "sourceFingerprint": fingerprint,
                    "source": (
                        f"{source.archive.name}:{source.name}"
                    ),
                    "world": world,
                },
            )
            extraction_stats["generated"] += 1

        environment = map_environment(
            manifest["worldspawn"],
            manifest["fog"],
            manifest["authoredSun"],
        )
        light_grid_bin, light_grid_classic_bin = write_map_light_grids(
            map_dir,
            game,
            map_id,
            manifest["lights"],
            world.get("boundsCodUnits"),
            environment,
            regenerated,
        )
        # Stage-2 lamp lightmaps backfill exactly like the grids above: the
        # BSP bytes are read even on the unchanged-fingerprint path, so an
        # M3-era extraction gains the lamp pages on its next run.
        lamp_lightmaps_json = write_map_lamp_lightmaps(
            map_dir,
            game,
            map_id,
            bsp,
            manifest["lights"],
            regenerated,
        )
        # Runs on both paths (a straight archive copy, no bake), so an
        # existing extraction gains its sun profile on the next run.
        sun_profile = write_map_sun_profile(
            map_dir,
            game,
            map_id,
            asset_index,
        )
        catalog_entries.append(
            map_catalog_entry_v2(
                game,
                map_id,
                source,
                manifest,
                world,
                fingerprint,
                audio,
                light_grid_bin,
                light_grid_classic_bin,
                lamp_lightmaps_json,
                sun_profile,
            )
        )

    if len(catalog_entries) != len(specs):
        raise RuntimeError(
            f"expected {len(specs)} multiplayer maps, "
            f"found {len(catalog_entries)}"
        )

    allowed_specs = shipping_map_specs(include_uo=has_uo)
    allowed_keys = {
        (game, map_id)
        for game, _source_tier, map_id in allowed_specs
    }
    selected_ids = {spec[2] for spec in specs}
    authoritative = (
        requested is None
        or selected_ids == {spec[2] for spec in allowed_specs}
    )
    # A canonical full-roster invocation is authoritative even when the
    # launcher passes all seven ids through --maps. A true developer subset
    # may retain other shipping maps, but can never retain an excluded map.
    merged_maps = selected_catalog_maps(
        maps_root / "catalog.json",
        catalog_entries,
        allowed_keys=allowed_keys,
        authoritative=authoritative,
    )
    # Publish the catalog BEFORE deleting anything. Pruning is the only
    # destructive step in the import, and a kill between the rmtree and the
    # catalog write used to leave a catalog referencing directories that no
    # longer exist — a package that validates on paper and fails at mount.
    # Writing first (and atomically) means the on-disk state only ever moves
    # from one consistent package to the next.
    write_json(
        maps_root / "catalog.json",
        {
            "format": "FriendsOfDuty.MapCatalog",
            "version": 2,
            "codUnitToMetre": COD_UNIT_TO_METRE,
            "maps": merged_maps,
        },
    )
    pruned_maps, pruned_shared_textures = prune_stale_map_outputs(
        pak_root,
        merged_maps,
    )
    for game, map_id in pruned_maps:
        print(f"pruned stale map: {game}:{map_id}")
    if pruned_shared_textures:
        print(
            "pruned stale shared map textures: "
            f"{pruned_shared_textures}"
        )
    all_model_assets.update(
        collect_pak_model_assets(pak_root, merged_maps)
    )
    # Active multiplayer GSC and its recursively reached EFX can spawn
    # models that are not placed directly in a BSP entity list. The script
    # closure is extracted before maps in the canonical pipeline, so include
    # that exact MP-only model roster in the prop contract as well.
    script_content_path = (
        pak_root / "scripts" / "mp" / "mp_content.json"
    )
    if script_content_path.is_file():
        try:
            script_content = json.loads(
                script_content_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            script_content = None
        script_models = []
        if isinstance(script_content, dict):
            manifest_maps = script_content.get("selectedMaps")
            expected_manifest_maps = sorted(selected_ids)
            if manifest_maps == expected_manifest_maps:
                candidate_models = script_content.get("modelNames", [])
                if isinstance(candidate_models, list):
                    script_models = candidate_models
            else:
                print(
                    "ignored stale MP GSC model roster: selectedMaps "
                    f"{manifest_maps!r} != {expected_manifest_maps!r}"
                )
        all_model_assets.update(
            model
            for model in script_models
            if isinstance(model, str) and model
        )
    unique_model_ids = sorted(
        {name.casefold() for name in all_model_assets}
    )
    write_json(
        pak_root / "props" / "required_models.json",
        {
            "format": "FriendsOfDuty.OriginalMultiplayerPropModels",
            "version": 1,
            "models": unique_model_ids,
            "count": len(unique_model_ids),
        },
    )
    print(
        "MULTIPLAYER MAP PAK EXPORT COMPLETE — "
        f"maps={len(merged_maps)} "
        f"(this run={len(catalog_entries)}), "
        f"generated={extraction_stats['generated']}, "
        f"unchanged={extraction_stats['unchanged']}, "
        f"uniqueProps={len(unique_model_ids)}"
    )
    print(maps_root / "catalog.json")


def main() -> None:
    args = arguments()
    if args.pak_root is not None:
        run_pak_mode(args)
    else:
        run_unity_mode(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
