"""Do the flags predict anything? Measured on finished games, not asserted.

THE PROBLEM THIS EXISTS TO FIX. The page ships five flags - steam, key
numbers, outliers, freezes, movement - and not one of them has ever been
checked. They were chosen because they are the things people talk about, which
is not evidence. A flag that predicts nothing but looks meaningful is worse
than no flag: it costs attention and it invites bets.

WHAT IS GRADED, and what each one would have to do to earn its place:

  steam     several books moved the same way. If that means anything, the
            line should keep going that way by close. If it reverts as often
            as it continues, steam is just noise with a name.

  outlier   one book off the consensus. There are two stories and they point
            opposite ways: either that book is SLOW and will come back to the
            field (worthless), or it is EARLY and the field will come to it
            (the only flag on the page that is actually a bet). The measure
            settles which.

  key       a spread crossed 3 or 7. If key numbers matter, a line that
            crosses one should stick there rather than drift back.

EVERY MEASURE NEEDS A CONTROL, and that is most of the work here. "Lines that
steamed moved 0.4 more points" is not a finding - lines that merely moved also
keep moving, and without the comparison you are measuring momentum and calling
it steam.

NO LOOK-AHEAD. A flag seen at time T is graded only on what happened strictly
after T, and the closing line is the last value recorded before the game
started. Grading a flag against a line it already contained would make
everything look brilliant.

    python grade.py --self-test     # check the instrument, no data needed
    python grade.py                 # grade whatever history exists
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta

import numpy as np
import pandas as pd

import config as C
import flags as F
import store as S

log = logging.getLogger("grade")

MIN_SAMPLE = 40          # below this, say "not enough yet" rather than a number


def _boot(a, b, n=2000, seed=7):
    """Bootstrap interval for a difference of means. Same instrument used on
    the hockey work, for the same reason: a difference without an interval is
    an anecdote with a decimal point."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    d = np.array([rng.choice(a, len(a), True).mean()
                  - rng.choice(b, len(b), True).mean() for _ in range(n)])
    return float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def consensus_series(moves: pd.DataFrame) -> pd.DataFrame:
    """The median line across books, at every moment it changed.

    Median rather than mean on purpose: one book with a stale number drags a
    mean and barely moves a median, and a stale book is exactly what the
    outlier flag is about.
    """
    if moves.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "ts", "val"])
    m = moves.copy()
    m["val"] = F._value(m)
    m = m.dropna(subset=["val"])
    if m.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "ts", "val"])
    # Forward-fill each book's last value so the median at any instant is
    # taken over every book's CURRENT number, not only the ones that happened
    # to move at that instant. Without this the "consensus" at a moment when
    # one book moved is that one book.
    wide = (m.pivot_table(index=["event_id", "market", "side", "ts"],
                          columns="book", values="val", aggfunc="last")
            .groupby(level=[0, 1, 2]).ffill())
    cons = wide.median(axis=1).rename("val").reset_index()
    return cons.sort_values("ts")


def closing(cons: pd.DataFrame, events: list) -> pd.DataFrame:
    """The last consensus recorded BEFORE each game started."""
    starts = {}
    for e in events or []:
        try:
            t = pd.Timestamp(e.get("commence_time"))
            starts[e.get("id")] = t.tz_localize("UTC") if t.tzinfo is None else t
        except Exception:                                      # noqa: BLE001
            continue
    if cons.empty or not starts:
        return pd.DataFrame(columns=["event_id", "market", "side", "close",
                                     "start"])
    c = cons.copy()
    c["start"] = c["event_id"].map(starts)
    c = c.dropna(subset=["start"])
    c = c[c["ts"] <= c["start"]]
    if c.empty:
        return pd.DataFrame(columns=["event_id", "market", "side", "close",
                                     "start"])
    last = (c.sort_values("ts")
            .groupby(["event_id", "market", "side"], as_index=False).last())
    return last.rename(columns={"val": "close"})[
        ["event_id", "market", "side", "close", "start"]]


