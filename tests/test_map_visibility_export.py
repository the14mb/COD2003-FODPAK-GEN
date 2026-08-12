from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
# The Call of Duty installs live beside the repo, not in it. Kept separate
# from ROOT so a test can never look for game data among the source.
DATA_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "exporter"))

import import_cod_multiplayer_maps as maps  # noqa: E402
import package as fod_package  # noqa: E402
import pipeline as fod_pipeline  # noqa: E402


def synthetic_bsp(*, bad_pvs_diagonal: bool = False) -> bytes:
    """Two visible leaves/cells split by x=0, with one PVS row each."""
    planes = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    cells = b"".join(
        (
            struct.pack(
                "<6f7i",
                0.0,
                -10.0,
                -10.0,
                10.0,
                10.0,
                10.0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ),
            struct.pack(
                "<6f7i",
                -10.0,
                -10.0,
                -10.0,
                0.0,
                10.0,
                10.0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ),
        )
    )
    nodes = struct.pack(
        "<3i6i",
        0,
        -1,
        -2,
        -10,
        -10,
        -10,
        10,
        10,
        10,
    )
    # cluster, area, firstSurface, surfaceCount, firstBrush, brushCount,
    # graphicsCell, firstLight, lightCount.
    leaves = b"".join(
        (
            struct.pack("<9i", 0, 0, 0, 0, 0, 0, 0, 0, 0),
            struct.pack("<9i", 1, 0, 0, 0, 0, 0, 1, 0, 0),
        )
    )
    leaf_surfaces = struct.pack("<I", 0)
    pvs = bytearray((1, 0, 0, 0, 2, 0, 0, 0))
    if bad_pvs_diagonal:
        pvs[4] = 0
    visibility = struct.pack("<ii", 2, 4) + bytes(pvs)
    payloads = {
        maps.PLANE_LUMP_INDEX: planes,
        maps.CELL_LUMP_INDEX: cells,
        maps.NODE_LUMP_INDEX: nodes,
        maps.LEAF_LUMP_INDEX: leaves,
        maps.LEAF_SURFACE_LUMP_INDEX: leaf_surfaces,
        maps.VISIBILITY_LUMP_INDEX: visibility,
    }
    header_size = 8 + 31 * 8
    data = bytearray(header_size)
    data[:4] = b"IBSP"
    struct.pack_into("<i", data, 4, maps.SUPPORTED_VERSION)
    for index, payload in sorted(payloads.items()):
        offset = len(data)
        struct.pack_into("<ii", data, 8 + index * 8, len(payload), offset)
        data.extend(payload)
    return bytes(data)


def optimization_document() -> dict[str, object]:
    return {
        "format": maps.MAP_OPTIMIZATION_FORMAT,
        "version": maps.MAP_OPTIMIZATION_VERSION,
        "sourceBspVersion": 59,
        "codUnitToMetre": maps.COD_UNIT_TO_METRE,
        "sectorStrategy": "unity-xz-grid-v1",
        "sectorSizeMetres": maps.RENDER_SECTOR_SIZE_METRES,
        "fallbackWorldGlb": "maps/cod1/mp_test/world.glb",
        "sectors": [
            {
                "name": "grid_m001_m001",
                "glb": "maps/cod1/mp_test/sectors/grid_m001_m001.glb",
                "gridX": -1,
                "gridZ": -1,
                "clusterIndices": [0],
                "alwaysVisible": False,
                "triangles": 1,
                "boundsCodUnits": {
                    "minimum": [1.0, 0.0, 0.0],
                    "maximum": [2.0, 1.0, 1.0],
                },
            },
            {
                "name": "always_visible",
                "glb": "maps/cod1/mp_test/sectors/always_visible.glb",
                "gridX": 0,
                "gridZ": 0,
                "clusterIndices": [],
                "alwaysVisible": True,
                "triangles": 1,
                "boundsCodUnits": {
                    "minimum": [-1.0, 0.0, 0.0],
                    "maximum": [1.0, 1.0, 1.0],
                },
            },
        ],
        "visibility": {
            "clusterCount": 2,
            "rowBytes": 4,
            "pvsBase64": base64.b64encode(
                bytes((1, 0, 0, 0, 2, 0, 0, 0))
            ).decode("ascii"),
            "planeCount": 1,
            "planesBase64": base64.b64encode(
                struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
            ).decode("ascii"),
            "nodeCount": 1,
            "nodesBase64": base64.b64encode(
                struct.pack("<3i", 0, -1, -2)
            ).decode("ascii"),
            "leafCount": 2,
            "leavesBase64": base64.b64encode(
                struct.pack("<4i", 0, 0, 1, 1)
            ).decode("ascii"),
            "cells": [
                {
                    "minimum": [0.0, -10.0, -10.0],
                    "maximum": [10.0, 10.0, 10.0],
                },
                {
                    "minimum": [-10.0, -10.0, -10.0],
                    "maximum": [0.0, 10.0, 10.0],
                },
            ],
        },
        "counts": {
            "sectors": 2,
            "triangles": 2,
            "alwaysVisibleTriangles": 1,
            "clusters": 2,
            "cells": 2,
        },
    }


