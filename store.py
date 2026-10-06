"""The change log. The one architectural decision that cannot be changed later.

A poll every thirty minutes is forty-eight snapshots a day. Written whole,
across fifteen sports and eight books, that is roughly three gigabytes a year -
a repository that clones slowly, then badly, then not at all.

But a line does not move forty-eight times a day. It moves perhaps six. So
only the MOVES are written: a row appears when a (game, book, market, side)
has a different number than the last time it was seen, and no row appears when
it does not. That is about thirty-six megabytes a year and it is the SAME
INFORMATION - the line at any past moment is the last row at or before it.

Retrofitting this later is not possible, because the history you did not store
is gone. It goes in first.

LAYOUT

    data/moves/<sport>/<YYYY-MM-DD>.csv.gz     one file a day, append-only
    data/state/<sport>.json                    the last seen value per key
    data/events/<sport>.json                   the schedule, from the FREE
                                               /events endpoint

The daily file is what makes pruning possible without rewriting history: old
days are whole files that can be deleted. The state file is what makes the
comparison cheap - without it, deciding whether a line moved would mean
reading back a year of changes on every poll.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("store")

ROOT = Path(__file__).resolve().parent
MOVES = ROOT / "data" / "moves"
STATE = ROOT / "data" / "state"
EVENTS = ROOT / "data" / "events"

# The row. Deliberately flat and deliberately short - this is written millions
# of times and read as a table, so a nested shape would cost both size and the
# ability to load it with one pandas call.
FIELDS = ["ts", "event_id", "book", "market", "side", "point", "price"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_path(sport: str, when: datetime) -> Path:
    return MOVES / sport / f"{when.date().isoformat()}.csv.gz"


def _key(event_id: str, book: str, market: str, side: str) -> str:
    return f"{event_id}|{book}|{market}|{side}"


def load_state(sport: str) -> dict:
    """The last value seen for every line we are tracking in this sport."""
    p = STATE / f"{sport}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:            # noqa: BLE001
        # A corrupt state file must not stop collection. The cost of starting
        # fresh is one redundant row per line on the next poll; the cost of
        # raising here is a day of lost history.
        log.warning("%s state unreadable (%s); starting it again", sport, exc)
        return {}


def save_state(sport: str, state: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / f"{sport}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
    # Atomic, because a workflow cancelled mid-write would otherwise leave a
    # half-written state file that the next run reads as "nothing is tracked"
    # and then re-records every line in the sport.
    os.replace(tmp, p)


def _changed(old: dict | None, point, price) -> bool:
    """Is this actually a different number?

    `None` and a missing key are the same thing - a line that has just
    appeared - and both count as a change, because the first sighting is the
    row everything else is measured against.

    Comparison is on the VALUES, not on a formatted string. 6.5 and "6.5"
    arrive from JSON as different types across books, and comparing them as
    text would record a move every single poll for the books that quote one
    way and none for the books that quote the other.
    """
    if old is None:
        return True
    try:
        same_point = (old.get("point") is None and point is None) or (
            old.get("point") is not None and point is not None
            and abs(float(old["point"]) - float(point)) < 1e-9)
        same_price = (old.get("price") is None and price is None) or (
            old.get("price") is not None and price is not None
            and abs(float(old["price"]) - float(price)) < 1e-9)
    except (TypeError, ValueError):
        return True
    return not (same_point and same_price)


def record(sport: str, payload: list, when: datetime | None = None) -> dict:
    """Write the lines that moved. Returns a small summary.

    `payload` is the raw list the /odds endpoint returns, unmodified. Parsing
    it here rather than in the collector means the shape the API actually
    sends is handled in ONE place, and a change to that shape breaks one file.
    """
    when = when or _utc_now()
    state = load_state(sport)
    rows = []

    for ev in payload or []:
        eid = ev.get("id")
        if not eid:
            continue
        for bk in ev.get("bookmakers") or []:
            book = bk.get("key")
            if not book:
                continue
            for mk in bk.get("markets") or []:
                market = mk.get("key")
                if not market:
                    continue
                for oc in mk.get("outcomes") or []:
                    side = oc.get("name")
                    if side is None:
                        continue
                    point = oc.get("point")
                    price = oc.get("price")
                    k = _key(eid, book, market, side)
                    if not _changed(state.get(k), point, price):
                        continue
                    rows.append({
                        "ts": when.isoformat(timespec="seconds"),
                        "event_id": eid, "book": book, "market": market,
                        "side": side,
                        "point": "" if point is None else point,
                        "price": "" if price is None else price,
                    })
                    state[k] = {"point": point, "price": price,
                                "ts": when.isoformat(timespec="seconds")}

    if rows:
        append(sport, rows, when)
    save_state(sport, state)
    return {"sport": sport, "events": len(payload or []), "moves": len(rows)}


def append(sport: str, rows: list, when: datetime | None = None) -> None:
    """Add rows to today's file for this sport, header only when it is new.

    Gzip members CONCATENATE: writing a second gzip stream onto the end of a
    gzip file produces a file that every reader decompresses as the two
    joined. That is what makes an append-only compressed log possible without
    rewriting the day's data on every poll, which at forty-eight polls a day
    would mean rewriting the whole file forty-eight times.
    """
    when = when or _utc_now()
    p = _day_path(sport, when)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    if new:
        w.writeheader()
    w.writerows(rows)
    with open(p, "ab") as fh:
        fh.write(gzip.compress(buf.getvalue().encode("utf-8")))


def read_moves(sport: str, days: int = 7):
    """Every change for this sport over the last `days`, oldest first."""
    import pandas as pd

    end = _utc_now().date()
    frames = []
    for i in range(days):
        p = _day_path(sport, _utc_now() - timedelta(days=i))
        if not p.exists():
            continue
        try:
            frames.append(pd.read_csv(p, compression="gzip"))
        except Exception as exc:                               # noqa: BLE001
            log.warning("could not read %s: %s", p.name, exc)
    if not frames:
        return pd.DataFrame(columns=FIELDS)
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)


def save_events(sport: str, events: list) -> None:
    """The schedule. From the FREE endpoint, so this is always current for
    every sport no matter what the odds budget allowed."""
    EVENTS.mkdir(parents=True, exist_ok=True)
    (EVENTS / f"{sport}.json").write_text(
        json.dumps(events or [], separators=(",", ":")))


def load_events(sport: str) -> list:
    p = EVENTS / f"{sport}.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def prune(keep_days: int) -> int:
    """Delete whole day-files past the horizon. Returns how many went.

    Whole files, never rows: a prune that had to rewrite files would have to
    decompress and recompress the archive on a schedule, and a crash halfway
    through that loses data the append path was careful to protect.
    """
    if keep_days <= 0:
        return 0
    cutoff = (_utc_now() - timedelta(days=keep_days)).date()
    gone = 0
    if not MOVES.exists():
        return 0
    for sport_dir in MOVES.iterdir():
        if not sport_dir.is_dir():
            continue
        for f in sport_dir.glob("*.csv.gz"):
            try:
                day = datetime.fromisoformat(f.stem).date()
            except ValueError:
                continue
            if day < cutoff:
                f.unlink()
                gone += 1
    return gone
