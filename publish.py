"""Turn the change log into what the page reads.

The page is static and gets no server, so everything it needs has to be a
file. One JSON per sport, plus a small index, written into docs/data/.

TWO THINGS ARE KEPT SMALL ON PURPOSE.

The history each game carries is THINNED, not truncated: the consensus line
at each change, down-sampled so a game that moved two hundred times still
draws a readable sparkline. Truncating instead would throw away the early
part, which is exactly the part "it opened at" needs.

And `allow_nan=False` with a `json.loads` round-trip is the gate before
anything is written. `NaN` is not JSON - Python writes it happily and every
browser refuses the file - and a page that silently fails to load is worse
than one that is obviously empty.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config as C
import flags as F
import store as S

log = logging.getLogger("publish")

OUT = Path(__file__).resolve().parent / "docs" / "data"
MAX_POINTS = 60          # per line, in the sparkline


def _clean(v):
    """Anything that cannot survive JSON becomes null, early and once."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if not math.isfinite(f) else round(f, 4)
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def _thin(points: list, cap: int = MAX_POINTS) -> list:
    """Keep the shape, keep the ends, drop the middle evenly.

    The first and last points are always kept: they are "first seen" and
    "now", which the page quotes as numbers, and losing either to a sampler
    would make the headline disagree with the chart under it.
    """
    if len(points) <= cap:
        return points
    keep = np.linspace(0, len(points) - 1, cap).round().astype(int)
    keep = sorted(set(keep.tolist()) | {0, len(points) - 1})
    return [points[i] for i in keep]


def _series(moves: pd.DataFrame, event_id: str, market: str,
            side: str) -> list:
    """The consensus value over time for one line, as [ts, value] pairs."""
    m = moves[(moves["event_id"] == event_id) & (moves["market"] == market)
              & (moves["side"] == side)].copy()
    if m.empty:
        return []
    m["val"] = F._value(m)
    m = m.dropna(subset=["val"]).sort_values("ts")
    if m.empty:
        return []
    # One point per CHANGE, taking the median across whichever books had
    # reported by then. A per-book series would be four times the data and
    # four overlapping lines nobody can read at sparkline size.
    g = m.groupby("ts")["val"].median()
    pts = [[t.isoformat(timespec="minutes"), _clean(v)] for t, v in g.items()]
    return _thin(pts)


def _titles() -> dict:
    """Display names from the API's own catalogue, if collect.py has saved
    one. Falls back to the key with its underscores opened out - ugly, but
    never blank, and the first real poll replaces it."""
    p = S.EVENTS.parent / "sports.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def build_sport(sport: str, days: int = 7) -> dict:
    events = S.load_events(sport)
    moves = S.read_moves(sport, days=days)
    n = F.notable(moves, sport, events)
    mv = n["movement"]

    by_event = {}
    for e in events:
        eid = e.get("id")
        if not eid:
            continue
        by_event[eid] = {
            "id": eid,
            "home": e.get("home_team"),
            "away": e.get("away_team"),
            "start": e.get("commence_time"),
            "lines": {},
            "flags": [],
            "tracked": False,
        }

    # The current state of each line, plus where it came from.
    for _, r in mv.iterrows():
        g = by_event.get(r["event_id"])
        if g is None:
            continue
        g["tracked"] = True
        g["lines"].setdefault(r["market"], {})[r["side"]] = {
            "first": _clean(r["first"]), "now": _clean(r["now"]),
            "move": _clean(r["move"]), "books": _clean(r["books"]),
            "changes": _clean(r["n_changes"]),
            "series": _series(moves, r["event_id"], r["market"], r["side"]),
        }

    def add(df, kind, fmt):
        for _, r in df.iterrows():
            g = by_event.get(r["event_id"])
            if g is not None:
                g["flags"].append({"kind": kind, "text": fmt(r)})

    add(n["steam"], "steam",
        lambda r: f"{int(r['books'])} books moved {r['direction']} on "
                  f"{r['market']} ({r['side']})")
    add(n["key_numbers"], "key",
        lambda r: f"{r['market']} crossed "
                  f"{', '.join(str(k) for k in r['crossed'])} "
                  f"({r['from']:+g} to {r['to']:+g})")
    add(n["outliers"], "outlier",
        lambda r: f"{r['book']} is {r['diff']:+g} off the consensus on "
                  f"{r['market']} ({r['side']})")
    add(n["frozen"], "frozen",
        lambda r: f"{r['market']} ({r['side']}) has not moved in "
                  f"{r['hours_still']:.0f}h")
    add(n["big_moves"], "move",
        lambda r: f"{r['market']} ({r['side']}) moved {r['move']:+g} "
                  f"from first seen")

    games = sorted(by_event.values(), key=lambda g: (g["start"] or "", g["id"]))
    return {
        "sport": sport,
        "title": C.SPORTS.get(sport, {}).get("title", sport),
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": games,
        "n_tracked": sum(1 for g in games if g["tracked"]),
        "n_flagged": sum(1 for g in games if g["flags"]),
    }


def write(days: int = 7) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    titles = _titles()
    index = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "sports": []}
    for sport in C.SPORTS:
        data = build_sport(sport, days=days)
        if not data["games"]:
            continue
        p = OUT / f"{sport}.json"
        # THE GATE. allow_nan=False refuses to write a file no browser can
        # parse, and the round-trip proves the bytes on disk load back.
        text = json.dumps(data, allow_nan=False, separators=(",", ":"))
        json.loads(text)
        p.write_text(text)
        index["sports"].append({
            "key": sport,
            "title": (titles.get(sport, {}).get("title")
                      or sport.split("_", 1)[-1].replace("_", " ").upper()),
            "group": titles.get(sport, {}).get("group", ""),
            "games": len(data["games"]),
            "tracked": data["n_tracked"],
            "flagged": data["n_flagged"],
            "file": f"data/{sport}.json",
        })
    # SPORTS WITH LINE HISTORY FIRST, then by how much is flagged, then by
    # size. Ordering by game count alone buried the second tracked sport
    # behind four that had only a schedule, which made a working site look
    # like it was watching exactly one thing.
    index["sports"].sort(key=lambda s: (s["tracked"] == 0, -s["flagged"],
                                        -s["games"], s["key"]))
    text = json.dumps(index, allow_nan=False, separators=(",", ":"))
    json.loads(text)
    (OUT / "index.json").write_text(text)
    return index


def main() -> int:
    idx = write()
    print(f"{len(idx['sports'])} sport file(s) written")
    for s in idx["sports"]:
        print(f"  {s['key']:<32s} {s['games']:>4d} games, "
              f"{s['tracked']:>4d} tracked, {s['flagged']:>4d} flagged")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
