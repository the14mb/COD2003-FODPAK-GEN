#!/usr/bin/env python3
"""Reconstruct clean alpha for CoD 1 PAVLOV decals exported from RGB DDS files."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def build_alpha(source: Path, output: Path, background_threshold: int) -> None:
    rgb = np.asarray(Image.open(source).convert("RGB"))
    value = rgb.max(axis=2)

    # Only dark pixels connected to the image border are transparent. This
    # preserves the broken outer rim instead of turning its gaps into black.
    traversable = (value <= background_threshold).astype(np.uint8)
    padded = cv2.copyMakeBorder(
        traversable, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
    flood_mask = np.zeros(
        (padded.shape[0] + 2, padded.shape[1] + 2), dtype=np.uint8)
    exterior = padded.copy()
    cv2.floodFill(exterior, flood_mask, (0, 0), 2)
    exterior = exterior[1:-1, 1:-1] == 2
    silhouette = (~exterior).astype(np.uint8)

    # Remove isolated DXT speckles.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        silhouette, connectivity=8)
    if count <= 1:
        raise RuntimeError(f"No decal silhouette found in {source}")
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    silhouette = (labels == largest).astype(np.uint8)
    height, width = silhouette.shape
    yy, xx = np.ogrid[:height, :width]
    radius = min(width, height) * 0.285
    central_core = (
        (xx - width * 0.5) ** 2 + (yy - height * 0.52) ** 2
        <= radius ** 2)
    silhouette[central_core] = 1

    # Pull the silhouette inward one texel before feathering to remove colored
    # compression halos without hardening the edge.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    silhouette = cv2.morphologyEx(
        silhouette, cv2.MORPH_OPEN, kernel, iterations=1)
    silhouette = cv2.erode(silhouette, kernel, iterations=1)
    alpha = cv2.GaussianBlur(
        silhouette.astype(np.float32), (5, 5), 0.9)

    # Black means transparency in the fragmented rim, but not in the central
    # blast bowl. Fade black-key pixels outside that bowl and retain the
    # depression with a soft radial mask.
    detail_visibility = np.clip(
        (value.astype(np.float32) - 8.0) / (44.0 - 8.0), 0.0, 1.0)
    distance = np.sqrt(
        (xx - width * 0.5) ** 2 + (yy - height * 0.52) ** 2)
    core_inner = min(width, height) * 0.20
    core_outer = min(width, height) * 0.30
    core_visibility = np.clip(
        (core_outer - distance) / (core_outer - core_inner), 0.0, 1.0)
    core_visibility = core_visibility * core_visibility * (
        3.0 - 2.0 * core_visibility)
    alpha *= np.maximum(detail_visibility, core_visibility)
    alpha = np.clip(np.rint(alpha * 255.0), 0, 255).astype(np.uint8)
    alpha[alpha < 4] = 0

    # Avoid absolute black in the retained blast bowl; CoD's result reads as
    # dark soil/soot rather than an unlit void.
    soil_floor = np.array([46.0, 37.0, 29.0], dtype=np.float32)
    corrected_rgb = np.maximum(
        rgb.astype(np.float32),
        core_visibility[..., None] * soil_floor)
    rgba = np.dstack((
        np.clip(np.rint(corrected_rgb), 0, 255).astype(np.uint8),
        alpha))
    output.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(output, optimize=True)
    print(
        f"Wrote {output} ({rgb.shape[1]}x{rgb.shape[0]}, "
        f"alpha coverage={(alpha > 0).mean():.1%})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--background-threshold", type=int, default=24)
    args = parser.parse_args()
    build_alpha(args.source, args.output, args.background_threshold)


if __name__ == "__main__":
    main()
