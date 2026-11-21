"""Tiny kinematic simulator for testing navigation + zone play (no enemies, no physics of jumps).

Walls = inflated nav grid cells (the mech centre cannot enter them); moves slide along walls.
Usage: python sim.py N_MATCHES SEED
"""
import gzip
import json
import math
import random
import sys
import time

sys.path.insert(0, "/home/claude/mech/bot")
import bot as B  # noqa: E402
from nav import Grid  # noqa: E402

KIT = "/home/claude/mech/kit/mechfight-kit"
hello = json.loads(gzip.open(f"{KIT}/check/stream.jsonl.gz", "rt").readline())
cfg = hello["config"]
G = Grid()


def rand_free(rng, cx, cy, r):
    while True:
        a = rng.random() * 2 * math.pi
        d = r * math.sqrt(rng.random())
        x, y = cx + d * math.cos(a), cy + d * math.sin(a)
        if G.minx + 40 < x < G.minx + G.gx * 2 - 40 and G.miny + 40 < y < G.miny + G.gy * 2 - 40 and G.free_xy(x, y):
            return x, y


def run(seed):
    rng = random.Random(seed)
    z = cfg["zone"]
    phases = z["phases"]
    centers = [list(z["start_center"])]
    radii = [z["start_radius"]]
    for ph in phases:
        cx, cy = centers[-1]
        r = radii[-1]
        nr = ph["radius"]
        while True:
            a = rng.random() * 2 * math.pi
            d = (r - nr) * rng.random()
            nx_, ny_ = cx + d * math.cos(a), cy + d * math.sin(a)
            if G.free_xy(nx_, ny_):
                break
        centers.append([nx_, ny_])
        radii.append(nr)
    x, y = rand_free(rng, z["start_center"][0], z["start_center"][1], z["start_radius"] * 0.8)
    vx = vy = 0.0
    hp = 1400.0
    h = dict(hello)
    h["slot"] = seed % 16
    bot = B.Bot(h)
    dt = 0.1
    t = 0.0
    out_time = 0.0
    worst = 0.0
    stuck = 0
    phase_t = 0.0
    k = 0
    while t < 470 and hp > 0:
        # zone state
        hold, shrink = phases[k]["hold_s"], phases[k]["shrink_s"]
        ss, se = phase_t + hold, phase_t + hold + shrink
        if t >= se and k < len(phases) - 1:
            phase_t = se
            k += 1
            continue
        frac = min(max((t - ss) / shrink, 0), 1)
        cc, nc = centers[k], centers[k + 1]
        ccx = cc[0] + (nc[0] - cc[0]) * frac
        ccy = cc[1] + (nc[1] - cc[1]) * frac
        r = radii[k] + (radii[k + 1] - radii[k]) * frac
        in_zone = math.hypot(x - ccx, y - ccy) <= r
        if not in_zone:
            hp -= phases[k]["dps"] * dt
            out_time += dt
        obs = {
            "type": "obs", "tick": int(t * 20), "t": t, "alive": 16,
            "self": {"id": h["slot"], "pos": [x, y, G.ground_at(x, y)], "vel": [vx, vy, 0], "aim": [0, 0], "hp": hp,
                     "max_hp": 1400, "energy": 100, "max_energy": 100, "grounded": True, "cubes": 0, "upgrades": [],
                     "weapons": [{"kind": "gun", "cooldown": 0, "ammo": 12, "reload": 0, "stats": cfg["weapons"]["gun"]}, None],
                     "in_zone": in_zone, "stats": dict(cfg["mech"])},
            "mechs": [], "projectiles": [], "crates": [], "cubes": [], "pickups": [], "sounds": [], "beams": [], "events": [],
            "drops": [], "offer": None,
            "zone": {"center": cc if t < ss else [ccx, ccy], "radius": radii[k] if t < ss else r, "next_center": nc,
                     "next_radius": radii[k + 1], "shrink_start": ss, "shrink_end": se, "dps": phases[k]["dps"]},
        }
        if t >= ss:
            obs["zone"]["center"] = cc
            obs["zone"]["radius"] = radii[k]
        t0 = time.perf_counter()
        a = bot.act(obs)
        worst = max(worst, time.perf_counter() - t0)
        mv = a.get("move", [0, 0])
        sp = cfg["mech"]["boost_speed"] if a.get("boost") else cfg["mech"]["walk_speed"]
        tvx, tvy = mv[0] * sp, mv[1] * sp
        acc = cfg["mech"]["ground_accel"] * dt
        dvx, dvy = tvx - vx, tvy - vy
        n = math.hypot(dvx, dvy)
        if n > acc:
            dvx, dvy = dvx / n * acc, dvy / n * acc
        vx += dvx
        vy += dvy
        nx_, ny_ = x + vx * dt, y + vy * dt
        if G.free_xy(nx_, ny_):
            x, y = nx_, ny_
        elif G.free_xy(nx_, y):
            x, vy = nx_, 0.0
        elif G.free_xy(x, ny_):
            y, vx = ny_, 0.0
        else:
            vx = vy = 0.0
            if math.hypot(*mv) > 0.3:
                stuck += 1
        t += dt
    return {"seed": seed, "survived_s": round(t, 1), "hp": round(hp), "out_s": round(out_time, 1), "worst_ms": round(worst * 1000, 1), "blocked_ticks": stuck}


if __name__ == "__main__":
    n, s0 = int(sys.argv[1]), int(sys.argv[2])
    res = [run(s0 + i) for i in range(n)]
    for r in res:
        print(r)
    print("mean out_s", sum(r["out_s"] for r in res) / n, "max worst_ms", max(r["worst_ms"] for r in res), "died", sum(r["hp"] <= 0 for r in res))
