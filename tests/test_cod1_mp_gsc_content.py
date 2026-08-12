from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from cod1_mp_gsc_content import (  # noqa: E402
    MANIFEST_FORMAT,
    MANIFEST_VERSION,
    MultiplayerGscContentError,
    analyze_multiplayer_gsc,
    analyze_multiplayer_gsc_sources,
    strip_gsc_comments,
)


@dataclass(frozen=True)
class FakeReference:
    name: str


class FakeIndex:
    def __init__(self, payloads: dict[str, bytes]):
        self.payloads = {
            name.replace("\\", "/").casefold(): payload
            for name, payload in payloads.items()
        }
        self.members = {
            name.replace("\\", "/").casefold(): FakeReference(name)
            for name in payloads
        }

    def read_ref(self, reference: FakeReference) -> bytes:
        return self.payloads[
            reference.name.replace("\\", "/").casefold()
        ]


class MultiplayerGscContentTests(unittest.TestCase):
    def test_comment_stripping_preserves_strings_and_excludes_dead_sinks(
        self,
    ) -> None:
        source = r'''
        main()
        {
            alias = "live_alias";
            // self playSound("line_comment_alias");
            /*
                self playSound("block_comment_alias");
                braces = "}";
            */
            self playSound(alias);
            shader = "gfx/hud/not//a_comment";
            precacheShader(shader);
        }
        '''

        clean = strip_gsc_comments(source)
        closure = analyze_multiplayer_gsc_sources(
            {"maps/mp/mp_test.gsc": source}
        )

        self.assertNotIn("line_comment_alias", clean)
        self.assertNotIn("block_comment_alias", clean)
        self.assertIn('"gfx/hud/not//a_comment"', clean)
        self.assertEqual(closure.sound_aliases, ("live_alias",))
        self.assertEqual(
            closure.shader_names,
            ("gfx/hud/not/a_comment",),
        )

    def test_variable_concatenation_and_local_cross_script_wrappers_close(
        self,
    ) -> None:
        closure = analyze_multiplayer_gsc_sources(
            {
                "maps/mp/mp_test.gsc": r'''
                    main()
                    {
                        game["team"] = "american";
                        game["team"] = "british";
                        alias = game["team"] + "_move_in";
                        maps\mp\shared_audio::announce(alias);
                    }
                ''',
                "maps/mp/shared_audio.gsc": r'''
                    announce(alias)
                    {
                        relay(alias);
                    }

                    relay(value)
                    {
                        self playLocalSound(value);
                    }
                ''',
            }
        )

        self.assertEqual(
            closure.sound_aliases,
            ("american_move_in", "british_move_in"),
        )
        shared = next(
            script
            for script in closure.scripts
            if script.source_path.casefold().endswith(
                "shared_audio.gsc"
            )
        )
        self.assertEqual(shared.sound_aliases, closure.sound_aliases)

    def test_cross_script_return_propagates_arguments_into_nested_sink(
        self,
    ) -> None:
        closure = analyze_multiplayer_gsc_sources(
            {
                "maps/mp/main.gsc": r'''
                    main()
                    {
                        precacheShader(
                            maps\mp\icons::rank_icon("axis")
                        );
                    }
                ''',
                "maps/mp/icons.gsc": r'''
                    rank_icon(team)
                    {
                        return "gfx/hud/" + team + "_rank";
                    }
                ''',
            }
        )

        self.assertEqual(
            closure.shader_names,
            ("gfx/hud/axis_rank",),
        )

    def test_branch_join_preserves_alternatives_without_impossible_chains(
        self,
    ) -> None:
        closure = analyze_multiplayer_gsc_sources(
            {
                "maps/mp/branching.gsc": r'''
                    main()
                    {
                        icon = "allies";
                        switch (objective)
                        {
                        case 0:
                            icon = icon + "_ammo";
                            break;
                        case 1:
                            icon = icon + "_fuel";
                            break;
                        default:
                            icon = icon + "_hq";
                            break;
                        }
                        icon = icon + "_large";
                        precacheShader(icon);

                        icon = "";
                        if (active)
                            icon = "gfx/hud/active";
                        else
                            icon = "gfx/hud/inactive";
                        hud setShader(icon, 32, 32);
                    }
                ''',
            }
        )

        self.assertEqual(
            closure.shader_names,
            (
                "allies_ammo_large",
                "allies_fuel_large",
                "allies_hq_large",
                "gfx/hud/active",
                "gfx/hud/inactive",
            ),
        )
        self.assertFalse(
            any(
                "ammo_fuel" in shader or "fuel_ammo" in shader
                for shader in closure.shader_names
            )
        )

    def test_all_asset_sinks_are_normalized_without_basename_guessing(
        self,
    ) -> None:
        closure = analyze_multiplayer_gsc_sources(
            {
                "maps/mp/assets.gsc": r'''
                    main()
                    {
                        model = "xmodel/" + "mp_field_radio";
                        precacheModel(model);
                        self setModel(model);

                        icon = "gfx/hud/" + "objective";
                        precacheShader(icon);
                        hud setShader(icon, 32, 32);
                        player.headicon = "gfx/hud/headicon@radio";

                        effect = "fx/map_mp/" + "radio_burst";
                        level._effect["radio"] = loadFx(effect);

                        sound = "radio" + "_activate";
                        self playLoopSound(sound);
                    }
                ''',
            }
        )

        self.assertEqual(closure.model_names, ("mp_field_radio",))
        self.assertEqual(
            closure.shader_names,
            ("gfx/hud/headicon@radio", "gfx/hud/objective"),
        )
        self.assertEqual(
            closure.effect_paths,
            ("fx/map_mp/radio_burst.efx",),
        )
        self.assertEqual(closure.sound_aliases, ("radio_activate",))
        self.assertFalse(closure.unresolved_sinks)

    def test_campaign_sources_and_calls_are_not_admitted(self) -> None:
        closure = analyze_multiplayer_gsc_sources(
            {
                "maps/mp/main.gsc": r'''
                    main()
                    {
                        self playSound("mp_live");
                        maps\sp\campaign_audio::main();
                    }
                ''',
                "maps/sp/campaign_audio.gsc": r'''
                    main()
                    {
                        self playSound("sp_forbidden");
                    }
                ''',
            }
        )

        self.assertEqual(closure.script_files, ("maps/mp/main.gsc",))
        self.assertEqual(closure.sound_aliases, ("mp_live",))
        self.assertNotIn("sp_forbidden", closure.sound_aliases)
        self.assertEqual(
            closure.excluded_script_calls,
            ("maps/sp/campaign_audio.gsc",),
        )

    def test_unsafe_source_and_cross_script_traversal_are_rejected(
        self,
    ) -> None:
        with self.assertRaisesRegex(
            MultiplayerGscContentError,
            "unsafe GSC source path",
        ):
            analyze_multiplayer_gsc_sources(
                {
                    "maps/mp/../sp/escape.gsc":
                        'main() { self playSound("bad"); }'
                }
            )

        with self.assertRaisesRegex(
            MultiplayerGscContentError,
            "unsafe GSC call path",
        ):
            analyze_multiplayer_gsc_sources(
                {
                    "maps/mp/main.gsc":
                        r"main() { maps\mp\..\sp\escape::main(); }"
                }
            )

    def test_unsafe_resolved_asset_references_are_rejected(self) -> None:
        fixtures = (
            ('self playSound("../secret");', "sound reference"),
            ('precacheModel("/xmodel/secret");', "model reference"),
            ('precacheShader("gfx/../../secret");', "shader reference"),
            ('loadFx("maps/sp/secret.efx");', "outside fx/"),
        )
        for statement, expected in fixtures:
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(
                    MultiplayerGscContentError,
                    expected,
                ):
                    analyze_multiplayer_gsc_sources(
                        {
                            "maps/mp/main.gsc":
                                f"main() {{ {statement} }}"
                        }
                    )

    def test_dynamic_engine_values_are_reported_and_can_be_strict(self) -> None:
        sources = {
            "maps/mp/main.gsc": r'''
                main()
                {
                    level._effect["dynamic"] = loadFx(self.script_fx);
                    precacheShader(nativeLayoutName());
                }
            ''',
        }

        closure = analyze_multiplayer_gsc_sources(sources)

        self.assertEqual(len(closure.unresolved_sinks), 2)
        self.assertEqual(
            {sink.sink for sink in closure.unresolved_sinks},
            {"loadfx", "precacheshader"},
        )
        with self.assertRaisesRegex(
            MultiplayerGscContentError,
            "unresolved",
        ):
            analyze_multiplayer_gsc_sources(
                sources,
                strict_unresolved=True,
            )

    def test_layered_index_api_selects_only_active_mp_gsc_and_manifest_is_stable(
        self,
    ) -> None:
        closure = analyze_multiplayer_gsc(
            FakeIndex(
                {
                    "maps/MP/z_last.gsc":
                        b'main() { self playSound("z_alias"); }',
                    "maps/mp/A_first.gsc":
                        b'main() { precacheModel("xmodel/a_model"); }',
                    "maps/sp/ignored.gsc":
                        b'main() { self playSound("sp_alias"); }',
                    "maps/mp/not_script.txt": b"ignored",
                }
            )
        )
        manifest = closure.to_manifest()

        self.assertEqual(
            closure.script_files,
            ("maps/mp/A_first.gsc", "maps/MP/z_last.gsc"),
        )
        self.assertEqual(closure.sound_aliases, ("z_alias",))
        self.assertEqual(closure.model_names, ("a_model",))
        self.assertEqual(manifest["format"], MANIFEST_FORMAT)
        self.assertEqual(manifest["version"], MANIFEST_VERSION)
        self.assertEqual(
            manifest["counts"],
            {
                "scripts": 2,
                "soundAliases": 1,
                "modelNames": 1,
                "shaderNames": 0,
                "effectPaths": 0,
                "unresolvedSinks": 0,
                "excludedScriptCalls": 0,
            },
        )

    def test_selected_maps_keep_shared_scripts_and_exclude_other_map_assets(
        self,
    ) -> None:
        closure = analyze_multiplayer_gsc(
            FakeIndex(
                {
                    "maps/mp/_utility.gsc":
                        b'main() { self playSound("shared_alias"); }',
                    "maps/mp/gametypes/tdm.gsc":
                        b'main() { precacheShader("shared_shader"); }',
                    "maps/mp/mp_pavlov.gsc":
                        b'main() { loadFx("fx/pavlov.efx"); }',
                    "maps/mp/mp_pavlov_fx.gsc":
                        b'main() { self playSound("pavlov_alias"); }',
                    "maps/mp/mp_bocage.gsc":
                        b'main() { loadFx("fx/bocage.efx"); }',
                    "maps/mp/mp_bocage_fx.gsc":
                        b'main() { self playSound("bocage_alias"); }',
                }
            ),
            selected_maps=("mp_pavlov",),
        )

        self.assertEqual(
            {path.casefold() for path in closure.script_files},
            {
                "maps/mp/_utility.gsc",
                "maps/mp/gametypes/tdm.gsc",
                "maps/mp/mp_pavlov.gsc",
                "maps/mp/mp_pavlov_fx.gsc",
            },
        )
        self.assertEqual(
            closure.sound_aliases,
            ("pavlov_alias", "shared_alias"),
        )
        self.assertEqual(closure.effect_paths, ("fx/pavlov.efx",))
        self.assertNotIn("bocage_alias", closure.sound_aliases)


if __name__ == "__main__":
    unittest.main()
