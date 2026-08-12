#!/usr/bin/env python3
"""Stage the reachable CoD 1 multiplayer weapon/player asset closure.

Later allowlisted archives override earlier ones, matching engine patch
loading. The selected official localized archives are available for exact MP
voiceover extraction, while arbitrary locale/mod/crack archives, campaign
XModels/XAnims/skins and SP WEAPONFILEs are never admitted to this staging
tree.
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
    MANAGED_EXACT_FILES,
    MANAGED_WEAPON_ROOTS,
    build_weapon_player_closure,
)
from cod1_shipping_maps import selected_shipping_map_ids


GENERIC_ASSET_ROOTS = MANAGED_ASSET_ROOTS
MP_ASSET_ROOTS = MANAGED_WEAPON_ROOTS
MP_EXACT_FILES = MANAGED_EXACT_FILES


def is_multiplayer_source(member: str) -> bool:
    """Whether the path is intrinsically multiplayer-namespaced.

    Global model/animation/skin paths deliberately return false here. Their
    admission requires graph reachability and cannot be decided from a path
    string alone.
    """
    key = member.replace("\\", "/").casefold()
    return key.startswith(MP_ASSET_ROOTS) or key in MP_EXACT_FILES


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "game_root",
        type=Path,
        help="Call of Duty install root (legacy Main/ path is also accepted)",
    )
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--maps",
        nargs="*",
        default=None,
        help=(
            "selected MP map ids, recorded for detached provenance; map "
            "XModels are staged by extract_cod_multiplayer_model_assets.py"
        ),
    )
    args = parser.parse_args()
    # United Offensive is mandatory; the flag cannot turn it off.
    args.include_uo = True

    supplied = args.game_root.resolve()
    game_root = supplied.parent if supplied.name.casefold() == "main" else supplied
    archives = official_archives(game_root, COD1_TIER)
    if args.include_uo:
        archives += official_archives(game_root, UO_TIER)
    index = LayeredArchiveIndex(archives)
    selected_maps = selected_shipping_map_ids(
        include_uo=args.include_uo,
        requested_maps=args.maps,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    closure = build_weapon_player_closure(
        index,
        include_uo=args.include_uo,
    )

    provenance: dict[str, dict[str, object]] = {}
    managed: set[str] = set()
    for requested in sorted(closure.paths, key=str.casefold):
        reference = index.get(requested)
        if reference is None:
            raise RuntimeError(
                f"multiplayer closure member disappeared: {requested}"
            )
        data = index.read_ref(reference)
        destination = args.output_dir / reference.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        normalized = reference.name.replace("\\", "/")
        managed.add(normalized.casefold())
        provenance[normalized] = index.provenance(reference, data)

    # A previous exporter could have admitted mod/SP/UO files. Prune only the
    # roots this extractor owns so stale, untrusted members cannot survive a
    # policy-tightened rerun.
    pruned = 0
    for root_name in GENERIC_ASSET_ROOTS + MP_ASSET_ROOTS:
        root = args.output_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                relative = path.relative_to(args.output_dir).as_posix().casefold()
                if relative not in managed:
                    path.unlink()
                    pruned += 1
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()

    manifest = {
        "format": "FriendsOfDuty.OfficialMultiplayerSource",
        "version": 2,
        "sourcePolicyVersion": POLICY_VERSION,
        "tiers": [COD1_TIER] + ([UO_TIER] if args.include_uo else []),
        "archiveFingerprint": policy_fingerprint(game_root),
        "closure": {
            "format": "FriendsOfDuty.MultiplayerReachabilityClosure",
            "version": 1,
            "kind": "weapon-player",
            "requestedMaps": list(selected_maps),
            "weaponFiles": list(closure.weapon_files),
            "playerAnimations": closure.player_animation_count,
            "expandedTurretAliases": list(
                closure.expanded_turret_aliases
            ),
            "excludedTurretAnimations": list(
                closure.excluded_turret_animations
            ),
            "models": closure.model_count,
        },
        "archives": [archive_record(path) for path in archives],
        "fileCount": len(provenance),
        "provenance": provenance,
    }
    (args.output_dir / "extraction_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {len(provenance)} reachable official multiplayer files to "
        f"{args.output_dir}; pruned {pruned} stale file(s)"
    )


if __name__ == "__main__":
    main()
