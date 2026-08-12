from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
# The Call of Duty installs and the extracted/derived content trees live
# beside the repo, not in it. Kept separate from ROOT so a test can never
# look for game data among the source.
DATA_ROOT = ROOT.parent
TOOLS = ROOT / "tools"
EXPORTER = ROOT / "exporter"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(EXPORTER))

from cod1_shipping_maps import (  # noqa: E402
    ALL_SHIPPING_MAP_IDS,
    BASE_SHIPPING_MAP_IDS,
    EXCLUDED_MULTIPLAYER_SCRIPTS,
    UO_SHIPPING_MAP_IDS,
    is_selected_multiplayer_script,
    normalize_requested_maps,
    selected_shipping_map_ids,
)
from import_cod_multiplayer_maps import (  # noqa: E402
    pak_map_specs,
    prune_stale_map_outputs,
    selected_catalog_maps,
)
from extract_cod1_weapon_presentation import (  # noqa: E402
    _prior_presentation_owned_paths,
    _prune_owned_paths,
)
import pipeline  # noqa: E402


EXPECTED_BASE = (
    "mp_carentan",
    "mp_chateau",
    "mp_pavlov",
    "mp_railyard",
    "mp_rocket",
)
EXPECTED_UO = ("mp_arnhem", "mp_cassino")
EXPECTED_ALL = frozenset((*EXPECTED_BASE, *EXPECTED_UO))


