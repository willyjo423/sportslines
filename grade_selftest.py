"""Point the grader at markets whose answer is known, before any real data.

Three worlds, built so the grader has to get all three right:

  NULL        books wander independently. Steam that appears is coincidence,
              and the grader must find nothing.
  REAL STEAM  when books agree, the line genuinely keeps going. The grader
              must find it.
  EARLY BOOK  the odd book out is right, and the field comes to it. The
              grader must say "bet" rather than "stale".

A grader that only ever finds effects is a rubber stamp; one that only ever
finds nothing is a broken instrument. Both directions, every time.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

BAD = 0
BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet", "espnbet"]


def check(label, cond, got=""):
    global BAD
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f": {got}" if got else ""))
    if not cond:
        BAD += 1


def build(S, sport, *, n_games=60, steam_pull=0.0, outlier_pull=0.0,
          seed=1, polls=26):
    """A market, poll by poll.

    `steam_pull`   how far the line keeps going after books agree. 0 is the
                   null - agreement means nothing.
    `outlier_pull` how much the field moves TOWARD a book that is out of
                   line. 0 means the odd book is simply stale.
    """
    rng = np.random.default_rng(seed)
    start = datetime.now(timezone.utc) - timedelta(hours=20)
    events = []
    for g in range(n_games):
        events.append({"id": f"g{g}", "home_team": f"H{g}", "away_team": f"A{g}",
                       "commence_time": (start + timedelta(hours=14)
                                         ).isoformat()})
    S.save_events(sport, events)

    true = {e["id"]: rng.choice([-6.5, -3.5, -2.5, 1.5, 3.5]) for e in events}
    # One book per game is deliberately offset, to be the "odd book".
    odd_book = {e["id"]: BOOKS[rng.integers(len(BOOKS))] for e in events}
    odd_gap = {e["id"]: rng.choice([-1.5, 1.5]) for e in events}
    pending = {}

    for p in range(polls):
        t = start + timedelta(minutes=30 * p)
        payload = []
        for e in events:
            eid = e["id"]
            # Occasionally the books agree and move together.
            #
            # THE DRIFT IS APPLIED AFTERWARDS, NOT AT THE SAME INSTANT. The
            # first version of this added the whole steam effect in the poll
            # where the books agreed - which put it inside the consensus the
            # flag is measured from, leaving nothing to continue into. The
            # grader correctly reported no continuation and the check called
            # that a failure. Steam means the line KEEPS going, so the extra
            # movement has to land in later polls.
            agreed = rng.random() < 0.25
            step = rng.choice([-0.5, 0.5]) if agreed else 0.0
            if agreed:
                true[eid] += step
                pending[eid] = pending.get(eid, 0.0) + steam_pull * step
            else:
                true[eid] += rng.choice([-0.5, 0, 0, 0.5])
            # Bleed any pending drift out over the following polls.
            if pending.get(eid):
                bite = pending[eid] / 3.0
                true[eid] += bite
                pending[eid] -= bite
                if abs(pending[eid]) < 0.05:
                    pending[eid] = 0.0
            # The field drifts toward the odd book, if this world says so.
            true[eid] += outlier_pull * odd_gap[eid] / polls

            bms = []
            for b in BOOKS:
                v = true[eid]
                if b == odd_book[eid]:
                    v += odd_gap[eid]
                elif not agreed and rng.random() < 0.5:
                    v += rng.choice([-0.5, 0.0, 0.5])
                bms.append({"key": b, "markets": [
                    {"key": "spreads", "outcomes": [
                        {"name": e["away_team"], "price": -110,
                         "point": round(v * 2) / 2},
                        {"name": e["home_team"], "price": -110,
                         "point": -round(v * 2) / 2}]}]})
            payload.append({**e, "bookmakers": bms})
        S.record(sport, payload, t)


def main() -> int:
    import sys
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import store as S
    import grade as G

    import config as C
    # THE STEAM WINDOW MUST MATCH THE POLLING GAP, and this fixture polls
    # every thirty minutes. Production is currently on three passes a day
    # with a 420-minute window, which is right there and catastrophic here:
    # 420 minutes spans fourteen of this fixture's polls, so nearly every
    # observation counts as "books agreed" and the control group vanishes.
    # The check failed on exactly that. Pinned to this fixture's own cadence,
    # because what is under test is the grader's logic, not the tuning.
    _win = C.STEAM_WINDOW_MIN
    C.STEAM_WINDOW_MIN = 60

    print("the grader, against markets whose answer is known\n")
    SPORT = "americanfootball_nfl"

    def world(**kw):
        tmp = Path(tempfile.mkdtemp())
        S.MOVES, S.STATE, S.EVENTS = tmp / "m", tmp / "s", tmp / "e"
        build(S, SPORT, **kw)
        moves = S.read_moves(SPORT, days=3)
        cons = G.consensus_series(moves)
        close = G.closing(cons, S.load_events(SPORT))
        res = (G.grade_steam(moves, cons, close),
               G.grade_outliers(moves, cons, close))
        shutil.rmtree(tmp, ignore_errors=True)
        return res

    # --- the plumbing has to work at all -----------------------------
    st, ol = world(seed=3)
    check("the grader finds games, flags and closing lines",
          st.get("n_flag", 0) > 0 and ol.get("n", 0) > 0,
          f"{st.get('n_flag',0)} steam, {ol.get('n',0)} outliers")

    # --- NULL: agreement means nothing -------------------------------
    st, ol = world(steam_pull=0.0, seed=11)
    if st.get("note"):
        check("null world produced enough to grade", False, st["note"])
    else:
        check("where steam means NOTHING, the interval crosses zero",
              st["lo"] <= 0 <= st["hi"],
              f"{st['effect']:+.3f} [{st['lo']:+.3f}, {st['hi']:+.3f}]")

    # --- REAL: agreement predicts continuation -----------------------
    st, ol = world(steam_pull=2.5, seed=12)
    if st.get("note"):
        check("steam world produced enough to grade", False, st["note"])
    else:
        check("where steam is REAL, the grader finds it", st["lo"] > 0,
              f"{st['effect']:+.3f} [{st['lo']:+.3f}, {st['hi']:+.3f}]")

    # --- the odd book is STALE: field never comes to it ---------------
    st, ol = world(outlier_pull=0.0, seed=13)
    if ol.get("note"):
        check("stale-book world produced enough to grade", False, ol["note"])
    else:
        check("a STALE odd book shows little pull toward it",
              ol["mean_pull"] < 0.3,
              f"field closed {100*ol['mean_pull']:+.1f}% of the gap")

    # --- the odd book is EARLY: field comes to it --------------------
    st, ol = world(outlier_pull=1.0, seed=14)
    if ol.get("note"):
        check("early-book world produced enough to grade", False, ol["note"])
    else:
        check("an EARLY odd book shows the field moving toward it",
              ol["mean_pull"] > 0.3,
              f"field closed {100*ol['mean_pull']:+.1f}% of the gap")

    # --- no look-ahead ------------------------------------------------
    # Every graded flag must sit strictly before the game started. If one
    # does not, the grade includes the answer it is predicting.
    tmp = Path(tempfile.mkdtemp())
    S.MOVES, S.STATE, S.EVENTS = tmp / "m2", tmp / "s2", tmp / "e2"
    build(S, SPORT, seed=5)
    moves = S.read_moves(SPORT, days=3)
    cons = G.consensus_series(moves)
    close = G.closing(cons, S.load_events(SPORT))
    starts = {e["id"]: e["commence_time"] for e in S.load_events(SPORT)}
    import pandas as pd
    late = 0
    for _, r in close.iterrows():
        st_ts = pd.Timestamp(starts[r["event_id"]])
        if st_ts.tzinfo is None:
            st_ts = st_ts.tz_localize("UTC")
        if r["start"] > st_ts:
            late += 1
    check("no closing line is taken from after the game started", late == 0,
          f"{late} late")
    shutil.rmtree(tmp, ignore_errors=True)

    # --- it refuses to answer on thin data ----------------------------
    tmp = Path(tempfile.mkdtemp())
    S.MOVES, S.STATE, S.EVENTS = tmp / "m3", tmp / "s3", tmp / "e3"
    build(S, SPORT, n_games=2, polls=3, seed=9)
    moves = S.read_moves(SPORT, days=3)
    cons = G.consensus_series(moves)
    close = G.closing(cons, S.load_events(SPORT))
    thin = G.grade_steam(moves, cons, close)
    # The single most important behaviour here: on two games it must say so
    # rather than print a confident number off a handful of rows.
    check("on thin data it says 'not enough' instead of guessing",
          bool(thin.get("note")), thin.get("note", "produced a number"))
    shutil.rmtree(tmp, ignore_errors=True)

    C.STEAM_WINDOW_MIN = _win
    print(f"\n{'all checks passed' if not BAD else f'{BAD} CHECK(S) FAILED'}")
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
