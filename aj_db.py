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
                  "messages", "briefs", "ai_spend", "goals", "profile"):
        conn.execute(f"DELETE FROM {table}")

    conn.execute("""
        INSERT INTO profile (id, name, north_star, identity, values_text, stakes,
                             horizon_years, intensity, onboarded_at)
        VALUES (1, ?, ?, ?, ?, ?, 3, 75, datetime('now'))
    """, (
        "Demo",
        "Run a freight-tech company doing $1M a year that I own outright, and be "
        "in the best shape of my life while I do it.",
        "The operator who ships. Not the guy with twelve tabs open and one repo "
        "with a good README.",
        "I don't trade my health for a quarter.\n"
        "I don't take money that costs me control.\n"
        "I tell the truth about the number, especially when it's bad.",
        "Another three years of being the smartest person in a room I don't own, "
        "watching people with worse ideas and better follow-through pass me.",
    ))

    goals = [
        # (title, why, domain, metric, unit, start, target, current, start_off, deadline_off, stakes)
        ("CarrierGuard to $8k MRR",
         "It's the first thing I've built that strangers pay for. Getting it to $8k "
         "means the company is real and I stop asking permission.",
         "business", "MRR", "$", 0, 8000, 2400, -120, 150,
         "If this stalls I go back to consulting for another year and the window closes."),
        ("Ship the freight-board rebuild",
         "Half-finished code is a tax I pay every time I open the repo.",
         "craft", "features shipped", "", 0, 12, 3, -60, 45,
         "Every week it sits, the rewrite gets more expensive and less likely."),
        ("Get to 12% body fat",
         "I think worse when I'm heavy. This is upstream of everything else.",
         "body", "body fat %", "%", 22, 12, 18.5, -90, 120,
         "I've started this four times. A fifth restart is a character problem, not a plan problem."),
    ]
    goal_ids = []
    for (title, why, domain, metric, unit, start, target, current,
         start_off, dl_off, stakes) in goals:
        cur = conn.execute("""
            INSERT INTO goals (title, why, domain, metric_name, unit, start_value,
                               target_value, current_value, start_date, deadline, stakes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (title, why, domain, metric, unit, start, target, current,
              _d(start_off), _d(dl_off), stakes))
        goal_ids.append(cur.lastrowid)
    mrr, board, body = goal_ids

    # Metric history. The shapes matter: MRR is climbing, the rebuild has not moved
    # in 22 days (AJ must catch that), body fat is drifting the wrong way lately.
    history = (
        [(mrr, v, o) for v, o in
         [(0, -120), (400, -100), (900, -80), (1400, -62), (1800, -41), (2100, -22),
          (2250, -11), (2400, -3)]] +
        [(board, v, o) for v, o in [(0, -60), (1, -47), (2, -36), (3, -22)]] +
        [(body, v, o) for v, o in
         [(22, -90), (21.1, -74), (20.2, -60), (19.4, -45), (18.9, -30),
          (18.4, -16), (18.5, -5)]]
    )
    for goal_id, value, offset in history:
        conn.execute("INSERT INTO metric_log (goal_id, value, at, day) VALUES (?,?,?,?)",
                     (goal_id, value, f"{_d(offset)} 09:00:00", _d(offset)))

    milestones = [
        (mrr, "First 10 paying carriers", -70, -66),
        (mrr, "Self-serve signup live", -30, -24),
        (mrr, "$5k MRR", 40, None),
        (mrr, "Two channel partners signed", 95, None),
        (board, "Schema migration merged", -30, -28),
        (board, "Driver post flow rebuilt", -6, None),      # overdue, undone
        (board, "Broker bidding rebuilt", 20, None),
        (body, "Under 19%", -20, -18),
        (body, "Under 16%", 45, None),
    ]
    for i, (gid, title, target_off, done_off) in enumerate(milestones):
        conn.execute("""INSERT INTO milestones (goal_id, title, target_date, done_at, sort)
                        VALUES (?,?,?,?,?)""",
                     (gid, title, _d(target_off),
                      _d(done_off) if done_off is not None else None, i))

    commitments = [
        # (goal, text, due_off, status, source, excuse)
        (mrr, "Call 20 carriers off the DAT list", -12, "kept", "aj", ""),
        (mrr, "Publish pricing page", -8, "kept", "you", ""),
        (board, "Finish the driver post flow", -6, "missed", "you",
         "Got pulled into a support fire for two days."),
        (body, "Lift 4x this week", -5, "missed", "aj",
         "Traveled Thursday and never made it back."),
        (mrr, "Email the 6 trial accounts that went quiet", -1, "open", "aj", ""),
        (board, "Merge the driver post branch", 1, "open", "you", ""),
        (mrr, "Two partner conversations booked", 4, "open", "aj", ""),
        (body, "Three lifts, no negotiation", 6, "open", "aj", ""),
    ]
    for gid, text, due_off, status, source, excuse in commitments:
        conn.execute("""
            INSERT INTO commitments (goal_id, text, due_date, status, source, excuse, resolved_at)
            VALUES (?,?,?,?,?,?,?)
        """, (gid, text, _d(due_off), status, source, excuse,
              _d(due_off) if status in ("kept", "missed") else None))

    checkins = [
        (-6, 4, 5.5, "Shipped the pricing page. Twenty carrier calls, three demos booked.", ""),
        (-5, 3, 2.0, "Travel day. Answered support, nothing else.", "On a plane"),
        (-4, 2, 0.5, "Wrote a lot of nothing. Read HN.", "No energy, no plan for the day"),
        (-3, 4, 4.0, "Back on it — schema migration merged, two trial calls.", ""),
        (-2, 3, 3.0, "Support fire. Two hours on the rebuild after 9pm.", "Support queue"),
        (-1, 4, 4.5, "Good day. Driver flow is 80% there.", ""),
    ]
    for off, energy, hours, log, blockers in checkins:
        conn.execute("""INSERT INTO checkins (day, energy, deep_hours, log, blockers)
                        VALUES (?,?,?,?,?)""",
                     (_d(off), energy, hours, log, blockers))

    conn.commit()
    conn.close()


def main():
    args = set(sys.argv[1:])
    if "--reset" in args and DB_FILE.exists():
        DB_FILE.unlink()
        print(f"removed {DB_FILE.name}")
    create_tables()
    print(f"tables ready in {DB_FILE.name}")
    if "--demo" in args:
        seed_demo()
        print("demo life loaded — three goals, one of them quietly dead, two blown commitments")
    else:
        print("empty — open the app and AJ will onboard you")


if __name__ == "__main__":
    main()
