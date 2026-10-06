"""Find the pillar's front-face edges numerically instead of by eye."""
import sys

import numpy as np
from PIL import Image

path = sys.argv[1]
img = Image.open(path).convert("L")
a = np.asarray(img, dtype=np.float32)
print(f"image: {img.size[0]} x {img.size[1]}")

H, W = a.shape
# vertical edge strength per column, averaged over the shaft (skip the lamp/cap)
for lo, hi in ((260, 700), (700, 1150), (1150, 1500)):
    band = a[lo:hi]
    gx = np.abs(np.diff(band, axis=1)).mean(axis=0)
    # smooth
    k = np.ones(9) / 9
    gxs = np.convolve(gx, k, mode="same")
    # top peaks, at least 25px apart
    order = np.argsort(-gxs)
    picks = []
    for x in order:
        if all(abs(int(x) - p) >= 25 for p in picks):
            picks.append(int(x))
        if len(picks) >= 6:
            break
    print(f"\ny {lo}-{hi}: strongest vertical edges at x = "
          + ", ".join(f"{x}({gxs[x]:.1f})" for x in sorted(picks)))

# brightness profile along a row: reveals the corner (bright front face vs darker side)
for y in (400, 800, 1200):
    row = a[y]
    prof = np.convolve(row, np.ones(15) / 15, mode="same")
    print(f"\nrow y={y}: brightness every 20px from x=280 to 720")
    print("  " + " ".join(f"{x}:{int(prof[x]):3d}" for x in range(280, 721, 20)))

# horizontal edges near the cap (top of the shaft) in the front-face column range
x0, x1 = 470, 620
col = a[:, x0:x1].mean(axis=1)
gy = np.abs(np.diff(col))
k = np.ones(5) / 5
gys = np.convolve(gy, k, mode="same")
order = np.argsort(-gys[:400])
picks = []
for y in order:
    if all(abs(int(y) - p) >= 8 for p in picks):
        picks.append(int(y))
    if len(picks) >= 6:
        break
print(f"\ncols {x0}-{x1}: strongest horizontal edges at y = "
      + ", ".join(f"{y}({gys[y]:.1f})" for y in sorted(picks)))
