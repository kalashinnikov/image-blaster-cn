#!/usr/bin/env python3
"""Render readable previews of a retextured mesh (world up = +Y).

Writes front / quarter / left / back / hero images, plus a close-up of the front
face. Useful after a `retexture` run to eyeball the result without opening the
browser viewer.

Usage:
    python preview3.py --glb MODEL.glb --atlas atlas_retextured.png \
        --out-dir out [--hero-out preview.png] [--tag 3]
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import retexture as R  # noqa: E402

VIEWS = {
    "front": (0.0, 0.0, -1.0),
    "quarter": (-0.62, -0.22, -0.76),
    "left": (-1.0, 0.0, -0.10),
    "back": (0.0, 0.0, 1.0),
    "hero": (-0.45, -0.30, -0.84),
}


def render(wp, uv, nrm, atlas, direction, width, height, light):
    e1, e2, view = R.basis_from_viewdir(direction, up=(0.0, 1.0, 0.0))
    mask, uvb, nrmb, _depth, _cand = R.render_view(wp, uv, width, height, e1, e2, view, nrm)
    img = R.shade(R.atlas_lookup(atlas, uvb), nrmb, light)
    img[~mask] = 245
    return img


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glb", required=True)
    ap.add_argument("--atlas", required=True, help="atlas PNG to texture the previews with")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--hero-out", help="also write the hero render to this path")
    ap.add_argument("--size", default="700x900", help="WxH of the reading views")
    ap.add_argument("--hero-size", default="512x512")
    ap.add_argument("--light", default="-0.35,0.45,0.82", help="light direction x,y,z")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)
    width, height = (int(v) for v in args.size.lower().split("x"))
    hero_w, hero_h = (int(v) for v in args.hero_size.lower().split("x"))
    light = np.array([float(v) for v in args.light.split(",")])
    light = light / np.linalg.norm(light)

    atlas = np.asarray(Image.open(args.atlas).convert("RGB"), dtype=np.uint8)
    gltf, blob = R.load_glb(args.glb)
    wp, uv, nrm = R.gather_tris(gltf, blob)

    for name, direction in VIEWS.items():
        w, h = (hero_w, hero_h) if name == "hero" else (width, height)
        img = render(wp, uv, nrm, atlas, direction, w, h, light)
        path = os.path.join(args.out_dir, f"final_{name}.png")
        Image.fromarray(img).save(path)
        print(path, img.shape)
        if name == "hero" and args.hero_out:
            Image.fromarray(img).save(args.hero_out)
            print("hero ->", args.hero_out)

    img = render(wp, uv, nrm, atlas, (0.0, 0.0, -1.0), 500, 2000, light)
    path = os.path.join(args.out_dir, "final_letters.png")
    Image.fromarray(img[380:1900]).save(path)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
