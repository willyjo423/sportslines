"""Check the collector against data whose answer is known, before it is
pointed at data whose answer is not.

Every test here runs on a fixture and makes NO network call, so the workflow
can run them on every poll for free. They exist because the failures that
matter in this project are silent ones: a parser that quietly records nothing,
a change detector that records everything, a flag that fires on every game.
None of those raise. All of them look like a working site until you read it.

    python tests.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

BAD = 0
BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "pointsbet"]
# ANCHORED TO NOW, not to a fixed date. `store.read_moves` looks for the last
# N days of files BY FILENAME, which is right in production and silently finds
# nothing if a fixture writes into a day months ago. The first draft of this
# used a fixed January date and every read-back check failed - the fixture was
# wrong, not the store. A fixture that differs from production in the
# dimension under test is how bugs hide.
T0 = (datetime.now(timezone.utc) - timedelta(hours=6)).replace(
    minute=0, second=0, microsecond=0)


def check(label: str, cond: bool, got="") -> None:
    global BAD
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + (f": {got}" if got else ""))
    if not cond:
        BAD += 1


def payload(spreads: dict, total=44.5, price=-110, eid="g1"):
    """The shape the API documents, with one point per book."""
    return [{
        "id": eid, "sport_key": "americanfootball_nfl",
        "commence_time": (T0 + timedelta(hours=10)).isoformat(),
        "home_team": "Home", "away_team": "Away",
        "bookmakers": [{
            "key": b, "title": b.title(), "last_update": T0.isoformat(),
            "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Away", "price": price, "point": p},
                    {"name": "Home", "price": -110, "point": -p}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": -110, "point": total},
                    {"name": "Under", "price": -110, "point": total}]},
            ]} for b, p in spreads.items()],
    }]


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import store as S
    import flags as F

    # Redirect every write into a scratch directory. A test suite that writes
    # into data/ would commit its own fixtures into the real history, which is
    # a far worse failure than any bug it could catch.
    S.MOVES, S.STATE, S.EVENTS = tmp / "moves", tmp / "state", tmp / "events"
    SP = "americanfootball_nfl"

    print("the collector, against fixtures\n")

    # --- the change log -----------------------------------------------
    r1 = S.record(SP, payload({b: 2.5 for b in BOOKS}), T0)
    check("a first sighting records every line", r1["moves"] == len(BOOKS) * 4,
          f"{r1['moves']} rows for {len(BOOKS)} books x 4 lines")

    r2 = S.record(SP, payload({b: 2.5 for b in BOOKS}),
                  T0 + timedelta(minutes=30))
    # THE WHOLE POINT. If this ever records anything, a year of history stops
    # being 36MB and becomes 3GB, and nothing downstream notices until the
    # clone takes ten minutes.
    check("an unchanged poll records NOTHING", r2["moves"] == 0,
          f"{r2['moves']} rows")

    r3 = S.record(SP, payload({b: 3.5 for b in BOOKS}),
                  T0 + timedelta(minutes=60))
    check("a moved spread records both sides", r3["moves"] == len(BOOKS) * 2,
          f"{r3['moves']} rows")

    r4 = S.record(SP, payload({b: 3.5 for b in BOOKS}, price=-120),
                  T0 + timedelta(minutes=90))
    # A price move at an unchanged number is a real move and the commonest
    # kind. A detector that only watched `point` would miss every one.
    check("a PRICE move at the same number is recorded",
          r4["moves"] == len(BOOKS), f"{r4['moves']} rows")

    # String vs float is how a "move every poll" bug gets in: books quote
    # 6.5 and "6.5" and JSON preserves the difference.
    S.record(SP, payload({b: 3.5 for b in BOOKS}, price=-120),
             T0 + timedelta(minutes=120))
    state = S.load_state(SP)
    k = [x for x in state if "spreads" in x][0]
    state[k]["point"] = str(state[k]["point"])
    S.save_state(SP, state)
    r5 = S.record(SP, payload({b: 3.5 for b in BOOKS}, price=-120),
                  T0 + timedelta(minutes=150))
    check("'3.5' and 3.5 are the same number, not a move", r5["moves"] == 0,
          f"{r5['moves']} rows")

    moves = S.read_moves(SP, days=2)
    check("every appended gzip member reads back",
          len(moves) == r1["moves"] + r3["moves"] + r4["moves"],
          f"{len(moves)} rows across {moves['ts'].nunique()} timestamps")

    # --- the flags ----------------------------------------------------
    shutil.rmtree(tmp, ignore_errors=True)
    S.MOVES, S.STATE, S.EVENTS = tmp / "moves", tmp / "state", tmp / "events"

    S.record(SP, payload({b: 2.5 for b in BOOKS}), T0)
    # Four books up to 3.5, one left behind at 2.5.
    S.record(SP, payload({**{b: 3.5 for b in BOOKS[:4]}, BOOKS[4]: 2.5}),
             T0 + timedelta(minutes=30))
    mv = S.read_moves(SP, days=2)

    st = F.steam(mv, window_min=10_000)
    up = st[(st["market"] == "spreads") & (st["direction"] == "up")]
    check("steam sees four books moving the same way",
          not up.empty and int(up["books"].iloc[0]) == 4,
          f"{0 if up.empty else int(up['books'].iloc[0])} books")

    # Direction is what makes it steam. Books scattering is not.
    S.MOVES, S.STATE = tmp / "m2", tmp / "s2"
    S.record(SP, payload({b: 2.5 for b in BOOKS}), T0)
    S.record(SP, payload({BOOKS[0]: 3.0, BOOKS[1]: 2.0, BOOKS[2]: 3.0,
                          BOOKS[3]: 2.0, BOOKS[4]: 2.5}),
             T0 + timedelta(minutes=30))
    sc = F.steam(S.read_moves(SP, days=2), window_min=10_000, min_books=3)
    check("books scattering in both directions is NOT steam",
          sc[sc["market"] == "spreads"].empty,
          f"{len(sc[sc['market'] == 'spreads'])} rows")

    S.MOVES, S.STATE = tmp / "moves", tmp / "state"
    kn = F.key_crossings(mv, SP)
    check("crossing 3 is flagged in football",
          not kn.empty and 3 in sum(kn["crossed"].tolist(), []),
          f"{len(kn)} crossing(s)")
    check("the same move is NOT flagged where there are no key numbers",
          F.key_crossings(mv, "tennis_atp_aus_open_singles").empty)

    ol = F.outliers(mv)
    lag = ol[ol["book"] == BOOKS[4]]
    check("the book left behind is the outlier", not lag.empty,
          f"{BOOKS[4]} off by "
          f"{0 if lag.empty else lag['diff'].iloc[0]:+g}")
    check("the four books that agree are not outliers",
          set(ol["book"]) == {BOOKS[4]}, f"{sorted(set(ol['book']))}")

    # An outlier needs three books to exist. With two, each is as far from the
    # median as the other and neither is the odd one out.
    S.MOVES, S.STATE = tmp / "m3", tmp / "s3"
    S.record(SP, payload({BOOKS[0]: 2.5, BOOKS[1]: 6.5}), T0)
    check("two books cannot produce an outlier between them",
          F.outliers(S.read_moves(SP, days=2)).empty)
    S.MOVES, S.STATE = tmp / "moves", tmp / "state"

    # --- publishing ---------------------------------------------------
    S.save_events(SP, [{"id": "g1", "home_team": "Home", "away_team": "Away",
                        "commence_time": (datetime.now(timezone.utc)
                                          + timedelta(hours=4)).isoformat()}])
    import publish as P
    out = Path(tempfile.mkdtemp()) / "data"
    P.OUT = out
    idx = P.write(days=2)
    check("a sport file is written", len(idx["sports"]) >= 1,
          f"{len(idx['sports'])} file(s)")

    text = (out / f"{SP}.json").read_text()
    # NaN is not JSON. Python writes it; every browser refuses the file, and
    # the page simply never loads with nothing in the console to say why.
    check("the written JSON contains no NaN or Infinity",
          "NaN" not in text and "Infinity" not in text)
    doc = json.loads(text)
    g = doc["games"][0]
    check("the game carries its line history",
          bool(g["lines"]) and bool(g["lines"]["spreads"]),
          f"{list(g['lines'])}")
    check("first seen and now are both present and differ",
          g["lines"]["spreads"]["Away"]["first"] == 2.5
          and g["lines"]["spreads"]["Away"]["now"] == 3.5,
          f"{g['lines']['spreads']['Away']['first']} -> "
          f"{g['lines']['spreads']['Away']['now']}")
    check("flags reached the game", len(g["flags"]) > 0,
          f"{len(g['flags'])} flag(s)")

    # --- the budget ---------------------------------------------------
    import odds as O
    import config as C
    # ANCHORED TO THE BUDGET DAY, not to midnight UTC. These checks used a
    # fixed 12:00 UTC as "midday" and started failing the moment the budget
    # day was moved to 4am Eastern - the tests were measuring the old design,
    # not a regression in the new one. A check pinned to a constant the code
    # no longer uses is worse than no check.
    DAY0 = datetime(2026, 10, 6, C.BUDGET_DAY_START_UTC, 0,
                    tzinfo=timezone.utc)
    noon = DAY0 + timedelta(hours=12)
    # PINNED, like FOCUS. These check how a MONTHLY figure is divided; with
    # config.DAILY_CREDITS set for a fixed-pot run, the monthly path is not
    # the one in use and the checks would be measuring a setting rather than
    # the code.
    #
    # ALL THREE TUNING KNOBS ARE PINNED, not just one. This has bitten three
    # times: a check written against the defaults starts failing the moment
    # the config is tuned for a real plan, and every time the TEST was wrong
    # rather than the code. A test of pacing must set up the pacing it is
    # testing; reading the live setting means it measures the setting.
    _was = (C.DAILY_CREDITS, C.POLLS_PER_DAY, C.FOCUS)
    C.DAILY_CREDITS, C.POLLS_PER_DAY, C.FOCUS = None, 48, 1
    import importlib
    importlib.reload(O)
    b = O.Budget(500, 0.9, now=noon)
    check("the free tier's day is about 15 credits",
          13 <= b.daily <= 16, f"{b.daily} a day")

    # THE BUG A DRY RUN FOUND. The first version released the whole day's
    # allowance to the first poll, which spent it and left forty-seven runs
    # with nothing. Half way through the day only about half should be out.
    check("by midday only about half the day's credits are released",
          0.4 * b.daily <= b.planned_today <= 0.65 * b.daily,
          f"{b.planned_today} of {b.daily} released at the day's midpoint")
    early = O.Budget(500, 0.9, now=DAY0 + timedelta(hours=1))
    check("and an hour into the budget day only a poll's worth is",
          early.planned_today <= max(1, b.daily // 4),
          f"{early.planned_today} released one hour in")

    # Paced across a whole day it must spend the allowance and not a credit
    # more - the property that actually protects the account.
    spent = 0
    buying = 0
    for i in range(C.POLLS_PER_DAY):
        t = DAY0 + timedelta(minutes=30 * i + 7)
        bb = O.Budget(500, 0.9, spent_today=spent, now=t)
        got = 0
        while bb.allows(1):
            bb.charge(1)
            got += 1
        spent += got
        buying += 1 if got else 0
    check("a full day of polls spends the allowance and no more",
          spent <= b.daily, f"{spent} spent against an allowance of {b.daily}")
    # The point is that it is SPREAD, not that it is frequent - how many polls
    # buy anything depends on what a poll costs, and on the free tier that is
    # a handful either way.
    check("and spreads it over the day instead of emptying at the reset",
          buying >= 4 and buying < C.POLLS_PER_DAY,
          f"{buying} of {C.POLLS_PER_DAY} polls bought data")

    # --- IMMINENCE BEATS PREFERENCE ----------------------------------
    # The real board on a Wednesday in October: MLB playoff games in three
    # hours, NFL not until Thursday night. Sorting by config priority alone
    # spent the whole day's credits on a line that would not move for a day
    # and a half.
    import collect as CO
    # FOCUS IS PINNED HERE, not read from config. These check what focus()
    # does when it narrows; reading the live setting meant that turning
    # narrowing off - the correct thing to do on a paid tier - broke two
    # tests that have nothing to do with the tier. A test of a behaviour must
    # set up that behaviour rather than hope the config still enables it.
    C.FOCUS = 1
    board = [
        {"sport": "baseball_mlb", "hours_to_first": 3.4, "priority": 75,
         "cost": 3, "events": 4},
        {"sport": "americanfootball_ncaaf", "hours_to_first": 6.4,
         "priority": 90, "cost": 3, "events": 66},
        {"sport": "americanfootball_nfl", "hours_to_first": 31.7,
         "priority": 100, "cost": 3, "events": 29},
    ]
    tight = O.Budget(500, 0.9, now=noon)
    picked = CO.focus(board, tight)
    check("a tight budget watches tonight's games, not Thursday's",
          len(picked) == 1 and picked[0]["sport"] == "baseball_mlb",
          f"{[r['sport'] for r in picked]}")

    # Within one band, preference is what decides - NCAAF over NHL at the
    # same hour, because that is what `priority` is for.
    same_band = [
        {"sport": "icehockey_nhl", "hours_to_first": 6.9, "priority": 80,
         "cost": 3, "events": 14},
        {"sport": "americanfootball_ncaaf", "hours_to_first": 6.4,
         "priority": 90, "cost": 3, "events": 66},
    ]
    # A GENUINELY tight budget. The first version of this check used the
    # midday one, which could afford both sports at six credits - so focus()
    # correctly returned both and the check called that a failure. The budget
    # has to actually be unable to cover the board or there is nothing to
    # focus.
    very_tight = O.Budget(500, 0.9, now=DAY0 + timedelta(hours=2))
    picked = CO.focus(same_band, very_tight)
    check("inside one band, priority still decides",
          len(picked) == 1 and picked[0]["sport"] == "americanfootball_ncaaf",
          f"{[r['sport'] for r in picked]}")

    # Everything far away must still produce a pick, not an empty poll.
    far = [{"sport": "boxing_boxing", "hours_to_first": 70.0, "priority": 25,
            "cost": 1, "events": 33}]
    check("nothing inside any band still picks something",
          len(CO.focus(far, very_tight)) == 1)

    # And a budget that can afford the lot must not narrow anything.
    rich = O.Budget(100_000, 0.9, now=noon)
    check("a budget that can afford everything focuses on nothing",
          len(CO.focus(board, rich)) == len(board))

    # And FOCUS=0 - the paid-tier setting - must never narrow, whatever the
    # budget says.
    C.FOCUS = 0
    check("FOCUS=0 never narrows, even on a tight budget",
          len(CO.focus(board, tight)) == len(board))
    C.DAILY_CREDITS, C.POLLS_PER_DAY, C.FOCUS = _was
    importlib.reload(O)

    # And the override itself: when set, it wins outright.
    C.DAILY_CREDITS = 70
    check("an explicit daily figure overrides the monthly one",
          O.Budget(500, 0.9, now=noon).daily == 70,
          f"{O.Budget(500, 0.9, now=noon).daily} a day")
    C.DAILY_CREDITS = _was[0]

    b2 = O.Budget(100_000, 0.9, now=noon)
    b2.charge(1, remaining=4)
    # The account balance is the truth and must win over the plan, or the last
    # days of a month quietly overspend.
    check("a low account balance beats a generous plan",
          not b2.allows(10) and b2.allows(3), f"{b2}")

    # --- the key is never printed -------------------------------------
    red = O._redact("https://api.the-odds-api.com/v4/x?apiKey=SECRET123&z=1",
                    "SECRET123")
    check("the key never survives into a log line",
          "SECRET123" not in red, red)
    check("and nor does one that arrived by another route",
          "abc999" not in O._redact("...apiKey=abc999&m=h2h", None))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'all checks passed' if not BAD else f'{BAD} CHECK(S) FAILED'}")
    return 1 if BAD else 0


if __name__ == "__main__":
    raise SystemExit(main())
