"""Probe a GLB: texture, UV layout, and which triangles face which way.

Read-only reconnaissance for the retexture spike. Prints a report and writes
`atlas_front_overlay.png` (downscaled atlas with the front-facing UV triangles
outlined) next to the output dir.
"""
import json
import os
import struct
import sys

import numpy as np
from PIL import Image, ImageDraw

CT_DTYPE = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def load_glb(path):
    with open(path, "rb") as fh:
        blob = fh.read()
    magic, version, length = struct.unpack_from("<4sII", blob, 0)
    if magic != b"glTF":
        raise SystemExit(f"not a glb: magic={magic!r}")
    chunks = []
    off = 12
    while off < len(blob):
        clen, ctype = struct.unpack_from("<II", blob, off)
        chunks.append((ctype, blob[off + 8:off + 8 + clen]))
        off += 8 + clen
    gj, bin_blob = None, None
    for ctype, cdata in chunks:
        if ctype == 0x4E4F534A:
            gj = json.loads(cdata.decode("utf-8"))
        elif ctype == 0x004E4942:
            bin_blob = cdata
    print(f"glb: version={version} declared_len={length} actual_len={len(blob)}")
    print("chunks: " + ", ".join(f"{hex(c)}:{len(d)}" for c, d in chunks))
    return gj, bin_blob


def read_accessor(gj, bin_blob, index):
    acc = gj["accessors"][index]
    bv = gj["bufferViews"][acc["bufferView"]]
    dtype = np.dtype(CT_DTYPE[acc["componentType"]])
    n = NC[acc["type"]]
    count = acc["count"]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or dtype.itemsize * n
    if stride == dtype.itemsize * n:
        arr = np.frombuffer(bin_blob, dtype=dtype, count=count * n, offset=base).reshape(count, n)
    else:
        arr = np.lib.stride_tricks.as_strided(
            np.frombuffer(bin_blob, dtype=dtype, offset=base),
            shape=(count, n),
            strides=(stride, dtype.itemsize),
        ).copy()
    return arr.astype(np.float64) if dtype == np.float32 else arr


def trs_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    t = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    R = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])
    m = np.eye(4)
    m[:3, :3] = R @ np.diag(s)
    m[:3, 3] = t
    return m


def world_matrices(gj):
    out = {}

    def walk(idx, parent):
        node = gj["nodes"][idx]
        m = parent @ trs_matrix(node)
        if "mesh" in node:
            out.setdefault(node["mesh"], []).append((idx, m))
        for child in node.get("children", []):
            walk(child, m)

    for root in gj.get("scenes", [{}])[gj.get("scene", 0)].get("nodes", []):
        walk(root, np.eye(4))
    return out


def transform_points(m, pts):
    ph = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
    return (ph @ m.T)[:, :3]


