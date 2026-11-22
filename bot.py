"""Mech Royale placement bot.

Collect power cubes in clusters and fill the second equipment slot along the
way. Hold a short memory of visible loot so camera turns do not reset the route.
"""
import math
import random
import time

import mechfight as mf
from nav import Grid, Search

TAU = 2 * math.pi
UPGRADE_PRIORITY = [
    "plating", "armor", "gun_damage", "laser_damage", "nanites", "lightning_efficiency", "gun_rate",
    "gun_mag", "lightning_range", "laser_charge", "servos", "gun_velocity", "gyros", "sensors",
    "reactor", "laser_pierce", "lightning_chain", "capacitor", "jets", "mortar_reload",
    "mortar_splash", "mortar_velocity",
]
GOOD_WEAPONS = {"lightning": 3, "laser": 2}
FARM_RANGE = 120  # m: drones worth walking to
FARM_HP = 0.6  # farm crates and drones only above this health fraction
REST_BELOW = 0.6  # stop and regenerate below this when nothing is in view


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def d2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Bot:
    name = "survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello["slot"]
        random.seed(hello.get("seed", 0))
        self.grid = Grid()
        w = self.cfg["weapons"]
        self.gun_speed = w["gun"]["speed"]
        self.walk = self.cfg["mech"]["walk_speed"]
        self.step_speed = self.cfg["sound"]["step_speed"]
        self.reach = self.cfg["loot"]["pickup_radius"]
        self.search = None
        self.path = None
        self.path_goal = None
        self.path_time = -99
        self.goal = None
        self.last_pos = None
        self.stuck_t = 0.0
        self.unstick_until = -1
        self.unstick_dir = None
        self.attackers = {}  # id -> (time, bearing)
        self.last_hit_bearing = None
        self.last_hit_time = -99
        self.strafe = 1 if self.slot % 2 else -1
        self.strafe_t = 0
        self.scan_yaw = None
        self.heard = None  # (time, bearing)
        self.blacklist = {}  # loot id -> until time
        self.crate_target = None
        self.t = 0.0
        self.loot_memory = {}

    # ------------------------------------------------------------ zone
    def zone_target(self, obs, pos):
        z = obs["zone"]
        nc, nr = z["next_center"], z["next_radius"]
        cc, cr = z["center"], z["radius"]
        t = obs["t"]
        late = nr <= 70
        margin = max(8.0, min(0.45 * nr, 45.0)) if not late else max(4.0, 0.5 * nr)
        inner = max(nr - margin, 0.0)
        dn = d2(pos, nc)
        if dn <= inner and not late:
            return None, 0.0  # safe where we are
        if dn < 1e-6:
            tgt = list(nc)
        else:
            k = inner / dn
            tgt = [nc[0] + (pos[0] - nc[0]) * k, nc[1] + (pos[1] - nc[1]) * k]
        # urgency: how soon must we be there
        dist = d2(pos, tgt)
        travel = dist / max(self.walk, 1)
        before = z["shrink_start"] - t
        if not obs["self"]["in_zone"]:
            urg = 1.0
        elif t >= z["shrink_start"]:
            # shrinking: stay well inside the moving circle
            frac = (t - z["shrink_start"]) / max(z["shrink_end"] - z["shrink_start"], 1e-3)
            frac = min(max(frac, 0), 1)
            ccx = cc[0] + (nc[0] - cc[0]) * frac
            ccy = cc[1] + (nc[1] - cc[1]) * frac
            r = cr + (nr - cr) * frac
            slack = r - math.hypot(pos[0] - ccx, pos[1] - ccy)
            urg = 1.0 if slack < 40 else 0.6
        else:
            urg = 1.0 if before < travel * 2.2 + 12 else (0.5 if before < travel * 4 + 25 else 0.3)
        return tgt, urg

    def nearby_drop(self, obs, pos, mechs):
        """Choose a short, unobstructed detour to an uncontested supply ring."""
        zone = obs["zone"]
        quiet_speed = min(self.walk, self.step_speed - 0.6)
        best = None
        best_time = math.inf
        for drop in obs.get("drops", []):
            xy = drop["pos"][:2]
            radius = drop["radius"]
            if drop["holder"] not in (None, self.slot):
                continue
            if d2(xy, zone["next_center"]) + radius > zone["next_radius"]:
                continue
            if any(d2(m["pos"], xy) < 90 for m in mechs):
                continue
            distance = d2(pos, xy)
            travel = max(0.0, distance - radius / 2) / max(quiet_speed, 0.1)
            remaining = drop["capture_s"] - (drop["progress"] if drop["holder"] == self.slot else 0)
            finish = max(drop["lands_in"], travel) + remaining
            if finish > 12 or finish >= best_time or obs["t"] + finish >= zone["shrink_end"]:
                continue
            if not self.grid.los(pos[0], pos[1], *xy):
                continue
            best_time = finish
            best = drop
        return best

    # ------------------------------------------------------------ navigation
    def reward_goal(self, obs, pos):
        """Value the reachable cube stacks and equipment using travel time."""
        slot = obs["self"]["weapons"][1]
        rank = GOOD_WEAPONS.get(slot["kind"], 0) if slot else 0
        # Items leave the observation when the camera turns. Keep a short
        # memory, and clear a remembered item when its pickup spot is empty.
        now = obs["t"]
        visible = {item["id"] for item in obs["pickups"] + obs["cubes"]}
        for collection in ("pickups", "cubes"):
            for item in obs[collection]:
                self.loot_memory[item["id"]] = (item, now, collection)
        self.loot_memory = {
            key: entry for key, entry in self.loot_memory.items()
            if now - entry[1] < 7 and (key in visible or d2(entry[0]["pos"], pos) > 7)
        }
        pickups = [item for item, _, kind in self.loot_memory.values() if kind == "pickups"]
        cubes = [item for item, _, kind in self.loot_memory.values() if kind == "cubes"]
        items = []
        for p in pickups:
            value = GOOD_WEAPONS.get(p["kind"], 0) - rank
            if value > 0 and self.blacklist.get(p["id"], 0) <= obs["t"]:
                items.append((p, value + (2 if rank == 0 else 0), True))
        for c in cubes:
            stack = sum(d2(c["pos"], o["pos"]) <= 8 for o in cubes)
            items.append((c, 1 + min(stack, 5), False))
        best = None
        best_value = 0
        for item, value, equip in items:
            xy = item["pos"]
            distance = d2(pos, xy)
            if distance > (150 if equip else 95) or abs(xy[2] - self.grid.ground_at(*xy[:2])) > 3:
                continue
            score = value / (10 + distance)
            if score > best_value and self.grid.los(pos[0], pos[1], *xy[:2]):
                best_value = score
                best = (xy, item["id"] if equip else None, distance)
        return best

    def nav_dir(self, pos, goal, now):
        g = self.grid
        if goal is None:
            return None
        if g.los(pos[0], pos[1], goal[0], goal[1]):
            self.path = None
            dx, dy = goal[0] - pos[0], goal[1] - pos[1]
            n = math.hypot(dx, dy)
            return (dx / n, dy / n) if n > 0.5 else None
        replan = (
            self.path is None
            or self.path_goal is None
            or d2(self.path_goal, goal) > 10
            or now - self.path_time > 4.0
        )
        if replan and self.search is None:
            s = g.nearest_free(*g.cell_of(pos[0], pos[1]), maxr=8)
            e = g.nearest_free(*g.cell_of(goal[0], goal[1]), maxr=25)
            if s and e:
                self.search = Search(g, s, e)
                self.search_goal = list(goal)
        if self.search is not None:
            t0 = time.perf_counter()
            while not self.search.step(600):
                if time.perf_counter() - t0 > 0.010:
                    break
            if self.search.done:
                self.path = self.search.path
                self.path_goal = self.search_goal
                self.path_time = now
                self.search = None
        if self.path:
            # drop passed points, then look ahead to the farthest visible point
            p = self.path
            best_k = 0
            bd = 1e9
            for k in range(min(len(p), 60)):
                dd = d2(p[k], pos)
                if dd < bd:
                    bd, best_k = dd, k
            self.path = p = p[best_k:]
            tgt = p[0]
            for k in range(min(len(p) - 1, 25), 0, -1):
                if g.los(pos[0], pos[1], p[k][0], p[k][1]):
                    tgt = p[k]
                    break
            dx, dy = tgt[0] - pos[0], tgt[1] - pos[1]
            n = math.hypot(dx, dy)
            if n > 0.3:
                return dx / n, dy / n
        # no path yet: head straight
        dx, dy = goal[0] - pos[0], goal[1] - pos[1]
        n = math.hypot(dx, dy)
        return (dx / n, dy / n) if n > 0.5 else None

    # ------------------------------------------------------------ aiming
    def lead(self, me, tgt, speed, zoff):
        mx, my, mz = me["pos"]
        mh = me["stats"].get("muzzle_height", 7)
        ox, oy, oz = mx, my, mz + mh
        tx, ty, tz = tgt["pos"]
        tz += zoff
        vx, vy, vz = tgt.get("vel", (0, 0, 0))
        tt = 0.0
        for _ in range(3):
            px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt * 0.5
            dist = math.sqrt((px - ox) ** 2 + (py - oy) ** 2 + (pz - oz) ** 2)
            tt = dist / speed if speed else 0.0
        px, py, pz = tx + vx * tt, ty + vy * tt, tz + vz * tt * 0.5
        yaw = math.atan2(py - oy, px - ox)
        pitch = math.atan2(pz - oz, math.hypot(px - ox, py - oy))
        return yaw, pitch, math.hypot(px - ox, py - oy)

    # ------------------------------------------------------------ dodging
    def shell_dodge(self, obs, me, pos):
        """Run out of the blast of a mortar shell that would land on us (shells are listed with pos and vel)."""
        mw = self.cfg["weapons"].get("mortar", {})
        g = mw.get("gravity", 22)
        size = 1 + 0.05 * me.get("cubes", 0)
        reach = mw.get("splash_radius", 7) + self.cfg["mech"]["radius"] * size + 2.5
        best = None
        for p in obs.get("projectiles", []):
            if p.get("kind") != "mortar":
                continue
            x, y, z = p["pos"]
            vx, vy, vz = p["vel"]
            drop = max(z - pos[2], 0.0)
            t = (vz + math.sqrt(vz * vz + 2 * g * drop)) / g
            if t > 4.5:
                continue
            lx, ly = x + vx * t, y + vy * t
            px, py = pos[0] + me["vel"][0] * t, pos[1] + me["vel"][1] * t
            if math.hypot(px - lx, py - ly) < reach and (best is None or t < best[0]):
                best = (t, lx, ly, vx, vy)
        if best is None:
            return None
        t, lx, ly, vx, vy = best
        dx, dy = pos[0] - lx, pos[1] - ly
        n = math.hypot(dx, dy)
        if n < 1.0:
            dx, dy = -vy, vx
            n = math.hypot(dx, dy) or 1.0
        dx, dy = dx / n, dy / n
        for turn in (0.0, 0.6, -0.6, 1.2, -1.2, 1.57, -1.57):
            c, s = math.cos(turn), math.sin(turn)
            ux, uy = dx * c - dy * s, dx * s + dy * c
            if self.grid.free_xy(pos[0] + ux * 5, pos[1] + uy * 5):
                return (ux, uy)
        return (dx, dy)

    # ------------------------------------------------------------ main
    def act(self, obs):
        me = obs["self"]
        pos = me["pos"]
        now = obs["t"]
        self.t = now
        act = {}
        hp_frac = me["hp"] / max(me["max_hp"], 1)
        z = obs["zone"]
        late = z["next_radius"] <= 70 or obs["alive"] <= 3

        # --- upgrades
        if obs.get("offer"):
            ch = obs["offer"]["choices"]
            best = min(range(len(ch)), key=lambda i: UPGRADE_PRIORITY.index(ch[i]["id"]) if ch[i]["id"] in UPGRADE_PRIORITY else 99)
            act["pick"] = best

        # --- events
        for ev in obs.get("events", []):
            if ev["type"] == "damaged" and ev.get("by") is not None:
                self.attackers[ev["by"]] = (now, ev.get("bearing"))
                self.last_hit_bearing = ev.get("bearing")
                self.last_hit_time = now
            elif ev["type"] == "take_failed":
                self.blacklist[ev["id"]] = now + 20
        for s in obs.get("sounds", []):
            if s["kind"] in ("gun", "laser", "lightning", "mortar", "laser_charge", "step", "boost", "jet") and s["dist"] != "far":
                self.heard = (now, s["bearing"])
        self.attackers = {k: v for k, v in self.attackers.items() if now - v[0] < 6}

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs:
            m["_d"] = d2(m["pos"], pos)
        for m in npcs:
            m["_d"] = d2(m["pos"], pos)
        mechs.sort(key=lambda m: m["_d"])

        # --- pick a fight target
        target = None
        for m in mechs:
            mhp = m.get("hp", 1400) / max(m.get("max_hp", 1400), 1)
            if m["id"] in self.attackers:
                target = m
                break
            if m["_d"] < 45:
                target = m
                break
            if mhp < 0.35 and m["_d"] < 110 and hp_frac > 0.5:
                target = m
                break
            if late and m["_d"] < 170:
                target = m
                break
        if target is None and mechs:
            # let hunters come to us: no long walks across open ground toward a mech that is not shooting us
            near = [m for m in mechs if m["_d"] < 60 or late]
            if near:
                target = min(near, key=lambda m: m["_d"] + 0.055 * m.get("hp", 1400))
        npc_target = None
        if target is None and not mechs:
            for n in npcs:
                # practice: house bots hunt us as a squad, so drones (2 cubes each) are free power
                if self.attackers.get(n["id"]) or (n["_d"] < FARM_RANGE and hp_frac > FARM_HP):
                    npc_target = n
                    break

        # --- movement goal
        zone_tgt, urg = self.zone_target(obs, pos)
        goal = zone_tgt
        quiet = urg < 0.55 and not late
        take = None
        threats_near = [m for m in mechs if m["_d"] < 90]

        # loot: weapons, cubes, crates (only when no threats around)
        can_loot = target is None and not threats_near and urg < 0.9
        supply = None
        if can_loot and npc_target is None and now - self.last_hit_time > 3:
            supply = self.nearby_drop(obs, pos, mechs)
        if supply is not None:
            # Leave a margin for braking; the four-second timer needs us inside.
            goal = supply["pos"][:2] if d2(pos, supply["pos"]) > supply["radius"] / 2 else None
        elif can_loot:
            slot1 = me["weapons"][1]
            have = {w["kind"] for w in me["weapons"] if w}
            cand = []
            for p in obs.get("pickups", []):
                if self.blacklist.get(p["id"], 0) > now or p["kind"] in have:
                    continue
                if p["kind"] not in GOOD_WEAPONS:
                    continue
                if slot1 is not None and GOOD_WEAPONS.get(slot1["kind"], 0) >= GOOD_WEAPONS[p["kind"]]:
                    continue
                if p["pos"][2] > self.grid.ground_at(p["pos"][0], p["pos"][1]) + 3:
                    continue  # on a roof
                dd = d2(p["pos"], pos)
                if dd < (150 if p["kind"] == "lightning" or slot1 is None else 100):
                    cand.append((dd - 20 * GOOD_WEAPONS[p["kind"]], dd, p))
            if cand:
                cand.sort(key=lambda c: c[0])
                _, dd, p = cand[0]
                goal = p["pos"][:2]
                if dd < self.reach * 0.9:
                    take = p["id"]
                    if slot1 is not None:
                        act["drop"] = 1
            else:
                cubes = [c for c in obs.get("cubes", []) if d2(c["pos"], pos) < 45 and c["pos"][2] < self.grid.ground_at(c["pos"][0], c["pos"][1]) + 3]
                if cubes:
                    goal = min(cubes, key=lambda c: d2(c["pos"], pos))["pos"][:2]
                elif hp_frac > FARM_HP:
                    crates = [c for c in obs.get("crates", []) if d2(c["pos"], pos) < 55 and c["pos"][2] < self.grid.ground_at(c["pos"][0], c["pos"][1]) + 3]
                    if crates:
                        self.crate_target = min(crates, key=lambda c: d2(c["pos"], pos))
        if take is not None:
            act["take"] = take

        # rest to regenerate (needs 5 s without dealing or taking damage) when hurt and alone
        resting = (target is None and npc_target is None and not mechs and hp_frac < REST_BELOW
                   and me["in_zone"] and urg < 0.9 and now - self.last_hit_time > 1.0)
        if resting:
            goal = None
            self.crate_target = None
        # --- fight / avoid adjustments to movement
        move = self.nav_dir(pos, goal, now) if goal else None
        speed = 1.0
        boost = False
        aim = None
        fire = [False, False]

        foe = target or npc_target
        if foe is not None:
            fd = foe["_d"]
            zoff = 4.5 if foe["kind"] == "mech" else 1.75
            yaw, pitch, hd = self.lead(me, foe, self.gun_speed, zoff)
            aim = [yaw, max(min(pitch, 1.2), -0.65)]
            ay, ap = me["aim"]
            err = abs(wrap(ay - yaw))
            tol = math.atan2(2.2 if foe["kind"] == "mech" else 1.4, max(hd, 1)) + 0.015
            gun = me["weapons"][0]
            if gun and gun["kind"] == "gun" and err < tol + 0.01 and fd < gun["stats"]["range"] * 0.95 and gun.get("reload", 0) == 0:
                fire[0] = True
            w1 = me["weapons"][1]
            if w1:
                if w1["kind"] == "lightning":
                    rng = w1["stats"].get("range", 45)
                    cone = math.radians(w1["stats"].get("cone_deg", 30)) / 2
                    if fd < rng * 0.95 and err < cone * 0.8 and me["energy"] > 6:
                        fire[1] = True
                elif w1["kind"] == "laser":
                    # hold to charge, let go (fire=False) to shoot along the current aim
                    full = w1["stats"].get("charge", 0.75)
                    charge = w1.get("charge", 0) or 0
                    lyaw = math.atan2(foe["pos"][1] - pos[1], foe["pos"][0] - pos[0])
                    lp = math.atan2(foe["pos"][2] + zoff - (pos[2] + me["stats"].get("muzzle_height", 7)), max(fd, 1))
                    if fd < 260 and w1.get("cooldown", 0) <= 0:
                        aim = [lyaw, lp]  # hitscan: no lead
                        on_target = abs(wrap(ay - lyaw)) <= tol
                        fire[1] = not (charge >= full - 0.02 and on_target)
                elif w1["kind"] == "mortar" and 40 < fd < 220 and w1.get("cooldown", 0) <= 0:
                    v = w1["stats"].get("speed", 72)
                    gr = w1["stats"].get("gravity", 22)
                    s = gr * fd / (v * v)
                    if s < 1:
                        aim = [aim[0], 0.5 * math.asin(s)]
                        if abs(wrap(ay - aim[0])) < 0.05:
                            fire[1] = True
            # movement in a fight: strafe across the line of fire, back off when losing
            by = math.atan2(foe["pos"][1] - pos[1], foe["pos"][0] - pos[0])
            if now - self.strafe_t > 1.3 + random.random():
                self.strafe = -self.strafe
                self.strafe_t = now
            sx, sy = math.cos(by + self.strafe * math.pi / 2), math.sin(by + self.strafe * math.pi / 2)
            foe_hp = foe.get("hp", 1400) / max(foe.get("max_hp", 1400), 1)
            losing = foe["kind"] == "mech" and hp_frac < 0.4 and foe_hp > hp_frac and not late
            if losing:
                bx, by2 = -math.cos(by), -math.sin(by)
                mx_, my_ = 0.8 * bx + 0.4 * sx, 0.8 * by2 + 0.4 * sy
                boost = me["energy"] > 25
            elif w1 and w1["kind"] == "lightning" and foe["kind"] == "mech" and fd > 30:
                # close in for the arc
                mx_, my_ = 0.6 * math.cos(by) + 0.5 * sx, 0.6 * math.sin(by) + 0.5 * sy
            else:
                mx_, my_ = sx, sy
                if fd < 18:
                    mx_ -= 0.5 * math.cos(by)
                    my_ -= 0.5 * math.sin(by)
            # Approach the game reward before the avatar despawns; the
            # two guaranteed bonus cubes appear at its last position.
            if foe["kind"] == "mech" and fd > 9:
                approach = self.nav_dir(pos, foe["pos"][:2], now)
                if approach:
                    boost = me["energy"] > 50 and fd > 35
                    mx_ = approach[0] + 0.45 * sx
                    my_ = approach[1] + 0.45 * sy
            if move is not None and urg >= 0.9:
                mx_, my_ = 0.6 * move[0] + 0.5 * mx_, 0.6 * move[1] + 0.5 * my_
            move = (mx_, my_)
            quiet = False
        else:
            # not fighting: keep threats in view, steer away from them
            if threats_near or mechs:
                m0 = mechs[0]
                by = math.atan2(m0["pos"][1] - pos[1], m0["pos"][0] - pos[0])
                aim = [by, 0.0]
                if m0["_d"] < 130:
                    awx, awy = -math.cos(by), -math.sin(by)
                    if move is None:
                        move = (awx * 0.7, awy * 0.7)
                    else:
                        move = (move[0] + 0.6 * awx, move[1] + 0.6 * awy)
                    quiet = False
            elif self.last_hit_bearing is not None and now - self.last_hit_time < 3:
                aim = [self.last_hit_bearing, 0.0]
            elif self.heard and now - self.heard[0] < 2.5:
                aim = [self.heard[1], 0.0]
            elif self.crate_target is not None and hp_frac > FARM_HP - 0.1:
                c = self.crate_target
                cd = d2(c["pos"], pos)
                ay = math.atan2(c["pos"][1] - pos[1], c["pos"][0] - pos[0])
                ap = math.atan2(c["pos"][2] + 1.2 - (pos[2] + 7), max(cd, 1))
                aim = [ay, ap]
                if cd < 60 and abs(wrap(me["aim"][0] - ay)) < 0.04 and me["weapons"][0] and me["weapons"][0].get("reload", 0) == 0:
                    fire[0] = True
                if not any(cc["id"] == c["id"] for cc in obs.get("crates", [])):
                    self.crate_target = None
            else:
                # slow scan when idle, otherwise look where we go
                if move is not None:
                    aim = [math.atan2(move[1], move[0]), 0.0]
                else:
                    if self.scan_yaw is None:
                        self.scan_yaw = me["aim"][0]
                    self.scan_yaw = wrap(self.scan_yaw + 0.35)
                    aim = [self.scan_yaw, 0.0]
            if target is None and me["weapons"][0] and me["weapons"][0].get("ammo", 99) < me["weapons"][0]["stats"].get("mag", 12) * 0.5 and not mechs:
                act["reload"] = True

        # damage from behind with no visible shooter: turn to face it
        if foe is None and self.last_hit_bearing is not None and now - self.last_hit_time < 1.5:
            aim = [self.last_hit_bearing, 0.0]
            quiet = False

        reward = self.reward_goal(obs, pos) if urg < 0.9 else None
        if reward:
            xy, equip_id, distance = reward
            if foe is None or distance < min(42, foe["_d"] * 0.6 + 8):
                move = self.nav_dir(pos, xy[:2], now)
                quiet = False
            if equip_id is not None and distance < self.reach * 0.9:
                act["take"] = equip_id
                if me["weapons"][1]:
                    act["drop"] = 1

        # --- mortar shells: get out of the blast first
        dodge = self.shell_dodge(obs, me, pos)
        if dodge is not None:
            move = dodge
            boost = me["energy"] > 8
            quiet = False
            if me["weapons"][1] and me["weapons"][1]["kind"] == "laser" and (me["weapons"][1].get("charge") or 0) > 0:
                fire[1] = False  # charging halves our speed

        # --- bullets: step sideways out of the line of an incoming stream
        elif obs.get("projectiles"):
            hit_r = self.cfg["mech"]["radius"] * (1 + 0.05 * me.get("cubes", 0)) + 1.2
            jk = None
            for p in obs["projectiles"]:
                if p.get("kind") == "mortar":
                    continue
                vx, vy, vz = p["vel"]
                v2 = vx * vx + vy * vy
                if v2 < 100:
                    continue
                rx, ry = pos[0] - p["pos"][0], pos[1] - p["pos"][1]
                tc = (rx * vx + ry * vy) / v2
                if not 0 < tc < 0.7:
                    continue
                cx, cy = rx - vx * tc, ry - vy * tc
                if math.hypot(cx, cy) < hit_r and (jk is None or tc < jk[0]):
                    jk = (tc, cx, cy, vx, vy)
            if jk is not None:
                tc, cx, cy, vx, vy = jk
                n = math.hypot(vx, vy)
                px_, py_ = -vy / n, vx / n
                if px_ * cx + py_ * cy < 0:
                    px_, py_ = -px_, -py_
                if self.grid.free_xy(pos[0] + px_ * 4, pos[1] + py_ * 4):
                    base = move if move is not None else (0.0, 0.0)
                    move = (0.35 * base[0] + px_, 0.35 * base[1] + py_)
                    quiet = False

        # --- stuck handling
        hv = math.hypot(me["vel"][0], me["vel"][1])
        if move is not None and math.hypot(*move) > 0.3 and me["grounded"] and hv < 1.0:
            self.stuck_t += 0.1
        else:
            self.stuck_t = max(0.0, self.stuck_t - 0.05)
        jump = False
        if self.stuck_t > 0.6 and now > self.unstick_until:
            self.unstick_until = now + 1.6
            self.unstick_dir = move
            self.path = None
            self.stuck_t = 0
        if now < self.unstick_until and self.unstick_dir is not None:
            jump = me["grounded"]
            boost = me["energy"] > 15
            move = self.unstick_dir

        # outside zone: hurry
        if not me["in_zone"]:
            boost = me["energy"] > 20
            quiet = False

        if move is not None:
            n = math.hypot(*move)
            if n > 1e-6:
                k = 1.0 / n
                if quiet and not boost:
                    k *= min(1.0, (self.step_speed - 0.6) / max(self.walk, 1))
                move = [move[0] * k, move[1] * k]
            act["move"] = move
        else:
            act["move"] = [0.0, 0.0]
        act["boost"] = bool(boost)
        if jump:
            act["jump"] = True
        if aim is not None:
            act["aim"] = [wrap(aim[0]), max(min(aim[1], 1.25), -0.65)]
        act["fire"] = fire
        mf.draw.label(f"{int(me['hp'])}hp {'F' if foe else ''}{' q' if quiet else ''} u{urg:.1f}")
        return act


if __name__ == "__main__":
    mf.run(Bot)
