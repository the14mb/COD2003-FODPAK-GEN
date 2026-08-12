#!/usr/bin/env python3
"""Extract authored PAVLOV props, lights, and fog from the original CoD1 data.

The map's visible BSP geometry does not contain its entity lump. This produces
the deterministic JSON consumed by Unity so a scene rebuild and a runtime
PavlovFpsDemo load both receive the same original placements.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import zipfile
from pathlib import Path

from cod1_archive_policy import require_official_archive

ENTITY_LUMP_INDEX = 29
COD_UNIT_TO_METRE = 0.0254
NON_GEOMETRY_MODELS = {"xmodel/fx"}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("pk3", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--bsp",
        default="maps/MP/mp_pavlov.bsp",
        help="BSP entry inside the PK3",
    )
    parser.add_argument(
        "--script",
        default="maps/MP/mp_pavlov.gsc",
        help="map script entry inside the PK3",
    )
    return parser.parse_args()


def parse_entities(bsp: bytes) -> list[dict[str, str]]:
    if bsp[:4] != b"IBSP" or struct.unpack_from("<i", bsp, 4)[0] != 59:
        raise ValueError("expected a Call of Duty 1 IBSP v59 map")
    length, offset = struct.unpack_from(
        "<ii", bsp, 8 + ENTITY_LUMP_INDEX * 8
    )
    text = bsp[offset : offset + length].decode("latin1").rstrip("\0")
    entities = []
    for body in re.findall(r"\{([^}]*)\}", text, re.DOTALL):
        entities.append(
            dict(re.findall(r'"([^"]*)"\s+"([^"]*)"', body))
        )
    if not entities or entities[0].get("classname") != "worldspawn":
        raise ValueError("PAVLOV entity lump has no worldspawn")
    return entities


def vector(value: str | None, default: tuple[float, float, float]) -> list[float]:
    if not value:
        return list(default)
    fields = value.split()
    if len(fields) != 3:
        raise ValueError(f"invalid three-component vector: {value!r}")
    return [float(field) for field in fields]


def color(value: str | None) -> list[float]:
    result = vector(value, (1.0, 1.0, 1.0))
    if max(result) > 1.0:
        result = [component / 255.0 for component in result]
    return [max(0.0, min(1.0, component)) for component in result]


def parse_fog(script: str) -> dict[str, object]:
    match = re.search(
        r"setCullFog\s*\(\s*"
        r"([-+.\d]+)\s*,\s*([-+.\d]+)\s*,\s*"
        r"([-+.\d]+)\s*,\s*([-+.\d]+)\s*,\s*([-+.\d]+)\s*,\s*"
        r"([-+.\d]+)\s*\)",
        script,
        re.IGNORECASE,
    )
    if match is None:
        raise ValueError("mp_pavlov.gsc contains no setCullFog call")
    values = [float(value) for value in match.groups()]
    return {
        "startCodUnits": values[0],
        "endCodUnits": values[1],
        "startMetres": values[0] * COD_UNIT_TO_METRE,
        "endMetres": values[1] * COD_UNIT_TO_METRE,
        "color": values[2:5],
        "transitionSeconds": values[5],
        "sourceCall": match.group(0),
    }


def main() -> None:
    arguments = parse_arguments()
    source_pk3 = require_official_archive(arguments.pk3)
    with zipfile.ZipFile(source_pk3) as archive:
        bsp = archive.read(arguments.bsp)
        script = archive.read(arguments.script).decode("latin1")

    entities = parse_entities(bsp)
    placements = []
    for source_index, entity in enumerate(entities):
        model_path = entity.get("model", "")
        if (
            entity.get("classname") not in {"misc_model", "script_model"}
            or not model_path.startswith("xmodel/")
            or model_path in NON_GEOMETRY_MODELS
        ):
            continue
        placements.append(
            {
                "sourceEntityIndex": source_index,
                "className": entity["classname"],
                "model": model_path.removeprefix("xmodel/"),
                "sourceModel": model_path,
                "origin": vector(entity.get("origin"), (0.0, 0.0, 0.0)),
                "angles": vector(entity.get("angles"), (0.0, 0.0, 0.0)),
                "scale": float(entity.get("modelscale", "1")),
                "lighting": color(entity.get("lightingPrecalc")),
            }
        )

    lights = []
    for source_index, entity in enumerate(entities):
        if entity.get("classname") != "light":
            continue
        lights.append(
            {
                "sourceEntityIndex": source_index,
                "origin": vector(entity.get("origin"), (0.0, 0.0, 0.0)),
                "angles": vector(entity.get("angles"), (0.0, 0.0, 0.0)),
                "color": color(entity.get("_color")),
                "intensity": float(entity.get("light", "300")),
                "overbrightShift": float(entity.get("overbrightShift", "1")),
            }
        )

    model_names = sorted({placement["model"] for placement in placements})
    payload = {
        "format": "FriendsOfDuty.PavlovOriginalEntities",
        "version": 1,
        "source": f"{arguments.pk3.name}:{arguments.bsp}",
        "fog": parse_fog(script),
        "modelAssets": model_names,
        "placements": placements,
        "lights": lights,
        "counts": {
            "sourceEntities": len(entities),
            "placements": len(placements),
            "uniqueModels": len(model_names),
            "lights": len(lights),
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "PAVLOV entities extracted: "
        f"{len(placements)} model placements, "
        f"{len(model_names)} unique geometry models, "
        f"{len(lights)} lights, fog={payload['fog']['sourceCall']}"
    )
    print(arguments.output)


if __name__ == "__main__":
    main()