def write_test_glb(path: Path, triangle_count: int) -> None:
    positions = np.zeros((triangle_count * 3, 3), dtype=np.float32)
    for triangle in range(triangle_count):
        vertex = triangle * 3
        positions[vertex] = (float(triangle), 0.0, 0.0)
        positions[vertex + 1] = (float(triangle) + 0.5, 1.0, 0.0)
        positions[vertex + 2] = (float(triangle) + 1.0, 0.0, 0.0)
    maps.write_glb(
        path,
        [
            {
                "material": "test",
                "positions": positions,
                "normals": np.tile(
                    np.asarray((0.0, 0.0, 1.0), dtype=np.float32),
                    (triangle_count * 3, 1),
                ),
                "uvs": np.zeros(
                    (triangle_count * 3, 2),
                    dtype=np.float32,
                ),
                "indices": np.arange(
                    triangle_count * 3,
                    dtype=np.uint32,
                ),
            }
        ],
    )


class BspVisibilityExportTests(unittest.TestCase):
    def test_v59_parser_keeps_clusters_and_graphics_cells_distinct(self) -> None:
        visibility = maps.parse_bsp_visibility(synthetic_bsp())

        self.assertEqual(visibility.cluster_count, 2)
        self.assertEqual(len(visibility.cells), 2)
        self.assertEqual(visibility.leaf_clusters, (0, 1))
        self.assertEqual(visibility.leaf_cells, (0, 1))
        self.assertEqual(
            maps.locate_bsp_leaf(visibility, (2.0, 0.0, 0.0)),
            0,
        )
        self.assertEqual(
            maps.locate_bsp_leaf(visibility, (-2.0, 0.0, 0.0)),
            1,
        )

    def test_parser_rejects_pvs_row_that_cannot_see_itself(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot see itself"):
            maps.parse_bsp_visibility(
                synthetic_bsp(bad_pvs_diagonal=True)
            )

    def test_exact_polygon_clipping_collects_every_spanned_cluster(
        self,
    ) -> None:
        visibility = maps.parse_bsp_visibility(synthetic_bsp())
        positions = np.asarray(
            (
                (2.0, 0.0, 0.0),
                (3.0, 1.0, 0.0),
                (3.0, 0.0, 1.0),
                (-2.0, 0.0, 0.0),
            ),
            dtype=np.float32,
        )

        self.assertEqual(
            maps.triangle_visibility_clusters(
                visibility,
                positions,
                np.asarray((0, 1, 2)),
            ),
            (0,),
        )
        self.assertEqual(
            maps.triangle_visibility_clusters(
                visibility,
                positions,
                np.asarray((0, 1, 3)),
            ),
            (0, 1),
        )
        # The compatibility helper cannot reduce a cross-cell face to one
        # graphics cell, but the optimized sector retains both clusters.
        self.assertIsNone(
            maps.triangle_visibility_cell(
                visibility,
                positions,
                np.asarray((0, 1, 3)),
            )
        )

    def test_partition_emits_every_triangle_exactly_once(self) -> None:
        visibility = maps.parse_bsp_visibility(synthetic_bsp())
        positions = np.asarray(
            (
                (2.0, 0.0, 0.0),
                (3.0, 1.0, 0.0),
                (3.0, 0.0, 1.0),
                (-2.0, 0.0, 0.0),
            ),
            dtype=np.float32,
        )
        sectors = maps.partition_world_render_sectors(
            visibility,
            ["textures/test/stone"],
            [
                (
                    0,
                    maps.LIGHTMAP_UNLIT_PAGE,
                    np.asarray(
                        ((0, 1, 2), (0, 1, 3)),
                        dtype=np.int64,
                    ),
                )
            ],
            positions,
        )

        self.assertEqual(set(sectors), {(-1, -1)})
        sector = sectors[(-1, -1)]
        self.assertEqual(sector.cluster_indices, (0, 1))
        # Faces are grouped by (material index, lightmap page) so each GLB
        # primitive binds one page.
        self.assertEqual(
            len(sector.faces[(0, maps.LIGHTMAP_UNLIT_PAGE)][0]),
            2,
        )

    def test_package_validator_checks_sector_asset_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            relative = Path("maps/cod1/mp_test/optimization.json")
            document = optimization_document()
            path = content / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(document), encoding="utf-8")
            for sector in document["sectors"]:
                glb = content / sector["glb"]
                write_test_glb(glb, sector["triangles"])
            write_test_glb(
                content / document["fallbackWorldGlb"],
                document["counts"]["triangles"],
            )

            errors: list[str] = []
            fod_package.validate_map_optimization(
                content,
                relative.as_posix(),
                "mp_test",
                "maps/cod1/mp_test/world.glb",
                errors,
            )
            self.assertEqual(errors, [])
            config = fod_pipeline.PipelineConfig(
                game_dir=content,
                content_dir=content,
            )
            catalog_entry = {
                "optimizationJson": relative.as_posix(),
                "worldGlb": document["fallbackWorldGlb"],
            }
            self.assertTrue(
                fod_pipeline._map_optimization_done(
                    config,
                    catalog_entry,
                )
            )

            write_test_glb(
                content / document["sectors"][0]["glb"],
                2,
            )
            errors = []
            fod_package.validate_map_optimization(
                content,
                relative.as_posix(),
                "mp_test",
                "maps/cod1/mp_test/world.glb",
                errors,
            )
            self.assertTrue(
                any("contains 2 triangles" in error for error in errors)
            )
            self.assertFalse(
                fod_pipeline._map_optimization_done(
                    config,
                    catalog_entry,
                )
            )
            write_test_glb(
                content / document["sectors"][0]["glb"],
                1,
            )

            original_second_glb = document["sectors"][1]["glb"]
            document["sectors"][1]["glb"] = (
                document["sectors"][0]["glb"]
            )
            path.write_text(json.dumps(document), encoding="utf-8")
            errors = []
            fod_package.validate_map_optimization(
                content,
                relative.as_posix(),
                "mp_test",
                "maps/cod1/mp_test/world.glb",
                errors,
            )
            self.assertTrue(
                any("invalid identity/counts" in error for error in errors)
            )
            self.assertFalse(
                fod_pipeline._map_optimization_done(
                    config,
                    catalog_entry,
                )
            )
            document["sectors"][1]["glb"] = original_second_glb
            path.write_text(json.dumps(document), encoding="utf-8")

            (content / document["sectors"][0]["glb"]).unlink()
            errors = []
            fod_package.validate_map_optimization(
                content,
                relative.as_posix(),
                "mp_test",
                "maps/cod1/mp_test/world.glb",
                errors,
            )
            self.assertTrue(
                any("GLB is missing" in error for error in errors)
            )


