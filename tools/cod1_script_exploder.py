#!/usr/bin/env python3
"""Exact asset reachability for stock multiplayer ``script_exploder``.

CoD1/UO BSPs bind destructible brush/model groups with ``script_exploder``.
Their ``xmodel/fx`` sentinels name an id in ``script_fxid``; the map's
``maps/mp/*_fx.gsc`` maps that id to a retail EFX.  This module follows only
that multiplayer-authored graph:

* active (non-commented) ``level._effect[id] = loadfx(...)`` registrations;
* nested playFx/deathFx/emitFx/impactFx EFX references;
* shader image dependencies, including indirection through fxshaders/*.shader;
* model emitters and embedded sound aliases.

It never enumerates a campaign map or selects an unrelated global FX asset.
The small global registration fallback is intentional: a few shipped MP BSPs
reuse ids such as ``dirt``/``dust`` without re-registering them in their own
map FX script.  A fallback is accepted only when that id has one unambiguous
definition across official multiplayer FX scripts.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable


IMAGE_EXTENSIONS = (".tga", ".dds", ".jpg", ".jpeg", ".png")
NESTED_EFFECT_FIELDS = ("playfx", "deathfx", "emitfx", "impactfx")
_SHADER_REFERENCE_CACHE: dict[int, tuple[object, tuple[object, ...]]] = {}
_SHADER_IMAGE_CACHE: dict[tuple[int, str], tuple[str, ...]] = {}
_SHADER_DECLARATION_CACHE: dict[
    int,
    tuple[object, dict[str, tuple[str, ...]]],
] = {}


class ScriptExploderClosureError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExploderEffectClosure:
    fx_id: str
    source_efx: str
    efx_files: tuple[str, ...]
    image_files: tuple[str, ...]
    model_names: tuple[str, ...]
    sound_aliases: tuple[str, ...]
    shader_names: tuple[str, ...]
    shader_images: tuple[tuple[str, tuple[str, ...]], ...]


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\r\n]*", "", text)


def _normal(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    parsed = PurePosixPath(normalized)
    if (
        not normalized
        or parsed.is_absolute()
        or any(part in ("", ".", "..") for part in parsed.parts)
        or any(":" in part for part in parsed.parts)
    ):
        raise ScriptExploderClosureError(
            f"unsafe script_exploder asset path: {path!r}"
        )
    return parsed.as_posix()


def _members(index) -> dict[str, object]:
    return index.members if hasattr(index, "members") else index


def _get(index, member: str):
    return index.get(_normal(member).casefold())


def _read_ref(index, reference) -> bytes:
    if hasattr(index, "read_ref"):
        return index.read_ref(reference)
    with zipfile.ZipFile(reference.archive) as archive:
        return archive.read(reference.name)


def _read(index, member: str) -> tuple[str, bytes] | None:
    reference = _get(index, member)
    if reference is None:
        return None
    return _normal(reference.name), _read_ref(index, reference)


def _block_values(text: str, field: str) -> tuple[str, ...]:
    values: list[str] = []
    for body in re.findall(
        rf"\b{re.escape(field)}\s*\[([^\]]*)\]",
        strip_comments(text),
        flags=re.IGNORECASE | re.DOTALL,
    ):
        values.extend(re.findall(r"[^\s{}\[\]]+", body))
    return tuple(values)


def effect_registrations(script: str) -> dict[str, str]:
    """Active ``level._effect`` assignments in one MP GSC."""
    clean = strip_comments(script)
    return {
        match.group(1).casefold(): _normal(match.group(2))
        for match in re.finditer(
            r"""level\._effect\s*\[\s*["']([^"']+)["']\s*\]\s*"""
            r"""=\s*loadfx\s*\(\s*["']([^"']+)["']\s*\)""",
            clean,
            flags=re.IGNORECASE,
        )
    }


def sound_registrations(script: str) -> dict[str, str]:
    """Active optional ``level.scr_sound`` assignments in one MP GSC."""
    clean = strip_comments(script)
    return {
        match.group(1).casefold(): match.group(2).casefold()
        for match in re.finditer(
            r"""level\.scr_sound\s*\[\s*["']([^"']+)["']\s*\]\s*"""
            r"""=\s*["']([^"']+)["']""",
            clean,
            flags=re.IGNORECASE,
        )
    }


def called_map_fx_scripts(map_script: str | None) -> tuple[str, ...]:
    if not map_script:
        return ()
    clean = strip_comments(map_script)
    result = {
        _normal(match.group(1)).casefold()
        for match in re.finditer(
            r"\b(maps[\\/]+mp[\\/]+[^:\s()]+_fx)\s*::\s*\w+\s*\(",
            clean,
            flags=re.IGNORECASE,
        )
    }
    return tuple(
        f"{path}.gsc" if not path.endswith(".gsc") else path
        for path in sorted(result)
    )


def _ordered_shader_references(index) -> tuple[object, ...]:
    cache_key = id(index)
    cached = _SHADER_REFERENCE_CACHE.get(cache_key)
    if cached is not None and cached[0] is index:
        return cached[1]
    references = {
        (str(reference.archive), reference.name.casefold()): reference
        for key, reference in _members(index).items()
        if key.endswith(".shader")
    }
    result = tuple(references[key] for key in sorted(references))
    # Retain the index beside the value so a collected object's id cannot be
    # reused for a different archive layer during a long exporter process.
    _SHADER_REFERENCE_CACHE[cache_key] = (index, result)
    return result


def _balanced_block(text: str, opening: int) -> str | None:
    depth = 0
    for position in range(opening, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[opening : position + 1]
    return None


def _shader_image_tokens(index, shader: str) -> tuple[str, ...]:
    """Last active exact shader declaration, matching archive layering."""
    target = _normal(shader)
    cache_key = (id(index), target.casefold())
    cached = _SHADER_IMAGE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    declaration_cache = _SHADER_DECLARATION_CACHE.get(id(index))
    if declaration_cache is not None and declaration_cache[0] is index:
        selected = declaration_cache[1].get(target.casefold(), ())
        _SHADER_IMAGE_CACHE[cache_key] = selected
        return selected

    declarations: dict[str, tuple[str, ...]] = {}
    declaration = re.compile(
        r"(?im)^\s*([^\s{}]+)\s*\r?$\s*\{"
    )
    for reference in _ordered_shader_references(index):
        text = strip_comments(
            _read_ref(index, reference).decode("latin1", "replace")
        )
        for match in declaration.finditer(text):
            name = _normal(match.group(1))
            opening = text.find("{", match.start())
            body = _balanced_block(text, opening)
            if body is None:
                # Some retail shader files contain conditional/legacy text
                # after their last valid declaration. Do not let an unrelated
                # malformed tail poison exact lookups for earlier declarations;
                # a requested declaration with no recorded block will still
                # fail closed when its image cannot be resolved.
                continue
            tokens: list[str] = []
            for line in body.splitlines():
                direct = re.match(
                    # A few retail shaders use the legacy two-token form
                    # ``map clamp /gfx/...`` rather than ``clampmap``.
                    r"\s*(?:map|clampmap)\s+(?:clamp\s+)?(\S+)",
                    line,
                    flags=re.IGNORECASE,
                )
                if direct is not None:
                    tokens.append(direct.group(1))
                    continue
                animated = re.match(
                    r"\s*animmap\s+\S+\s+(.+)",
                    line,
                    flags=re.IGNORECASE,
                )
                if animated is not None:
                    tokens.extend(animated.group(1).split())
            declarations[name.casefold()] = tuple(
                _normal(token)
                for token in tokens
                if token and not token.startswith("$")
            )
    _SHADER_DECLARATION_CACHE[id(index)] = (index, declarations)
    selected = declarations.get(target.casefold(), ())
    _SHADER_IMAGE_CACHE[cache_key] = selected
    return selected


def _resolve_image(index, token: str) -> str:
    normalized = _normal(token)
    lower = normalized.casefold()
    explicit = next(
        (
            extension
            for extension in IMAGE_EXTENSIONS
            if lower.endswith(extension)
        ),
        None,
    )
    if explicit is not None:
        # The retail renderer resolves images by virtual material name. GSC
        # often spells the original TGA while the shipped pak contains the
        # compiled DDS (and occasionally the inverse), so try the authored
        # spelling first and then the same stem in the engine's image order.
        stem = normalized[: -len(explicit)]
        candidates = (
            normalized,
            *(
                stem + extension
                for extension in IMAGE_EXTENSIONS
                if extension != explicit
            ),
        )
    else:
        candidates = tuple(
            normalized + extension
            for extension in IMAGE_EXTENSIONS
        )
    for candidate in candidates:
        reference = _get(index, candidate)
        if reference is not None:
            return _normal(reference.name)
    raise ScriptExploderClosureError(
        f"script_exploder shader image is missing: {token}"
    )


def _effect_path(value: str) -> str:
    normalized = _normal(value)
    return (
        normalized
        if normalized.casefold().endswith(".efx")
        else normalized + ".efx"
    )


def efx_output_path(source: str) -> str:
    path = PurePosixPath(_normal(source))
    parts = (
        path.parts[1:]
        if path.parts and path.parts[0].casefold() == "fx"
        else path.parts
    )
    return PurePosixPath(
        "fx",
        "exploders",
        "efx",
        *parts,
    ).as_posix()


def texture_output_path(source: str) -> str:
    path = PurePosixPath(_normal(source))
    return PurePosixPath(
        "fx",
        "exploders",
        "textures",
        *path.with_suffix(".png").parts,
    ).as_posix()


def efx_shader_names(text: str) -> tuple[str, ...]:
    """Shader names referenced by one EFX definition's ``shaders [ ... ]``
    blocks, deduplicated case-insensitively, first spelling retained."""
    names: dict[str, str] = {}
    for shader in _block_values(text, "shaders"):
        names.setdefault(shader.casefold(), _normal(shader))
    return tuple(names[key] for key in sorted(names))


def resolve_shader_images(index, shader: str) -> tuple[str, ...]:
    """Source image member path(s) behind one fx shader name.

    Declared shaders resolve through the layered ``*.shader`` scripts; a name
    with no declaration is the engine's auto-generated material over the
    same-named base texture (this is how the ``*_sn`` muzzle-flash shaders
    reach ``muzflash2.tga`` — their declarations map the suffixed name back
    to the base image). Raises ScriptExploderClosureError when no image
    exists under any spelling."""
    image_tokens = _shader_image_tokens(index, shader)
    if not image_tokens:
        image_tokens = (shader,)
    resolved: list[str] = []
    seen: set[str] = set()
    for token in image_tokens:
        image = _resolve_image(index, token)
        if image.casefold() not in seen:
            seen.add(image.casefold())
            resolved.append(image)
    return tuple(resolved)


def build_effect_closure(
    index,
    fx_id: str,
    source_efx: str,
    *,
    registered_sound_alias: str = "",
) -> ExploderEffectClosure:
    pending = [_effect_path(source_efx)]
    efx_files: dict[str, str] = {}
    shaders: dict[str, str] = {}
    models: dict[str, str] = {}
    aliases: dict[str, str] = {}
    if registered_sound_alias:
        aliases[registered_sound_alias.casefold()] = (
            registered_sound_alias.casefold()
        )

    while pending:
        requested = pending.pop(0)
        key = requested.casefold()
        if key in efx_files:
            continue
        loaded = _read(index, requested)
        if loaded is None:
            raise ScriptExploderClosureError(
                f"script_exploder EFX is missing: {requested}"
            )
        source_path, raw = loaded
        text = raw.decode("latin1", "replace")
        efx_files[key] = source_path
        for field in NESTED_EFFECT_FIELDS:
            for nested in _block_values(text, field):
                # Retail UO contains engine-rooted virtual asset references
                # such as ``/fx/map_trenches/mortar_mz_nest``.  CoD resolves
                # those identically to ``fx/...``; normalize before checking
                # the namespace so the recursive closure does not silently
                # drop a legitimate dependency.
                normalized_nested = _normal(nested)
                if normalized_nested.casefold().startswith("fx/"):
                    pending.append(_effect_path(normalized_nested))
        for shader in _block_values(text, "shaders"):
            shaders.setdefault(shader.casefold(), _normal(shader))
        for model in _block_values(text, "models"):
            normalized = _normal(model)
            if normalized.casefold().startswith("xmodel/"):
                name = normalized[len("xmodel/") :]
                models.setdefault(name.casefold(), name)
        for alias in _block_values(text, "sounds"):
            if alias.casefold() not in ("", "null"):
                aliases.setdefault(alias.casefold(), alias.casefold())

    image_files: dict[str, str] = {}
    shader_images: list[tuple[str, tuple[str, ...]]] = []
    for shader in (
        shaders[key] for key in sorted(shaders)
    ):
        resolved_for_shader = resolve_shader_images(index, shader)
        for image in resolved_for_shader:
            image_files.setdefault(image.casefold(), image)
        shader_images.append((shader, resolved_for_shader))

    return ExploderEffectClosure(
        fx_id=fx_id.casefold(),
        source_efx=_normal(source_efx),
        efx_files=tuple(
            efx_files[key] for key in sorted(efx_files)
        ),
        image_files=tuple(
            image_files[key] for key in sorted(image_files)
        ),
        model_names=tuple(models[key] for key in sorted(models)),
        sound_aliases=tuple(aliases[key] for key in sorted(aliases)),
        shader_names=tuple(shaders[key] for key in sorted(shaders)),
        shader_images=tuple(shader_images),
    )


def _all_mp_registrations(
    index,
) -> tuple[
    dict[str, dict[str, set[str]]],
    dict[str, dict[str, set[str]]],
]:
    effects: dict[str, dict[str, set[str]]] = {}
    sounds: dict[str, dict[str, set[str]]] = {}
    for key, reference in _members(index).items():
        if (
            not key.startswith("maps/mp/")
            or not key.endswith("_fx.gsc")
            or PurePosixPath(key).name == "_fx.gsc"
        ):
            continue
        tier_directory = reference.archive.parent.name.casefold()
        script = _read_ref(index, reference).decode("latin1", "replace")
        for fx_id, path in effect_registrations(script).items():
            effects.setdefault(fx_id, {}).setdefault(
                tier_directory,
                set(),
            ).add(path)
        for fx_id, alias in sound_registrations(script).items():
            sounds.setdefault(fx_id, {}).setdefault(
                tier_directory,
                set(),
            ).add(alias)
    return effects, sounds


def map_exploder_effects(
    index,
    map_id: str,
    entities: Iterable[dict[str, str]],
    map_script: str | None = None,
) -> tuple[ExploderEffectClosure, ...]:
    fx_ids = {
        entity.get("script_fxid", "").strip().casefold()
        for entity in entities
        if entity.get("script_exploder") and entity.get("script_fxid")
    }
    fx_ids.discard("")
    if not fx_ids:
        return ()

    registrations: dict[str, str] = {}
    sound_aliases: dict[str, str] = {}
    script_paths = list(called_map_fx_scripts(map_script))
    conventional = f"maps/mp/{map_id.casefold()}_fx.gsc"
    conventional_reference = _get(index, conventional)
    if conventional_reference is not None and conventional not in script_paths:
        script_paths.append(conventional)
    map_script_reference = _get(index, f"maps/mp/{map_id.casefold()}.gsc")
    preferred_tier_directory = (
        conventional_reference.archive.parent.name.casefold()
        if conventional_reference is not None
        else (
            map_script_reference.archive.parent.name.casefold()
            if map_script_reference is not None
            else ""
        )
    )
    for path in script_paths:
        loaded = _read(index, path)
        if loaded is None:
            raise ScriptExploderClosureError(
                f"{map_id}: called MP FX script is missing: {path}"
            )
        _source, raw = loaded
        script = raw.decode("latin1", "replace")
        registrations.update(effect_registrations(script))
        sound_aliases.update(sound_registrations(script))

    global_effects, global_sounds = _all_mp_registrations(index)
    result: list[ExploderEffectClosure] = []
    for fx_id in sorted(fx_ids):
        source = registrations.get(fx_id)
        if source is None:
            by_tier = global_effects.get(fx_id, {})
            candidates = by_tier.get(preferred_tier_directory, set())
            if not candidates:
                candidates = {
                    path
                    for tier_paths in by_tier.values()
                    for path in tier_paths
                }
            if len(candidates) != 1:
                raise ScriptExploderClosureError(
                    f"{map_id}: script_fxid '{fx_id}' has no unambiguous "
                    "official multiplayer loadfx registration"
                )
            source = next(iter(candidates))
        sound_alias = sound_aliases.get(fx_id, "")
        if not sound_alias:
            by_tier = global_sounds.get(fx_id, {})
            candidates = by_tier.get(preferred_tier_directory, set())
            if not candidates:
                candidates = {
                    alias
                    for tier_aliases in by_tier.values()
                    for alias in tier_aliases
                }
            if len(candidates) == 1:
                sound_alias = next(iter(candidates))
        result.append(
            build_effect_closure(
                index,
                fx_id,
                source,
                registered_sound_alias=sound_alias,
            )
        )
    return tuple(result)
