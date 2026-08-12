from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"


def load_prop_exporter():
    fake_bpy = types.ModuleType("bpy")
    fake_mathutils = types.ModuleType("mathutils")
    fake_mathutils.Matrix = object
    fake_mathutils.Quaternion = object
    fake_mathutils.Vector = object
    spec = importlib.util.spec_from_file_location(
        "fod_prop_exporter_fingerprint_under_test",
        TOOLS / "export_cod_multiplayer_props.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with (
        mock.patch.dict(
            sys.modules,
            {
                "bpy": fake_bpy,
                "mathutils": fake_mathutils,
            },
        ),
        mock.patch.object(
            sys,
            "path",
            [str(TOOLS), *sys.path],
        ),
    ):
        spec.loader.exec_module(module)
    return module


prop_exporter = load_prop_exporter()


class PropExportFingerprintTests(unittest.TestCase):
    def test_source_manifest_and_native_module_invalidate_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            package = root / "cod_asset_importer"
            source.mkdir()
            package.mkdir()
            manifest = source / "extraction_manifest.json"
            wrapper = package / "importer.py"
            native = package / "cod_asset_importer.abi3.so"
            manifest.write_text('{"archiveFingerprint":"first"}')
            wrapper.write_text("wrapper = 1")
            (package / "__init__.py").write_text("")
            native.write_bytes(b"native-one")

            importer = types.SimpleNamespace(
                __file__=str(wrapper),
                XMODEL_LOD_API_VERSION=1,
            )
            first = prop_exporter.prop_export_fingerprint(
                importer,
                source,
            )

            manifest.write_text('{"archiveFingerprint":"second"}')
            source_changed = prop_exporter.prop_export_fingerprint(
                importer,
                source,
            )
            self.assertNotEqual(first, source_changed)

            native.write_bytes(b"native-two")
            compiler_changed = prop_exporter.prop_export_fingerprint(
                importer,
                source,
            )
            self.assertNotEqual(source_changed, compiler_changed)

    def test_manifestless_source_falls_back_to_file_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            package = root / "cod_asset_importer"
            (source / "xmodel").mkdir(parents=True)
            package.mkdir()
            wrapper = package / "importer.py"
            model = source / "xmodel" / "tree"
            wrapper.write_text("wrapper = 1")
            (package / "__init__.py").write_text("")
            (package / "cod_asset_importer.pyd").write_bytes(b"native")
            model.write_bytes(b"source-one")
            importer = types.SimpleNamespace(
                __file__=str(wrapper),
                XMODEL_LOD_API_VERSION=1,
            )

            first = prop_exporter.prop_export_fingerprint(
                importer,
                source,
            )
            model.write_bytes(b"source-two")
            second = prop_exporter.prop_export_fingerprint(
                importer,
                source,
            )
            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
