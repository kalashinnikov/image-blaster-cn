"""Reproject the real photo onto the pillar's front face.

Approach: render the mesh from the front with an orthographic z-buffer, record
the atlas UV hit by every visible pixel, then scatter the photo's front-face
pixels back into the atlas at those UVs. Fragmented UV islands do not matter --
scattering covers exactly what is visible.

Subcommands:
  views    <glb> <outdir>                     -- render +Z/-Z/+X/-X with the current texture
  retexture <glb> <photo> <outglb> <outdir> [front|all]
                                              -- reproject, save, then render for verification

Optional overrides (before or after the subcommand arguments):
  --photo-quad "x,y x,y x,y x,y"   front-face quad in the photo (LT RT RB LB)
  --side-quad  "x,y x,y x,y x,y"   plain side-face strip in the photo (LT RT RB LB)
  --atlas-size N                   output texture size (default 4096)
  --front-w N / --front-h N        working resolution of the face render

The quad defaults are measured from the demo photo (a night shot of a stone gate
pillar); for any other photo you must measure your own -- use edge_profile.py to
locate the vertical corner lines, then read off the cap/base rows by eye.
"""
import json
import os
import struct
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_glb import load_glb, read_accessor, transform_points, world_matrices  # noqa: E402

ATLAS_SIZE = 4096
FRONT_W = 1024
FRONT_H = 4032

# Front-face quad in the original photo, measured by edge profiling:
# vertical corner lines x=457/749 at y~480 -> 469/763 at y~1325; cap underside
# at y=282; the shaft ends at the strongest horizontal band, y~1225 (a uniform
# scale of ~3.1 then matches the model's shaft rectangle in both axes).
PHOTO_QUAD = {
    "LT": (455.4, 282.0),
    "RT": (745.6, 282.0),
    "RB": (761.3, 1225.0),
    "LB": (467.9, 1225.0),
}

# The pillar's left side face in the same photo: plain (dim) stone between the
# silhouette at x~350 and the front face's left corner. Used to replace the
# baked-in gibberish on the model's side/back/top faces.
SIDE_QUAD = {
    "LT": (350.0, 282.0),
    "RT": (455.4, 282.0),
    "RB": (467.9, 1225.0),
    "LB": (363.0, 1225.0),
}


# ---------------------------------------------------------------- glb plumbing
def gather_tris(gj, bin_blob):
    wm = world_matrices(gj)
    wp_parts, uv_parts, nrm_parts = [], [], []
    for mesh_index, placements in wm.items():
        mesh = gj["meshes"][mesh_index]
        for _node_index, m in placements:
            for prim in mesh["primitives"]:
                pos = read_accessor(gj, bin_blob, prim["attributes"]["POSITION"])
                uv = read_accessor(gj, bin_blob, prim["attributes"]["TEXCOORD_0"])
                idx = read_accessor(gj, bin_blob, prim["indices"]).reshape(-1).astype(np.int64)
                wp = transform_points(m, pos)
                t = idx.reshape(-1, 3)
                wp_parts.append(wp[t])
                uv_parts.append(uv[t])
                nrm_attr = prim["attributes"].get("NORMAL")
                if nrm_attr is None:
                    n_parts = np.zeros_like(wp)
                else:
                    n = read_accessor(gj, bin_blob, nrm_attr).astype(np.float64)
                    rot = np.asarray(m, dtype=np.float64)[:3, :3]
                    n = n @ rot.T
                    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
                    n_parts = n[t]
                nrm_parts.append(n_parts)
    return np.concatenate(wp_parts), np.concatenate(uv_parts), np.concatenate(nrm_parts)


def read_atlas(gj, bin_blob, outdir):
    img = gj["images"][0]
    bv = gj["bufferViews"][img["bufferView"]]
    start = bv.get("byteOffset", 0)
    payload = bin_blob[start:start + bv["byteLength"]]
    path = os.path.join(outdir, "atlas_source.png")
    with open(path, "wb") as fh:
        fh.write(payload)
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8), path


