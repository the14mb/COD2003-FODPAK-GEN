"""fx/shaders.json: the package-time writer that maps every fx/efx shader
name to its packaged texture, and the warn-only validation around it plus the
weapons.json muzzle/world/shell efx presence check."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXPORTER = ROOT / "exporter"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(EXPORTER))

import cod1_script_exploder  # noqa: E402
import package as fod_package  # noqa: E402


EFX_TEXT = (
    "Sprite\n{\n"
    "    shaders [ weapon_v/spark gfx/effects/muzflash2_sn"
    " missing/unpackaged ]\n"
    "}\n"
)

SHADER_SCRIPT = """weapon_v/spark
{
    {
        map gfx/efx_assets/fire/spark_a.dds
    }
}
gfx/effects/muzflash2_sn
{
    {
        map gfx/effects/muzflash2.tga
    }
}
"""


def build_content(root: Path) -> Path:
    content = root / "content"
    (content / "fx" / "efx").mkdir(parents=True)
    (content / "fx" / "textures").mkdir(parents=True)
    (content / "fx" / "efx" / "test.efx").write_text(EFX_TEXT)
    (content / "fx" / "textures" / "spark_a.png").write_bytes(b"spark")
    (content / "fx" / "textures" / "muzflash2.png").write_bytes(b"flash")
    return content


def build_game(root: Path) -> tuple[Path, Path]:
    game_root = root / "game"
    archive_path = game_root / "Main" / "pak0.pk3"
    archive_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("fxshaders/pj_test.shader", SHADER_SCRIPT)
        archive.writestr("gfx/efx_assets/fire/spark_a.png", b"spark source")
        archive.writestr("gfx/effects/muzflash2.tga", b"flash source")
    return game_root, archive_path


class FxShaderManifestWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        cod1_script_exploder._SHADER_REFERENCE_CACHE.clear()
        cod1_script_exploder._SHADER_IMAGE_CACHE.clear()
        cod1_script_exploder._SHADER_DECLARATION_CACHE.clear()

    def test_writer_maps_declared_and_fallback_shaders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = build_content(root)
            game_root, archive_path = build_game(root)
            with (
                patch.object(
                    fod_package,
                    "selected_archives",
                    return_value=[archive_path],
                ),
                redirect_stdout(StringIO()) as output,
            ):
                fod_package.write_fx_shader_manifest(content, game_root)
            manifest = json.loads(
                (content / "fx" / "shaders.json").read_text()
            )
            self.assertEqual(
                manifest["format"], fod_package.FX_SHADERS_FORMAT
            )
            self.assertEqual(
                manifest["version"], fod_package.FX_SHADERS_VERSION
            )
            self.assertEqual(
                manifest["shaders"],
                [
                    {
                        "shader": "gfx/effects/muzflash2_sn",
                        "texture": "fx/textures/muzflash2.png",
                    },
                    {
                        "shader": "weapon_v/spark",
                        "texture": "fx/textures/spark_a.png",
                    },
                ],
            )
            # The unresolvable shader is omitted, not mapped to a file that
            # does not exist — and the omission is said out loud.
            self.assertIn("missing/unpackaged", output.getvalue())

    def test_every_written_entry_texture_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = build_content(root)
            game_root, archive_path = build_game(root)
            with (
                patch.object(
                    fod_package,
                    "selected_archives",
                    return_value=[archive_path],
                ),
                redirect_stdout(StringIO()),
            ):
                fod_package.write_fx_shader_manifest(content, game_root)
            manifest = json.loads(
                (content / "fx" / "shaders.json").read_text()
            )
            for entry in manifest["shaders"]:
                self.assertTrue((content / entry["texture"]).is_file(), entry)


class FxShaderManifestValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        cod1_script_exploder._SHADER_REFERENCE_CACHE.clear()
        cod1_script_exploder._SHADER_IMAGE_CACHE.clear()
        cod1_script_exploder._SHADER_DECLARATION_CACHE.clear()

    def test_missing_manifest_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            warnings: list[str] = []
            fod_package.validate_fx_shader_manifest(content, warnings)
            self.assertTrue(
                any("fx/shaders.json missing" in item for item in warnings),
                warnings,
            )

    def test_dangling_texture_and_uncovered_shader_warn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = build_content(root)
            (content / "fx" / "shaders.json").write_text(json.dumps({
                "format": fod_package.FX_SHADERS_FORMAT,
                "version": fod_package.FX_SHADERS_VERSION,
                "shaders": [
                    {
                        "shader": "weapon_v/spark",
                        "texture": "fx/textures/gone.png",
                    },
                ],
            }))
            warnings: list[str] = []
            fod_package.validate_fx_shader_manifest(content, warnings)
            self.assertTrue(
                any(
                    "references missing texture 'fx/textures/gone.png'"
                    in item
                    for item in warnings
                ),
                warnings,
            )
            self.assertTrue(
                any(
                    "lacks mappings for 2 shader(s)" in item
                    and "gfx/effects/muzflash2_sn" in item
                    for item in warnings
                ),
                warnings,
            )

    def test_complete_manifest_is_silent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = build_content(root)
            game_root, archive_path = build_game(root)
            (content / "fx" / "textures" / "unpackaged.png").write_bytes(
                b"third"
            )
            (content / "fx" / "shaders.json").write_text(json.dumps({
                "format": fod_package.FX_SHADERS_FORMAT,
                "version": fod_package.FX_SHADERS_VERSION,
                "shaders": [
                    {
                        "shader": "gfx/effects/muzflash2_sn",
                        "texture": "fx/textures/muzflash2.png",
                    },
                    {
                        "shader": "missing/unpackaged",
                        "texture": "fx/textures/unpackaged.png",
                    },
                    {
                        "shader": "weapon_v/spark",
                        "texture": "fx/textures/spark_a.png",
                    },
                ],
            }))
            warnings: list[str] = []
            fod_package.validate_fx_shader_manifest(content, warnings)
            self.assertEqual(warnings, [])


class WeaponEffectPresenceTests(unittest.TestCase):
    @staticmethod
    def _content(
        root: Path,
        efx_names: tuple[str, ...],
        world_muzzle_effect: str,
    ) -> Path:
        content = root / "content"
        (content / "fx" / "efx").mkdir(parents=True)
        for name in efx_names:
            (content / "fx" / "efx" / name).write_bytes(b"efx")
        (content / "weapons").mkdir(parents=True)
        (content / "weapons" / "weapons.json").write_text(json.dumps({
            "format": "FriendsOfDuty.Weapons",
            "version": 3,
            "weapons": [{
                "id": "kar98k",
                "definition": {
                    "muzzle_effect":
                        "fx/weapon/muzzleflash/mf_kar98_v.efx",
                    "world_muzzle_effect": world_muzzle_effect,
                    "shell_effect": "",
                },
            }],
        }))
        return content

    def test_doubled_extension_reference_resolves_flattened_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = self._content(
                Path(temporary),
                ("mf_kar98_v.efx", "mf_kar98.efx"),
                # Retail kar98k_mp's exact spelling.
                "fx/weapon/muzzleflash/mf_kar98.efx.efx",
            )
            warnings: list[str] = []
            fod_package.validate_weapon_effect_presence(content, warnings)
            self.assertEqual(warnings, [])

    def test_absent_world_flash_efx_warns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = self._content(
                Path(temporary),
                ("mf_kar98_v.efx",),
                "fx/weapon/muzzleflash/mf_kar98.efx.efx",
            )
            warnings: list[str] = []
            fod_package.validate_weapon_effect_presence(content, warnings)
            self.assertEqual(len(warnings), 1, warnings)
            self.assertIn("world_muzzle_effect", warnings[0])
            self.assertIn("kar98k", warnings[0])


if __name__ == "__main__":
    unittest.main()
