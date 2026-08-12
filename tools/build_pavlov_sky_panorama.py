#!/usr/bin/env python3
"""Build a seamless equirectangular PAVLOV sky from CoD/Quake cube faces."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image


FACE_NAMES = ("ft", "bk", "lf", "rt", "up", "dn")


def sample_face(image: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Bilinearly sample a face where u/v range from -1 to +1."""
    height, width, _ = image.shape
    x = np.clip((u * 0.5 + 0.5) * (width - 1), 0, width - 1)
    y = np.clip((0.5 - v * 0.5) * (height - 1), 0, height - 1)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    top = image[y0, x0] * (1.0 - fx) + image[y0, x1] * fx
    bottom = image[y1, x0] * (1.0 - fx) + image[y1, x1] * fx
    return top * (1.0 - fy) + bottom * fy


def build(source: Path, output: Path, width: int, height: int) -> None:
    faces = {
        name: np.asarray(Image.open(source / f"{name}.png").convert("RGB"),
                         dtype=np.float32)
        for name in FACE_NAMES
    }
    panorama = np.empty((height, width, 3), dtype=np.uint8)
    longitude = ((np.arange(width, dtype=np.float32) + 0.5) / width
                 * (2.0 * np.pi) - np.pi)

    # Chunking keeps peak memory modest inside Unity development workspaces.
    for first_row in range(0, height, 64):
        last_row = min(first_row + 64, height)
        rows = np.arange(first_row, last_row, dtype=np.float32)
        latitude = np.pi * 0.5 - (rows + 0.5) / height * np.pi
        lon, lat = np.meshgrid(longitude, latitude)
        cos_lat = np.cos(lat)
        x = cos_lat * np.sin(lon)
        y = np.sin(lat)
        z = cos_lat * np.cos(lon)
        ax, ay, az = np.abs(x), np.abs(y), np.abs(z)

        result = np.empty((last_row - first_row, width, 3), dtype=np.float32)
        vertical = (ay >= ax) & (ay >= az)
        horizontal_x = (~vertical) & (ax >= az)
        horizontal_z = ~(vertical | horizontal_x)

        selections = (
            (vertical & (y >= 0), "up", z / ay, x / ay),
            (vertical & (y < 0), "dn", z / ay, -x / ay),
            (horizontal_x & (x >= 0), "lf", -z / ax, y / ax),
            (horizontal_x & (x < 0), "rt", z / ax, y / ax),
            (horizontal_z & (z >= 0), "ft", x / az, y / az),
            (horizontal_z & (z < 0), "bk", -x / az, y / az),
        )
        for mask, name, u, v in selections:
            sampled = sample_face(faces[name], u, v)
            result[mask] = sampled[mask]

        panorama[first_row:last_row] = np.clip(
            np.rint(result), 0, 255).astype(np.uint8)

    output.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(panorama, "RGB").save(output, optimize=True)
    print(f"Wrote {output} ({width}x{height})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--width", type=int, default=2048)
    parser.add_argument("--height", type=int, default=1024)
    args = parser.parse_args()
    build(args.source, args.output, args.width, args.height)


if __name__ == "__main__":
    main()
