#!/usr/bin/env python3
"""Download the CI-built importer extensions into packaging/importers/.

`make importer-fetch`

Before this existed the only way to get an authored-LOD importer was rustup
plus a platform C++ linker, on every one of the four targets. That is the
prerequisite the single-executable work exists to remove, so the automatic
build-from-source fallback is gone and this replaces it.

Artifacts come from the build workflow at
<https://github.com/anarqz/cod-asset-importer>, which asserts
`XMODEL_LOD_API_VERSION == 1` before publishing anything — so what lands here
is known to be capable, not merely present.

Building locally is still possible for someone changing the Rust:
`python3 exporter/build_importer.py --from-source`.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE = REPO_ROOT / "packaging" / "importers"
IMPORTER_REPO = "anarqz/cod-asset-importer"
WORKFLOW = "build.yml"

#: Same names as the CI matrix and exporter/build_importer.CI_TARGETS.
TARGETS = {
    "linux-x64": "cod_asset_importer.abi3.so",
    "macos-arm64": "cod_asset_importer.abi3.so",
    "macos-x64": "cod_asset_importer.abi3.so",
    "windows-x64": "cod_asset_importer.pyd",
}


def _gh(*arguments: str) -> str:
    if shutil.which("gh") is None:
        raise SystemExit(
            "The GitHub CLI (gh) is required to fetch importer artifacts.\n"
            "  https://cli.github.com — then `gh auth login`.\n"
            "Alternatively download them by hand from\n"
            f"  https://github.com/{IMPORTER_REPO}/actions/workflows/{WORKFLOW}\n"
            f"and unzip each into {CACHE}/<target>/.")
    result = subprocess.run(
        ["gh", *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"gh {' '.join(arguments)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def latest_successful_run() -> str:
    """The newest run with artifacts worth taking.

    Deliberately not `--status success`: the matrix uses fail-fast: false, so
    a run where one target failed still published good artifacts for the
    others, and refusing those would block work for no reason.
    """
    runs = _gh("run", "list", "--repo", IMPORTER_REPO,
               "--workflow", WORKFLOW, "--branch", "master",
               "--limit", "10", "--json", "databaseId,conclusion",
               "--jq", ".[] | select(.conclusion != null) | .databaseId")
    if not runs:
        raise SystemExit(
            f"No completed runs of {WORKFLOW} in {IMPORTER_REPO}.")
    return runs.splitlines()[0]


def fetch(run_id: str, targets: list[str]) -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for target in targets:
        destination = CACHE / target
        destination.mkdir(parents=True, exist_ok=True)
        try:
            _gh("run", "download", run_id, "--repo", IMPORTER_REPO,
                "--name", f"importer-{target}", "--dir", str(destination))
        except SystemExit as error:
            # One missing target is not fatal: a matrix leg can fail while the
            # rest publish, and a developer only needs their own host's
            # artifact to work.
            print(f"  {target}: unavailable ({error})")
            continue
        artifact = destination / TARGETS[target]
        if not artifact.is_file():
            print(f"  {target}: downloaded but {TARGETS[target]} is missing")
            continue
        print(f"  {target}: {artifact.stat().st_size:,} bytes")
        fetched += 1
    return fetched


def install_host_artifact() -> None:
    """Put this machine's artifact where the exporter actually loads it."""
    sys.path.insert(0, str(REPO_ROOT / "exporter"))
    import build_importer

    target = build_importer.ci_target()
    if target is None:
        print("No published importer for this platform; nothing installed.")
        return
    source = CACHE / target / TARGETS[target]
    if not source.is_file():
        print(f"No cached artifact for {target}; nothing installed.")
        return
    addon = (build_importer.DEFAULT_IMPORTER_ROOT / "python"
             / "cod_asset_importer")
    if not addon.is_dir():
        print(f"Importer addon directory missing: {addon}")
        return
    installed = addon / build_importer.expected_artifact()
    shutil.copy2(source, installed)
    version, error = build_importer.artifact_lod_api_version(installed)
    print(f"installed {installed.relative_to(REPO_ROOT)} "
          f"(LOD API {version if version is not None else '?'}"
          f"{'; ' + error if error else ''})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", help="a specific workflow run id")
    parser.add_argument("--target", action="append", choices=sorted(TARGETS),
                        help="fetch only this target (repeatable)")
    parser.add_argument("--no-install", action="store_true",
                        help="populate the cache without installing")
    arguments = parser.parse_args()

    run_id = arguments.run or latest_successful_run()
    targets = arguments.target or sorted(TARGETS)
    print(f"Fetching importer artifacts from {IMPORTER_REPO} run {run_id}")
    fetched = fetch(run_id, targets)
    if fetched == 0:
        raise SystemExit(
            "No artifacts were fetched. Check "
            f"https://github.com/{IMPORTER_REPO}/actions")
    if not arguments.no_install:
        install_host_artifact()
    print(f"{fetched}/{len(targets)} targets in {CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