def rewrite_glb(gj, bin_blob, new_image_bytes, out_path):
    bvs = gj["bufferViews"]
    payloads = []
    for bv in bvs:
        start = bv.get("byteOffset", 0)
        payloads.append(bytearray(bin_blob[start:start + bv["byteLength"]]))
    payloads[gj["images"][0]["bufferView"]] = bytearray(new_image_bytes)

    buf = bytearray()
    for bv, payload in zip(bvs, payloads):
        while len(buf) % 4:
            buf.append(0)
        bv["byteOffset"] = len(buf)
        bv["byteLength"] = len(payload)
        buf.extend(payload)
    while len(buf) % 4:
        buf.append(0)
    gj["buffers"][0]["byteLength"] = len(buf)

    js = json.dumps(gj, separators=(",", ":")).encode("utf-8")
    while len(js) % 4:
        js += b" "
    total = 12 + 8 + len(js) + 8 + len(buf)
    with open(out_path, "wb") as fh:
        fh.write(struct.pack("<4sII", b"glTF", 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A))
        fh.write(js)
        fh.write(struct.pack("<II", len(buf), 0x004E4942))
        fh.write(bytes(buf))
    return total


# ------------------------------------------------------------------ rasterizer
def render_view(wp_tris, uv_tris, W, H, e1, e2, viewdir, nrm_tris=None, margin=0.02):
    """Orthographic render. Returns (mask, uv_buf, normal_buf, depth_buf)."""
    flat = wp_tris.reshape(-1, 3)
    c1 = flat @ e1
    c2 = flat @ e2
    lo1, hi1 = c1.min(), c1.max()
    lo2, hi2 = c2.min(), c2.max()
    pad1 = (hi1 - lo1) * margin
    pad2 = (hi2 - lo2) * margin
    lo1, hi1, lo2, hi2 = lo1 - pad1, hi1 + pad1, lo2 - pad2, hi2 + pad2

    sx = (c1.reshape(-1, 3) - lo1) / (hi1 - lo1) * (W - 1)
    sy = (hi2 - c2.reshape(-1, 3)) / (hi2 - lo2) * (H - 1)
    depth = (wp_tris @ viewdir).reshape(-1, 3)

    a = wp_tris[:, 0]
    b = wp_tris[:, 1]
    c = wp_tris[:, 2]
    n = np.cross(b - a, c - a)
    area3 = 0.5 * np.linalg.norm(n, axis=1)
    keep = area3 > 1e-12
    nrm = np.zeros_like(n)
    nrm[keep] = n[keep] / (2 * area3[keep])[:, None]
    facing = (nrm @ viewdir) < -1e-9          # normal points back at the camera

    uv_buf = np.zeros((H, W, 2), dtype=np.float32)
    nrm_buf = np.zeros((H, W, 3), dtype=np.float32)
    depth_buf = np.full((H, W), np.inf, dtype=np.float64)
    mask = np.zeros((H, W), dtype=bool)

    order = np.where(facing)[0]
    cand = 0
    for i in order:
        xs3 = sx[i]
        ys3 = sy[i]
        px0 = max(int(np.floor(xs3.min())) - 1, 0)
        px1 = min(int(np.ceil(xs3.max())) + 1, W - 1)
        py0 = max(int(np.floor(ys3.min())) - 1, 0)
        py1 = min(int(np.ceil(ys3.max())) + 1, H - 1)
        if px1 < px0 or py1 < py0:
            continue
        cand += 1
        xs = np.arange(px0, px1 + 1) + 0.5
        ys = np.arange(py0, py1 + 1) + 0.5
        gx, gy = np.meshgrid(xs, ys)

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
        d = w0 * depth[i][0] + w1 * depth[i][1] + w2 * depth[i][2]
        sub_d = depth_buf[py0:py1 + 1, px0:px1 + 1]
        hit = inside & (d < sub_d)
        if not hit.any():
            continue
        ys_i, xs_i = np.nonzero(hit)
        gy_i = ys_i + py0
        gx_i = xs_i + px0
        w0h = w0[hit]
        w1h = w1[hit]
        w2h = w2[hit]
        uv_buf[gy_i, gx_i] = (
            w0h[:, None] * uv_tris[i][0] + w1h[:, None] * uv_tris[i][1] + w2h[:, None] * uv_tris[i][2]
        )
        nh = w0h[:, None] * nrm[i][0] + w1h[:, None] * nrm[i][1] + w2h[:, None] * nrm[i][2]
        if nrm_tris is not None:
            nh = (w0h[:, None] * nrm_tris[i][0] + w1h[:, None] * nrm_tris[i][1]
                  + w2h[:, None] * nrm_tris[i][2])
        ln = np.linalg.norm(nh, axis=1, keepdims=True)
        nrm_buf[gy_i, gx_i] = nh / np.maximum(ln, 1e-9)
        depth_buf[gy_i, gx_i] = d[hit]
        mask[gy_i, gx_i] = True
    return mask, uv_buf, nrm_buf, depth_buf, cand


