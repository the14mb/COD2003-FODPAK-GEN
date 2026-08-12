#!/usr/bin/env python3
"""Official Call of Duty 1 multiplayer source policy.

Every Friends of Duty extractor must obtain PK3 paths through this module.
Retail archives are layered in the game's patch order; the official English
localized archives are included because they contain multiplayer team
voiceovers, while low-resolution, crack, and arbitrary mod/map PK3 files are
never considered content sources.

The helpers deliberately keep archive and member hashes separate:

* archive SHA-256 values fingerprint the selected installation for pipeline
  invalidation;
* member SHA-256 values prove the exact bytes copied into a staging/output tree.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


POLICY_FORMAT = "FriendsOfDuty.OfficialArchivePolicy"
# 4: United Offensive became mandatory. Every version-3 manifest describes a
# selectable tier that no longer exists, so bumping this rejects them with a
# clean "unsupported version" instead of letting a base-only provenance file
# through the roster check.
POLICY_VERSION = 4

COD1_TIER = "cod1"
UO_TIER = "uo"

# Retail multiplayer data/patch archives in engine load order. The English
# localized paks are an essential MP source: Main/localized_english_pak1 holds
# sound/Voiceovers/MP and uo/localized_english_pakuo00 holds the expansion MP
# announcer/radio voice sets. Reachability still selects only maps/mp-driven
# members, so admitting these archives does not admit their campaign audio.
# Intentionally excluded: other/unselected locales, *_lowres, crack paks, and
# every unrecognized PK3.
COD1_ARCHIVES = (
    "localized_english_pak0.pk3",
    "localized_english_pak1.pk3",
    "localized_english_pak2.pk3",
    "localized_english_pak3.pk3",
    "pak0.pk3",
    "pak1.pk3",
    "pak2.pk3",
    "pak3.pk3",
    "pak4.pk3",
    "pak5.pk3",
    "pak6.pk3",
    "pak8.pk3",
    "pak9.pk3",
    "paka.pk3",
)
UO_ARCHIVES = (
    "localized_english_pakuo00.pk3",
    "pakuo00.pk3",
    "pakuo01.pk3",
    "pakuo02.pk3",
    "pakuo03.pk3",
    "pakuo04.pk3",
    "pakuo05.pk3",
    "pakuo06.pk3",
)

ARCHIVES_BY_TIER = {
    COD1_TIER: COD1_ARCHIVES,
    UO_TIER: UO_ARCHIVES,
}
DIRECTORY_BY_TIER = {
    COD1_TIER: "Main",
    UO_TIER: "uo",
}


class OfficialArchiveError(RuntimeError):
    """The selected install is absent, partial, or outside the allowlist."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_FILE_HASH_CACHE: dict[tuple[str, int, int], str] = {}


def sha256_file(path: Path) -> str:
    """SHA-256 with an in-process stat-keyed cache.

    A pipeline computes signatures for many steps. Hashing each 100+ MiB retail
    archive once preserves strong invalidation without repeatedly rereading it.
    """
    resolved = path.resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    cached = _FILE_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _FILE_HASH_CACHE[key] = value
    return value


def _tier_directory(game_root: Path, tier: str) -> Path:
    """Locate a tier's directory, matching its name case-insensitively.

    Retail installs are not consistent about case — a Steam/Proton tree can
    carry `Main/` beside `UO/`, and a case-sensitive filesystem (every Linux
    box, so every Steam Deck) then reports United Offensive as absent. That was
    survivable while UO was optional; now that a missing UO aborts the export,
    an exact-case lookup would lock those installs out entirely.
    """
    try:
        directory = DIRECTORY_BY_TIER[tier]
    except KeyError as error:
        raise ValueError(f"unknown content tier: {tier}") from error
    root = game_root.resolve()
    exact = root / directory
    if exact.is_dir():
        return exact
    if root.is_dir():
        wanted = directory.casefold()
        for candidate in sorted(root.iterdir()):
            if candidate.is_dir() and candidate.name.casefold() == wanted:
                return candidate
    return exact


