from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The Call of Duty installs and fod_content/ live beside the repo, not in it.
# Kept separate from ROOT so a test can never look for game data among the
# source. Mirrors pipeline.DATA_ROOT, which is asserted below.
DATA_ROOT = ROOT.parent
TOOLS = ROOT / "tools"
EXPORTER = ROOT / "exporter"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(EXPORTER))

from cod1_archive_policy import (  # noqa: E402
    COD1_ARCHIVES,
    UO_ARCHIVES,
    official_archives,
    source_policy_manifest,
    validate_policy_manifest,
)
from cod1_playeranim import (  # noqa: E402
    MG42_HORIZONTAL,
    MG42_VERTICAL,
    build_inventory,
    parse_active_references,
)
from cod1_weapon_metadata import (  # noqa: E402
    parse_weapon_file,
    weapon_animation_metadata,
)
from extract_cod1_impacts import (  # noqa: E402
    MINEFIELD_EFX,
    MINEFIELD_TEXTURES,
)
from extract_cod1_assets import is_multiplayer_source  # noqa: E402
from extract_cod1_weapon_presentation import (  # noqa: E402
    GLOBAL_MULTIPLAYER_SOUND_ALIASES,
    SOUND_FIELDS,
)
import import_cod_multiplayer_maps as map_importer  # noqa: E402
import pipeline  # noqa: E402


PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "fod_package_under_test",
    EXPORTER / "package.py",
)
assert PACKAGE_SPEC is not None and PACKAGE_SPEC.loader is not None
fod_package = importlib.util.module_from_spec(PACKAGE_SPEC)
PACKAGE_SPEC.loader.exec_module(fod_package)


class RepositoryLayoutTests(unittest.TestCase):
    """The exporter and the tests each resolve the two roots themselves. They
    have to agree: if they drift, tests quietly look for game data in the
    wrong place and skip instead of fail."""

    def test_roots_match_the_pipeline(self) -> None:
        self.assertEqual(pipeline.REPO_ROOT, ROOT)
        self.assertEqual(pipeline.DATA_ROOT, DATA_ROOT)

    def test_source_lives_in_the_repo_and_data_does_not(self) -> None:
        # exporter/ and tools/ are versioned here; the content package and the
        # game installs are deliberately outside, so a reclone never carries
        # copyrighted data and never loses the toolchain.
        self.assertTrue((ROOT / "exporter" / "pipeline.py").is_file())
        self.assertTrue((ROOT / "tools").is_dir())
        self.assertEqual(DATA_ROOT, ROOT.parent)
        self.assertFalse((ROOT / "Call of Duty").exists())


