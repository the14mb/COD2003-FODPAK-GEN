#!/usr/bin/env python3
"""Minimal pure-Python GLB v2 writer for Friends of Duty static meshes.

Container shape: one scene, one root node named "world", one mesh whose
primitives map one-to-one onto BSP material groups.  Every primitive owns
compact POSITION/NORMAL/TEXCOORD_0 float32 accessors plus uint32 indices and
references a material carrying a name only — textures are rebound at runtime
from the pak materials.json table.

Geometry convention chain (GLB data must reproduce the proven OBJ result):

  Legacy OBJ path (proven in-game):
    BSP CoD space (x, y, z) inches, Z-up
      -> OBJ   v  = (x, z, -y)      vt = (u, 1 - v)      faces (a, c, b)
      -> Unity ModelImporter (globalScale 0.0254) mirrors X and reverses
         triangle winding
      -> Unity pos = (-x, z, -y) * 0.0254   uv = (u, 1 - v)   faces (a, b, c)

  GLB path (this writer; the importer supplies the already-baked arrays):
    POSITION   = (x, z, -y) * 0.0254      metres, scale baked
    NORMAL     = normalize((nx, nz, -ny)) no scale
    TEXCOORD_0 = (u, v)                   raw BSP UV
    indices    = (a, c, b)                swapped from BSP draw order

  FodGlbStaticLoader/glTFast then apply pos (-x, y, z), reversed triangle
  winding and uv v' = 1 - v, yielding exactly the legacy Unity result above.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np


GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942
COMPONENT_FLOAT32 = 5126
COMPONENT_UINT32 = 5125
TARGET_ARRAY_BUFFER = 34962
TARGET_ELEMENT_ARRAY_BUFFER = 34963
MODE_TRIANGLES = 4


def _as_array(
    primitive: dict[str, object],
    key: str,
    dtype: type,
    columns: int | None,
) -> np.ndarray:
    values = np.ascontiguousarray(np.asarray(primitive[key], dtype=dtype))
    if columns is None:
        if values.ndim != 1:
            raise ValueError(f"{key} must be one-dimensional")
    elif values.ndim != 2 or values.shape[1] != columns:
        raise ValueError(f"{key} must have shape (N, {columns})")
    return values


def write_glb(
    path: Path | str,
    primitives: list[dict[str, object]],
    node_name: str = "world",
) -> None:
    """Write a GLB whose single mesh has one primitive per entry.

    Each primitive dict carries: material (str), positions (N,3 float32),
    normals (N,3 float32), uvs (N,2 float32), indices (M uint32, M % 3 == 0).
    An optional uvs2 (N,2 float32) lightmap UV channel is written as
    TEXCOORD_1 when present; primitives without it write POSITION/NORMAL/
    TEXCOORD_0 exactly as before. An optional lightmapIndex (int, -1 for
    unlit) is written to the primitive's glTF extras for runtime page binding.
    """
    if not primitives:
        raise ValueError("GLB requires at least one primitive")

    binary = bytearray()
    buffer_views: list[dict[str, int]] = []
    accessors: list[dict[str, object]] = []
    materials: list[dict[str, str]] = []
    mesh_primitives: list[dict[str, object]] = []

    def add_view(blob: bytes, target: int) -> int:
        offset = len(binary)
        binary.extend(blob)
        binary.extend(b"\0" * (-len(binary) % 4))
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": offset,
                "byteLength": len(blob),
                "target": target,
            }
        )
        return len(buffer_views) - 1

    def add_accessor(
        view: int,
        component_type: int,
        count: int,
        kind: str,
        minimum: list[float] | None = None,
        maximum: list[float] | None = None,
    ) -> int:
        accessor: dict[str, object] = {
            "bufferView": view,
            "componentType": component_type,
            "count": count,
            "type": kind,
        }
        if minimum is not None:
            accessor["min"] = minimum
        if maximum is not None:
            accessor["max"] = maximum
        accessors.append(accessor)
        return len(accessors) - 1

    for primitive in primitives:
        positions = _as_array(primitive, "positions", np.float32, 3)
        # Collision-only meshes (clip.glb) never reach a renderer, so they
        # omit both shading channels rather than ship a buffer of zeros.
        normals = (
            _as_array(primitive, "normals", np.float32, 3)
            if primitive.get("normals") is not None
            else None
        )
        uvs = (
            _as_array(primitive, "uvs", np.float32, 2)
            if primitive.get("uvs") is not None
            else None
        )
        uvs2 = (
            _as_array(primitive, "uvs2", np.float32, 2)
            if primitive.get("uvs2") is not None
            else None
        )
        indices = _as_array(primitive, "indices", np.uint32, None)
        vertex_count = len(positions)
        if vertex_count == 0:
            raise ValueError("primitive has no vertices")
        if uvs2 is not None and uvs is None:
            raise ValueError("lightmap UVs require a base UV channel")
        if (normals is not None and len(normals) != vertex_count) or (
            uvs is not None and len(uvs) != vertex_count
        ):
            raise ValueError("attribute arrays disagree on vertex count")
        if uvs2 is not None and len(uvs2) != vertex_count:
            raise ValueError("lightmap UV array disagrees on vertex count")
        if len(indices) == 0 or len(indices) % 3:
            raise ValueError("index count must be a positive multiple of 3")
        if int(indices.max()) >= vertex_count:
            raise ValueError("index references a missing vertex")

        index_accessor = add_accessor(
            add_view(indices.tobytes(), TARGET_ELEMENT_ARRAY_BUFFER),
            COMPONENT_UINT32,
            len(indices),
            "SCALAR",
        )
        position_accessor = add_accessor(
            add_view(positions.tobytes(), TARGET_ARRAY_BUFFER),
            COMPONENT_FLOAT32,
            vertex_count,
            "VEC3",
            positions.min(axis=0).tolist(),
            positions.max(axis=0).tolist(),
        )
        attributes: dict[str, int] = {"POSITION": position_accessor}
        if normals is not None:
            attributes["NORMAL"] = add_accessor(
                add_view(normals.tobytes(), TARGET_ARRAY_BUFFER),
                COMPONENT_FLOAT32,
                vertex_count,
                "VEC3",
            )
        if uvs is not None:
            attributes["TEXCOORD_0"] = add_accessor(
                add_view(uvs.tobytes(), TARGET_ARRAY_BUFFER),
                COMPONENT_FLOAT32,
                vertex_count,
                "VEC2",
            )
        if uvs2 is not None:
            attributes["TEXCOORD_1"] = add_accessor(
                add_view(uvs2.tobytes(), TARGET_ARRAY_BUFFER),
                COMPONENT_FLOAT32,
                vertex_count,
                "VEC2",
            )
        materials.append({"name": str(primitive["material"])})
        mesh_primitive: dict[str, object] = {
            "attributes": attributes,
            "indices": index_accessor,
            "material": len(materials) - 1,
            "mode": MODE_TRIANGLES,
        }
        # Carry the CoD lightmap page index (or -1 for unlit) as glTF extras so
        # the runtime can bind the matching page texture per primitive. Self-
        # contained per GLB and robust to primitive ordering.
        lightmap_index = primitive.get("lightmapIndex")
        if lightmap_index is not None:
            mesh_primitive["extras"] = {"lightmapIndex": int(lightmap_index)}
        mesh_primitives.append(mesh_primitive)

    document = {
        "asset": {
            "version": "2.0",
            "generator": "FriendsOfDuty fod_glb_writer",
        },
        "scene": 0,
        "scenes": [{"name": node_name, "nodes": [0]}],
        "nodes": [{"name": node_name, "mesh": 0}],
        "meshes": [{"name": node_name, "primitives": mesh_primitives}],
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(binary)}],
    }
    json_blob = json.dumps(
        document, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    json_blob += b" " * (-len(json_blob) % 4)
    binary_blob = bytes(binary)

    total = 12 + 8 + len(json_blob) + 8 + len(binary_blob)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        output.write(struct.pack("<III", GLB_MAGIC, GLB_VERSION, total))
        output.write(struct.pack("<II", len(json_blob), CHUNK_JSON))
        output.write(json_blob)
        output.write(struct.pack("<II", len(binary_blob), CHUNK_BIN))
        output.write(binary_blob)
