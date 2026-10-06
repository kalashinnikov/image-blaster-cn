#!/usr/bin/env python3
"""Structural check of a GLB produced by a retexture run.

Verifies the container (magic / version / declared length), the BIN chunk size,
every bufferView (4-byte alignment, no overrun of the BIN chunk) and every
accessor, then reports images, mesh primitives and materials.

Usage:
    python check_glb.py <file.glb>
"""
import json
import struct
import sys

TYPE_SIZE = {"SCALAR": 4, "VEC2": 8, "VEC3": 12, "VEC4": 16}


def check(path):
    with open(path, "rb") as fh:
        blob = fh.read()

    magic, version, declared = struct.unpack_from("<III", blob, 0)
    offset, chunks = 12, []
    while offset < len(blob):
        length, kind = struct.unpack_from("<II", blob, offset)
        chunks.append((kind, offset + 8, length))
        offset += 8 + length

    gltf = json.loads(blob[chunks[0][1]:chunks[0][1] + chunks[0][2]].decode("utf-8"))
    bin_length = chunks[1][2] if len(chunks) > 1 else 0

    print(f"magic={magic:#x} version={version} declared={declared} actual={len(blob)} bin_chunk={bin_length}")
    ok = magic == 0x46546C67 and version == 2 and declared == len(blob)

    print(f"buffers: {[(b.get('byteLength'), b.get('uri')) for b in gltf.get('buffers', [])]}")
    problems = []
    for i, view in enumerate(gltf.get("bufferViews", [])):
        start, length = view.get("byteOffset", 0), view["byteLength"]
        if start % 4:
            problems.append((i, "unaligned byteOffset", start))
        if start + length > bin_length:
            problems.append((i, "overruns BIN chunk", start + length))
    print(f"bufferViews: {len(gltf.get('bufferViews', []))}  problems: {problems or 'none'}")
    ok = ok and not problems

    problems = []
    for i, acc in enumerate(gltf.get("accessors", [])):
        view = gltf["bufferViews"][acc["bufferView"]]
        need = acc.get("byteOffset", 0) + acc["count"] * TYPE_SIZE.get(acc["type"], 4)
        if need > view["byteLength"]:
            problems.append((i, acc["type"], acc["count"], view["byteLength"]))
    print(f"accessors: {len(gltf.get('accessors', []))}  problems: {problems or 'none'}")
    ok = ok and not problems

    images = gltf.get("images", [])
    print("images: " + str([(im.get("mimeType"),
                             gltf["bufferViews"][im["bufferView"]]["byteLength"]) for im in images]))
    prims = [(p.get("mode"), sorted(p["attributes"])) for m in gltf.get("meshes", []) for p in m["primitives"]]
    print(f"mesh primitives: {prims}")
    print(f"materials: {[m.get('name') for m in gltf.get('materials', [])]}")

    print("RESULT:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def main(argv):
    if len(argv) != 1:
        raise SystemExit(__doc__)
    return check(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
