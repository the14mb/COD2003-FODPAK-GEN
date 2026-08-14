"""Retail impact table (fx/*.csv) parsing/merge, the fx/impacts.json payload
shape, the materials.json surfaceparm harvest, and the warn-only package
validation around both."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXPORTER = ROOT / "exporter"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(EXPORTER))

import cod1_impact_table as impact_table  # noqa: E402
import import_cod_multiplayer_maps as maps  # noqa: E402
import package as fod_package  # noqa: E402


# Trimmed retail shapes: quoted whole-line comments padded with empty cells,
# a duplicate key inside one file, a blank third cell, and a two-cell row —
# both blank forms mean "play no effect" and must stay distinct from an
# absent row.
BASE_TABLE = """# This file maps impact types to effects,,
"# It can be overridden, in part",,
bullet_small_normal,default,fx/impacts/default_hit.efx
bullet_small_normal,bark,fx/impacts/default_hit.efx
bullet_small_normal,bark,fx/impacts/default_hit.efx
bullet_small_normal,dirt,fx/impacts/small_gravel2.efx
bullet_small_normal,grass,fx/impacts/small_grass.efx
bullet_small_reflect,metal,
bullet_small_reflect,foliage
grenade_bounce,default,
"""

UO_TABLE = """grenade_bounce,default,fx\\weapon\\impacts\\grenade_bounce_generic.efx
"""

GMI_TABLE = """bullet_rifle_normal,grass,fx\\weapon\\impacts\\impact_smg_grass.efx
"""

NOT_A_TABLE = """soundalias,file,volume,pitch
mp_death,sound/death.wav,1.0,1.0
"""


class ParseImpactTableTests(unittest.TestCase):
    def test_rows_are_normalized_and_blank_cells_preserved(self) -> None:
        rows = impact_table.parse_impact_table(BASE_TABLE)
        self.assertIsNotNone(rows)
        self.assertIn(
            ("bullet_small_normal", "dirt", "fx/impacts/small_gravel2.efx"),
            rows,
        )
        # Blank third cell AND missing third cell both read "".
        self.assertIn(("bullet_small_reflect", "metal", ""), rows)
        self.assertIn(("bullet_small_reflect", "foliage", ""), rows)

    def test_case_and_slashes_normalize(self) -> None:
        rows = impact_table.parse_impact_table(
            "Bullet_Small_Normal,METAL,fx\\impacts\\metalhit_small.efx\n"
        )
        self.assertEqual(
            rows,
            (
                (
                    "bullet_small_normal",
                    "metal",
                    "fx/impacts/metalhit_small.efx",
                ),
            ),
        )

    def test_wrong_shape_is_not_a_table(self) -> None:
        self.assertIsNone(impact_table.parse_impact_table(NOT_A_TABLE))
        self.assertIsNone(impact_table.parse_impact_table("# only,comments\n"))
        self.assertIsNone(impact_table.parse_impact_table("one_cell_row\n"))


class MergeImpactTablesTests(unittest.TestCase):
    def _archive(self, root: Path, name: str, members: dict[str, str]) -> Path:
        path = root / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, text in members.items():
                archive.writestr(member, text)
        return path

    def test_later_alphabetical_filename_wins_per_key(self) -> None:
        # gmi < iw alphabetically, so iw's row overrides gmi's for the same
        # key while gmi's other rows survive.
        gmi = impact_table.parse_impact_table(
            "bullet_rifle_normal,grass,fx/old.efx\n"
            "bullet_rifle_normal,metal,fx/metal.efx\n"
        )
        iw = impact_table.parse_impact_table(
            "bullet_rifle_normal,grass,fx/new.efx\n"
        )
        merged = impact_table.merge_impact_rows([gmi, iw])
        self.assertEqual(
            merged,
            (
                ("bullet_rifle_normal", "grass", "fx/new.efx"),
                ("bullet_rifle_normal", "metal", "fx/metal.efx"),
            ),
        )

    def test_duplicate_key_inside_one_file_keeps_last_value(self) -> None:
        rows = impact_table.parse_impact_table(
            "bullet_small_normal,bark,fx/a.efx\n"
            "bullet_small_normal,bark,fx/b.efx\n"
        )
        self.assertEqual(
            impact_table.merge_impact_rows([rows]),
            (("bullet_small_normal", "bark", "fx/b.efx"),),
        )

    def test_archive_layering_merges_same_named_files_per_row(self) -> None:
        """UO's iw_impacts.csv copy has no bullet rows; the merge must keep
        Main's bullets while UO's grenade row overrides Main's blank one."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main_pak = self._archive(
                root,
                "pak5.pk3",
                {"fx/iw_impacts.csv": BASE_TABLE},
            )
            uo_pak = self._archive(
                root,
                "pakuo00.pk3",
                {
                    "fx/iw_impacts.csv": UO_TABLE,
                    "fx/gmi_impacts.csv": GMI_TABLE,
                    # Not row-shaped: must be discovered and rejected.
                    "fx/soundalias.csv": NOT_A_TABLE,
                    # Not directly under fx/: never considered.
                    "fx/sub/other_impacts.csv": BASE_TABLE,
                },
            )
            tables = impact_table.discover_impact_tables([main_pak, uo_pak])
            self.assertEqual(
                [(name, archive.name) for name, archive, _rows in tables],
                [
                    ("gmi_impacts.csv", "pakuo00.pk3"),
                    ("iw_impacts.csv", "pak5.pk3"),
                    ("iw_impacts.csv", "pakuo00.pk3"),
                ],
            )
            merged = impact_table.merged_impact_rows_from_archives(
                [main_pak, uo_pak]
            )
            by_key = {(i, s): e for i, s, e in merged}
            # Main's bullet rows survive the UO copy that dropped them.
            self.assertEqual(
                by_key[("bullet_small_normal", "dirt")],
                "fx/impacts/small_gravel2.efx",
            )
            # UO's copy overrides the shared grenade key (blank -> efx),
            # with backslashes normalized.
            self.assertEqual(
                by_key[("grenade_bounce", "default")],
                "fx/weapon/impacts/grenade_bounce_generic.efx",
            )
            # gmi's per-class row is present as data.
            self.assertEqual(
                by_key[("bullet_rifle_normal", "grass")],
                "fx/weapon/impacts/impact_smg_grass.efx",
            )
            # Deliberate blanks stay "".
            self.assertEqual(by_key[("bullet_small_reflect", "metal")], "")

    def test_manifest_payload_shape(self) -> None:
        payload = impact_table.impacts_manifest_payload(
            (
                ("bullet_small_normal", "dirt", "fx/impacts/small_gravel2.efx"),
                ("bullet_small_reflect", "metal", ""),
            )
        )
        self.assertEqual(
            payload,
            {
                "format": "FriendsOfDuty.Impacts",
                "version": 1,
                "rows": [
                    {
                        "impact": "bullet_small_normal",
                        "surface": "dirt",
                        "efx": "fx/impacts/small_gravel2.efx",
                    },
                    {
                        "impact": "bullet_small_reflect",
                        "surface": "metal",
                        "efx": "",
                    },
                ],
            },
        )

    def test_helpers_derive_vocabulary_and_closure_set(self) -> None:
        rows = (
            ("bullet_small_normal", "default", "fx/impacts/default_hit.efx"),
            ("bullet_small_normal", "grass", "fx/impacts/small_grass.efx"),
            ("bullet_large_normal", "grass", "fx/impacts/small_grass.efx"),
            ("bullet_small_reflect", "metal", ""),
            ("bullet_rifle_normal", "grass", "fx/weapon/impacts/rifle.efx"),
            ("grenade_bounce", "vehicle", "fx/weapon/impacts/bounce.efx"),
        )
        # "default" is the engine's could-not-classify bucket, not a
        # surfaceparm token.
        self.assertEqual(
            impact_table.surface_vocabulary(rows),
            frozenset({"grass", "metal", "vehicle"}),
        )
        # Only the four bullet_small/large types select closures; blanks and
        # the gmi per-class row do not.
        self.assertEqual(
            impact_table.bullet_closure_efx_paths(rows),
            ("fx/impacts/default_hit.efx", "fx/impacts/small_grass.efx"),
        )


