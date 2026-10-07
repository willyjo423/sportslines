"""What to watch, and how much it is allowed to cost.

EVERYTHING EXPENSIVE IS IN THIS FILE, on purpose. The Odds API charges

    cost = [number of markets] x [number of regions]

per sport per poll, so the only levers that move the bill are the sport list,
the market list and how often the workflow runs. Putting them in code
scattered across three modules is how a hobby project quietly turns into
sixty dollars a month nobody decided to spend.

THE BUDGET IS A HARD STOP, not a target. `collect.py` asks this file how many
credits today may use, spends them in priority order, and stops. On the free
tier that means the most interesting sport gets watched and the rest get their
schedule shown without odds; on a paid tier the same code watches everything.
Changing tiers is one number here.
"""
from __future__ import annotations

# ----------------------------------------------------------------- the money
# Credits per month on your plan. The Odds API tiers, as of this writing:
#   free    500        20K    20,000 ($30)
#   100K  100,000 ($59)        5M  5,000,000 ($119)
#
# Set this to what you are actually paying for. The collector reads the real
# remaining balance out of the `x-requests-remaining` response header too, and
# the lower of the two wins - so a wrong number here cannot overspend a real
# account, it can only under-spend it.
MONTHLY_CREDITS = 500

# A safety margin, because the month's last day should not be the one where
# the site goes blank. 0.9 means nine tenths of the allowance gets scheduled
# and the rest is slack for manual runs and retries.
BUDGET_USE = 0.90

# SPEND EXACTLY THIS MANY CREDITS A DAY, overriding the monthly figure above.
#
# `MONTHLY_CREDITS / 30` is the right default for an open-ended subscription,
# and the wrong one for "I have 500 credits and I want seven days out of
# them". Set this and the arithmetic is yours: 500 credits over 7 days is
# about 70 a day, and a full-board poll of tonight's eight sports costs 20,
# so that is three complete passes a day with slack.
#
# None means "work it out from MONTHLY_CREDITS".
DAILY_CREDITS = 70

# How many times a day the workflow runs. The cron is "7,37 * * * *", so 48.
# The collector needs this to PACE itself: without it the first run of the day
# spends the whole allowance and the other forty-seven do nothing, which is
# exactly what the first dry run of this project showed.
POLLS_PER_DAY = 3

# WHEN THE BUDGET DAY STARTS, in UTC hours. 8 is 4am Eastern.
#
# This is not cosmetic. Pacing against midnight UTC means the allowance
# resets at 8pm Eastern - the middle of US prime time, when lines move most -
# so the evening gets the start-of-day trickle and the credits are fully
# released at four in the morning when nothing is happening. Anchoring the
# day to 4am Eastern puts the reset in the dead zone and has the allowance
# mostly released by the time the evening slate is being bet.
BUDGET_DAY_START_UTC = 8

# WHEN THE BUDGET IS TIGHT, WATCH FEWER THINGS PROPERLY.
#
# Line movement needs the SAME game seen repeatedly. Fifteen credits spent on
# six sports once a day buys six snapshots and no movement at all; spent on
# one sport it buys a chart. So when the day's allowance cannot cover every
# sport on every poll, the collector sticks to the top `FOCUS` sports by
# priority rather than chasing whichever game starts soonest - consistency is
# what makes a history, and a different sport each poll makes none.
#
# Set it to 0 to always try every sport, which is the right setting once the
# budget is large enough to afford them all.
# 0 means never narrow - every sport on the board gets polled every time.
# That is the right setting once the plan can afford it, and at the 100K tier
# peak season runs about 42% of the allowance, so it can.
FOCUS = 0

# IMMINENCE BEATS PREFERENCE. Hours-to-first-game bands, soonest first.
#
# A line for a game thirty hours away barely moves - there will be sixty more
# polls before it starts. A line for a game in three hours is moving now and
# will not be moving later. So the budget goes to the earliest band that has
# anything in it, and `priority` only decides who wins WITHIN a band.
#
# Without this, config priority alone decided, and on a Wednesday in October
# that spent the entire day's allowance on Thursday-night NFL while four MLB
# playoff games started in three hours and went unwatched.
IMMINENCE_BANDS = [6, 12, 24, 48]