SHADER_SCRIPT_FIXTURE = """\
textures/normandy/ground/a_grass1b
{
    surfaceparm grass
    polygonOffset
    qer_editorimage textures/normandy/ground/grass@lp_brecort1b.tga
    {
        map textures/normandy/ground/grass@lp_brecort1b
        rgbGen exactVertex
        alphagen vertex
        blendFunc blend
    nextbundle
        map $lightmap
        blendFunc filter
    }
}

textures/holland/terrain/blend_rubble2b
{
    polygonOffset
    qer_editorimage textures/holland/terrain/rubble2b.tga
    {
        map textures/holland/terrain/rubble2b.tga
        rgbGen exactVertex
        alphagen vertex
        blendFunc blend
    nextbundle
        map $lightmap
        blendFunc filter
    }
}

textures/sfx/facade_tiler2
{
    {
        map clamp textures/sfx/facade_tiler2
        rgbGen vertex
        alphaGen vertex
        alphaFunc GE128
        depthWrite
    nextbundle
        map $lightmap
    }
}

textures/normandy/decals/128windowborder01
{
    surfaceparm nonsolid
    polygonOffset
    {
        map textures/normandy/decals/128windowborder01
        rgbGen exactVertex
        alphaFunc GE128
    nextbundle
        map $lightmap
    }
}
"""


