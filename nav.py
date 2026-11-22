"""Navigation over a precomputed walkability grid (built offline from the static map).

Incremental A*: plan() starts a search, step(budget) expands up to `budget` nodes per call so a
search can be spread over several 50 ms decisions.
"""
import heapq
import math
import os
import struct
import zlib

SQ2 = math.sqrt(2.0)
NEIGH = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, SQ2), (1, -1, SQ2), (-1, 1, SQ2), (-1, -1, SQ2))


class Grid:
    def __init__(self, path=None):
        path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "nav.bin")
        blob = zlib.decompress(open(path, "rb").read())
        self.gx, self.gy = struct.unpack_from("<ii", blob, 0)
        self.minx, self.miny, self.cell = struct.unpack_from("<ddd", blob, 8)
        n = self.gx * self.gy
        off = 32
        self.blocked = bytearray(blob[off:off + n])
        off += n
        self.solid = blob[off:off + n]
        off += n
        self.ground = struct.unpack_from(f"<{n}H", blob, off)
        off += 2 * n
        self.cover = blob[off:off + n] if len(blob) >= off + n else bytes(n)

    # --- coordinates
    def cell_of(self, x, y):
        i = int((x - self.minx) / self.cell)
        j = int((y - self.miny) / self.cell)
        return min(max(i, 0), self.gx - 1), min(max(j, 0), self.gy - 1)

    def center(self, i, j):
        return self.minx + (i + 0.5) * self.cell, self.miny + (j + 0.5) * self.cell

    def free(self, i, j):
        return 0 <= i < self.gx and 0 <= j < self.gy and not self.blocked[j * self.gx + i]

    def free_xy(self, x, y):
        i, j = self.cell_of(x, y)
        return self.free(i, j)

    def ground_at(self, x, y):
        i, j = self.cell_of(x, y)
        return (self.ground[j * self.gx + i] - 200) / 10.0

    def solid_at(self, x, y):
        i, j = self.cell_of(x, y)
        return self.solid[j * self.gx + i]

    def best_cover(self, cx, cy, r, pref, inside):
        """free cell within r m of (cx, cy) with the most cover, tie-broken by distance to pref;
        inside(x, y) -> bool restricts candidates"""
        c = self.cell
        i0, j0 = self.cell_of(cx - r, cy - r)
        i1, j1 = self.cell_of(cx + r, cy + r)
        best = None
        gx = self.gx
        st = 2 if r <= 60 else 4
        for j in range(j0, j1 + 1, st):
            for i in range(i0, i1 + 1, st):
                k = j * gx + i
                if self.blocked[k]:
                    continue
                sc = self.cover[k]
                if sc < 4:
                    continue
                x, y = self.minx + (i + 0.5) * c, self.miny + (j + 0.5) * c
                if (x - cx) ** 2 + (y - cy) ** 2 > r * r or not inside(x, y):
                    continue
                v = sc - 0.04 * math.hypot(x - pref[0], y - pref[1])
                if best is None or v > best[0]:
                    best = (v, x, y, sc)
        return best

    def nearest_free(self, i, j, maxr=30):
        if self.free(i, j):
            return i, j
        for r in range(1, maxr):
            best = None
            for di in range(-r, r + 1):
                for dj in (-r, r):
                    if self.free(i + di, j + dj):
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, i + di, j + dj)
            for dj in range(-r + 1, r):
                for di in (-r, r):
                    if self.free(i + di, j + dj):
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, i + di, j + dj)
            if best:
                return best[1], best[2]
        return None

    def los(self, x0, y0, x1, y1):
        """walkable straight line between two points (sampled every half cell)"""
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / (self.cell * 0.5)))
        gx = self.gx
        b = self.blocked
        for k in range(n + 1):
            t = k / n
            i = int((x0 + (x1 - x0) * t - self.minx) / self.cell)
            j = int((y0 + (y1 - y0) * t - self.miny) / self.cell)
            if i < 0 or j < 0 or i >= gx or j >= self.gy or b[j * gx + i]:
                return False
        return True

    def sight(self, x0, y0, z0, x1, y1, z1):
        """rough line of sight against building heights (solid grid)"""
        d = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(d / self.cell))
        gx = self.gx
        for k in range(1, n):
            t = k / n
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            i = int((x - self.minx) / self.cell)
            j = int((y - self.miny) / self.cell)
            if i < 0 or j < 0 or i >= gx or j >= self.gy:
                return False
            s = self.solid[j * gx + i]
            if s:
                top = (self.ground[j * gx + i] - 200) / 10.0 + s
                if top > z0 + (z1 - z0) * t:
                    return False
        return True


class Search:
    """Incremental A* from start cell to goal cell."""

    def __init__(self, grid, start, goal):
        self.g = grid
        self.start = start
        self.goal = goal
        self.done = False
        self.path = None
        gi, gj = goal
        self.gi, self.gj = gi, gj
        sid = start[1] * grid.gx + start[0]
        self.cost = {sid: 0.0}
        self.came = {sid: -1}
        self.open = [(self._h(*start), 0.0, sid)]
        self.closed = set()
        self.best = (self._h(*start), sid)  # closest node seen (fallback)

    def _h(self, i, j):
        dx = abs(i - self.gi)
        dy = abs(j - self.gj)
        return (dx + dy) + (SQ2 - 2) * min(dx, dy)

    def step(self, budget):
        if self.done:
            return True
        g = self.g
        gx, gy = g.gx, g.gy
        blocked = g.blocked
        open_ = self.open
        cost = self.cost
        came = self.came
        closed = self.closed
        goal_id = self.gj * gx + self.gi
        n = 0
        while open_ and n < budget:
            f, c, nid = heapq.heappop(open_)
            if nid in closed:
                continue
            closed.add(nid)
            n += 1
            if nid == goal_id:
                self._finish(nid)
                return True
            i = nid % gx
            j = nid // gx
            h = f - c
            if h < self.best[0]:
                self.best = (h, nid)
            for di, dj, w in NEIGH:
                ni = i + di
                nj = j + dj
                if ni < 0 or nj < 0 or ni >= gx or nj >= gy:
                    continue
                mid = nj * gx + ni
                if blocked[mid]:
                    continue
                if di and dj and (blocked[j * gx + ni] or blocked[nj * gx + i]):
                    continue  # no corner cutting
                nc = c + w
                if nc < cost.get(mid, 1e18):
                    cost[mid] = nc
                    came[mid] = nid
                    heapq.heappush(open_, (nc + self._h(ni, nj), nc, mid))
        if not open_:
            # unreachable: go to the closest point we found
            self._finish(self.best[1])
            return True
        return False

    def _finish(self, nid):
        gx = self.g.gx
        cells = []
        while nid != -1:
            cells.append((nid % gx, nid // gx))
            nid = self.came[nid]
        cells.reverse()
        self.path = [self.g.center(i, j) for i, j in cells]
        self.done = True
