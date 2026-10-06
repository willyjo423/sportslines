"""The Odds API client, and the budget that decides how much of it to use.

TWO ENDPOINTS, AND ONLY ONE OF THEM COSTS ANYTHING.

    /v4/sports/{sport}/events   FREE. Which games exist and when they start.
    /v4/sports/{sport}/odds     cost = markets x regions, per call.

That asymmetry is the whole design. The schedule for every sport can be
refreshed on every run at zero cost, so the site always knows about every
game in every sport no matter what tier you are on. The budget only decides
which of those games get their LINES watched.

The key never appears in a log line, an error message or a committed file.
`_redact` runs over anything that gets printed, because the quickest way to
leak a key is an exception whose message contains the URL that failed.
"""
from __future__ import annotations

import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("odds")

BASE = "https://api.the-odds-api.com/v4"
TIMEOUT = 30
RETRIES = 3


class OddsError(RuntimeError):
    pass


class Budget:
    """How many credits today may spend, and what is left of them.

    Two numbers bound it and the SMALLER always wins:

      the plan      what config says you pay for, divided across the month
      the account   `x-requests-remaining`, which the API returns on every
                    costed call and which is the truth

    Trusting only the first would overspend a real account whose month is
    nearly up. Trusting only the second would spend March's allowance in the
    first week, because on the 1st it reads as the whole month. Both.
    """

    def __init__(self, monthly: int, use: float = 0.9, days_in_month: int = 30):
        self.planned_today = max(1, int(monthly * use / days_in_month))
        self.spent = 0
        self.remaining_account = None          # filled in by the first call

    def allows(self, cost: int) -> bool:
        if self.spent + cost > self.planned_today:
            return False
        if (self.remaining_account is not None
                and cost > self.remaining_account):
            return False
        return True

    def charge(self, cost: int, remaining=None) -> None:
        self.spent += cost
        if remaining is not None:
            try:
                self.remaining_account = int(remaining)
            except (TypeError, ValueError):
                pass

    def __str__(self) -> str:
        left = self.planned_today - self.spent
        acct = ("unknown" if self.remaining_account is None
                else f"{self.remaining_account:,}")
        return (f"{self.spent} of {self.planned_today} credits used today, "
                f"{left} left; account balance {acct}")


def _redact(text: str, key: str | None) -> str:
    """Never print the key. Not in a URL, not in an exception, not once."""
    out = str(text)
    if key:
        out = out.replace(key, "***")
    # Belt and braces: anything that looks like the parameter, whatever its
    # value, in case a different key reaches a log by another route.
    import re
    return re.sub(r"(apiKey=)[^&\s]+", r"\1***", out)


def _get(path: str, params: dict, key: str) -> tuple:
    """GET, with retries on the failures that are worth retrying.

    A 401 or 422 is an argument the server will refuse again, so retrying it
    just burns time; a 429 or a 5xx is the server asking to be asked later.
    Retrying everything is how a broken sport key turns into ninety seconds
    of pointless waiting on every run.
    """
    q = dict(params)
    q["apiKey"] = key
    url = f"{BASE}{path}?{urllib.parse.urlencode(q)}"
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "line-watch/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                import json
                body = json.loads(r.read().decode("utf-8"))
                return body, dict(r.headers)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403, 404, 422):
                raise OddsError(
                    f"{exc.code} from {_redact(path, key)}: "
                    f"{_redact(exc.read()[:200], key)}") from None
            log.warning("HTTP %s on %s, attempt %d", exc.code, path,
                        attempt + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            log.warning("network error on %s: %s", path,
                        _redact(exc, key))
        time.sleep(2 ** attempt)
    raise OddsError(f"{_redact(path, key)} failed after {RETRIES} tries: "
                    f"{_redact(last, key)}")


def api_key() -> str:
    k = os.environ.get("ODDS_API_KEY", "").strip()
    if not k:
        raise OddsError(
            "ODDS_API_KEY is not set. Put it in the repository's Actions "
            "secrets - never in a file in the repo, and never in a chat.")
    return k


def list_sports(key: str) -> list:
    """Every sport the API carries, and whether it is in season. FREE."""
    body, _ = _get("/sports/", {"all": "false"}, key)
    return body or []


def events(sport: str, key: str) -> list:
    """The schedule for one sport. FREE - no markets, no regions, no cost."""
    body, _ = _get(f"/sports/{sport}/events", {}, key)
    return body or []


def odds(sport: str, markets: list, regions: str, odds_format: str,
         key: str, budget: Budget) -> tuple:
    """Lines for one sport. COSTS markets x regions.

    Returns (payload, cost). The budget is checked BEFORE the call, not
    after - a budget that only notices it has overspent is a bill, not a
    budget.
    """
    cost = len(markets) * len(regions.split(","))
    if not budget.allows(cost):
        return None, 0
    body, headers = _get(f"/sports/{sport}/odds", {
        "regions": regions,
        "markets": ",".join(markets),
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }, key)
    # The server tells us what it actually charged. Trusting our own
    # arithmetic instead would drift the moment the pricing changes.
    charged = headers.get("x-requests-last")
    try:
        cost = int(charged) if charged is not None else cost
    except (TypeError, ValueError):
        pass
    budget.charge(cost, headers.get("x-requests-remaining"))
    return body or [], cost
