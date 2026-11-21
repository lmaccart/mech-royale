# Mech Royale bot

A survival-duelist bot for the Builders Buildathon Mech Royale track
(`mechfight/1`, 16-mech 3D battle royale). Score is placement:
`mean(16 - place)` over 48 practice matches against a 15-bot squad that
shares vision and hunts you (one hunter early, all of them late).
Practice best on the board at the time of writing: ~6.38.

## Layout

| File | What |
| --- | --- |
| `bot.py` | The bot. Stdlib only, imports the kit wrapper. |
| `mechfight.py` | Kit wrapper (protocol loop, newest-observation rule). Unmodified. |
| `Dockerfile` | `python:3.13-slim`, entrypoint `bot.py` at repo root. |

Built from the official `mechfight-kit.tar.gz` (kit `python/` starter).
No third-party code or pre-weekend code beyond that kit.

## Strategy

Placement, not kills — but in practice the squad never fights itself, so
every placement gained has to be earned by outliving/killing hunters.

- **Farm between fights.** Nearest weapon pickup first (lightning > laser >
  mortar), then cubes, supply drops (3 cubes + full heal, only uncontested),
  NPC drones (2 cubes, kited at ~60 m) and crates — all filtered to stay
  inside the next zone and away from remembered hunters.
- **Win the 1v1, refuse the 2v1.** Target-locked dueling with weapon-triangle
  range bands: close to 30 m with lightning (240 DPS + gun stacked), stay
  outside 45 m against enemy lightning, laser at 85 m, mortar lobs over
  cover with a ballistic solver when there is no line of sight. Constant
  randomized strafing, harder jukes against a charging laser.
- **Kite and heal.** Outnumbered, low, or losing a duel: flee through the
  building A* toward the zone while facing/shooting the pursuer, break line
  of sight, then exploit the 5 s / 45 HP-per-s regen. Moving under 5 m/s is
  silent (no step sounds) while sneaking.
- **Zone discipline.** Urgency from the shrink clock; loot is only chased
  if the next circle is still makeable. Full heal drops are contested only
  when no hunter is near.
- **Upgrades.** Reactive armor > plating > repair nanites > gun damage/rate,
  then weapon-specific picks for what is actually held.

## World model

Loaded in `hello` (map at `/arena` in matches):

- Heightfield grid (4 m) over terrain + building roof surfaces from
  `v2/world.json` prisms / `v2/terrain.i16`, with precomputed edge
  passability at the 1.2 m step-up. A* (bounded expansions, best-effort
  endpoint) replans at most every 1.6 s; greedy line-of-sight shortcutting;
  jump/jet climbing for roof loot; stuck detection with sidestep recovery.
- Line-of-sight rays against prism volumes + terrain for fire control
  (don't dump the gun into a wall; switch to mortar instead).

Measured with `check/check.py`: p50 0.2 ms, p99 < 1 ms, worst ~20 ms
(local machine) against the 50 ms budget.

## Test

```sh
python3 kit/check/check.py -- python3 -u bot.py
docker build -t mech-royale .
python3 kit/check/check.py --map-dir /arena -- docker run --rm -i --network none \
  --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m \
  --user 65534:65534 -v "$PWD/kit/arena:/arena:ro" mech-royale
```

(Kit files `check/` and `arena/` are dev-only and git-ignored here;
download `mechfight-kit.tar.gz` from the track page to reproduce.)
