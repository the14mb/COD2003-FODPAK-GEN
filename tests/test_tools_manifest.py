"""The payload allow-list must stay equal to the computed import closure.

Two failures this prevents, both of which ship today:

  * a new import leaves a module OUT of the payload, and the export dies on
    the player's machine at a step that works fine in a checkout;
  * a new dev tool ships TO every customer, as 24 of them plus
    build_importer.py currently do — the last of which prompts a player to
    install rustup.

The manifest is generated, so this test is the thing that makes it binding.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packaging"))

import tools_closure  # noqa: E402


class ManifestTests(unittest.TestCase):
    def test_the_checked_in_manifest_equals_the_computed_closure(self):
        computed = tools_closure.closure()
        recorded = tools_closure.read_manifest()
        missing = sorted(set(computed) - set(recorded))
        extra = sorted(set(recorded) - set(computed))
        self.assertEqual(
            (missing, extra), ([], []),
            "packaging/tools_manifest.txt is stale — regenerate with "
            "`python3 packaging/tools_closure.py --write`. "
            f"missing from the manifest: {missing}; "
            f"no longer reachable: {extra}")

    def test_every_manifest_entry_exists(self):
        for name in tools_closure.read_manifest():
            with self.subTest(module=name):
                self.assertTrue(
                    (tools_closure.TOOLS_DIR / f"{name}.py").is_file())

    def test_every_tool_is_either_shipped_or_explicitly_unreachable(self):
        """No third category: a tool is in the payload or it is not."""
        everything = {path.stem for path in tools_closure.TOOLS_DIR.glob("*.py")}
        self.assertEqual(
            everything,
            set(tools_closure.closure()) | set(tools_closure.unreachable()))

    def test_the_blender_step_tools_are_all_shipped(self):
        """These six ARE the export; missing one is a dead pipeline."""
        for name in ("export_cod1_demo_viewmodels", "batch_export_cod1_models",
                     "export_cod1_multiplayer_players",
                     "export_cod_multiplayer_props"):
            with self.subTest(module=name):
                self.assertIn(name, tools_closure.closure())

    def test_shared_helpers_reached_only_by_import_are_shipped(self):
        """The closure has to follow imports, not just argv.

        fod_export_common and fod_glb_writer are never named on a command
        line; they are imported by the tools that are. A manifest built from
        argv alone would ship an export that cannot start.
        """
        for name in ("fod_export_common", "fod_glb_writer", "fod_decal_alpha",
                     "cod1_archive_policy"):
            with self.subTest(module=name):
                self.assertIn(name, tools_closure.closure())

    def test_dev_only_tools_never_ship(self):
        # generate_ui_assets was a fifth name here until the generator was
        # split out of the game repository. It produces the GAME's menu chrome
        # into Assets/Resources/UI and explicitly defers pak art to this tool,
        # so it stayed behind — and a module that is not in tools/ cannot be in
        # the exclusion set. The four below still cover every naming shape the
        # allow-list has to reject.
        excluded = set(tools_closure.unreachable())
        for name in ("audit_viewmodel_pair", "report_skeleton_hierarchy",
                     "diagnose_cod1_viewmodel", "render_viewmodel_pose"):
            with self.subTest(module=name):
                self.assertIn(name, excluded)

    def test_the_two_tools_a_deny_list_would_leak_are_excluded(self):
        """Why this is an allow-list.

        Neither name matches audit_*/report_*/diagnose_*/render_*/test_*, so
        the obvious deny-list ships both. An allow-list fails closed.
        """
        excluded = set(tools_closure.unreachable())
        self.assertIn("extract_cod1_pavlov_entities", excluded)
        self.assertIn("sync_cod1_unity_weapons", excluded)

    def test_the_closure_is_a_strict_subset_of_tools(self):
        self.assertLess(
            len(tools_closure.closure()),
            len(list(tools_closure.TOOLS_DIR.glob("*.py"))))


if __name__ == "__main__":
    unittest.main()
