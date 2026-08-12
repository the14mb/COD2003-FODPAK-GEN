"""United Offensive is mandatory, and the seven-map roster is not selectable.

Friends of Duty requires Call of Duty *and* United Offensive. UO is not an
optional map pack: it supplies the smoke grenade (the 24th weapon, and a real
multiplayer loadout entry), 95 additional player animations, the jeep turret
rig, and two of the seven roster maps. A package built without it fails
validation and is refused at mount, so the exporter must never start one.

These tests pin the three properties that make that true in practice:
the gate fires before anything is written, it explains what to install, and
neither the tier nor the map list can be chosen by the caller.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))

from cod1_archive_policy import (  # noqa: E402
    ARCHIVES_BY_TIER,
    COD1_TIER,
    POLICY_FORMAT,
    POLICY_VERSION,
    UO_TIER,
    has_complete_uo,
    uo_installation_status,
    validate_policy_manifest,
)

from exporter import friends_of_duty_exporter as exporter  # noqa: E402

# The exporter loads pipeline.py through its own importlib spec (so the
# shipped payload stays self-contained), which makes `exporter.fod_pipeline` a
# DIFFERENT module object from `from exporter import pipeline` — and therefore
# a different PipelineError class. Always assert against the instance the
# exporter actually raises.
fod_pipeline = exporter.fod_pipeline  # noqa: E402


def write_install(game_dir: Path, uo: str) -> Path:
    """Build a fake retail tree. uo is 'complete', 'absent' or 'partial'."""
    main = game_dir / "Main"
    main.mkdir(parents=True, exist_ok=True)
    for name in ARCHIVES_BY_TIER["cod1"]:
        (main / name).write_bytes(b"archive")
    if uo == "absent":
        return game_dir
    uo_dir = game_dir / "uo"
    uo_dir.mkdir(exist_ok=True)
    names = ARCHIVES_BY_TIER["uo"]
    if uo == "partial":
        names = names[:3]
    for name in names:
        (uo_dir / name).write_bytes(b"archive")
    return game_dir


class UoInstallationStatusTests(unittest.TestCase):
    def test_absent_uo_is_reported_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = write_install(Path(temporary) / "game", "absent")
            ok, message = uo_installation_status(root)
            self.assertFalse(ok)
            self.assertIn("United Offensive", message)
            self.assertIn("No files were written", message)
            self.assertFalse(has_complete_uo(root))

    def test_partial_uo_names_the_missing_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = write_install(Path(temporary) / "game", "partial")
            ok, message = uo_installation_status(root)
            self.assertFalse(ok)
            self.assertIn("incomplete", message)
            self.assertIn("pakuo06.pk3", message)
            # A partial install must not escape as OfficialArchiveError from
            # the plain predicate — several callers use it inside a boolean.
            self.assertFalse(has_complete_uo(root))

    def test_complete_uo_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = write_install(Path(temporary) / "game", "complete")
            ok, message = uo_installation_status(root)
            self.assertTrue(ok)
            self.assertEqual(message, "")
            self.assertTrue(has_complete_uo(root))

    def test_tier_directory_is_case_insensitive(self) -> None:
        # Steam/Proton trees ship 'UO/' beside 'Main/'; on a case-sensitive
        # filesystem an exact-case lookup would lock those installs out now
        # that a missing UO aborts the export.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "game"
            write_install(root, "complete")
            (root / "uo").rename(root / "UO")
            ok, message = uo_installation_status(root)
            self.assertTrue(ok, message)


class SourcePolicyTests(unittest.TestCase):
    """provenance/source_archives.json must describe both tiers."""

    def _manifest(self) -> dict:
        return {
            "format": POLICY_FORMAT,
            "version": POLICY_VERSION,
            "tiers": [COD1_TIER, UO_TIER],
            "archives": [
                {
                    "tier": tier,
                    "path": f"{directory}/{name}",
                    "bytes": 1,
                    "sha256": "0" * 64,
                }
                for tier, directory in (("cod1", "Main"), ("uo", "uo"))
                for name in ARCHIVES_BY_TIER[tier]
            ],
        }

    def test_full_tier_manifest_validates(self) -> None:
        self.assertEqual(validate_policy_manifest(self._manifest()), [])

    def test_base_only_tiers_are_rejected(self) -> None:
        payload = self._manifest()
        payload["tiers"] = [COD1_TIER]
        errors = validate_policy_manifest(payload)
        self.assertTrue(
            any("United Offensive" in error for error in errors),
            errors,
        )

    def test_missing_uo_archives_are_rejected(self) -> None:
        payload = self._manifest()
        payload["archives"] = [
            record for record in payload["archives"] if record["tier"] == "cod1"
        ]
        errors = validate_policy_manifest(payload)
        self.assertTrue(
            any("roster is incomplete" in error for error in errors),
            errors,
        )

    def test_pre_migration_policy_version_is_rejected(self) -> None:
        # Every version-3 manifest describes a selectable tier that no longer
        # exists, so it must be refused rather than re-interpreted.
        payload = self._manifest()
        payload["version"] = 3
        errors = validate_policy_manifest(payload)
        self.assertTrue(
            any("version" in error for error in errors),
            errors,
        )


class PipelineGateTests(unittest.TestCase):
    def test_run_pipeline_refuses_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = write_install(root / "game", "absent")
            content = root / "content" / "current"
            cfg = fod_pipeline.PipelineConfig(
                game_dir=game,
                content_dir=content,
            )
            with self.assertRaises(fod_pipeline.PipelineError) as caught:
                fod_pipeline.run_pipeline(cfg, log=lambda _line: None)
            self.assertIn("United Offensive", str(caught.exception))
            # The gate is the first statement in run_pipeline, ahead of both
            # mkdir calls: a player without UO is left with nothing to clean up.
            self.assertFalse(content.exists())
            self.assertFalse(cfg.notes_dir.exists())

    def test_run_export_pipeline_leaves_no_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = write_install(root / "game", "absent")
            content = root / "content" / "current"
            content.mkdir(parents=True)
            cfg = fod_pipeline.PipelineConfig(
                game_dir=game,
                content_dir=content,
            )
            with self.assertRaises(fod_pipeline.PipelineError):
                exporter.run_export_pipeline(cfg, log=lambda _line: None)
            leftovers = [
                path.name
                for path in content.parent.iterdir()
                if path.name != "current"
            ]
            self.assertEqual(leftovers, [], f"stranded working tree: {leftovers}")


class SelectionFlagTests(unittest.TestCase):
    """The tier and the roster are not the caller's to choose."""

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = write_install(root / "game", "complete")
            return subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "exporter" / "friends_of_duty_exporter.py"),
                    "--cli",
                    "--game-dir",
                    str(game),
                    "--output",
                    str(root / "content" / "current"),
                    *extra,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

    def test_maps_subset_is_rejected(self) -> None:
        result = self._run("--maps", "mp_pavlov")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no longer supported", result.stdout + result.stderr)

    def test_legacy_tier_flags_are_accepted_and_ignored(self) -> None:
        # An already-installed game spawns this exporter and still passes
        # --include-uo (ExporterLauncher.cs). Erroring on it would break
        # updating from any older build, so it must stay a silent no-op.
        for flag in ("--include-uo", "--all-mp"):
            with self.subTest(flag=flag):
                result = self._run(flag)
                combined = result.stdout + result.stderr
                self.assertNotIn("no longer supported", combined)
                self.assertNotIn("unrecognized arguments", combined)

    def test_pipeline_config_carries_no_selection_fields(self) -> None:
        # There is nothing left to select: no tier flag, no map list.
        cfg = fod_pipeline.PipelineConfig(
            game_dir=Path("unused"),
            content_dir=Path("unused"),
        )
        for retired in ("include_uo", "all_mp_maps", "maps", "has_uo"):
            self.assertFalse(
                hasattr(cfg, retired),
                f"PipelineConfig still exposes {retired}",
            )


if __name__ == "__main__":
    unittest.main()