def basis_from_viewdir(d, up=(0.0, 0.0, 1.0)):
    """Camera basis as a real view: e1 = right, e2 = up, d = view direction.

    Getting e1 wrong flips the image horizontally, and because the same basis is
    used both to sample the photo and to render the check images, a mirrored
    mapping looks perfect in every self-check -- it only shows up in a viewer.
    """
    d = np.asarray(d, dtype=np.float64)
    d = d / np.linalg.norm(d)
    up = np.asarray(up, dtype=np.float64)
    if abs(float(d @ up)) > 0.95:
        # pick any world axis that is not (nearly) parallel to the view direction
        for cand in ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)):
            cand = np.asarray(cand, dtype=np.float64)
            if abs(float(d @ cand)) <= 0.95:
                up = cand
                break
    e1 = np.cross(d, up)
    e1 = e1 / np.linalg.norm(e1)
    e2 = np.cross(e1, d)
    e2 = e2 / np.linalg.norm(e2)
    return e1, e2, d


# ------------------------------------------------------------------- atlas ops
def atlas_lookup(atlas, uv):
    ah, aw = atlas.shape[:2]
    tx = np.clip((uv[..., 0] * (aw - 1)).astype(np.int32), 0, aw - 1)
    ty = np.clip((uv[..., 1] * (ah - 1)).astype(np.int32), 0, ah - 1)
    return atlas[ty, tx]


def shade(colors, normals, light, ambient=0.35):
    lam = np.clip(normals @ light, 0.0, 1.0)
    f = (ambient + (1 - ambient) * lam)[..., None]
    return np.clip(colors.astype(np.float32) * f, 0, 255).astype(np.uint8)


