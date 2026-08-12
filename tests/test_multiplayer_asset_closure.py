from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The Call of Duty installs and the extracted/derived content trees live
# beside the repo, not in it. Kept separate from ROOT so a test can never
# look for game data among the source.
DATA_ROOT = ROOT.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from cod1_archive_policy import (  # noqa: E402
    LayeredArchiveIndex,
    selected_archives,
)
from cod1_multiplayer_closure import (  # noqa: E402
    build_map_model_closure,
    build_weapon_player_closure,
    collect_player_animation_source,
    collect_mounted_mg42_source,
    collect_weapon_source,
    MOUNTED_MG42_ANIMATIONS,
    MOUNTED_MG42_WEAPON_FILES,
    parse_xmodel_v14_dependencies,
)
from cod1_playeranim import MG42_HORIZONTAL, MG42_VERTICAL  # noqa: E402
from cod1_script_exploder import (  # noqa: E402
    ScriptExploderClosureError,
    build_effect_closure,
    effect_registrations,
    map_exploder_effects,
)
from import_cod_multiplayer_maps import (  # noqa: E402
    exploder_brush_assets,
    gameplay_entity_manifest,
    layered_index,
    parse_entities,
    read_entry,
)
from exporter.package import (  # noqa: E402
    BASE_SCRIPT_EXPLODER_EFFECT_IDS,
    SCRIPT_EXPLODER_CLOSURE_COUNTS,
    UO_SCRIPT_EXPLODER_EFFECT_IDS,
    validate_map_script_exploders,
)


@dataclass(frozen=True)
class FakeReference:
    name: str
    archive: Path = Path("/retail/Main/pak0.pk3")


class FakeIndex:
    def __init__(self, members: dict[str, bytes]):
        self.payloads = {
            name.replace("\\", "/").casefold(): data
            for name, data in members.items()
        }
        self.members = {
            key: FakeReference(name)
            for name in members
            for key in (name.replace("\\", "/").casefold(),)
        }

    def get(self, member: str):
        return self.members.get(
            member.replace("\\", "/").casefold()
        )

    def read_ref(self, reference: FakeReference) -> bytes:
        return self.payloads[reference.name.casefold()]


def xmodel(
    lods: tuple[tuple[str, tuple[str, ...]], ...],
) -> bytes:
    data = bytearray(struct.pack("<H", 14))
    data.extend(bytes(24))
    for index in range(3):
        name = lods[index][0] if index < len(lods) else ""
        data.extend(struct.pack("<f", float(index * 100)))
        data.extend(name.encode("ascii") + b"\0")
    data.extend(bytes(4))
    data.extend(struct.pack("<I", 0))
    for _name, materials in lods:
        data.extend(struct.pack("<H", len(materials)))
        for material in materials:
            data.extend(material.encode("ascii") + b"\0")
    return bytes(data)


def bsp(entities: str) -> bytes:
    encoded = entities.encode("latin1") + b"\0"
    header_size = 8 + 30 * 8
    data = bytearray(header_size)
    data[:4] = b"IBSP"
    struct.pack_into("<i", data, 4, 59)
    struct.pack_into(
        "<ii",
        data,
        8 + 29 * 8,
        len(encoded),
        header_size,
    )
    data.extend(encoded)
    return bytes(data)


def bsp_lumps(lumps: dict[int, bytes]) -> bytes:
    header_size = 8 + 30 * 8
    data = bytearray(header_size)
    data[:4] = b"IBSP"
    struct.pack_into("<i", data, 4, 59)
    for index in sorted(lumps):
        payload = lumps[index]
        offset = len(data)
        struct.pack_into(
            "<ii",
            data,
            8 + index * 8,
            len(payload),
            offset,
        )
        data.extend(payload)
    return bytes(data)


