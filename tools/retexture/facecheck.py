#!/usr/bin/env python3
"""Contact sheet of the four vertical faces, from one or more atlases.

Renders +Z / -Z / +X / -X with a true camera basis (right = +e1) and prints each
face's mean shaded luma. Handy to confirm that the side/back faces no longer
carry the provider's baked-in gibberish, and that the face that reads brightest
is the one that got the photo (i.e. the face you think is the front really is).

Usage:
    python facecheck.py --glb MODEL.glb --atlas ORIGINAL.png:source \
        --atlas RETEXTURED.png:new --out-dir out
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import retexture as R  # noqa: E402

FACES = [("+Z", (0.0, 0.0, -1.0)), ("-Z", (0.0, 0.0, 1.0)),
         ("+X", (-1.0, 0.0, 0.0)), ("-X", (1.0, 0.0, 0.0))]
LUMA = np.array([0.299, 0.587, 0.114])


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glb", required=True)
    ap.add_argument("--atlas", action="append", required=True, metavar="PATH:TAG")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--size", default="300x800")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    width, height = (int(v) for v in args.size.lower().split("x"))
    light = np.array([-0.35, 0.45, 0.82])
    light = light / np.linalg.norm(light)

    gltf, blob = R.load_glb(args.glb)
    wp, uv, nrm = R.gather_tris(gltf, blob)

    for spec in args.atlas:
        path, _, tag = spec.rpartition(":")
        if not path:
            path, tag = spec, os.path.splitext(os.path.basename(spec))[0]
        atlas = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        cells, lumas = [], []
        for _name, direction in FACES:
            e1, e2, view = R.basis_from_viewdir(direction, up=(0.0, 1.0, 0.0))
            mask, uvb, nrmb, _d, _c = R.render_view(wp, uv, width, height, e1, e2, view, nrm)
            img = R.shade(R.atlas_lookup(atlas, uvb), nrmb, light)
            img[~mask] = 245
            lumas.append(float((img[mask].astype(np.float64) @ LUMA).mean()))
            cells.append(img)

        sheet = Image.new("RGB", (width * len(FACES) + 10 * (len(FACES) + 1), height + 20), (245, 245, 245))
        for i, cell in enumerate(cells):
            sheet.paste(Image.fromarray(cell), (10 + i * (width + 10), 20))
        out_path = os.path.join(args.out_dir, f"faces_{tag}.png")
        sheet.save(out_path)
        print(f"{tag}: {out_path}")
        for (name, _d), luma in zip(FACES, lumas):
            print(f"   {name} face mean shaded luma {luma:6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
