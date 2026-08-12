from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "exporter"))

import build_importer


class BuildImporterTests(unittest.TestCase):
    def test_the_ci_cache_covers_every_shipping_desktop_host(self):
        """Every shipping target must have a correct artifact cached.

        Deliberately checks the artifact's ARCHITECTURE rather than its LOD
        API for foreign targets: artifact_lod_api_version() works by dlopen-ing
        the module, which can only ever succeed for the host's own
        architecture. That check belongs where it can actually run -- the CI
        matrix asserts XMODEL_LOD_API_VERSION == 1 on each native runner
        before publishing. Here we prove the fetch put the right binary in the
        right place; test_the_host_artifact_is_lod_capable covers the rest.
        """
        cases = (
            ("windows-x64", ("Windows", "x86_64")),
            ("linux-x64", ("Linux", "x86_64")),
            ("macos-arm64", ("Darwin", "arm64")),
            ("macos-x64", ("Darwin", "x86_64")),
        )
        for target, expected_platform in cases:
            with self.subTest(target=target):
                directory = build_importer.IMPORTER_CACHE / target
                if not directory.is_dir():
                    self.skipTest(
                        f"packaging/importers/{target} is absent — "
                        "run `make importer-fetch`")
                artifacts = [
                    path for path in directory.iterdir()
                    if path.suffix in {".so", ".pyd"}
                ]
                self.assertEqual(
                    len(artifacts), 1,
                    f"expected exactly one extension in {directory}")
                artifact = artifacts[0]
                self.assertEqual(
                    expected_platform,
                    build_importer.artifact_platform(artifact),
                    f"{target} holds a binary for the wrong platform")
                self.assertGreater(artifact.stat().st_size, 2_000_000)

    def test_the_host_artifact_is_lod_capable(self):
        """The one artifact this machine can actually load must be v3.6.

        This is the check whose absence let CI pass green against binaries
        the production prop path refuses.
        """
        target = build_importer.ci_target()
        self.assertIsNotNone(target, "no published importer for this host")
        cached = build_importer.IMPORTER_CACHE / target
        if not cached.is_dir():
            self.skipTest("run `make importer-fetch`")
        with tempfile.TemporaryDirectory() as temporary:
            importer_root = Path(temporary) / "cod-asset-importer"
            (importer_root / "python" / "cod_asset_importer").mkdir(
                parents=True)
            installed = build_importer.build(
                importer_root, require_lod=True, log=lambda _m: None)
            version, error = build_importer.artifact_lod_api_version(installed)
            self.assertEqual(
                version, build_importer.REQUIRED_LOD_API_VERSION, error)

    def test_no_stale_release_archives_remain(self):
        """All five shipped v3.5 zips were LOD-API-0 and refused by
        production, while _extract_from_release would happily pick one. They
        are deleted; this stops them coming back."""
        release = ROOT / "tools" / "cod-asset-importer" / "release"
        self.assertEqual(sorted(release.glob("*.zip")), [])

    def test_source_builds_are_opt_in(self):
        """An automatic cargo fallback is what made rustup a prerequisite."""
        with tempfile.TemporaryDirectory() as temporary:
            importer_root = Path(temporary)
            (importer_root / "python" / "cod_asset_importer").mkdir(
                parents=True)
            with (
                mock.patch.object(
                    build_importer, "_install_from_cache", return_value=False),
                mock.patch.object(
                    build_importer, "_extract_from_release",
                    return_value=False),
                mock.patch.object(build_importer, "_cargo_build") as cargo,
            ):
                with self.assertRaises(build_importer.BuildError) as caught:
                    build_importer.build(importer_root, require_lod=True)
            cargo.assert_not_called()
            message = str(caught.exception)
            self.assertIn("make importer-fetch", message)
            self.assertIn("--from-source", message)

    def test_the_cache_is_preferred_over_release_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            importer_root = Path(temporary)
            (importer_root / "python" / "cod_asset_importer").mkdir(
                parents=True)
            with (
                mock.patch.object(
                    build_importer, "_install_from_cache",
                    return_value=True) as cache,
                mock.patch.object(
                    build_importer, "_extract_from_release") as archive,
            ):
                build_importer.build(importer_root, require_lod=True)
            cache.assert_called_once()
            archive.assert_not_called()

    def test_the_installed_extension_matches_this_host(self):
        """Whatever is installed must be for the machine that will load it.

        This replaces a test that hardcoded ("Darwin", "arm64"): it passed on
        the development Mac and failed on the Windows box that actually ships
        the exporter, which is precisely backwards for a check about host
        compatibility.
        """
        installed = (
            ROOT / "tools" / "cod-asset-importer" / "python"
            / "cod_asset_importer" / build_importer.expected_artifact()
        )
        if not installed.is_file():
            self.skipTest(
                f"{installed.name} is not installed — run `make importer-fetch`")
        self.assertEqual(
            build_importer.host_platform(),
            build_importer.artifact_platform(installed),
        )

    def test_force_build_never_reinstalls_a_bundled_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            importer_root = Path(temporary)
            addon = importer_root / "python" / "cod_asset_importer"
            addon.mkdir(parents=True)
            target = addon / build_importer.expected_artifact()
            with (
                mock.patch.object(
                    build_importer,
                    "_extract_from_release",
                ) as extract,
                mock.patch.object(
                    build_importer,
                    "_cargo_build",
                    side_effect=lambda _root, output, _log:
                        output.write_bytes(b"locally-built"),
                ) as cargo_build,
            ):
                installed = build_importer.build(
                    importer_root,
                    force=True,
                )

            self.assertEqual(installed.read_bytes(), b"locally-built")
            extract.assert_not_called()
            cargo_build.assert_called_once()

    def test_authored_lod_requirement_rebuilds_lod0_only_binary(self):
        """With --from-source, a LOD0-only binary is replaced rather than
        accepted. Without it, build() raises instead (see
        test_source_builds_are_opt_in)."""
        with tempfile.TemporaryDirectory() as temporary:
            importer_root = Path(temporary)
            addon = importer_root / "python" / "cod_asset_importer"
            addon.mkdir(parents=True)
            target = addon / build_importer.expected_artifact()
            target.write_bytes(b"legacy")
            with (
                mock.patch.object(
                    build_importer,
                    "_matches_host",
                    return_value=True,
                ),
                mock.patch.object(
                    build_importer,
                    "artifact_lod_api_version",
                    side_effect=[(0, ""), (1, "")],
                ),
                mock.patch.object(
                    build_importer,
                    "_extract_from_release",
                    return_value=False,
                ),
                mock.patch.object(
                    build_importer,
                    "_install_from_cache",
                    return_value=False,
                ),
                mock.patch.object(
                    build_importer,
                    "_cargo_build",
                    side_effect=lambda _root, output, _log:
                        output.write_bytes(b"lod-capable"),
                ) as cargo_build,
            ):
                installed = build_importer.build(
                    importer_root,
                    require_lod=True,
                    from_source=True,
                )

            self.assertEqual(installed.read_bytes(), b"lod-capable")
            cargo_build.assert_called_once()

    def test_check_reports_lod0_only_without_claiming_lod_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            importer_root = Path(temporary)
            addon = importer_root / "python" / "cod_asset_importer"
            addon.mkdir(parents=True)
            target = addon / build_importer.expected_artifact()
            target.write_bytes(b"legacy")
            with (
                mock.patch.object(
                    build_importer,
                    "_matches_host",
                    return_value=True,
                ),
                mock.patch.object(
                    build_importer,
                    "artifact_platform",
                    return_value=None,
                ),
                mock.patch.object(
                    build_importer,
                    "artifact_lod_api_version",
                    return_value=(0, ""),
                ),
            ):
                base_ok, base_message = build_importer.check(importer_root)
                lod_ok, lod_message = build_importer.check(
                    importer_root,
                    require_lod=True,
                )

            self.assertTrue(base_ok)
            self.assertFalse(lod_ok)
            self.assertIn("LOD0-only", base_message)
            self.assertIn("v3.6+", lod_message)
            self.assertIn("--require-lod", lod_message)


if __name__ == "__main__":
    unittest.main()
