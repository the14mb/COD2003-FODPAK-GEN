"""The pieces that only behave differently inside a frozen build.

The existing suite calls build_steps() and takes the sys.executable path
unconditionally, so none of it covers the bundled branch. These tests do,
without needing an actual PyInstaller build.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORTER_DIR = REPO_ROOT / "exporter"
sys.path.insert(0, str(EXPORTER_DIR))

import cod_autodetect  # noqa: E402
import fod_paths  # noqa: E402
import pipeline  # noqa: E402
import progress as fod_progress  # noqa: E402
import toolchain  # noqa: E402


def _config() -> pipeline.PipelineConfig:
    return pipeline.PipelineConfig(
        game_dir=Path("/game"),
        content_dir=Path("/out/current"),
        blender=Path("/blender"),
    )


class HostInvocationTests(unittest.TestCase):
    def test_a_checkout_spawns_the_interpreter(self):
        argv = pipeline._python(_config(), "extract_cod1_assets.py", "a", "b")
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("extract_cod1_assets.py"))
        self.assertEqual(argv[2:], ["a", "b"])

    def test_a_bundle_re_execs_itself(self):
        """There is no separate python in a frozen build to spawn."""
        with mock.patch.object(fod_paths, "is_bundled", return_value=True), \
                mock.patch.object(
                    fod_paths, "self_exe",
                    return_value="/Apps/FriendsOfDutyExporter"):
            argv = pipeline._python(
                _config(), "extract_cod1_assets.py", "a", "b")
        self.assertEqual(argv[0], "/Apps/FriendsOfDutyExporter")
        self.assertEqual(argv[1], "--fod-run-tool")
        self.assertTrue(argv[2].endswith("extract_cod1_assets.py"))
        self.assertTrue(Path(argv[2]).is_absolute(), "tool path must be absolute")
        self.assertEqual(argv[3:], ["a", "b"])

    def test_the_package_step_uses_the_same_mechanism(self):
        """package.py is spawned too, and was a literal sys.executable list."""
        with mock.patch.object(fod_paths, "is_bundled", return_value=True), \
                mock.patch.object(
                    fod_paths, "self_exe", return_value="/Apps/Exporter"):
            steps = {s.key: s for s in pipeline.build_steps(_config())}
            argv = steps["package"].build_argv(_config())
        self.assertEqual(argv[0], "/Apps/Exporter")
        self.assertEqual(argv[1], "--fod-run-tool")
        self.assertTrue(argv[2].endswith("package.py"))


class LauncherDispatchTests(unittest.TestCase):
    """Run the real dispatch in a real subprocess.

    Mocking would prove nothing here: the whole point is that a re-exec'd
    process ends up indistinguishable from `python <tool> args...`, and only
    an actual process can demonstrate that.
    """

    FIXTURE = textwrap.dedent(
        """
        import json, os, sys
        import sibling
        print(json.dumps({
            "argv0_name": os.path.basename(sys.argv[0]),
            "argv0_absolute": os.path.isabs(sys.argv[0]),
            "args": sys.argv[1:],
            "name": __name__,
            "syspath0_name": os.path.basename(sys.path[0]),
            "sibling": sibling.VALUE,
        }))
        """
    )

    def _run(self, *arguments: str) -> dict:
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            (directory / "fake_tool.py").write_text(
                self.FIXTURE, encoding="utf-8")
            (directory / "sibling.py").write_text(
                "VALUE = 'bare-name-import'", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(EXPORTER_DIR / "fod_launcher.py"),
                 "--fod-run-tool", str(directory / "fake_tool.py"),
                 *arguments],
                capture_output=True, text=True, check=True)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_the_tool_sees_a_direct_invocation(self):
        report = self._run("--alpha", "beta")
        self.assertEqual(report["argv0_name"], "fake_tool.py")
        self.assertTrue(report["argv0_absolute"])
        self.assertEqual(report["args"], ["--alpha", "beta"])

    def test_module_scope_code_runs_as_main(self):
        """28 of the 50 tools have no main() and work at import time."""
        self.assertEqual(self._run()["name"], "__main__")

    def test_sibling_modules_import_by_bare_name(self):
        """tools/ modules import each other unqualified.

        That only resolves because a direct invocation puts the script's own
        directory at sys.path[0]; the dispatch has to reproduce it.
        """
        report = self._run()
        self.assertEqual(report["sibling"], "bare-name-import")
        self.assertEqual(report["syspath0_name"], Path(report["syspath0_name"]).name)

    def test_a_missing_tool_fails_loudly(self):
        result = subprocess.run(
            [sys.executable, str(EXPORTER_DIR / "fod_launcher.py"),
             "--fod-run-tool", "/nonexistent/tool.py"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no such tool", result.stderr)


class ProgressWeightingTests(unittest.TestCase):
    KEYS = ["extract_cod1", "viewmodels", "maps", "props", "package"]

    def setUp(self):
        self._protocol = mock.patch.dict(
            os.environ, {fod_paths.PROGRESS_ENV: "0"})
        self._protocol.start()
        self.addCleanup(self._protocol.stop)

    def test_the_fraction_starts_at_zero_and_ends_at_one(self):
        reporter = fod_progress.ExportProgress(self.KEYS)
        self.assertEqual(reporter.fraction(), 0.0)
        for index, key in enumerate(self.KEYS):
            reporter.begin(index, len(self.KEYS), key, key)
            reporter.end(key, "done", 1.0)
        self.assertAlmostEqual(reporter.fraction(), 1.0, places=6)

    def test_the_fraction_never_goes_backwards(self):
        reporter = fod_progress.ExportProgress(self.KEYS)
        seen = 0.0
        for index, key in enumerate(self.KEYS):
            reporter.begin(index, len(self.KEYS), key, key)
            for done in (1, 5, 10):
                reporter.item(key, done, 10, "x")
                self.assertGreaterEqual(reporter.fraction(), seen)
                seen = reporter.fraction()
            reporter.end(key, "done", 1.0)
            self.assertGreaterEqual(reporter.fraction(), seen)
            seen = reporter.fraction()

    def test_expensive_steps_are_weighted_above_cheap_ones(self):
        """A per-step tick would sit still through maps and props."""
        table = fod_progress.weights()
        self.assertGreater(table["maps"], table["hud"])
        self.assertGreater(table["players"], table["shellcasing"])

    def test_a_resumed_run_credits_skipped_steps_immediately(self):
        reporter = fod_progress.ExportProgress(self.KEYS)
        for index, key in enumerate(self.KEYS[:-1]):
            reporter.skipped(index, len(self.KEYS), key, key)
        self.assertGreater(
            reporter.fraction(), 0.5,
            "a rerun that skips almost everything must not crawl from zero")

    def test_a_stray_item_for_another_step_does_not_move_the_bar(self):
        reporter = fod_progress.ExportProgress(self.KEYS)
        reporter.begin(0, 5, "extract_cod1", "x")
        before = reporter.fraction()
        reporter.item("maps", 9, 10, "late line from a finished child")
        self.assertEqual(reporter.fraction(), before)

    def test_an_unknown_step_key_still_produces_a_usable_fraction(self):
        reporter = fod_progress.ExportProgress(["brand_new_step"])
        reporter.begin(0, 1, "brand_new_step", "x")
        reporter.end("brand_new_step", "done", 1.0)
        self.assertAlmostEqual(reporter.fraction(), 1.0, places=6)


class ItemForwardingTests(unittest.TestCase):
    def test_a_child_item_line_is_lifted_out_of_stdout(self):
        seen = []
        pipeline._forward_item(
            "@fod v1 item step=props done=112 total=239 label=xmodel/wall",
            lambda done, total, label: seen.append((done, total, label)))
        self.assertEqual(seen, [(112, 239, "xmodel/wall")])

    def test_ordinary_output_is_ignored(self):
        seen = []
        for line in ("[3/19] Export map props", "", "@fod v1 begin step=props"):
            pipeline._forward_item(line, lambda *a: seen.append(a))
        self.assertEqual(seen, [])

    def test_a_malformed_item_never_raises(self):
        """Losing a progress tick must never lose an export."""
        for line in ("@fod v1 item ", "@fod v1 item done=x total=y",
                     "@fod v1 item done=1", "@fod v1 item total=2"):
            with self.subTest(line=line):
                pipeline._forward_item(
                    line, lambda *a: self.fail("should not have parsed"))


class ToolchainSignatureTests(unittest.TestCase):
    def test_blender_steps_declare_blender(self):
        for key in ("viewmodels", "players", "props"):
            with self.subTest(step=key):
                self.assertIn("blenderVersion", toolchain.toolchain_for(key))

    def test_host_steps_declare_the_interpreter_stack_instead(self):
        facts = toolchain.toolchain_for("maps")
        self.assertIn("numpyVersion", facts)
        self.assertIn("pillowVersion", facts)
        self.assertNotIn(
            "blenderVersion", facts,
            "a Blender bump must not invalidate the 391 MB maps step")

    def test_every_importer_step_hashes_the_python_wrapper(self):
        """A pure-Python importer change must invalidate every step using it.

        This is the gap that silently kept broken output. A recursion bug in
        cod_asset_importer/importer.py made every material import fail; the
        native .pyd was untouched, so the markers for worldmodels,
        shellcasing and projectiles stayed valid, the resume logic skipped
        all three, and 73 texture PNGs' worth of broken output survived into
        the package. Only `props` listed importer.py in signature_files.
        """
        for key in sorted(toolchain.IMPORTER_STEPS):
            with self.subTest(step=key):
                facts = toolchain.toolchain_for(key)
                self.assertIn("importerPythonSha256", facts)
                self.assertNotEqual(facts["importerPythonSha256"], "missing")

    def test_a_wrapper_change_invalidates_importer_steps_only(self):
        cfg = _config()
        steps = {s.key: s for s in pipeline.build_steps(cfg)}
        watched = ("worldmodels", "shellcasing", "projectiles", "players")
        before = {k: self._signature(cfg, steps[k]) for k in watched + ("maps",)}
        with mock.patch.object(
                toolchain, "importer_python_sha256", return_value="changed"):
            after = {k: self._signature(cfg, steps[k]) for k in watched + ("maps",)}
        for key in watched:
            self.assertNotEqual(
                before[key], after[key],
                f"{key} must re-run when the importer wrapper changes")
        self.assertEqual(
            before["maps"], after["maps"],
            "the importer is irrelevant to maps; it must not re-run")

    def test_the_build_id_is_never_hashed(self):
        """Hashing it would make every build a full re-export."""
        for key in ("viewmodels", "maps", "package"):
            serialised = json.dumps(toolchain.toolchain_for(key))
            self.assertNotIn("exporterBuild", serialised)
            self.assertNotIn("payloadSha", serialised)

    # _step_signature fingerprints the player's pk3 archives, which a test
    # machine has no business owning. Only the toolchain half is under test
    # here, so the archive half is stubbed.
    def _signature(self, cfg, step) -> dict:
        with mock.patch.object(
                pipeline, "policy_fingerprint", return_value="stub"):
            return json.loads(pipeline._step_signature(cfg, step))

    def test_the_signature_carries_the_toolchain_and_the_new_schema(self):
        self.assertEqual(pipeline.PIPELINE_SCHEMA_VERSION, 4)
        steps = {s.key: s for s in pipeline.build_steps(_config())}
        payload = self._signature(_config(), steps["viewmodels"])
        self.assertEqual(payload["pipelineSchema"], 4)
        self.assertIn("blenderVersion", payload["toolchain"])

    def test_a_blender_bump_invalidates_only_the_blender_steps(self):
        """The gap this closes: today a Blender bump invalidates nothing."""
        cfg = _config()
        steps = {s.key: s for s in pipeline.build_steps(cfg)}
        before = {key: self._signature(cfg, steps[key])
                  for key in ("viewmodels", "maps")}
        with mock.patch.object(
                toolchain, "blender_version", return_value="9.9.9"):
            after = {key: self._signature(cfg, steps[key])
                     for key in ("viewmodels", "maps")}
        self.assertNotEqual(before["viewmodels"], after["viewmodels"])
        self.assertEqual(
            before["maps"], after["maps"],
            "a Blender bump must not re-run the 391 MB maps step")


class AutodetectTests(unittest.TestCase):
    def test_a_directory_without_main_is_not_an_install(self):
        with tempfile.TemporaryDirectory() as scratch:
            self.assertFalse(cod_autodetect.looks_like_install(Path(scratch)))

    def test_an_empty_main_directory_is_not_an_install(self):
        """A leftover empty folder must not shadow the real install."""
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "Main").mkdir()
            self.assertFalse(cod_autodetect.looks_like_install(Path(scratch)))

    def test_main_with_a_pk3_is_an_install(self):
        with tempfile.TemporaryDirectory() as scratch:
            main = Path(scratch) / "Main"
            main.mkdir()
            (main / "pak0.pk3").write_bytes(b"PK\x03\x04")
            self.assertTrue(cod_autodetect.looks_like_install(Path(scratch)))

    def test_candidates_are_absolute_and_unique(self):
        found = cod_autodetect.candidates()
        self.assertTrue(all(path.is_absolute() for path in found))
        self.assertEqual(len(found), len(set(found)))

    def test_the_environment_override_is_preferred(self):
        with tempfile.TemporaryDirectory() as scratch:
            with mock.patch.dict(
                    os.environ, {"FOD_COD_GAME_DIR": scratch}):
                self.assertEqual(
                    cod_autodetect.candidates()[0], Path(scratch).resolve())


if __name__ == "__main__":
    unittest.main()
