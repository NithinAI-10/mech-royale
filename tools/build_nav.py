"""Offline: rasterize the static map into navigation grids shipped with the bot.

Writes nav.bin (zlib): header + uint8 grids at CELL metres:
  blocked  : 1 where a mech (radius ~2.5 m + margin) cannot stand
  solid    : height (m, clipped 0..255) of the tallest solid over ground in the cell (cover / sight)
  ground   : terrain height in decimetres + 200 as uint16 (for pitch aim)
"""
import json
import sys
import zlib

import numpy as np

KIT = sys.argv[1]
OUT = sys.argv[2]
CELL = 2.0

w = json.load(open(f"{KIT}/arena/v2/world.json"))
t = w["terrain"]
nx, ny = t["nx"], t["ny"]
H = np.fromfile(f"{KIT}/arena/v2/terrain.i16", dtype="<i2").reshape(ny, nx) / 100.0
b = w["bounds"]
minx, miny = t["minX"], t["minY"]
GX = int(np.ceil((t["minX"] + (nx - 1) - minx) / CELL))
GY = int(np.ceil((t["minY"] + (ny - 1) - miny) / CELL))
xs = minx + (np.arange(GX) + 0.5) * CELL
ys = miny + (np.arange(GY) + 0.5) * CELL
X, Y = np.meshgrid(xs, ys)


def ground(xq, yq):
    fx = np.clip((xq - minx), 0, nx - 1.001)
    fy = np.clip((yq - miny), 0, ny - 1.001)
    i = fx.astype(int)
    j = fy.astype(int)
    ax = fx - i
    ay = fy - j
    return (H[j, i] * (1 - ax) * (1 - ay) + H[j, i + 1] * ax * (1 - ay) + H[j + 1, i] * (1 - ax) * ay + H[j + 1, i + 1] * ax * ay)


G = ground(X, Y)


def pip(px, py, ring):
    """vectorized even-odd point in polygon; ring flat list"""
    xs_ = np.array(ring[0::2])
    ys_ = np.array(ring[1::2])
    inside = np.zeros(px.shape, dtype=bool)
    n = len(xs_)
    for k in range(n):
        x1, y1 = xs_[k], ys_[k]
        x2, y2 = xs_[(k + 1) % n], ys_[(k + 1) % n]
        cond = (y1 > py) != (y2 > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (py - y1) / (y2 - y1) + x1
        inside ^= cond & (px < xint)
    return inside


blocked = np.zeros((GY, GX), dtype=bool)
solid = np.zeros((GY, GX), dtype=np.float32)
for p in w["prisms"]:
    ring = p["ring"]
    rx = ring[0::2]
    ry = ring[1::2]
    i0 = max(int((min(rx) - minx) / CELL) - 1, 0)
    i1 = min(int((max(rx) - minx) / CELL) + 2, GX)
    j0 = max(int((min(ry) - miny) / CELL) - 1, 0)
    j1 = min(int((max(ry) - miny) / CELL) + 2, GY)
    if i0 >= i1 or j0 >= j1:
        continue
    sx, sy = X[j0:j1, i0:i1], Y[j0:j1, i0:i1]
    ins = pip(sx, sy, ring)
    for hole in p.get("holes") or []:
        if hole:
            ins &= ~pip(sx, sy, hole)
    g = G[j0:j1, i0:i1]
    # obstructs a standing mech: solid between feet+step and head
    obstruct = ins & (p["top"] > g + 1.2) & (p["base"] < g + 9.0)
    blocked[j0:j1, i0:i1] |= obstruct
    hgt = np.where(ins & (p["base"] < g + 9.0), p["top"] - g, 0)
    solid[j0:j1, i0:i1] = np.maximum(solid[j0:j1, i0:i1], hgt)

# slopes: per cell, max height change to a neighbour over CELL
gpad = np.pad(G, 1, mode="edge")
steep = np.zeros_like(blocked)
for dj, di in ((0, 1), (1, 0), (0, -1), (-1, 0)):
    nb = gpad[1 + dj:1 + dj + GY, 1 + di:1 + di + GX]
    steep |= np.abs(nb - G) / CELL > np.tan(np.radians(50))
blocked |= steep
# map margin (edge is a wall)
blocked[:2, :] = blocked[-2:, :] = True
blocked[:, :2] = blocked[:, -2:] = True

# inflate by mech radius 2.5 m (+0.5 margin): 2 cells
infl = blocked.copy()
for dj in range(-2, 3):
    for di in range(-2, 3):
        if dj * dj + di * di <= 4:
            infl |= np.roll(np.roll(blocked, dj, 0), di, 1)

# cover score per cell: how many of 16 directions hit a tall solid (>= 8 m) within 14 m
tall = solid >= 8.0
cover = np.zeros((GY, GX), dtype=np.uint8)
for k in range(16):
    a = 2 * np.pi * k / 16
    hit = np.zeros((GY, GX), dtype=bool)
    for r in (3.0, 5.0, 7.0, 9.0, 11.0, 14.0):
        di = int(round(np.cos(a) * r / CELL))
        dj = int(round(np.sin(a) * r / CELL))
        hit |= np.roll(np.roll(tall, -dj, 0), -di, 1)
    cover += hit.astype(np.uint8)
cover[infl] = 0

print("grid", GX, GY, "raw blocked", blocked.mean().round(3), "inflated", infl.mean().round(3))
hdr = np.array([GX, GY], dtype="<i4").tobytes() + np.array([minx, miny, CELL], dtype="<f8").tobytes()
gq = np.clip(np.round(G * 10) + 200, 0, 65535).astype("<u2")
blob = hdr + infl.astype(np.uint8).tobytes() + np.clip(solid, 0, 255).astype(np.uint8).tobytes() + gq.tobytes() + cover.tobytes()
print("cover>=6:", (cover >= 6).sum(), "cover>=9:", (cover >= 9).sum())
open(OUT, "wb").write(zlib.compress(blob, 9))
print("wrote", OUT, len(zlib.compress(blob, 9)))
# preview image for sanity
try:
    img = np.where(infl, 0, 255).astype(np.uint8)[::-1]
    with open(OUT + ".pgm", "wb") as f:
        f.write(f"P5 {GX} {GY} 255\n".encode() + img.tobytes())
except Exception:
    pass