class MultiplayerClosureUnitTests(unittest.TestCase):
    def test_xmodel_v14_parser_returns_lods_and_skin_dependencies(self) -> None:
        payload = xmodel(
            (
                ("crate_high0", ("wood@crate.dds", "metal@hinge.tga")),
                ("crate_low0", ("wood@crate.dds",)),
            )
        )
        self.assertEqual(
            parse_xmodel_v14_dependencies(payload, "xmodel/mp_crate"),
            (
                (
                    "crate_high0",
                    ("wood@crate.dds", "metal@hinge.tga"),
                ),
                ("crate_low0", ("wood@crate.dds",)),
            ),
        )

    def test_map_closure_excludes_sp_bsp_and_non_prop_actor_models(self) -> None:
        members = {
            "maps/mp/mp_test.bsp": bsp(
                """
                { "classname" "worldspawn" }
                { "classname" "misc_model" "model" "xmodel/mp_crate" }
                { "classname" "actor" "model" "xmodel/sp_cinematic_actor" }
                """
            ),
            "maps/campaign_test.bsp": bsp(
                """
                { "classname" "worldspawn" }
                { "classname" "misc_model" "model" "xmodel/sp_campaign_prop" }
                """
            ),
            "xmodel/mp_crate": xmodel(
                (("mp_crate0", ("mp@crate.dds",)),)
            ),
            "xmodelparts/mp_crate0": b"parts",
            "xmodelsurfs/mp_crate0": b"surfs",
            "skins/mp@crate.dds": b"skin",
            "xmodel/health_medium": xmodel(
                (("health_medium0", ("pickup@firstaid_kit.dds",)),)
            ),
            "xmodelparts/health_medium0": b"parts",
            "xmodelsurfs/health_medium0": b"surfs",
            "skins/pickup@firstaid_kit.dds": b"skin",
            "xmodel/sp_cinematic_actor": xmodel(
                (("sp_actor0", ("sp@actor.dds",)),)
            ),
            "xmodelparts/sp_actor0": b"parts",
            "xmodelsurfs/sp_actor0": b"surfs",
            "skins/sp@actor.dds": b"skin",
            "xmodel/sp_campaign_prop": xmodel(
                (("sp_campaign0", ("sp@campaign.dds",)),)
            ),
            "xmodelparts/sp_campaign0": b"parts",
            "xmodelsurfs/sp_campaign0": b"surfs",
            "skins/sp@campaign.dds": b"skin",
        }
        closure = build_map_model_closure(FakeIndex(members))
        folded = {path.casefold() for path in closure.paths}
        self.assertEqual(closure.selected_maps, ("mp_test",))
        self.assertEqual(
            folded,
            {
                "xmodel/mp_crate",
                "xmodelparts/mp_crate0",
                "xmodelsurfs/mp_crate0",
                "skins/mp@crate.dds",
                "xmodel/health_medium",
                "xmodelparts/health_medium0",
                "xmodelsurfs/health_medium0",
                "skins/pickup@firstaid_kit.dds",
            },
        )
        self.assertFalse(any("sp_" in path for path in folded))

    def test_weapon_closure_follows_mp_alternate_and_no_global_assets(self) -> None:
        members = {
            "weapons/mp/test_mp": (
                b"WEAPONFILE\\gunModel\\viewmodel_test"
                b"\\worldModel\\xmodel/weapon_test"
                b"\\fireAnim\\viewmodel_test_fire"
                b"\\meleeAnim\\viewmodel_test_melee"
                b"\\altWeapon\\test_slow_mp"
            ),
            "weapons/mp/test_slow_mp": (
                b"WEAPONFILE\\gunModel\\viewmodel_test"
                b"\\fireAnim\\viewmodel_test_slow_fire"
            ),
            "weapons/sp/test": b"WEAPONFILE\\gunModel\\sp_test",
            "xanim/viewmodel_test_fire": b"anim",
            "xanim/viewmodel_test_melee": b"anim",
            "xanim/viewmodel_test_slow_fire": b"anim",
            "xanim/sp_campaign_reload": b"anim",
        }
        files, animations, models, resolved = collect_weapon_source(
            FakeIndex(members),
            weapon_files=("test_mp",),
        )
        self.assertEqual(
            {item.casefold() for item in files},
            {"weapons/mp/test_mp", "weapons/mp/test_slow_mp"},
        )
        self.assertEqual(
            {item.casefold() for item in animations},
            {
                "xanim/viewmodel_test_fire",
                "xanim/viewmodel_test_melee",
                "xanim/viewmodel_test_slow_fire",
            },
        )
        self.assertEqual(
            models,
            frozenset(("shellcasing", "viewmodel_test", "weapon_test")),
        )
        self.assertEqual(resolved, ("test_mp", "test_slow_mp"))

    def test_mounted_mg42_closure_is_exact_weapon_rig_family(self) -> None:
        members = {
            "weapons/mp/mg42_bipod_stand_mp": (
                b"WEAPONFILE\\idleAnim\\standMG42gun_aim_foward"
                b"\\fireAnim\\standMG42gun_fire_foward"
            ),
            "weapons/mp/mg42_bipod_prone_mp": (
                b"WEAPONFILE\\idleAnim\\proneMG42gun_aim_forward_medium"
                b"\\fireAnim\\proneMG42gun_fire_forward_medium"
            ),
            **{
                f"xanim/{animation.name}": b"anim"
                for animation in MOUNTED_MG42_ANIMATIONS
            },
            "xanim/scripted_mg42_campaign_fire": b"sp",
            "xanim/standMG42gunner_fire_forward_level": b"player rig",
        }
        weapon_files, animations = collect_mounted_mg42_source(
            FakeIndex(members)
        )
        self.assertEqual(
            {path.casefold() for path in weapon_files},
            {
                f"weapons/mp/{name}".casefold()
                for name in MOUNTED_MG42_WEAPON_FILES
            },
        )
        self.assertEqual(
            {path.casefold() for path in animations},
            {
                f"xanim/{animation.name}".casefold()
                for animation in MOUNTED_MG42_ANIMATIONS
            },
        )

    def test_playeranim_closure_expands_only_mp_turret_player_family(self) -> None:
        mounted_aims = {
            (
                f"xanim/standMG42gunner_aim_{horizontal}_{vertical}"
            ): b"anim"
            for horizontal in MG42_HORIZONTAL["stand"]
            for vertical in MG42_VERTICAL
        }
        index = FakeIndex(
            {
                "mp/playeranim.script": b"""
                    ANIMATIONS
                    STATE COMBAT
                    {
                      walk
                      {
                        default
                        {
                          legs pb_walk
                          // torso sp_campaign_death
                          both standMG42_aim turretanim
                        }
                      }
                    }
                """,
                "xanim/pb_walk": b"anim",
                "xanim/sp_campaign_death": b"anim",
                "xanim/standMG42gun_aim_foward": b"weapon rig",
                "xanim/pb_standMG42gunner_aim_forward_level":
                    b"unselected duplicate family",
                **mounted_aims,
            }
        )
        script, animations, expanded, excluded = (
            collect_player_animation_source(index)
        )
        self.assertEqual(script.casefold(), "mp/playeranim.script")
        self.assertEqual(
            {item.casefold() for item in animations},
            {
                "xanim/pb_walk",
                *{
                    name.casefold()
                    for name in mounted_aims
                },
            },
        )
        self.assertEqual(expanded, ("standMG42_aim",))
        self.assertEqual(excluded, ())


