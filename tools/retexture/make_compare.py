#!/usr/bin/env python3
"""Side-by-side comparison of two preview renders, cropped and height-matched.

Automatically crops each image to the model (ignoring a flat background or a
black backdrop) and scales both to a common height, so neither side looks better
just because of framing.

Usage:
    python make_compare.py --image before.png:"before  provider bake" \
        --image after.png:"after  reprojected" --out compare.png [--height 430]
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw


def crop_model(img):
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    near_bg = np.abs(a - np.array([245, 245, 245])).max(axis=2) < 8
    near_black = a.max(axis=2) < 26
    fg = ~(near_bg | near_black)
    ys, xs = np.nonzero(fg)
    if len(xs) == 0:
        return img
    return img.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))


def fit(img, height):
    width = max(int(round(img.width * height / img.height)), 1)
    return img.resize((width, height), Image.LANCZOS)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", action="append", required=True, metavar="PATH:LABEL",
                    help="an image to place in the sheet, repeatable (2+ makes sense)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--height", type=int, default=430)
    args = ap.parse_args(argv)

    if len(args.image) < 2:
        raise SystemExit("give at least two --image PATH:LABEL entries")

    cells = []
    for spec in args.image:
        path, _, label = spec.rpartition(":")
        if not path:
            path, label = spec, os.path.splitext(os.path.basename(spec))[0]
        cells.append((label, fit(crop_model(Image.open(path)), args.height)))

    pad = 14
    total_w = sum(cell.width for _label, cell in cells) + pad * (len(cells) + 1)
    canvas = Image.new("RGB", (total_w, args.height + pad * 2 + 24), (245, 245, 245))
    x = pad
    draw = ImageDraw.Draw(canvas)
    for label, cell in cells:
        canvas.paste(cell, (x, pad + 24))
        draw.text((x, pad + 8), label, fill=(30, 30, 30))
        x += cell.width + pad
    canvas.save(args.out)
    print(args.out, canvas.size)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