def _moves_with_direction(moves: pd.DataFrame) -> pd.DataFrame:
    """Every individual book move, with its direction and the moment it
    happened. The raw material for both steam and its control."""
    m = moves.copy()
    m["val"] = F._value(m)
    m = m.dropna(subset=["val"]).sort_values("ts")
    g = ["event_id", "book", "market", "side"]
    m["prev"] = m.groupby(g)["val"].shift(1)
    m["delta"] = m["val"] - m["prev"]
    if not len(m):
        return m
    # Dropped FIRST, then filtered on the surviving frame. Writing this as
    # `m.dropna(...)[m["delta"] != 0]` builds the mask from the original
    # index and applies it to a shorter one, which pandas reindexes with a
    # warning and fills with NaN - silently keeping and dropping the wrong
    # rows.
    m = m.dropna(subset=["delta"])
    return m[m["delta"] != 0]


def grade_steam(moves: pd.DataFrame, cons: pd.DataFrame,
                close: pd.DataFrame) -> dict:
    """Does a steam move keep going?

    THE CONTROL IS THE POINT. A line that just moved tends to keep moving -
    that is momentum, and it happens with or without several books agreeing.
    So the comparison is: of all the moments a line moved, do the ones where
    THREE OR MORE BOOKS moved together continue further by close than the ones
    where only one or two did?

    Both groups are measured the same way: how far the consensus travelled
    from that moment to the close, SIGNED by the direction of the move. A
    positive number means the line kept going; negative means it came back.
    """
    out = {"name": "steam", "n_flag": 0, "n_ctrl": 0}
    mv = _moves_with_direction(moves)
    if mv.empty or cons.empty or close.empty:
        out["note"] = "no finished games with history yet"
        return out

    # How many distinct books moved the same way inside the window, at each
    # moment a move happened.
    win = pd.Timedelta(minutes=C.STEAM_WINDOW_MIN)
    mv = mv.assign(dir=np.sign(mv["delta"]))
    rows = []
    for key, grp in mv.groupby(["event_id", "market", "side", "dir"]):
        grp = grp.sort_values("ts")
        ts = grp["ts"].to_numpy()
        books = grp["book"].to_numpy()
        for i in range(len(grp)):
            lo = ts[i] - win
            sel = (ts <= ts[i]) & (ts >= lo)
            rows.append({"event_id": key[0], "market": key[1], "side": key[2],
                         "dir": key[3], "ts": ts[i],
                         "n_books": len(set(books[sel]))})
    if not rows:
        out["note"] = "no moves to grade"
        return out
    ev = pd.DataFrame(rows)

    # The consensus at that moment, and at the close.
    cons_s = cons.sort_values("ts")
    ev = ev.sort_values("ts")
    ev = pd.merge_asof(ev, cons_s.rename(columns={"val": "at_flag"}),
                       on="ts", by=["event_id", "market", "side"],
                       direction="backward")
    ev = ev.merge(close, on=["event_id", "market", "side"], how="inner")
    # STRICTLY BEFORE the close, or the flag is being graded on a line it
    # already includes.
    ev = ev[(ev["ts"] < ev["start"]) & ev["at_flag"].notna()]
    if ev.empty:
        out["note"] = "no flags landed before a game started"
        return out

    ev["travel"] = (ev["close"] - ev["at_flag"]) * ev["dir"]
    flag = ev[ev["n_books"] >= C.STEAM_BOOKS]["travel"]
    ctrl = ev[ev["n_books"] < C.STEAM_BOOKS]["travel"]
    out["n_flag"], out["n_ctrl"] = len(flag), len(ctrl)
    if len(flag) < MIN_SAMPLE or len(ctrl) < MIN_SAMPLE:
        out["note"] = (f"not enough yet - {len(flag)} steam, {len(ctrl)} "
                       f"control, need {MIN_SAMPLE} of each")
        return out
    lo, hi = _boot(flag, ctrl)
    out.update(flagged=float(flag.mean()), control=float(ctrl.mean()),
               effect=float(flag.mean() - ctrl.mean()), lo=lo, hi=hi)
    return out


