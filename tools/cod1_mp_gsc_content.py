#!/usr/bin/env python3
"""Bounded asset reachability for active retail multiplayer GSC.

The stock game stores multiplayer map and game-type logic below
``maps/mp``.  Those scripts reference presentation assets through a small
number of engine sinks (for example ``playSound`` and ``precacheModel``),
but the sink argument is often passed through a wrapper or assembled from
``game[...]`` constants.

This module performs a deliberately small, auditable string analysis:

* only the active, layered ``maps/mp/**/*.gsc`` members are read;
* line and block comments are removed with string literals preserved;
* literal strings, symbol assignments, ``+`` concatenation, function
  parameters, local calls, and explicit cross-script calls are propagated;
* cross-script return values made solely from those expressions are followed;
* sound, model, shader, and ``loadFx`` sinks are collected deterministically.

It is not a general GSC interpreter.  Expressions fed by BSP fields, native
functions, or otherwise dynamic state are reported as unresolved rather than
being guessed.  Calls outside ``maps/mp`` are recorded but never followed.
Unsafe source, call, or asset paths are hard errors.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Mapping

from cod1_shipping_maps import is_selected_multiplayer_script


MANIFEST_FORMAT = "FriendsOfDuty.MultiplayerGscContentClosure"
MANIFEST_VERSION = 1

MAX_VALUES_PER_EXPRESSION = 16_384
MAX_ANALYSIS_PASSES = 1_024

_SOUND_SINKS = frozenset(
    (
        "loopsound",
        "playlocalsound",
        "playloopsound",
        "playsound",
        "precachesound",
    )
)
_MODEL_SINKS = frozenset(
    (
        "precachemodel",
        "setmodel",
        "setviewmodel",
    )
)
_SHADER_SINKS = frozenset(("precacheshader", "setshader"))
_EFFECT_SINKS = frozenset(("loadfx",))
_ALL_SINKS = _SOUND_SINKS | _MODEL_SINKS | _SHADER_SINKS | _EFFECT_SINKS

_FUNCTION_HEADER = re.compile(
    r"\b(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*\((?P<parameters>[^()]*)\)\s*\{"
)
_CALL = re.compile(
    r"(?:(?P<path>"
    r"[A-Za-z_][A-Za-z0-9_.-]*"
    r"(?:[\\/]+[A-Za-z_.-][A-Za-z0-9_.-]*)+"
    r")\s*::\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_SYMBOL = re.compile(
    r"""
    [A-Za-z_][A-Za-z0-9_]*
    (?:
        \s*\.\s*[A-Za-z_][A-Za-z0-9_]*
        |
        \s*\[
            \s*
            (?:
                "(?:\\.|[^"\\])*"
                |
                '(?:\\.|[^'\\])*'
                |
                [A-Za-z_][A-Za-z0-9_]*
                |
                [0-9]+
            )
            \s*
        \]
    )*
    """,
    flags=re.VERBOSE,
)
_ASSIGNMENT = re.compile(
    rf"(?P<lhs>{_SYMBOL.pattern})"
    r"\s*(?<![=!<>+\-*/%&|])=(?!=)\s*"
    r"(?P<rhs>[^;]*);",
    flags=re.VERBOSE | re.DOTALL,
)
_RETURN = re.compile(r"\breturn\b(?P<value>[^;]*);", flags=re.IGNORECASE)


class MultiplayerGscContentError(RuntimeError):
    """The selected MP script graph is unsafe or exceeds analysis bounds."""


@dataclass(frozen=True)
class UnresolvedAssetSink:
    script_path: str
    function_name: str
    sink: str
    expression: str

    def to_manifest(self) -> dict[str, str]:
        return {
            "script": self.script_path,
            "function": self.function_name,
            "sink": self.sink,
            "expression": self.expression,
        }


@dataclass(frozen=True)
class MultiplayerGscScriptContent:
    source_path: str
    sound_aliases: tuple[str, ...]
    model_names: tuple[str, ...]
    shader_names: tuple[str, ...]
    effect_paths: tuple[str, ...]

    def to_manifest(self) -> dict[str, object]:
        return {
            "source": self.source_path,
            "soundAliases": list(self.sound_aliases),
            "modelNames": list(self.model_names),
            "shaderNames": list(self.shader_names),
            "effectPaths": list(self.effect_paths),
        }


@dataclass(frozen=True)
class MultiplayerGscContentClosure:
    script_files: tuple[str, ...]
    sound_aliases: tuple[str, ...]
    model_names: tuple[str, ...]
    shader_names: tuple[str, ...]
    effect_paths: tuple[str, ...]
    scripts: tuple[MultiplayerGscScriptContent, ...]
    unresolved_sinks: tuple[UnresolvedAssetSink, ...]
    excluded_script_calls: tuple[str, ...]

    def to_manifest(self) -> dict[str, object]:
        """Return the canonical JSON-ready closure record."""
        return {
            "format": MANIFEST_FORMAT,
            "version": MANIFEST_VERSION,
            "scriptFiles": list(self.script_files),
            "soundAliases": list(self.sound_aliases),
            "modelNames": list(self.model_names),
            "shaderNames": list(self.shader_names),
            "effectPaths": list(self.effect_paths),
            "scripts": [script.to_manifest() for script in self.scripts],
            "unresolvedSinks": [
                sink.to_manifest() for sink in self.unresolved_sinks
            ],
            "excludedScriptCalls": list(self.excluded_script_calls),
            "counts": {
                "scripts": len(self.script_files),
                "soundAliases": len(self.sound_aliases),
                "modelNames": len(self.model_names),
                "shaderNames": len(self.shader_names),
                "effectPaths": len(self.effect_paths),
                "unresolvedSinks": len(self.unresolved_sinks),
                "excludedScriptCalls": len(self.excluded_script_calls),
            },
        }


@dataclass(frozen=True)
class _Atom:
    kind: str
    value: str


@dataclass(frozen=True)
class _Expression:
    parts: tuple[_Atom, ...]
    source: str


@dataclass(frozen=True)
class _Function:
    script_path: str
    name: str
    parameters: tuple[str, ...]
    body: str

    @property
    def key(self) -> str:
        return f"{self.script_path.casefold()}::{self.name.casefold()}"

    @property
    def return_symbol(self) -> str:
        return f"@return:{self.key}"


@dataclass(frozen=True)
class _Call:
    path: str
    name: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class _Sink:
    function: _Function
    kind: str
    name: str
    expression: _Expression | None
    source: str


@dataclass(frozen=True)
class _SimpleNode:
    text: str


@dataclass(frozen=True)
class _SequenceNode:
    children: tuple[object, ...]


@dataclass(frozen=True)
class _BranchNode:
    branches: tuple[object, ...]
    include_entry: bool


@dataclass(frozen=True)
class _LoopNode:
    body: object


def strip_gsc_comments(text: str) -> str:
    """Remove comments without interpreting comment markers in strings."""
    output = list(text)
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "string":
            if char == "\\" and following:
                index += 2
                continue
            if char == quote:
                state = "code"
            index += 1
            continue
        if state == "line":
            if char in "\r\n":
                state = "code"
            else:
                output[index] = " "
            index += 1
            continue
        if state == "block":
            if char == "*" and following == "/":
                output[index] = " "
                output[index + 1] = " "
                index += 2
                state = "code"
                continue
            if char not in "\r\n":
                output[index] = " "
            index += 1
            continue
        if char in ('"', "'"):
            state = "string"
            quote = char
            index += 1
            continue
        if char == "/" and following == "/":
            output[index] = " "
            output[index + 1] = " "
            index += 2
            state = "line"
            continue
        if char == "/" and following == "*":
            output[index] = " "
            output[index + 1] = " "
            index += 2
            state = "block"
            continue
        index += 1
    if state == "block":
        raise MultiplayerGscContentError("unterminated GSC block comment")
    return "".join(output)


def _normal_path(value: str, *, description: str) -> str:
    candidate = value.strip().replace("\\", "/")
    parsed = PurePosixPath(candidate)
    if (
        not candidate
        or candidate.startswith("/")
        or parsed.is_absolute()
        or any(part in ("", ".", "..") for part in parsed.parts)
        or any(":" in part for part in parsed.parts)
        or any(ord(char) < 32 for char in candidate)
    ):
        raise MultiplayerGscContentError(
            f"unsafe {description}: {value!r}"
        )
    return parsed.as_posix()


def _normal_source_path(value: str) -> str:
    path = _normal_path(value, description="GSC source path")
    if not path.casefold().endswith(".gsc"):
        raise MultiplayerGscContentError(
            f"multiplayer script is not a GSC file: {value!r}"
        )
    if not path.casefold().startswith("maps/mp/"):
        raise MultiplayerGscContentError(
            f"script is outside maps/mp: {value!r}"
        )
    return path


def _normal_call_path(value: str) -> str:
    path = _normal_path(value, description="GSC call path")
    if not path.casefold().endswith(".gsc"):
        path += ".gsc"
    return path


def _normal_asset(value: str, *, kind: str) -> str:
    normalized = _normal_path(value, description=f"{kind} reference")
    if kind == "model" and normalized.casefold().startswith("xmodel/"):
        normalized = normalized[len("xmodel/") :]
        if not normalized:
            raise MultiplayerGscContentError(
                f"empty xmodel reference: {value!r}"
            )
    if kind == "effect":
        if not normalized.casefold().startswith("fx/"):
            raise MultiplayerGscContentError(
                f"loadFx reference is outside fx/: {value!r}"
            )
        if not normalized.casefold().endswith(".efx"):
            normalized += ".efx"
    return normalized


def _string_mask(text: str) -> list[bool]:
    """Return true for code positions and false inside quoted literals."""
    result = [True] * len(text)
    index = 0
    quote = ""
    while index < len(text):
        if quote:
            result[index] = False
            if text[index] == "\\" and index + 1 < len(text):
                result[index + 1] = False
                index += 2
                continue
            if text[index] == quote:
                quote = ""
            index += 1
            continue
        if text[index] in ('"', "'"):
            quote = text[index]
            result[index] = False
        index += 1
    return result


def _matching_delimiter(
    text: str,
    opening: int,
    opener: str,
    closer: str,
) -> int | None:
    depth = 0
    quote = ""
    index = opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and index + 1 < len(text):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _split_top_level(text: str, delimiter: str) -> tuple[str, ...]:
    result: list[str] = []
    start = 0
    round_depth = 0
    square_depth = 0
    brace_depth = 0
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and index + 1 < len(text):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
        elif char == "(":
            round_depth += 1
        elif char == ")":
            round_depth -= 1
        elif char == "[":
            square_depth += 1
        elif char == "]":
            square_depth -= 1
        elif char == "{":
            brace_depth += 1
        elif char == "}":
            brace_depth -= 1
        elif (
            char == delimiter
            and round_depth == 0
            and square_depth == 0
            and brace_depth == 0
        ):
            result.append(text[start:index].strip())
            start = index + 1
        index += 1
    result.append(text[start:].strip())
    return tuple(result)


def _unwrap_parentheses(text: str) -> str:
    value = text.strip()
    while value.startswith("("):
        closing = _matching_delimiter(value, 0, "(", ")")
        if closing != len(value) - 1:
            break
        value = value[1:-1].strip()
    return value


def _unquote(value: str) -> str | None:
    text = value.strip()
    if len(text) < 2 or text[0] not in ('"', "'") or text[-1] != text[0]:
        return None
    quote = text[0]
    body = text[1:-1]
    output: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            if following in (quote, "\\"):
                output.append(following)
                index += 2
                continue
        output.append(char)
        index += 1
    return "".join(output)


def _normal_symbol(value: str, function: _Function) -> str | None:
    text = value.strip()
    if _SYMBOL.fullmatch(text) is None:
        return None
    compact = re.sub(r"\s+", "", text).casefold()
    if compact.startswith(("game[", "game.", "level[", "level.")):
        return f"@global:{compact}"
    if compact.startswith("self."):
        return f"@self:{compact[len('self.'):]}"
    return f"@local:{function.key}:{compact}"


def _extract_functions(
    script_path: str,
    text: str,
) -> tuple[_Function, ...]:
    functions: list[_Function] = []
    code_mask = _string_mask(text)
    cursor = 0
    while True:
        match = _FUNCTION_HEADER.search(text, cursor)
        if match is None:
            break
        if not code_mask[match.start()]:
            cursor = match.end()
            continue
        # Function declarations are top-level.  Counting braces before the
        # candidate avoids treating control-flow calls as nested declarations.
        depth = 0
        for position, char in enumerate(text[: match.start()]):
            if not code_mask[position]:
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
        if depth != 0:
            cursor = match.end()
            continue
        opening = text.find("{", match.start(), match.end())
        closing = _matching_delimiter(text, opening, "{", "}")
        if closing is None:
            raise MultiplayerGscContentError(
                f"{script_path}: function {match.group('name')} has "
                "an unterminated body"
            )
        parameters: list[str] = []
        raw_parameters = match.group("parameters").strip()
        if raw_parameters:
            for raw in _split_top_level(raw_parameters, ","):
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw) is None:
                    raise MultiplayerGscContentError(
                        f"{script_path}: unsupported parameter {raw!r} "
                        f"in {match.group('name')}"
                    )
                parameters.append(raw.casefold())
        functions.append(
            _Function(
                script_path=script_path,
                name=match.group("name"),
                parameters=tuple(parameters),
                body=text[opening + 1 : closing],
            )
        )
        cursor = closing + 1
    return tuple(functions)


def _scan_calls(body: str) -> tuple[_Call, ...]:
    calls: list[_Call] = []
    code_mask = _string_mask(body)
    cursor = 0
    while True:
        match = _CALL.search(body, cursor)
        if match is None:
            break
        if not code_mask[match.start()]:
            cursor = match.end()
            continue
        opening = body.find("(", match.start(), match.end())
        closing = _matching_delimiter(body, opening, "(", ")")
        if closing is None:
            cursor = match.end()
            continue
        argument_text = body[opening + 1 : closing]
        arguments = (
            ()
            if not argument_text.strip()
            else _split_top_level(argument_text, ",")
        )
        calls.append(
            _Call(
                path=match.group("path") or "",
                name=match.group("name"),
                arguments=arguments,
            )
        )
        # Continue just after the opening token so calls nested in arguments
        # are also available for parameter/return propagation.
        cursor = match.end()
    return tuple(calls)


def _full_call(value: str) -> _Call | None:
    text = _unwrap_parentheses(value)
    match = _CALL.fullmatch(text)
    if match is not None:
        # ``_CALL`` includes only the opening parenthesis, so a fullmatch is
        # not normally possible.  Kept for clarity if the scanner changes.
        return None
    match = _CALL.match(text)
    if match is None:
        return None
    opening = text.find("(", match.start(), match.end())
    closing = _matching_delimiter(text, opening, "(", ")")
    if closing != len(text) - 1:
        return None
    raw_arguments = text[opening + 1 : closing]
    return _Call(
        path=match.group("path") or "",
        name=match.group("name"),
        arguments=(
            ()
            if not raw_arguments.strip()
            else _split_top_level(raw_arguments, ",")
        ),
    )


def _first_code_match(
    pattern: re.Pattern[str],
    text: str,
) -> re.Match[str] | None:
    mask = _string_mask(text)
    return next(
        (
            match
            for match in pattern.finditer(text)
            if match.start() < len(mask) and mask[match.start()]
        ),
        None,
    )


def _word_at(text: str, position: int, word: str) -> bool:
    end = position + len(word)
    return (
        text[position:end].casefold() == word.casefold()
        and (
            position == 0
            or not (
                text[position - 1].isalnum()
                or text[position - 1] == "_"
            )
        )
        and (
            end == len(text)
            or not (text[end].isalnum() or text[end] == "_")
        )
    )


class _BodyParser:
    """Small structural parser used only to preserve assignment flow."""

    def __init__(self, text: str):
        self.text = text
        self.position = 0

    def parse(self, *, stop_at_closing_brace: bool = False) -> _SequenceNode:
        children: list[object] = []
        while self.position < len(self.text):
            self._skip_space()
            if self.position >= len(self.text):
                break
            if (
                stop_at_closing_brace
                and self.text[self.position] == "}"
            ):
                self.position += 1
                break
            node = self._statement()
            if node is not None:
                children.append(node)
        return _SequenceNode(tuple(children))

    def _skip_space(self) -> None:
        while (
            self.position < len(self.text)
            and self.text[self.position].isspace()
        ):
            self.position += 1

    def _condition_end(self) -> int | None:
        self._skip_space()
        if (
            self.position >= len(self.text)
            or self.text[self.position] != "("
        ):
            return None
        return _matching_delimiter(
            self.text,
            self.position,
            "(",
            ")",
        )

    def _statement(self) -> object | None:
        self._skip_space()
        if self.position >= len(self.text):
            return None
        if self.text[self.position] == "{":
            self.position += 1
            return self.parse(stop_at_closing_brace=True)
        if _word_at(self.text, self.position, "if"):
            return self._if_statement()
        if _word_at(self.text, self.position, "switch"):
            return self._switch_statement()
        if (
            _word_at(self.text, self.position, "for")
            or _word_at(self.text, self.position, "while")
        ):
            return self._loop_statement()
        if _word_at(self.text, self.position, "do"):
            return self._do_statement()
        if self.text[self.position] == "}":
            # A malformed/unexpected brace is consumed to guarantee progress.
            self.position += 1
            return None
        return self._simple_statement()

    def _if_statement(self) -> _BranchNode:
        self.position += len("if")
        closing = self._condition_end()
        if closing is None:
            return _BranchNode((self._simple_statement(),), True)
        self.position = closing + 1
        then_node = self._statement() or _SequenceNode(())
        self._skip_space()
        else_node: object | None = None
        if _word_at(self.text, self.position, "else"):
            self.position += len("else")
            else_node = self._statement() or _SequenceNode(())
        return _BranchNode(
            (then_node,) if else_node is None else (then_node, else_node),
            include_entry=else_node is None,
        )

    def _switch_statement(self) -> _BranchNode:
        self.position += len("switch")
        closing = self._condition_end()
        if closing is None:
            return _BranchNode((self._simple_statement(),), True)
        self.position = closing + 1
        self._skip_space()
        if (
            self.position >= len(self.text)
            or self.text[self.position] != "{"
        ):
            return _BranchNode((self._statement() or _SequenceNode(()),), True)
        opening = self.position
        closing_brace = _matching_delimiter(
            self.text,
            opening,
            "{",
            "}",
        )
        if closing_brace is None:
            closing_brace = len(self.text) - 1
        body = self.text[opening + 1 : closing_brace]
        self.position = closing_brace + 1
        ranges = self._switch_case_ranges(body)
        if not ranges:
            return _BranchNode(
                (_BodyParser(body).parse(),),
                include_entry=True,
            )
        branches = tuple(
            _BodyParser(body[start:end]).parse()
            for start, end in ranges
        )
        has_default = bool(
            re.search(r"(?i)(?:^|[;{}])\s*default\s*:", body)
        )
        return _BranchNode(branches, include_entry=not has_default)

    @staticmethod
    def _switch_case_ranges(body: str) -> tuple[tuple[int, int], ...]:
        mask = _string_mask(body)
        labels: list[tuple[int, int]] = []
        depth = 0
        index = 0
        while index < len(body):
            if not mask[index]:
                index += 1
                continue
            char = body[index]
            if char == "{":
                depth += 1
                index += 1
                continue
            if char == "}":
                depth -= 1
                index += 1
                continue
            if depth == 0 and (
                _word_at(body, index, "case")
                or _word_at(body, index, "default")
            ):
                colon = index
                quote = ""
                round_depth = 0
                while colon < len(body):
                    current = body[colon]
                    if quote:
                        if current == "\\" and colon + 1 < len(body):
                            colon += 2
                            continue
                        if current == quote:
                            quote = ""
                    elif current in ('"', "'"):
                        quote = current
                    elif current == "(":
                        round_depth += 1
                    elif current == ")":
                        round_depth -= 1
                    elif current == ":" and round_depth == 0:
                        break
                    colon += 1
                if colon < len(body):
                    labels.append((index, colon + 1))
                    index = colon + 1
                    continue
            index += 1
        return tuple(
            (
                label_end,
                labels[position + 1][0]
                if position + 1 < len(labels)
                else len(body),
            )
            for position, (_label_start, label_end) in enumerate(labels)
        )

    def _loop_statement(self) -> _LoopNode:
        keyword = (
            "for"
            if _word_at(self.text, self.position, "for")
            else "while"
        )
        self.position += len(keyword)
        closing = self._condition_end()
        if closing is not None:
            self.position = closing + 1
        return _LoopNode(self._statement() or _SequenceNode(()))

    def _do_statement(self) -> _LoopNode:
        self.position += len("do")
        body = self._statement() or _SequenceNode(())
        self._skip_space()
        if _word_at(self.text, self.position, "while"):
            self.position += len("while")
            closing = self._condition_end()
            if closing is not None:
                self.position = closing + 1
            self._skip_space()
            if (
                self.position < len(self.text)
                and self.text[self.position] == ";"
            ):
                self.position += 1
        return _LoopNode(body)

    def _simple_statement(self) -> _SimpleNode:
        start = self.position
        round_depth = 0
        square_depth = 0
        quote = ""
        while self.position < len(self.text):
            char = self.text[self.position]
            if quote:
                if char == "\\" and self.position + 1 < len(self.text):
                    self.position += 2
                    continue
                if char == quote:
                    quote = ""
                self.position += 1
                continue
            if char in ('"', "'"):
                quote = char
            elif char == "(":
                round_depth += 1
            elif char == ")":
                round_depth -= 1
            elif char == "[":
                square_depth += 1
            elif char == "]":
                square_depth -= 1
            elif (
                char == ";"
                and round_depth == 0
                and square_depth == 0
            ):
                self.position += 1
                return _SimpleNode(self.text[start:self.position])
            elif (
                char in "{}"
                and round_depth == 0
                and square_depth == 0
            ):
                if self.position == start:
                    self.position += 1
                return _SimpleNode(self.text[start:self.position])
            self.position += 1
        return _SimpleNode(self.text[start:self.position])


class _Analyzer:
    def __init__(self, sources: Mapping[str, str]):
        self.sources = dict(sources)
        self.functions = tuple(
            function
            for path in sorted(self.sources, key=str.casefold)
            for function in _extract_functions(
                path,
                strip_gsc_comments(self.sources[path]),
            )
        )
        self.by_key = {function.key: function for function in self.functions}
        self.excluded_calls: set[str] = set()

    def _target(
        self,
        caller: _Function,
        call: _Call,
    ) -> _Function | None:
        if call.path:
            path = _normal_call_path(call.path)
            if not path.casefold().startswith("maps/mp/"):
                self.excluded_calls.add(path)
                return None
        else:
            path = caller.script_path
        return self.by_key.get(
            f"{path.casefold()}::{call.name.casefold()}"
        )

    def _expression(
        self,
        source: str,
        function: _Function,
    ) -> _Expression | None:
        text = _unwrap_parentheses(source)
        if not text:
            return None
        pieces = _split_top_level(text, "+")
        if len(pieces) > 1:
            parts: list[_Atom] = []
            for piece in pieces:
                expression = self._expression(piece, function)
                if expression is None:
                    return None
                parts.extend(expression.parts)
            return _Expression(tuple(parts), source.strip())
        literal = _unquote(text)
        if literal is not None:
            return _Expression((_Atom("literal", literal),), source.strip())
        symbol = _normal_symbol(text, function)
        if symbol is not None:
            return _Expression((_Atom("symbol", symbol),), source.strip())
        call = _full_call(text)
        if call is not None:
            target = self._target(function, call)
            if target is not None:
                return _Expression(
                    (_Atom("symbol", target.return_symbol),),
                    source.strip(),
                )
        return None

    def _parameter_symbol(
        self,
        function: _Function,
        parameter: str,
    ) -> str:
        result = _normal_symbol(parameter, function)
        assert result is not None
        return result

    def analyze(
        self,
        *,
        strict_unresolved: bool,
    ) -> MultiplayerGscContentClosure:
        equations: list[tuple[str, _Expression]] = []
        sinks: list[_Sink] = []
        definitions: dict[str, list[str]] = {}
        parameter_inputs: dict[tuple[str, str], str] = {}
        definition_counter = 0

        def symbol_expression(symbol: str) -> _Expression:
            return _Expression((_Atom("symbol", symbol),), symbol)

        def add_shared_definition(
            destination: str,
            expression: _Expression,
        ) -> None:
            """Union one global/self/return definition without recursion."""
            nonlocal definition_counter
            prior = tuple(definitions.get(destination, ()))
            rewritten_parts: list[_Atom] = []
            for atom in expression.parts:
                if atom.kind != "symbol" or atom.value != destination:
                    rewritten_parts.append(atom)
                    continue
                snapshot = (
                    f"{destination}#snapshot:{definition_counter}"
                )
                for previous in prior:
                    equations.append(
                        (snapshot, symbol_expression(previous))
                    )
                rewritten_parts.append(_Atom("symbol", snapshot))
            rewritten = _Expression(
                tuple(rewritten_parts),
                expression.source,
            )
            version = f"{destination}#definition:{definition_counter}"
            definition_counter += 1
            equations.append((version, rewritten))
            equations.append((destination, symbol_expression(version)))
            definitions.setdefault(destination, []).append(version)

        def bind_local_state(
            expression: _Expression | None,
            state: Mapping[str, tuple[str, ...]],
        ) -> _Expression | None:
            """Bind local reads to the reaching definitions at this point."""
            nonlocal definition_counter
            if expression is None:
                return None
            parts: list[_Atom] = []
            for atom in expression.parts:
                if atom.kind != "symbol" or not atom.value.startswith(
                    "@local:"
                ):
                    parts.append(atom)
                    continue
                reaching = state.get(atom.value, ())
                if len(reaching) == 1:
                    parts.append(_Atom("symbol", reaching[0]))
                    continue
                snapshot = (
                    f"{atom.value}#reaching:{definition_counter}"
                )
                definition_counter += 1
                for version in reaching:
                    equations.append(
                        (snapshot, symbol_expression(version))
                    )
                # With no reaching definition the snapshot intentionally has
                # no equation and therefore remains unresolved.
                parts.append(_Atom("symbol", snapshot))
            return _Expression(tuple(parts), expression.source)

        def assign_local(
            destination: str,
            expression: _Expression,
            state: dict[str, tuple[str, ...]],
        ) -> None:
            nonlocal definition_counter
            version = f"{destination}#definition:{definition_counter}"
            definition_counter += 1
            equations.append((version, expression))
            state[destination] = (version,)

        def merge_states(
            states: tuple[Mapping[str, tuple[str, ...]], ...],
        ) -> dict[str, tuple[str, ...]]:
            result: dict[str, tuple[str, ...]] = {}
            keys = {
                key
                for state in states
                for key in state
            }
            for key in keys:
                result[key] = tuple(
                    sorted(
                        {
                            version
                            for state in states
                            for version in state.get(key, ())
                        }
                    )
                )
            return result

        for function in self.functions:
            for parameter in function.parameters:
                destination = self._parameter_symbol(function, parameter)
                input_symbol = f"{destination}#parameter"
                parameter_inputs[(function.key, parameter)] = input_symbol

        def process_call(
            function: _Function,
            call: _Call,
            state: Mapping[str, tuple[str, ...]],
        ) -> None:
            name = call.name.casefold()
            if call.path:
                # Validate every explicit call path, even native-looking or
                # unresolved calls, before deciding whether to follow it.
                self._target(function, call)
            if name in _ALL_SINKS:
                raw = call.arguments[0] if call.arguments else ""
                kind = (
                    "sound"
                    if name in _SOUND_SINKS
                    else "model"
                    if name in _MODEL_SINKS
                    else "shader"
                    if name in _SHADER_SINKS
                    else "effect"
                )
                sinks.append(
                    _Sink(
                        function=function,
                        kind=kind,
                        name=name,
                        expression=bind_local_state(
                            self._expression(raw, function),
                            state,
                        ),
                        source=raw.strip(),
                    )
                )
                return
            target = self._target(function, call)
            if target is None:
                return
            for parameter, raw_argument in itertools.zip_longest(
                target.parameters,
                call.arguments,
                fillvalue=None,
            ):
                if parameter is None or raw_argument is None:
                    continue
                expression = bind_local_state(
                    self._expression(raw_argument, function),
                    state,
                )
                if expression is not None:
                    equations.append(
                        (
                            parameter_inputs[(target.key, parameter)],
                            expression,
                        )
                    )

        def process_simple(
            function: _Function,
            node: _SimpleNode,
            state: dict[str, tuple[str, ...]],
        ) -> dict[str, tuple[str, ...]]:
            statement = node.text.strip()
            if not statement:
                return state
            entry_state = dict(state)
            for call in _scan_calls(statement):
                process_call(function, call, entry_state)

            return_match = _first_code_match(_RETURN, statement)
            if return_match is not None:
                expression = bind_local_state(
                    self._expression(
                        return_match.group("value"),
                        function,
                    ),
                    entry_state,
                )
                if expression is not None:
                    add_shared_definition(
                        function.return_symbol,
                        expression,
                    )

            assignment = _first_code_match(_ASSIGNMENT, statement)
            if assignment is None:
                return state
            lhs_text = assignment.group("lhs")
            rhs_text = assignment.group("rhs")
            destination = _normal_symbol(lhs_text, function)
            expression = bind_local_state(
                self._expression(rhs_text, function),
                entry_state,
            )
            compact_lhs = re.sub(r"\s+", "", lhs_text).casefold()
            if compact_lhs.endswith(".headicon"):
                sinks.append(
                    _Sink(
                        function=function,
                        kind="shader",
                        name="headicon",
                        expression=expression,
                        source=rhs_text.strip(),
                    )
                )
            if destination is None or expression is None:
                if (
                    destination is not None
                    and destination.startswith("@local:")
                ):
                    # An unsupported/native RHS still kills the previous
                    # reaching local definition.
                    state[destination] = ()
                return state
            if destination.startswith("@local:"):
                assign_local(destination, expression, state)
            else:
                add_shared_definition(destination, expression)
            return state

        def execute(
            function: _Function,
            node: object,
            state: dict[str, tuple[str, ...]],
        ) -> dict[str, tuple[str, ...]]:
            if isinstance(node, _SimpleNode):
                return process_simple(function, node, state)
            if isinstance(node, _SequenceNode):
                current = state
                for child in node.children:
                    current = execute(function, child, current)
                return current
            if isinstance(node, _BranchNode):
                outputs = tuple(
                    execute(function, branch, dict(state))
                    for branch in node.branches
                )
                if node.include_entry:
                    outputs += (dict(state),)
                return merge_states(outputs)
            if isinstance(node, _LoopNode):
                output = execute(function, node.body, dict(state))
                return merge_states((state, output))
            raise MultiplayerGscContentError(
                f"{function.script_path}: unknown GSC flow node "
                f"{type(node).__name__}"
            )

        for function in self.functions:
            initial_state = {
                self._parameter_symbol(function, parameter): (
                    parameter_inputs[(function.key, parameter)],
                )
                for parameter in function.parameters
            }
            flow = _BodyParser(
                strip_gsc_comments(function.body)
            ).parse()
            execute(function, flow, initial_state)

        values: dict[str, set[str]] = {}
        for _pass in range(MAX_ANALYSIS_PASSES):
            changed = False
            for destination, expression in equations:
                resolved = self._evaluate(expression, values)
                if not resolved:
                    continue
                bucket = values.setdefault(destination, set())
                before = len(bucket)
                bucket.update(resolved)
                if len(bucket) > MAX_VALUES_PER_EXPRESSION:
                    raise MultiplayerGscContentError(
                        f"string propagation exceeded "
                        f"{MAX_VALUES_PER_EXPRESSION} values at {destination}"
                    )
                changed |= len(bucket) != before
            if not changed:
                break
        else:
            raise MultiplayerGscContentError(
                f"GSC analysis did not converge in {MAX_ANALYSIS_PASSES} passes"
            )

        per_script: dict[str, dict[str, dict[str, str]]] = {
            path: {
                "sound": {},
                "model": {},
                "shader": {},
                "effect": {},
            }
            for path in self.sources
        }
        unresolved: set[UnresolvedAssetSink] = set()
        for sink in sinks:
            resolved = (
                self._evaluate(sink.expression, values)
                if sink.expression is not None
                else set()
            )
            meaningful = {value for value in resolved if value}
            if not meaningful:
                # A deliberate empty string clears head icons and is not an
                # unresolved asset dependency.
                literal_empty = (
                    sink.expression is not None
                    and sink.expression.parts
                    and all(
                        atom.kind == "literal" and atom.value == ""
                        for atom in sink.expression.parts
                    )
                )
                if sink.source and not literal_empty:
                    unresolved.add(
                        UnresolvedAssetSink(
                            script_path=sink.function.script_path,
                            function_name=sink.function.name,
                            sink=sink.name,
                            expression=sink.source,
                        )
                    )
                continue
            bucket = per_script[sink.function.script_path][sink.kind]
            for value in meaningful:
                normalized = _normal_asset(value, kind=sink.kind)
                bucket.setdefault(normalized.casefold(), normalized)

        ordered_unresolved = tuple(
            sorted(
                unresolved,
                key=lambda item: (
                    item.script_path.casefold(),
                    item.function_name.casefold(),
                    item.sink.casefold(),
                    item.expression.casefold(),
                ),
            )
        )
        if strict_unresolved and ordered_unresolved:
            first = ordered_unresolved[0]
            raise MultiplayerGscContentError(
                f"unresolved {first.sink} expression in "
                f"{first.script_path}::{first.function_name}: "
                f"{first.expression}"
            )

        script_records: list[MultiplayerGscScriptContent] = []
        global_values: dict[str, dict[str, str]] = {
            "sound": {},
            "model": {},
            "shader": {},
            "effect": {},
        }
        for path in sorted(self.sources, key=str.casefold):
            assets = per_script[path]
            for kind, bucket in assets.items():
                for key, value in bucket.items():
                    global_values[kind].setdefault(key, value)
            script_records.append(
                MultiplayerGscScriptContent(
                    source_path=path,
                    sound_aliases=_ordered(assets["sound"]),
                    model_names=_ordered(assets["model"]),
                    shader_names=_ordered(assets["shader"]),
                    effect_paths=_ordered(assets["effect"]),
                )
            )
        return MultiplayerGscContentClosure(
            script_files=tuple(
                sorted(self.sources, key=str.casefold)
            ),
            sound_aliases=_ordered(global_values["sound"]),
            model_names=_ordered(global_values["model"]),
            shader_names=_ordered(global_values["shader"]),
            effect_paths=_ordered(global_values["effect"]),
            scripts=tuple(script_records),
            unresolved_sinks=ordered_unresolved,
            excluded_script_calls=tuple(
                sorted(self.excluded_calls, key=str.casefold)
            ),
        )

    @staticmethod
    def _evaluate(
        expression: _Expression | None,
        values: Mapping[str, set[str]],
    ) -> set[str]:
        if expression is None:
            return set()
        groups: list[tuple[str, ...]] = []
        combinations = 1
        for atom in expression.parts:
            current = (
                (atom.value,)
                if atom.kind == "literal"
                else tuple(sorted(values.get(atom.value, ())))
            )
            if not current:
                return set()
            combinations *= len(current)
            if combinations > MAX_VALUES_PER_EXPRESSION:
                raise MultiplayerGscContentError(
                    f"expression exceeds {MAX_VALUES_PER_EXPRESSION} "
                    f"string combinations: {expression.source}"
                )
            groups.append(current)
        return {"".join(parts) for parts in itertools.product(*groups)}


def _ordered(values: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(values[key] for key in sorted(values))


def analyze_multiplayer_gsc_sources(
    sources: Mapping[str, str | bytes],
    *,
    strict_unresolved: bool = False,
    selected_maps: Iterable[str] | None = None,
) -> MultiplayerGscContentClosure:
    """Analyze an explicit source mapping.

    Safe files outside ``maps/mp`` are ignored.  A path which appears to be
    under ``maps/mp`` but escapes it through ``..`` is rejected.
    """
    selected: dict[str, str] = {}
    map_filter = (
        {
            str(map_id).strip().casefold()
            for map_id in selected_maps
            if str(map_id).strip()
        }
        if selected_maps is not None
        else None
    )
    folded_paths: set[str] = set()
    for raw_path, payload in sorted(
        sources.items(),
        key=lambda item: str(item[0]).casefold(),
    ):
        candidate = str(raw_path).strip().replace("\\", "/")
        folded_candidate = candidate.casefold()
        if not folded_candidate.startswith("maps/mp/"):
            continue
        if (
            map_filter is not None
            and not is_selected_multiplayer_script(
                candidate,
                map_filter,
            )
        ):
            continue
        path = _normal_source_path(candidate)
        key = path.casefold()
        if key in folded_paths:
            raise MultiplayerGscContentError(
                f"duplicate layered multiplayer script: {path}"
            )
        folded_paths.add(key)
        selected[path] = (
            payload.decode("latin1", "replace")
            if isinstance(payload, bytes)
            else str(payload)
        )
    return _Analyzer(selected).analyze(
        strict_unresolved=strict_unresolved
    )


def analyze_multiplayer_gsc(
    index,
    *,
    strict_unresolved: bool = False,
    selected_maps: Iterable[str] | None = None,
) -> MultiplayerGscContentClosure:
    """Analyze active multiplayer GSC members in a layered archive index.

    When ``selected_maps`` is supplied, shared/gametype scripts remain active
    while top-level scripts owned by other maps are excluded.
    """
    members = index.members if hasattr(index, "members") else index
    sources: dict[str, bytes] = {}
    map_filter = (
        {
            str(map_id).strip().casefold()
            for map_id in selected_maps
            if str(map_id).strip()
        }
        if selected_maps is not None
        else None
    )
    for key, reference in sorted(
        members.items(),
        key=lambda item: str(item[0]).casefold(),
    ):
        candidate = str(getattr(reference, "name", key)).replace("\\", "/")
        if (
            not candidate.casefold().startswith("maps/mp/")
            or not candidate.casefold().endswith(".gsc")
        ):
            continue
        if (
            map_filter is not None
            and not is_selected_multiplayer_script(
                candidate,
                map_filter,
            )
        ):
            continue
        if hasattr(index, "read_ref"):
            payload = index.read_ref(reference)
        else:
            payload = reference
        if not isinstance(payload, bytes):
            raise MultiplayerGscContentError(
                f"{candidate}: archive index did not return bytes"
            )
        sources[candidate] = payload
    return analyze_multiplayer_gsc_sources(
        sources,
        strict_unresolved=strict_unresolved,
        selected_maps=map_filter,
    )
