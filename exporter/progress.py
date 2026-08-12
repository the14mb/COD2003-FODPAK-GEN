#!/usr/bin/env python3
"""The `@fod v1` progress protocol, and the weighting behind its fraction.

The export is 19 steps, two of which (`maps`, `props`) run for minutes. Ticking
a bar once per step makes it sit still for most of the run, so the fraction is
weighted by each step's relative cost and refined by per-item counts inside
the long steps.

The protocol is line-delimited ASCII on stdout, one line per event, flushed
immediately, and is emitted ONLY when a program rather than a person is
reading — `friends_of_duty_exporter` sets FOD_PROGRESS_PROTOCOL when it is
started with --game-callback. Without it every function here is a no-op and
the console output is byte-identical to what it printed before this module
existed, which is what lets a developer diff exporter output across the
change.

Consumers must tolerate unknown line kinds and unknown fields: this is a
forward-compatible wire format between two independently updated programs (a
shipped game and a shipped exporter), not an internal call.

    @fod v1 prepare phase=download done=143654912 total=365109248 label=...
    @fod v1 begin   step=props index=17 total=19 title=Export map props
    @fod v1 item    step=props done=112 total=239 label=xmodel/carentan_wall_a
    @fod v1 end     step=props status=done secs=12.3
    @fod v1 overall frac=0.83
    @fod v1 done    ok=1 package=/Users/x/.../Content/current

`index` is 0-based, matching ProgressFn's first argument. `total` is the
number of steps ACTUALLY being run, which `--only` shortens — a consumer must
never hardcode 19.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fod_paths

WEIGHTS_FILENAME = "step_weights.json"

#: Used when a step key is absent from the table. Deliberately small: an
#: unknown step is far more likely to be a new cheap one than a new
#: expensive one, and over-weighting it would stall the bar.
DEFAULT_WEIGHT = 2.0

_WEIGHTS: dict[str, float] | None = None


def _weights_path() -> Path:
    return fod_paths.EXPORTER_DIR.parent / "packaging" / WEIGHTS_FILENAME


def weights() -> dict[str, float]:
    """Per-step relative costs for this platform, falling back to `default`.

    A missing or malformed table is not worth failing an export over — the
    bar degrades to uniform steps, which is what it did before.
    """
    global _WEIGHTS
    if _WEIGHTS is not None:
        return _WEIGHTS
    table: dict[str, float] = {}
    try:
        payload = json.loads(_weights_path().read_text(encoding="utf-8"))
        platform_table = payload.get(sys.platform) or payload.get("default")
        table = {
            key: float(value)
            for key, value in (platform_table or {}).items()
            if not key.startswith("_")
        }
    except (OSError, ValueError, TypeError):
        table = {}
    _WEIGHTS = table
    return table


def _emit(kind: str, **fields) -> None:
    if not fod_paths.progress_protocol_enabled():
        return
    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        # Values are ASCII and space-free except for the trailing free-text
        # fields, which the consumer reads to end of line by convention.
        parts.append(f"{key}={value}")
    sys.stdout.write(f"@fod v1 {kind} " + " ".join(parts) + "\n")
    sys.stdout.flush()


class ExportProgress:
    """Turns per-step and per-item events into a weighted overall fraction.

    One instance per pipeline run. Not thread-safe by design: every call site
    is on the pipeline's own thread, and the child processes report through
    stdout rather than by calling in.
    """

    def __init__(self, step_keys: list[str]):
        table = weights()
        self._keys = list(step_keys)
        self._weight = {
            key: table.get(key, DEFAULT_WEIGHT) for key in self._keys}
        self._total = sum(self._weight.values()) or 1.0
        self._completed = 0.0
        self._current: str | None = None
        self._current_fraction = 0.0

    def begin(self, index: int, total: int, key: str, title: str) -> None:
        self._current = key
        self._current_fraction = 0.0
        _emit("begin", step=key, index=index, total=total, title=title)
        self.emit_overall()

    def item(self, key: str, done: int, total: int, label: str = "") -> None:
        """Refines the fraction inside a long step.

        Guarded on the step actually being current: a stray item line from a
        finished child must not drag the bar backwards.
        """
        if total > 0 and key == self._current:
            self._current_fraction = min(1.0, done / total)
        _emit("item", step=key, done=done, total=total, label=label or None)
        self.emit_overall()

    def end(self, key: str, status: str, seconds: float) -> None:
        self._completed += self._weight.get(key, DEFAULT_WEIGHT)
        self._current = None
        self._current_fraction = 0.0
        _emit("end", step=key, status=status, secs=f"{seconds:.1f}")
        self.emit_overall()

    def skipped(self, index: int, total: int, key: str, title: str) -> None:
        """A resumed run credits skipped steps in full, immediately.

        Without this a rerun that skips 18 of 19 steps would show a bar
        crawling from zero, which reads as "it is redoing everything".
        """
        self._completed += self._weight.get(key, DEFAULT_WEIGHT)
        _emit("begin", step=key, index=index, total=total, title=title)
        _emit("end", step=key, status="skipped", secs="0.0")
        self.emit_overall()

    def fraction(self) -> float:
        current = (
            self._weight.get(self._current, DEFAULT_WEIGHT)
            * self._current_fraction
            if self._current else 0.0
        )
        return min(1.0, (self._completed + current) / self._total)

    def emit_overall(self) -> None:
        _emit("overall", frac=f"{self.fraction():.4f}")

    def done(self, ok: bool, package: Path | str | None = None) -> None:
        _emit("done", ok=1 if ok else 0,
              package=str(package) if package else None)


def emit_prepare(phase: str, done: int = 0, total: int = 0,
                 label: str = "") -> None:
    """The one-time Blender acquisition, reported outside the step weighting.

    Kept separate on purpose: the download is bounded by the player's
    connection while the export is bounded by CPU, so folding them into one
    fraction produces a confidently wrong estimate.
    """
    fields = {"phase": phase}
    if total:
        fields["done"] = done
        fields["total"] = total
    if label:
        fields["label"] = label
    _emit("prepare", **fields)
