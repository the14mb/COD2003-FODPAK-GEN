"""weapons.json definition contract: make_weapon's WEAPONFILE field
selection, exercised without Blender.

export_cod1_demo_viewmodels.py is a Blender background script whose tail
performs the actual GLB export at import time, so the module cannot simply be
imported here. The definition logic (make_weapon, DEFINITION_KEYS,
weapon_definition, WEAPONS) all lives ABOVE the export block; execute exactly
that prefix with bpy/mathutils/cod_asset_importer stubbed and a synthetic
staging tree, and assert on the real functions."""

from __future__ import annotations

import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

TOOL_PATH = TOOLS / "export_cod1_demo_viewmodels.py"
TOOL_SOURCE = TOOL_PATH.read_text()
EXPORT_MARKER = '\nif EXPORT_FORMAT == "glb":\n'

# Retail kar98k_mp spells its worldFlashEffect with the doubled extension;
# the definition must carry it VERBATIM (the flattening/folding is the
# extractor's and the consumer's job, per docs/CONTENT_PIPELINE.md §1.4).
WORLD_FLASH = "fx/weapon/muzzleflash/mf_test.efx.efx"

WEAPON_TEMPLATE = (
    "WEAPONFILE"
    "\\weaponClass\\rifle"
    "\\gunModel\\xmodel/gun"
    "\\handModel\\xmodel/hands"
    "\\worldModel\\xmodel/weapon_test"
    "\\meleeAnim\\shared_melee"
    "\\meleeDamage\\50"
    "\\meleeDelay\\0.5"
    "\\meleeTime\\1.0"
    "\\viewFlashEffect\\fx/weapon/muzzleflash/mf_test_v.efx"
    f"\\worldFlashEffect\\{WORLD_FLASH}"
    "\\shellEjectEffect\\fx/shellejects/rifle.efx"
)


def roster_weapon_files() -> list[str]:
    """The base ROSTER's WEAPONFILE names, read from the tool source."""
    for node in ast.parse(TOOL_SOURCE).body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "ROSTER"
        ):
            return [entry[1] for entry in ast.literal_eval(node.value)]
    raise AssertionError("ROSTER not found in export_cod1_demo_viewmodels.py")


def load_definition_namespace(source_root: Path) -> dict:
    prefix, marker, _ = TOOL_SOURCE.partition(EXPORT_MARKER)
    if not marker:
        raise AssertionError(
            "export block marker not found in export_cod1_demo_viewmodels.py"
        )
    bpy = types.ModuleType("bpy")
    mathutils = types.ModuleType("mathutils")
    for name in ("Matrix", "Quaternion", "Vector"):
        setattr(mathutils, name, object)
    importer_package = types.ModuleType("cod_asset_importer")
    importer_native = types.ModuleType("cod_asset_importer.cod_asset_importer")
    importer_native.GAME_VERSION = "CoD"
    importer_package.importer = object()
    importer_package.cod_asset_importer = importer_native
    stubs = {
        "bpy": bpy,
        "mathutils": mathutils,
        "cod_asset_importer": importer_package,
        "cod_asset_importer.cod_asset_importer": importer_native,
    }
    argv = [
        "blender", "--background", "--python", str(TOOL_PATH), "--",
        str(source_root), str(source_root / "out"),
        str(source_root / "importer"), str(TOOLS),
        "--format", "glb", "--pak-root", str(source_root / "pak"),
    ]
    namespace = {
        "__file__": str(TOOL_PATH),
        "__name__": "export_cod1_demo_viewmodels_definitions",
    }
    with (
        patch.dict(sys.modules, stubs),
        patch.object(sys, "argv", argv),
    ):
        exec(compile(prefix, str(TOOL_PATH), "exec"), namespace)
    return namespace


class WeaponDefinitionFieldTests(unittest.TestCase):
    def test_world_muzzle_effect_is_selected_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            weapons = source_root / "weapons" / "mp"
            weapons.mkdir(parents=True)
            (source_root / "xanim").mkdir()
            (source_root / "xanim" / "shared_melee").write_bytes(b"anim")
            for weapon_file in roster_weapon_files():
                (weapons / weapon_file).write_text(WEAPON_TEMPLATE)

            namespace = load_definition_namespace(source_root)

            self.assertIn(
                "world_muzzle_effect", namespace["DEFINITION_KEYS"]
            )
            weapon = next(
                entry
                for entry in namespace["WEAPONS"]
                if entry["name"] == "kar98k"
            )
            self.assertEqual(
                weapon["muzzle_effect"],
                "fx/weapon/muzzleflash/mf_test_v.efx",
            )
            self.assertEqual(weapon["world_muzzle_effect"], WORLD_FLASH)
            self.assertEqual(
                weapon["shell_effect"], "fx/shellejects/rifle.efx"
            )
            definition = namespace["weapon_definition"](weapon)
            self.assertEqual(
                definition["world_muzzle_effect"], WORLD_FLASH
            )

    def test_blank_world_flash_stays_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            weapons = source_root / "weapons" / "mp"
            weapons.mkdir(parents=True)
            (source_root / "xanim").mkdir()
            (source_root / "xanim" / "shared_melee").write_bytes(b"anim")
            blank = WEAPON_TEMPLATE.replace(
                f"\\worldFlashEffect\\{WORLD_FLASH}", ""
            )
            for weapon_file in roster_weapon_files():
                (weapons / weapon_file).write_text(blank)

            namespace = load_definition_namespace(source_root)
            weapon = next(
                entry
                for entry in namespace["WEAPONS"]
                if entry["name"] == "kar98k"
            )
            self.assertEqual(weapon["world_muzzle_effect"], "")


if __name__ == "__main__":
    unittest.main()
