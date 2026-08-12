"""Unit tests for the runtime Blender provisioner and the path helpers.

Nothing here touches the network. The one thing that must never regress
silently is the *refusal* behaviour: a wrong version, a missing glTF hook or
a bad checksum has to stop the export, because each of them produces content
that differs from what this build was tested against.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "exporter"))

import blender_provisioner as provisioner  # noqa: E402
import fod_paths  # noqa: E402


class PinTests(unittest.TestCase):
    def test_pin_covers_every_shipping_target(self):
        targets = provisioner.pin()["targets"]
        self.assertEqual(
            set(targets),
            {"windows-x64", "macos-arm64", "macos-x64", "linux-x64"},
        )

    def test_every_target_is_complete_and_plausible(self):
        for name, entry in provisioner.pin()["targets"].items():
            with self.subTest(target=name):
                for key in ("archive", "url", "sha256", "archive_bytes",
                            "form", "inner_dir", "executable"):
                    self.assertIn(key, entry)
                self.assertEqual(len(entry["sha256"]), 64, "sha256 length")
                int(entry["sha256"], 16)  # raises unless it is real hex
                self.assertTrue(entry["url"].startswith("https://"))
                self.assertIn(entry["archive"], entry["url"])
                # Every official build is comfortably over 100 MB; a value
                # that small would mean the pin was written by hand.
                self.assertGreater(entry["archive_bytes"], 100_000_000)
                self.assertIn(entry["form"], {"zip", "tar.xz", "dmg"})

    def test_version_and_label_agree(self):
        self.assertEqual(
            provisioner.pinned_version_label(),
            ".".join(str(part) for part in provisioner.pinned_version()),
        )

    def test_pin_is_not_read_from_a_loose_file_when_frozen(self):
        """A shipped build takes the pin from build_stamp, not from disk.

        An editable pin beside the executable would let anything that can
        write there choose both the archive and the hash it is checked
        against, which defeats the verification entirely.
        """
        source = (REPO_ROOT / "exporter" / "blender_provisioner.py").read_text(
            encoding="utf-8")
        self.assertIn("import build_stamp", source)


class HostTargetTests(unittest.TestCase):
    def test_each_platform_maps_to_a_pinned_target(self):
        cases = [
            ("win32", "x86_64", "windows-x64"),
            ("darwin", "arm64", "macos-arm64"),
            ("darwin", "x86_64", "macos-x64"),
            ("linux", "x86_64", "linux-x64"),
        ]
        for platform, machine, expected in cases:
            with self.subTest(platform=platform, machine=machine):
                with mock.patch.object(provisioner.sys, "platform", platform), \
                        mock.patch.object(
                            provisioner.os, "uname",
                            create=True,
                            return_value=mock.Mock(machine=machine)):
                    self.assertEqual(provisioner.host_target(), expected)

    def test_linux_arm_is_refused_with_a_reason(self):
        """Blender publishes no Linux ARM build, so there is nothing to fetch.

        Saying so here is the whole point: the alternative is failing several
        steps into an export with a missing-executable error.
        """
        with mock.patch.object(provisioner.sys, "platform", "linux"), \
                mock.patch.object(
                    provisioner.os, "uname", create=True,
                    return_value=mock.Mock(machine="aarch64")):
            with self.assertRaises(provisioner.ProvisionError) as caught:
                provisioner.host_target()
        self.assertIn("no Linux ARM build", str(caught.exception))
        self.assertIn("remote-import", str(caught.exception))


class ProbeVerdictTests(unittest.TestCase):
    GOOD = {
        "version": [4, 5, 1],
        "gltf_op": True,
        "hooks": {"dedup": True, "animation": True, "sampling_range": True},
    }

    def test_the_pinned_build_is_accepted(self):
        provisioner._require_usable(Path("/blender"), dict(self.GOOD))

    def test_a_different_version_is_refused(self):
        report = dict(self.GOOD, version=[4, 2, 0])
        with self.assertRaises(provisioner.ProvisionError) as caught:
            provisioner._require_usable(Path("/blender"), report)
        self.assertIn("4.2.0", str(caught.exception))
        self.assertIn(provisioner.pinned_version_label(), str(caught.exception))

    def test_a_newer_version_is_refused_too(self):
        """5.x is the realistic accident: blender.org serves it by default."""
        report = dict(self.GOOD, version=[5, 2, 0])
        with self.assertRaises(provisioner.ProvisionError):
            provisioner._require_usable(Path("/blender"), report)

    def test_a_missing_gltf_exporter_is_refused(self):
        report = dict(self.GOOD, gltf_op=False)
        with self.assertRaises(provisioner.ProvisionError) as caught:
            provisioner._require_usable(Path("/blender"), report)
        self.assertIn("glTF", str(caught.exception))

    def test_each_missing_private_hook_is_named(self):
        for hook in ("dedup", "animation", "sampling_range"):
            with self.subTest(hook=hook):
                hooks = dict(self.GOOD["hooks"])
                hooks[hook] = False
                report = dict(self.GOOD, hooks=hooks)
                with self.assertRaises(provisioner.ProvisionError) as caught:
                    provisioner._require_usable(Path("/blender"), report)
                self.assertIn(hook, str(caught.exception))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = mock.patch.dict(
            os.environ,
            {fod_paths.BLENDER_CACHE_ENV: str(
                Path(self.enterContext_tempdir()))},
        )
        self._tmp.start()
        self.addCleanup(self._tmp.stop)

    def enterContext_tempdir(self) -> str:
        import tempfile

        directory = tempfile.mkdtemp(prefix="fod-provisioner-test-")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def test_no_stamp_means_no_cache_hit(self):
        self.assertIsNone(provisioner.cached_blender(provisioner.target_pin()))

    def test_a_stamp_from_a_different_archive_is_not_a_hit(self):
        """A pin bump must not silently reuse the previous download."""
        entry = provisioner.target_pin()
        root = provisioner.install_root()
        executable = provisioner.executable_in(root, entry)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("not really blender", encoding="utf-8")
        provisioner._stamp_path(root).write_text(
            json.dumps({"archive_sha256": "0" * 64}), encoding="utf-8")
        self.assertIsNone(provisioner.cached_blender(entry))

    def test_a_matching_stamp_with_no_executable_is_not_a_hit(self):
        entry = provisioner.target_pin()
        root = provisioner.install_root()
        root.mkdir(parents=True, exist_ok=True)
        provisioner._stamp_path(root).write_text(
            json.dumps({"archive_sha256": entry["sha256"]}), encoding="utf-8")
        self.assertIsNone(provisioner.cached_blender(entry))

    def test_a_matching_stamp_with_an_executable_is_a_hit(self):
        entry = provisioner.target_pin()
        root = provisioner.install_root()
        executable = provisioner.executable_in(root, entry)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("blender", encoding="utf-8")
        provisioner._stamp_path(root).write_text(
            json.dumps({"archive_sha256": entry["sha256"]}), encoding="utf-8")
        self.assertEqual(provisioner.cached_blender(entry), executable)

    def test_the_cache_is_version_keyed(self):
        """A pin bump downloads beside the old tree, never over it."""
        self.assertEqual(
            provisioner.install_root().name,
            provisioner.pinned_version_label(),
        )

    def test_refusing_to_download_still_says_how_to_proceed(self):
        with self.assertRaises(provisioner.ProvisionError) as caught:
            provisioner.resolve(allow_download=False, log=lambda _m: None)
        message = str(caught.exception)
        entry = provisioner.target_pin()
        self.assertIn(entry["url"], message)
        self.assertIn(entry["sha256"], message)
        self.assertIn("--blender", message)


class OfflineInstructionTests(unittest.TestCase):
    def test_instructions_name_url_hash_and_destination(self):
        entry = provisioner.target_pin()
        text = provisioner.offline_instructions(entry)
        self.assertIn(entry["url"], text)
        self.assertIn(entry["sha256"], text)
        self.assertIn(entry["archive"], text)


class ChildEnvironmentTests(unittest.TestCase):
    def test_blender_never_inherits_the_frozen_loader_paths(self):
        """Restoring these would hand Blender the game's own libraries.

        On Linux the game launcher prepends the game directory and Steam's
        runtime to LD_LIBRARY_PATH before the exporter starts, so anything
        derived from our environment is already poisoned.
        """
        polluted = {
            "LD_LIBRARY_PATH": "/game:/steam/scout",
            "DYLD_LIBRARY_PATH": "/game",
            "_MEIPASS": "/tmp/_MEI123",
            "PYTHONHOME": "/frozen",
            "BLENDER_USER_SCRIPTS": "/home/player/addons",
        }
        with mock.patch.dict(os.environ, polluted):
            env = fod_paths.child_env("blender")
        for name in polluted:
            self.assertNotIn(name, env, name)

    def test_blender_is_told_not_to_write_bytecode(self):
        """Blender writes .pyc into its own install and breaks its seal."""
        env = fod_paths.child_env("blender")
        self.assertEqual(env.get("PYTHONDONTWRITEBYTECODE"), "1")

    def test_host_children_keep_the_frozen_loader_paths(self):
        with mock.patch.dict(os.environ, {"_MEIPASS": "/tmp/_MEI123"}):
            env = fod_paths.child_env("host")
        self.assertEqual(env.get("_MEIPASS"), "/tmp/_MEI123")

    def test_both_kinds_pin_hash_seed_and_encoding(self):
        for kind in ("host", "blender"):
            with self.subTest(kind=kind):
                env = fod_paths.child_env(kind)
                self.assertEqual(env.get("PYTHONHASHSEED"), "0")
                self.assertEqual(env.get("PYTHONUTF8"), "1")

    def test_essential_variables_survive(self):
        """Starting from an empty environment breaks subprocesses outright."""
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            env = fod_paths.child_env("blender")
        self.assertEqual(env.get("PATH"), "/usr/bin")


class ProgressProtocolTests(unittest.TestCase):
    def test_prepare_lines_are_silent_without_the_protocol_flag(self):
        """A human's console must look exactly as it did before."""
        import friends_of_duty_exporter as exporter
        import io
        import contextlib

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(fod_paths.PROGRESS_ENV, None)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exporter.emit_prepare("download", 1, 2, "x.zip")
            self.assertEqual(buffer.getvalue(), "")

    def test_prepare_lines_are_emitted_with_the_protocol_flag(self):
        import friends_of_duty_exporter as exporter
        import io
        import contextlib

        with mock.patch.dict(os.environ, {fod_paths.PROGRESS_ENV: "1"}):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exporter.emit_prepare("download", 1024, 2048, "x.zip")
            self.assertEqual(
                buffer.getvalue().strip(),
                "@fod v1 prepare phase=download done=1024 total=2048 "
                "label=x.zip",
            )


class PipelineInvocationTests(unittest.TestCase):
    def test_blender_steps_run_with_factory_startup(self):
        """The export must not load the player's add-ons or preferences."""
        import pipeline

        cfg = pipeline.PipelineConfig(
            game_dir=Path("/game"),
            content_dir=Path("/out/current"),
            blender=Path("/blender"),
        )
        argv = pipeline._blender(cfg, "export_cod1_demo_viewmodels.py", "a")
        self.assertIn("--factory-startup", argv)
        self.assertLess(argv.index("--background"), argv.index("--python"))
        self.assertEqual(argv[-1], "a")


if __name__ == "__main__":
    unittest.main()