SHADER_SCRIPT = """textures/normandy/grass_top
{
    surfaceparm grass
    {
        map textures/normandy/grass_top.tga
    }
}
textures/normandy/decal_window
{
    surfaceparm nomarks
    surfaceparm trans
    {
        map textures/normandy/decal_window.tga
    }
}
textures/normandy/plain_wall
{
    {
        map textures/normandy/plain_wall.tga
    }
}
"""


class SurfaceparmHarvestTests(unittest.TestCase):
    def _index(self, root: Path) -> dict[str, object]:
        archive_path = root / "pak5.pk3"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("scripts/normandy.shader", SHADER_SCRIPT)
            archive.writestr("fx/iw_impacts.csv", BASE_TABLE)
        return maps.layered_index([archive_path])

    def test_explicit_vocabulary_token_in_field_out(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = self._index(Path(temporary))
            vocabulary = maps.impact_surface_vocabulary(index)
            self.assertIn("grass", vocabulary)
            self.assertNotIn("default", vocabulary)
            surfaces = maps.surfaceparm_materials(index, vocabulary)
            self.assertEqual(
                surfaces.get("normandy/grass_top"),
                "grass",
            )

    def test_behaviour_parms_and_absent_parm_get_no_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index = self._index(Path(temporary))
            surfaces = maps.surfaceparm_materials(
                index,
                maps.impact_surface_vocabulary(index),
            )
            # nomarks/trans share the keyword but describe behaviour.
            self.assertNotIn("normandy/decal_window", surfaces)
            # No surfaceparm at all.
            self.assertNotIn("normandy/plain_wall", surfaces)


class ImpactsPackageValidationTests(unittest.TestCase):
    @staticmethod
    def _write_manifest(content: Path, rows: list[dict[str, str]]) -> None:
        (content / "fx").mkdir(parents=True, exist_ok=True)
        (content / "fx" / "impacts.json").write_text(json.dumps({
            "format": fod_package.IMPACTS_FORMAT,
            "version": fod_package.IMPACTS_VERSION,
            "rows": rows,
        }))

    def test_missing_manifest_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            warnings: list[str] = []
            fod_package.validate_impacts_manifest(Path(temporary), warnings)
            self.assertTrue(
                any("fx/impacts.json missing" in item for item in warnings),
                warnings,
            )

    def test_bullet_row_efx_must_be_packaged_but_gmi_rows_need_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            (content / "fx" / "efx").mkdir(parents=True)
            (content / "fx" / "efx" / "small_grass.efx").write_bytes(b"efx")
            self._write_manifest(content, [
                {
                    "impact": "bullet_small_normal",
                    "surface": "grass",
                    "efx": "fx/impacts/small_grass.efx",
                },
                {
                    "impact": "bullet_large_normal",
                    "surface": "metal",
                    "efx": "fx/impacts/metalhit_large.efx",
                },
                # Blank = deliberate no-effect, never a warning.
                {
                    "impact": "bullet_small_reflect",
                    "surface": "metal",
                    "efx": "",
                },
                # gmi per-class rows are data only; their efx are
                # deliberately not packaged.
                {
                    "impact": "bullet_rifle_normal",
                    "surface": "grass",
                    "efx": "fx/weapon/impacts/impact_smg_grass.efx",
                },
            ])
            warnings: list[str] = []
            fod_package.validate_impacts_manifest(content, warnings)
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn("metalhit_large", warnings[0])
            self.assertNotIn("impact_smg_grass", warnings[0])

    def test_materials_surface_tokens_must_be_in_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            self._write_manifest(content, [
                {
                    "impact": "bullet_small_reflect",
                    "surface": "grass",
                    "efx": "",
                },
            ])
            map_dir = content / "maps" / "cod1" / "mp_test"
            map_dir.mkdir(parents=True)
            (map_dir / "materials.json").write_text(json.dumps([
                {"group": "ok", "surface": "grass"},
                {"group": "bad", "surface": "linoleum"},
                {"group": "absent"},
            ]))
            warnings: list[str] = []
            fod_package.validate_impacts_manifest(content, warnings)
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn("'linoleum'", warnings[0])
            self.assertIn("maps/cod1/mp_test/materials.json", warnings[0])
            self.assertNotIn("grass", warnings[0])

    def test_complete_package_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            (content / "fx" / "efx").mkdir(parents=True)
            (content / "fx" / "efx" / "small_grass.efx").write_bytes(b"efx")
            self._write_manifest(content, [
                {
                    "impact": "bullet_small_normal",
                    "surface": "grass",
                    "efx": "fx/impacts/small_grass.efx",
                },
                {
                    "impact": "bullet_small_reflect",
                    "surface": "grass",
                    "efx": "",
                },
            ])
            map_dir = content / "maps" / "cod1" / "mp_test"
            map_dir.mkdir(parents=True)
            (map_dir / "materials.json").write_text(json.dumps([
                {"group": "ok", "surface": "grass"},
            ]))
            warnings: list[str] = []
            fod_package.validate_impacts_manifest(content, warnings)
            self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
