"""
Where each node goes on the map.

Two layouts, both precomputed here so the browser never runs a physics
simulation over 13,000 nodes:

  atlas  - one island per subsystem, packed, members arranged inside by type.
           Deterministic, stdlib only, and legible: you can find a thing.
  force  - Fruchterman-Reingold, seeded from the atlas so the subsystem
           structure survives.  Needs numpy; skipped without it.

The atlas is the default.  A force layout of a power-law graph puts the hubs
in the middle and everything else in a ring around them, which is pretty and
tells you almost nothing.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Sequence, Tuple

GOLDEN = math.pi * (3.0 - math.sqrt(5.0))     # 2.39996 rad


# ---------------------------------------------------------------------------
# Atlas
# ---------------------------------------------------------------------------

def _pack(radii: Sequence[float]) -> List[Tuple[float, float]]:
    """Place discs, biggest first, on an expanding spiral without overlap."""
    placed: List[Tuple[float, float, float]] = []
    out: List[Tuple[float, float]] = [(0.0, 0.0)] * len(radii)
    order = sorted(range(len(radii)), key=lambda i: -radii[i])

    for i in order:
        r = radii[i]
        if not placed:
            placed.append((0.0, 0.0, r))
            out[i] = (0.0, 0.0)
            continue
        step = max(2.0, r * 0.35)
        found = None
        turn = 0.0
        while found is None:
            turn += 0.35
            rad = step * turn * 0.55
            x, y = rad * math.cos(turn), rad * math.sin(turn)
            if all((x - px) ** 2 + (y - py) ** 2 > (r + pr + 6.0) ** 2
                   for px, py, pr in placed):
                found = (x, y)
        placed.append((found[0], found[1], r))
        out[i] = found
    return out


def atlas(types: Sequence[str], subs: Sequence[str],
          type_order: Sequence[str]) -> List[float]:
    """One island per subsystem.  Returns a flat [x0,y0,x1,y1,...] list."""
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, s in enumerate(subs):
        groups[s or "(ungrouped)"].append(i)

    keys = sorted(groups, key=lambda s: (-len(groups[s]), s))
    radii = [2.6 * math.sqrt(len(groups[k])) + 7.0 for k in keys]
    centres = _pack(radii)

    rank = {t: n for n, t in enumerate(type_order)}
    xy = [0.0] * (2 * len(types))

    for key, (cx, cy), radius in zip(keys, centres, radii):
        members = sorted(groups[key],
                         key=lambda i: (rank.get(types[i], 99), types[i]))
        n = len(members)
        for k, node in enumerate(members):
            # phyllotaxis fills a disc evenly, and because members are sorted
            # by type the types come out as rings rather than a speckle
            rr = radius * 0.88 * math.sqrt((k + 0.5) / n)
            th = k * GOLDEN
            xy[2 * node] = cx + rr * math.cos(th)
            xy[2 * node + 1] = cy + rr * math.sin(th)

    return [round(v, 2) for v in xy]


def islands(subs: Sequence[str], xy: Sequence[float]) -> List[Dict]:
    """Label and radius for each subsystem island, for the viewer to draw."""
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, s in enumerate(subs):
        groups[s or "(ungrouped)"].append(i)
    out = []
    for key, members in groups.items():
        xs = [xy[2 * i] for i in members]
        ys = [xy[2 * i + 1] for i in members]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        r = max(math.hypot(x - cx, y - cy) for x, y in zip(xs, ys)) + 4
        out.append({"name": key, "x": round(cx, 2), "y": round(cy, 2),
                    "r": round(r, 2), "n": len(members)})
    return sorted(out, key=lambda d: -d["n"])


#: Top to bottom, the order a request travels: a user picks a menu option, a CL
#: driver sets up, a program runs, it leans on service modules, they share
#: record layouts and screens, and it all lands on data.  Anything with no tier
#: - the invented placeholder nodes for calls we could not resolve - sits at the
#: bottom, where it is visible rather than hidden.
TIER_ORDER = ("entry", "driver", "program", "service", "contract", "data", "")

#: Rough aspect of the whole drawing.  Wide, because there are six or seven
#: bands and up to twenty-one libraries, and the libraries are the axis that
#: needs the room.
LAYERED_WIDTH = 1600.0
LAYERED_HEIGHT = 900.0


def layered(tiers: Sequence[str], libs: Sequence[str]) -> List[float]:
    """Tier as a horizontal band, library as a column within it.

    The two questions the segregation was for, on one screen: how far is this
    member from something a user can start, and whose library is it in.  A
    subsystem atlas answers neither - it groups by a name prefix and says
    nothing at all about direction of flow.

    Bands run in TIER_ORDER rather than by size, because the order *is* the
    information.  Columns are ordered by library name so a library keeps the
    same column in every band and the eye can follow it downwards.
    """
    present = [t for t in TIER_ORDER if any(t == x for x in tiers)]
    if not present:
        return [0.0] * (2 * len(tiers))

    libraries = sorted({l for l in libs if l})
    columns_ = libraries + [""]             # "" is the no-library column
    col_of = {name: n for n, name in enumerate(columns_)}

    # A band is tall in proportion to the square root of what it holds, so a
    # band of 3,600 files does not squeeze the other five into a hairline.
    counts = {t: sum(1 for x in tiers if x == t) for t in present}
    weights = {t: math.sqrt(counts[t]) or 1.0 for t in present}
    total = sum(weights.values())

    xy = [0.0] * (2 * len(tiers))
    col_width = LAYERED_WIDTH / max(1, len(columns_))
    y_cursor = 0.0

    for tier in present:
        band_height = LAYERED_HEIGHT * weights[tier] / total
        buckets: Dict[int, List[int]] = defaultdict(list)
        for i, t in enumerate(tiers):
            if t == tier:
                buckets[col_of.get(libs[i] or "", len(columns_) - 1)].append(i)

        for column, members in buckets.items():
            n = len(members)
            across = max(1, int(math.ceil(math.sqrt(
                n * col_width / max(band_height, 1.0)))))
            down = max(1, int(math.ceil(n / across)))
            cell_w = col_width * 0.86 / across
            cell_h = band_height * 0.78 / down
            x0 = column * col_width + col_width * 0.07
            y0 = y_cursor + band_height * 0.11

            for k, node in enumerate(members):
                xy[2 * node] = x0 + (k % across) * cell_w + cell_w * 0.5
                xy[2 * node + 1] = y0 + (k // across) * cell_h + cell_h * 0.5

        y_cursor += band_height

    return [round(v, 2) for v in xy]


def bands(tiers: Sequence[str], xy: Sequence[float]) -> List[Dict]:
    """Label and vertical extent of each tier band, for the viewer to draw."""
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, t in enumerate(tiers):
        groups[t or "(no tier)"].append(i)
    out = []
    for key, members in groups.items():
        ys = [xy[2 * i + 1] for i in members]
        out.append({"name": key, "y0": round(min(ys), 2),
                    "y1": round(max(ys), 2), "n": len(members)})
    order = {t or "(no tier)": n for n, t in enumerate(TIER_ORDER)}
    return sorted(out, key=lambda d: order.get(d["name"], 99))


def lanes(libs: Sequence[str], xy: Sequence[float]) -> List[Dict]:
    """Label and horizontal extent of each library column."""
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, l in enumerate(libs):
        groups[l or "(library unknown)"].append(i)
    out = []
    for key, members in groups.items():
        xs = [xy[2 * i] for i in members]
        out.append({"name": key, "x0": round(min(xs), 2),
                    "x1": round(max(xs), 2), "n": len(members)})
    return sorted(out, key=lambda d: d["x0"])


# ---------------------------------------------------------------------------
# Force
# ---------------------------------------------------------------------------

def force(n_nodes: int, pairs: Sequence[Tuple[int, int]],
          deg: Sequence[int], seed_xy: Sequence[float],
          iterations: int = 400, samples: int = 40) -> List[float]:
    """Fruchterman-Reingold with sampled repulsion.  Returns [] without numpy.

    Repulsion is estimated from a random sample of other nodes each step
    rather than all pairs - 13,000 nodes is 88M pairs an iteration otherwise.
    Attraction along an edge is damped by the degree of its endpoints, or the
    handful of copybooks that 3,211 members COPY drag the whole map into a
    single knot.
    """
    try:
        import numpy as np
    except ImportError:
        return []

    if not pairs:
        return list(seed_xy)

    rng = np.random.default_rng(20260810)         # deterministic builds
    pos = np.asarray(seed_xy, dtype=np.float64).reshape(-1, 2).copy()
    pos += rng.normal(0.0, 0.3, pos.shape)        # break exact ties

    src = np.fromiter((s for s, _ in pairs), dtype=np.int64, count=len(pairs))
    dst = np.fromiter((t for _, t in pairs), dtype=np.int64, count=len(pairs))

    d = np.asarray(deg, dtype=np.float64)
    weight = 1.0 / np.sqrt(np.maximum(1.0, np.minimum(d[src], d[dst])))

    span = float(np.abs(pos).max()) * 2.0 or 1000.0
    k = math.sqrt((span * span) / max(1, n_nodes))
    temp = span * 0.06
    cool = temp / (iterations + 1)

    for _ in range(iterations):
        disp = np.zeros_like(pos)

        # -- attraction along edges
        delta = pos[dst] - pos[src]
        dist = np.maximum(np.sqrt((delta ** 2).sum(1)), 1e-4)
        f = (dist / k) * weight
        unit = delta / dist[:, None]
        pull = unit * f[:, None]
        for axis in (0, 1):
            disp[:, axis] += np.bincount(src, pull[:, axis], minlength=n_nodes)
            disp[:, axis] -= np.bincount(dst, pull[:, axis], minlength=n_nodes)

        # -- repulsion from a random sample, scaled back up to the full graph
        idx = rng.integers(0, n_nodes, size=(n_nodes, samples))
        rd = pos[:, None, :] - pos[idx]
        rdist = np.maximum(np.sqrt((rd ** 2).sum(2)), 1e-3)
        rf = (k * k) / rdist * (n_nodes / samples)
        disp += (rd / rdist[:, :, None] * rf[:, :, None]).sum(1)

        # -- move, capped by the falling temperature
        length = np.maximum(np.sqrt((disp ** 2).sum(1)), 1e-9)
        pos += disp / length[:, None] * np.minimum(length, temp)[:, None]
        temp -= cool

    pos -= pos.mean(0)
    return [round(float(v), 2) for v in pos.reshape(-1)]
