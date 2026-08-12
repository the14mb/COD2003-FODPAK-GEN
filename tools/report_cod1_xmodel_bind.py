"""Report raw CoD 1 XModel bones and surface bounds before Blender import."""

from __future__ import annotations

import sys
from pathlib import Path


source = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
importer_root = Path(sys.argv[sys.argv.index("--") + 2]).resolve()
names = sys.argv[sys.argv.index("--") + 3 :]
sys.path.insert(0, str(importer_root))

from cod_asset_importer.cod_asset_importer import GAME_VERSION, Loader


class Reporter:
    def xmodel(self, model) -> None:
        print(f"MODEL {model.name()}")
        bones = model.bones()
        for index, bone in enumerate(bones):
            print(
                f"  BONE {index} name={bone.name()} parent={bone.parent()} "
                f"position={tuple(round(value, 5) for value in bone.position())} "
                f"rotation={tuple(round(value, 6) for value in bone.rotation())}"
            )
        for index, surface in enumerate(model.surfaces()):
            values = surface.vertices()
            points = list(zip(values[0::3], values[1::3], values[2::3]))
            minimum = tuple(min(point[axis] for point in points) for axis in range(3))
            maximum = tuple(max(point[axis] for point in points) for axis in range(3))
            center = tuple(
                (minimum[axis] + maximum[axis]) * 0.5 for axis in range(3)
            )
            print(
                f"  SURFACE {index} material={surface.material()} "
                f"vertices={len(points)} "
                f"min={tuple(round(value, 4) for value in minimum)} "
                f"max={tuple(round(value, 4) for value in maximum)} "
                f"center={tuple(round(value, 4) for value in center)}"
            )


reporter = Reporter()
loader = Loader(importer=reporter)
for name in names:
    loader.import_xmodel(
        asset_path=str(source),
        file_path=str(source / "xmodel" / name),
        selected_version=GAME_VERSION.CoD,
        angles=(0.0, 0.0, 0.0),
        origin=(0.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
    )
