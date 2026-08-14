"""Third-person muzzle-flash extraction: the WEAPONFILE effect-field walk,
the nested playFx/shader-sprite closure, retail's doubled-extension typo, and
the flattened-basename collision guard."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXPORTER = ROOT / "exporter"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(EXPORTER))

import cod1_script_exploder  # noqa: E402
import extract_cod1_weapon_presentation as weapon_presentation  # noqa: E402


def png_bytes(color: tuple[int, int, int, int]) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (2, 2), color).save(buffer, format="PNG")
    return buffer.getvalue()


WORLD_FLASH_EFX = """FxRunner
{
    playfx [ fx/weapon/muzzleflash/_global_r ]
}
Sprite
{
    shaders [ weapon_v/spark ]
}
"""

GLOBAL_LIGHT_EFX = """Light
{
    shaders [ gfx/effects/muzflash2_sn ]
}
"""

# gfx/effects/muzflash2_sn has a declaration mapping the suffixed name back
# to the base texture — the retail *_sn view-flash scheme. weapon_v/spark
# names a .dds that ships (here) only as .png, exercising the engine's
# image-extension fallback.
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


class WeaponPresentationFxTests(unittest.TestCase):
    def setUp(self) -> None:
        # The exploder caches shader tables by id(index); a dead index's id
        # can be reused by a fresh one between tests in one process.
        cod1_script_exploder._SHADER_REFERENCE_CACHE.clear()
        cod1_script_exploder._SHADER_IMAGE_CACHE.clear()
        cod1_script_exploder._SHADER_DECLARATION_CACHE.clear()

    def _run(
        self,
        root: Path,
        archive_members: dict[str, bytes],
        weapon_fields: dict[str, str],
        turret_pins: tuple[str, ...] = (),
    ) -> Path:
        archive_path = root / "game" / "Main" / "pak0.pk3"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path, "w") as archive:
            for member, data in archive_members.items():
                archive.writestr(member, data)
        source = root / "source" / "weapons" / "mp"
        source.mkdir(parents=True, exist_ok=True)
        if weapon_fields:
            (source / "kar98k_mp").write_text(
                "WEAPONFILE"
                + "".join(
                    f"\\{field}\\{value}"
                    for field, value in weapon_fields.items()
                ),
            )
        pak = root / "pak"

        analysis = SimpleNamespace(sound_aliases=set(), effect_paths=())
        arguments = [
            "extract_cod1_weapon_presentation.py",
            str(archive_path.parent),
            str(root / "source"),
            "--pak-root",
            str(pak),
        ]
        with (
            patch.object(
                weapon_presentation,
                "official_archives",
                return_value=[archive_path],
            ),
            patch.object(
                weapon_presentation,
                "selected_shipping_map_ids",
                return_value=(),
            ),
            patch.object(
                weapon_presentation,
                "analyze_multiplayer_gsc",
                return_value=analysis,
            ),
            patch.object(
                weapon_presentation,
                "PRESENTATION_BASE_FILES",
                (),
            ),
            patch.object(
                weapon_presentation,
                "MOUNTED_TURRET_FLASH_EFX",
                turret_pins,
            ),
            patch.object(
                weapon_presentation,
                "GLOBAL_MULTIPLAYER_SOUND_ALIASES",
                (),
            ),
            patch.object(sys, "argv", arguments),
            redirect_stdout(StringIO()),
        ):
            weapon_presentation.main()
        return pak

    def test_world_flash_closure_ships_nested_efx_and_sprites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pak = self._run(
                root,
                {
                    "fx/weapon/muzzleflash/mf_test.efx": WORLD_FLASH_EFX,
                    "fx/weapon/muzzleflash/_global_r.efx": GLOBAL_LIGHT_EFX,
                    "fxshaders/pj_test.shader": SHADER_SCRIPT,
                    "gfx/efx_assets/fire/spark_a.png": png_bytes(
                        (255, 128, 0, 255)
                    ),
                    "gfx/effects/muzflash2.tga": png_bytes(
                        (255, 255, 255, 255)
                    ),
                },
                # Retail UO spells three worldFlashEffect values with the
                # doubled extension; the member on disk has one.
                {"worldFlashEffect": "fx/weapon/muzzleflash/mf_test.efx.efx"},
            )

            self.assertEqual(
                {child.name for child in (pak / "fx" / "efx").iterdir()},
                {"mf_test.efx", "_global_r.efx"},
            )
            self.assertEqual(
                {child.name for child in (pak / "fx" / "textures").iterdir()},
                {"spark_a.png", "muzflash2.png"},
            )
            manifest = json.loads(
                (pak / "audio" / "presentation.json").read_text()
            )
            self.assertEqual(
                manifest["presentation_files"],
                [
                    "fx/weapon/muzzleflash/_global_r.efx",
                    "fx/weapon/muzzleflash/mf_test.efx",
                    "gfx/effects/muzflash2.tga",
                    "gfx/efx_assets/fire/spark_a.png",
                ],
            )
            provenance = {
                record["path"] for record in manifest["provenance"]
            }
            self.assertEqual(
                provenance,
                {
                    "fx/efx/mf_test.efx",
                    "fx/efx/_global_r.efx",
                    "fx/textures/spark_a.png",
                    "fx/textures/muzflash2.png",
                },
            )

    def test_turret_flash_pins_are_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pak = self._run(
                root,
                {
                    "fx/muzzleflashes/mg42hv.efx": (
                        "Sprite\n{\n    shaders [ weapon_v/spark ]\n}\n"
                    ),
                    "fxshaders/pj_test.shader": SHADER_SCRIPT,
                    "gfx/efx_assets/fire/spark_a.png": png_bytes(
                        (255, 0, 0, 255)
                    ),
                },
                {},
                turret_pins=("fx/muzzleflashes/mg42hv.efx",),
            )
            self.assertTrue((pak / "fx" / "efx" / "mg42hv.efx").is_file())
            self.assertTrue(
                (pak / "fx" / "textures" / "spark_a.png").is_file()
            )

    def test_flatten_collision_with_different_content_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(
                RuntimeError,
                "basename collision",
            ):
                self._run(
                    root,
                    {
                        "fx/a/dup.efx": b"alpha effect",
                        "fx/b/dup.efx": b"beta effect",
                    },
                    {
                        "viewFlashEffect": "fx/a/dup.efx",
                        "worldFlashEffect": "fx/b/dup.efx",
                    },
                )

    def test_flatten_duplicate_with_identical_content_extracts_once(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pak = self._run(
                root,
                {
                    "fx/a/dup.efx": b"same effect",
                    "fx/b/dup.efx": b"same effect",
                },
                {
                    "viewFlashEffect": "fx/a/dup.efx",
                    "worldFlashEffect": "fx/b/dup.efx",
                },
            )
            self.assertEqual(
                {child.name for child in (pak / "fx" / "efx").iterdir()},
                {"dup.efx"},
            )
            manifest = json.loads(
                (pak / "audio" / "presentation.json").read_text()
            )
            self.assertEqual(
                manifest["presentation_files"],
                ["fx/a/dup.efx", "fx/b/dup.efx"],
            )


if __name__ == "__main__":
    unittest.main()
