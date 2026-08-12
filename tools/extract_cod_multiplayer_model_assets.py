#!/usr/bin/env python3
"""Stage XModel dependencies reachable from selected official MP maps.

Only stock archives are considered. United Offensive is layered over CoD1,
matching the expansion's runtime lookup rules. Campaign models and their skins
are excluded even though the retail archives share global model roots.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cod1_archive_policy import (
    COD1_TIER,
    POLICY_VERSION,
    UO_TIER,
    LayeredArchiveIndex,
    archive_record,
    official_archives,
    policy_fingerprint,
)
from cod1_multiplayer_closure import (
    MANAGED_ASSET_ROOTS,
    MANAGED_WEAPON_ROOTS,
    build_map_model_closure,
)
from cod1_shipping_maps import selected_shipping_map_ids

ASSET_ROOTS = MANAGED_ASSET_ROOTS + MANAGED_WEAPON_ROOTS


def arguments() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--game-root",
        type=Path,
        default=root / "Call of Duty",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "extracted_assets" / "cod_multiplayer_source",
    )
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--maps",
        nargs="*",
        default=None,
        help="stage props reachable from only these official MP map ids",
    )
    args = parser.parse_args()
    # United Offensive is mandatory; the flag cannot turn it off.
    args.include_uo = True
    return args


def main() -> None:
    args = arguments()
    game_root = args.game_root.resolve()
    archives = official_archives(game_root, COD1_TIER)
    if args.include_uo:
        archives += official_archives(game_root, UO_TIER)
    index = LayeredArchiveIndex(archives)
    selected_maps = selected_shipping_map_ids(
        include_uo=args.include_uo,
        requested_maps=args.maps,
    )
    closure = build_map_model_closure(index, selected_maps)

    args.output.mkdir(parents=True, exist_ok=True)
    provenance: dict[str, dict[str, object]] = {}
    managed: set[str] = set()
    for requested in sorted(closure.paths, key=str.casefold):
        reference = index.get(requested)
        if reference is None:
            raise RuntimeError(
                f"multiplayer map closure member disappeared: {requested}"
            )
        name = reference.name.replace("\\", "/")
        data = index.read_ref(reference)
        destination = args.output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        managed.add(name.casefold())
        provenance[name.casefold()] = index.provenance(reference, data)

    pruned = 0
    for root_name in ASSET_ROOTS:
        root = args.output / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                relative = path.relative_to(args.output).as_posix().casefold()
                if relative not in managed:
                    path.unlink()
                    pruned += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    manifest = {
        "format": "FriendsOfDuty.OfficialMultiplayerModelSource",
        "version": 2,
        "sourcePolicyVersion": POLICY_VERSION,
        "tiers": [COD1_TIER] + ([UO_TIER] if args.include_uo else []),
        "archiveFingerprint": policy_fingerprint(game_root),
        "closure": {
            "format": "FriendsOfDuty.MultiplayerReachabilityClosure",
            "version": 1,
            "kind": "map-models",
            "selectedMaps": list(closure.selected_maps),
            "models": closure.model_count,
            "mountedWeaponFiles": list(
                closure.mounted_weapon_files
            ),
            "mountedWeaponAnimations": list(
                closure.mounted_weapon_animations
            ),
        },
        "archives": [archive_record(archive) for archive in archives],
        "fileCount": len(provenance),
        "provenance": provenance,
    }
    (args.output / "extraction_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"OFFICIAL MP MAP MODEL CLOSURE READY — "
        f"files={len(provenance)}, pruned={pruned}, output={args.output}"
    )


if __name__ == "__main__":
    main()