class ScriptExploderClosureUnitTests(unittest.TestCase):
    def test_registration_parser_ignores_disabled_effects(self) -> None:
        self.assertEqual(
            effect_registrations(
                """
                // level._effect["commented"] =
                //     loadfx("fx/sp/commented.efx");
                /*
                level._effect["blocked"] =
                    loadfx("fx/sp/blocked.efx");
                */
                level._effect["active"] =
                    loadfx("fx/mp/active.efx");
                """
            ),
            {"active": "fx/mp/active.efx"},
        )

    def test_recursive_efx_closure_preserves_ordered_shader_frames(self) -> None:
        closure = build_effect_closure(
            FakeIndex(
                {
                    "fx/mp/root.efx": b"""
                        Particle
                        {
                            shaders [ fx/mp/spark ]
                            models [ xmodel/gib_test ]
                            sounds [ exploder_test ]
                            playFx [ fx/mp/nested ]
                        }
                    """,
                    "fx/mp/nested.efx": b"""
                        Emitter
                        {
                            shaders [ gfx/fx/direct ]
                        }
                    """,
                    "fxshaders/mp.shader": b"""
                        fx/mp/spark
                        {
                            animMap 12 gfx/fx/frame_b gfx/fx/frame_a
                        }
                    """,
                    "gfx/fx/frame_a.tga": b"a",
                    "gfx/fx/frame_b.dds": b"b",
                    "gfx/fx/direct.jpg": b"direct",
                }
            ),
            "blast",
            "fx/mp/root.efx",
        )
        self.assertEqual(
            closure.efx_files,
            ("fx/mp/nested.efx", "fx/mp/root.efx"),
        )
        self.assertEqual(closure.model_names, ("gib_test",))
        self.assertEqual(closure.sound_aliases, ("exploder_test",))
        self.assertEqual(
            dict(closure.shader_images)["fx/mp/spark"],
            ("gfx/fx/frame_b.dds", "gfx/fx/frame_a.tga"),
        )
        self.assertEqual(
            set(closure.image_files),
            {
                "gfx/fx/direct.jpg",
                "gfx/fx/frame_a.tga",
                "gfx/fx/frame_b.dds",
            },
        )

    def test_recursive_efx_closure_normalizes_retail_rooted_fx_path(
        self,
    ) -> None:
        closure = build_effect_closure(
            FakeIndex(
                {
                    "fx/map_mp/mp_mortar_mz.efx": b"""
                        FxRunner
                        {
                            playfx
                            [
                                /fx/map_trenches/mortar_mz_nest
                            ]
                        }
                    """,
                    "fx/map_trenches/mortar_mz_nest.efx": b"""
                        Particle
                        {
                            shaders [ gfx/fx/mortar ]
                        }
                    """,
                    "gfx/fx/mortar.dds": b"mortar",
                }
            ),
            "fx/map_mp/mp_mortar_mz.efx",
            "/fx/map_mp/mp_mortar_mz",
        )

        self.assertEqual(
            closure.source_efx,
            "fx/map_mp/mp_mortar_mz",
        )
        self.assertEqual(
            closure.efx_files,
            (
                "fx/map_mp/mp_mortar_mz.efx",
                "fx/map_trenches/mortar_mz_nest.efx",
            ),
        )
        self.assertEqual(
            closure.image_files,
            ("gfx/fx/mortar.dds",),
        )

    def test_shader_map_clamp_and_compiled_extension_are_exactly_resolved(
        self,
    ) -> None:
        closure = build_effect_closure(
            FakeIndex(
                {
                    "fx/mp/root.efx": b"""
                        Particle
                        {
                            shaders [ gfx/fx/clamped ]
                        }
                    """,
                    "fxshaders/mp.shader": b"""
                        gfx/fx/clamped
                        {
                            map clamp gfx/fx/authored.tga
                        }
                    """,
                    # Retail shader text commonly retains the authored TGA
                    # spelling while the selected pak stores compiled DDS.
                    "gfx/fx/authored.dds": b"compiled",
                }
            ),
            "clamped",
            "fx/mp/root.efx",
        )

        self.assertEqual(
            dict(closure.shader_images)["gfx/fx/clamped"],
            ("gfx/fx/authored.dds",),
        )
        self.assertEqual(
            closure.image_files,
            ("gfx/fx/authored.dds",),
        )

    def test_ambiguous_global_registration_is_rejected(self) -> None:
        index = FakeIndex(
            {
                "maps/mp/a_fx.gsc": b"""
                    level._effect["dust"] =
                        loadfx("fx/mp/dust_a.efx");
                """,
                "maps/mp/b_fx.gsc": b"""
                    level._effect["dust"] =
                        loadfx("fx/mp/dust_b.efx");
                """,
            }
        )
        with self.assertRaisesRegex(
            ScriptExploderClosureError,
            "no unambiguous",
        ):
            map_exploder_effects(
                index,
                "test",
                (
                    {
                        "script_exploder": "1",
                        "script_fxid": "dust",
                    },
                ),
            )

    def test_unsafe_and_missing_effect_references_are_rejected(self) -> None:
        with self.assertRaisesRegex(
            ScriptExploderClosureError,
            "unsafe",
        ):
            effect_registrations(
                """
                level._effect["bad"] =
                    loadfx("../campaign/bad.efx");
                """
            )
        with self.assertRaisesRegex(
            ScriptExploderClosureError,
            "missing",
        ):
            build_effect_closure(
                FakeIndex({}),
                "missing",
                "fx/mp/missing.efx",
            )

    def test_gameplay_manifest_retains_exploder_target_graph_and_delay_keys(
        self,
    ) -> None:
        models = [
            {
                "minimum": [-100.0, -100.0, -100.0],
                "maximum": [100.0, 100.0, 100.0],
            },
            {
                "minimum": [0.0, 0.0, 0.0],
                "maximum": [10.0, 20.0, 30.0],
            },
        ]
        entities = [
            {"classname": "worldspawn"},
            {
                "classname": "script_model",
                "script_exploder": "7",
                "script_fxid": "blast",
                "script_delay_min": "0.25",
                "target": "auto7",
            },
            {
                "classname": "script_brushmodel",
                "script_exploder": "7",
                "model": "*1",
            },
            {
                "classname": "info_null",
                "targetname": "auto7",
                "target": "auto8",
            },
            {
                "classname": "info_null",
                "targetname": "auto8",
            },
        ]
        records = gameplay_entity_manifest(
            entities,
            models,
            {},
            Path("/unused"),
            "audio",
        )
        by_source = {
            record["sourceEntityIndex"]: record
            for record in records
        }
        self.assertEqual(set(by_source), {1, 2, 3, 4})
        self.assertTrue(by_source[1]["hasScriptDelayMin"])
        self.assertFalse(by_source[1]["hasScriptDelayMax"])
        self.assertEqual(by_source[1]["scriptDelayMin"], 0.25)
        self.assertEqual(by_source[3]["targetName"], "auto7")
        self.assertEqual(by_source[4]["targetName"], "auto8")

    def test_only_script_brushmodel_claims_draw_soup(self) -> None:
        material = b"textures/mp/wall\0".ljust(64, b"\0") + bytes(8)
        soup = struct.pack("<HHIHHI", 0, 0, 0, 0, 3, 0)
        data = bsp_lumps({0: material, 6: soup})
        models = [
            {
                "minimum": [0.0, 0.0, 0.0],
                "maximum": [1.0, 1.0, 1.0],
                "firstTriangleSoup": 0,
                "triangleSoupCount": 0,
            },
            {
                "minimum": [0.0, 0.0, 0.0],
                "maximum": [1.0, 1.0, 1.0],
                "firstTriangleSoup": 0,
                "triangleSoupCount": 1,
            },
        ]
        entities = [
            {"classname": "worldspawn"},
            {
                "classname": "script_model",
                "script_exploder": "3",
                "model": "xmodel/gib_test",
            },
            {
                "classname": "script_brushmodel",
                "script_exploder": "3",
                "model": "*1",
            },
        ]
        assets = exploder_brush_assets(
            data,
            entities,
            models,
            "cod1",
            "test",
        )
        self.assertEqual(set(assets), {2})
        self.assertEqual(assets[2]["_soupIndices"], [0])
        self.assertEqual(
            assets[2]["glb"],
            "maps/cod1/test/exploders/entity_2.glb",
        )

    def test_manifest_validation_proves_effect_and_target_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            efx = content / "fx/exploders/efx/mp/root.efx"
            texture = (
                content
                / "fx/exploders/textures/gfx/fx/frame.png"
            )
            efx.parent.mkdir(parents=True)
            texture.parent.mkdir(parents=True)
            efx.write_bytes(b"Particle {}")
            texture.write_bytes(b"png")
            payload = {
                "format":
                    "FriendsOfDuty.OriginalMultiplayerMapEntities",
                "version": 2,
                "modelAssets": ["gib_test"],
                "exploderEffects": [
                    {
                        "fxId": "blast",
                        "sourceEfx": "fx/mp/root.efx",
                        "efxFiles": [
                            "fx/exploders/efx/mp/root.efx"
                        ],
                        "texturePngs": [
                            "fx/exploders/textures/gfx/fx/frame.png"
                        ],
                        "shaderTextures": [
                            {
                                "shader": "fx/mp/frame",
                                "texturePngs": [
                                    "fx/exploders/textures/"
                                    "gfx/fx/frame.png"
                                ],
                            }
                        ],
                        "aliasNames": [],
                        "modelNames": ["gib_test"],
                    }
                ],
                "entities": [
                    {"classname": "worldspawn"},
                    {
                        "classname": "script_model",
                        "script_exploder": "4",
                        "script_fxid": "blast",
                        "script_delay_min": "0.1",
                        "target": "auto7",
                    },
                    {
                        "classname": "info_null",
                        "targetname": "auto7",
                    },
                ],
                "gameplayEntities": [
                    {
                        "sourceEntityIndex": 1,
                        "scriptExploderId": 4,
                        "scriptFxId": "blast",
                        "hasScriptDelayMin": True,
                        "hasScriptDelayMax": False,
                        "targetName": "",
                        "target": "auto7",
                        "exploderBrushGlb": "",
                        "exploderBrushMaterials": [],
                    },
                    {
                        "sourceEntityIndex": 2,
                        "scriptExploderId": 0,
                        "scriptFxId": "",
                        "hasScriptDelayMin": False,
                        "hasScriptDelayMax": False,
                        "targetName": "auto7",
                        "target": "",
                        "exploderBrushGlb": "",
                        "exploderBrushMaterials": [],
                    },
                ],
                "counts": {
                    "scriptExploderEntities": 1,
                    "exploderEffects": 1,
                    "exploderBrushes": 0,
                },
            }
            errors: list[str] = []
            validate_map_script_exploders(
                content,
                "test",
                payload,
                errors,
            )
            self.assertEqual(errors, [])

            payload["gameplayEntities"][0][
                "hasScriptDelayMin"
            ] = False
            payload["gameplayEntities"].pop()
            errors = []
            validate_map_script_exploders(
                content,
                "test",
                payload,
                errors,
            )
            self.assertTrue(
                any("key presence" in error for error in errors)
            )
            self.assertTrue(
                any("target entity 2" in error for error in errors)
            )


