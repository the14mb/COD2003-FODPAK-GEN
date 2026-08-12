#!/usr/bin/env python3
"""Regenerate (or verify) packaging/blender_pin.json from blender.org.

Two jobs, one implementation:

  make blender-pin VERSION=4.5.1     rewrite the pin
  make blender-pin-check             assert the pin still matches upstream

The second is the standing guard against the one failure mode a runtime
download introduces that a bundled payload could not have: the pinned archive
moving, being renamed, or being pruned. If that happens, EVERY existing
installation loses the ability to export, not just new ones — so it is worth
discovering on a scheduled CI run rather than from a player's bug report.

Checksums are never computed here. Blender publishes one checksum file per
release listing every artifact (`blender-<version>.sha256`); there are no
per-artifact `.sha256` siblings — asking for one returns 404. Copying the
published digest is the point: a hash we compute ourselves from a file we
just downloaded proves only that the download did not corrupt, which is what
TLS already tells us.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PACKAGING_DIR = Path(__file__).resolve().parent
PIN_PATH = PACKAGING_DIR / "blender_pin.json"

#: blender.org answers a bare urllib User-Agent with 403.
USER_AGENT = "FriendsOfDutyExporter/1.0 (+https://github.com/anarqz)"
TIMEOUT_SECONDS = 60

#: The artifact each target uses, and where its executable lands once
#: unpacked. Keep in step with blender_provisioner._extract.
TARGETS = {
    "windows-x64": {
        "suffix": "windows-x64.zip",
        "form": "zip",
        "inner_dir": "blender-{version}-windows-x64",
        "executable": "blender.exe",
    },
    "macos-arm64": {
        "suffix": "macos-arm64.dmg",
        "form": "dmg",
        "inner_dir": "Blender.app",
        "executable": "Blender.app/Contents/MacOS/Blender",
    },
    "macos-x64": {
        "suffix": "macos-x64.dmg",
        "form": "dmg",
        "inner_dir": "Blender.app",
        "executable": "Blender.app/Contents/MacOS/Blender",
    },
    "linux-x64": {
        "suffix": "linux-x64.tar.xz",
        "form": "tar.xz",
        "inner_dir": "blender-{version}-linux-x64",
        "executable": "blender",
    },
}


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return response.read()


def _content_length(url: str) -> int:
    """Byte size, used for the download progress bar's denominator.

    A HEAD is enough and avoids pulling ~350 MB just to count it.
    """
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        length = response.headers.get("Content-Length")
    if not length:
        raise RuntimeError(f"no Content-Length for {url}")
    return int(length)


def release_dir(version: str) -> str:
    major_minor = ".".join(version.split(".")[:2])
    return f"https://download.blender.org/release/Blender{major_minor}/"


def published_checksums(version: str) -> dict[str, str]:
    """Parse `blender-<version>.sha256` into {filename: digest}."""
    url = f"{release_dir(version)}blender-{version}.sha256"
    text = _get(url).decode("utf-8", errors="replace")
    digests: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"([0-9a-f]{64})\s+\*?(\S+)", line.strip())
        if match:
            digests[match.group(2)] = match.group(1)
    if not digests:
        raise RuntimeError(f"no checksums parsed from {url}")
    return digests


def build_pin(version: str, gltf_generator: str) -> dict:
    digests = published_checksums(version)
    base = release_dir(version)
    targets = {}
    for name, shape in TARGETS.items():
        archive = f"blender-{version}-{shape['suffix']}"
        if archive not in digests:
            raise RuntimeError(
                f"{archive} is not listed in blender-{version}.sha256")
        url = base + archive
        targets[name] = {
            "archive": archive,
            "url": url,
            "sha256": digests[archive],
            "archive_bytes": _content_length(url),
            "form": shape["form"],
            "inner_dir": shape["inner_dir"].format(version=version),
            "executable": shape["executable"],
        }
    existing = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    return {
        "_comment": existing.get("_comment", []),
        "version": version,
        "version_tuple": [int(part) for part in version.split(".")],
        "release_dir": base,
        "checksum_file": f"{base}blender-{version}.sha256",
        "gltf_generator": gltf_generator,
        "targets": targets,
    }


def check() -> int:
    """Non-zero if the pin no longer matches what blender.org publishes."""
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    version = pin["version"]
    problems: list[str] = []
    try:
        digests = published_checksums(version)
    except (urllib.error.URLError, OSError, RuntimeError) as error:
        print(f"FAIL cannot read the published checksums: {error}")
        return 1
    for name, entry in pin["targets"].items():
        archive = entry["archive"]
        published = digests.get(archive)
        if published is None:
            problems.append(f"{name}: {archive} is no longer published")
            continue
        if published != entry["sha256"]:
            problems.append(
                f"{name}: sha256 changed upstream "
                f"({entry['sha256'][:16]}… -> {published[:16]}…)")
            continue
        try:
            size = _content_length(entry["url"])
        except (urllib.error.URLError, OSError, RuntimeError) as error:
            problems.append(f"{name}: {entry['url']} unreachable ({error})")
            continue
        if size != entry["archive_bytes"]:
            problems.append(
                f"{name}: size changed ({entry['archive_bytes']} -> {size})")
            continue
        print(f"OK   {name}: {archive}")
    if problems:
        print("\n".join(f"FAIL {problem}" for problem in problems))
        print(
            "\nThe pinned Blender is no longer available as pinned. Every "
            "existing installation depends on this URL, so this is not a "
            "cosmetic failure. Bump the pin (and re-run the smoke and "
            "golden-harness passes) rather than silently repointing it.")
        return 1
    print(f"\nBlender pin {version} matches upstream.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", help="Blender version to pin, e.g. 4.5.1")
    parser.add_argument(
        "--gltf-generator",
        default="Khronos glTF Blender I/O v4.5.47",
        help="generator string this build embeds in exported GLBs")
    parser.add_argument("--check", action="store_true",
                        help="verify the existing pin instead of rewriting it")
    arguments = parser.parse_args()

    if arguments.check:
        return check()
    if not arguments.version:
        parser.error("--version is required unless --check is given")
    pin = build_pin(arguments.version, arguments.gltf_generator)
    PIN_PATH.write_text(
        json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PIN_PATH} for Blender {arguments.version}")
    for name, entry in pin["targets"].items():
        print(f"  {name:<13} {entry['archive_bytes']:>12,} B  "
              f"{entry['sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
