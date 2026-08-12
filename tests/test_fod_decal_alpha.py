from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from tools.fod_decal_alpha import build_alpha


class DecalAlphaTests(unittest.TestCase):
    def test_dark_scorch_key_builds_transparency_instead_of_aborting(self) -> None:
        pixels = np.zeros((32, 32, 3), dtype=np.uint8)
        pixels[8:24, 10:22] = 8
        pixels[12:20, 12:20] = 10
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dark_scorch.png"
            output = root / "dark_scorch_rgba.png"
            Image.fromarray(pixels, "RGB").save(source)

            build_alpha(source, output)

            rgba = np.asarray(Image.open(output).convert("RGBA"))
            self.assertEqual(int(rgba[0, 0, 3]), 0)
            self.assertGreater(int(rgba[16, 16, 3]), 0)
            self.assertLess(np.count_nonzero(rgba[..., 3]), 32 * 32)


if __name__ == "__main__":
    unittest.main()