def grade_outliers(moves: pd.DataFrame, cons: pd.DataFrame,
                   close: pd.DataFrame) -> dict:
    """Is the odd book early, or just slow?

    The measure is how far the CONSENSUS travelled toward the outlier, as a
    fraction of the gap between them:

        1.0   the field came all the way to the odd book - it was early, and
              the number it was showing was the bet
        0.0   the field never moved - the odd book was simply wrong or stale
        <0    the field moved AWAY - worse than useless

    This is the only flag on the page that is directly actionable, so it is
    the one most worth knowing the truth about.
    """
    out = {"name": "outlier", "n": 0}
    if moves.empty or cons.empty or close.empty:
        out["note"] = "no finished games with history yet"
        return out
    m = moves.copy()
    m["val"] = F._value(m)
    m = m.dropna(subset=["val"]).sort_values("ts")
    if m.empty:
        out["note"] = "nothing to grade"
        return out

    m = pd.merge_asof(m, cons.sort_values("ts").rename(columns={"val": "cons"}),
                      on="ts", by=["event_id", "market", "side"],
                      direction="backward")
    m = m.merge(close, on=["event_id", "market", "side"], how="inner")
    m = m[(m["ts"] < m["start"]) & m["cons"].notna()]
    if m.empty:
        out["note"] = "no observations before a game started"
        return out

    m["gap"] = m["val"] - m["cons"]
    lim = m["market"].map(C.OUTLIER_MIN).fillna(np.inf)
    hit = m[m["gap"].abs() >= lim].copy()
    if len(hit) < MIN_SAMPLE:
        out["n"] = len(hit)
        out["note"] = f"not enough yet - {len(hit)} outliers, need {MIN_SAMPLE}"
        return out
    # How much of the gap the field closed, in the outlier's direction.
    hit["pull"] = (hit["close"] - hit["cons"]) / hit["gap"]
    hit = hit[np.isfinite(hit["pull"])]
    pull = hit["pull"].to_numpy()
    out.update(n=len(pull), mean_pull=float(np.mean(pull)),
               median_pull=float(np.median(pull)),
               share_right=float(np.mean(pull > 0.5)))
    # Interval on the mean, by resampling against a zero-pull null.
    lo, hi = _boot(pull, np.zeros(len(pull)))
    out["lo"], out["hi"] = lo, hi
    return out