def fill_holes(atlas, written, iterations=4):
    out = atlas.copy()
    cur = written.copy()
    for _ in range(iterations):
        if cur.all():
            break
        acc = np.zeros_like(out, dtype=np.float32)
        cnt = np.zeros(cur.shape, dtype=np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                sh = np.roll(np.roll(out, dy, axis=0), dx, axis=1)
                shm = np.roll(np.roll(cur, dy, axis=0), dx, axis=1)
                acc += sh.astype(np.float32) * shm[..., None]
                cnt += shm
        new = (~cur) & (cnt > 0)
        with np.errstate(invalid="ignore"):
            avg = acc / np.maximum(cnt, 1)[..., None]
        out[new] = np.clip(avg[new], 0, 255).astype(np.uint8)
        cur |= new
    return out, cur


# --------------------------------------------------------------- photo -> rect
def homography(src_quad, dst_quad):
    """Map points given in src_quad to dst_quad (both 4x2, same order)."""

    def norm4(pts):
        m = np.zeros((8, 8))
        b = np.zeros(8)
        for i, (x, y) in enumerate(pts):
            X, Y = dst_quad[i]
            m[i * 2] = [x, y, 1, 0, 0, 0, -X * x, -X * y]
            m[i * 2 + 1] = [0, 0, 0, x, y, 1, -Y * x, -Y * y]
            b[i * 2] = X
            b[i * 2 + 1] = Y
        return m, b

    m, b = norm4(src_quad)
    h = np.linalg.solve(m, b)
    return np.append(h, 1.0).reshape(3, 3)


def warp_quad(photo, quad, size, out_path):
    """Perspective-warp the quad out of the photo into a rectangle."""
    W, H = size
    unit = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    src = [quad["LT"], quad["RT"], quad["RB"], quad["LB"]]
    h = homography(unit, src)          # unit square -> photo pixels

    us = (np.arange(W) + 0.5) / W
    vs = (np.arange(H) + 0.5) / H
    U, V = np.meshgrid(us, vs)
    den = h[2, 0] * U + h[2, 1] * V + h[2, 2]
    X = (h[0, 0] * U + h[0, 1] * V + h[0, 2]) / den
    Y = (h[1, 0] * U + h[1, 1] * V + h[1, 2]) / den

    ph, pw = photo.shape[:2]
    x0 = np.floor(X).astype(np.int64)
    y0 = np.floor(Y).astype(np.int64)
    fx = (X - x0)[..., None]
    fy = (Y - y0)[..., None]
    x0c = np.clip(x0, 0, pw - 1)
    y0c = np.clip(y0, 0, ph - 1)
    x1c = np.clip(x0 + 1, 0, pw - 1)
    y1c = np.clip(y0 + 1, 0, ph - 1)
    p = photo.astype(np.float32)
    top = p[y0c, x0c] * (1 - fx) + p[y0c, x1c] * fx
    bot = p[y1c, x0c] * (1 - fx) + p[y1c, x1c] * fx
    out = top * (1 - fy) + bot * fy
    out = np.clip(out, 0, 255).astype(np.uint8)
    Image.fromarray(out).save(out_path)
    return out


# ------------------------------------------------------------------- subcommands
def cmd_profile(glb, outdir):
    """Row-by-row structure of the front view: where is the shaft's flat face?"""
    os.makedirs(outdir, exist_ok=True)
    gj, blob = load_glb(glb)
    wp_tris, uv_tris, nrm_tris = gather_tris(gj, blob)
    e1, e2, vd = basis_from_viewdir((0, 0, -1))
    mask, uv, nrm, depth, _c = render_view(wp_tris, uv_tris, FRONT_W, FRONT_H, e1, e2, vd, nrm_tris)
    lo = wp_tris.reshape(-1, 3).min(axis=0)
    hi = wp_tris.reshape(-1, 3).max(axis=0)
    print(f"world bbox y {lo[1]:.4f} .. {hi[1]:.4f}  (height {hi[1] - lo[1]:.4f})")
    zface = nrm[..., 2]
    step = 100
    print(f"{'row':>5} {'worldY':>8} {'px':>7} {'x0':>5} {'x1':>5} {'width':>6} {'n.Z>0.7':>8} {'depth p90':>10}")
    for r0 in range(0, FRONT_H, step):
        r1 = min(r0 + step, FRONT_H)
        m = mask[r0:r1]
        if not m.any():
            continue
        rows_any = m.any(axis=1)
        rr = np.nonzero(rows_any)[0]
        cols = np.nonzero(m.any(axis=0))[0]
        ymid = (r0 + rr.mean())
        world_y = hi[1] - ymid / (FRONT_H - 1) * (hi[1] - lo[1])
        good = zface[r0:r1][m]
        dep = depth[r0:r1][m]
        print(f"{r0:>5} {world_y:>8.4f} {int(m.sum()):>7} {cols.min():>5} {cols.max():>5} "
              f"{cols.max() - cols.min():>6} {100 * float((good > 0.7).mean()):>7.1f}% "
              f"{np.percentile(dep, 90):>10.4f}")


def cmd_views(glb, outdir):
    os.makedirs(outdir, exist_ok=True)
    gj, blob = load_glb(glb)
    wp_tris, uv_tris, nrm_tris = gather_tris(gj, blob)
    atlas, _ = read_atlas(gj, blob, outdir)
    light = np.array([-0.35, 0.45, 0.82])
    light = light / np.linalg.norm(light)
    for name, d in (("posZ", (0, 0, 1)), ("negZ", (0, 0, -1)), ("posX", (1, 0, 0)), ("negX", (-1, 0, 0))):
        e1, e2, vd = basis_from_viewdir(d)
        mask, uv, nrm, _depth, cand = render_view(wp_tris, uv_tris, 560, 900, e1, e2, vd, nrm_tris)
        colors = atlas_lookup(atlas, uv)
        img = shade(colors, nrm, light)
        img[~mask] = 255
        p = os.path.join(outdir, f"view_{name}.png")
        Image.fromarray(img).save(p)
        print(f"  {name}: visible {int(mask.sum())} px, tris considered {cand} -> {p}")


def sample_photo(photo, h, U, V):
    """Bilinear sample the photo at photo-pixel coords given by homography h."""
    den = h[2, 0] * U + h[2, 1] * V + h[2, 2]
    X = (h[0, 0] * U + h[0, 1] * V + h[0, 2]) / den
    Y = (h[1, 0] * U + h[1, 1] * V + h[1, 2]) / den
    ph, pw = photo.shape[:2]
    x0 = np.floor(X).astype(np.int64)
    y0 = np.floor(Y).astype(np.int64)
    fx = (X - x0)[..., None]
    fy = (Y - y0)[..., None]
    x0c = np.clip(x0, 0, pw - 1)
    y0c = np.clip(y0, 0, ph - 1)
    x1c = np.clip(x0 + 1, 0, pw - 1)
    y1c = np.clip(y0 + 1, 0, ph - 1)
    p = photo.astype(np.float32)
    top = p[y0c, x0c] * (1 - fx) + p[y0c, x1c] * fx
    bot = p[y1c, x0c] * (1 - fx) + p[y1c, x1c] * fx
    out = top * (1 - fy) + bot * fy
    return np.clip(out, 0, 255).astype(np.uint8), X, Y


def shaft_rect(mask, photo_aspect):
    """Locate the shaft's flat face in the front render.

    The cap underside is a sharp step in silhouette width, so the first row
    that reaches the shaft's width marks the top. The model's shaft tapers by a
    few percent towards the base, so its bottom is not a clean width threshold
    either: instead the photo's own aspect is honoured, which both removes the
    vertical distortion and lands the photo's lower edge on the base moulding.
    """
    H, W = mask.shape
    rows_any = mask.any(axis=1)
    idx = np.nonzero(rows_any)[0]
    col_first = np.argmax(mask, axis=1)
    col_last = W - 1 - np.argmax(mask[:, ::-1], axis=1)
    width = np.where(rows_any, col_last - col_first, 0)
    mid = idx[(idx > 0.35 * H) & (idx < 0.55 * H)]
    mw = float(np.median(width[mid]))

    top = int(idx[0])
    for r in idx:
        if r > 0.02 * H and width[r] >= 0.75 * mw:
            top = int(r)
            break
    probe_end = min(top + int(0.40 * photo_aspect * mw), H - 1)
    probe = np.arange(top, max(probe_end, top + 1))
    xl = float(np.median(col_first[probe]))
    xr = float(np.median(col_last[probe]))
    w = xr - xl
    bot = int(round(top + photo_aspect * w))
    return top, bot, xl, xr, mw


def scatter(new_atlas, written, uv_sel, colors, splat=4, protect=None):
    """Paint `colors` at the texels addressed by `uv_sel`, plus a splat margin.

    `protect` is a boolean atlas mask of texels that must not be touched (the
    front face's footprint) -- the atlas is finely fragmented, so a margin of a
    few texels from one face's islands lands straight on another face's.
    """
    ah, aw = new_atlas.shape[:2]
    tx = np.clip((uv_sel[:, 0] * (aw - 1)).round().astype(np.int32), 0, aw - 1)
    ty = np.clip((uv_sel[:, 1] * (ah - 1)).round().astype(np.int32), 0, ah - 1)
    for oy in range(-splat, splat + 1):
        for ox in range(-splat, splat + 1):
            yy = np.clip(ty + oy, 0, ah - 1)
            xx = np.clip(tx + ox, 0, aw - 1)
            if protect is None:
                new_atlas[yy, xx] = colors
                written[yy, xx] = True
            else:
                keep = ~protect[yy, xx]
                if keep.any():
                    new_atlas[yy[keep], xx[keep]] = colors[keep]
                    written[yy[keep], xx[keep]] = True
    return written


def paint_face(new_atlas, written, wp, uv, nt, viewdir, src_quad, photo, tag, outdir,
               W=700, H=900, light=None, protect=None, splat=4):
    """Reproject a photo patch onto whichever face is visible from `viewdir`.

    The whole silhouette is mapped (cap and base included) -- the source is
    featureless stone, so a loose fit costs nothing and keeps this simple.
    """
    e1, e2, vd = basis_from_viewdir(viewdir, up=(0.0, 1.0, 0.0))
    mask, uvb, nrmb, _d, _c = render_view(wp, uv, W, H, e1, e2, vd, nt)
    ys, xs = np.nonzero(mask)
    top, bot = float(ys.min()), float(ys.max())
    xl, xr = float(xs.min()), float(xs.max())
    U = (xs - xl) / (xr - xl)
    V = (ys - top) / (bot - top)
    unit = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    h = homography(unit, [src_quad["LT"], src_quad["RT"], src_quad["RB"], src_quad["LB"]])
    colors, _X, _Y = sample_photo(photo, h, U.astype(np.float64), V.astype(np.float64))
    written = scatter(new_atlas, written, uvb[ys, xs], colors, splat=splat, protect=protect)
    print(f"{tag}: painted {len(ys)} px ({W}x{H} view, source quad "
          f"{src_quad['LT']} .. {src_quad['RB']})")
    if light is not None:
        img = shade(atlas_lookup(new_atlas, uvb), nrmb, light)
        img[~mask] = 245
        Image.fromarray(img).save(os.path.join(outdir, f"pass_{tag}.png"))
    return written



def cmd_retexture(glb, photo_path, out_glb, outdir, faces="front"):
    os.makedirs(outdir, exist_ok=True)
    gj, blob = load_glb(glb)
    wp_tris, uv_tris, nrm_tris = gather_tris(gj, blob)
    atlas, atlas_path = read_atlas(gj, blob, outdir)
    print(f"atlas {atlas.shape[1]}x{atlas.shape[0]} from {atlas_path}")

    photo = np.asarray(Image.open(photo_path).convert("RGB"), dtype=np.uint8)
    print(f"photo {photo.shape[1]}x{photo.shape[0]} {photo_path}")

    e1, e2, vd = basis_from_viewdir((0, 0, -1))        # camera on +Z looking towards -Z
    mask, uv, nrm, _depth, cand = render_view(wp_tris, uv_tris, FRONT_W, FRONT_H, e1, e2, vd, nrm_tris)
    unit = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    src = [PHOTO_QUAD["LT"], PHOTO_QUAD["RT"], PHOTO_QUAD["RB"], PHOTO_QUAD["LB"]]
    quad_w = float(np.hypot(src[1][0] - src[0][0], src[1][1] - src[0][1]))
    quad_h = float(np.hypot(src[3][0] - src[0][0], src[3][1] - src[0][1]))
    h = homography(unit, src)

    top, bot, xl, xr, median_w = shaft_rect(mask, quad_h / quad_w)
    print(f"front render: {int(mask.sum())} visible px, {cand} triangles considered")
    print(f"shaft rect in the front render: rows {top}..{bot} ({bot - top + 1}px), "
          f"cols {xl:.0f}..{xr:.0f} ({xr - xl + 1:.0f}px), "
          f"aspect {(bot - top + 1) / (xr - xl + 1):.3f}")
    print(f"photo quad {quad_w:.0f}x{quad_h:.0f}px aspect {quad_h / quad_w:.3f} "
          f"-> scale x {(xr - xl) / quad_w:.2f}, y {(bot - top) / quad_h:.2f}")

    ys, xs = np.nonzero(mask)
    U = (xs - xl) / (xr - xl)
    V = (ys - top) / (bot - top)
    src_colors, X, Y = sample_photo(photo, h, U.astype(np.float64), V.astype(np.float64))
    inb = (X >= 0) & (X < photo.shape[1] - 1) & (Y >= 0) & (Y < photo.shape[0] - 1)
    # the shaft rect maps to the photo quad; the cap and the base moulding are the
    # same plane a little further out, so extrapolating the homography covers them
    # (v<0 = the cap, v>1 = the base) as long as we stay inside the photo
    sel = inb & (V >= -0.13) & (V <= 1.35)
    ys, xs, src_colors = ys[sel], xs[sel], src_colors[sel]
    print(f"mapping {int(mask.sum())} visible front pixels -> photo: "
          f"{len(ys)} kept ({100 * len(ys) / int(mask.sum()):.1f}%), {int((~inb).sum())} fell outside the photo")

    mapped = np.full((FRONT_H, FRONT_W, 3), 255, dtype=np.uint8)
    mapped[ys, xs] = src_colors
    Image.fromarray(mapped).resize((FRONT_W // 2, FRONT_H // 2)).save(
        os.path.join(outdir, "front_mapped.png"))

    uv_sel = uv[ys, xs]
    ah, aw = atlas.shape[:2]

    new_atlas = atlas.copy()
    written = np.zeros((ah, aw), dtype=bool)
    written = scatter(new_atlas, written, uv_sel, src_colors)
    print(f"scattered into {int(written.sum())} atlas texels ({100 * written.mean():.2f}% of the atlas)")
    # the front pass owns these texels: the atlas packs every face into one
    # dense 4096x4096 sheet, so the other faces must not paint over them
    front_protect = written.copy()
    front_snapshot = new_atlas.copy()

    if faces == "all":
        light2 = np.array([-0.35, 0.45, 0.82])
        light2 = light2 / np.linalg.norm(light2)
        OTHER_FACES = (
            ("posX", (-1.0, 0.0, 0.0)),    # camera on +X, sees the +X side
            ("negX", (1.0, 0.0, 0.0)),     # camera on -X, sees the -X side
            ("negZ", (0.0, 0.0, 1.0)),     # camera behind, sees the back
            ("posY", (0.0, -1.0, 0.0)),    # camera above, sees the cap's top
            ("negY", (0.0, 1.0, 0.0)),     # camera below, sees the underside
        )
        for tag, d in OTHER_FACES:
            written = paint_face(new_atlas, written, wp_tris, uv_tris, nrm_tris, d,
                                 SIDE_QUAD, photo, tag, outdir, W=700, H=900, light=light2,
                                 protect=front_protect, splat=2)
        # put the front's texels back exactly as the front pass left them
        new_atlas[front_protect] = front_snapshot[front_protect]
        written |= front_protect
        print(f"restored the front footprint ({int(front_protect.sum())} texels) "
              f"after the side passes")

    new_atlas, written = fill_holes(new_atlas, written, iterations=8)
    ys_all, xs_all = np.nonzero(mask)
    uv_all = uv[ys_all, xs_all]
    txa = np.clip((uv_all[:, 0] * (aw - 1)).round().astype(np.int32), 0, aw - 1)
    tya = np.clip((uv_all[:, 1] * (ah - 1)).round().astype(np.int32), 0, ah - 1)
    print(f"painted {100 * written.mean():.2f}% of the atlas; coverage of the whole "
          f"front-visible footprint after fill: {100 * written[tya, txa].mean():.2f}%")
    out_atlas = os.path.join(outdir, "atlas_retextured.png")
    Image.fromarray(new_atlas).save(out_atlas)

    from io import BytesIO
    bio = BytesIO()
    Image.fromarray(new_atlas).save(bio, format="PNG", optimize=True)
    png = bio.getvalue()
    total = rewrite_glb(gj, blob, png, out_glb)
    print(f"wrote {out_glb} ({total} bytes, png {len(png)} bytes)")

    # verification renders from the new file
    gj2, blob2 = load_glb(out_glb)
    wp2, uv2, nrm2 = gather_tris(gj2, blob2)
    light2 = np.array([-0.35, 0.45, 0.82])
    light2 = light2 / np.linalg.norm(light2)
    e1, e2, vd = basis_from_viewdir((0, 0, -1))
    mask2, uv_b, nrm_b, _d, _c = render_view(wp2, uv2, FRONT_W, FRONT_H, e1, e2, vd, nrm2)
    colors = atlas_lookup(new_atlas, uv_b)
    img = shade(colors, nrm_b, light2)
    img[~mask2] = 255
    Image.fromarray(img).save(os.path.join(outdir, "after_front.png"))
    box = (max(int(xl) - 40, 0), max(top - 100, 0), min(int(xr) + 40, FRONT_W), min(bot + 100, FRONT_H))
    Image.fromarray(img[box[1]:box[3], box[0]:box[2]]).save(
        os.path.join(outdir, "after_front_shaft.png"))

    for name, d in (("quarter", (-0.62, -0.30, -0.72)), ("back", (0, 0, 1)), ("right", (1.0, 0, 0.15))):
        e1, e2, vd = basis_from_viewdir(d)
        mask3, uv3, nrm3, _d, _c = render_view(wp2, uv2, 700, 900, e1, e2, vd, nrm2)
        colors = atlas_lookup(new_atlas, uv3)
        img3 = shade(colors, nrm3, light2)
        img3[~mask3] = 255
        Image.fromarray(img3).save(os.path.join(outdir, f"after_{name}.png"))
    print("verification renders written")


def parse_quad(text):
    """'x1,y1 x2,y2 x3,y3 x4,y4' -> {'LT','RT','RB','LB'} (clockwise from top-left)."""
    pts = []
    for chunk in text.replace(";", " ").split():
        x, y = chunk.replace(",", " ").split()
        pts.append((float(x), float(y)))
    if len(pts) != 4:
        raise SystemExit(f"need 4 'x,y' points (LT RT RB LB), got {len(pts)}: {text!r}")
    return dict(zip(("LT", "RT", "RB", "LB"), pts))


def take_options(argv):
    """Pull the optional overrides out of argv and patch the module constants."""
    global ATLAS_SIZE, FRONT_W, FRONT_H, PHOTO_QUAD, SIDE_QUAD
    rest = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--photo-quad", "--side-quad", "--atlas-size", "--front-w", "--front-h"):
            if i + 1 >= len(argv):
                raise SystemExit(f"{arg} needs a value")
            value = argv[i + 1]
            if arg == "--photo-quad":
                PHOTO_QUAD = parse_quad(value)
            elif arg == "--side-quad":
                SIDE_QUAD = parse_quad(value)
            elif arg == "--atlas-size":
                ATLAS_SIZE = int(value)
            elif arg == "--front-w":
                FRONT_W = int(value)
            else:
                FRONT_H = int(value)
            i += 2
            continue
        rest.append(arg)
        i += 1
    return rest


if __name__ == "__main__":
    argv = take_options(sys.argv[1:])
    if not argv:
        raise SystemExit(__doc__)
    cmd = argv[0]
    args = argv[1:]
    if cmd == "views":
        cmd_views(args[0], args[1])
    elif cmd == "profile":
        cmd_profile(args[0], args[1])
    elif cmd == "retexture":
        faces = args[4] if len(args) > 4 else "front"
        cmd_retexture(args[0], args[1], args[2], args[3], faces)
    else:
        raise SystemExit(f"unknown subcommand {cmd}")
