from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from cod1_shipping_maps import shipping_map_specs  # noqa: E402


PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "fod_package_map_roster_under_test",
    ROOT / "exporter" / "package.py",
)
assert PACKAGE_SPEC is not None and PACKAGE_SPEC.loader is not None
fod_package = importlib.util.module_from_spec(PACKAGE_SPEC)
PACKAGE_SPEC.loader.exec_module(fod_package)


# Kept split only to prove tier OWNERSHIP (which archive supplies which map).
# Both halves always ship: United Offensive is mandatory.
BASE_ROSTER = {
    (game, map_id)
    for game, _source_tier, map_id in shipping_map_specs(include_uo=False)
}
UO_ROSTER = {
    (game, map_id)
    for game, _source_tier, map_id in shipping_map_specs(include_uo=True)
} - BASE_ROSTER
ROSTER = BASE_ROSTER | UO_ROSTER


def entries(roster: set[tuple[str, str]]) -> list[dict[str, str]]:
    return [
        {"game": game, "mapId": map_id}
        for game, map_id in sorted(roster)
    ]


def map_texture_fixture(
    content: Path,
    roster: set[tuple[str, str]],
) -> list[dict[str, str]]:
    catalog: list[dict[str, str]] = []
    for game, map_id in sorted(roster):
        materials_relative = f"maps/{game}/{map_id}/materials.json"
        texture_relative = (
            f"maps/shared/{game}/textures/{map_id}_surface.png"
        )
        materials_path = content / materials_relative
        materials_path.parent.mkdir(parents=True, exist_ok=True)
        materials_path.write_text(
            json.dumps([{
                "group": f"{map_id}_surface",
                "texture": texture_relative,
            }]),
            encoding="utf-8",
        )
        texture_path = content / texture_relative
        texture_path.parent.mkdir(parents=True, exist_ok=True)
        texture_path.write_bytes(b"png")
        catalog.append({
            "game": game,
            "mapId": map_id,
            "materialsJson": materials_relative,
        })
    return catalog


class PackageMapRosterTests(unittest.TestCase):
    def test_constants_assign_approved_maps_to_their_retail_tiers(self) -> None:
        # One roster now, and it owns both tiers.
        self.assertEqual(fod_package.selected_map_roster(), frozenset(ROSTER))
        self.assertEqual(len(BASE_ROSTER), 5)
        self.assertEqual(len(UO_ROSTER), 2)

    def test_base_only_catalog_is_rejected(self) -> None:
        # United Offensive is mandatory: a five-map catalog is incomplete,
        # not a smaller product.
        errors: list[str] = []
        fod_package.validate_map_catalog_roster(entries(BASE_ROSTER), errors)
        self.assertTrue(errors)
        self.assertTrue(
            any("mp_arnhem" in error or "mp_cassino" in error for error in errors),
            errors,
        )

    def test_uo_package_accepts_exactly_the_seven_shipping_maps(self) -> None:
        errors: list[str] = []
        actual = fod_package.validate_map_catalog_roster(
            entries(BASE_ROSTER | UO_ROSTER),
            errors,
        )
        self.assertEqual(actual, BASE_ROSTER | UO_ROSTER)
        self.assertEqual(errors, [])

    def test_excluded_stock_map_is_rejected(self) -> None:
        errors: list[str] = []
        fod_package.validate_map_catalog_roster(
            entries(BASE_ROSTER | UO_ROSTER | {("cod1", "mp_bocage")}),
            errors,
        )
        self.assertTrue(
            any(
                "approved shipping maps" in error
                and "mp_bocage" in error
                for error in errors
            ),
            errors,
        )

    def test_wrong_tier_is_rejected(self) -> None:
        wrong_tier = (BASE_ROSTER | UO_ROSTER) - {("uo", "mp_arnhem")}
        wrong_tier.add(("cod1", "mp_arnhem"))
        errors: list[str] = []
        fod_package.validate_map_catalog_roster(
            entries(wrong_tier),
            errors,
        )
        self.assertTrue(
            any(
                "approved shipping maps" in error
                and "('cod1', 'mp_arnhem')" in error
                and "('uo', 'mp_arnhem')" in error
                for error in errors
            ),
            errors,
        )

    def test_duplicate_approved_map_is_rejected(self) -> None:
        catalog = entries(BASE_ROSTER | UO_ROSTER)
        catalog.append({"game": "cod1", "mapId": "mp_pavlov"})
        errors: list[str] = []
        fod_package.validate_map_catalog_roster(
            catalog,
            errors,
        )
        self.assertTrue(
            any(
                "duplicate map/game pairs" in error
                and "mp_pavlov" in error
                for error in errors
            ),
            errors,
        )

    def test_script_exploder_contract_excludes_nonshipping_maps(self) -> None:
        self.assertEqual(
            set(fod_package.BASE_SCRIPT_EXPLODER_EFFECT_IDS),
            {"mp_pavlov", "mp_railyard", "mp_rocket"},
        )
        self.assertEqual(fod_package.UO_SCRIPT_EXPLODER_EFFECT_IDS, {})
        self.assertEqual(
            fod_package.UO_SCRIPT_EXPLODER_ALIAS_VARIANTS,
            {},
            "mp_foy-only explo_tower01 must not leak into the shipping roster",
        )
        expected_counts = {
            "maps": 3,
            "effects": 4,
            "efxFiles": 2,
            "textureFiles": 6,
            "modelNames": 0,
            "aliasNames": 0,
        }
        self.assertEqual(
            fod_package.SCRIPT_EXPLODER_CLOSURE_COUNTS,
            expected_counts,
        )

    def test_shared_map_textures_equal_approved_material_reachability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            catalog = map_texture_fixture(content, BASE_ROSTER | UO_ROSTER)
            errors: list[str] = []
            references = fod_package.validate_shared_map_texture_closure(
                content,
                catalog,
                errors,
            )
            self.assertEqual(len(references), 7)
            self.assertEqual(errors, [])

    def test_stale_excluded_map_texture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            catalog = map_texture_fixture(content, BASE_ROSTER | UO_ROSTER)
            stale = (
                content
                / "maps/shared/cod1/textures/mp_bocage_surface.png"
            )
            stale.write_bytes(b"stale")
            errors: list[str] = []
            fod_package.validate_shared_map_texture_closure(
                content,
                catalog,
                errors,
            )
            self.assertTrue(
                any(
                    "unreferenced=" in error
                    and "mp_bocage_surface.png" in error
                    for error in errors
                ),
                errors,
            )

    def test_missing_material_texture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            catalog = map_texture_fixture(content, BASE_ROSTER)
            missing = (
                content
                / "maps/shared/cod1/textures/mp_pavlov_surface.png"
            )
            missing.unlink()
            errors: list[str] = []
            fod_package.validate_shared_map_texture_closure(
                content,
                catalog,
                errors,
            )
            self.assertTrue(
                any(
                    "references missing file" in error
                    and "mp_pavlov_surface.png" in error
                    for error in errors
                ),
                errors,
            )
            self.assertTrue(
                any(
                    "missing=" in error
                    and "mp_pavlov_surface.png" in error
                    for error in errors
                ),
                errors,
            )


if __name__ == "__main__":
    unittest.main()