class ShippingMapRosterTests(unittest.TestCase):
    def test_cut_system_scripts_never_enter_the_gsc_closure(self) -> None:
        # The seven modes and the two drivable vehicles this product cut are
        # the only reason their objective props (mp_bomb1, o_ctf_flag_*, the
        # HQ radios, the Base Assault bunkers) and the jeep/tank damage
        # swaps ever reached props/required_models.json. _util_mp_gmi.gsc is
        # in the list for the same reason: its only content sink is the Base
        # Assault bunker table.
        self.assertEqual(
            EXCLUDED_MULTIPLAYER_SCRIPTS,
            frozenset(
                (
                    "gametypes/bas.gsc",
                    "gametypes/bel.gsc",
                    "gametypes/ctf.gsc",
                    "gametypes/dom.gsc",
                    "gametypes/hq.gsc",
                    "gametypes/re.gsc",
                    "gametypes/sd.gsc",
                    "_jeepdrive_gmi.gsc",
                    "_jeepeffects_gmi.gsc",
                    "_tankdrive_gmi.gsc",
                    "_tankeffects_gmi.gsc",
                    "_util_mp_gmi.gsc",
                )
            ),
        )
        for excluded in EXCLUDED_MULTIPLAYER_SCRIPTS:
            self.assertFalse(
                is_selected_multiplayer_script(
                    f"maps/mp/{excluded}",
                    EXPECTED_ALL,
                ),
                excluded,
            )
        # Retail authors these paths in mixed case; the archive index is
        # case-insensitive and so is the filter.
        self.assertFalse(
            is_selected_multiplayer_script(
                "maps\\MP\\gametypes\\CTF.gsc",
                EXPECTED_ALL,
            )
        )
        # The shipped modes and every shared helper stay.
        for kept in (
            "maps/mp/gametypes/dm.gsc",
            "maps/mp/gametypes/tdm.gsc",
            "maps/mp/gametypes/_gameobjects.gsc",
            "maps/mp/gametypes/_spawnlogic.gsc",
            "maps/mp/_load.gsc",
            "maps/mp/_minefields.gsc",
            "maps/mp/_turret_gmi.gsc",
        ):
            self.assertTrue(
                is_selected_multiplayer_script(kept, EXPECTED_ALL),
                kept,
            )

    def test_authoritative_roster_is_exact_and_tiered(self) -> None:
        self.assertEqual(BASE_SHIPPING_MAP_IDS, EXPECTED_BASE)
        self.assertEqual(UO_SHIPPING_MAP_IDS, EXPECTED_UO)
        self.assertEqual(ALL_SHIPPING_MAP_IDS, EXPECTED_ALL)
        self.assertEqual(
            selected_shipping_map_ids(include_uo=False),
            tuple(sorted(EXPECTED_BASE)),
        )
        self.assertEqual(
            selected_shipping_map_ids(include_uo=True),
            tuple(sorted(EXPECTED_ALL)),
        )

    def test_requested_maps_cannot_expand_the_shipping_roster(self) -> None:
        self.assertEqual(
            normalize_requested_maps(
                ["MP_PAVLOV,mp_rocket", "mp_pavlov"]
            ),
            frozenset(("mp_pavlov", "mp_rocket")),
        )
        with self.assertRaisesRegex(ValueError, "shipping rotation"):
            normalize_requested_maps(["mp_bocage"])
        with self.assertRaisesRegex(ValueError, "United Offensive"):
            selected_shipping_map_ids(
                include_uo=False,
                requested_maps=["mp_arnhem"],
            )

    def test_legacy_all_mp_flag_cannot_expand_the_allowlist(self) -> None:
        retail_names = (
            *EXPECTED_BASE,
            "mp_bocage",
            "mp_brecourt",
            "mp_stalingrad",
        )
        fake_maps = {
            name: SimpleNamespace(name=f"maps/mp/{name}.bsp")
            for name in retail_names
        }
        with patch(
            "import_cod_multiplayer_maps.multiplayer_maps",
            return_value=fake_maps,
        ):
            specs = pak_map_specs(
                Path("/retail"),
                [Path("/retail/Main/pak0.pk3")],
                requested=None,
                all_mp=True,
                has_uo=False,
            )
        self.assertEqual(
            {map_id for _game, _source, map_id in specs},
            set(EXPECTED_BASE),
        )

    def test_pipeline_emits_no_tier_or_subset_selection_flags(self) -> None:
        """The roster and the tier are fixed, so no step may negotiate them.

        Previously every map-owned closure received an explicit --maps list
        (and optionally --include-uo). Passing a narrowed list produced a
        catalog the runtime refuses to mount, so the flags were removed from
        both sides rather than merely defaulted.
        """
        config = pipeline.PipelineConfig(
            game_dir=DATA_ROOT / "Call of Duty",
            # The real default, not a hand-written path: one step resolves a
            # prop-model list relative to the content directory and cannot
            # build its argv without it, so pointing this at a location that
            # never holds a package made the test error rather than assert.
            content_dir=pipeline.DEFAULT_CONTENT_DIR,
            # Blender steps refuse to build an argv without one.
            blender=Path("/nonexistent/blender"),
        )
        for step in pipeline.build_steps(config):
            if step.build_argv is None:
                continue
            argv = step.build_argv(config)
            for retired in ("--maps", "--include-uo", "--all-mp"):
                self.assertNotIn(retired, argv, f"{step.key} still passes {retired}")

    def test_authoritative_catalog_and_outputs_prune_excluded_map_assets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pak_root = Path(temporary)
            maps_root = pak_root / "maps"
            keep_texture = (
                maps_root / "shared" / "cod1" / "textures" / "keep.png"
            )
            stale_texture = (
                maps_root / "shared" / "cod1" / "textures" / "stale.png"
            )
            keep_texture.parent.mkdir(parents=True)
            keep_texture.write_bytes(b"keep")
            stale_texture.write_bytes(b"stale")

            pavlov = maps_root / "cod1" / "mp_pavlov"
            bocage = maps_root / "cod1" / "mp_bocage"
            pavlov.mkdir(parents=True)
            bocage.mkdir(parents=True)
            (bocage / "world.glb").write_bytes(b"excluded")
            materials = pavlov / "materials.json"
            materials.write_text(
                json.dumps(
                    [
                        {
                            "texture":
                                "maps/shared/cod1/textures/keep.png"
                        }
                    ]
                ),
                encoding="utf-8",
            )
            old_catalog = {
                "maps": [
                    {"game": "cod1", "mapId": "mp_bocage"},
                    {"game": "cod1", "mapId": "mp_pavlov"},
                ]
            }
            catalog_path = maps_root / "catalog.json"
            catalog_path.write_text(json.dumps(old_catalog), encoding="utf-8")
            current = [
                {
                    "game": "cod1",
                    "mapId": "mp_pavlov",
                    "materialsJson":
                        "maps/cod1/mp_pavlov/materials.json",
                }
            ]

            selected = selected_catalog_maps(
                catalog_path,
                current,
                allowed_keys={
                    ("cod1", map_id) for map_id in EXPECTED_BASE
                },
                authoritative=True,
            )
            removed_maps, removed_textures = prune_stale_map_outputs(
                pak_root,
                selected,
            )

            self.assertEqual(
                [(entry["game"], entry["mapId"]) for entry in selected],
                [("cod1", "mp_pavlov")],
            )
            self.assertIn(("cod1", "mp_bocage"), removed_maps)
            self.assertEqual(removed_textures, 1)
            self.assertFalse(bocage.exists())
            self.assertFalse(stale_texture.exists())
            self.assertTrue(keep_texture.is_file())

    def test_presentation_pruning_does_not_claim_ordnance_outputs(
        self,
    ) -> None:
        manifest = {
            "aliases": [
                {
                    "alias": "excluded_map_alias",
                    "variants": [{"file": "maps/excluded.wav"}],
                },
                {
                    "alias": "grenade_explode_default",
                    "variants": [{"file": "weapons/grenade.wav"}],
                },
            ],
            "provenance": [
                {"path": "audio/maps/excluded.wav"},
                {"path": "audio/weapons/grenade.wav"},
                {
                    "path": "fx/efx/grenade1.efx",
                    "member": "fx/explosions/grenade1.efx",
                },
            ],
        }
        owned = _prior_presentation_owned_paths(
            manifest,
            pak_mode=True,
        )
        self.assertEqual(owned, {"audio/maps/excluded.wav"})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            excluded = root / "audio" / "maps" / "excluded.wav"
            ordnance = root / "audio" / "weapons" / "grenade.wav"
            ordnance_fx = root / "fx" / "efx" / "grenade1.efx"
            excluded.parent.mkdir(parents=True)
            ordnance.parent.mkdir(parents=True)
            ordnance_fx.parent.mkdir(parents=True)
            excluded.write_bytes(b"excluded")
            ordnance.write_bytes(b"ordnance")
            ordnance_fx.write_bytes(b"ordnance-fx")
            self.assertEqual(_prune_owned_paths(root, owned), 1)
            self.assertFalse(excluded.exists())
            self.assertTrue(ordnance.is_file())
            self.assertTrue(ordnance_fx.is_file())


if __name__ == "__main__":
    unittest.main()
