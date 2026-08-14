#!/usr/bin/env python3
"""Retail per-surface impact table (fx/*.csv) parsing and merge.

CoD1/UO map bullet/ordnance hits to effects through CSV tables in the pk3
``fx/`` folder. ``Main/pak5.pk3 fx/iw_impacts.csv`` is the base table; its own
header comments document the override model this module reproduces:

* a row is ``impact_type, surface_type, efx_path`` — the surface is one of the
  valid ``surfaceparm`` shader tokens or ``default``;
* any CSV in ``fx/`` with a later name alphabetically overrides earlier rows
  per (impact, surface) key (United Offensive ships ``fx/gmi_impacts.csv``
  with its per-weapon-class ``bullet_<class>_*`` types this way);
* a deliberately blank effect cell means "play no effect" and must be kept
  distinct from an absent row.

Archive layering supplies the third axis: UO ships its OWN ``fx/iw_impacts.csv``
carrying only the grenade/molotov/rocket rows (its bullets moved to the gmi
per-class types), mounted over Main's copy. Same-named files therefore merge in
engine load order — Main's rows first, UO's rows overriding per key — which is
what keeps Main's ``bullet_small_*``/``bullet_large_*`` rows (the types the
Friends of Duty runtime consumes) in the merged table while UO's richer
grenade rows win where both speak.

Import-safe for every exporter environment: stdlib only.
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable

IMPACTS_FORMAT = "FriendsOfDuty.Impacts"
IMPACTS_VERSION = 1

# The impact types whose efx the package ships as a full effect closure
# (fx/efx documents + sprite textures). The runtime presents small/large
# bullet hits through the CoD1-style types; the UO gmi per-class rows
# (bullet_rifle_*, bullet_smg_*, ...) ship as table data only.
BULLET_CLOSURE_IMPACT_TYPES = (
    "bullet_small_normal",
    "bullet_small_reflect",
    "bullet_large_normal",
    "bullet_large_reflect",
)


def parse_impact_table(text: str) -> tuple[tuple[str, str, str], ...] | None:
    """Rows of one impact CSV, or None when the file is not shaped like one.

    Retail is loose about the third column: a blank efx cell may be an empty
    third field or a missing one (``bullet_small_reflect,foliage``), and both
    mean "no effect here" — preserved as "". Comment rows start with ``#``
    (several are quoted whole-line cells followed by empty padding cells).
    A file containing any data row that does not reduce to 2–3 cells with a
    non-empty impact and surface is not an impact table.
    """
    rows: list[tuple[str, str, str]] = []
    for record in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        cells = [cell.strip() for cell in record]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if cells[0].startswith("#"):
            continue
        if len(cells) not in (2, 3) or not cells[0] or not cells[1]:
            return None
        efx = cells[2] if len(cells) == 3 else ""
        rows.append(
            (
                cells[0].lower(),
                cells[1].lower(),
                efx.replace("\\", "/"),
            )
        )
    return tuple(rows) if rows else None


def discover_impact_tables(
    archives: Iterable[Path],
) -> list[tuple[str, Path, tuple[tuple[str, str, str], ...]]]:
    """(filename, archive, rows) for every impact table, in merge order.

    Enumerates CSV members directly under ``fx/`` across the archives (given
    in engine load order), keeps the ones whose rows match the table shape,
    and orders them alphabetically by filename — the retail override rule —
    with same-named files staying in archive load order (the stable sort),
    so a later archive's copy overrides an earlier one's rows per key.
    """
    discovered: list[tuple[str, Path, tuple[tuple[str, str, str], ...]]] = []
    for archive_path in archives:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                normalized = info.filename.replace("\\", "/")
                folded = normalized.casefold()
                if not folded.startswith("fx/") or not folded.endswith(".csv"):
                    continue
                if "/" in folded[len("fx/"):]:
                    continue
                rows = parse_impact_table(
                    archive.read(info).decode("latin1", "replace")
                )
                if rows is None:
                    continue
                discovered.append(
                    (PurePosixPath(normalized).name, archive_path, rows)
                )
    discovered.sort(key=lambda item: item[0].casefold())
    return discovered


def merge_impact_rows(
    tables: Iterable[tuple[tuple[str, str, str], ...]],
) -> tuple[tuple[str, str, str], ...]:
    """Merge tables in the given order, later rows overriding earlier ones
    keyed on (impact, surface). A key keeps its first-seen position, so the
    merged table reads in the source files' own row order."""
    merged: dict[tuple[str, str], tuple[str, str, str]] = {}
    for rows in tables:
        for row in rows:
            merged[(row[0], row[1])] = row
    return tuple(merged.values())


def merged_impact_rows_from_archives(
    archives: Iterable[Path],
) -> tuple[tuple[str, str, str], ...]:
    return merge_impact_rows(
        rows for _name, _archive, rows in discover_impact_tables(archives)
    )


def impacts_manifest_payload(
    rows: Iterable[tuple[str, str, str]],
) -> dict[str, object]:
    """fx/impacts.json payload: ALL merged rows (grenade/rocket/gmi types
    too — the runtime filters), blank efx kept as ""."""
    return {
        "format": IMPACTS_FORMAT,
        "version": IMPACTS_VERSION,
        "rows": [
            {"impact": impact, "surface": surface, "efx": efx}
            for impact, surface, efx in rows
        ],
    }


def surface_vocabulary(
    rows: Iterable[tuple[str, str, str]],
) -> frozenset[str]:
    """Surface tokens the table speaks, minus the ``default`` sentinel —
    ``default`` is the engine's could-not-classify bucket, never a
    ``surfaceparm`` a shader can declare."""
    return frozenset(
        surface for _impact, surface, _efx in rows if surface != "default"
    )


def bullet_closure_efx_paths(
    rows: Iterable[tuple[str, str, str]],
) -> tuple[str, ...]:
    """Distinct efx paths of the non-blank BULLET_CLOSURE_IMPACT_TYPES rows,
    casefold-sorted; these are the effects the package ships in full."""
    selected: dict[str, str] = {}
    for impact, _surface, efx in rows:
        if impact in BULLET_CLOSURE_IMPACT_TYPES and efx:
            selected.setdefault(efx.casefold(), efx)
    return tuple(selected[key] for key in sorted(selected))
