#!/usr/bin/env python3
"""Focused regression tests for multiplayer gameplay-entity export."""

from __future__ import annotations

import math
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# Entity parsing has no raster/GLB dependency, so keep these focused tests
# runnable on a plain Python install instead of requiring Blender's exporter
# environment just to import the map module.
sys.modules.setdefault("numpy", types.ModuleType("numpy"))
pil_package = sys.modules.setdefault("PIL", types.ModuleType("PIL"))
pil_image = sys.modules.setdefault("PIL.Image", types.ModuleType("PIL.Image"))
pil_package.Image = pil_image
sky_module = types.ModuleType("build_pavlov_sky_panorama")
sky_module.build = lambda *args, **kwargs: None
sys.modules.setdefault("build_pavlov_sky_panorama", sky_module)
decal_module = types.ModuleType("fod_decal_alpha")
decal_module.build_alpha = lambda *args, **kwargs: None
sys.modules.setdefault("fod_decal_alpha", decal_module)
glb_module = types.ModuleType("fod_glb_writer")
glb_module.write_glb = lambda *args, **kwargs: None
sys.modules.setdefault("fod_glb_writer", glb_module)
policy_module = types.ModuleType("cod1_archive_policy")
policy_module.COD1_TIER = "cod1"
policy_module.UO_TIER = "uo"
policy_module.official_archives = lambda *args, **kwargs: []
sys.modules.setdefault("cod1_archive_policy", policy_module)

import import_cod_multiplayer_maps as importer


