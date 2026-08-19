"""
AJ 2.0 — database build.

    python3 aj_db.py            # create the tables, leave them empty
    python3 aj_db.py --demo     # create + load a demo life to click through
    python3 aj_db.py --reset    # delete the file first (destructive, asks nothing)

Empty is the default on purpose. This is your database, and an app that greets
you with someone else's goals teaches you to ignore what's on the screen. The
demo exists for one reason: to prove the engine works before you've given it
anything real, and to give the momentum math something to chew on.

The demo life is a person mid-struggle, not a person winning — three goals where
one is genuinely ahead, one is quietly dead (no metric movement in three weeks),
and one is at-risk with a deadline close enough to hurt. Two commitments are
already blown. A coach demo where everything is green proves nothing.
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from config import Config

DB_FILE = Path(__file__).parent / Config.DB_PATH
SCHEMA_FILE = Path(__file__).parent / "schema.sql"

TODAY = date.today()


def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # Off by default in SQLite, and the ON DELETE CASCADE in the schema is a lie
    # without it — deleting a goal would orphan its milestones instead.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()
    conn.executescript(SCHEMA_FILE.read_text())
    conn.commit()
    conn.close()


def is_onboarded():
    """True once the profile row exists — the app routes on this."""
    conn = get_connection()
    try:
        row = conn.execute("SELECT onboarded_at FROM profile WHERE id = 1").fetchone()
        return bool(row and row["onboarded_at"])
    except sqlite3.OperationalError:
        return False          # tables not built yet
    finally:
        conn.close()


def _d(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


def seed_demo():
    """Load the demo life. Idempotent-by-wipe: clears user data, keeps the schema."""
    conn = get_connection()
    for table in ("metric_log", "milestones", "commitments", "checkins",
                  "messages", "briefs", "ai_spend", "events", "transactions",
                  "sources", "goals", "profile"):
        conn.execute(f"DELETE FROM {table}")

    conn.execute("""
        INSERT INTO profile (id, name, north_star, identity, values_text, stakes,
                             horizon_years, intensity, onboarded_at)
        VALUES (1, ?, ?, ?, ?, ?, 3, 90, datetime('now'))
    """, (
        "Demo",
        "Run a freight-tech company doing $1M a year that I own outright, and be "
        "in the best shape of my life while I do it.",
        "The operator who ships. Not the guy with twelve tabs open and one repo "
        "with a good README.",
        "I don't trade my health for a quarter.\n"
        "I don't take money that costs me control.\n"
        "I tell the truth about the number, especially when it's bad.\n"
        "I do not miss her recitals.",
        "Another three years of being the smartest person in a room I don't own, "
        "watching people with worse ideas and better follow-through pass me.",
    ))

    goals = [
        # (title, why, domain, metric, unit, start, target, current, start_off,
        #  deadline_off, stakes, status)
        ("CarrierGuard to $8k MRR",
         "It's the first thing I've built that strangers pay for. Getting it to $8k "
         "means the company is real and I stop asking permission.",
         "business", "MRR", "$", 0, 8000, 6100, -120, 150,
         "If this stalls I go back to consulting for another year and the window closes.",
         "active"),
        ("Ship the freight-board rebuild",
         "Half-finished code is a tax I pay every time I open the repo.",
         "craft", "features shipped", "", 0, 12, 10, -60, 45,
         "Every week it sits, the rewrite gets more expensive and less likely.",
         "active"),
        ("Get to 12% body fat",
         "I think worse when I'm heavy. This is upstream of everything else.",
         "body", "body fat %", "%", 22, 12, 14.2, -90, 120,
         "I've started this four times. A fifth restart is a character problem, not a plan problem.",
         "active"),
        ("Six months of runway in the bank",
         "Runway is the difference between choosing customers and taking them.",
         "money", "months of runway", "", 1, 6, 6, -150, -10,
         "Without it, one slow quarter turns into a job hunt.",
         "won"),
    ]
    goal_ids = []
    for (title, why, domain, metric, unit, start, target, current,
         start_off, dl_off, stakes, status) in goals:
        cur = conn.execute("""
            INSERT INTO goals (title, why, domain, metric_name, unit, start_value,
                               target_value, current_value, start_date, deadline,
                               stakes, status, closed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (title, why, domain, metric, unit, start, target, current,
              _d(start_off), _d(dl_off), stakes, status,
              _d(dl_off) if status == "won" else None))
        goal_ids.append(cur.lastrowid)
    mrr, board, body, runway = goal_ids

    # Metric history. Every line climbs, and each one is measured recently — this
    # is what a board that's being worked actually looks like.
    history = (
        [(mrr, v, o) for v, o in
         [(0, -120), (600, -100), (1400, -80), (2300, -62), (3400, -41), (4500, -22),
          (5400, -11), (6100, -1)]] +
        [(board, v, o) for v, o in
         [(0, -60), (2, -47), (4, -36), (6, -25), (8, -12), (10, -2)]] +
        [(body, v, o) for v, o in
         [(22, -90), (20.4, -74), (18.6, -60), (17.1, -45), (15.8, -30),
          (14.9, -16), (14.2, -2)]] +
        [(runway, v, o) for v, o in [(1, -150), (3, -90), (5, -40), (6, -10)]]
    )
    for goal_id, value, offset in history:
        conn.execute("INSERT INTO metric_log (goal_id, value, at, day) VALUES (?,?,?,?)",
                     (goal_id, value, f"{_d(offset)} 09:00:00", _d(offset)))

    milestones = [
        (mrr, "First 10 paying carriers", -70, -72),
        (mrr, "Self-serve signup live", -30, -33),
        (mrr, "$5k MRR", -8, -12),
        (mrr, "Two channel partners signed", 40, None),
        (board, "Schema migration merged", -30, -31),
        (board, "Driver post flow rebuilt", -6, -8),
        (board, "Broker bidding rebuilt", 20, None),
        (body, "Under 19%", -40, -44),
        (body, "Under 16%", -5, -9),
        (body, "Under 14%", 45, None),
        (runway, "Six months banked", -12, -14),
    ]
    for i, (gid, title, target_off, done_off) in enumerate(milestones):
        conn.execute("""INSERT INTO milestones (goal_id, title, target_date, done_at, sort)
                        VALUES (?,?,?,?,?)""",
                     (gid, title, _d(target_off),
                      _d(done_off) if done_off is not None else None, i))

    commitments = [
        # (goal, text, due_off, status, source, excuse)
        (mrr, "Call 20 carriers off the DAT list", -21, "kept", "aj", ""),
        (mrr, "Publish pricing page", -17, "kept", "you", ""),
        (board, "Finish the driver post flow", -12, "kept", "you", ""),
        (body, "Lift 4x this week", -10, "kept", "aj", ""),
        (mrr, "Email the 12 trial accounts", -8, "kept", "aj", ""),
        (board, "Merge the driver post branch", -6, "kept", "you", ""),
        (body, "Three lifts, no negotiation", -4, "kept", "aj", ""),
        (mrr, "Two partner conversations booked", -2, "kept", "aj", ""),
        (None, "Be at her recital, phone in the car", -3, "kept", "you", ""),
        (mrr, "Send the partner agreement to legal", 2, "open", "aj", ""),
        (board, "Broker bidding flow behind a flag", 5, "open", "you", ""),
        (body, "Four lifts, no negotiation", 6, "open", "aj", ""),
    ]
    for gid, text, due_off, status, source, excuse in commitments:
        conn.execute("""
            INSERT INTO commitments (goal_id, text, due_date, status, source, excuse, resolved_at)
            VALUES (?,?,?,?,?,?,?)
        """, (gid, text, _d(due_off), status, source, excuse,
              _d(due_off) if status in ("kept", "missed") else None))

    checkins = [
        (-9, 4, 5.0, "Twenty carrier calls, four demos booked. Lifted.", ""),
        (-8, 4, 4.5, "Trial emails out. Two replies same day.", ""),
        (-7, 3, 3.5, "Driver post flow done and merged.", ""),
        (-6, 5, 6.0, "Partner call went well. Shipped the bidding skeleton.", ""),
        (-5, 4, 4.0, "MRR crossed $5.4k. Lifted, ate clean.", ""),
        (-4, 4, 5.0, "Broker bidding half done. Two more trials converted.", ""),
        (-3, 5, 3.0, "Short day on purpose — her recital. Worth it.", ""),
        (-2, 4, 5.5, "Partner agreement drafted. MRR $6.1k.", ""),
        (-1, 4, 5.0, "Cleared the board. Lifted. Good week.", ""),
    ]
    for off, energy, hours, log, blockers in checkins:
        conn.execute("""INSERT INTO checkins (day, energy, deep_hours, log, blockers,
                        grade, debrief, engine) VALUES (?,?,?,?,?,?,?,'offline')""",
                     (_d(off), energy, hours, log, blockers,
                      88, "Closed what was on the board and the number moved. Do it again."))

    _seed_demo_feeds(conn)
    conn.commit()
    conn.close()


