"""Mech Royale bot: survival duelist.

Strategy (practice = 1 vs a 15-bot hunting squad; final = 16-mech FFA):
  farm weapons/cubes/NPCs between fights, win isolated 1v1s, kite and heal
  (5 s out of combat -> 45 HP/s), never take a fair 2v1, own the next zone,
  contest supply drops (full heal + 3 cubes) only when safe.

Stdlib only. Map-aware: A* over terrain+roof heights, LOS vs buildings.
"""
import heapq
import json
import math
import os
import random
import sys
from array import array

import mechfight as mf

TAU = math.tau


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def wrap(a):
    while a > math.pi:
        a -= TAU
    while a < -math.pi:
        a += TAU
    return a


def d2(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)


# ---------------------------------------------------------------- map
class World:
    RES = 4.0
    STEP = 1.35  # step_up 1.2 + slack

    def __init__(self, map_dir, files):
        wj = None
        ti = None
        for f in files or []:
            if f.endswith("world.json"):
                wj = os.path.join(map_dir, f)
            if f.endswith("terrain.i16"):
                ti = os.path.join(map_dir, f)
        with open(wj) as fh:
            w = json.load(fh)
        ter = w["terrain"]
        self.tminx = float(ter["minX"])
        self.tminy = float(ter["minY"])
        self.tstep = float(ter["step"])
        self.nx = int(ter["nx"])
        self.ny = int(ter["ny"])
        data = array("h")
        with open(ti, "rb") as fh:
            data.frombytes(fh.read())
        if sys.byteorder == "big":
            data.byteswap()
        self.ter = data
        self.prisms = []
        self.buckets = {}
        BS = 32.0
        self.bs = BS
        for p in w.get("prisms", []):
            ring = p["ring"]
            pts = [(ring[i], ring[i + 1]) for i in range(0, len(ring) - 1, 2)]
            if len(pts) < 3:
                continue
            holes = []
            for h in p.get("holes") or []:
                hp = [(h[i], h[i + 1]) for i in range(0, len(h) - 1, 2)]
                if len(hp) >= 3:
                    holes.append(hp)
            xs = [q[0] for q in pts]
            ys = [q[1] for q in pts]
            pr = {
                "ring": pts, "holes": holes, "base": float(p["base"]), "top": float(p["top"]),
                "minx": min(xs), "maxx": max(xs), "miny": min(ys), "maxy": max(ys),
                "kind": p.get("kind", "building"),
            }
            idx = len(self.prisms)
            self.prisms.append(pr)
            for bx in range(int(pr["minx"] // BS), int(pr["maxx"] // BS) + 1):
                for by in range(int(pr["miny"] // BS), int(pr["maxy"] // BS) + 1):
                    self.buckets.setdefault((bx, by), []).append(idx)
        b = w["bounds"]
        self.gx0 = math.floor(b["minX"] / self.RES) * self.RES
        self.gy0 = math.floor(b["minY"] / self.RES) * self.RES
        self.gw = int(math.ceil((b["maxX"] - self.gx0) / self.RES)) + 1
        self.gh = int(math.ceil((b["maxY"] - self.gy0) / self.RES)) + 1
        n = self.gw * self.gh
        self.grid = array("f", bytes(4 * n))
        for gy in range(self.gh):
            y = self.gy0 + gy * self.RES
            base = gy * self.gw
            for gx in range(self.gw):
                x = self.gx0 + gx * self.RES
                self.grid[base + gx] = self.surface_h(x, y)
        # precomputed ground passability: east / north edges (STEP height diff)
        self.passE = bytearray(n)
        self.passN = bytearray(n)
        gw, gh, grid = self.gw, self.gh, self.grid
        for gy in range(gh):
            base = gy * gw
            for gx in range(gw):
                i = base + gx
                h = grid[i]
                if gx + 1 < gw and abs(grid[i + 1] - h) <= self.STEP:
                    self.passE[i] = 1
                if gy + 1 < gh and abs(grid[i + gw] - h) <= self.STEP:
                    self.passN[i] = 1

    # -- terrain / surfaces -------------------------------------------------
    def terrain_h(self, x, y):
        fx = clamp((x - self.tminx) / self.tstep, 0.0, self.nx - 1.001)
        fy = clamp((y - self.tminy) / self.tstep, 0.0, self.ny - 1.001)
        i = int(fx)
        j = int(fy)
        tx = fx - i
        ty = fy - j
        nx = self.nx
        d = self.ter
        i1 = min(i + 1, nx - 1)
        j1 = min(j + 1, self.ny - 1)
        v00 = d[j * nx + i]
        v10 = d[j * nx + i1]
        v01 = d[j1 * nx + i]
        v11 = d[j1 * nx + i1]
        return ((v00 * (1 - tx) + v10 * tx) * (1 - ty) + (v01 * (1 - tx) + v11 * tx) * ty) * 0.01

    @staticmethod
    def _in_ring(x, y, ring):
        inside = False
        px, py = ring[-1]
        for qx, qy in ring:
            if (qy > y) != (py > y):
                if x < (px - qx) * (y - qy) / (py - qy) + qx:
                    inside = not inside
            px, py = qx, qy
        return inside

    def _prisms_at(self, x, y):
        return self.buckets.get((int(x // self.bs), int(y // self.bs)), ())

    def _in_prism_xy(self, pr, x, y):
        if x < pr["minx"] or x > pr["maxx"] or y < pr["miny"] or y > pr["maxy"]:
            return False
        if not self._in_ring(x, y, pr["ring"]):
            return False
        for h in pr["holes"]:
            if self._in_ring(x, y, h):
                return False
        return True

    def surface_h(self, x, y):
        h = self.terrain_h(x, y)
        cands = [self.prisms[i] for i in self._prisms_at(x, y)]
        if not cands:
            return h
        cands.sort(key=lambda p: p["base"])
        for pr in cands:
            if pr["base"] <= h + 1.5 and pr["top"] > h and self._in_prism_xy(pr, x, y):
                h = pr["top"]
        return h

    def los_blocked(self, ax, ay, az, bx, by, bz):
        dist = math.hypot(bx - ax, by - ay, bz - az)
        steps = int(clamp(dist / 2.0, 2, 70))
        for s in range(1, steps):
            t = s / steps
            x = ax + (bx - ax) * t
            y = ay + (by - ay) * t
            z = az + (bz - az) * t
            if z < self.terrain_h(x, y) - 0.4:
                return True
            for i in self._prisms_at(x, y):
                pr = self.prisms[i]
                if pr["base"] - 0.3 <= z <= pr["top"] + 0.3 and self._in_prism_xy(pr, x, y):
                    return True
        return False

    # -- grid / path ---------------------------------------------------------
    def _cell(self, x, y):
        gx = int(clamp((x - self.gx0) / self.RES, 0, self.gw - 1))
        gy = int(clamp((y - self.gy0) / self.RES, 0, self.gh - 1))
        return gx, gy

    def grid_h(self, x, y):
        gx, gy = self._cell(x, y)
        return self.grid[gy * self.gw + gx]

    def seg_walkable(self, ax, ay, bx, by):
        dist = d2(ax, ay, bx, by)
        steps = int(clamp(dist / 2.0, 1, 60))
        prev = self.surface_h(ax, ay)
        for s in range(1, steps + 1):
            t = s / steps
            h = self.surface_h(ax + (bx - ax) * t, ay + (by - ay) * t)
            if abs(h - prev) > 1.35 + 1.05 * (dist / steps):
                return False
            prev = h
        return True

    def find_path(self, sx, sy, gx_, gy_):
        gw, gh, grid = self.gw, self.gh, self.grid
        sgx, sgy = self._cell(sx, sy)
        ggx, ggy = self._cell(gx_, gy_)
        start = sgy * gw + sgx
        goal = ggy * gw + ggx
        if start == goal:
            return [(gx_, gy_)]
        g = [1e18] * (gw * gh)
        came = array("i", [-1]) * (gw * gh)
        closed = bytearray(gw * gh)
        g[start] = 0.0
        heap = [(0.0, start)]
        best = start
        best_h = 1e18
        reached = False
        expansions = 0
        passE, passN = self.passE, self.passN
        RES = self.RES
        SQ = 1.4142
        while heap and expansions < 11000:
            _, cur = heapq.heappop(heap)
            if closed[cur]:
                continue
            closed[cur] = 1
            expansions += 1
            if cur == goal:
                best = cur
                reached = True
                break
            cx = cur % gw
            cy = cur // gw
            hx = ggx - cx if ggx >= cx else cx - ggx
            hy = ggy - cy if ggy >= cy else cy - ggy
            hh = (hx + hy) + (SQ - 2.0) * (hx if hx < hy else hy)
            if hh < best_h:
                best_h = hh
                best = cur
            ch = grid[cur]
            gc = g[cur]
            # neighbors via precomputed edges; (index, cost, needs corner check cells)
            nbrs = []
            if cx + 1 < gw and passE[cur]:
                nbrs.append((cur + 1, RES))
            if cx > 0 and passE[cur - 1]:
                nbrs.append((cur - 1, RES))
            if cy + 1 < gh and passN[cur]:
                nbrs.append((cur + gw, RES))
            if cy > 0 and passN[cur - gw]:
                nbrs.append((cur - gw, RES))
            diag = RES * SQ
            if cx + 1 < gw and cy + 1 < gh and passE[cur] and passN[cur] and passN[cur + 1]:
                ni = cur + gw + 1
                if abs(grid[ni] - ch) <= self.STEP:
                    nbrs.append((ni, diag))
            if cx > 0 and cy + 1 < gh and passE[cur - 1] and passN[cur] and passN[cur - 1]:
                ni = cur + gw - 1
                if abs(grid[ni] - ch) <= self.STEP:
                    nbrs.append((ni, diag))
            if cx + 1 < gw and cy > 0 and passE[cur] and passN[cur - gw] and passN[cur + 1 - gw]:
                ni = cur - gw + 1
                if abs(grid[ni] - ch) <= self.STEP:
                    nbrs.append((ni, diag))
            if cx > 0 and cy > 0 and passE[cur - 1] and passN[cur - gw] and passN[cur - 1 - gw]:
                ni = cur - gw - 1
                if abs(grid[ni] - ch) <= self.STEP:
                    nbrs.append((ni, diag))
            for ni, dc in nbrs:
                if closed[ni]:
                    continue
                ng = gc + dc
                if ng < g[ni]:
                    g[ni] = ng
                    came[ni] = cur
                    nx_ = ni % gw
                    ny_ = ni // gw
                    hx = ggx - nx_ if ggx >= nx_ else nx_ - ggx
                    hy = ggy - ny_ if ggy >= ny_ else ny_ - ggy
                    f = ng + ((hx + hy) + (SQ - 2.0) * (hx if hx < hy else hy)) * RES
                    heapq.heappush(heap, (f, ni))
        if best == start and best != goal:
            return None
        cells = []
        cur = best
        while cur != -1:
            cells.append(cur)
            if cur == start:
                break
            cur = came[cur]
        cells.reverse()
        pts = [(self.gx0 + (c % gw) * self.RES, self.gy0 + (c // gw) * self.RES) for c in cells]
        if reached:
            pts.append((gx_, gy_))
        self.last_reached = reached
        # greedy shortcut smoothing (bounded checks: A* ticks share the 50 ms budget)
        out = []
        i = 0
        while i < len(pts) - 1:
            j = len(pts) - 1
            checks = 0
            while j > i + 1 and checks < 7 and not self.seg_walkable(pts[i][0], pts[i][1], pts[j][0], pts[j][1]):
                j -= 1
                checks += 1
            out.append(pts[j])
            i = j
        return out


# ---------------------------------------------------------------- bot
UPG_SCORE = {
    "armor": 100, "plating": 92, "nanites": 88,
    "gun_damage": 74, "gun_rate": 72, "lightning_efficiency": 68,
    "servos": 66, "laser_pierce": 66, "lightning_range": 64, "laser_charge": 62,
    "lightning_chain": 60, "laser_damage": 70, "mortar_reload": 58, "gyros": 58,
    "mortar_splash": 56, "capacitor": 52, "reactor": 50, "gun_velocity": 50,
    "gun_mag": 48, "jets": 46, "sensors": 44, "mortar_velocity": 44,
}
TIER = {"gun": 0, "mortar": 1, "laser": 2, "lightning": 3}

# Submission experiments: one big lever per scored run.
# v2 "castle": get a special weapon, jet onto a mid-rise roof inside the
# zone, and make hunters climb up one at a time to fight us.
MODE = "duelist"
UPG_SCORE_BERSERK = {
    "gun_damage": 100, "gun_rate": 98, "armor": 90, "plating": 86,
    "laser_damage": 88, "lightning_efficiency": 84, "nanites": 72,
    "servos": 70, "laser_charge": 66, "lightning_range": 64, "gyros": 62,
    "lightning_chain": 60, "gun_velocity": 58, "mortar_reload": 56,
    "laser_pierce": 54, "capacitor": 50, "gun_mag": 48, "mortar_splash": 46,
    "reactor": 44, "jets": 40, "sensors": 36, "mortar_velocity": 34,
}


class Bot:
    name = "mech-royale"

    def __init__(self, hello):
        cfg = hello.get("config", {})
        self.cfg = cfg
        self.rng = random.Random(int(hello.get("seed", 7)) & 0xFFFFFFFF)
        try:
            self.world = World(hello["map"]["dir"], hello["map"].get("files"))
        except Exception as e:  # map is optional: steering fallback still plays
            print("map load failed:", e, file=sys.stderr, flush=True)
            self.world = None
        self.enemies = {}      # id -> dict(pos, vel, hp, weapons, t, charging, cubes)
        self.cubes_mem = {}    # id -> (x, y, z, t)
        self.pick_mem = {}     # id -> (x, y, z, t, kind)
        self.crate_mem = {}    # id -> (x, y, z, t, hp)
        self.sounds = []       # (t, bearing, dist, kind)
        self.path = []
        self.path_goal = None
        self.replan_at = -1.0
        self.last_t = 0.0
        self.aim_cmd = [0.0, 0.0]
        self.scan_yaw = 0.0
        self.last_damage_t = -99.0
        self.last_dealt_t = -99.0
        self.last_shot_t = -99.0
        self.last_attacker = None
        self.strafe_dir = 1
        self.strafe_until = 0.0
        self.juke_at = -1.0
        self.target_id = None
        self.target_until = 0.0
        self.stuck_xy = None
        self.stuck_since = 0.0
        self.recover_until = -1.0
        self.wander_pt = None
        self.wander_until = -1.0
        self.climb_until = -1.0
        self.loot_site = None
        # castle: candidate roofs (center, top) from world prisms
        self.roofs = []
        self.roof_bad = {}
        self.roof_cur = None
        self.roof_since = -1.0
        self.anchor = None
        self.orbit_dir = 1
        if self.world:
            for pr in self.world.prisms:
                wdt = pr["maxx"] - pr["minx"]
                hgt = pr["maxy"] - pr["miny"]
                if wdt < 15 or hgt < 15:
                    continue
                cx = (pr["minx"] + pr["maxx"]) / 2
                cy = (pr["miny"] + pr["maxy"]) / 2
                rise = pr["top"] - self.world.terrain_h(cx, cy)
                if 14 <= rise <= 55 and pr["top"] > self.surface(cx, cy) - 1:
                    self.roofs.append((cx, cy, pr["top"]))

    # -- helpers -------------------------------------------------------------
    def surface(self, x, y, fallback=0.0):
        if self.world:
            return self.world.surface_h(x, y)
        return fallback

    def los(self, a, b):
        """True if a->b (3D points) is clear of buildings/terrain."""
        if not self.world:
            return True
        return not self.world.los_blocked(a[0], a[1], a[2], b[0], b[1], b[2])

    @staticmethod
    def slot_of(me, kind):
        for i, w in enumerate(me.get("weapons") or []):
            if w and w.get("kind") == kind:
                return i
        return -1

    @staticmethod
    def kinds_held(me):
        return [w["kind"] for w in (me.get("weapons") or []) if w]

    def zone_state(self, obs):
        z = obs["zone"]
        t = obs["t"]
        c = z["center"]
        nc = z["next_center"]
        if t <= z["shrink_start"]:
            cur = (c, z["radius"])
        elif t >= z["shrink_end"]:
            cur = (nc, z["next_radius"])
        else:
            f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
            cur = ((c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f),
                   z["radius"] + (z["next_radius"] - z["radius"]) * f)
        return cur, (nc, z["next_radius"]), z

    def navigate(self, me, t, dest, boost=False, speed=1.0, climb_z=None):
        """Move toward dest=(x,y). Returns (move, boost, jump)."""
        x, y, z = me["pos"]
        dx, dy = dest[0] - x, dest[1] - y
        dist = math.hypot(dx, dy)
        if dist < 1.5:
            return [0.0, 0.0], False, False
        jump = False
        use_boost = boost
        dest_z = climb_z if climb_z is not None else self.surface(dest[0], dest[1], z)
        # climb: destination surface well above our feet and close -> jump+jet up
        if self.world and dest_z - z > 1.4 and dist < 18.0 and me["energy"] > 22:
            mv = [dx / dist, dy / dist]
            if me["grounded"]:
                return mv, False, True
            return mv, True, False
        # wall directly ahead but low enough to hop
        wx, wy = None, None
        if self.world:
            goal_moved = (self.path_goal is not None
                          and d2(self.path_goal[0], self.path_goal[1], dest[0], dest[1]) > 18.0)
            if t >= self.replan_at and (goal_moved or self.path_goal is None or not self.path):
                p = self.world.find_path(x, y, dest[0], dest[1])
                if p:
                    self.path = p
                self.path_goal = (dest[0], dest[1])
                # unreachable-at-ground-level goals (roofs) retry rarely
                self.replan_at = t + (5.0 if not getattr(self.world, "last_reached", True) else 1.6)
            while self.path and d2(x, y, self.path[0][0], self.path[0][1]) < 3.5:
                self.path.pop(0)
            if self.path:
                if self.world.seg_walkable(x, y, dest[0], dest[1]):
                    self.path = []
                else:
                    wx, wy = self.path[0]
        # stuck recovery
        if self.stuck_xy is None or d2(x, y, self.stuck_xy[0], self.stuck_xy[1]) > 2.0:
            self.stuck_xy = (x, y)
            self.stuck_since = t
        elif t - self.stuck_since > 1.1 and t > self.recover_until:
            self.recover_until = t + 0.7
            self.strafe_dir = -self.strafe_dir
            self.path = []
            self.replan_at = -1.0
        tx, ty = (wx, wy) if wx is not None else dest
        vx, vy = tx - x, ty - y
        vd = math.hypot(vx, vy) or 1.0
        mv = [vx / vd, vy / vd]
        if t < self.recover_until:
            mv = [-mv[1] * self.strafe_dir, mv[0] * self.strafe_dir]
            jump = bool(me["grounded"])
        # low wall hop while pathing
        if self.world and me["grounded"]:
            ah = self.surface(x + mv[0] * 5.0, y + mv[1] * 5.0, z)
            if 1.3 < ah - z < 3.2:
                jump = True
        if speed != 1.0:
            mv = [mv[0] * speed, mv[1] * speed]
        return mv, use_boost, jump

    def combat_move(self, me, t, vx, vy, boost=False):
        """Direct combat steering with wall slide. v need not be normalized."""
        x, y, z = me["pos"]
        vd = math.hypot(vx, vy)
        if vd < 1e-6:
            return [0.0, 0.0], boost, False
        vx, vy = vx / vd, vy / vd
        if self.world:
            ah = self.surface(x + vx * 6.0, y + vy * 6.0, z)
            if ah - z > 1.4:  # wall: try sliding either way
                for s in (1, -1):
                    rx, ry = -vy * s, vx * s
                    if self.surface(x + rx * 6.0, y + ry * 6.0, z) - z <= 1.4:
                        return [rx, ry], boost, False
                return [-vx, -vy], boost, bool(me["grounded"])
        return [vx, vy], boost, False

    # -- main ------------------------------------------------------------------
    def act(self, obs):
        t = float(obs["t"])
        dt = clamp(t - self.last_t, 0.01, 0.5)
        self.last_t = t
        me = obs["self"]
        x, y, z = me["pos"]
        hp = me["hp"]
        max_hp = max(1.0, me["max_hp"])
        hp_frac = hp / max_hp
        energy = me["energy"]
        act = {"tick": obs["tick"], "move": [0.0, 0.0], "aim": list(self.aim_cmd),
               "fire": [False, False], "boost": False, "jump": False}
        self.scan_yaw = me["aim"][0]

        # ---- memory updates
        for e in obs.get("events", []):
            ty = e.get("type")
            if ty == "damaged":
                self.last_damage_t = t
                self.last_attacker = e.get("by")
            elif ty == "hit":
                self.last_dealt_t = t
            elif ty == "kill":
                tgt = self.enemies.pop(e.get("target"), None)
                if tgt is not None:
                    self.loot_site = (tgt["pos"][0], tgt["pos"][1], tgt["pos"][2], t)
        seen_mech = {}
        npcs = []
        for m in obs.get("mechs", []):
            if m["kind"] == "mech":
                seen_mech[m["id"]] = m
                self.enemies[m["id"]] = {
                    "pos": m["pos"], "vel": m.get("vel", [0, 0, 0]), "hp": m.get("hp", 1400),
                    "weapons": m.get("weapons", []), "t": t,
                    "charging": bool(m.get("charging")), "cubes": m.get("cubes", 0),
                }
            else:
                npcs.append(m)
        for cid, c in list(self.cubes_mem.items()):
            if t - c[3] > 25:
                del self.cubes_mem[cid]
        for c in obs.get("cubes", []):
            self.cubes_mem[c["id"]] = (c["pos"][0], c["pos"][1], c["pos"][2], t)
        for pid, p in list(self.pick_mem.items()):
            if t - p[3] > 90:
                del self.pick_mem[pid]
        for p in obs.get("pickups", []):
            self.pick_mem[p["id"]] = (p["pos"][0], p["pos"][1], p["pos"][2], t, p["kind"])
        for cid, c in list(self.crate_mem.items()):
            if t - c[4] > 90:
                del self.crate_mem[cid]
        for c in obs.get("crates", []):
            self.crate_mem[c["id"]] = (c["pos"][0], c["pos"][1], c["pos"][2], t, c.get("hp", 300))
        for s in obs.get("sounds", []):
            if s["kind"] in ("gun", "mortar", "laser", "lightning", "explosion", "npc_gun"):
                self.sounds.append((t, s["bearing"], s["dist"], s["kind"]))
        self.sounds = [s for s in self.sounds if t - s[0] < 4.0]
        for eid, e in list(self.enemies.items()):
            if t - e["t"] > 30:
                del self.enemies[eid]

        # ---- offer
        if obs.get("offer"):
            ch = obs["offer"]["choices"]
            table = UPG_SCORE_BERSERK if MODE == "berserk" else UPG_SCORE
            best = max(range(len(ch)), key=lambda i: table.get(ch[i]["id"], 30))
            act["pick"] = best

        # ---- take weapons under our feet
        held = self.kinds_held(me)
        for p in obs.get("pickups", []):
            if d2(x, y, p["pos"][0], p["pos"][1]) < 4.0 and abs(p["pos"][2] - z) < 4.0:
                kind = p["kind"]
                if kind in held:
                    continue
                empty = [i for i, w in enumerate(me["weapons"]) if w is None]
                if empty and not (empty == [0] and kind != "gun"):
                    act["take"] = p["id"]
                    break
                if len(held) >= 2:
                    # swap out our weakest special for a better one
                    specials = [(TIER.get(w["kind"], 0), i) for i, w in enumerate(me["weapons"]) if w and w["kind"] != "gun"]
                    if specials:
                        low_tier, low_slot = min(specials)
                        wlow = me["weapons"][low_slot]
                        charging = wlow and wlow.get("kind") == "laser" and (wlow.get("charge") or 0) > 0
                        if TIER.get(kind, 0) > low_tier and not charging:
                            act["take"] = p["id"]
                            act["drop"] = low_slot
                            break

        (cur_c, cur_r), (next_c, next_r), zinfo = self.zone_state(obs)
        dist_next_center = d2(x, y, next_c[0], next_c[1])
        outside_next = dist_next_center - next_r
        dist_cur_center = d2(x, y, cur_c[0], cur_c[1])
        outside_cur = dist_cur_center - cur_r
        zone_urgent = outside_cur > -5 or (outside_next > 0 and t > zinfo["shrink_start"] - 25)
        in_combat_recently = (t - max(self.last_damage_t, self.last_dealt_t)) < 5.0

        enemies = sorted(seen_mech.values(), key=lambda m: d2(x, y, m["pos"][0], m["pos"][1]))
        e_dists = [d2(x, y, m["pos"][0], m["pos"][1]) for m in enemies]
        nearest_e = enemies[0] if enemies else None
        nearest_d = e_dists[0] if e_dists else 1e9
        n_close = sum(1 for dd in e_dists if dd < 110)

        _size = 1.0 + 0.05 * (me.get("cubes") or 0)
        muzzle = [x, y, z + 7.0 * _size]

        def aim_at(tx, ty, tz, lead_vel=None, proj_speed=None, from_z=None):
            fz = from_z if from_z is not None else muzzle[2]
            px, py, pz = tx, ty, tz
            if lead_vel is not None and proj_speed:
                dd = math.hypot(px - x, py - y, pz - fz)
                tt = dd / proj_speed
                for _ in range(2):
                    px = tx + lead_vel[0] * tt
                    py = ty + lead_vel[1] * tt
                    pz = tz + (lead_vel[2] * tt if abs(lead_vel[2]) > 0.1 else 0.0)
                    tt = math.hypot(px - x, py - y, pz - fz) / proj_speed
            hd = math.hypot(px - x, py - y)
            return [math.atan2(py - y, px - x), math.atan2(pz - fz, max(hd, 0.01))], (px, py, pz)

        def enemy_center(m):
            es = 1.0 + 0.05 * (m.get("cubes") or 0)
            return (m["pos"][0], m["pos"][1], m["pos"][2] + 4.5 * es)

        # ================= COMBAT vs mechs =================
        if enemies:
            # target lock: lowest hp-weighted nearest
            cand = min(enemies, key=lambda m: d2(x, y, m["pos"][0], m["pos"][1]) + 0.25 * m.get("hp", 1400))
            if self.target_id is not None and t < self.target_until:
                locked = seen_mech.get(self.target_id)
                if locked is not None:
                    cand = locked
            else:
                self.target_id = cand["id"]
                self.target_until = t + 0.6
            tgt = cand
            tx, ty, tz = tgt["pos"]
            tdist = d2(x, y, tx, ty)
            thp = tgt.get("hp", 1400)
            tcenter = enemy_center(tgt)
            los_t = self.los(muzzle, list(tcenter))
            t_weaps = tgt.get("weapons", []) or []
            flee = False
            if MODE in ("brawler", "duelist"):
                if n_close >= 3:
                    flee = True
                if n_close >= 2 and hp_frac < 0.30:
                    flee = True
                if hp_frac < 0.12 and thp > 200:
                    flee = True
            elif MODE == "berserk":
                if n_close >= 3:
                    flee = True
                if n_close >= 2 and hp_frac < 0.45:
                    flee = True
                if hp_frac < 0.16 and thp > 160:
                    flee = True
            else:
                if n_close >= 2 and not (hp_frac > 0.85 and thp < 250):
                    flee = True
                if hp_frac < 0.30 and thp > 200 and tdist < 140:
                    flee = True
                if hp_frac < 0.55 and thp > hp + 380 and tdist < 90 and n_close >= 1:
                    flee = True
                if t - self.last_damage_t < 0.5 and hp_frac < 0.22 and thp > 120:
                    flee = True

            # desired range by matchup
            have_light = self.slot_of(me, "lightning") >= 0
            have_laser = self.slot_of(me, "laser") >= 0
            have_mortar = self.slot_of(me, "mortar") >= 0
            if have_light:
                desired = 30.0
            elif "lightning" in t_weaps and not have_light:
                desired = 68.0  # stay out of arc range
            elif have_laser:
                desired = 85.0
            elif have_mortar and not los_t:
                desired = 95.0
            else:
                desired = 62.0
            if thp < 280 and hp_frac > 0.5:  # press a finish
                desired = min(desired, 34.0 if have_light else 45.0)
            if MODE == "berserk":
                desired = 26.0 if have_light else (52.0 if have_laser else 40.0)
                if thp < 500 and hp_frac > 0.45:
                    desired = min(desired, 30.0)
            if MODE in ("brawler", "duelist"):
                desired = 20.0 if have_light else 32.0
            juking = False
            if MODE == "duelist":
                if not have_light and "lightning" in t_weaps:
                    desired = max(desired, 58.0)  # don't dive an arc with just a gun
                # juke the moment their torso is actually lined up on us
                brg_to_us = math.atan2(y - ty, x - tx)
                if (abs(wrap((tgt.get("aim") or [0, 0])[0] - brg_to_us)) < 0.14 and tdist < 100
                        and t >= self.juke_at):
                    juking = True
                    self.juke_at = t + 0.45
                    self.strafe_dir = -self.strafe_dir
                    self.strafe_until = t + 0.30

            # strafe clock
            if t >= self.strafe_until:
                self.strafe_dir = self.rng.choice((-1, 1))
                self.strafe_until = t + self.rng.uniform(0.45, 0.95)
            away_x, away_y = x - tx, y - ty
            ad = math.hypot(away_x, away_y) or 1.0
            away_x, away_y = away_x / ad, away_y / ad
            perp_x, perp_y = -away_y * self.strafe_dir, away_x * self.strafe_dir
            radial = 0.0
            if tdist > desired + 9:
                radial = -1.0  # toward target (away vector is from target to us)
            elif tdist < desired - 9:
                radial = 1.0
            mvx = perp_x * 0.95 + away_x * radial
            mvy = perp_y * 0.95 + away_y * radial
            boost = False
            if tgt.get("charging"):
                boost = energy > 15
                mvx, mvy = perp_x, perp_y
            if tdist > 130:
                boost = energy > 30
            if MODE in ("berserk", "brawler") and tdist > desired + 9 and energy > 25:
                boost = True

            if flee:
                # run: away from all threats, biased into the zone
                fx, fy = 0.0, 0.0
                for m in enemies:
                    dd = max(12.0, d2(x, y, m["pos"][0], m["pos"][1]))
                    w = 160.0 / (dd * dd)
                    fx += (x - m["pos"][0]) / dd * w * 160.0
                    fy += (y - m["pos"][1]) / dd * w * 160.0
                zx, zy = next_c[0] - x, next_c[1] - y
                zd = math.hypot(zx, zy) or 1.0
                pull = 0.9 if outside_next > -40 else 0.35
                dest = (clamp(x + fx + zx / zd * 70 * pull, -500, 545),
                        clamp(y + fy + zy / zd * 70 * pull, -325, 380))
                # keep flee destination inside next zone when possible
                dd2 = d2(dest[0], dest[1], next_c[0], next_c[1])
                if dd2 > next_r * 0.92 and next_r > 5:
                    s = (next_r * 0.85) / dd2
                    dest = (next_c[0] + (dest[0] - next_c[0]) * s, next_c[1] + (dest[1] - next_c[1]) * s)
                if MODE == "orbit" and self.anchor is not None and d2(x, y, self.anchor[0], self.anchor[1]) < 110:
                    # flee around the building, not in a straight line
                    cx, cy, r = self.anchor
                    ang = math.atan2(y - cy, x - cx) + self.orbit_dir * 1.15
                    dest = (cx + math.cos(ang) * r, cy + math.sin(ang) * r)
                mv, bst, jmp = self.navigate(me, t, dest, boost=energy > 8, speed=1.0)
                act["move"], act["boost"], act["jump"] = mv, bst, jmp
                act["aim"] = aim_at(*tcenter)[0]  # face/shoot the hunter while running
            else:
                mv, bst, jmp = self.combat_move(me, t, mvx, mvy, boost=boost)
                act["move"], act["boost"], act["jump"] = mv, bst, jmp
                jump_p = 0.16 if MODE in ("brawler", "duelist") else 0.07
                if tdist < 55 and me["grounded"] and (self.rng.random() < jump_p or juking):
                    act["jump"] = True

            # castle: never strafe off our own roof in a fair fight
            if MODE == "castle" and not flee and self.world and (z - self.world.terrain_h(x, y)) > 3.0:
                mvx_, mvy_ = act["move"]
                if (mvx_ or mvy_) and self.surface(x + mvx_ * 6, y + mvy_ * 6, z) < z - 3:
                    for s in (1, -1):
                        rx_, ry_ = -mvy_ * s, mvx_ * s
                        if self.surface(x + rx_ * 6, y + ry_ * 6, z) >= z - 3:
                            act["move"] = [rx_, ry_]
                            break
                    else:
                        act["move"] = [0.0, 0.0]

            # ---- weapon fire control. One torso aim: mortar (lobbed) excludes
            # the others; laser aims direct; gun leads; lightning is yaw-only
            # and stacks with whatever else is firing.
            gun_i = self.slot_of(me, "gun")
            gun = me["weapons"][gun_i] if gun_i >= 0 else None
            gun_speed = (gun["stats"].get("speed", 252) if gun else 252)
            gun_range = (gun["stats"].get("range", 230) if gun else 230)
            fire = [False, False]
            light_i = self.slot_of(me, "lightning")
            laser_i = self.slot_of(me, "laser")
            mortar_i = self.slot_of(me, "mortar")
            light_w = me["weapons"][light_i] if light_i >= 0 else None
            if (light_w is not None and tdist <= light_w["stats"].get("range", 45) * 0.98
                    and los_t and energy > (10 if not flee else 4)):
                fire[light_i] = True
            direct_aim = aim_at(*tcenter)[0]
            gun_aim = aim_at(tcenter[0], tcenter[1], tcenter[2],
                             lead_vel=tgt.get("vel"), proj_speed=gun_speed)[0]
            aim = gun_aim
            gun_ok = gun_i >= 0 and los_t and tdist <= gun_range
            # laser state machine (hold to charge, release to fire)
            laser_busy = False
            if laser_i >= 0:
                lw = me["weapons"][laser_i]
                charge = lw.get("charge") or 0.0
                charge_time = lw["stats"].get("charge", 0.75)
                want = los_t and tdist <= lw["stats"].get("range", 280) and not flee and not (
                    "lightning" in t_weaps and tdist < 55) and not fire[light_i]
                if charge > 0:
                    laser_busy = True
                    aim = direct_aim
                    err = abs(wrap(aim[0] - me["aim"][0])) + abs(aim[1] - me["aim"][1])
                    release = charge >= charge_time * 0.97 and err < 0.08
                    release = release or (not los_t and charge >= lw["stats"].get("min_charge", 0.15))
                    release = release or (charge >= charge_time and err < 0.15)
                    release = release or flee  # never crawl at half speed while running
                    fire[laser_i] = not release  # hold = keep charging
                    gun_ok = gun_ok and tdist < 95
                elif want and (lw.get("cooldown") or 0) <= 0:
                    laser_busy = True
                    fire[laser_i] = True
                    aim = direct_aim
                    gun_ok = gun_ok and tdist < 95
            # mortar: only when it is the right tool and the laser is idle
            mortar_fired = False
            if not laser_busy and not fire[light_i] and mortar_i >= 0 and (not los_t or tdist > 55) and 22 < tdist < 210:
                mw = me["weapons"][mortar_i]
                if (mw.get("cooldown") or 0) <= 0.05:
                    sol = self._mortar_aim(me, tgt, mw)
                    if sol is not None:
                        if abs(wrap(sol[0] - me["aim"][0])) < 0.05 and abs(sol[1] - me["aim"][1]) < 0.05:
                            fire[mortar_i] = True
                            mortar_fired = True
                        aim = sol
                        gun_ok = False
            if gun_ok and not mortar_fired:
                err = abs(wrap(aim[0] - me["aim"][0])) + abs(aim[1] - me["aim"][1])
                if err < 0.10:
                    fire[gun_i] = True
            # gun reload management
            if gun is not None and (gun.get("ammo") or 0) <= 2 and (gun.get("reload") or 0) <= 0:
                if not los_t or tdist > 110 or flee:
                    act["reload"] = True
            act["aim"] = aim
            act["fire"] = fire
            if any(fire):
                self.last_shot_t = t
            self.aim_cmd = list(act["aim"])
            return act

        # ================= no visible mech: threats from memory/sound ====
        healing = hp_frac < (0.38 if MODE == "berserk" else 0.62) and (t - self.last_damage_t) > 1.0
        mem_threat = None
        mem_d = 1e9
        for e in self.enemies.values():
            if t - e["t"] < 10:
                dd = d2(x, y, e["pos"][0], e["pos"][1])
                if dd < mem_d:
                    mem_d, mem_threat = dd, e
        snd_threat = None
        for (st, brg, db, kind) in self.sounds:
            if db in ("near", "mid") and t - st < 3.0:
                snd_threat = (brg, db)
                break
        alert = mem_threat is not None and mem_d < 130
        if MODE == "berserk" and mem_threat is not None and hp_frac > 0.5 and not zone_urgent:
            # hunt the hunter: close on last known position before it resets
            dest = (mem_threat["pos"][0], mem_threat["pos"][1])
            mv, bst, jmp = self.navigate(me, t, dest, boost=energy > 25)
            act["move"], act["boost"], act["jump"] = mv, bst, jmp
            act["aim"] = [math.atan2(dest[1] - y, dest[0] - x), 0.0]
            self.aim_cmd = list(act["aim"])
            return act
        if (healing or hp_frac < 0.3) and (alert or snd_threat) and not zone_urgent:
            # break away quietly and heal
            if mem_threat is not None:
                ax, ay = x - mem_threat["pos"][0], y - mem_threat["pos"][1]
            else:
                ax, ay = -math.cos(snd_threat[0]), -math.sin(snd_threat[0])
            ad = math.hypot(ax, ay) or 1.0
            dest = (x + ax / ad * 70, y + ay / ad * 70)
            mv, bst, jmp = self.navigate(me, t, dest, boost=hp_frac < 0.25 and energy > 20,
                                         speed=0.4 if hp_frac >= 0.25 else 1.0)
            act["move"], act["boost"], act["jump"] = mv, bst, jmp
            if mem_threat is not None:
                act["aim"] = [math.atan2(mem_threat["pos"][1] - y, mem_threat["pos"][0] - x), 0.0]
            self.aim_cmd = list(act["aim"])
            return act

        # ---- supply drops
        drop_dest = None
        for dr in obs.get("drops", []):
            dd = d2(x, y, dr["pos"][0], dr["pos"][1])
            near_enemy = any(t - e["t"] < 9 and d2(dr["pos"][0], dr["pos"][1], e["pos"][0], e["pos"][1]) < 80
                             for e in self.enemies.values())
            if near_enemy:
                continue
            if dr["lands_in"] <= 0:
                if dd < dr["radius"]:
                    if dr.get("holder") in (None, me["id"]):
                        act["move"] = [0.0, 0.0]
                        act["aim"] = [self.scan_yaw + 1.1 * dt * 3, 0.0]
                        self.aim_cmd = list(act["aim"])
                        return act
                elif dd < 190:
                    drop_dest = (dr["pos"][0], dr["pos"][1], dr["pos"][2])
            elif dr["lands_in"] < 16 and dd < 150:
                drop_dest = (dr["pos"][0], dr["pos"][1], dr["pos"][2])

        # ---- zone emergency first
        if zone_urgent:
            dest = (next_c[0], next_c[1])
            mv, bst, jmp = self.navigate(me, t, dest, boost=energy > 12)
            act["move"], act["boost"], act["jump"] = mv, bst, jmp
            act["aim"] = [math.atan2(dest[1] - y, dest[0] - x), 0.0]
            self.aim_cmd = list(act["aim"])
            return act

        # ================= orbit: circle one building forever =================
        if MODE == "orbit" and self.world:
            if self.anchor is None or d2(self.anchor[0], self.anchor[1], next_c[0], next_c[1]) > next_r * 0.95:
                best_a, best_sc = None, 1e18
                for pr in self.world.prisms:
                    wdt, hgt = pr["maxx"] - pr["minx"], pr["maxy"] - pr["miny"]
                    if not (18 <= wdt <= 75 and 18 <= hgt <= 75):
                        continue
                    cx, cy = (pr["minx"] + pr["maxx"]) / 2, (pr["miny"] + pr["maxy"]) / 2
                    if d2(cx, cy, next_c[0], next_c[1]) > next_r * 0.9:
                        continue
                    sc = d2(x, y, cx, cy)
                    if sc < best_sc:
                        best_sc, best_a = sc, (cx, cy, max(wdt, hgt) / 2 + 13.0)
                self.anchor = best_a
            # brief detour for loot at our feet, otherwise keep circling
            loot = None
            for p in obs.get("pickups", []):
                if d2(x, y, p["pos"][0], p["pos"][1]) < 40:
                    loot = (p["pos"][0], p["pos"][1], p["pos"][2])
                    break
            if loot is None:
                for c in obs.get("cubes", []):
                    if d2(x, y, c["pos"][0], c["pos"][1]) < 40:
                        loot = (c["pos"][0], c["pos"][1], c["pos"][2])
                        break
            if loot is not None and d2(x, y, loot[0], loot[1]) > 3:
                mv, bst, jmp = self.navigate(me, t, (loot[0], loot[1]), climb_z=loot[2])
                act["move"], act["boost"], act["jump"] = mv, bst, jmp
                act["aim"] = [math.atan2(loot[1] - y, loot[0] - x), 0.0]
                self.aim_cmd = list(act["aim"])
                return act
            if self.anchor is not None:
                cx, cy, r = self.anchor
                ang = math.atan2(y - cy, x - cx) + self.orbit_dir * 0.55
                dest = (cx + math.cos(ang) * r, cy + math.sin(ang) * r)
                mv, bst, jmp = self.navigate(
                    me, t, dest,
                    boost=(mem_threat is not None and energy > 25),
                    speed=0.45 if healing else 1.0)
                act["move"], act["boost"], act["jump"] = mv, bst, jmp
                if mem_threat is not None:
                    act["aim"] = [math.atan2(mem_threat["pos"][1] - y, mem_threat["pos"][0] - x), 0.0]
                else:
                    act["aim"] = [math.atan2(dest[1] - y, dest[0] - x), 0.0]
                self.aim_cmd = list(act["aim"])
                return act

        # ================= castle: take a roof, stay on it =================
        if MODE == "castle" and self.world and (have_special_c := any(
                w and w["kind"] != "gun" for w in me["weapons"])) or (
                MODE == "castle" and self.world and (z - self.world.terrain_h(x, y)) > 3.0):
            on_roof = (z - self.world.terrain_h(x, y)) > 3.0
            best_roof = None
            best_score = 1e18
            for (rx, ry, rt) in self.roofs:
                if t < self.roof_bad.get((rx, ry), -1.0):
                    continue
                if d2(rx, ry, next_c[0], next_c[1]) > next_r * 0.92:
                    continue
                sc = d2(x, y, rx, ry) - (60.0 if on_roof and d2(x, y, rx, ry) < 32 else 0.0)
                if sc < best_score:
                    best_score, best_roof = sc, (rx, ry, rt)
            if best_roof is not None:
                rx, ry, rt = best_roof
                if self.roof_cur != (rx, ry):
                    self.roof_cur = (rx, ry)
                    self.roof_since = t
                if on_roof and d2(x, y, rx, ry) < 34 and abs(rt - z) < 5:
                    # camp: grab same-roof loot, else hold center and scan
                    dest = None
                    for p in obs.get("pickups", []):
                        if abs(p["pos"][2] - z) < 3 and d2(x, y, p["pos"][0], p["pos"][1]) < 30:
                            dest = (p["pos"][0], p["pos"][1])
                            break
                    if dest is None:
                        for c in obs.get("cubes", []):
                            if abs(c["pos"][2] - z) < 3 and d2(x, y, c["pos"][0], c["pos"][1]) < 30:
                                dest = (c["pos"][0], c["pos"][1])
                                break
                    if dest is not None and d2(x, y, dest[0], dest[1]) > 3:
                        mv, bst, jmp = self.navigate(me, t, dest, climb_z=z)
                        act["move"], act["boost"], act["jump"] = mv, bst, jmp
                        act["aim"] = [math.atan2(dest[1] - y, dest[0] - x), 0.0]
                    elif d2(x, y, rx, ry) > 7:
                        mv, bst, jmp = self.navigate(me, t, (rx, ry), climb_z=rt)
                        act["move"], act["boost"], act["jump"] = mv, bst, jmp
                        act["aim"] = [math.atan2(ry - y, rx - x), 0.0]
                    else:
                        act["move"] = [0.0, 0.0]
                        act["aim"] = [wrap(self.scan_yaw + 1.05 * dt * 3.0), 0.0]
                    self.aim_cmd = list(act["aim"])
                    return act
                # go take the roof
                if t - self.roof_since > 25:
                    self.roof_bad[(rx, ry)] = t + 60
                    self.roof_cur = None
                mv, bst, jmp = self.navigate(me, t, (rx, ry),
                                             boost=d2(x, y, rx, ry) > 80 and energy > 35,
                                             climb_z=rt)
                act["move"], act["boost"], act["jump"] = mv, bst, jmp
                act["aim"] = [math.atan2(ry - y, rx - x), 0.0]
                self.aim_cmd = list(act["aim"])
                return act

        # ================= farming =================
        def zone_ok(px, py):
            if d2(px, py, next_c[0], next_c[1]) < next_r * 0.94:
                return True
            # acceptable if we can still make the next circle comfortably
            need = max(0.0, d2(px, py, next_c[0], next_c[1]) - next_r)
            return (zinfo["shrink_start"] - t) > need / 11.0 + 18

        def hunter_near(px, py, rad=85):
            return any(t - e["t"] < 12 and d2(px, py, e["pos"][0], e["pos"][1]) < rad
                       for e in self.enemies.values())

        best = None  # (score, kind, data)

        def consider(score, kind, data):
            nonlocal best
            if best is None or score < best[0]:
                best = (score, kind, data)

        have_special = any(w and w["kind"] != "gun" for w in me["weapons"])
        special_tiers = [TIER.get(w["kind"], 0) for w in me["weapons"] if w and w["kind"] != "gun"]
        worst_tier = min(special_tiers) if special_tiers else 0
        for pid, (px, py, pz, pt, kind) in self.pick_mem.items():
            if kind == "gun" or kind in held:
                continue
            dd = d2(x, y, px, py)
            if MODE in ("brawler", "duelist") and kind != "lightning" and not have_special and dd > 70:
                continue  # hold out for lightning unless it's right here
            if abs(pz - z) > 20 and dd < 12:
                pass
            if not zone_ok(px, py) or hunter_near(px, py):
                continue
            want = (not have_special) or TIER.get(kind, 0) > worst_tier
            if want:
                consider(dd - (260 if not have_special else 90), "pickup", (px, py, pz, pid))
        for cid, (cx_, cy_, cz_, ct) in self.cubes_mem.items():
            dd = d2(x, y, cx_, cy_)
            if dd < 9 and cid not in [c["id"] for c in obs.get("cubes", [])] and t - ct > 0.3:
                continue  # stale memory under our feet
            if not zone_ok(cx_, cy_) or hunter_near(cx_, cy_, 70):
                continue
            consider(dd - 70, "cube", (cx_, cy_, cz_, cid))
        if self.loot_site is not None:
            lx, ly, lz, lt = self.loot_site
            if t - lt > 25 or d2(x, y, lx, ly) < 5:
                self.loot_site = None
            elif zone_ok(lx, ly):
                consider(d2(x, y, lx, ly) - 320, "cube", (lx, ly, lz, -1))
        if drop_dest is not None:
            consider(d2(x, y, drop_dest[0], drop_dest[1]) - (300 if hp_frac < 0.7 else 160),
                     "drop", drop_dest)
        # NPC farm: safe, healthy, no mech threat memory nearby
        npc_target = None
        if not healing and hp_frac > 0.6 and not alert:
            for n in npcs:
                nd = d2(x, y, n["pos"][0], n["pos"][1])
                if nd < 190 and zone_ok(n["pos"][0], n["pos"][1]) and not hunter_near(n["pos"][0], n["pos"][1], 110):
                    if npc_target is None or nd < d2(x, y, npc_target["pos"][0], npc_target["pos"][1]):
                        npc_target = n
        crate_target = None
        if not healing and hp_frac > 0.55 and not alert:
            for c in obs.get("crates", []):
                cd = d2(x, y, c["pos"][0], c["pos"][1])
                if cd < 170 and zone_ok(c["pos"][0], c["pos"][1]) and not hunter_near(c["pos"][0], c["pos"][1], 100):
                    if crate_target is None or cd < d2(x, y, crate_target["pos"][0], crate_target["pos"][1]):
                        crate_target = c

        if npc_target is not None:
            consider(d2(x, y, npc_target["pos"][0], npc_target["pos"][1]) - 120, "npc", npc_target)
        if crate_target is not None:
            consider(d2(x, y, crate_target["pos"][0], crate_target["pos"][1]) - 55, "crate", crate_target)

        gun_i = self.slot_of(me, "gun")
        gun = me["weapons"][gun_i] if gun_i >= 0 else None

        if best is not None:
            _, kind, data = best
            if kind == "npc":
                n = data
                nd = d2(x, y, n["pos"][0], n["pos"][1])
                nc = (n["pos"][0], n["pos"][1], n["pos"][2] + 1.8)
                los_n = self.los(muzzle, list(nc))
                # hold ~60 m: outside aggro sweet spot, kite as it chases
                if nd > 78 or not los_n:
                    mv, bst, jmp = self.navigate(me, t, (n["pos"][0], n["pos"][1]),
                                                 boost=nd > 130 and energy > 40, climb_z=n["pos"][2])
                    act["move"], act["boost"], act["jump"] = mv, bst, jmp
                elif nd < 42:
                    mv, bst, jmp = self.combat_move(me, t, x - n["pos"][0], y - n["pos"][1])
                    act["move"], act["boost"], act["jump"] = mv, bst, jmp
                else:
                    if t >= self.strafe_until:
                        self.strafe_dir = self.rng.choice((-1, 1))
                        self.strafe_until = t + self.rng.uniform(0.5, 1.0)
                    ax_, ay_ = (x - n["pos"][0]), (y - n["pos"][1])
                    ad = math.hypot(ax_, ay_) or 1.0
                    mv, bst, jmp = self.combat_move(me, t, -ay_ / ad * self.strafe_dir, ax_ / ad * self.strafe_dir)
                    act["move"], act["boost"], act["jump"] = mv, bst, jmp
                aim = aim_at(*nc, lead_vel=n.get("vel"), proj_speed=(gun["stats"].get("speed", 252) if gun else 252))[0]
                act["aim"] = aim
                if gun_i >= 0 and los_n and nd <= 225:
                    err = abs(wrap(aim[0] - me["aim"][0])) + abs(aim[1] - me["aim"][1])
                    fire = [False, False]
                    if err < 0.1:
                        fire[gun_i] = True
                        self.last_shot_t = t
                    act["fire"] = fire
                    if (gun.get("ammo") or 0) <= 2 and (gun.get("reload") or 0) <= 0:
                        act["reload"] = True
                self.aim_cmd = list(act["aim"])
                return act
            if kind == "crate":
                c = data
                cd = d2(x, y, c["pos"][0], c["pos"][1])
                cc = (c["pos"][0], c["pos"][1], c["pos"][2] + 1.2)
                los_c = self.los(muzzle, list(cc))
                if cd > 95 or not los_c:
                    mv, bst, jmp = self.navigate(me, t, (c["pos"][0], c["pos"][1]),
                                                 boost=cd > 140 and energy > 40, climb_z=c["pos"][2])
                    act["move"], act["boost"], act["jump"] = mv, bst, jmp
                    act["aim"] = [math.atan2(c["pos"][1] - y, c["pos"][0] - x), 0.0]
                else:
                    act["move"] = [0.0, 0.0]
                    aim = aim_at(*cc)[0]
                    act["aim"] = aim
                    if gun_i >= 0:
                        fire = [False, False]
                        err = abs(wrap(aim[0] - me["aim"][0])) + abs(aim[1] - me["aim"][1])
                        if err < 0.1:
                            fire[gun_i] = True
                            self.last_shot_t = t
                        act["fire"] = fire
                self.aim_cmd = list(act["aim"])
                return act
            # pickup / cube / drop: just go there
            dest = (data[0], data[1])
            dd = d2(x, y, dest[0], dest[1])
            sneak = healing and (alert or snd_threat)
            mv, bst, jmp = self.navigate(me, t, dest,
                                         boost=dd > 70 and energy > 35 and not sneak,
                                         speed=0.4 if sneak else 1.0,
                                         climb_z=data[2] if len(data) > 2 else None)
            act["move"], act["boost"], act["jump"] = mv, bst, jmp
            act["aim"] = [math.atan2(dest[1] - y, dest[0] - x), 0.0]
            if alert and mem_threat is not None:
                act["aim"] = [math.atan2(mem_threat["pos"][1] - y, mem_threat["pos"][0] - x), 0.0]
            self.aim_cmd = list(act["aim"])
            return act

        # ================= idle: stage inside next zone, scan =================
        if outside_next > -next_r * 0.45:
            dest = next_c
            mv, bst, jmp = self.navigate(me, t, dest, boost=energy > 30 and outside_next > 40)
            act["move"], act["boost"], act["jump"] = mv, bst, jmp
            act["aim"] = [math.atan2(dest[1] - y, dest[0] - x), 0.0]
        else:
            if self.wander_pt is None or t > self.wander_until or d2(x, y, self.wander_pt[0], self.wander_pt[1]) < 4:
                ang = self.rng.uniform(0, TAU)
                rr = self.rng.uniform(15, 55)
                wx = clamp(x + math.cos(ang) * rr, next_c[0] - next_r * 0.8, next_c[0] + next_r * 0.8)
                wy = clamp(y + math.sin(ang) * rr, next_c[1] - next_r * 0.8, next_c[1] + next_r * 0.8)
                self.wander_pt = (wx, wy)
                self.wander_until = t + 14
            mv, bst, jmp = self.navigate(me, t, self.wander_pt, boost=False,
                                         speed=0.45 if healing else 0.8)
            act["move"], act["boost"], act["jump"] = mv, bst, jmp
            yaw = self.scan_yaw + 1.05 * dt * 3.0
            act["aim"] = [wrap(yaw), 0.0]
        self.aim_cmd = list(act["aim"])
        return act

    def _mortar_aim(self, me, tgt, mw):
        x, y, z = me["pos"]
        st = mw["stats"]
        v = st.get("speed", 72)
        g = st.get("gravity", 22)
        mz = z + 9.5
        tp = tgt["pos"]
        dx, dy = tp[0] - x, tp[1] - y
        d = math.hypot(dx, dy)
        if d < 8:
            return None
        # flight-time lead iteration
        lx, ly = tp[0], tp[1]
        pitch = 0.7
        for _ in range(3):
            dz = (tp[2] + 3.0) - mz
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0:
                return None
            pitch = math.atan((v * v - math.sqrt(disc)) / (g * d))
            ft = d / max(5.0, v * math.cos(pitch))
            vel = tgt.get("vel", [0, 0, 0])
            lx, ly = tp[0] + vel[0] * ft, tp[1] + vel[1] * ft
            dx, dy = lx - x, ly - y
            d = math.hypot(dx, dy)
            if d < 8:
                return None
        return [math.atan2(dy, dx), clamp(pitch, -0.7, 1.3)]


mf.run(Bot)
