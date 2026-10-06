"""One poll. Refresh every schedule, then spend the budget on lines.

ORDER MATTERS, and it is: schedule first, odds second.

`/events` is free, so every sport's schedule is refreshed on every run no
matter what tier you are on. That means the site always shows every game in
every sport - what the budget buys is whether a game's LINES are watched, not
whether it appears. A free-tier site is a full schedule with line history on
the handful of games that mattered most; a paid one is the same site with
history on all of them.

PRIORITY, when the budget runs out before the sports do:

    1. a sport with games starting sooner beats one starting later
    2. a sport with no games in the next two days is skipped entirely,
       because watching a line nobody is betting is the cheapest thing to cut
    3. ties go to config's `priority`

    python collect.py            # one poll
    python collect.py --dry-run  # what it WOULD spend, no calls, no cost
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

import config as C
import odds as O
import store as S

SPEND_FILE = "spend.json"

log = logging.getLogger("collect")

LOOKAHEAD_HOURS = 48


def _spend_today() -> int:
    """How many credits today's earlier polls already used.

    Kept in a file because each workflow run is a fresh container that
    remembers nothing. Without it every poll would believe it was the first
    of the day, and the pacing would release the same share forty-eight times
    over - a budget that resets on every run is not a budget.
    """
    p = S.EVENTS.parent / SPEND_FILE
    today = datetime.now(timezone.utc).date().isoformat()
    if not p.exists():
        return 0
    try:
        d = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    # A new UTC day starts the count again, which is also what the API's own
    # quota does NOT do - it resets monthly - so the account balance stays the
    # backstop for the month and this only paces the day.
    return int(d.get("spent", 0)) if d.get("date") == today else 0


def _record_spend(used: int) -> None:
    p = S.EVENTS.parent / SPEND_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    p.write_text(json.dumps({"date": today, "spent": _spend_today() + used},
                            separators=(",", ":")))


def _hours_to_first(events: list) -> float:
    """Hours until this sport's next game. `inf` if it has none upcoming."""
    now = datetime.now(timezone.utc)
    best = float("inf")
    for e in events or []:
        try:
            t = datetime.fromisoformat(
                str(e.get("commence_time", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        delta = (t - now).total_seconds() / 3600.0
        # A game in progress is still worth watching; one finished is not.
        if delta > -4:
            best = min(best, delta)
    return best


def save_catalogue(key: str) -> None:
    """The API's own sport list, with its own titles. FREE.

    Without this the page has to prettify a sport KEY, which produces
    "Americanfootball Nfl" and "Tennis Atp Aus Open Singles". The endpoint
    that knows the real names costs nothing, so there is no reason to guess
    at them.
    """
    try:
        cat = O.list_sports(key)
    except O.OddsError as exc:
        log.warning("could not refresh the sport catalogue: %s", exc)
        return
    S.EVENTS.mkdir(parents=True, exist_ok=True)
    (S.EVENTS.parent / "sports.json").write_text(
        json.dumps({s["key"]: {"title": s.get("title") or s["key"],
                               "group": s.get("group") or ""}
                    for s in cat if s.get("key")},
                   separators=(",", ":"), sort_keys=True))


def plan(key: str, dry: bool = False) -> list:
    """Which sports to poll, in order, with what it will cost.

    The schedule refresh happens here and is free, so even a dry run leaves
    the site's game list current.
    """
    rows = []
    for sport, cfg in C.SPORTS.items():
        try:
            ev = O.events(sport, key)
        except O.OddsError as exc:
            # A sport key that has gone stale - leagues get renamed between
            # seasons - must not take the whole run down with it.
            log.warning("skipping %s: %s", sport, exc)
            continue
        if not dry:
            S.save_events(sport, ev)
        soon = _hours_to_first(ev)
        rows.append({
            "sport": sport,
            "events": len(ev),
            "hours_to_first": soon,
            "cost": len(cfg["markets"]) * len(C.REGIONS.split(",")),
            "priority": cfg.get("priority", 0),
            "markets": cfg["markets"],
        })

    live = [r for r in rows if r["hours_to_first"] <= LOOKAHEAD_HOURS]
    # Sooner first; config priority settles the ties.
    live.sort(key=lambda r: (r["hours_to_first"], -r["priority"]))
    return live


def focus(order: list, budget: O.Budget) -> list:
    """When the allowance cannot cover everything, watch fewer things often.

    This is the lesson of the first dry run. Six sports polled once a day is
    six snapshots and NO line movement; one sport polled fifteen times is a
    chart. Movement needs the same game seen repeatedly, so when the budget
    is tight the collector sticks to the top `FOCUS` sports by config
    priority rather than following whichever game happens to start soonest -
    a different sport each poll builds no history at all.

    Once the budget can afford every sport on every poll this does nothing,
    which is the correct behaviour for a paid tier.
    """
    if not C.FOCUS or not order:
        return order
    full = sum(r["cost"] for r in order)
    if budget.allows(full):
        return order
    by_priority = sorted(order, key=lambda r: (-r["priority"],
                                               r["hours_to_first"]))
    keep = {r["sport"] for r in by_priority[:C.FOCUS]}
    return [r for r in order if r["sport"] in keep]


def run(dry: bool = False) -> int:
    key = O.api_key()
    budget = O.Budget(C.MONTHLY_CREDITS, C.BUDGET_USE,
                      spent_today=_spend_today())
    log.info("budget: %s", budget)

    if not dry:
        save_catalogue(key)
    order = plan(key, dry=dry)
    all_n = len(order)
    order = focus(order, budget)
    print(f"{all_n} sport(s) with a game inside {LOOKAHEAD_HOURS}h; "
          f"{budget.planned_today} credit(s) released for this poll "
          f"(day's allowance {budget.daily}, {budget.spent_today} already "
          f"used today)")
    if len(order) < all_n:
        print(f"  budget is tight, so this poll watches the top {len(order)} "
              f"by priority rather than spreading thin - a sport polled once "
              f"a day shows no movement at all")
    print(f"  {'sport':<32s} {'games':>6s} {'next':>8s} {'cost':>5s}  status")

    total_moves = 0
    for r in order:
        nxt = ("live" if r["hours_to_first"] <= 0
               else f"{r['hours_to_first']:.1f}h")
        if dry:
            status = "would poll" if budget.allows(r["cost"]) else "over budget"
            if budget.allows(r["cost"]):
                budget.charge(r["cost"])
        elif not budget.allows(r["cost"]):
            status = "SKIPPED - budget"
        else:
            try:
                payload, cost = O.odds(r["sport"], r["markets"], C.REGIONS,
                                       C.ODDS_FORMAT, key, budget)
            except O.OddsError as exc:
                status = f"ERROR {exc}"
                payload = None
            if payload is None:
                status = status if "ERROR" in locals().get("status", "") \
                    else "SKIPPED - budget"
            else:
                summary = S.record(r["sport"], payload)
                total_moves += summary["moves"]
                status = (f"{summary['moves']} move(s) recorded"
                          if summary["moves"] else "no change")
        print(f"  {r['sport']:<32s} {r['events']:>6d} {nxt:>8s} "
              f"{r['cost']:>5d}  {status}")

    print(f"\n{budget}")
    if not dry:
        _record_spend(budget.spent)
        gone = S.prune(C.KEEP_DAYS)
        if gone:
            print(f"pruned {gone} day-file(s) past {C.KEEP_DAYS} days")
        print(f"{total_moves} line change(s) recorded this poll")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="show what it would poll and what that costs, "
                         "without spending a credit on odds")
    a = ap.parse_args()
    return run(dry=a.dry_run)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