def _seed_demo_feeds(conn):
    """
    The rest of a life: her schedule, the school calendar, and a month of money.

    Separate from the goal seed because these come from feeds in the real app —
    an .ics subscription and a bank export — and this is what those produce.
    """
    sources = [
        ("dance", "ics", "Dance — BAND calendar", "https://band.us/band/00000000/ics/example"),
        ("school", "ics", "School calendar", "https://school.example.edu/calendar/feed.ics"),
        ("bank", "csv", "Checking — monthly export", ""),
    ]
    source_ids = {}
    for key, kind, name, url in sources:
        cur = conn.execute("""INSERT INTO sources (kind, name, url, person, enabled,
                              last_synced_at) VALUES (?,?,?,?,1,datetime('now'))""",
                           (kind, name, url, "Daughter" if key != "bank" else ""))
        source_ids[key] = cur.lastrowid

    events = [
        ("dance", "Dance — technique class", 0, "17:30", "18:30", "Studio B"),
        ("school", "Early release", 1, "13:00", "13:00", ""),
        ("dance", "Dance — rehearsal", 2, "17:30", "19:00", "Studio B"),
        ("school", "Parent-teacher conference", 3, "16:15", "16:35", "Room 214"),
        ("dance", "Costume fitting", 4, "16:00", "17:00", "Studio A"),
        ("dance", "Dance — technique class", 5, "17:30", "18:30", "Studio B"),
        ("school", "Science fair project due", 6, "08:00", "08:00", ""),
        ("dance", "Spring recital — call time 4:00", 9, "16:00", "20:00", "Civic Auditorium"),
        ("school", "Report cards issued", 12, "08:00", "08:00", ""),
    ]
    for key, title, off, start, end, location in events:
        day = _d(off)
        conn.execute("""INSERT INTO events (source_id, title, day, start_time, end_time,
                        location, person, uid) VALUES (?,?,?,?,?,?,?,?)""",
                     (source_ids[key], title, day, start, end, location, "Daughter",
                      f"demo-{key}-{off}"))

    # Two months of money, because a single month has nothing to be compared to
    # and "up from what?" is the only interesting question about a spend number.
    # The board is winning; dining is the one line drifting, which is what a real
    # good month looks like.
    fixed = [("Rent", -2150.00, "housing"), ("Car payment", -540.00, "transport"),
             ("Utilities", -180.00, "housing"), ("Insurance", -240.00, "insurance"),
             ("Dance tuition", -185.00, "kids")]
    money = []
    for month, (dining_each, dining_count) in enumerate([(58.00, 4), (94.00, 7)]):
        base = -60 + month * 30
        money += [(base + 1, "Payroll deposit", 6400.00, "income"),
                  (base + 15, "Payroll deposit", 6400.00, "income"),
                  (base + 16, "Transfer to savings", -1500.00, "savings"),
                  (base + 20, "Stripe payout", 1220.00, "income")]
        for i, (label, amount, category) in enumerate(fixed):
            money.append((base + 2 + i, label, amount, category))
        for i in range(4):
            money.append((base + 5 + i * 6, "Groceries", -198.00 - i * 8, "food"))
        for i in range(dining_count):
            money.append((base + 3 + i * 4, "Restaurants", -dining_each - i * 3, "dining"))
        money.append((base + 9, "Gas", -62.00, "transport"))
        money.append((base + 24, "AWS", -99.00, "business"))
    money.append((-11, "Costume deposit", -140.00, "kids"))

    for off, description, amount, category in money:
        conn.execute("""INSERT INTO transactions (source_id, day, description, amount,
                        category) VALUES (?,?,?,?,?)""",
                     (source_ids["bank"], _d(off), description, amount, category))


def main():
    args = set(sys.argv[1:])
    if "--reset" in args and DB_FILE.exists():
        DB_FILE.unlink()
        print(f"removed {DB_FILE.name}")
    create_tables()
    print(f"tables ready in {DB_FILE.name}")
    if "--demo" in args:
        seed_demo()
        print("demo life loaded — a board that is winning, her schedule, and two months of money")
    else:
        print("empty — open the app and AJ will onboard you")


if __name__ == "__main__":
    main()
