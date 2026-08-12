from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
EXPORTER = ROOT / "exporter"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(EXPORTER))

PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "fod_package_prop_lods_under_test",
    EXPORTER / "package.py",
)
assert PACKAGE_SPEC is not None and PACKAGE_SPEC.loader is not None
fod_package = importlib.util.module_from_spec(PACKAGE_SPEC)
PACKAGE_SPEC.loader.exec_module(fod_package)

import pipeline  # noqa: E402


class PropLodValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.content = Path(self.temporary.name) / "content"
        self.game = Path(self.temporary.name) / "game"
        self.game.mkdir()
        for relative in (
            "props/tree/prop.glb",
            "props/tree/lod1.glb",
            "props/tree/lod2.glb",
        ):
            path = self.content / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_runtime_metadata_can_parse_but_is_not_export_complete(
        self,
    ) -> None:
        prop = {
            "name": "tree",
            "glb": "props/tree/prop.glb",
            "animated": False,
        }
        errors: list[str] = []
        fod_package.validate_prop_lods(self.content, prop, errors)
        self.assertEqual(errors, [])
        required_errors: list[str] = []
        fod_package.validate_prop_lods(
            self.content,
            prop,
            required_errors,
            required=True,
        )
        self.assertTrue(
            any("authored lods are missing" in error
                for error in required_errors),
            required_errors,
        )
        self.assertFalse(
            pipeline._prop_lods_done(
                pipeline.PipelineConfig(self.game, self.content),
                prop,
            )
        )

    def test_animated_prop_does_not_require_static_lods(self) -> None:
        self.assertTrue(
            pipeline._prop_lods_done(
                pipeline.PipelineConfig(self.game, self.content),
                {
                    "name": "mg42_bipod",
                    "glb": "props/tree/prop.glb",
                    "animated": True,
                },
            )
        )

    def test_authored_lod_inventory_is_validated_with_all_files(self) -> None:
        prop = {
            "name": "tree",
            "glb": "props/tree/prop.glb",
            "lods": [
                {
                    "glb": "props/tree/prop.glb",
                    "sourceSurface": "tree1",
                    "distance": 100,
                },
                {
                    "glb": "props/tree/lod1.glb",
                    "sourceSurface": "tree2",
                    "distance": 250,
                },
                {
                    "glb": "props/tree/lod2.glb",
                    "sourceSurface": "tree3",
                    "distance": 0,
                },
            ],
        }
        errors: list[str] = []
        fod_package.validate_prop_lods(self.content, prop, errors)
        self.assertEqual(errors, [])
        self.assertTrue(
            pipeline._prop_lods_done(
                pipeline.PipelineConfig(self.game, self.content),
                prop,
            )
        )

    def test_bad_distances_and_missing_files_are_rejected(self) -> None:
        prop = {
            "name": "tree",
            "glb": "props/tree/prop.glb",
            "lods": [
                {
                    "glb": "props/tree/prop.glb",
                    "sourceSurface": "tree1",
                    "distance": 100,
                },
                {
                    "glb": "props/tree/missing.glb",
                    "sourceSurface": "",
                    "distance": 50,
                },
            ],
        }
        errors: list[str] = []
        fod_package.validate_prop_lods(self.content, prop, errors)
        self.assertTrue(any("missing glb" in error for error in errors), errors)
        self.assertTrue(
            any("sourceSurface is empty" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("cull distance must exceed" in error for error in errors),
            errors,
        )
        self.assertFalse(
            pipeline._prop_lods_done(
                pipeline.PipelineConfig(self.game, self.content),
                prop,
            )
        )

    def test_duplicate_or_unsafe_lod_paths_are_rejected(self) -> None:
        prop = {
            "name": "tree",
            "glb": "props/tree/prop.glb",
            "lods": [
                {
                    "glb": "props/tree/prop.glb",
                    "sourceSurface": "tree1",
                    "distance": 100,
                },
                {
                    "glb": "props/tree/prop.glb",
                    "sourceSurface": "tree2",
                    "distance": 200,
                },
                {
                    "glb": "../outside.glb",
                    "sourceSurface": "tree3",
                    "distance": 0,
                },
            ],
        }
        errors: list[str] = []
        fod_package.validate_prop_lods(self.content, prop, errors)
        self.assertTrue(any("duplicate glb" in error for error in errors), errors)
        self.assertTrue(
            any("unsafe content path" in error for error in errors),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
