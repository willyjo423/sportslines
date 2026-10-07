"""What to watch, and how much it is allowed to cost.

EVERYTHING EXPENSIVE IS IN THIS FILE, on purpose. The Odds API charges

    cost = [number of markets] x [number of regions]

per sport per poll, so the only things that move the bill are the sport list,
the market list, how far ahead you look, and how often the workflow runs. All
four are here. Scattering them across three modules is how a hobby project
quietly turns into sixty dollars a month nobody decided to spend.

CURRENTLY TUNED FOR: the free plan, 500 credits a month, spent over about a
week. Same-day games only, eight refreshes a day, two markets a sport. That
works out to 8 credits a pass and 64 a day.

TO MOVE TO THE 100K TIER ($59), three edits and nothing else:
    MONTHLY_CREDITS = 100000
    DAILY_CREDITS   = None        (let the monthly figure decide)
    LOOKAHEAD_HOURS = 48
and put the third market back on the sports that have one.
"""
from __future__ import annotations

# ----------------------------------------------------------------- the money
# Credits per month on your plan. The Odds API tiers, as of this writing:
#   free    500        20K    20,000 ($30)
#   100K  100,000 ($59)        5M  5,000,000 ($119)
#
# The collector also reads the real remaining balance out of the
# `x-requests-remaining` response header, and the LOWER of the two wins - so a
# wrong number here cannot overspend a real account, only under-spend it.
MONTHLY_CREDITS = 500

# A safety margin, because the month's last day should not be the one where
# the site goes blank. 0.9 schedules nine tenths of the allowance and leaves
# the rest as slack for manual runs and retries.
BUDGET_USE = 0.90

# SPEND EXACTLY THIS MANY CREDITS A DAY, overriding the monthly figure above.
#
# `MONTHLY_CREDITS / 30` is right for an open-ended subscription and wrong for
# "I have 500 credits and I want a week out of them". This makes the
# arithmetic yours.
#
# 70 rather than the 64 actually needed: the allowance is released gradually
# through the day and the schedule below clusters in the evening, so at 64 the
# last pass of the day was starved. Swept it - 70 is the smallest figure that
# funds all eight.
#
# None means "work it out from MONTHLY_CREDITS".
DAILY_CREDITS = 70

# How many times a day the workflow runs. MUST MATCH the cron in
# .github/workflows/watch.yml - the collector uses it to pace itself, and a
# figure that disagrees with the schedule either starves the last passes of
# the day or lets the first one spend everything.
POLLS_PER_DAY = 8

# WHEN THE BUDGET DAY STARTS, in UTC hours. 8 is 4am Eastern.
#
# Not cosmetic. Pacing against midnight UTC resets the allowance at 8pm
# Eastern - the middle of US prime time, when lines move most - so the evening
# would get the start-of-day trickle while the credits sat fully released at
# four in the morning. Anchoring to 4am Eastern puts the reset in the dead
# zone.
BUDGET_DAY_START_UTC = 8

# WHEN THE BUDGET CANNOT COVER EVERY SPORT, WATCH THE TOP N PROPERLY.
#
# Line movement needs the SAME game seen repeatedly: credits spread over six
# sports once a day buy six snapshots and no movement at all, while the same
# credits on one sport buy a chart.
#
# 0 means never narrow - every sport on the board, every pass. That is the
# setting here, because trimming to two markets and same-day games brought a
# full pass down to 8 credits, which the allowance covers.
FOCUS = 0

# IMMINENCE BEATS PREFERENCE, when FOCUS is narrowing. Hours-to-first-game
# bands, soonest first. The earliest band with anything in it wins outright,
# and `priority` only decides who wins WITHIN a band.
#
# Without this, priority alone decided - and on a Wednesday in October that
# spent the whole day's allowance on Thursday-night NFL while four MLB playoff
# games started in three hours and went unwatched.
IMMINENCE_BANDS = [6, 12, 24, 48]