def uo_installation_status(game_root: Path) -> tuple[bool, str]:
    """Report whether United Offensive is usable, and why not when it is not.

    Friends of Duty requires Call of Duty *and* United Offensive: UO supplies
    the smoke grenade, 95 additional player animations and two of the seven
    maps, so a package built without it is not a smaller product, it is a
    broken one. This is the single place that decides that, and it never raises —
    callers use it to fail early with a sentence a player can act on, rather
    than surfacing an archive-policy traceback part-way through an export.
    """
    try:
        archives = official_archives(game_root, UO_TIER, required=False)
    except OfficialArchiveError as error:
        return False, (
            "Call of Duty: United Offensive is installed but incomplete.\n"
            f"{error}\n"
            "Verify or reinstall United Offensive, then run the exporter "
            "again."
        )
    if not archives:
        return False, (
            "Friends of Duty requires both Call of Duty and Call of Duty: "
            "United Offensive.\n"
            "No United Offensive archives were found in "
            f"{_tier_directory(game_root, UO_TIER)}.\n"
            "Install United Offensive into the same folder as Main, then run "
            "the exporter again. No files were written."
        )
    return True, ""


def official_archives(
    game_root: Path,
    tier: str = COD1_TIER,
    *,
    required: bool = True,
) -> list[Path]:
    """Return only allowlisted retail archives in deterministic load order.

    An optional tier may be wholly absent. A partially installed tier fails
    instead of silently producing an internally inconsistent package.
    """
    directory = _tier_directory(game_root, tier)
    names = ARCHIVES_BY_TIER[tier]
    present_by_name = {
        path.name.casefold(): path
        for path in directory.iterdir()
        if path.is_file()
    } if directory.is_dir() else {}
    present = [
        present_by_name[name.casefold()]
        for name in names
        if name.casefold() in present_by_name
    ]
    if not present:
        if required:
            raise OfficialArchiveError(
                f"no official {tier} archives found under {directory}"
            )
        return []
    missing = [
        directory / name
        for name in names
        if name.casefold() not in present_by_name
    ]
    if missing:
        raise OfficialArchiveError(
            f"partial {tier} installation; missing official archive(s): "
            + ", ".join(str(path) for path in missing)
        )
    return present


def has_complete_uo(game_root: Path) -> bool:
    """True only for a complete United Offensive install.

    Delegates to uo_installation_status so a PARTIAL uo/ reports False here
    instead of raising OfficialArchiveError at the caller — several callers
    use this as a plain predicate inside a boolean expression.
    """
    ok, _message = uo_installation_status(game_root)
    return ok


def selected_archives(game_root: Path) -> list[Path]:
    """Every allowlisted retail archive, in load order.

    Both tiers, unconditionally: Friends of Duty requires Call of Duty and
    United Offensive, so there is no selection left to make. UO's archives
    mount after the base ones and legitimately override them (the multiplayer
    WEAPONFILEs, among others), which is why the order matters.
    """
    return (
        official_archives(game_root, COD1_TIER)
        + official_archives(game_root, UO_TIER)
    )


def archive_tier(path: Path) -> str:
    name = path.name.casefold()
    for tier, names in ARCHIVES_BY_TIER.items():
        if name in {item.casefold() for item in names}:
            expected_dir = DIRECTORY_BY_TIER[tier].casefold()
            if path.parent.name.casefold() != expected_dir:
                break
            return tier
    raise OfficialArchiveError(f"archive is not an allowlisted retail source: {path}")


def require_official_archive(path: Path) -> Path:
    resolved = path.resolve()
    archive_tier(resolved)
    if not resolved.is_file():
        raise OfficialArchiveError(f"official archive is missing: {resolved}")
    return resolved


def archive_relative_path(path: Path) -> str:
    tier = archive_tier(path)
    return f"{DIRECTORY_BY_TIER[tier]}/{path.name}"


def archive_record(path: Path) -> dict[str, object]:
    resolved = require_official_archive(path)
    stat = resolved.stat()
    return {
        "tier": archive_tier(resolved),
        "path": archive_relative_path(resolved),
        "bytes": stat.st_size,
        "sha256": sha256_file(resolved),
    }


def source_policy_manifest(game_root: Path) -> dict[str, object]:
    return {
        "format": POLICY_FORMAT,
        "version": POLICY_VERSION,
        "tiers": [COD1_TIER, UO_TIER],
        "archives": [
            archive_record(path) for path in selected_archives(game_root)
        ],
    }


def policy_manifest_fingerprint(payload: dict[str, object]) -> str:
    canonical_payload = {
        key: payload.get(key)
        for key in ("format", "version", "tiers", "archives")
    }
    canonical = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(canonical)


def policy_fingerprint(game_root: Path) -> str:
    return policy_manifest_fingerprint(source_policy_manifest(game_root))


def write_policy_manifest(
    destination: Path,
    game_root: Path,
) -> dict[str, object]:
    payload = source_policy_manifest(game_root)
    # Fingerprint the payload we are about to write rather than rebuilding it
    # from the install: re-deriving it re-reads and re-hashes every archive,
    # and any drift between the two reads would be silently baked in.
    payload["fingerprint"] = policy_manifest_fingerprint(payload)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


