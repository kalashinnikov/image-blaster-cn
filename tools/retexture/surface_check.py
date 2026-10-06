#!/usr/bin/env python3
"""Chirality check that involves no camera basis at all.

Two renders of a mesh can agree with each other and still both be mirrored: if
the sampling step and the self-check share the same (wrong) left/right basis,
everything looks self-consistent and only a real viewer ever sees the flip.

So this check never builds a camera. It rasterises the face in *surface*
coordinates (+X right, +Y up) and reads the atlas through the mesh's own
TEXCOORD_0, then correlates that image against the source photo, direct vs.
horizontally flipped. Direct must win for the texture to sit on the model the
way a viewer needs it.

Running it on the *original* (provider-baked) atlas as well cross-checks the
convention: the bake was made from the same photo, so it must also correlate
better unflipped.

Usage:
    python surface_check.py --glb MODEL.glb --photo PHOTO.jpg \
        --atlas ORIGINAL.png:source --atlas RETEXTURED.png:new \
        --out-dir out [--photo-quad "LT RT RB LB"] [--normal-axis z]
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import retexture as R  # noqa: E402  (sibling module, same directory)

AXIS = {"x": 0, "y": 1, "z": 2}
LUMA = np.array([0.299, 0.587, 0.114])


def parse_args(argv):
    ap = argparse.ArgumentParser(add_help=True, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glb", required=True, help="model whose UVs are checked")
    ap.add_argument("--photo", required=True, help="source photo")
    ap.add_argument("--atlas", action="append", required=True,
                    metavar="PATH:TAG", help="atlas texture to test, repeatable")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--photo-quad", help='"x,y x,y x,y x,y" front-face quad in the photo')
    ap.add_argument("--normal-axis", default="z", choices=sorted(AXIS),
                    help="axis the face points along (default z)")
    ap.add_argument("--normal-threshold", type=float, default=0.6)
    ap.add_argument("--centroid-threshold", type=float, default=0.0)
    ap.add_argument("--width", type=int, default=290, help="reference sampling width")
    ap.add_argument("--height", type=int, default=943, help="reference sampling height")
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    if args.photo_quad:
        R.PHOTO_QUAD = R.parse_quad(args.photo_quad)

    axis = AXIS[args.normal_axis]
    width, height = args.width, args.height
    os.makedirs(args.out_dir, exist_ok=True)

    gltf, blob = R.load_glb(args.glb)
    wp, uv, _nrm = R.gather_tris(gltf, blob)

    a, b, c = wp[:, 0], wp[:, 1], wp[:, 2]
    normals = np.cross(b - a, c - a)
    normals = normals / np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    centroid = wp.mean(axis=1)
    front = (normals[:, axis] > args.normal_threshold) & (centroid[:, axis] > args.centroid_threshold)
    print(f"front-face triangles: {int(front.sum())} of {len(wp)}")

    pts = wp[front]
    xmin, xmax = pts[:, :, 0].min(), pts[:, :, 0].max()
    ymin, ymax = pts[:, :, 1].min(), pts[:, :, 1].max()
    print(f"front slab extent x {xmin:.4f}..{xmax:.4f}  y {ymin:.4f}..{ymax:.4f}")

    sx = (wp[:, :, 0] - xmin) / (xmax - xmin) * (width - 1)
    sy = (ymax - wp[:, :, 1]) / (ymax - ymin) * (height - 1)

    homography = R.homography([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
                              [R.PHOTO_QUAD["LT"], R.PHOTO_QUAD["RT"], R.PHOTO_QUAD["RB"], R.PHOTO_QUAD["LB"]])
    photo = np.asarray(Image.open(args.photo).convert("RGB"), dtype=np.uint8)
    uu, vv = np.meshgrid((np.arange(width) + 0.5) / width, (np.arange(height) + 0.5) / height)
    ref, _x, _y = R.sample_photo(photo, homography, uu.ravel(), vv.ravel())
    ref = ref.reshape(height, width, 3).astype(np.float64)

    def surface_image(atlas_path):
        atlas = np.asarray(Image.open(atlas_path).convert("RGB"), dtype=np.uint8)
        img = np.zeros((height, width, 3), dtype=np.float64)
        hit = np.zeros((height, width), dtype=bool)
        for i in np.where(front)[0]:
            xs3, ys3 = sx[i], sy[i]
            px0 = max(int(np.floor(xs3.min())), 0)
            px1 = min(int(np.ceil(xs3.max())), width - 1)
            py0 = max(int(np.floor(ys3.min())), 0)
            py1 = min(int(np.ceil(ys3.max())), height - 1)
            if px1 < px0 or py1 < py0:
                continue
            gx, gy = np.meshgrid(np.arange(px0, px1 + 1) + 0.5, np.arange(py0, py1 + 1) + 0.5)
            ax, ay = xs3[0], ys3[0]
            bx, by = xs3[1], ys3[1]
            cx, cy = xs3[2], ys3[2]
            area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if abs(area) < 1e-12:
                continue
            w0 = ((bx - gx) * (cy - gy) - (by - gy) * (cx - gx)) / area
            w1 = ((cx - gx) * (ay - gy) - (cy - gy) * (ax - gx)) / area
            w2 = 1.0 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            uvb = w0[..., None] * uv[i][0] + w1[..., None] * uv[i][1] + w2[..., None] * uv[i][2]
            colors = R.atlas_lookup(atlas, uvb.reshape(-1, 2)).reshape(gx.shape + (3,)).astype(np.float64)
            img[py0:py1 + 1, px0:px1 + 1][inside] = colors[inside]
            hit[py0:py1 + 1, px0:px1 + 1][inside] = True
        return img, hit

    verdicts = []
    for spec in args.atlas:
        path, _, tag = spec.rpartition(":")
        if not path:
            path, tag = spec, os.path.splitext(os.path.basename(spec))[0]
        img, hit = surface_image(path)
        out = img.copy()
        out[~hit] = 245
        out_path = os.path.join(args.out_dir, f"surf_{tag}.png")
        Image.fromarray(out.astype(np.uint8)).save(out_path)
        mask = hit & (img.sum(axis=2) > 0)
        x = img @ LUMA
        y = ref @ LUMA
        direct = float(np.corrcoef(x[mask], y[mask])[0, 1])
        flipped = float(np.corrcoef(x[mask], y[mask][::-1])[0, 1])
        good = direct > flipped
        verdicts.append(good)
        print(f"{tag:9s} pixels {int(mask.sum()):7d}   corr direct {direct:+.3f}   "
              f"corr flipped {flipped:+.3f}   -> {'DIRECT ok' if good else 'FLIPPED!'}   ({out_path})")

    if not all(verdicts):
        print("FAIL: at least one atlas reads mirrored -- the projection basis is wrong")
        return 1
    print("OK: every atlas reads unflipped in surface coordinates")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