def main():
    glb = sys.argv[1]
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    gj, bin_blob = load_glb(glb)

    print(f"\nnodes={len(gj.get('nodes', []))} meshes={len(gj.get('meshes', []))} "
          f"materials={len(gj.get('materials', []))} images={len(gj.get('images', []))} "
          f"textures={len(gj.get('textures', []))}")
    for i, mat in enumerate(gj.get("materials", [])):
        pbr = mat.get("pbrMetallicRoughness", {})
        bct = pbr.get("baseColorTexture", {}).get("index")
        print(f"  material[{i}] name={mat.get('name')!r} baseColorTex={bct} "
              f"metallic={pbr.get('metallicFactor')} roughness={pbr.get('roughnessFactor')} "
              f"normalTex={mat.get('normalTexture', {}).get('index')}")

    atlas_paths = []
    for i, img in enumerate(gj.get("images", [])):
        mime = img.get("mimeType", "?")
        if "bufferView" in img:
            bv = gj["bufferViews"][img["bufferView"]]
            start = bv.get("byteOffset", 0)
            payload = bin_blob[start:start + bv["byteLength"]]
        else:
            uri = img["uri"]
            if uri.startswith("data:"):
                import base64
                payload = base64.b64decode(uri.split(",", 1)[1])
            else:
                payload = open(os.path.join(os.path.dirname(glb), uri), "rb").read()
        ext = ".png" if "png" in mime else ".jpg"
        p = os.path.join(outdir, f"atlas_{i}{ext}")
        with open(p, "wb") as fh:
            fh.write(payload)
        im = Image.open(p)
        print(f"  image[{i}] mime={mime} bytes={len(payload)} size={im.size} mode={im.mode} -> {p}")
        atlas_paths.append((p, im, gj.get("textures", [{}])[i].get("source")))

    wm = world_matrices(gj)
    inv = {v[0][0]: k for k, v in wm.items()}

    total_tris = 0
    bins = {}
    front_tris = []          # (uv_tri, world_tri) per axis bin
    uv_min = np.array([np.inf, np.inf])
    uv_max = np.array([-np.inf, -np.inf])

    for mesh_index, placements in wm.items():
        mesh = gj["meshes"][mesh_index]
        for node_index, m in placements:
            for pi, prim in enumerate(mesh["primitives"]):
                pos = read_accessor(gj, bin_blob, prim["attributes"]["POSITION"])
                uv = read_accessor(gj, bin_blob, prim["attributes"]["TEXCOORD_0"]) if "TEXCOORD_0" in prim["attributes"] else None
                idx = read_accessor(gj, bin_blob, prim["indices"]).reshape(-1).astype(np.int64) if "indices" in prim else np.arange(len(pos))
                wp = transform_points(m, pos)
                tris = idx.reshape(-1, 3)
                total_tris += len(tris)
                if uv is not None:
                    uv_min = np.minimum(uv_min, uv.min(axis=0))
                    uv_max = np.maximum(uv_max, uv.max(axis=0))
                a, b, c = wp[tris[:, 0]], wp[tris[:, 1]], wp[tris[:, 2]]
                n = np.cross(b - a, c - a)
                area = 0.5 * np.linalg.norm(n, axis=1)
                keep = area > 1e-12
                nrm = np.zeros_like(n)
                nrm[keep] = n[keep] / (2 * area[keep])[:, None]
                axis = np.argmax(np.abs(nrm), axis=1)
                sign = np.sign(nrm[np.arange(len(nrm)), axis])
                key = np.where(keep, axis * 2 + (sign < 0).astype(int), -1)
                for k in np.unique(key):
                    sel = key == k
                    bins.setdefault(k, 0.0)
                    bins[k] += float(area[sel].sum())
                    if k >= 0 and uv is not None:
                        front_tris.append(("primsplit", mesh_index, node_index, pi, k, tris[sel], uv, wp, area[sel]))

    axis_name = {0: "X", 1: "Y", 2: "Z"}
    print(f"\ntotal triangles: {total_tris}")
    print(f"uv range: min={uv_min.round(4).tolist()} max={uv_max.round(4).tolist()}")
    print("area by facing (normal axis, sign):")
    for k in sorted(bins, key=lambda k: -bins[k]):
        label = "degenerate" if k < 0 else f"{'-' if k % 2 else '+'}{axis_name[k // 2]}"
        print(f"  {label:>3}  area={bins[k]:.4f}  ({100 * bins[k] / sum(bins.values()):5.1f}%)")

    if not front_tris:
        print("no UVs on any primitive - aborting overlay")
        return

    biggest = max(bins, key=lambda k: bins[k])
    print(f"\nlargest facing bin: {'-' if biggest % 2 else '+'}{axis_name[biggest // 2]}")

    for label_k in (biggest,):
        recs = [r for r in front_tris if r[4] == label_k]
        if not recs:
            continue
        tri_count = sum(len(r[5]) for r in recs)
        uvs = np.concatenate([r[6][r[5].reshape(-1)] for r in recs])
        pts = np.concatenate([r[7] for r in recs])
        print(f"  front bin triangles: {tri_count}")
        print(f"  front bin uv bbox: {uvs.min(axis=0).round(4).tolist()} .. {uvs.max(axis=0).round(4).tolist()}")
        print(f"  front bin world bbox: {pts.min(axis=0).round(4).tolist()} .. {pts.max(axis=0).round(4).tolist()}")

        # overlay on the atlas (glTF: uv (0,0) is the top-left of the image)
        path, im, _ = atlas_paths[0]
        scale = min(1.0, 1024 / max(im.size))
        canvas = im.convert("RGB").resize(
            (max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS
        )
        draw = ImageDraw.Draw(canvas)
        w, h = canvas.size
        drawn = 0
        for rec in recs:
            tris, uv = rec[5], rec[6]
            for t in tris:
                if drawn >= 4000:
                    break
                xy = [(float(uv[vi][0]) * w, float(uv[vi][1]) * h) for vi in t]
                draw.line([xy[0], xy[1], xy[2], xy[0]], fill=(255, 0, 0), width=1)
                drawn += 1
            if drawn >= 4000:
                break
        out = os.path.join(outdir, "atlas_front_overlay.png")
        canvas.save(out)
        print(f"  overlay written: {out} ({canvas.size})")

        used = np.unique(np.round(uvs, 4), axis=0)
        print(f"  distinct uv verts in front bin: {len(used)}")

        # separate UV islands: cluster triangles whose uv verts are near-equal
        parent = {}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        keys = [tuple(np.round(v, 4)) for v in np.unique(np.round(uvs, 4), axis=0)]
        for key in keys:
            parent[key] = key
        for rec in recs:
            tris, uv = rec[5], rec[6]
            for t in tris:
                a, b, c = (tuple(np.round(uv[vi], 4)) for vi in t)
                union(a, b)
                union(b, c)
        groups = {}
        for key in keys:
            groups.setdefault(find(key), []).append(key)
        sizes = sorted((len(v) for v in groups.values()), reverse=True)
        print(f"  uv islands in front bin: {len(groups)} (vertex counts, top 8: {sizes[:8]})")
        for root, members in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:3]:
            arr = np.array(members)
            print(f"    island of {len(members)} verts: uv {arr.min(axis=0).round(4).tolist()} .. {arr.max(axis=0).round(4).tolist()}")


if __name__ == "__main__":
    main()
