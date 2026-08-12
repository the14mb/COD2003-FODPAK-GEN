#!/usr/bin/env python3
"""Extract the CoD1 in-game HUD art (compass, health bar, text backing, and
the kill-feed weapon icons) from the original PK3 archives into a fodpak
content tree (Docs/CONTENT_PIPELINE.md §1.2 ui/hud).

Per-file provenance, verified against the retail CoD1+UO install on 2026-07-25:

  ui/hud/compassback.png      <- Main/pak5.pk3 gfx/hud/hud@compassback.tga (256px)
  ui/hud/compassface.png      <- Main/pak5.pk3 gfx/hud/hud@compassface.tga (256px)
  ui/hud/compass_arrow.png    <- Main/pak5.pk3 gfx/hud/hud@compass_arrow.tga (64px)
  ui/hud/compasshighlight.png <- Main/pak5.pk3 gfx/hud/hud@compasshighlight.tga
                                 (256px; optional — warn-only when absent)
  ui/hud/health_back.png      <- Main/pak5.pk3 gfx/hud/hud@health_back.dds (128x8)
  ui/hud/health_bar.png       <- Main/pak5.pk3 gfx/hud/hud@health_bar.dds (128x8)
  ui/hud/health_cross.png     <- Main/pak5.pk3 gfx/hud/hud@health_cross.dds (16x16)
  ui/hud/textback.png         <- Main/pak5.pk3 gfx/hud/hud@weaponnameback.dds,
                                 falling back to Main/pak0.pk3 ui/assets/BLACKGRAD.tga
  ui/hud/death/<suffix>.png   <- every gfx/hud/hud@death_<suffix> found, suffix
                                 lowercased (Main/pak5.pk3 ships 25 icons,
                                 Main/pak8.pk3 adds hud@death_antitank)
  ui/hud/stance_stand.png     <- Main/pak5.pk3 gfx/hud/stance_stand.dds (64px)
  ui/hud/stance_crouch.png    <- Main/pak5.pk3 gfx/hud/stance_crouch.dds (64px)
  ui/hud/stance_prone.png     <- Main/pak5.pk3 gfx/hud/stance_prone.dds (64px)
                                 (UO re-ships recompressed stance_* variants in
                                 pakuo00.pk3; Main is preferred — this is the
                                 OG CoD1 triangle-with-soldier-silhouette art)
  ui/hud/ammo_bullet.png      <- Main/pak5.pk3 gfx/icons/hud@ammo2.dds (64px;
                                 a strip of five fanned rifle rounds, NOT a
                                 single bullet — hud@ammo5/hud@ammo9 are
                                 pixel-identical copies; keep the strip whole)
  ui/hud/ammo_back.png        <- Main/pak5.pk3 gfx/hud/hud@ammocounterback.dds
                                 (128x64 rounded ammo-box backdrop)
  ui/hud/grenade_frag.png     <- Main/pak5.pk3 gfx/icons/hud@us_grenade.dds
                                 (64px Mk2 pineapple; weapons/*/fraggrenade
                                 name it as the HUD ammo icon)
  ui/hud/scope/ge.png         <- uo/pakuo00.pk3 gfx/reticle/scope@scope_overlay_ge.dds
  ui/hud/scope/rs.png         <- uo/pakuo00.pk3 gfx/reticle/scope@scope_overlay_rs.dds
  ui/hud/scope/us.png         <- uo/pakuo00.pk3 gfx/reticle/scope@scope_overlay_us.dds
                                 (the magnified-optic frames the scoped
                                 rifle records name as adsOverlayShader,
                                 authored 520x400; straight-alpha surround
                                 with a circular aperture and the reticle
                                 BAKED IN, so no crosshair is drawn over one)
  ui/hud/scope/fieldglasses.png <- uo/pakuo00.pk3 gfx/reticle/scope@fieldglasses_overlay.dds
                                 (binoculars; warn-only until they are
                                 wired to the artillery spotter)
  ui/hud/reticle_mg42.png     <- Main/pak5.pk3 gfx/reticle/mg42_cross.tga
                                 (32px cross; weapons/mp/mg42_bipod_*_mp name
                                 it as reticleCenter at reticleCenterSize 32,
                                 drawn on the 640x480 virtual canvas)
  ui/hud/grenade_smoke.png    <- uo/pakuo00.pk3 gfx/icons/hud@us_smokegrenade.dds
                                 (64px; UO-only — warn-only when absent, base
                                 CoD1 has no smoke grenade)

The original menus reference these as .tga while several files actually ship
as .dds, so lookup matches the basename WITHOUT extension ('@' is literal,
case-insensitive) across every Main/*.pk3 and uo/*.pk3 — casefold sort order
matches engine patch order, later archives win (except the stance pieces,
which prefer Main so UO's recompressed copies don't shadow the originals).

Original 640x480 virtual layout, from ui_mp/hud.menu (Main/pak0+pak8) and the
identical SP ui/hud.menu (localized_english_pak0), for the HUD to match:

  stance menuDef        rect 100   434.375  40 40   (CG_PLAYER_STANCE)
  weaponinfo menuDef    rect 0     420.375 640 40, children relative:
    ammocounterback     rect 557.5   1.25   80 40   (abs 557.5,421.625)
    weaponfiremode      rect 537.5  10      20 20   (abs 537.5,430.375)
    ammotex             rect 570    24.25   55 40   (abs 570,444.625,
                                                     CG_PLAYER_AMMO_VALUE)
  The bullets/grenade glyph has no menu rect of its own — the engine
  ownerdraws the weapon's ammo icon inside the ammocounterback box.
"""

