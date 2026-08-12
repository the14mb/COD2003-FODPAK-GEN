from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
# The Call of Duty installs live beside the repo, not in it. Kept separate
# from ROOT so a test can never look for game data among the source.
DATA_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "tools"))

import import_cod_multiplayer_maps as maps  # noqa: E402


class RuntimeLampFormulaTests(unittest.TestCase):
    """resolve_runtime_lamps/lamp_attenuation are the single shared Python
    replica of RuntimeMapBuilder's ForceVertex lamp contract; the viewmodel
    grids AND the stage-2 lamp lightmap bake both consume them, so these pins
    are the lockstep guard LAMP_LIGHTING_REWORK_SPEC.md (E) demands."""

    def test_default_lamp_matches_runtime_formulas(self) -> None:
        positions, intensities, ranges_m, colors = maps.resolve_runtime_lamps(
            [
                {
                    "origin": [10.0, -20.0, 30.0],
                    "color": [1.0, 0.5, 0.25],
                    "intensity": 300.0,
                    "overbrightShift": 1.0,
                }
            ],
            "mp_carentan",
        )
        self.assertEqual(positions.shape, (1, 3))
        np.testing.assert_allclose(positions[0], [10.0, -20.0, 30.0])
        # intensity_u = I/50 * 2^(shift-0.8) = 6 * 2^0.2
        self.assertAlmostEqual(
            float(intensities[0]), 6.0 * 2.0 ** 0.2, places=5
        )
        # range_m = sqrt(I) * 0.9, uncapped at 300
        self.assertAlmostEqual(
            float(ranges_m[0]), math.sqrt(300.0) * 0.9, places=5
        )
        np.testing.assert_allclose(colors[0], [1.0, 0.5, 0.25], rtol=1e-6)

    def test_intensity_and_range_clamps(self) -> None:
        _, intensities, ranges_m, _ = maps.resolve_runtime_lamps(
            [
                {"origin": [0, 0, 0], "intensity": 100000.0,
                 "overbrightShift": 1.0},
                {"origin": [0, 0, 0], "intensity": 0.5,
                 "overbrightShift": 1.0},
            ],
            "mp_carentan",
        )
        # Bright lamp: intensity capped at 9, range capped at 24 m off-Pavlov.
        self.assertAlmostEqual(float(intensities[0]), 9.0, places=6)
        self.assertAlmostEqual(float(ranges_m[0]), 24.0, places=6)
        # Dim lamp: I floored to 1 -> 1/50*2^0.2 < 0.3 floor; range floored 3.
        self.assertAlmostEqual(float(intensities[1]), 0.3, places=6)
        self.assertAlmostEqual(float(ranges_m[1]), 3.0, places=6)

    def test_pavlov_uses_the_22m_range_cap(self) -> None:
        _, _, ranges_m, _ = maps.resolve_runtime_lamps(
            [{"origin": [0, 0, 0], "intensity": 100000.0}],
            "MP_PAVLOV",
        )
        self.assertAlmostEqual(float(ranges_m[0]), 22.0, places=6)

    def test_bad_origin_skipped_and_color_fallback(self) -> None:
        positions, _, _, colors = maps.resolve_runtime_lamps(
            [
                {"origin": None, "intensity": 300.0},
                {"origin": [1, 2, 3], "color": [2.0, -1.0, 0.5]},
                {"origin": [4, 5, 6]},
            ],
            "mp_carentan",
        )
        self.assertEqual(len(positions), 2)
        # Colour clamped to [0, 1]; a missing colour takes ReadColor's fallback.
        np.testing.assert_allclose(colors[0], [1.0, 0.0, 0.5], rtol=1e-6)
        np.testing.assert_allclose(
            colors[1], maps.LIGHT_GRID_FALLBACK_COLOR, rtol=1e-6
        )

    def test_attenuation_shape_and_hard_range_cutoff(self) -> None:
        distances = np.array([0.0, 5.0, 9.999, 10.0, 15.0], dtype=np.float32)
        atten = maps.lamp_attenuation(distances, 10.0)
        self.assertAlmostEqual(float(atten[0]), 1.0, places=6)
        # 1 / (1 + 25 * 0.5^2)
        self.assertAlmostEqual(float(atten[1]), 1.0 / 7.25, places=6)
        self.assertGreater(float(atten[2]), 0.0)
        # Hard zero at and beyond range, no tail.
        self.assertEqual(float(atten[3]), 0.0)
        self.assertEqual(float(atten[4]), 0.0)
        self.assertTrue(np.all(np.diff(atten[:3]) < 0.0))