class LocalRetailClosureTests(unittest.TestCase):
    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "Main" / "pak0.pk3").is_file(),
        "local retail archives unavailable",
    )
    def test_base_retail_closure_keeps_required_mp_sources_and_drops_sp(self) -> None:
        index = LayeredArchiveIndex(
            selected_archives(DATA_ROOT / "Call of Duty")
        )
        weapon_player = build_weapon_player_closure(index)
        map_models = build_map_model_closure(index)
        paths = {
            item.casefold()
            for item in weapon_player.paths | map_models.paths
        }
        self.assertIn("mp/playeranim.script", paths)
        self.assertIn("xanim/pb_sprint", paths)
        self.assertIn("xmodel/playerbody_american_airborne", paths)
        self.assertIn("xmodel/weapon_bar", paths)
        self.assertIn("weapons/mp/bar_mp", paths)
        self.assertIn("xmodel/objective_german_field_radio", paths)
        self.assertIn("weapons/mp/mg42_bipod_stand_mp", paths)
        self.assertIn("weapons/mp/mg42_bipod_prone_mp", paths)
        self.assertIn("xanim/standmg42gun_fire_foward", paths)
        self.assertIn("xanim/pronemg42gun_fire_forward_level", paths)
        self.assertNotIn("xanim/training_foley_todayobstacle", paths)
        self.assertFalse(any(path.startswith("weapons/sp/") for path in paths))

    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "uo" / "pakuo00.pk3").is_file(),
        "local United Offensive retail archives unavailable",
    )
    def test_shipping_retail_exploder_graph_is_exact(
        self,
    ) -> None:
        index = layered_index(
            selected_archives(DATA_ROOT / "Call of Duty")
        )
        expected = {
            **BASE_SCRIPT_EXPLODER_EFFECT_IDS,
            **UO_SCRIPT_EXPLODER_EFFECT_IDS,
        }
        all_efx: set[str] = set()
        all_images: set[str] = set()
        all_models: set[str] = set()
        all_aliases: set[str] = set()
        total_effects = 0
        for map_id, expected_ids in sorted(expected.items()):
            bsp_reference = index[f"maps/mp/{map_id}.bsp"]
            data = read_entry(bsp_reference)
            entities = parse_entities(data)
            script_reference = index.get(f"maps/mp/{map_id}.gsc")
            script = (
                read_entry(script_reference).decode(
                    "latin1",
                    "replace",
                )
                if script_reference is not None
                else None
            )
            effects = map_exploder_effects(
                index,
                map_id,
                entities,
                script,
            )
            self.assertEqual(
                tuple(sorted(effect.fx_id for effect in effects)),
                tuple(sorted(expected_ids)),
                map_id,
            )
            total_effects += len(effects)
            all_efx.update(
                path
                for effect in effects
                for path in effect.efx_files
            )
            all_images.update(
                path
                for effect in effects
                for path in effect.image_files
            )
            all_models.update(
                model
                for effect in effects
                for model in effect.model_names
            )
            all_aliases.update(
                alias
                for effect in effects
                for alias in effect.sound_aliases
            )

        counts = SCRIPT_EXPLODER_CLOSURE_COUNTS
        self.assertEqual(len(expected), counts["maps"])
        self.assertEqual(total_effects, counts["effects"])
        self.assertEqual(len(all_efx), counts["efxFiles"])
        self.assertEqual(len(all_images), counts["textureFiles"])
        self.assertEqual(len(all_models), counts["modelNames"])
        self.assertEqual(len(all_aliases), counts["aliasNames"])


if __name__ == "__main__":
    unittest.main()