@dataclass(frozen=True)
class ArchiveMember:
    archive: Path
    name: str
    size: int
    crc32: int

    @property
    def tier(self) -> str:
        return archive_tier(self.archive)

    @property
    def source(self) -> str:
        return f"{archive_relative_path(self.archive)}:{self.name}"


class LayeredArchiveIndex:
    """Case-insensitive member index with official engine layering."""

    def __init__(self, archives: Iterable[Path]):
        self.archives = [require_official_archive(path) for path in archives]
        self.members: dict[str, ArchiveMember] = {}
        for archive_path in self.archives:
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    normalized = info.filename.replace("\\", "/")
                    self.members[normalized.casefold()] = ArchiveMember(
                        archive=archive_path,
                        name=normalized,
                        size=info.file_size,
                        crc32=info.CRC,
                    )

    def get(self, member: str) -> ArchiveMember | None:
        return self.members.get(member.replace("\\", "/").casefold())

    def read_ref(self, reference: ArchiveMember) -> bytes:
        with zipfile.ZipFile(reference.archive) as archive:
            return archive.read(reference.name)

    def read(self, member: str) -> bytes | None:
        reference = self.get(member)
        return self.read_ref(reference) if reference is not None else None

    def matching(self, prefixes: tuple[str, ...]) -> list[ArchiveMember]:
        folded = tuple(prefix.casefold() for prefix in prefixes)
        return [
            self.members[key]
            for key in sorted(self.members)
            if key.startswith(folded)
        ]

    def provenance(
        self,
        reference: ArchiveMember,
        data: bytes | None = None,
    ) -> dict[str, object]:
        payload = self.read_ref(reference) if data is None else data
        return {
            "tier": reference.tier,
            "archive": archive_relative_path(reference.archive),
            "member": reference.name,
            "bytes": len(payload),
            "crc32": f"{reference.crc32:08x}",
            "sha256": sha256_bytes(payload),
        }


def validate_policy_manifest(payload: dict[str, object]) -> list[str]:
    """Return validation errors without touching the source installation."""
    errors: list[str] = []
    if payload.get("format") != POLICY_FORMAT:
        errors.append("source policy format is missing or unsupported")
    if payload.get("version") != POLICY_VERSION:
        errors.append("source policy version is missing or unsupported")
    tiers = payload.get("tiers")
    if not isinstance(tiers, list) or COD1_TIER not in tiers:
        errors.append("source policy does not declare the base cod1 tier")
        tiers = []
    elif tiers != [COD1_TIER, UO_TIER]:
        # The only legal sequence now. A base-only manifest is a package
        # built before United Offensive became mandatory.
        errors.append(
            "source policy does not declare United Offensive; Friends of "
            "Duty requires Call of Duty + United Offensive"
        )
    allowed_by_tier = {
        tier: {
            f"{DIRECTORY_BY_TIER[tier]}/{name}".casefold()
            for name in names
        }
        for tier, names in ARCHIVES_BY_TIER.items()
    }
    allowed = {
        f"{DIRECTORY_BY_TIER[tier]}/{name}".casefold()
        for tier, names in ARCHIVES_BY_TIER.items()
        for name in names
    }
    archives = payload.get("archives")
    if not isinstance(archives, list) or not archives:
        errors.append("source policy contains no archive records")
        return errors
    seen: set[str] = set()
    for record in archives:
        if not isinstance(record, dict):
            errors.append("source policy contains a malformed archive record")
            continue
        path = str(record.get("path", ""))
        if path.casefold() not in allowed:
            errors.append(f"nonofficial source archive declared: {path or '?'}")
        if path.casefold() in seen:
            errors.append(f"duplicate source archive declared: {path}")
        seen.add(path.casefold())
        tier = record.get("tier")
        if (
            tier not in allowed_by_tier
            or path.casefold() not in allowed_by_tier[tier]
        ):
            errors.append(f"source archive tier/path mismatch: {path or '?'}")
        size = record.get("bytes")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            errors.append(f"source archive has invalid size: {path or '?'}")
        digest = str(record.get("sha256", ""))
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            errors.append(f"source archive has invalid SHA-256: {path or '?'}")
    # The full union of both tiers — a package that declares fewer archives
    # than the product requires is incomplete regardless of what it claims.
    if seen != allowed:
        errors.append("source policy archive roster is incomplete or unexpected")
    fingerprint = payload.get("fingerprint")
    if (
        fingerprint is not None
        and fingerprint != policy_manifest_fingerprint(payload)
    ):
        errors.append("source policy fingerprint does not match its records")
    return errors
