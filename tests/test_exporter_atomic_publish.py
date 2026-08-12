from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from exporter import friends_of_duty_exporter as exporter

from tools.cod1_archive_policy import ARCHIVES_BY_TIER


def write_complete_install(game_dir: Path) -> Path:
    """A Call of Duty + United Offensive tree the mandatory-UO gate accepts.

    These tests mock run_pipeline, so nothing reads the archives' contents —
    but run_export_pipeline now refuses to seed a working tree without a
    complete UO install, and that check is deliberately not mockable.
    """
    for tier, directory in (("cod1", "Main"), ("uo", "uo")):
        target = game_dir / directory
        target.mkdir(parents=True, exist_ok=True)
        for name in ARCHIVES_BY_TIER[tier]:
            (target / name).write_bytes(b"archive")
    return game_dir


class AtomicExporterPublishTests(unittest.TestCase):
    def config(self, content: Path):
        return exporter.fod_pipeline.PipelineConfig(
            game_dir=write_complete_install(content.parent / "game"),
            content_dir=content,
        )

    def test_success_replaces_package_only_after_pipeline_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current"
            current.mkdir()
            (current / "fodpak.json").write_text(
                '{"generation":"old"}',
                encoding="utf-8",
            )
            (current / "old.txt").write_text("playable", encoding="utf-8")

            def build(cfg, **_kwargs):
                self.assertNotEqual(cfg.content_dir, current)
                # The old playable package remains untouched while work runs.
                self.assertEqual(
                    (current / "fodpak.json").read_text(encoding="utf-8"),
                    '{"generation":"old"}',
                )
                (cfg.content_dir / "fodpak.json").write_text(
                    '{"generation":"new"}',
                    encoding="utf-8",
                )
                (cfg.content_dir / "new.txt").write_text(
                    "validated",
                    encoding="utf-8",
                )

            with mock.patch.object(
                exporter.fod_pipeline,
                "run_pipeline",
                side_effect=build,
            ):
                promoted = exporter.run_export_pipeline(
                    self.config(current),
                    log=lambda _line: None,
                )

            self.assertTrue(promoted)
            self.assertEqual(
                (current / "fodpak.json").read_text(encoding="utf-8"),
                '{"generation":"new"}',
            )
            self.assertTrue((current / "new.txt").is_file())
            self.assertFalse(
                exporter.export_working_directory(current).exists()
            )
            self.assertFalse(list(root.glob(".current.previous-*")))

    def test_failed_pipeline_preserves_playable_package_and_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current"
            current.mkdir()
            (current / "fodpak.json").write_text(
                "stable",
                encoding="utf-8",
            )

            def fail(cfg, **_kwargs):
                (cfg.content_dir / "partial.txt").write_text(
                    "resume me",
                    encoding="utf-8",
                )
                raise exporter.fod_pipeline.PipelineError(
                    "conversion stopped"
                )

            with mock.patch.object(
                exporter.fod_pipeline,
                "run_pipeline",
                side_effect=fail,
            ):
                with self.assertRaisesRegex(
                    exporter.fod_pipeline.PipelineError,
                    "conversion stopped",
                ):
                    exporter.run_export_pipeline(
                        self.config(current),
                        log=lambda _line: None,
                    )

            self.assertEqual(
                (current / "fodpak.json").read_text(encoding="utf-8"),
                "stable",
            )
            self.assertTrue(
                (
                    exporter.export_working_directory(current)
                    / "partial.txt"
                ).is_file()
            )

    def test_promotion_refuses_work_without_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current"
            work = root / ".current.exporting"
            current.mkdir()
            work.mkdir()
            (current / "fodpak.json").write_text(
                "stable",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                exporter.fod_pipeline.PipelineError,
                "no fodpak.json",
            ):
                exporter.promote_working_directory(work, current)
            self.assertEqual(
                (current / "fodpak.json").read_text(encoding="utf-8"),
                "stable",
            )


class ExporterImporterRequirementTests(unittest.TestCase):
    def test_status_requires_authored_lod_api(self):
        with mock.patch.object(
            exporter.fod_build_importer,
            "check",
            return_value=(False, "LOD0-only"),
        ) as check:
            self.assertEqual(
                exporter.importer_addon_status(),
                (False, "LOD0-only"),
            )
        check.assert_called_once_with(require_lod=True)

    def test_cli_stops_when_the_importer_is_not_lod_capable(self):
        """The check now runs INSIDE Blender.

        It used to shell out with `sys.executable -c`, which under
        PyInstaller hands the snippet to the exporter's own argparse
        ("unrecognized arguments: -c ..."), and it would otherwise have to
        import the GPL extension into this process -- the boundary
        Docs/EXPORTER_SINGLE_EXECUTABLE.md section 9.1 rests on. A shipped
        payload has nothing to prepare, only something to verify.
        """
        args = Namespace(
            blender=None,
            game_dir=None,
            output=Path("unused"),
            force=False,
            all_mp=False,
            maps=[],
            only=None,
            zip=None,
        )
        with (
            mock.patch.object(
                exporter, "python_ok", return_value=(True, "Python ready")),
            mock.patch.object(
                exporter, "module_ok", return_value=(True, "module ready")),
            mock.patch.object(
                exporter, "find_blender",
                return_value=(Path("/Blender"), "Blender ready")),
            mock.patch.object(
                exporter.fod_blender, "importer_status",
                return_value=(False, "LOD0-only")) as status,
        ):
            self.assertEqual(exporter.run_cli(args), 1)
        status.assert_called_once()
        self.assertEqual(status.call_args.args[0], Path("/Blender"),
                         "the probe must run under the resolved Blender")

    def test_the_cli_never_builds_the_importer_itself(self):
        """A player's exporter must never try to compile anything.

        An automatic source build is what made a Rust toolchain a de-facto
        prerequisite; the shipped payload arrives with the extension in place.
        """
        source = (Path(exporter.__file__)).read_text(encoding="utf-8")
        run_cli = source[source.index("def run_cli("):source.index("def run_gui(")]
        self.assertNotIn("fod_build_importer", run_cli)


if __name__ == "__main__":
    unittest.main()