class LightmapTexelRasterizerTests(unittest.TestCase):
    """Texel-center coverage and barycentric interpolation on a known
    triangle; the world positions are laid out to equal the UVs so the
    interpolated position of a covered texel must be its own texel center."""

    DIM = 8

    def _rasterize(self, uv_px, triangles):
        world_positions = np.column_stack(
            (uv_px[:, 0], uv_px[:, 1], np.zeros(len(uv_px)))
        ).astype(np.float32)
        world_normals = np.tile(
            np.array([0.0, 0.0, 1.0], dtype=np.float32), (len(uv_px), 1)
        )
        covered = np.zeros(self.DIM * self.DIM, dtype=bool)
        texel_positions = np.zeros((self.DIM * self.DIM, 3), dtype=np.float32)
        texel_normals = np.zeros((self.DIM * self.DIM, 3), dtype=np.float32)
        written = maps.rasterize_lightmap_texels(
            uv_px,
            world_positions,
            world_normals,
            np.asarray(triangles),
            covered,
            texel_positions,
            texel_normals,
            page_dim=self.DIM,
        )
        return written, covered, texel_positions, texel_normals

    def test_known_triangle_coverage_and_interpolation(self) -> None:
        # Right triangle over the page corner: hypotenuse x + y = 8. Texel
        # centers strictly inside satisfy (x+.5)+(y+.5) < 8, i.e. x+y <= 6;
        # centers with x+y >= 8 are strictly outside. The x+y == 7 row sits
        # exactly on the hypotenuse and is tolerance-dependent, so it is
        # deliberately not pinned.
        uv_px = np.array([[0.0, 0.0], [8.0, 0.0], [0.0, 8.0]])
        written, covered, texel_positions, texel_normals = self._rasterize(
            uv_px, [[0, 1, 2]]
        )
        self.assertGreaterEqual(written, 28)
        for y in range(self.DIM):
            for x in range(self.DIM):
                index = y * self.DIM + x
                if x + y <= 6:
                    self.assertTrue(covered[index], f"texel {x},{y}")
                    # World == UV plane: the interpolated position must land
                    # exactly on the texel center.
                    np.testing.assert_allclose(
                        texel_positions[index],
                        [x + 0.5, y + 0.5, 0.0],
                        atol=1e-5,
                    )
                    np.testing.assert_allclose(
                        texel_normals[index], [0.0, 0.0, 1.0], atol=1e-6
                    )
                elif x + y >= 8:
                    self.assertFalse(covered[index], f"texel {x},{y}")

    def test_degenerate_triangle_covers_nothing(self) -> None:
        uv_px = np.array([[2.0, 2.0], [2.0, 2.0], [2.0, 2.0]])
        written, covered, _, _ = self._rasterize(uv_px, [[0, 1, 2]])
        self.assertEqual(written, 0)
        self.assertFalse(covered.any())

    def test_offpage_uvs_are_clamped_away(self) -> None:
        # A chart hanging off the page edge must only write in-bounds texels:
        # hypotenuse x + y = 4, so coverage stays within x, y <= 3.
        uv_px = np.array([[-4.0, -4.0], [8.0, -4.0], [-4.0, 8.0]])
        written, covered, _, _ = self._rasterize(uv_px, [[0, 1, 2]])
        self.assertGreater(written, 0)
        indices = np.nonzero(covered)[0]
        self.assertTrue(np.all(indices % self.DIM <= 3))
        self.assertTrue(np.all(indices // self.DIM <= 3))


class TriangleOcclusionGridTests(unittest.TestCase):
    """Any-hit queries against a trivial two-triangle wall: the exact case the
    bake depends on — a lamp behind a wall must not light the texel."""

    def _wall_grid(self) -> maps.TriangleOcclusionGrid:
        # Quad at x = 5 spanning y, z in [-10, 10], split on its diagonal.
        vertices = np.array(
            [
                [5.0, -10.0, -10.0],
                [5.0, 10.0, -10.0],
                [5.0, 10.0, 10.0],
                [5.0, -10.0, 10.0],
            ],
            dtype=np.float32,
        )
        triangles = np.array([[0, 1, 2], [0, 2, 3]])
        return maps.TriangleOcclusionGrid(vertices, triangles, cell_size=4.0)

    def test_wall_blocks_and_misses(self) -> None:
        grid = self._wall_grid()
        origins = np.array(
            [
                [0.0, 1.0, 2.0],   # straight through the wall
                [0.0, 1.0, 2.0],   # away from the wall
                [0.0, 20.0, 2.0],  # parallel path beside the wall
                [6.0, 1.0, 2.0],   # starts behind the wall, moves away
                [0.0, 1.0, 2.0],   # segment ends before the wall
            ]
        )
        targets = np.array(
            [
                [10.0, 1.0, 2.0],
                [-10.0, 1.0, 2.0],
                [10.0, 20.0, 2.0],
                [10.0, 1.0, 2.0],
                [4.0, 1.0, 2.0],
            ]
        )
        np.testing.assert_array_equal(
            grid.occluded(origins, targets, stop_short_cod=0.5),
            [True, False, False, False, False],
        )

    def test_stop_short_spares_geometry_at_the_target(self) -> None:
        # The wall sits 1 unit before the target: with a 2-unit stop-short the
        # segment ends in front of it (a fixture polygon around the lamp),
        # with a tiny stop-short it is a real occluder.
        grid = self._wall_grid()
        origins = np.array([[0.0, 1.0, 2.0]])
        targets = np.array([[6.0, 1.0, 2.0]])
        self.assertFalse(
            grid.occluded(origins, targets, stop_short_cod=2.0)[0]
        )
        self.assertTrue(
            grid.occluded(origins, targets, stop_short_cod=0.25)[0]
        )

    def test_empty_soup_never_occludes(self) -> None:
        grid = maps.TriangleOcclusionGrid(
            np.zeros((0, 3), dtype=np.float32),
            np.zeros((0, 3), dtype=np.int64),
        )
        occluded = grid.occluded(
            np.array([[0.0, 0.0, 0.0]]), np.array([[10.0, 0.0, 0.0]])
        )
        np.testing.assert_array_equal(occluded, [False])


class LampPageDilationTests(unittest.TestCase):
    def test_gutter_grows_one_texel_per_pass(self) -> None:
        pixels = np.zeros((5, 5, 3), dtype=np.float32)
        covered = np.zeros((5, 5), dtype=bool)
        pixels[2, 2] = [1.0, 0.5, 0.0]
        covered[2, 2] = True
        maps.dilate_lamp_page(pixels, covered, passes=1)
        # The 8-neighbourhood filled with the sole covered value...
        np.testing.assert_allclose(pixels[1, 1], [1.0, 0.5, 0.0])
        np.testing.assert_allclose(pixels[2, 3], [1.0, 0.5, 0.0])
        self.assertTrue(covered[1:4, 1:4].all())
        # ...and nothing two texels out after a single pass.
        np.testing.assert_allclose(pixels[0, 0], [0.0, 0.0, 0.0])
        self.assertFalse(covered[0, 0])


if __name__ == "__main__":
    unittest.main()
