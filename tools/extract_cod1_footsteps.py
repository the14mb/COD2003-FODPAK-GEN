#!/usr/bin/env python3
"""Extract and document the original Call of Duty 1 footstep system."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import shutil
import wave
import zipfile
from collections import defaultdict
from pathlib import Path

from cod1_archive_policy import (
    COD1_TIER,
    archive_record,
    archive_relative_path,
    official_archives,
    sha256_bytes,
)

ALIAS_COLUMNS = (
    "name",
    "sequence",
    "file",
    "vol_min",
    "vol_max",
    "pitch_min",
    "pitch_max",
    "dist_min",
    "dist_max",
    "channel",
    "type",
    "probability",
    "loop",
    "masterslave",
    "loadspec",
    "subtitle",
)


def clip_family(filename: str) -> str:
    stem = Path(filename).stem.lower()
    if stem.startswith("foot_"):
        return re.sub(r"\d+$", "", stem[5:])
    if stem.startswith("water_wade"):
        return "water"
    if stem.startswith("crawl"):
        return "prone"
    if stem.startswith("land_"):
        return "landing"
    return "misc"


def as_number(value: str) -> float | int | None:
    if not value:
        return None
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def read_alias_rows(raw_csv: bytes) -> list[dict[str, object]]:
    text = raw_csv.decode("utf-8-sig", errors="replace")
    rows: list[dict[str, object]] = []
    for fields in csv.reader(io.StringIO(text)):
        fields += [""] * (len(ALIAS_COLUMNS) - len(fields))
        name = fields[0].strip()
        lower = name.lower()
        if not (
            lower.startswith("step_run_")
            or lower.startswith("step_walk_")
            or lower.startswith("step_prone_")
            or lower.startswith("step_scrape_")
            or lower.startswith("land_")
        ):
            continue
        row: dict[str, object] = {
            column: fields[index].strip()
            for index, column in enumerate(ALIAS_COLUMNS)
        }
        for column in (
            "sequence",
            "vol_min",
            "vol_max",
            "pitch_min",
            "pitch_max",
            "dist_min",
            "dist_max",
            "probability",
        ):
            row[column] = as_number(str(row[column]))
        rows.append(row)
    return rows


def route_parts(alias: str) -> tuple[str, str]:
    lower = alias.lower()
    for prefix, movement in (
        ("step_run_", "run"),
        ("step_walk_", "walk"),
        ("step_prone_", "prone"),
        ("step_scrape_", "scrape"),
        ("land_", "land"),
    ):
        if lower.startswith(prefix):
            return movement, lower[len(prefix):]
    raise ValueError(alias)


def inspect_wav(path: Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        details = {
            "channels": audio.getnchannels(),
            "sample_width_bits": audio.getsampwidth() * 8,
            "sample_rate_hz": rate,
            "frames": frames,
            "duration_seconds": round(frames / rate, 6),
            "compression": audio.getcomptype(),
        }
    details["bytes"] = path.stat().st_size
    details["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return details


def find_archive(main_dir: Path, preferred: str, member_prefix: str) -> Path:
    game_root = main_dir.parent if main_dir.name.casefold() == "main" else main_dir
    archives = official_archives(game_root, COD1_TIER)
    candidate = next(
        (
            path for path in archives
            if path.name.casefold() == preferred.casefold()
        ),
        None,
    )
    if candidate is not None:
        return candidate
    for archive_path in archives:
        with zipfile.ZipFile(archive_path) as archive:
            if any(name.lower().startswith(member_prefix)
                   for name in archive.namelist()):
                return archive_path
    raise FileNotFoundError(
        f"no pk3 under {main_dir} contains {member_prefix}")


def extract(main_dir: Path, output: Path, pak_mode: bool = False) -> None:
    sound_archive = find_archive(main_dir, "pak2.pk3", "sound/footsteps/")
    alias_archive = find_archive(main_dir, "pak1.pk3", "soundaliases/iw_sound.csv")

    clips_dir = output / "clips"
    references_dir = output / "references"
    clips_dir.mkdir(parents=True, exist_ok=True)
    references_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(alias_archive) as archive:
        alias_member = next(
            name for name in archive.namelist()
            if name.lower() == "soundaliases/iw_sound.csv")
        raw_aliases = archive.read(alias_member)
    (references_dir / "iw_sound.csv").write_bytes(raw_aliases)
    alias_rows = read_alias_rows(raw_aliases)

    extracted: dict[str, dict[str, object]] = {}
    with zipfile.ZipFile(sound_archive) as archive:
        members = sorted(
            name for name in archive.namelist()
            if name.lower().startswith("sound/footsteps/")
            and name.lower().endswith(".wav")
        )
        for member in members:
            filename = Path(member).name
            family = clip_family(filename)
            destination = clips_dir / family / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            relative = destination.relative_to(output).as_posix()
            extracted[filename.lower()] = {
                "path": relative,
                "source_archive": archive_relative_path(sound_archive),
                "source_member": member,
                "family": family,
                **inspect_wav(destination),
            }

    routes: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list))
    missing: set[str] = set()
    for row in alias_rows:
        movement, surface = route_parts(str(row["name"]))
        filename = Path(str(row["file"])).name.lower()
        record = dict(row)
        if filename in extracted:
            record["clip"] = extracted[filename]["path"]
        elif filename and filename != "null.wav":
            missing.add(str(row["file"]))
        routes[surface][movement].append(record)

    alias_reference = references_dir / "iw_sound_footstep_aliases.csv"
    with alias_reference.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=ALIAS_COLUMNS)
        writer.writeheader()
        writer.writerows(
            {key: row.get(key, "") for key in ALIAS_COLUMNS}
            for row in alias_rows)

    durations = [
        float(details["duration_seconds"]) for details in extracted.values()]
    manifest = {
        "format": "Friends of Duty CoD1 Footsteps v1",
        "source": {
            "game": "Call of Duty (2003)",
            "audio_archive": archive_record(sound_archive),
            "alias_archive": archive_record(alias_archive),
            "alias_member": "soundaliases/iw_sound.csv",
            "alias_member_sha256": sha256_bytes(raw_aliases),
        },
        "summary": {
            "clip_count": len(extracted),
            "alias_row_count": len(alias_rows),
            "surface_count": len(routes),
            "total_duration_seconds": round(sum(durations), 3),
            "missing_referenced_files": sorted(missing),
        },
        "clips": dict(sorted(extracted.items())),
        "surface_routes": {
            surface: dict(sorted(movements.items()))
            for surface, movements in sorted(routes.items())
        },
    }
    (output / "footstep_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "README.md").write_text(
        "# Original Call of Duty footstep assets\n\n"
        "Extracted losslessly from the user's Call of Duty installation. "
        "The WAV files are unchanged. `footstep_manifest.json` reconstructs "
        "the shipped surface/movement alias routing and audio parameters. "
        "`references/iw_sound.csv` is the complete original alias source; "
        "`references/iw_sound_footstep_aliases.csv` contains only footsteps, "
        "prone movement, scrapes, and landing aliases.\n",
        encoding="utf-8")

    print(
        f"Extracted {len(extracted)} WAV files, {len(alias_rows)} alias rows, "
        f"and {len(routes)} surfaces to {output}")
    print(
        f"Duration: {sum(durations):.3f}s; missing referenced files: "
        f"{len(missing)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("main_dir", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--pak-root", type=Path,
                        help="fodpak content root; clips land in "
                             "audio/footsteps/clips/<family>/ with the "
                             "reference manifest beside them")
    args = parser.parse_args()
    if (args.output is None) == (args.pak_root is None):
        parser.error("expected exactly one of OUTPUT or --pak-root")
    if args.pak_root:
        extract(args.main_dir, args.pak_root / "audio" / "footsteps", pak_mode=True)
    else:
        extract(args.main_dir, args.output)


if __name__ == "__main__":
    main()