from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

from cod1_archive_policy import (
    COD1_TIER,
    UO_TIER,
    official_archives,
)

# (output name under ui/hud/, source basename without extension, required)
CORE_PIECES = (
    ("compassback.png", "hud@compassback", True),
    ("compassface.png", "hud@compassface", True),
    ("compass_arrow.png", "hud@compass_arrow", True),
    ("compasshighlight.png", "hud@compasshighlight", False),
    ("health_back.png", "hud@health_back", True),
    ("health_bar.png", "hud@health_bar", True),
    ("health_cross.png", "hud@health_cross", True),
)

# textback.png sources, first hit wins
TEXTBACK_STEMS = ("hud@weaponnameback", "blackgrad")

# (output name under ui/hud/, candidate source stems tried in order, required)
# All prefer Main archives over uo so UO's recompressed re-ships of the same
# basenames (stance_*) don't shadow the original CoD1 art.
STANCE_AMMO_PIECES = (
    ("stance_stand.png", ("stance_stand",), True),
    ("stance_crouch.png", ("stance_crouch",), True),
    ("stance_prone.png", ("stance_prone",), True),
    ("ammo_bullet.png", ("hud@ammo2", "hud@ammo5", "hud@ammo9"), True),
    ("ammo_back.png", ("hud@ammocounterback",), True),
    ("grenade_frag.png", ("hud@us_grenade",), True),
    # UO-only: base CoD1 ships no smoke grenade, so warn-only.
    ("grenade_smoke.png", ("hud@us_smokegrenade",), False),
    # The mounted MG42's own crosshair. Only Main/pak5.pk3 ships the stem,
    # so prefer_main is a no-op here and there is no tier split.
    ("reticle_mg42.png", ("mg42_cross",), True),
)

# Magnified-optic overlays, one per nation, named directly by the scoped
# rifle records the package ships: kar98k_scoped -> ..._ge,
# mosin_nagant_scoped -> ..._rs, springfield -> ..._us, each authored at
# 520x400. They are full straight-alpha frames -- a near-black surround
# with a circular aperture AND THE RETICLE BAKED IN -- so a weapon that
# has one needs no procedurally drawn crosshair at all.
#
# UO-only (uo/pakuo00.pk3), which is fine: UO is mandatory for the
# exporter. prefer_main is deliberately NOT used here, unlike the stance
# art above, because these have no CoD1 counterpart to protect.
SCOPE_PIECES = (
    ("scope/ge.png", ("scope@scope_overlay_ge",), True),
    ("scope/rs.png", ("scope@scope_overlay_rs",), True),
    ("scope/us.png", ("scope@scope_overlay_us",), True),
    ("scope/fieldglasses.png", ("scope@fieldglasses_overlay",), False),
)

DEATH_STEM_PREFIX = "hud@death_"


class ArchiveIndex:
    def __init__(self, game_root: Path, include_uo: bool = False) -> None:
        self.archives = official_archives(game_root, COD1_TIER)
        if include_uo:
            self.archives += official_archives(game_root, UO_TIER)
        self.by_stem: dict[str, tuple[Path, str]] = {}
        self.main_by_stem: dict[str, tuple[Path, str]] = {}
        for archive_path in self.archives:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.namelist():
                    if member.endswith("/"):
                        continue
                    key = member.replace("\\", "/").lower()
                    self.by_stem[Path(key).stem] = (archive_path, member)
                    if archive_path.parent.name == "Main":
                        self.main_by_stem[Path(key).stem] = (archive_path, member)

    def read(self, stem: str, prefer_main: bool = False) -> tuple[bytes, str] | None:
        source = None
        if prefer_main:
            source = self.main_by_stem.get(stem.lower())
        if source is None:
            source = self.by_stem.get(stem.lower())
        if source is None:
            return None
        archive_path, member = source
        with zipfile.ZipFile(archive_path) as archive:
            return archive.read(member), f"{archive_path.parent.name}/{archive_path.name}:{member}"

    def find_stems(self, prefix: str) -> list[str]:
        return sorted(stem for stem in self.by_stem if stem.startswith(prefix))