class OfficialArchivePolicyTests(unittest.TestCase):
    def _fake_install(self, root: Path, uo: str = "complete") -> None:
        """Fake retail tree. uo is 'complete', 'absent' or 'partial'.

        Defaults to complete because United Offensive is mandatory — a tree
        without it is now the exceptional case, not the baseline.
        """
        (root / "Main").mkdir(parents=True)
        for index, name in enumerate(COD1_ARCHIVES):
            (root / "Main" / name).write_bytes(f"main-{index}".encode())
        (root / "Main" / "w_canka_awe_v2.pk3").write_bytes(b"mod")
        (root / "Main" / "localized_french_pak0.pk3").write_bytes(
            b"unselected locale"
        )
        if uo == "absent":
            return
        (root / "uo").mkdir()
        names = UO_ARCHIVES if uo == "complete" else UO_ARCHIVES[:3]
        for index, name in enumerate(names):
            (root / "uo" / name).write_bytes(f"uo-{index}".encode())
        (root / "uo" / "nevado.pk3").write_bytes(b"custom-map")

    def test_allowlist_keeps_selected_locale_and_ignores_mods(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._fake_install(root)
            self.assertEqual(
                [path.name for path in official_archives(root, "cod1")],
                list(COD1_ARCHIVES),
            )
            self.assertEqual(
                [path.name for path in official_archives(root, "uo")],
                list(UO_ARCHIVES),
            )

    def test_policy_manifest_records_hashes_and_rejects_unknown_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._fake_install(root)
            manifest = source_policy_manifest(root)
            # Both tiers, always: United Offensive is not selectable.
            self.assertEqual(manifest["tiers"], ["cod1", "uo"])
            self.assertEqual(
                len(manifest["archives"]),
                len(COD1_ARCHIVES) + len(UO_ARCHIVES),
            )
            self.assertTrue(
                all(len(record["sha256"]) == 64 for record in manifest["archives"])
            )
            manifest["archives"][0]["path"] = "Main/evil_mod.pk3"
            errors = validate_policy_manifest(manifest)
            self.assertTrue(
                any("nonofficial source archive" in error for error in errors)
            )

    def test_multiplayer_source_filter_excludes_sp_weaponfiles(self) -> None:
        self.assertTrue(is_multiplayer_source("weapons/mp/bar_mp"))
        self.assertTrue(is_multiplayer_source("mp/playeranim.script"))
        # Global roots mix campaign and multiplayer data. Even a known MP
        # name is admitted only through the dependency closure, never from
        # the path prefix alone.
        self.assertFalse(is_multiplayer_source("xanim/pb_sprint"))
        self.assertFalse(is_multiplayer_source("weapons/sp/bar"))
        self.assertFalse(is_multiplayer_source("maps/sp/trainstation.bsp"))

    @unittest.skipUnless(
        (
            DATA_ROOT
            / "Call of Duty"
            / "Main"
            / "localized_english_pak1.pk3"
        ).is_file()
        and (
            DATA_ROOT
            / "fod_content"
            / ".fod_staging"
            / "cod1_source"
            / "weapons"
            / "mp"
            / "bar_mp"
        ).is_file(),
        "local retail localized archive/staging unavailable",
    )
    def test_reached_localized_mp_voice_is_packaged_but_sp_voice_is_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pak_root = Path(temporary) / "pak"
            result = subprocess.run(
                [
                    sys.executable,
                    str(TOOLS / "extract_cod1_weapon_presentation.py"),
                    str(DATA_ROOT / "Call of Duty" / "Main"),
                    str(
                        DATA_ROOT
                        / "fod_content"
                        / ".fod_staging"
                        / "cod1_source"
                    ),
                    "--pak-root",
                    str(pak_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(
                (
                    pak_root / "audio" / "presentation.json"
                ).read_text()
            )
            aliases = {
                entry["alias"].casefold(): entry
                for entry in manifest["aliases"]
            }

            # maps/mp/_teams.gsc reaches this alias. Its bytes exist only in
            # the official localized English pak even though the alias CSV
            # declaration itself is in Main/pak1.pk3.
            move_in = aliases["american_move_in"]
            self.assertTrue(move_in["variants"])
            mp_files = {
                variant["file"].casefold()
                for variant in move_in["variants"]
            }
            self.assertIn(
                "voiceovers/mp/us/cmds/us_cmd_movein01.wav",
                mp_files,
            )
            provenance = {
                record["path"].casefold(): record
                for record in manifest["provenance"]
            }
            move_record = provenance[
                "audio/voiceovers/mp/us/cmds/"
                "us_cmd_movein01.wav"
            ]
            self.assertEqual(
                move_record["archive"],
                "Main/localized_english_pak1.pk3",
            )

            # The same selected archive contains campaign dialog. Archive
            # admission is not member admission: no SP alias or file may
            # cross the maps/mp reachability boundary.
            self.assertNotIn("buildingcommissar", aliases)
            self.assertFalse(
                any(
                    record["member"].casefold() ==
                    "sound/voiceovers/stalingrad/"
                    "buildingcommissar.wav"
                    for record in manifest["provenance"]
                )
            )
            self.assertFalse(
                (
                    pak_root
                    / "audio"
                    / "voiceovers"
                    / "stalingrad"
                    / "BuildingCommissar.wav"
                ).exists()
            )


class PlayerAnimationInventoryTests(unittest.TestCase):
    def test_parser_handles_comments_compound_bindings_and_casefolding(self) -> None:
        references = parse_active_references(
            """
            /* both commented_out */
            ANIMATIONS
            STATE COMBAT
            {
              walk
              {
                default
                {
                  legs walk torso SHOOT
                  torso shoot
                  both turret_pose turretanim
                }
              }
            }
            EVENTS
            meleeattack
            {
              default
              {
                torso MELEE
              }
            }
            // legs line_comment
            """
        )
        self.assertEqual(
            [reference.name for reference in references],
            ["walk", "SHOOT", "turret_pose", "MELEE"],
        )
        self.assertEqual(references[1].occurrences, 2)
        self.assertEqual(references[1].layers, {"torso"})
        self.assertEqual(
            references[1].action_roles,
            {"movement:combat/walk"},
        )
        self.assertTrue(references[2].turret_animation)
        self.assertEqual(
            references[3].action_roles,
            {"event:meleeattack"},
        )

    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "Main" / "pak4.pk3").is_file()
        and (DATA_ROOT / "fod_content" / ".fod_staging" / "cod1_source"
             / "xanim").is_dir(),
        "local retail archives/staging unavailable",
    )
    def test_local_retail_playeranim_coverage_counts(self) -> None:
        xanim_root = (
            DATA_ROOT / "fod_content" / ".fod_staging"
            / "cod1_source" / "xanim"
        )
        # (archive, active identifiers, exported animations, expanded
        # turret aliases, expanded turret animations, excluded turret
        # aliases). Only United Offensive's script binds the jeep, so only
        # its row loses animations to the vehicle cut.
        cases = (
            ("Main/pak4.pk3", 165, 269, 4, 108, ()),
            (
                "uo/pakuo01.pk3",
                258,
                354,
                4,
                108,
                (
                    "vehicleJeepDriverIdle",
                    "vehicleJeepDriverDriving",
                    "vehicleJeepGunnerFire",
                    "vehicleJeepGunnerAim",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for (
                relative,
                active,
                exported,
                aliases,
                expanded,
                excluded,
            ) in cases:
                with zipfile.ZipFile(DATA_ROOT / "Call of Duty" / relative) as archive:
                    data = archive.read("mp/playeranim.script")
                script = Path(temporary) / relative.replace("/", "_")
                script.write_bytes(data)
                inventory = build_inventory(script, xanim_root)
                self.assertEqual(inventory.active_identifier_count, active)
                self.assertEqual(len(inventory.animations), exported)
                self.assertEqual(len(inventory.expanded_turret_aliases), aliases)
                self.assertEqual(
                    inventory.expanded_turret_animation_count,
                    expanded,
                )
                self.assertEqual(
                    inventory.excluded_turret_animations,
                    excluded,
                )
                self.assertTrue(
                    all(reference.action_roles for reference in inventory.animations)
                )
                names = {
                    reference.name.casefold()
                    for reference in inventory.animations
                }
                self.assertFalse(
                    any(
                        name.startswith(("standmg42gun_", "pronemg42gun_"))
                        or name.startswith(
                            (
                                "pb_standmg42gunner_",
                                "pb_pronemg42gunner_",
                            )
                        )
                        for name in names
                    )
                )
                mg42_references = [
                    reference
                    for reference in inventory.animations
                    if reference.turret_weapon == "mg42"
                ]
                self.assertEqual(len(mg42_references), 108)
                self.assertTrue(
                    all(
                        reference.source_alias
                        and any(
                            role.startswith("turret:")
                            for role in reference.action_roles
                        )
                        for reference in mg42_references
                    )
                )
                self.assertEqual(
                    {
                        (
                            reference.turret_stance,
                            reference.turret_horizontal,
                            reference.turret_vertical,
                        )
                        for reference in mg42_references
                        if reference.turret_phase == "aim"
                    },
                    {
                        (stance, horizontal, vertical)
                        for stance, horizontals in MG42_HORIZONTAL.items()
                        for horizontal in horizontals
                        for vertical in MG42_VERTICAL
                    },
                )
                # No vehicle clip of any kind survives: the driver/gunner
                # turret families are refused as aliases, and the four direct
                # c_mp_jeep_passenger_* bindings are dropped by name.
                self.assertFalse(
                    any(
                        name.startswith("c_mp_jeep_")
                        for name in names
                    )
                )


class WeaponMetadataTests(unittest.TestCase):
    PRIMARY_WEAPON_FILES = (
        "bar_mp", "bren_mp", "colt_mp", "enfield_mp", "fg42_mp",
        "fraggrenade_mp", "kar98k_mp", "kar98k_sniper_mp", "luger_mp",
        "m1carbine_mp", "m1garand_mp", "mk1britishfrag_mp",
        "mosin_nagant_mp", "mosin_nagant_sniper_mp", "mp40_mp",
        "mp44_mp", "panzerfaust_mp", "ppsh_mp",
        "rgd-33russianfrag_mp", "springfield_mp", "sten_mp",
        "stielhandgranate_mp", "thompson_mp", "smokegrenade_mp",
    )

    @unittest.skipUnless(
        (DATA_ROOT / "fod_content" / ".fod_staging" / "cod1_source"
         / "weapons" / "mp" / "bar_mp").is_file(),
        "local staged WEAPONFILEs unavailable",
    )
    def test_bar_unions_slow_variant_and_emits_melee_values(self) -> None:
        source = DATA_ROOT / "fod_content" / ".fod_staging" / "cod1_source"
        metadata = weapon_animation_metadata(source, "bar_mp")
        self.assertEqual(metadata["alternateWeaponFile"], "bar_slow_mp")
        self.assertIn("viewmodel_bar_lastshot", metadata["animations"])
        self.assertTrue(
            any(
                role["variant"] == "alternate"
                and role["role"] == "last_fire"
                and role["sourceAnimation"] == "viewmodel_bar_lastshot"
                for role in metadata["animationRoles"]
            )
        )
        self.assertEqual(metadata["meleeDamage"], 200.0)
        self.assertEqual(metadata["meleeDelay"], 0.15)
        self.assertEqual(metadata["meleeTime"], 0.95)

    @unittest.skipUnless(
        (DATA_ROOT / "fod_content" / ".fod_staging" / "cod1_source"
         / "weapons" / "mp" / "smokegrenade_mp").is_file(),
        "local staged base/UO WEAPONFILEs unavailable",
    )
    def test_every_selected_weapon_has_idle_fire_melee_and_valid_sources(
        self,
    ) -> None:
        source = DATA_ROOT / "fod_content" / ".fod_staging" / "cod1_source"
        for weapon_file in self.PRIMARY_WEAPON_FILES:
            with self.subTest(weapon_file=weapon_file):
                metadata = weapon_animation_metadata(source, weapon_file)
                roles = {
                    record["role"]
                    for record in metadata["animationRoles"]
                }
                self.assertTrue({"idle", "fire", "melee"} <= roles)
                self.assertGreater(metadata["meleeDamage"], 0)
                self.assertGreater(metadata["meleeTime"], 0)
                self.assertGreaterEqual(metadata["meleeDelay"], 0)
                self.assertTrue(
                    all(
                        record["weaponFile"].casefold().endswith("_mp")
                        and "/" not in record["weaponFile"]
                        and "\\" not in record["weaponFile"]
                        for record in metadata["animationRoles"]
                    )
                )


class PipelineAndPackageTests(unittest.TestCase):
    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "Main" / "pak5.pk3").is_file(),
        "local retail archives unavailable",
    )
    def test_minefield_selection_is_exact_mp_effect_graph(self) -> None:
        self.assertEqual(
            set(GLOBAL_MULTIPLAYER_SOUND_ALIASES),
            {
                "minefield_click",
                "explo_mine",
                "health_pickup_medium",
                "melee_swing_small",
                "melee_swing_large",
                "player_out_of_ammo",
                "bullet_small_flesh",
                "bullet_large_flesh",
                # UO barrel overheat (EV_OVERHEATING). Engine-played, named
                # by no WEAPONFILE key, so it has to be pinned globally like
                # the melee swings and flesh reports above.
                "mg_overheat",
                # The match-result announcer. Only the round-based gametype
                # scripts named these, and none of those ship, but Team
                # Deathmatch announces its winner — so they are pinned here
                # rather than reached through a script the closure dropped.
                "mp_announcer_allies_win",
                "mp_announcer_axis_win",
                "mp_announcer_round_draw",
            },
        )
        self.assertEqual(
            dict(MINEFIELD_EFX),
            {
                "minefield": "fx/impacts/newimps/minefield.efx",
                "fluff1": "fx/impacts/fluff1.efx",
                "dirthit_mortar": "fx/impacts/dirthit_mortar.efx",
            },
        )
        self.assertEqual(
            {name for name, _members in MINEFIELD_TEXTURES},
            set(fod_package.MINEFIELD_FX_TEXTURES) - {"dustlayer1"},
        )
        with zipfile.ZipFile(
            DATA_ROOT / "Call of Duty" / "Main" / "pak5.pk3"
        ) as archive:
            for _name, member in MINEFIELD_EFX:
                self.assertIn(member, archive.namelist())
            for _name, candidates in MINEFIELD_TEXTURES:
                self.assertTrue(
                    any(member in archive.namelist() for member in candidates)
                )
        with zipfile.ZipFile(
            DATA_ROOT / "Call of Duty" / "Main" / "pak1.pk3"
        ) as archive:
            rows = {
                row[0]: row
                for row in csv.reader(
                    archive.read(
                        "soundaliases/iw_sound.csv"
                    ).decode("utf-8-sig").splitlines()
                )
                if row and row[0] in
                GLOBAL_MULTIPLAYER_SOUND_ALIASES
            }
        self.assertEqual(
            rows["minefield_click"][2:9],
            [
                "misc/metal_click.wav",
                "0.8", "0.94", "", "", "500", "",
            ],
        )
        self.assertEqual(
            rows["explo_mine"][2:9],
            [
                "explosions/explo_mine01.wav",
                "1.5", "1.5", "0.8", "1.1", "750", "6000",
            ],
        )

    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "Main" / "pak1.pk3").is_file(),
        "local retail archives unavailable",
    )
    def test_weapon_foley_aliases_resolve_from_retail_rows(self) -> None:
        # The alt-mode switch rides the WEAPONFILE altSwitchSound field;
        # the swing pair and the dry click are engine-called and pinned
        # as global aliases.
        self.assertIn("altSwitchSound", SOUND_FIELDS)
        with zipfile.ZipFile(
            DATA_ROOT / "Call of Duty" / "Main" / "pak1.pk3"
        ) as archive:
            rows: dict[str, list[list[str]]] = {}
            for row in csv.reader(
                archive.read(
                    "soundaliases/iw_sound.csv"
                ).decode("utf-8-sig").splitlines()
            ):
                if row and row[0] in (
                    "melee_swing_small",
                    "melee_swing_large",
                    "player_out_of_ammo",
                    "weap_bar_altswitch",
                ):
                    rows.setdefault(row[0], []).append(row)
            members = {name.casefold() for name in archive.namelist()}

        for swing in ("melee_swing_small", "melee_swing_large"):
            self.assertTrue(rows[swing])
            for row in rows[swing]:
                # 100..250-unit envelope, distinct from the 7..500 the
                # rest of the weapon foley uses.
                self.assertEqual(row[7:9], ["100", "250"])
                self.assertIn(
                    ("sound/" + row[2]).casefold(),
                    members,
                )
        self.assertEqual(
            rows["player_out_of_ammo"][0][2:9],
            [
                "weapons/dry_fire.wav",
                "0.45", "0.57", "0.9", "1.1", "7", "500",
            ],
        )
        self.assertIn("sound/weapons/dry_fire.wav", members)
        self.assertEqual(
            rows["weap_bar_altswitch"][0][2],
            "weapons/alt_fire_switch.wav",
        )
        self.assertIn("sound/weapons/alt_fire_switch.wav", members)

    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "Main" / "pak1.pk3").is_file(),
        "local retail archives unavailable",
    )
    def test_flesh_hit_aliases_resolve_from_retail_mp_rows(self) -> None:
        # The victim-side hit reports are engine-called (no WEAPONFILE
        # field names them) and multiplayer-gated: iw_sound.csv carries
        # both an all_sp and an all_mp row set for each alias, and the
        # MP set widens the envelope to 180..1400 units. The exporter
        # ingests every non-null row, so the MP rows must exist and
        # every referenced WAV must resolve inside sound/.
        with zipfile.ZipFile(
            DATA_ROOT / "Call of Duty" / "Main" / "pak1.pk3"
        ) as archive:
            rows: dict[str, list[list[str]]] = {}
            for row in csv.reader(
                archive.read(
                    "soundaliases/iw_sound.csv"
                ).decode("utf-8-sig").splitlines()
            ):
                if row and row[0] in (
                    "bullet_small_flesh",
                    "bullet_large_flesh",
                ):
                    rows.setdefault(row[0], []).append(row)
            members = {name.casefold() for name in archive.namelist()}

        for alias in ("bullet_small_flesh", "bullet_large_flesh"):
            mp_rows = [
                row for row in rows[alias]
                if len(row) > 14 and row[14] == "all_mp"
            ]
            self.assertEqual(len(mp_rows), 8)
            for row in mp_rows:
                self.assertEqual(row[3:5], ["1.2", "1.5"])
                self.assertEqual(row[7:9], ["180", "1400"])
                self.assertIn(
                    ("sound/" + row[2]).casefold(),
                    members,
                )

    @unittest.skipUnless(
        (DATA_ROOT / "fod_content" / ".fod_staging" / "cod1_source"
         / "weapons" / "mp" / "smokegrenade_mp").is_file(),
        "local staged base/UO WEAPONFILEs unavailable",
    )
    def test_drop_ammo_bounds_match_every_retail_mp_weaponfile(self) -> None:
        weapon_files = dict(zip(
            (
                "bar", "bren", "colt45", "enfield", "fg42",
                "fraggrenade", "kar98k", "kar98k_scoped", "luger",
                "m1carbine", "m1garand", "mk1britishfrag",
                "mosin_nagant", "mosin_nagant_scoped", "mp40", "mp44",
                "panzerfaust", "ppsh", "rgd33", "springfield", "sten",
                "stielhandgranate", "thompson", "smokegrenade",
            ),
            WeaponMetadataTests.PRIMARY_WEAPON_FILES,
        ))
        # One case now: United Offensive is mandatory, and its archives
        # override the base MP WEAPONFILEs, so the staged tree is the only
        # source a shipping package can be built from.
        cases = (
            (
                DATA_ROOT / "fod_content" / ".fod_staging"
                / "cod1_source" / "weapons" / "mp",
                fod_package.MULTIPLAYER_DROP_AMMO_BOUNDS,
                fod_package.MULTIPLAYER_MAX_AMMO,
            ),
        )
        for source, expected_bounds, expected_max_ammo in cases:
            for weapon_id, expected in expected_bounds.items():
                values = parse_weapon_file(
                    source / weapon_files[weapon_id]
                )
                actual = (
                    int(values["dropAmmoMin"]),
                    int(values["dropAmmoMax"]),
                )
                self.assertEqual(
                    actual,
                    expected,
                    f"{source}: {weapon_id}",
                )
                self.assertEqual(
                    int(values["maxAmmo"]),
                    expected_max_ammo[weapon_id],
                    f"{source}: {weapon_id} maxAmmo",
                )

    def test_ordnance_staging_precedes_all_dependent_exports(self) -> None:
        config = pipeline.PipelineConfig(
            game_dir=DATA_ROOT / "Call of Duty",
            content_dir=DATA_ROOT / "fod_content" / "current",
        )
        steps = pipeline.build_steps(config)
        positions = {step.key: index for index, step in enumerate(steps)}
        for dependent in (
            "viewmodels",
            "worldmodels",
            "projectiles",
            "weapon_data",
        ):
            self.assertLess(positions["stage_ordnance"], positions[dependent])
            self.assertIn(
                "stage_ordnance",
                next(
                    step for step in steps if step.key == dependent
                ).depends_on,
            )

    @unittest.skipUnless(
        (DATA_ROOT / "Call of Duty" / "Main" / "pak5.pk3").is_file()
        and (DATA_ROOT / "Call of Duty" / "uo" / "pakuo06.pk3").is_file(),
        "local retail archives unavailable",
    )
    def test_default_map_inventory_is_every_selected_tier_mp_bsp(self) -> None:
        game_root = DATA_ROOT / "Call of Duty"
        base_archives = official_archives(game_root, "cod1")
        base_specs = map_importer.pak_map_specs(
            game_root,
            base_archives,
            requested=None,
            all_mp=False,
            has_uo=False,
        )
        self.assertEqual(
            {map_id for _game, _source, map_id in base_specs},
            set(fod_package.BASE_MAP_IDS),
        )
        all_specs = map_importer.pak_map_specs(
            game_root,
            base_archives,
            requested=None,
            all_mp=False,
            has_uo=True,
        )
        self.assertEqual(
            {
                (game, map_id)
                for game, _source, map_id in all_specs
            },
            {
                *(("cod1", map_id) for map_id in fod_package.BASE_MAP_IDS),
                *(("uo", map_id) for map_id in fod_package.UO_MAP_IDS),
            },
        )

    @unittest.skipUnless(
        (DATA_ROOT / "fod_content" / "current" / "weapons" / "weapons.json").is_file(),
        "local package unavailable",
    )
    def test_local_glb_animation_table_matches_existing_manifest(self) -> None:
        content = DATA_ROOT / "fod_content" / "current"
        weapons = json.loads(
            (content / "weapons" / "weapons.json").read_text()
        )["weapons"]
        bar = next(weapon for weapon in weapons if weapon["id"] == "bar")
        self.assertEqual(
            set(fod_package.glb_animation_names(content / bar["viewmodelGlb"])),
            set(bar["animations"]),
        )


if __name__ == "__main__":
    unittest.main()
