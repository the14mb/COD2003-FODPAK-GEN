#!/usr/bin/env python3
"""Render the exporter's brand assets from the Friends of Duty store artwork.

    python3 packaging/make_brand_assets.py --artwork ~/Desktop/friends-of-duty-artwork

Writes into `exporter/assets/`, which build_exporter copies beside the shipped
executable. The store art itself is deliberately NOT committed: it is large,
it belongs to the game rather than to this tool, and only these few derived
files are needed. Re-run this when the branding changes.

Everything here is baked at build time on purpose. The GUI loads these with
Tk's own PhotoImage, which reads PNG but cannot scale or composite alpha
sensibly, so the compositing has to happen now rather than at runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS = REPO_ROOT / "exporter" / "assets"

#: The header fades into this on the right, and the GUI paints the same value
#: behind it, so a window wider than the image extends invisibly.
INK = (11, 13, 15)

#: Windows wants every size in one .ico or it rescales the largest badly in
#: the taskbar and alt-tab.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

HEADER_SIZE = (1400, 120)
#: Where the backdrop starts giving way to flat INK.
FADE_START, FADE_END = 760, 1180


def _load(artwork: Path, name: str) -> Image.Image:
    path = artwork / name
    if not path.is_file():
        raise SystemExit(f"missing source artwork: {path}")
    return Image.open(path).convert("RGBA")


def build_icons(artwork: Path) -> None:
    star = _load(artwork, "icon-transparent.png")
    star.save(ASSETS / "fod.ico", sizes=[(s, s) for s in ICON_SIZES])
    # Tk's iconphoto path, used as the fallback and for non-Windows hosts.
    star.resize((64, 64), Image.LANCZOS).save(ASSETS / "fod_icon.png")
    print(f"fod.ico       {', '.join(str(s) for s in ICON_SIZES)}")
    print("fod_icon.png  64x64")


def build_header(artwork: Path) -> None:
    width, height = HEADER_SIZE
    backdrop = _load(artwork, "store-background.png")

    # Take a wide strip from the middle of the street scene, where the ruins
    # read as texture rather than as identifiable buildings competing with the
    # wordmark, then darken hard so white type stays legible over it.
    strip_height = int(backdrop.width * height / width)
    top = int(backdrop.height * 0.42) - strip_height // 2
    strip = backdrop.crop((0, max(0, top), backdrop.width,
                           max(0, top) + strip_height))
    strip = strip.resize((width, height), Image.LANCZOS)
    strip = Image.blend(strip, Image.new("RGBA", (width, height), INK + (255,)),
                        0.52)

    # Fade the right-hand side to flat INK so the GUI can extend the band with
    # a solid colour at any window width without a visible seam.
    mask = Image.new("L", (width, height))
    columns = []
    for x in range(width):
        if x <= FADE_START:
            columns.append(0)
        elif x >= FADE_END:
            columns.append(255)
        else:
            columns.append(
                int(255 * (x - FADE_START) / (FADE_END - FADE_START)))
    mask.putdata([columns[x] for _ in range(height) for x in range(width)])
    header = Image.composite(
        Image.new("RGBA", (width, height), INK + (255,)), strip, mask)

    # The wordmark, sized to the band and left-aligned with generous margin.
    logo = _load(artwork, "logo-wide-transparent.png")
    logo_height = 84
    logo = logo.resize(
        (int(logo.width * logo_height / logo.height), logo_height),
        Image.LANCZOS)
    header.alpha_composite(logo, (34, (height - logo_height) // 2))

    # A one-pixel steel rule along the bottom, picking up the star's metal.
    for x in range(width):
        header.putpixel((x, height - 1), (94, 104, 112, 255))

    header.convert("RGB").save(ASSETS / "header.png", optimize=True)
    print(f"header.png    {width}x{height}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artwork", required=True, type=Path,
        help="directory holding the Friends of Duty store artwork")
    arguments = parser.parse_args()

    ASSETS.mkdir(parents=True, exist_ok=True)
    build_icons(arguments.artwork)
    build_header(arguments.artwork)
    print(f"wrote into {ASSETS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
