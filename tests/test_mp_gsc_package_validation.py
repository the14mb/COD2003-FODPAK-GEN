from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from exporter.package import (  # noqa: E402
    MP_GSC_CONTENT_COUNTS,
    MP_GSC_MISSING_RETAIL_EFFECT_REASON,
    MP_GSC_MISSING_RETAIL_EFFECTS,
    MP_GSC_SILENT_SOUND_ALIASES,
    MP_GSC_UNRESOLVED_SHADER_REASON,
    _mp_gsc_validate_presentation,
    _mp_gsc_efx_output,
    _mp_gsc_texture_output,
    validate_multiplayer_gsc_content,
)
from cod1_shipping_maps import selected_shipping_map_ids  # noqa: E402


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def provenance(
    source: str,
    raw: bytes,
    *,
    tier: str = "cod1",
) -> dict[str, object]:
    return {
        "tier": tier,
        "archive": (
            "Main/pak5.pk3"
            if tier == "cod1"
            else "uo/pakuo00.pk3"
        ),
        "member": source,
        "bytes": len(raw),
        "crc32": "00000000",
        "sha256": sha256(raw),
    }


class MultiplayerGscPackageValidationTests(unittest.TestCase):
    def build_fixture(
        self,
        content: Path,
    ) -> tuple[dict[str, object], set[str], set[str]]:
        counts = dict(MP_GSC_CONTENT_COUNTS)
        scripts = [
            f"maps/mp/script{index:03d}.gsc"
            for index in range(counts["scripts"])
        ]
        direct_aliases = [
            f"alias{index:03d}"
            for index in range(counts["soundAliases"])
        ]
        direct_models = [
            f"model{index:03d}"
            for index in range(counts["modelNames"])
        ]
        shaders = [
            f"shader{index:03d}"
            for index in range(counts["shaderNames"])
        ]
        resolved_effect_count = counts["resolvedEffects"]
        resolved_effects = [
            f"fx/effect{index:03d}.efx"
            for index in range(resolved_effect_count)
        ]
        effect_paths = sorted(
            [
                *resolved_effects,
                *MP_GSC_MISSING_RETAIL_EFFECTS,
            ],
            key=str.casefold,
        )
        embedded_aliases = [
            f"embeddedalias{index:03d}"
            for index in range(
                counts["runtimeSoundAliases"]
                - counts["soundAliases"]
            )
        ]
        embedded_models = [
            f"embeddedmodel{index:03d}"
            for index in range(
                counts["runtimeModelNames"]
                - counts["modelNames"]
            )
        ]
        runtime_aliases = sorted(
            [*direct_aliases, *embedded_aliases],
            key=str.casefold,
        )
        runtime_models = sorted(
            [*direct_models, *embedded_models],
            key=str.casefold,
        )

        analysis_scripts = []
        for index, source in enumerate(scripts):
            analysis_scripts.append({
                "source": source,
                "soundAliases": direct_aliases if index == 0 else [],
                "modelNames": direct_models if index == 0 else [],
                "shaderNames": shaders if index == 0 else [],
                "effectPaths": effect_paths if index == 0 else [],
            })
        unresolved_sinks = [
            {
                "script": scripts[0],
                "function": "main",
                "sink": "playsound",
                "expression": f"self.dynamic{index}",
            }
            for index in range(counts["unresolvedSinks"])
        ]
        excluded_calls = [
            f"mptype/excluded{index:03d}.gsc"
            for index in range(counts["excludedScriptCalls"])
        ]
        analysis_counts = {
            key: counts[key]
            for key in (
                "scripts",
                "soundAliases",
                "modelNames",
                "shaderNames",
                "effectPaths",
                "unresolvedSinks",
                "excludedScriptCalls",
            )
        }
        analysis = {
            "format": "FriendsOfDuty.MultiplayerGscContentClosure",
            "version": 1,
            "scriptFiles": scripts,
            "soundAliases": direct_aliases,
            "modelNames": direct_models,
            "shaderNames": shaders,
            "effectPaths": effect_paths,
            "scripts": analysis_scripts,
            "unresolvedSinks": unresolved_sinks,
            "excludedScriptCalls": excluded_calls,
            "counts": analysis_counts,
        }

        script_files = []
        for source in scripts:
            raw = f"script:{source}".encode()
            script_files.append({
                "source": source,
                "sourceProvenance": provenance(source, raw),
                "sha256": sha256(raw),
            })

        efx_sources = list(resolved_effects)
        efx_sources.extend(
            f"fx/nested/nested{index:03d}.efx"
            for index in range(
                counts["recursiveEfxFiles"] - len(resolved_effects)
            )
        )
        efx_sources.sort(key=str.casefold)
        efx_files = []
        for source in efx_sources:
            raw = f"efx:{source}".encode()
            path = _mp_gsc_efx_output(source)
            destination = content / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            efx_files.append({
                "source": source,
                "path": path,
                "sourceProvenance": provenance(source, raw),
                "sha256": sha256(raw),
            })

        texture_sources = [
            f"gfx/test/texture{index:03d}.tga"
            for index in range(counts["textureFiles"])
        ]
        texture_files = []
        for source in texture_sources:
            source_raw = f"source-texture:{source}".encode()
            output_raw = f"png:{source}".encode()
            path = _mp_gsc_texture_output(source)
            destination = content / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(output_raw)
            texture_files.append({
                "source": source,
                "path": path,
                "sourceProvenance": provenance(source, source_raw),
                "sourceSha256": sha256(source_raw),
                "sha256": sha256(output_raw),
            })

        textures_by_effect: list[list[str]] = [
            [] for _source in resolved_effects
        ]
        for index, source in enumerate(texture_sources):
            textures_by_effect[index % len(resolved_effects)].append(
                _mp_gsc_texture_output(source)
            )
        nested_outputs = [
            _mp_gsc_efx_output(source)
            for source in efx_sources
            if source.casefold().startswith("fx/nested/")
        ]
        effects = []
        for index, source in enumerate(resolved_effects):
            texture_paths = textures_by_effect[index]
            effect_efx = [_mp_gsc_efx_output(source)]
            if index == 0:
                effect_efx.extend(nested_outputs)
            effects.append({
                "fxId": source.casefold(),
                "sourceEfx": source,
                "efxFiles": effect_efx,
                "texturePngs": texture_paths,
                "shaderTextures": [
                    {
                        "shader": f"effect_shader_{index:03d}_{frame:03d}",
                        "texturePngs": [texture],
                    }
                    for frame, texture in enumerate(texture_paths)
                ],
                "aliasNames": embedded_aliases if index == 0 else [],
                "modelNames": embedded_models if index == 0 else [],
            })

        resolved_shader_count = counts["resolvedShaderHandles"]
        shader_textures = [
            {
                "shader": shader,
                "texturePngs": [
                    _mp_gsc_texture_output(
                        texture_sources[index % len(texture_sources)]
                    )
                ],
            }
            for index, shader in enumerate(
                shaders[:resolved_shader_count]
            )
        ]
        unresolved_shaders = [
            {
                "shader": shader,
                "reason": MP_GSC_UNRESOLVED_SHADER_REASON,
            }
            for shader in shaders[resolved_shader_count:]
        ]
        missing_effects = [
            {
                "path": path,
                "reason": MP_GSC_MISSING_RETAIL_EFFECT_REASON,
            }
            for path in MP_GSC_MISSING_RETAIL_EFFECTS
        ]
        manifest: dict[str, object] = {
            "format": "FriendsOfDuty.MultiplayerGscRuntimeContent",
            "version": 1,
            "tiers": ["cod1", "uo"],
            "selectedMaps": list(
                selected_shipping_map_ids(include_uo=True)
            ),
            "analysis": analysis,
            "effects": effects,
            "shaderTextures": shader_textures,
            "soundAliases": runtime_aliases,
            "modelNames": runtime_models,
            "missingRetailEffects": missing_effects,
            "unresolvedShaders": unresolved_shaders,
            "scriptFiles": script_files,
            "efxFiles": efx_files,
            "textureFiles": texture_files,
            "counts": counts,
        }
        manifest_path = content / "scripts/mp/mp_content.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        presentation_aliases = (
            set(runtime_aliases)
            - MP_GSC_SILENT_SOUND_ALIASES
        )
        return manifest, presentation_aliases, set(runtime_models)

    @staticmethod
    def write_manifest(content: Path, manifest: dict[str, object]) -> None:
        (content / "scripts/mp/mp_content.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def validate_fixture(
        self,
        content: Path,
        aliases: set[str],
        models: set[str],
    ) -> list[str]:
        errors: list[str] = []
        validate_multiplayer_gsc_content(
            content,
            errors,
            presentation_aliases=aliases,
            prop_names=models,
        )
        return errors

    def test_valid_base_closure_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            _manifest, aliases, models = self.build_fixture(content)
            self.assertEqual(
                self.validate_fixture(content, aliases, models),
                [],
            )

    def test_missing_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            errors: list[str] = []
            validate_multiplayer_gsc_content(
                Path(temporary),
                errors,
            )
            self.assertTrue(
                any("mp_content.json missing" in error for error in errors)
            )

    def test_schema_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            manifest["guessedFallbacks"] = ["fx/generic.efx"]
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any("schema is unsupported" in error for error in errors)
            )

    def test_selected_map_roster_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            manifest["selectedMaps"] = [
                *manifest["selectedMaps"],
                "mp_bocage",
            ]
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any(
                    "selectedMaps differs from the exact shipping map roster"
                    in error
                    for error in errors
                ),
                errors,
            )

    def test_missing_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            first = manifest["textureFiles"][0]
            (content / first["path"]).unlink()
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any(
                    "texture file[0] output hash/path mismatch" in error
                    for error in errors
                )
            )

    def test_output_hash_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            first = manifest["efxFiles"][0]
            (content / first["path"]).write_bytes(b"tampered")
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any(
                    "EFX file[0] output hash/path mismatch" in error
                    for error in errors
                )
            )

    def test_source_provenance_hash_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            manifest["scriptFiles"][0]["sourceProvenance"]["sha256"] = (
                "0" * 64
            )
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any(
                    "source hash/provenance mismatch" in error
                    for error in errors
                )
            )

    def test_unsafe_source_derived_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            manifest["textureFiles"][0]["path"] = "../escaped.png"
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any("path is unsafe" in error for error in errors)
            )
            self.assertTrue(
                any(
                    "output is not the exact source-derived path" in error
                    for error in errors
                )
            )

    def test_effect_shader_closure_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            effect = manifest["effects"][0]
            effect["shaderTextures"][0]["texturePngs"] = [
                manifest["effects"][1]["texturePngs"][0]
            ]
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any(
                    "shader mapping does not exactly cover" in error
                    for error in errors
                )
            )

    def test_uo_requires_exact_declared_retail_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(
                content,
            )
            errors = self.validate_fixture(
                content,
                aliases,
                models,
            )
            self.assertEqual(errors, [])
            manifest["missingRetailEffects"][0]["path"] = (
                "fx/explosions/vehicles/generic_complete.efx"
            )
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(
                content,
                aliases,
                models,
            )
            self.assertTrue(
                any(
                    "missing retail effect roster differs" in error
                    for error in errors
                )
            )

    def test_archive_tier_mismatch_is_rejected(self) -> None:
        # UO provenance is legitimate now, so the remaining defect this
        # guards is a record whose archive and tier disagree.
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            record = manifest["scriptFiles"][0]["sourceProvenance"]
            record["tier"] = "cod1"
            record["archive"] = "uo/pakuo00.pk3"
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(errors, "archive/tier mismatch must be rejected")

    def test_uo_provenance_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            manifest, aliases, models = self.build_fixture(content)
            record = manifest["scriptFiles"][0]["sourceProvenance"]
            record["tier"] = "uo"
            record["archive"] = "uo/pakuo00.pk3"
            self.write_manifest(content, manifest)
            errors = self.validate_fixture(content, aliases, models)
            self.assertFalse(
                [e for e in errors if "nonselected/nonofficial" in e],
                errors,
            )

    def test_runtime_alias_and_model_rosters_must_be_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            _manifest, aliases, models = self.build_fixture(content)
            aliases.remove("alias000")
            models.remove("model000")
            errors = self.validate_fixture(content, aliases, models)
            self.assertTrue(
                any("omits active MP GSC aliases" in error for error in errors)
            )
            self.assertTrue(
                any("omits active MP GSC/EFX models" in error for error in errors)
            )

    def test_localized_mp_voice_provenance_passes_and_sp_voice_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            content = Path(temporary)
            mp_file = (
                "voiceovers/mp/us/cmds/us_cmd_movein01.wav"
            )
            raw = b"reached multiplayer voice"
            destination = content / "audio" / mp_file
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            variant = {
                "file": mp_file,
                "volume_min": 1.0,
                "volume_max": 1.0,
                "pitch_min": 1.0,
                "pitch_max": 1.0,
                "distance_min_inches": 45.0,
                "distance_max_inches": 4800.0,
            }
            presentation = {
                "selectedMaps": list(
                    selected_shipping_map_ids(include_uo=True)
                ),
                "aliases": [{
                    "alias": "american_move_in",
                    "variants": [variant],
                }],
                "provenance": [{
                    "path": "audio/" + mp_file,
                    "archive":
                        "Main/localized_english_pak1.pk3",
                    "member": "sound/" + mp_file,
                    "bytes": len(raw),
                    "sha256": sha256(raw),
                }],
            }
            errors: list[str] = []
            _mp_gsc_validate_presentation(
                content,
                presentation,
                {"american_move_in"},
                errors,
                game_root=None,
                retail_index=None,
            )
            self.assertEqual(errors, [])

            presentation["selectedMaps"] = [
                *presentation["selectedMaps"],
                "mp_bocage",
            ]
            errors = []
            _mp_gsc_validate_presentation(
                content,
                presentation,
                {"american_move_in"},
                errors,
                game_root=None,
                retail_index=None,
            )
            self.assertTrue(
                any(
                    "selectedMaps differs from the exact shipping map roster"
                    in error
                    for error in errors
                ),
                errors,
            )
            presentation["selectedMaps"] = list(
                selected_shipping_map_ids(include_uo=True)
            )

            sp_file = (
                "voiceovers/stalingrad/BuildingCommissar.wav"
            )
            sp_raw = b"campaign voice"
            sp_destination = content / "audio" / sp_file
            sp_destination.parent.mkdir(parents=True, exist_ok=True)
            sp_destination.write_bytes(sp_raw)
            variant["file"] = sp_file
            presentation["provenance"] = [{
                "path": "audio/" + sp_file,
                "archive": "Main/localized_english_pak1.pk3",
                "member": "sound/" + sp_file,
                "bytes": len(sp_raw),
                "sha256": sha256(sp_raw),
            }]
            errors = []
            _mp_gsc_validate_presentation(
                content,
                presentation,
                {"american_move_in"},
                errors,
                game_root=None,
                retail_index=None,
            )
            self.assertTrue(
                any(
                    "SinglePlayer voiceover member" in error
                    for error in errors
                )
            )


if __name__ == "__main__":
    unittest.main()
