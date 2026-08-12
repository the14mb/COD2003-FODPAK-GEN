from __future__ import annotations

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

import extract_cod1_weapon_presentation as weapon_presentation  # noqa: E402
from extract_cod1_ordnance import (  # noqa: E402
    MemberIndex,
    merge_presentation,
)
import package as fod_package  # noqa: E402


CSV_ROW = (
    "grenade_explode_default,,Explosions/Explo_Metal01.wav,"
    "1,1,1,1,120,0\n"
)
RETAIL_MEMBER = "Sound/Explosions/Explo_Metal01.wav"
PACKAGED_FILE = "explosions/explo_metal01.wav"


def write_archive(path: Path, alias: str = "grenade_explode_default") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = CSV_ROW.replace("grenade_explode_default", alias)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("SoundAliases/IW_Sound.CSV", row)
        archive.writestr(RETAIL_MEMBER, b"mixed-case retail audio")


class AudioPathCasingTests(unittest.TestCase):
    def test_weapon_presentation_packages_mixed_case_member_lowercase(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "game" / "Main" / "pak0.pk3"
            write_archive(archive_path, alias="minefield_click")
            source = root / "source" / "weapons" / "mp"
            source.mkdir(parents=True)
            pak = root / "pak"

            analysis = SimpleNamespace(
                sound_aliases=set(),
                effect_paths=(),
            )
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
                    "GLOBAL_MULTIPLAYER_SOUND_ALIASES",
                    ("minefield_click",),
                ),
                patch.object(sys, "argv", arguments),
                redirect_stdout(StringIO()),
            ):
                weapon_presentation.main()

            manifest = json.loads(
                (pak / "audio" / "presentation.json").read_text()
            )
            alias = next(
                entry
                for entry in manifest["aliases"]
                if entry["alias"] == "minefield_click"
            )
            self.assertEqual(alias["variants"][0]["file"], PACKAGED_FILE)
            self.assertEqual(
                {child.name for child in (pak / "audio").iterdir()},
                {"explosions", "presentation.json"},
            )
            self.assertEqual(
                {child.name for child in (pak / "audio" / "explosions").iterdir()},
                {"explo_metal01.wav"},
            )
            record = next(
                item
                for item in manifest["provenance"]
                if item["path"] == "audio/" + PACKAGED_FILE
            )
            self.assertEqual(record["member"], RETAIL_MEMBER)

    def test_new_weapon_foley_aliases_package_lowercase(self) -> None:
        # The melee swing / dry click globals and the WEAPONFILE
        # altSwitchSound field all package through the same canonical
        # lowercase path policy as every other alias — a mixed-case
        # retail member must never leak its casing into the pak
        # (fatal on SteamOS).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "game" / "Main" / "pak0.pk3"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "SoundAliases/IW_Sound.CSV",
                    "melee_swing_small,1,Weapons/Melee/Melee_Swing01.wav,"
                    "1.07,1.25,0.9,1.05,100,250\n"
                    "player_out_of_ammo,,Weapons/Dry_Fire.wav,"
                    "0.45,0.57,0.9,1.1,7,500\n"
                    "weap_bar_altswitch,,Weapons/Alt_Fire_Switch.wav,"
                    "1,1,,,7,500\n",
                )
                archive.writestr(
                    "Sound/Weapons/Melee/Melee_Swing01.wav",
                    b"swing",
                )
                archive.writestr(
                    "Sound/Weapons/Dry_Fire.wav",
                    b"click",
                )
                archive.writestr(
                    "Sound/Weapons/Alt_Fire_Switch.wav",
                    b"switch",
                )
            source = root / "source" / "weapons" / "mp"
            source.mkdir(parents=True)
            (source / "bar_mp").write_text(
                "WEAPONFILE\\altSwitchSound\\weap_bar_altswitch",
            )
            pak = root / "pak"

            analysis = SimpleNamespace(
                sound_aliases=set(),
                effect_paths=(),
            )
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
                    "GLOBAL_MULTIPLAYER_SOUND_ALIASES",
                    ("melee_swing_small", "player_out_of_ammo"),
                ),
                patch.object(sys, "argv", arguments),
                redirect_stdout(StringIO()),
            ):
                weapon_presentation.main()

            manifest = json.loads(
                (pak / "audio" / "presentation.json").read_text()
            )
            packaged = {
                entry["alias"]: [
                    variant["file"]
                    for variant in entry["variants"]
                ]
                for entry in manifest["aliases"]
            }
            self.assertEqual(
                packaged["melee_swing_small"],
                ["weapons/melee/melee_swing01.wav"],
            )
            self.assertEqual(
                packaged["player_out_of_ammo"],
                ["weapons/dry_fire.wav"],
            )
            self.assertEqual(
                packaged["weap_bar_altswitch"],
                ["weapons/alt_fire_switch.wav"],
            )
            weapon_audio = pak / "audio" / "weapons"
            self.assertEqual(
                {child.name for child in weapon_audio.iterdir()},
                {
                    "melee",
                    "dry_fire.wav",
                    "alt_fire_switch.wav",
                },
            )
            self.assertEqual(
                {
                    child.name
                    for child in (weapon_audio / "melee").iterdir()
                },
                {"melee_swing01.wav"},
            )

    def test_flesh_hit_aliases_package_lowercase(self) -> None:
        # The victim-side flesh hit reports are pinned globals like the
        # melee swings; their retail members live under the mixed-case
        # Sound/Weapons/Impact/ tree and must package through the same
        # canonical lowercase path policy (fatal on SteamOS otherwise).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "game" / "Main" / "pak0.pk3"
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "SoundAliases/IW_Sound.CSV",
                    "bullet_small_flesh,1,"
                    "Weapons/Impact/Impact_Flesh_02.wav,"
                    "1.2,1.5,0.9,1.1,180,1400\n"
                    "bullet_large_flesh,1,"
                    "Weapons/Impact/bullet_Flesh_mega.wav,"
                    "1.2,1.5,0.9,1.1,180,1400\n",
                )
                archive.writestr(
                    "Sound/Weapons/Impact/Impact_Flesh_02.wav",
                    b"small",
                )
                archive.writestr(
                    "Sound/Weapons/Impact/bullet_Flesh_mega.wav",
                    b"large",
                )
            source = root / "source" / "weapons" / "mp"
            source.mkdir(parents=True)
            pak = root / "pak"

            analysis = SimpleNamespace(
                sound_aliases=set(),
                effect_paths=(),
            )
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
                    "GLOBAL_MULTIPLAYER_SOUND_ALIASES",
                    ("bullet_small_flesh", "bullet_large_flesh"),
                ),
                patch.object(sys, "argv", arguments),
                redirect_stdout(StringIO()),
            ):
                weapon_presentation.main()

            manifest = json.loads(
                (pak / "audio" / "presentation.json").read_text()
            )
            packaged = {
                entry["alias"]: [
                    variant["file"]
                    for variant in entry["variants"]
                ]
                for entry in manifest["aliases"]
            }
            self.assertEqual(
                packaged["bullet_small_flesh"],
                ["weapons/impact/impact_flesh_02.wav"],
            )
            self.assertEqual(
                packaged["bullet_large_flesh"],
                ["weapons/impact/bullet_flesh_mega.wav"],
            )
            self.assertEqual(
                {
                    child.name
                    for child in (
                        pak / "audio" / "weapons" / "impact"
                    ).iterdir()
                },
                {
                    "impact_flesh_02.wav",
                    "bullet_flesh_mega.wav",
                },
            )

    def test_ordnance_rerun_case_renames_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "game" / "Main" / "pak0.pk3"
            write_archive(archive_path)
            pak = root / "pak"
            old_file = (
                pak / "audio" / "Explosions" / "Explo_Metal01.wav"
            )
            old_file.parent.mkdir(parents=True)
            old_file.write_bytes(b"old")
            (pak / "audio" / "presentation.json").write_text(json.dumps({
                "aliases": [{
                    "alias": "grenade_explode_default",
                    "variants": [{
                        "file": "Explosions/Explo_Metal01.wav",
                        "volume_min": 1.0,
                        "volume_max": 1.0,
                        "pitch_min": 1.0,
                        "pitch_max": 1.0,
                        "distance_min_inches": 120.0,
                        "distance_max_inches": 0.0,
                    }],
                }],
                "presentation_files": [],
                "sourceArchives": [],
                "provenance": [],
            }))

            with redirect_stdout(StringIO()):
                merge_presentation(
                    pak,
                    MemberIndex([archive_path]),
                    [],
                    [archive_path],
                )

            manifest = json.loads(
                (pak / "audio" / "presentation.json").read_text()
            )
            self.assertEqual(
                manifest["aliases"][0]["variants"][0]["file"],
                PACKAGED_FILE,
            )
            self.assertIn(
                "explosions",
                {child.name for child in (pak / "audio").iterdir()},
            )
            self.assertEqual(
                {child.name for child in (pak / "audio" / "explosions").iterdir()},
                {"explo_metal01.wav"},
            )
            record = next(
                item
                for item in manifest["provenance"]
                if item["path"] == "audio/" + PACKAGED_FILE
            )
            self.assertEqual(record["member"], RETAIL_MEMBER)

    def test_retail_reconstruction_expects_lowercase_package_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "game" / "Main" / "pak0.pk3"
            write_archive(archive_path)

            class RetailIndex:
                @staticmethod
                def get(member: str) -> object | None:
                    if member.casefold() == RETAIL_MEMBER.casefold():
                        return SimpleNamespace(name=RETAIL_MEMBER)
                    return None

            with patch.object(
                fod_package,
                "selected_archives",
                return_value=[archive_path],
            ):
                rows = fod_package._mp_gsc_retail_alias_files(
                    root / "game",
                    retail_index=RetailIndex(),
                )
            self.assertEqual(
                rows["grenade_explode_default"][0]["file"],
                PACKAGED_FILE,
            )

    def test_content_path_rejects_manifest_disk_case_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            disk_file = (
                content / "audio" / "Explosions" / "Explo_Metal01.wav"
            )
            disk_file.parent.mkdir(parents=True)
            disk_file.write_bytes(b"audio")
            errors: list[str] = []
            fod_package.content_path(
                content,
                "audio/" + PACKAGED_FILE,
                "audio test",
                errors,
            )
            self.assertTrue(
                any("path casing differs from disk" in error for error in errors),
                errors,
            )


if __name__ == "__main__":
    unittest.main()
