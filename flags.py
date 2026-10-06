"""What is worth pointing at, from market data alone.

WHAT THIS CANNOT DO, stated first because it is the thing people expect.

Reverse line movement - the line moving against where the public money is -
is the flag most people mean by "interesting". It needs ticket and handle
percentages. The Odds API does not carry them and neither does any free
source. Nothing in this file is reverse line movement, nothing here should be
read as sharp money, and the page says so. A steam move is many books agreeing
quickly; whether the money behind it was smart is not visible here.

WHAT IT CAN DO, all of it computable from a change log of prices:

  steam       several books moving the same way inside a short window
  key number  a spread or total crossing a number games actually land on
  outlier     one book sitting well off what everyone else has
  move        the distance from first sighting to now, and how fast
  freeze      a line that has not moved while its neighbours have

Each flag answers "what happened", never "what will happen". That distinction
is the entire difference between this file and a model, and a model is what
you said you did not want.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

import config as C

log = logging.getLogger("flags")


def _latest(moves: pd.DataFrame) -> pd.DataFrame:
    """The current value of every line. The change log's last row per key."""
    if moves.empty:
        return moves
    return (moves.sort_values("ts")
            .groupby(["event_id", "book", "market", "side"], as_index=False)
            .last())


def _first(moves: pd.DataFrame) -> pd.DataFrame:
    """The FIRST value we ever saw. Not the opening line.

    The true opener is whatever the book hung before anyone was watching, and
    buying it costs ten times a normal call on the historical endpoint. This
    is the first number this project saw, which is a different and more
    honest thing, and the page labels it "first seen" for that reason.
    """
    if moves.empty:
        return moves
    return (moves.sort_values("ts")
            .groupby(["event_id", "book", "market", "side"], as_index=False)
            .first())


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _value(df: pd.DataFrame) -> pd.Series:
    """The number that MOVED, per market.

    For spreads and totals that is the point; for a moneyline there is no
    point and the price is the line. Treating them alike would either compare
    a half-point to ten cents or drop the moneyline entirely.
    """
    pt, pr = _num(df["point"]), _num(df["price"])
    return np.where(df["market"].isin(["spreads", "totals"]), pt, pr)


def movement(moves: pd.DataFrame) -> pd.DataFrame:
    """First seen against now, per game, market and side, across books.

    The consensus is the MEDIAN across books rather than the mean, because
    one book with a stale number drags a mean and barely touches a median,
    and a stale number is exactly what the outlier flag is for.
    """
    if moves.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "first",
                                     "now", "move", "books", "n_changes"])
    m = moves.copy()
    m["val"] = _value(m)
    m = m.dropna(subset=["val"])
    if m.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "first",
                                     "now", "move", "books", "n_changes"])

    f = _first(m).groupby(["event_id", "market", "side"])["val"].median()
    l = _latest(m).groupby(["event_id", "market", "side"])["val"].median()
    books = (_latest(m).groupby(["event_id", "market", "side"])["book"]
             .nunique())
    n = m.groupby(["event_id", "market", "side"]).size()

    out = pd.DataFrame({"first": f, "now": l, "books": books,
                        "n_changes": n}).reset_index()
    out["move"] = out["now"] - out["first"]
    return out


def steam(moves: pd.DataFrame, window_min: int = None,
          min_books: int = None) -> pd.DataFrame:
    """Several books moving the SAME WAY inside a short window.

    Direction is what makes it steam. Books drift apart and back together all
    day; counting books that merely changed would flag every game every hour.
    What is unusual is agreement - four books moving the same direction within
    the hour is a different event from four books moving in four directions.
    """
    window_min = window_min or C.STEAM_WINDOW_MIN
    min_books = min_books or C.STEAM_BOOKS
    if moves.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "books",
                                     "direction", "size", "ts"])
    m = moves.copy()
    m["val"] = _value(m)
    m = m.dropna(subset=["val"]).sort_values("ts")
    if m.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "books",
                                     "direction", "size", "ts"])

    # The change each row represents, within its own line's history.
    m["prev"] = m.groupby(["event_id", "book", "market", "side"])["val"].shift(1)
    m["delta"] = m["val"] - m["prev"]
    m = m.dropna(subset=["delta"])
    m = m[m["delta"].abs() > 0]
    if m.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "books",
                                     "direction", "size", "ts"])

    cut = m["ts"].max() - timedelta(minutes=window_min)
    recent = m[m["ts"] >= cut]
    if recent.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "books",
                                     "direction", "size", "ts"])

    recent = recent.assign(dir=np.sign(recent["delta"]))
    g = recent.groupby(["event_id", "market", "side", "dir"])
    agg = g.agg(books=("book", "nunique"), size=("delta", "sum"),
                ts=("ts", "max")).reset_index()
    hit = agg[agg["books"] >= min_books].copy()
    hit["direction"] = np.where(hit["dir"] > 0, "up", "down")
    return hit.drop(columns=["dir"]).sort_values("books", ascending=False)


