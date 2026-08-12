#!/usr/bin/env python3
"""Reconstruct clean alpha for CoD 1 black-key decals exported as RGB images.

cv2-free port of build_pavlov_decal_alpha.py (numpy + Pillow only). The
algorithm is kept faithful: border-connected dark pixels become transparent,
DXT speckles are dropped by keeping the largest silhouette component, the
central blast bowl is always preserved, the edge is pulled inward one texel
and feathered, dark detail outside the bowl fades out, and the retained bowl
gets a soil floor instead of absolute black.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image


def _label_components(mask: np.ndarray, connectivity: int) -> np.ndarray:
    """Label the connected components of a boolean mask.

    Run-based two-pass labelling with union-find; supports 4- and
    8-connectivity. Returns an int32 label image where 0 is background and
    components are numbered from 1.
    """
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    parent = [0]

    def find(index: int) -> int:
        root = index
        while parent[root] != root:
            root = parent[root]
        while parent[index] != root:
            parent[index], index = root, parent[index]
        return root

    reach = 1 if connectivity == 8 else 0
    previous_runs: list[tuple[int, int, int]] = []
    for row in range(height):
        line = mask[row]
        if not line.any():
            previous_runs = []
            continue
        cells = line.astype(np.int8)
        boundaries = np.diff(cells)
        starts = np.flatnonzero(boundaries == 1) + 1
        ends = np.flatnonzero(boundaries == -1) + 1
        if cells[0]:
            starts = np.concatenate(([0], starts))
        if cells[-1]:
            ends = np.concatenate((ends, [width]))
        runs: list[tuple[int, int, int]] = []
        for start, end in zip(starts.tolist(), ends.tolist()):
            label = len(parent)
            parent.append(label)
            for previous_start, previous_end, previous_label in previous_runs:
                if previous_start < end + reach and start < previous_end + reach:
                    root_a = find(label)
                    root_b = find(previous_label)
                    if root_a != root_b:
                        parent[max(root_a, root_b)] = min(root_a, root_b)
            labels[row, start:end] = label
            runs.append((start, end, label))
        previous_runs = runs

    if len(parent) == 1:
        return labels
    roots = np.array([find(index) for index in range(len(parent))], np.int32)
    compact = np.zeros_like(roots)
    compact[np.unique(roots[1:])] = np.arange(
        1, len(np.unique(roots[1:])) + 1, dtype=np.int32)
    return compact[roots[labels]]


def _erode_cross(binary: np.ndarray) -> np.ndarray:
    """Binary erosion with the 3x3 cross; the border does not erode."""
    padded = np.pad(binary, 1, constant_values=True)
    return (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:])


def _dilate_cross(binary: np.ndarray) -> np.ndarray:
    """Binary dilation with the 3x3 cross; the border does not dilate."""
    padded = np.pad(binary, 1, constant_values=False)
    return (
        padded[1:-1, 1:-1]
        | padded[:-2, 1:-1]
        | padded[2:, 1:-1]
        | padded[1:-1, :-2]
        | padded[1:-1, 2:])


def _gaussian_blur_5x5(values: np.ndarray, sigma: float) -> np.ndarray:
    """Separable 5x5 Gaussian blur with reflect-101 borders."""
    positions = np.arange(-2.0, 3.0)
    kernel = np.exp(-(positions ** 2) / (2.0 * sigma * sigma))
    kernel /= kernel.sum()
    height, width = values.shape
    padded = np.pad(values, ((2, 2), (0, 0)), mode="reflect")
    vertical = sum(
        kernel[index] * padded[index : index + height, :]
        for index in range(5))
    padded = np.pad(vertical, ((0, 0), (2, 2)), mode="reflect")
    return sum(
        kernel[index] * padded[:, index : index + width]
        for index in range(5))


def build_alpha(source: Path, output: Path, background_threshold: int = 24) -> None:
    image = Image.open(source)
    rgb = np.asarray(image.convert("RGB"))
    if "A" in image.getbands():
        # Some DDS decoders leave undefined (often bright) RGB under the
        # keyed-out texels. The retired extractor decoded fully transparent
        # texels as black (DXT punch-through) — restore that so the
        # black-key survives an RGBA source.
        source_alpha = np.asarray(image.convert("RGBA"))[..., 3]
        rgb = np.where((source_alpha == 0)[..., None], 0, rgb)
    value = rgb.max(axis=2)

    # Only dark pixels connected to the image border are transparent. This
    # preserves the broken outer rim instead of turning its gaps into black.
    traversable = value <= background_threshold
    padded = np.pad(traversable, 1, constant_values=True)
    dark_labels = _label_components(padded, connectivity=4)
    exterior = (dark_labels == dark_labels[0, 0])[1:-1, 1:-1]
    silhouette = ~exterior

    # Remove isolated DXT speckles.
    labels = _label_components(silhouette, connectivity=8)
    if labels.max() < 1:
        # Some stock UO soot/scorch decals are intentionally darker than the
        # crater threshold (their entire RGB range can be 0..10). They still
        # contain a valid black-key mask, just no pixel bright enough for the
        # terrain-crater reconstruction above. Recover that mask from its
        # contrast over the border-connected black level instead of aborting
        # the complete multiplayer map export or shipping an opaque black
        # quad. A truly uniform image remains an error because it contains no
        # recoverable authored silhouette.
        border = np.concatenate((
            value[0, :],
            value[-1, :],
            value[:, 0],
            value[:, -1],
        )).astype(np.float32)
        background = float(np.median(border))
        signal = np.maximum(
            value.astype(np.float32) - background,
            0.0,
        )
        peak = float(np.percentile(signal, 99.5))
        if peak <= 0.0:
            raise RuntimeError(f"No decal silhouette found in {source}")
        normalized = np.clip(signal / peak, 0.0, 1.0)
        alpha = _gaussian_blur_5x5(
            normalized * normalized,
            0.9,
        )
        alpha = np.clip(
            np.rint(alpha * 220.0),
            0,
            255,
        ).astype(np.uint8)
        alpha[alpha < 4] = 0
        rgba = np.dstack((rgb, alpha))
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgba, "RGBA").save(output, optimize=True)
        print(
            f"Wrote {output} ({rgb.shape[1]}x{rgb.shape[0]}, "
            "adaptive dark-key alpha, "
            f"coverage={(alpha > 0).mean():.1%})"
        )
        return
    areas = np.bincount(labels.ravel())
    largest = 1 + int(np.argmax(areas[1:]))
    silhouette = labels == largest
    height, width = silhouette.shape
    yy, xx = np.ogrid[:height, :width]
    radius = min(width, height) * 0.285
    central_core = (
        (xx - width * 0.5) ** 2 + (yy - height * 0.52) ** 2
        <= radius ** 2)
    silhouette |= central_core

    # Pull the silhouette inward one texel before feathering to remove colored
    # compression halos without hardening the edge.
    silhouette = _dilate_cross(_erode_cross(silhouette))
    silhouette = _erode_cross(silhouette)
    alpha = _gaussian_blur_5x5(silhouette.astype(np.float32), 0.9)

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