class MultiplayerGameplayEntityExportTests(unittest.TestCase):
    @staticmethod
    def bsp_with_lights(
        records: list[
            tuple[
                int,
                tuple[float, float, float],
                tuple[float, float, float],
            ]
        ],
    ) -> bytes:
        header_size = 8 + 32 * 8
        lights = bytearray(importer.LIGHT_LUMP_STRIDE * len(records))
        for index, (light_type, compiled_color, direction) in enumerate(
            records
        ):
            offset = index * importer.LIGHT_LUMP_STRIDE
            struct.pack_into("<i", lights, offset, light_type)
            struct.pack_into("<3f", lights, offset + 4, *compiled_color)
            struct.pack_into("<3f", lights, offset + 28, *direction)
        bsp = bytearray(header_size + len(lights))
        bsp[:4] = b"IBSP"
        struct.pack_into("<i", bsp, 4, importer.SUPPORTED_VERSION)
        struct.pack_into(
            "<ii",
            bsp,
            8 + importer.LIGHT_LUMP_INDEX * 8,
            len(lights),
            header_size,
        )
        bsp[header_size:] = lights
        return bytes(bsp)

    def test_compiled_directional_light_becomes_exact_noon_anchor(
        self,
    ) -> None:
        pitch = -30.0
        yaw = 40.0
        pitch_radians = math.radians(pitch)
        yaw_radians = math.radians(yaw)
        horizontal = math.cos(pitch_radians)
        direction_cod = (
            horizontal * math.cos(yaw_radians),
            horizontal * math.sin(yaw_radians),
            -math.sin(pitch_radians),
        )
        compiled_color = (0.75, 0.76, 0.77)
        bsp = self.bsp_with_lights(
            [
                (2, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)),
                (
                    importer.DIRECTIONAL_LIGHT_TYPE,
                    compiled_color,
                    direction_cod,
                ),
            ]
        )

        authored = importer.parse_authored_sun(
            bsp,
            {"sundirection": "-30 40 0"},
        )
        environment = importer.map_environment(
            {"sundirection": "-30 40 0"},
            None,
            authored,
        )

        self.assertEqual(authored["source"], "bsp-lump-30")
        self.assertEqual(authored["startHour"], 12.0)
        self.assertAlmostEqual(authored["heading"], 50.0, places=5)
        self.assertAlmostEqual(authored["arcPeak"], 30.0, places=5)
        for actual, expected in zip(
            authored["compiledColor"],
            compiled_color,
        ):
            self.assertAlmostEqual(actual, expected, places=6)
        for actual, expected in zip(
            authored["directionUnity"],
            (-direction_cod[0], direction_cod[2], -direction_cod[1]),
        ):
            self.assertAlmostEqual(actual, expected, places=6)
        self.assertIs(environment["authoredSun"], authored)

    def test_compiled_directional_light_validation_fails_closed(
        self,
    ) -> None:
        valid_direction = (1.0, 0.0, 0.0)
        with self.subTest("duplicate"):
            bsp = self.bsp_with_lights(
                [
                    (1, (1.0, 1.0, 1.0), valid_direction),
                    (1, (1.0, 1.0, 1.0), valid_direction),
                ]
            )
            with self.assertRaisesRegex(ValueError, "exactly one"):
                importer.parse_authored_sun(
                    bsp,
                    {"sundirection": "0 0 0"},
                )

        with self.subTest("not normalized"):
            bsp = self.bsp_with_lights(
                [(1, (1.0, 1.0, 1.0), (2.0, 0.0, 0.0))]
            )
            with self.assertRaisesRegex(ValueError, "not normalized"):
                importer.parse_authored_sun(
                    bsp,
                    {"sundirection": "0 0 0"},
                )

        with self.subTest("worldspawn disagreement"):
            bsp = self.bsp_with_lights(
                [(1, (1.0, 1.0, 1.0), valid_direction)]
            )
            with self.assertRaisesRegex(ValueError, "disagrees"):
                importer.parse_authored_sun(
                    bsp,
                    {"sundirection": "0 90 0"},
                )

    def test_collision_only_ladder_brush_keeps_exact_bounds(self) -> None:
        header_size = 8 + 32 * 8
        materials = bytearray(importer.MATERIAL_LUMP_STRIDE * 2)
        materials[
            importer.MATERIAL_LUMP_STRIDE :
            importer.MATERIAL_LUMP_STRIDE + 64
        ] = b"textures/common/ladder_wood".ljust(64, b"\0")
        sides = bytearray(importer.BRUSH_SIDE_LUMP_STRIDE * 6)
        distances = (-64.0, -56.0, 128.0, 144.0, 12.0, 108.0)
        for index, distance in enumerate(distances):
            plane_bits = struct.unpack(
                "<I",
                struct.pack("<f", distance),
            )[0]
            struct.pack_into(
                "<II",
                sides,
                index * importer.BRUSH_SIDE_LUMP_STRIDE,
                plane_bits,
                1,
            )
        brushes = struct.pack("<HH", 6, 1)
        payloads = {
            importer.MATERIAL_LUMP_INDEX: bytes(materials),
            importer.BRUSH_SIDE_LUMP_INDEX: bytes(sides),
            importer.BRUSH_LUMP_INDEX: brushes,
        }
        cursor = header_size
        bsp = bytearray(header_size + sum(map(len, payloads.values())))
        bsp[:4] = b"IBSP"
        struct.pack_into("<i", bsp, 4, importer.SUPPORTED_VERSION)
        for lump_index, payload in payloads.items():
            struct.pack_into(
                "<ii",
                bsp,
                8 + lump_index * 8,
                len(payload),
                cursor,
            )
            bsp[cursor : cursor + len(payload)] = payload
            cursor += len(payload)

        result = importer.ladder_brush_volumes(bytes(bsp))

        self.assertEqual(
            result,
            [
                {
                    "sourceBrushIndex": 0,
                    "sourceMaterial": "textures/common/ladder_wood",
                    "center": [-60.0, 136.0, 60.0],
                    "size": [8.0, 16.0, 96.0],
                }
            ],
        )

    def test_model_lump_produces_exact_trigger_bounds(self) -> None:
        header_size = 8 + 32 * 8
        model_bytes = bytearray(importer.MODEL_LUMP_STRIDE * 2)
        struct.pack_into(
            "<6f6i",
            model_bytes,
            importer.MODEL_LUMP_STRIDE,
            -64.0,
            -32.0,
            8.0,
            96.0,
            48.0,
            72.0,
            4,
            5,
            6,
            7,
            8,
            9,
        )
        bsp = bytearray(header_size + len(model_bytes))
        bsp[:4] = b"IBSP"
        struct.pack_into("<i", bsp, 4, importer.SUPPORTED_VERSION)
        struct.pack_into(
            "<ii",
            bsp,
            8 + importer.MODEL_LUMP_INDEX * 8,
            len(model_bytes),
            header_size,
        )
        bsp[header_size:] = model_bytes

        models = importer.parse_brush_models(bytes(bsp))
        bounds = importer.brush_model_bounds({"model": "*1"}, models)

        self.assertEqual(bounds, ([16.0, 8.0, 40.0], [160.0, 80.0, 64.0]))
        self.assertEqual(models[1]["firstBrush"], 8)
        self.assertEqual(models[1]["brushCount"], 9)

    def test_weapon_pickup_id_comes_from_official_radiant_name(self) -> None:
        entry = importer.ArchiveEntry(Path("official.pk3"), "weapon", 0)
        with mock.patch.object(
            importer,
            "read_entry",
            return_value=b"\\radiantName\\mpweapon_mp40\\displayName\\MP40",
        ):
            result = importer.runtime_weapon_ids(
                {"weapons/mp/mp40_mp": entry}
            )

        self.assertEqual(result, {"mpweapon_mp40": "mp40"})

    def test_arena_gametypes_preserve_official_order_and_uniqueness(self) -> None:
        entry = importer.ArchiveEntry(Path("official.pk3"), "arena", 0)
        # BYTE-EXACT retail shape, straight out of Main/paka.pk3 and
        # uo/pakuo01.pk3 mp/CoDmaps.arena: CRLF line endings, hard tabs, and
        # the key authored BARE with only the value quoted. The previous
        # fixture used Quake 3's fully-quoted `"key" "value"` form, which
        # appears in no shipped .arena — so this test passed while the parser
        # matched zero real records and every map silently lost bel/hq.
        arena = (
            b'{\r\n\tmap\t\t"mp_pavlov"\r\n'
            b'\tlongname\t"mp_pavlov"\r\n'
            b'\tgametype\t"dm tdm sd re bel hq"\r\n}\r\n\r\n'
            b'{\r\n\tmap\t\t"mp_cassino"\r\n'
            b'\tlongname\t"mp_cassino"\r\n'
            b'\tgametype\t"dom ctf dm tdm re dm sd bel hq"\r\n}\r\n'
        )
        with mock.patch.object(importer, "read_entry", return_value=arena):
            result = importer.parse_arena_game_types(
                {"mp/codmaps.arena": entry}
            )

        self.assertEqual(
            result["mp_pavlov"],
            ["dm", "tdm", "sd", "re", "bel", "hq"],
        )
        # Authoring order preserved, the repeated "dm" collapsed.
        self.assertEqual(
            result["mp_cassino"],
            ["dom", "ctf", "dm", "tdm", "re", "sd", "bel", "hq"],
        )
        # longname must never be mistaken for a gametype key.
        self.assertNotIn("longname", result)

    def test_manifest_keeps_source_identity_and_exact_brush_bounds(self) -> None:
        models = [
            {"minimum": [0.0, 0.0, 0.0], "maximum": [0.0, 0.0, 0.0]},
            {
                "minimum": [-8.0, -4.0, 2.0],
                "maximum": [8.0, 12.0, 18.0],
            },
        ]
        entities = [
            {"classname": "worldspawn"},
            {
                "classname": "trigger_hurt",
                "model": "*1",
                "dmg": "25",
                "targetname": "lava",
            },
            {
                "classname": "mpweapon_mp40",
                "origin": "1 2 3",
            },
        ]
        with mock.patch.object(
            importer,
            "runtime_weapon_ids",
            return_value={"mpweapon_mp40": "mp40"},
        ):
            result = importer.gameplay_entity_manifest(
                entities,
                models,
                {},
                Path("."),
                "maps/mp_test/ambience",
            )

        self.assertEqual(result[0]["sourceEntityIndex"], 1)
        self.assertEqual(result[0]["boundsCenter"], [0.0, 4.0, 10.0])
        self.assertEqual(result[0]["boundsSize"], [16.0, 16.0, 16.0])
        self.assertEqual(result[0]["damage"], 25.0)
        self.assertEqual(result[1]["sourceEntityIndex"], 2)
        self.assertEqual(result[1]["weaponId"], "mp40")

    def test_cut_gametype_objectives_never_reach_the_manifest(self) -> None:
        entities = [
            {"classname": "worldspawn"},
            # Every objective identity the stock gametype scripts use.
            {
                "classname": "trigger_multiple",
                "script_gameobjectname": "bombzone",
                "origin": "1 0 0",
            },
            {
                "classname": "trigger_lookat",
                "script_gameobjectname": "bombzone",
                "origin": "2 0 0",
            },
            {
                "classname": "trigger_use",
                "script_gameobjectname": "re",
                "origin": "3 0 0",
            },
            {
                "classname": "script_model",
                "targetname": "retrieval_objective",
                "origin": "4 0 0",
            },
            {"classname": "mp_retrieval_objective", "origin": "5 0 0"},
            {
                "classname": "script_model",
                "targetname": "hqradio",
                "origin": "6 0 0",
            },
            {"classname": "mp_gmi_ctf_flag", "origin": "7 0 0"},
            {
                "classname": "trigger_multiple",
                "script_gameobjectname": "ctf",
                "origin": "8 0 0",
            },
            # _gameobjects splits script_gameobjectname on whitespace, so a
            # shared entity must be refused on either token.
            {
                "classname": "trigger_multiple",
                "script_gameobjectname": "dom retrieval",
                "origin": "9 0 0",
            },
            {
                "classname": "trigger_multiple",
                "script_gameobjectname": "flag_cap",
                "origin": "10 0 0",
            },
        ]

        with mock.patch.object(
            importer,
            "runtime_weapon_ids",
            return_value={},
        ):
            result = importer.gameplay_entity_manifest(
                entities,
                [],
                {},
                Path("."),
                "maps/mp_test/ambience",
            )

        self.assertEqual(result, [])

    def test_deathmatch_entities_and_exploders_outrank_objective_names(
        self,
    ) -> None:
        entities = [
            {"classname": "worldspawn"},
            {"classname": "trigger_hurt", "dmg": "25", "origin": "1 0 0"},
            {
                "classname": "trigger_multiple",
                "targetname": "minefield",
                "origin": "2 0 0",
            },
            {"classname": "mpweapon_mp40", "origin": "3 0 0"},
            {"classname": "misc_mg42", "origin": "4 0 0"},
            # mp_pavlov's destroyable antitank-rifle set pieces are
            # script_models the mapper NAMED "bombzone"; their real identity
            # is the exploder id, and they are Deathmatch scenery.
            {
                "classname": "script_model",
                "script_gameobjectname": "bombzone",
                "script_exploder": "1",
                "origin": "5 0 0",
            },
            {
                "classname": "trigger_multiple",
                "script_gameobjectname": "bombzone",
                "script_exploder": "2",
                "origin": "6 0 0",
            },
        ]

        with mock.patch.object(
            importer,
            "runtime_weapon_ids",
            return_value={"mpweapon_mp40": "mp40"},
        ):
            result = importer.gameplay_entity_manifest(
                entities,
                [],
                {},
                Path("."),
                "maps/mp_test/ambience",
            )

        self.assertEqual(
            [record["sourceEntityIndex"] for record in result],
            [1, 2, 3, 4, 5, 6],
        )

    def test_catalog_gametypes_narrow_to_the_shipped_modes(self) -> None:
        entry = importer.ArchiveEntry(Path("official.pk3"), "arena", 0)
        arena = (
            b'{\r\n\tmap\t\t"mp_cassino"\r\n'
            b'\tlongname\t"mp_cassino"\r\n'
            b'\tgametype\t"dom ctf dm tdm re sd bel hq"\r\n}\r\n'
        )
        with mock.patch.object(importer, "read_entry", return_value=arena):
            result = importer.supported_game_types(
                "mp_cassino",
                [],
                {"mp/codmaps.arena": entry},
            )

        self.assertEqual(result, ["dm", "tdm"])

    def test_map_fingerprint_is_bound_to_multiplayer_gsc(self) -> None:
        entry = importer.ArchiveEntry(Path("pak8.pk3"), "maps/mp/test.bsp", 4)

        first = importer.source_fingerprint(
            entry,
            b"same-bsp",
            'radio.origin = (1, 2, 3);',
        )
        second = importer.source_fingerprint(
            entry,
            b"same-bsp",
            'radio.origin = (4, 5, 6);',
        )

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
