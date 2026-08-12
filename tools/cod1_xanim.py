#!/usr/bin/env python3
"""Reader for Call of Duty 1's compiled (version 14) XAnim files."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        values = struct.unpack_from("<" + fmt, self.data, self.pos)
        self.pos += size
        return values[0] if len(values) == 1 else values

    def bytes(self, count: int) -> bytes:
        value = self.data[self.pos : self.pos + count]
        self.pos += count
        return value

    def cstring(self) -> str:
        end = self.data.index(0, self.pos)
        value = self.data[self.pos:end].decode("ascii")
        self.pos = end + 1
        return value


@dataclass
class Track:
    indices: list[int]
    values: list[tuple[float, ...]]


@dataclass
class BoneTrack:
    name: str
    rotation: Track | None
    translation: Track | None


@dataclass
class Animation:
    name: str
    frame_count: int
    frame_rate: int
    looped: bool
    bones: list[BoneTrack]
    notes: list[tuple[str, int]]


def _indices(reader: Reader, count: int, loop_frames: int, byte_indices: bool) -> list[int]:
    if count >= loop_frames:
        return list(range(count))
    return [reader.take("B" if byte_indices else "H") for _ in range(count)]


def _rotation(reader: Reader, count: int, loop_frames: int, byte_indices: bool,
              half: bool, flip: bool) -> Track | None:
    if count == 0:
        return None
    indices = [0] if count == 1 else _indices(reader, count, loop_frames, byte_indices)
    values: list[tuple[float, float, float, float]] = []
    for index in range(count):
        if half:
            x = reader.take("h")
            y = round(math.sqrt(max(0, 0x3FFF0001 - x * x)))
            # XQuat2 stores the Z/W pair; X and Y are implicitly zero.
            raw = [0, 0, x, y]
        else:
            x, y, z = reader.take("hhh")
            w = round(math.sqrt(max(0, 0x3FFF0001 - x * x - y * y - z * z)))
            raw = [x, y, z, w]
        if (index == 0 and flip) or (
            index > 0 and sum(a * b for a, b in zip(values[-1], raw)) < 0
        ):
            raw = [-v for v in raw]
        length = math.sqrt(sum(v * v for v in raw)) or 1.0
        values.append(tuple(v / length for v in raw))
    return Track(indices, values)


def _translation(reader: Reader, count: int, loop_frames: int,
                 byte_indices: bool) -> Track | None:
    if count == 0:
        return None
    indices = [0] if count == 1 else _indices(
        reader, count, loop_frames, byte_indices
    )
    # CoD 1's version-14 layout predates the min/size quantization introduced
    # by later IW engines. Every keyed translation remains three floats.
    return Track(indices, [reader.take("fff") for _ in range(count)])


def _name_table(data: bytes, bone_count: int, search_start: int):
    """Locate CoD's bone-name table after the optional root-delta track."""
    for start in range(search_start, min(len(data), search_start + 1024)):
        position = start
        names = []
        try:
            for _ in range(bone_count):
                end = data.index(0, position, min(len(data), position + 80))
                raw = data[position:end]
                if not raw or any(value < 32 or value > 126 for value in raw):
                    raise ValueError
                names.append(raw.decode("ascii"))
                position = end + 1
        except (ValueError, UnicodeDecodeError):
            continue
        if not names:
            # A candidate offset that yielded nothing is simply not the
            # table. Reached for real by United Offensive's weapons, several
            # of which ship an XAnim with a zero bone count (an empty
            # placeholder clip), where this used to raise IndexError out of
            # the scan and abort the whole viewmodel export.
            continue
        prefixes = (
            "bip", "tag_", "torso", "back_", "neck", "pelvis", "head"
        )
        if (
            names[0].lower().startswith(prefixes)
            and sum(name.lower().startswith(prefixes) for name in names)
            >= max(1, bone_count // 2)
        ):
            return start, names, position
    raise ValueError("could not locate XAnim bone-name table")


def load(path: Path) -> Animation:
    reader = Reader(path.read_bytes())
    version = reader.take("H")
    if version != 14:
        raise ValueError(f"{path}: expected CoD XAnim 14, got {version}")
    frame_count, bone_count = reader.take("HH")
    flags = reader.take("B")
    frame_rate = reader.take("H")
    looped = bool(flags & 1)
    loop_frames = frame_count + 1 if looped else frame_count
    byte_indices = max(0, loop_frames - 1) < 256
    mask_size = (bone_count + 7) // 8
    names_start, names, data_start = _name_table(
        reader.data, bone_count, reader.pos
    )
    masks_start = names_start - mask_size * 2
    if masks_start < reader.pos:
        raise ValueError(f"{path}: invalid XAnim mask/name layout")
    # Bytes between the fixed header and masks are the optional root-delta
    # track. Multiplayer motion is network-authoritative, so it is deliberately
    # omitted while all skeletal animation remains exact.
    flip_mask = reader.data[masks_start:names_start - mask_size]
    half_mask = reader.data[names_start - mask_size:names_start]
    reader.pos = data_start
    bones = []
    for index, name in enumerate(names):
        flip = bool(flip_mask[index // 8] & (1 << (index % 8)))
        half = bool(half_mask[index // 8] & (1 << (index % 8)))
        rotation = _rotation(
            reader, reader.take("H"), loop_frames, byte_indices, half, flip
        )
        translation = _translation(
            reader, reader.take("H"), loop_frames, byte_indices
        )
        bones.append(BoneTrack(name.lower(), rotation, translation))
    note_count = reader.take("B")
    notes = [(reader.cstring(), reader.take("H")) for _ in range(note_count)]
    if reader.pos != len(reader.data):
        raise ValueError(f"{path}: {len(reader.data) - reader.pos} unread bytes")
    return Animation(path.name, frame_count, frame_rate, looped, bones, notes)


def sample(track: Track | None, frame: int, quaternion: bool = False):
    if track is None:
        return (0.0, 0.0, 0.0, 1.0) if quaternion else (0.0, 0.0, 0.0)
    if len(track.values) == 1 or frame <= track.indices[0]:
        return track.values[0]
    if frame >= track.indices[-1]:
        return track.values[-1]
    right = next(i for i, key in enumerate(track.indices) if key >= frame)
    left = right - 1
    span = track.indices[right] - track.indices[left]
    factor = (frame - track.indices[left]) / span
    a, b = track.values[left], track.values[right]
    if quaternion:
        dot = sum(x * y for x, y in zip(a, b))
        if dot < 0:
            b = tuple(-x for x in b)
            dot = -dot
        if dot > 0.9995:
            value = tuple(x + factor * (y - x) for x, y in zip(a, b))
            length = math.sqrt(sum(x * x for x in value))
            return tuple(x / length for x in value)
        theta = math.acos(max(-1, min(1, dot)))
        sine = math.sin(theta)
        return tuple(
            (math.sin((1 - factor) * theta) * x + math.sin(factor * theta) * y) / sine
            for x, y in zip(a, b)
        )
    return tuple(x + factor * (y - x) for x, y in zip(a, b))