# ------------------------------------------------------------- what to watch
# Sport keys are The Odds API's own (`/v4/sports` lists them all, free).
#
# TWO MARKETS A SPORT, AND WHICH TWO DEPENDS ON THE SPORT. Every market is a
# credit, so a third one costs a third of the refresh rate.
#
#   spread sports     the moneyline is close to redundant - it moves with the
#                     spread, because they price the same opinion two ways -
#                     so it is the one to drop.
#   hockey, baseball  the moneyline IS the market and the puck line and run
#                     line are the afterthought, so these keep h2h and drop
#                     spreads instead.
#   soccer, tennis,   two-way or three-way prices only. Asking for a spread
#   combat sports     an ATP match does not have still costs a credit and
#                     returns nothing.
#
# Measured on a real same-day board: 12 credits a pass down to 8, which took
# five refreshes a day up to eight.
#
# `priority` breaks ties when FOCUS is narrowing. Higher goes first.
SPORTS = {
    # --- spread sports: the spread and the total, no moneyline
    "americanfootball_nfl":      {"markets": ["spreads", "totals"], "priority": 100},
    "americanfootball_ncaaf":    {"markets": ["spreads", "totals"], "priority": 90},
    "basketball_nba":            {"markets": ["spreads", "totals"], "priority": 85},
    "basketball_ncaab":          {"markets": ["spreads", "totals"], "priority": 70},
    "basketball_wnba":           {"markets": ["spreads", "totals"], "priority": 60},

    # --- moneyline sports: the price and the total, no puck or run line
    "icehockey_nhl":             {"markets": ["h2h", "totals"], "priority": 80},
    "baseball_mlb":              {"markets": ["h2h", "totals"], "priority": 75},

    # --- soccer: three-way price plus totals
    "soccer_epl":                {"markets": ["h2h", "totals"], "priority": 55},
    "soccer_uefa_champs_league": {"markets": ["h2h", "totals"], "priority": 55},
    "soccer_spain_la_liga":      {"markets": ["h2h", "totals"], "priority": 45},
    "soccer_italy_serie_a":      {"markets": ["h2h", "totals"], "priority": 45},
    "soccer_germany_bundesliga": {"markets": ["h2h", "totals"], "priority": 45},
    "soccer_usa_mls":            {"markets": ["h2h", "totals"], "priority": 40},

    # --- two-way, price only. One credit a pass, which is why they are
    #     affordable to carry even on the free plan.
    "tennis_atp_aus_open_singles": {"markets": ["h2h"], "priority": 35},
    "tennis_wta_aus_open_singles": {"markets": ["h2h"], "priority": 30},
    "mma_mixed_martial_arts":      {"markets": ["h2h"], "priority": 35},
    "boxing_boxing":               {"markets": ["h2h"], "priority": 25},
}

# HOW FAR AHEAD TO WATCH, in hours.
#
# A sport costs nothing on a day it has no game inside this window, so this is
# the cheapest lever in the file. 48 meant paying to watch Thursday-night NFL
# on a Tuesday, when its line has barely started moving. 18 is same-day plus a
# little, which is what a high refresh rate is actually for.
LOOKAHEAD_HOURS = 18

# One region. Adding "uk,eu" would TRIPLE the bill for books most people in
# the US cannot bet at - the easiest way there is to waste the budget.
REGIONS = "us"

# American odds, and point spreads as whole or half numbers. The alternative
# is decimal; American is what the books in this region quote.
ODDS_FORMAT = "american"

# ------------------------------------------------------- what counts as news
# A move has to clear these before it is called anything. Per MARKET, because
# half a point of spread and ten cents of price are not the same size of
# event.
MOVE_MIN = {
    "spreads": 0.5,      # points
    "totals": 0.5,       # points
    "h2h": 10,           # cents of American price
}

# Football margins pile up on 3 and 7, so crossing one is worth more than the
# half point it took. Basketball and hockey have far flatter distributions -
# the entries here are the small bumps that do exist, and the page says they
# are weaker.
KEY_NUMBERS = {
    "americanfootball_nfl":   [3, 7, 10, 14, 6, 4],
    "americanfootball_ncaaf": [3, 7, 10, 14, 6, 4],
    "basketball_nba":         [5, 7, 10],
    "basketball_ncaab":       [3, 5, 7, 10],
    "icehockey_nhl":          [1.5],
    "baseball_mlb":           [1.5],
}

# How many books must move the same way, inside the window, before it is steam
# rather than one trader with a view.
STEAM_BOOKS = 3

# THE WINDOW HAS TO BE WIDER THAN THE GAP BETWEEN PASSES, or steam can never
# fire - and a flag that never fires looks exactly like one that looked and
# found nothing. At eight passes a day the gaps run two to three hours, so 200
# minutes means "several books moved the same way between consecutive passes".
# Set it back to 60 if you ever poll every half hour.
STEAM_WINDOW_MIN = 200

# How far off the consensus a book must sit before it is worth pointing at.
OUTLIER_MIN = {"spreads": 1.0, "totals": 1.0, "h2h": 20}

# How long a line can sit unchanged, while the board around it moves, before
# the stillness is itself the observation. Keep it above the polling gap, or
# "has not moved" is mostly a statement about how often you looked.
FREEZE_HOURS = 9

# ------------------------------------------------------------------- keeping
# Days of change history to keep per sport. A year of CHANGES is tens of
# megabytes, which a repo carries fine; a year of full snapshots is gigabytes,
# which it does not. See store.py for why only changes are written.
KEEP_DAYS = 400