def grade_keys(moves: pd.DataFrame, cons: pd.DataFrame, close: pd.DataFrame,
               sport: str) -> dict:
    """Does crossing 3 or 7 stick, or drift back?

    Control: moves of the same SIZE that did not cross a key number. Without
    that, this measures "big moves stick", which is a different claim.
    """
    out = {"name": "key numbers", "n_flag": 0, "n_ctrl": 0}
    keys = C.KEY_NUMBERS.get(sport)
    if not keys:
        out["note"] = f"{sport} has no key numbers configured"
        return out
    if cons.empty or close.empty:
        out["note"] = "no finished games with history yet"
        return out

    c = cons.sort_values("ts").copy()
    c = c[c["market"].isin(["spreads", "totals"])]
    if c.empty:
        out["note"] = "no spread or total history"
        return out
    g = ["event_id", "market", "side"]
    c["prev"] = c.groupby(g)["val"].shift(1)
    c = c.dropna(subset=["prev"])
    c = c.merge(close, on=g, how="inner")
    c = c[c["ts"] < c["start"]]
    if c.empty:
        out["note"] = "no consensus changes before a game started"
        return out

    def crossed(a, b):
        lo, hi = (a, b) if a < b else (b, a)
        return any((lo < k < hi) or (lo < -k < hi) for k in keys)

    c["crossed"] = [crossed(a, b) for a, b in zip(c["prev"], c["val"])]
    c["dir"] = np.sign(c["val"] - c["prev"])
    c["size"] = (c["val"] - c["prev"]).abs()
    c["travel"] = (c["close"] - c["val"]) * c["dir"]

    flag = c[c["crossed"]]
    # Same-size moves that did NOT cross, so size is not doing the work.
    sizes = set(np.round(flag["size"], 2))
    ctrl = c[~c["crossed"] & np.isin(np.round(c["size"], 2), list(sizes))]
    out["n_flag"], out["n_ctrl"] = len(flag), len(ctrl)
    if len(flag) < MIN_SAMPLE or len(ctrl) < MIN_SAMPLE:
        out["note"] = (f"not enough yet - {len(flag)} crossings, "
                       f"{len(ctrl)} same-size controls, need {MIN_SAMPLE}")
        return out
    lo, hi = _boot(flag["travel"], ctrl["travel"])
    out.update(flagged=float(flag["travel"].mean()),
               control=float(ctrl["travel"].mean()),
               effect=float(flag["travel"].mean() - ctrl["travel"].mean()),
               lo=lo, hi=hi)
    return out


def report(sport: str, results: list) -> None:
    print(f"\n{sport}")
    for r in results:
        print(f"\n  {r['name'].upper()}")
        if r.get("note"):
            print(f"    {r['note']}")
            continue
        if r["name"] == "outlier":
            print(f"    {r['n']:,} outlier observations")
            print(f"    the field closed {100 * r['mean_pull']:+.1f}% of the "
                  f"gap toward the odd book  [{100*r['lo']:+.1f}%, "
                  f"{100*r['hi']:+.1f}%]")
            print(f"    median {100 * r['median_pull']:+.1f}%; the odd book "
                  f"was the one the field came to {100*r['share_right']:.0f}% "
                  f"of the time")
            if r["lo"] > 0.25:
                print("    -> EARLY, not stale. This flag is a bet: the field "
                      "moves toward it.")
            elif r["hi"] < 0.1:
                print("    -> STALE. The field does not come to it. Delete "
                      "the flag; it points at slow books, not value.")
            else:
                print("    -> inconclusive on this much data.")
            continue
        print(f"    flagged {r['n_flag']:,}   control {r['n_ctrl']:,}")
        print(f"    kept going {r['flagged']:+.3f} vs control "
              f"{r['control']:+.3f}  ->  {r['effect']:+.3f} "
              f"[{r['lo']:+.3f}, {r['hi']:+.3f}]")
        if r["lo"] > 0:
            print("    -> REAL. It continues further than an ordinary move.")
        elif r["hi"] < 0:
            print("    -> BACKWARDS. It reverts more than an ordinary move - "
                  "which is still information, pointing the other way.")
        else:
            print("    -> NOTHING. The interval crosses zero. This flag does "
                  "not beat an ordinary move and should be deleted.")


def run(days: int = 30) -> int:
    any_data = False
    for sport in C.SPORTS:
        moves = S.read_moves(sport, days=days)
        if moves.empty:
            continue
        any_data = True
        events = S.load_events(sport)
        cons = consensus_series(moves)
        close = closing(cons, events)
        report(sport, [
            grade_steam(moves, cons, close),
            grade_outliers(moves, cons, close),
            grade_keys(moves, cons, close, sport),
        ])
    if not any_data:
        print("No line history yet. Let the collector run for a few days - "
              "grading needs games that have FINISHED, with their lines "
              "recorded before they started.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--self-test", action="store_true",
                    help="check the grader against data whose answer is "
                         "known, with no history and no network")
    a = ap.parse_args()
    if a.self_test:
        import grade_selftest
        return grade_selftest.main()
    return run(days=a.days)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