def write_shader_fixture_archive(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("scripts/fixture.shader", SHADER_SCRIPT_FIXTURE)
        archive.writestr(
            "textures/normandy/ground/grass@lp_brecort1b.dds", b"dds"
        )
        archive.writestr("textures/holland/terrain/rubble2b.dds", b"dds")


class ShaderScriptTextureResolutionTests(unittest.TestCase):
    """Shader-script image indirection and stanza-derived alpha tests.

    Some BSP materials exist only as shader-script names: no pk3 ships an
    image called textures/normandy/ground/a_grass1b — its stanza names
    grass@lp_brecort1b instead. Before the indirection tier those groups
    shipped texture:"" and the runtime painted the 'grass' fallback, which
    is exactly Carentan's plain-green floor bug.
    """

    def build_index(self, directory: Path) -> dict[str, object]:
        archive = directory / "fixture.pk3"
        write_shader_fixture_archive(archive)
        return maps.layered_index([archive])

    def test_stanza_indirection_resolves_referenced_diffuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = self.build_index(Path(temporary))
            shader_images = maps.shader_diffuse_images(index)

            self.assertEqual(
                shader_images["normandy/ground/a_grass1b"],
                "textures/normandy/ground/grass@lp_brecort1b",
            )
            self.assertIsNone(
                maps.resolve_texture(
                    "textures/normandy/ground/a_grass1b",
                    index,
                )
            )
            resolved = maps.resolve_texture(
                "textures/normandy/ground/a_grass1b",
                index,
                shader_images,
            )
            self.assertIsNotNone(resolved)
            self.assertEqual(
                resolved.name,
                "textures/normandy/ground/grass@lp_brecort1b.dds",
            )

    def test_referenced_extension_is_dropped_for_dds_preference(
        self,
    ) -> None:
        """gmi_terrain.shader writes `map .../rubble2b.tga` while the image
        ships as rubble2b.dds; the reference must re-enter the candidate
        machinery without its authored extension."""
        with tempfile.TemporaryDirectory() as temporary:
            index = self.build_index(Path(temporary))
            shader_images = maps.shader_diffuse_images(index)

            resolved = maps.resolve_texture(
                "textures/holland/terrain/blend_rubble2b",
                index,
                shader_images,
            )
            self.assertIsNotNone(resolved)
            self.assertEqual(
                resolved.name,
                "textures/holland/terrain/rubble2b.dds",
            )

    def test_alpha_test_derivation_requires_texture_alpha(self) -> None:
        """alphaFunc GE128 flags a material only when the tested alpha comes
        from the texture: `alphaGen vertex` stanzas tested vertex alpha the
        export does not carry, and clipping Railyard's painted facade
        backdrops by texture alpha instead would punch wrong holes."""
        with tempfile.TemporaryDirectory() as temporary:
            index = self.build_index(Path(temporary))
            tested = maps.alpha_tested_materials(index)

            self.assertIn("normandy/decals/128windowborder01", tested)
            self.assertNotIn("sfx/facade_tiler2", tested)
            self.assertNotIn("normandy/ground/a_grass1b", tested)


@unittest.skipUnless(
    (DATA_ROOT / "Call of Duty" / "Main").is_dir(),
    "requires the original Call of Duty pk3 archives",
)
class ShippingArchiveTextureResolutionTests(unittest.TestCase):
    """The indirection tier against the real pk3s: exactly the groups the
    Carentan plain-green bug named must resolve, and the explicitly
    out-of-scope groups must keep their current behaviour. No blanket
    no-empty-textures assertion — sky shells, ladder collision and Arnhem's
    water legitimately ship without a diffuse."""

    GAME_ROOT = DATA_ROOT / "Call of Duty"

    # material -> the pk3 image its shader stanza references, per tier.
    COD1_INDIRECTIONS = {
        "textures/normandy/ground/a_grass1b":
            "textures/normandy/ground/grass@lp_brecort1b.dds",
        "textures/normandy/ground/dirt_earthbase":
            "textures/normandy/ground/dirt@earthbasenoise_lpa.dds",
        "textures/normandy/walls/paper_fleur_wcoldpapr1_nodlight":
            "textures/normandy/walls/paper@fleur_wcoldpapr1.dds",
    }
    UO_INDIRECTIONS = {
        "textures/holland/terrain/blend_rubble2b":
            "textures/holland/terrain/rubble2b.dds",
        "textures/italy/terrain/ground-04b_blend":
            "textures/italy/terrain/dirt@ground-04b.dds",
        "textures/italy/terrain/ground-05_blend":
            "textures/italy/terrain/grass@ground-05.dds",
        "textures/italy/terrain/cliff_wall-04_blend":
            "textures/italy/terrain/rock@cliff_wall-04.dds",
        "textures/ukraine/terrain/grass_ponyri_01_blend":
            "textures/ukraine/terrain/grass@ponyri_01.dds",
    }

    def resolve_tier(self, game: str, expectations: dict[str, str]) -> None:
        index = maps.layered_index(
            maps.official_archives(self.GAME_ROOT, game)
        )
        shader_images = maps.shader_diffuse_images(index)
        for material, image in expectations.items():
            with self.subTest(material=material):
                self.assertIsNone(
                    maps.resolve_texture(material, index),
                    "expected a shader-script-only material",
                )
                resolved = maps.resolve_texture(
                    material,
                    index,
                    shader_images,
                )
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved.name, image)

    def test_carentan_and_chateau_blend_layers_resolve(self) -> None:
        self.resolve_tier("cod1", self.COD1_INDIRECTIONS)

    def test_arnhem_and_cassino_blend_layers_resolve(self) -> None:
        self.resolve_tier("uo", self.UO_INDIRECTIONS)

    def test_railyard_facades_stay_unflagged(self) -> None:
        """Every sfx/facade_* stanza tests vertex alpha, so the GE128
        derivation must leave Railyard's backdrops opaque."""
        index = maps.layered_index(
            maps.official_archives(self.GAME_ROOT, "cod1")
        )
        tested = maps.alpha_tested_materials(index)
        for facade in (
            "sfx/facade_tiler1",
            "sfx/facade_tiler2",
            "sfx/facade_tiler2top",
            "sfx/facade_noper2",
        ):
            self.assertNotIn(facade, tested)
        self.assertIn("normandy/decals/128windowborder01", tested)

    def test_arnhem_water_stays_out_of_scope(self) -> None:
        """sfx/arnhemwater's stanza references base-game damwater.jpg, which
        the UO archive tier does not carry — the water group keeps its empty
        texture until it gets real water handling."""
        index = maps.layered_index(
            maps.official_archives(self.GAME_ROOT, "uo")
        )
        shader_images = maps.shader_diffuse_images(index)
        self.assertIsNone(
            maps.resolve_texture(
                "textures/sfx/arnhemwater",
                index,
                shader_images,
            )
        )


if __name__ == "__main__":
    unittest.main()