# ------------------------------------------------------------- what to watch
# Sport keys are The Odds API's own (`/v4/sports` lists them all, free).
#
# MARKETS ARE PER SPORT because they are not all three-market sports, and
# asking for a market a sport does not have still costs a credit. Tennis is
# head-to-head; it has no spread to move. Soccer is three-way. Paying for
# `spreads` on an ATP match buys nothing and costs the same as paying for one
# that exists.
#
# `priority` breaks ties when the budget runs out before the sports do. Higher
# goes first.
SPORTS = {
    # --- the three-market US sports: moneyline, spread, total
    "americanfootball_nfl":      {"markets": ["h2h", "spreads", "totals"], "priority": 100},
    "americanfootball_ncaaf":    {"markets": ["h2h", "spreads", "totals"], "priority": 90},
    "basketball_nba":            {"markets": ["h2h", "spreads", "totals"], "priority": 85},
    "basketball_ncaab":          {"markets": ["h2h", "spreads", "totals"], "priority": 70},
    "basketball_wnba":           {"markets": ["h2h", "spreads", "totals"], "priority": 60},
    "icehockey_nhl":             {"markets": ["h2h", "spreads", "totals"], "priority": 80},
    "baseball_mlb":              {"markets": ["h2h", "spreads", "totals"], "priority": 75},

    # --- soccer: three-way moneyline plus totals. Spreads exist as Asian
    #     handicap and are carried patchily, so they are left off rather than
    #     paid for and found empty.
    "soccer_epl":                {"markets": ["h2h", "totals"], "priority": 55},
    "soccer_uefa_champs_league": {"markets": ["h2h", "totals"], "priority": 55},
    "soccer_spain_la_liga":      {"markets": ["h2h", "totals"], "priority": 45},
    "soccer_italy_serie_a":      {"markets": ["h2h", "totals"], "priority": 45},
    "soccer_germany_bundesliga": {"markets": ["h2h", "totals"], "priority": 45},
    "soccer_usa_mls":            {"markets": ["h2h", "totals"], "priority": 40},

    # --- two-way, moneyline only. One credit a poll instead of three, which
    #     is why they are affordable to carry even on a small plan.
    "tennis_atp_aus_open_singles": {"markets": ["h2h"], "priority": 35},
    "tennis_wta_aus_open_singles": {"markets": ["h2h"], "priority": 30},
    "mma_mixed_martial_arts":      {"markets": ["h2h"], "priority": 35},
    "boxing_boxing":               {"markets": ["h2h"], "priority": 25},
}

# One region. Adding "uk,eu" would TRIPLE the bill for books most people in
# the US cannot bet at, which is the single easiest way to waste the budget.
REGIONS = "us"

# American odds, and the point spreads as whole or half numbers. The other
# option is decimal; American is what the books in this region quote.
ODDS_FORMAT = "american"

# ------------------------------------------------------- what counts as news
# A move has to clear these before it is called anything. They are per MARKET
# because a half point of spread and ten cents of moneyline are not the same
# size of event.
MOVE_MIN = {
    "spreads": 0.5,      # points
    "totals": 0.5,       # points
    "h2h": 10,           # cents of American price
}

# Football lands on 3 and 7 more than on any other number, so crossing one is
# worth more than the half point it took. Basketball and hockey have far
# flatter distributions - the entries here are the small bumps that do exist,
# and the honesty note on the page says they are weaker.
KEY_NUMBERS = {
    "americanfootball_nfl":   [3, 7, 10, 14, 6, 4],
    "americanfootball_ncaaf": [3, 7, 10, 14, 6, 4],
    "basketball_nba":         [5, 7, 10],
    "basketball_ncaab":       [3, 5, 7, 10],
    "icehockey_nhl":          [1.5],
    "baseball_mlb":           [1.5],
}

# How many books have to move the same way, inside the window, before it is
# steam rather than one trader with a view.
# AT THREE POLLS A DAY, A ONE-HOUR WINDOW CAN NEVER CONTAIN TWO
# OBSERVATIONS, so a 60-minute steam window would mean steam never fires -
# and a flag that never fires is indistinguishable, on the page, from one
# that looked and found nothing. Widened to span the gap between polls, so
# "steam" here means several books moved the same way BETWEEN consecutive
# passes. Coarser than the real thing, and honest about it.
# Back to 60 when polling every 30 minutes.
STEAM_BOOKS = 3
STEAM_WINDOW_MIN = 420

# How far off the consensus a book has to sit before it is worth pointing at.
OUTLIER_MIN = {"spreads": 1.0, "totals": 1.0, "h2h": 20}

# How long a line can sit unchanged, while its neighbours move, before that
# itself is the observation.
# Same reasoning: at six-hour gaps, "has not moved in six hours" is mostly a
# statement about how often we looked.
FREEZE_HOURS = 18

# ------------------------------------------------------------------ keeping
# Days of change history to keep per sport. A year of changes is tens of
# megabytes, which a repo carries fine; a year of full snapshots is gigabytes,
# which it does not. See store.py for why only changes are written.
KEEP_DAYS = 400