def key_crossings(moves: pd.DataFrame, sport: str) -> pd.DataFrame:
    """A spread or total that moved ACROSS a number games land on.

    Only football really earns this. Margins there pile up on 3 and 7 because
    of how scoring works, so moving from 2.5 to 3.5 passes through far more
    probability than moving from 5.5 to 6.5. Basketball and hockey have much
    flatter distributions, which is why their entries in config are short and
    the page says they are weaker.
    """
    keys = C.KEY_NUMBERS.get(sport)
    cols = ["event_id", "market", "side", "from", "to", "crossed"]
    if not keys or moves.empty:
        return pd.DataFrame(columns=cols)
    mv = movement(moves)
    mv = mv[mv["market"].isin(["spreads", "totals"])]
    if mv.empty:
        return pd.DataFrame(columns=cols)

    rows = []
    for _, r in mv.iterrows():
        a, b = r["first"], r["now"]
        if not (np.isfinite(a) and np.isfinite(b)) or a == b:
            continue
        lo, hi = (a, b) if a < b else (b, a)
        # Both signs: a spread of -3 and +3 are the same key number seen from
        # the two sides, and only checking the positive one would miss every
        # favourite.
        crossed = [k for k in keys
                   if (lo < k < hi) or (lo < -k < hi)]
        if crossed:
            rows.append({"event_id": r["event_id"], "market": r["market"],
                         "side": r["side"], "from": a, "to": b,
                         "crossed": crossed})
    return pd.DataFrame(rows, columns=cols)


def outliers(moves: pd.DataFrame) -> pd.DataFrame:
    """One book well off the consensus. The only flag that is a BET.

    Everything else here describes what the market did. This one says where
    you could get a different number than everyone else is offering, which is
    the only thing on the page that is directly actionable - and usually it
    means that book is slow, which is exactly why it is worth knowing.
    """
    cols = ["event_id", "market", "side", "book", "value", "consensus", "diff"]
    if moves.empty:
        return pd.DataFrame(columns=cols)
    cur = _latest(moves).copy()
    cur["val"] = _value(cur)
    cur = cur.dropna(subset=["val"])
    if cur.empty:
        return pd.DataFrame(columns=cols)

    cons = (cur.groupby(["event_id", "market", "side"])["val"]
            .median().rename("consensus"))
    n = cur.groupby(["event_id", "market", "side"])["book"].nunique()
    cur = cur.merge(cons, on=["event_id", "market", "side"], how="left")
    cur = cur.merge(n.rename("n_books"), on=["event_id", "market", "side"],
                    how="left")
    # Two books cannot have an outlier between them: with one on each side of
    # the median, both are equally far from it and neither is the odd one out.
    cur = cur[cur["n_books"] >= 3]
    if cur.empty:
        return pd.DataFrame(columns=cols)
    cur["diff"] = cur["val"] - cur["consensus"]
    lim = cur["market"].map(C.OUTLIER_MIN).fillna(np.inf)
    hit = cur[cur["diff"].abs() >= lim].copy()
    hit = hit.rename(columns={"val": "value"})
    return hit[cols].sort_values("diff", key=abs, ascending=False)


def frozen(moves: pd.DataFrame, events: list, hours: int = None) -> pd.DataFrame:
    """A line that has not moved while the rest of the board has.

    Only interesting for a game that is actually close to starting. A line
    for next Sunday has not moved because nobody has bet it yet, which is not
    news; one that has sat still through the afternoon of game day is.
    """
    hours = hours or C.FREEZE_HOURS
    cols = ["event_id", "market", "side", "last_move", "hours_still"]
    if moves.empty:
        return pd.DataFrame(columns=cols)
    cur = _latest(moves)
    now = moves["ts"].max()
    still = (now - cur["ts"]).dt.total_seconds() / 3600.0
    out = cur.assign(hours_still=still)
    out = out[out["hours_still"] >= hours]
    if out.empty:
        return pd.DataFrame(columns=cols)
    # Only games starting soon enough for stillness to mean anything.
    soon = {e.get("id") for e in events or []
            if _starts_within(e, hours=36)}
    out = out[out["event_id"].isin(soon)]
    return (out.rename(columns={"ts": "last_move"})[cols]
            .sort_values("hours_still", ascending=False))


def _starts_within(event: dict, hours: int) -> bool:
    try:
        t = pd.Timestamp(event.get("commence_time"))
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        now = pd.Timestamp.utcnow().tz_localize("UTC") \
            if pd.Timestamp.utcnow().tzinfo is None else pd.Timestamp.utcnow()
        return pd.Timedelta(0) <= (t - now) <= pd.Timedelta(hours=hours)
    except Exception:                                          # noqa: BLE001
        return False


def notable(moves: pd.DataFrame, sport: str, events: list) -> dict:
    """Everything worth saying about one sport, in one call."""
    mv = movement(moves)
    big = pd.DataFrame(columns=mv.columns)
    if not mv.empty:
        lim = mv["market"].map(C.MOVE_MIN).fillna(np.inf)
        big = mv[mv["move"].abs() >= lim].copy()
        big = big.sort_values("move", key=abs, ascending=False)
    return {
        "movement": mv,
        "big_moves": big,
        "steam": steam(moves),
        "key_numbers": key_crossings(moves, sport),
        "outliers": outliers(moves),
        "frozen": frozen(moves, events),
    }