def convert_to_png(data: bytes, destination: Path) -> None:
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in ("RGB", "RGBA", "L", "LA"):
            image = image.convert("RGBA")
        image.save(destination, format="PNG")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("game_root", type=Path,
                        help="Call of Duty installation containing Main/ and optionally uo/")
    parser.add_argument("--pak-root", type=Path, required=True,
                        help="fodpak content root (receives ui/hud/)")
    parser.add_argument("--notes-file", type=Path,
                        help="optional file receiving one line per unresolvable asset")
    parser.add_argument(
        "--include-uo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    # United Offensive is mandatory; the flag cannot turn it off.
    args.include_uo = True

    index = ArchiveIndex(
        args.game_root.resolve(),
        include_uo=args.include_uo,
    )
    hud_dir = args.pak_root.resolve() / "ui" / "hud"
    if not args.include_uo:
        (hud_dir / "grenade_smoke.png").unlink(missing_ok=True)

    notes: list[str] = []
    missing_required: list[str] = []
    extracted = 0

    for output_name, stem, required in CORE_PIECES:
        result = index.read(stem)
        if result is None:
            message = f"hud: no pk3 member found for gfx/hud/{stem}.tga/.dds"
            print(f"WARNING {message}")
            notes.append(message)
            if required:
                missing_required.append(stem)
            continue
        data, provenance = result
        convert_to_png(data, hud_dir / output_name)
        extracted += 1
        print(f"ui/hud/{output_name} <- {provenance}")

    for output_name, stems, required in STANCE_AMMO_PIECES:
        for stem in stems:
            result = index.read(stem, prefer_main=True)
            if result is not None:
                break
        if result is None:
            hint = " (UO-only; base CoD1 ships none)" if not required else ""
            message = (f"hud: no pk3 member found for any of "
                       f"{'/'.join(stems)} (ui/hud/{output_name}){hint}")
            print(f"WARNING {message}")
            notes.append(message)
            if required:
                missing_required.append(stems[0])
            continue
        data, provenance = result
        convert_to_png(data, hud_dir / output_name)
        extracted += 1
        print(f"ui/hud/{output_name} <- {provenance}")

    for output_name, stems, required in SCOPE_PIECES:
        result = None
        for stem in stems:
            result = index.read(stem)
            if result is not None:
                break
        if result is None:
            message = (f"hud: no pk3 member found for any of "
                       f"{'/'.join(stems)} (ui/hud/{output_name})")
            print(f"WARNING {message}")
            notes.append(message)
            if required:
                missing_required.append(stems[0])
            continue
        data, provenance = result
        convert_to_png(data, hud_dir / output_name)
        extracted += 1
        print(f"ui/hud/{output_name} <- {provenance}")

    for stem in TEXTBACK_STEMS:
        result = index.read(stem)
        if result is None:
            continue
        if stem != TEXTBACK_STEMS[0]:
            message = ("hud: hud@weaponnameback not found — textback.png uses "
                       "the ui/assets/BLACKGRAD.tga fallback")
            print(f"WARNING {message}")
            notes.append(message)
        data, provenance = result
        convert_to_png(data, hud_dir / "textback.png")
        extracted += 1
        print(f"ui/hud/textback.png <- {provenance}")
        break
    else:
        message = ("hud: neither hud@weaponnameback nor BLACKGRAD found — "
                   "ui/hud/textback.png unavailable")
        print(f"WARNING {message}")
        notes.append(message)

    death_stems = index.find_stems(DEATH_STEM_PREFIX)
    expected_death_files = {
        f"{stem[len(DEATH_STEM_PREFIX):].lower()}.png"
        for stem in death_stems
    }
    death_dir = hud_dir / "death"
    if death_dir.is_dir():
        for stale in death_dir.glob("*.png"):
            if stale.name.casefold() not in expected_death_files:
                stale.unlink()
    for stem in death_stems:
        suffix = stem[len(DEATH_STEM_PREFIX):].lower()
        result = index.read(stem)
        assert result is not None
        data, provenance = result
        convert_to_png(data, hud_dir / "death" / f"{suffix}.png")
        extracted += 1
        print(f"ui/hud/death/{suffix}.png <- {provenance}")
    if not death_stems:
        message = "hud: no hud@death_* kill icons found in any pk3"
        print(f"WARNING {message}")
        notes.append(message)

    if args.notes_file and notes:
        args.notes_file.parent.mkdir(parents=True, exist_ok=True)
        args.notes_file.write_text("\n".join(notes) + "\n", encoding="utf-8")

    print(f"Extracted {extracted} HUD assets "
          f"({len(death_stems)} kill icons, {len(notes)} note(s)).")
    if missing_required:
        raise SystemExit(
            "missing required HUD assets: " + ", ".join(missing_required))


if __name__ == "__main__":
    main()
