"""
AJ 2.0 — the momentum engine.

Every number AJ throws at you is computed here, in plain Python, from rows with
dates on them. Nothing in this file calls a model.

That separation is the whole architecture. A coach that generates both the
assessment and the evidence is a coach that can talk itself — and you — into
anything. So the arithmetic of your life lives here and is deterministic; the
model's job is to read these numbers and decide what to say about them. When AJ
tells you a goal is dead, you can click through to the rows that say so.

Four things this file knows how to do:

    credibility()   what your word has been worth, from the commitment ledger
    goal_health()   pace against the calendar, per goal
    signals()       what a good chief of staff would flag this morning
    snapshot()      all of it, assembled once, for the UI and the prompts alike
"""

from datetime import date, datetime, timedelta

# Commitment follow-through has a half-life. A miss three weeks ago should not
# weigh the same as a miss yesterday, or the score can never recover and stops
# being information.
CREDIBILITY_HALF_LIFE_DAYS = 21
CREDIBILITY_WINDOW_DAYS = 90

# An open commitment this far past due is counted as a miss. Leaving something
# open forever is the most common way to avoid a bad number, so the math closes
# that door.
GRACE_DAYS = 2

# A goal whose number hasn't moved in this long gets called out, regardless of
# how it looks against pace. This is the check that catches a goal you have
# quietly stopped working on while it still reads green.
STALE_DAYS = 14

STATUS_LABELS = {
    "strong": "Ahead",
    "on_track": "On track",
    "watch": "Slipping",
    "at_risk": "At risk",
}


def _today():
    return date.today()


def _parse(value):
    """Dates arrive from SQLite as 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."""
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _days_ago(value, today=None):
    d = _parse(value)
    return None if d is None else ((today or _today()) - d).days


# --------------------------------------------------------------- credibility --

def credibility(conn, today=None):
    """
    What your word has been worth lately, 0-100.

    Recency-weighted over resolved commitments in the trailing 90 days, with
    overdue-and-still-open items counted as misses. Returns the score plus the
    counts behind it, because a score with no denominator is a horoscope.
    """
    today = today or _today()
    cutoff = (today - timedelta(days=CREDIBILITY_WINDOW_DAYS)).isoformat()
    rows = conn.execute("""
        SELECT status, due_date, resolved_at FROM commitments
        WHERE due_date >= ?
    """, (cutoff,)).fetchall()

    kept_w = total_w = 0.0
    kept = missed = dropped = open_overdue = 0

    for row in rows:
        age = _days_ago(row["resolved_at"] or row["due_date"], today)
        if age is None or age < 0:
            continue                      # not due yet — no verdict to give
        status = row["status"]
        if status == "open" and age > GRACE_DAYS:
            status, open_overdue = "missed", open_overdue + 1
        elif status == "open":
            continue

        if status == "dropped":
            # Explicitly abandoning a commitment is honest, and honesty should not
            # be punished the way a silent miss is. Counted and shown, not scored.
            dropped += 1
            continue

        weight = 0.5 ** (age / CREDIBILITY_HALF_LIFE_DAYS)
        total_w += weight
        if status == "kept":
            kept += 1
            kept_w += weight
        else:
            missed += 1

    resolved = kept + missed
    score = round(100 * kept_w / total_w) if total_w else None

    if score is None:
        label, tone = "No record yet", "neutral"
    elif score >= 85:
        label, tone = "Your word is good", "strong"
    elif score >= 65:
        label, tone = "Mostly reliable", "on_track"
    elif score >= 40:
        label, tone = "Slipping", "watch"
    else:
        label, tone = "You don't do what you say", "at_risk"

    return {"score": score, "label": label, "tone": tone, "kept": kept,
            "missed": missed, "dropped": dropped, "open_overdue": open_overdue,
            "resolved": resolved}


# --------------------------------------------------------------- goal health --

def goal_health(goal, conn=None, today=None):
    """
    One goal, measured against the calendar rather than against your feelings.

    Progress and pace are both expressed as fractions of the distance from
    start_value to target_value, which makes goals where the number goes down
    (body fat, debt, churn) work without a special case: both the numerator and
    the denominator flip sign together.
    """
    today = today or _today()
    start_v = float(goal["start_value"])
    target_v = float(goal["target_value"])
    current_v = float(goal["current_value"])
    span = target_v - start_v

    progress = 1.0 if span == 0 else (current_v - start_v) / span
    progress = max(-1.0, min(progress, 2.0))     # clamp so one bad reading can't distort a bar

    start_d = _parse(goal["start_date"]) or today
    deadline = _parse(goal["deadline"]) or today
    total_days = max((deadline - start_d).days, 1)
    elapsed = (today - start_d).days
    days_left = (deadline - today).days
    expected = max(0.0, min(elapsed / total_days, 1.0))

    if goal["status"] == "won":
        status = "strong"
    elif progress >= 1.0:
        status = "strong"
    elif days_left < 0:
        status = "at_risk"
    elif expected <= 0.02:
        status = "on_track"                      # too early to judge anyone
    else:
        ratio = progress / expected
        status = ("strong" if ratio >= 1.05 else
                  "on_track" if ratio >= 0.85 else
                  "watch" if ratio >= 0.6 else "at_risk")

    # What it now takes per week to still land this. The number that turns
    # "I'm behind" into a decision about whether the deadline is real.
    remaining = target_v - current_v
    weeks_left = max(days_left, 0) / 7
    per_week = (remaining / weeks_left) if weeks_left >= 0.5 else None

    stale_days = None
    if conn is not None:
        row = conn.execute("""SELECT day FROM metric_log WHERE goal_id = ?
                              ORDER BY at DESC LIMIT 1""", (goal["id"],)).fetchone()
        stale_days = _days_ago(row["day"], today) if row else _days_ago(goal["created_at"], today)

    return {
        "progress": progress,
        "progress_pct": round(progress * 100),
        "expected": expected,
        "expected_pct": round(expected * 100),
        "gap_pct": round((progress - expected) * 100),
        "status": status,
        "label": STATUS_LABELS[status],
        "days_left": days_left,
        "total_days": total_days,
        "remaining": remaining,
        "per_week": per_week,
        "stale_days": stale_days,
        "is_stale": stale_days is not None and stale_days >= STALE_DAYS,
    }


def format_value(goal, value):
    """Render a metric the way the goal defines it: $2,400 / 18.5% / 3 features."""
    if value is None:
        return "—"
    unit = (goal["unit"] or "").strip()
    rounded = round(float(value), 1)
    if unit == "$":
        # Cents on an MRR target are noise; keep them only for small amounts.
        return f"${rounded:,.0f}" if abs(rounded) >= 100 else f"${rounded:,.2f}".rstrip("0").rstrip(".")
    text = f"{rounded:,.0f}" if float(rounded).is_integer() else f"{rounded:,.1f}"
    if unit == "%":
        return f"{text}%"
    return f"{text} {unit}".strip()


# ------------------------------------------------------------------- streaks --

def format_delta(goal, value):
    """
    Render a distance or a rate as a magnitude plus a direction.

    Goals that count down (body fat, debt, churn) carry a negative "remaining",
    and "-6.5% to go" reads like a mistake. The sign is information about which
    way the number has to move, so it becomes a word instead of a minus.
    """
    if value is None:
        return "—"
    direction = " down" if value < 0 else ""
    return format_value(goal, abs(value)) + direction


def streak(conn, today=None):
    """Consecutive days with a check-in, ending today or yesterday."""
    today = today or _today()
    days = {row["day"] for row in
            conn.execute("SELECT day FROM checkins ORDER BY day DESC LIMIT 400")}
    if not days:
        return {"days": 0, "checked_in_today": False}

    checked_today = today.isoformat() in days
    cursor = today if checked_today else today - timedelta(days=1)
    count = 0
    while cursor.isoformat() in days:
        count += 1
        cursor -= timedelta(days=1)
    return {"days": count, "checked_in_today": checked_today}


# ------------------------------------------------------------------- signals --

def signals(state):
    """
    What a good chief of staff would put in front of you this morning, ordered by
    how much it should worry you. Deterministic — the model receives this list,
    it does not invent it.

    Levels match the styling in base.html: critical, high, medium, good, info.
    """
    out = []
    today = state["today"]

    for c in state["commitments"]["overdue"]:
        days = (today - _parse(c["due_date"])).days
        out.append({
            "level": "critical" if days > 5 else "high",
            "label": f"{days}d overdue: {c['text']}",
            "detail": ("You set this one." if c["source"] == "you"
                       else "AJ set this one and you accepted it.")
                      + " It is still open. Close it, or say out loud that you're dropping it.",
        })

    for goal in state["goals"]:
        h = goal["health"]
        if goal["status"] != "active":
            continue
        if h["is_stale"]:
            out.append({
                "level": "critical" if h["stale_days"] >= 21 else "high",
                "label": f"\"{goal['title']}\" hasn't moved in {h['stale_days']} days",
                "detail": "No new reading on "
                          f"{goal['metric_name']} since then. A goal you aren't measuring "
                          "is a goal you've stopped working on — decide whether it's alive.",
            })
        if h["status"] == "at_risk" and h["days_left"] >= 0:
            out.append({
                "level": "high",
                "label": f"\"{goal['title']}\" is {abs(h['gap_pct'])}% behind pace",
                "detail": f"{h['days_left']} days left and "
                          f"{format_delta(goal, h['remaining'])} to go — that's "
                          f"{format_delta(goal, h['per_week'])} a week from here."
                          if h["per_week"] is not None else
                          f"{h['days_left']} days left and the gap is still growing.",
            })
        elif h["status"] == "strong" and h["days_left"] >= 0:
            out.append({
                "level": "good",
                "label": f"\"{goal['title']}\" is ahead of pace",
                "detail": f"{h['progress_pct']}% of the distance covered against "
                          f"{h['expected_pct']}% of the calendar. Protect whatever is "
                          "working here before you add anything new.",
            })
        if 0 <= h["days_left"] <= 14 and h["status"] != "strong":
            out.append({
                "level": "high",
                "label": f"\"{goal['title']}\" is due in {h['days_left']} days",
                "detail": "Either the next two weeks look nothing like the last two, "
                          "or this deadline was decoration. Both are decisions.",
            })

    for m in state["milestones_overdue"]:
        out.append({
            "level": "medium",
            "label": f"Milestone past due: {m['title']}",
            "detail": f"Was due {abs(_days_ago(m['target_date'], today))} days ago on "
                      f"\"{m['goal_title']}\".",
        })

    cred = state["credibility"]
    if cred["score"] is not None and cred["score"] < 65 and cred["resolved"] >= 3:
        out.append({
            "level": "critical" if cred["score"] < 40 else "high",
            "label": f"Credibility {cred['score']} — {cred['kept']} kept, {cred['missed']} missed",
            "detail": "This is the only number that predicts the others. Cut what you "
                      "commit to until you're keeping it, then add back.",
        })

    st = state["streak"]
    if not st["checked_in_today"]:
        out.append({
            "level": "medium",
            "label": "No check-in today",
            "detail": "Two minutes. AJ can't coach a day it can't see."
                      + (f" You're on a {st['days']}-day streak." if st["days"] else ""),
        })
    elif st["days"] >= 7:
        out.append({
            "level": "good",
            "label": f"{st['days']}-day check-in streak",
            "detail": "The habit is holding. That's the input; the goals are the output.",
        })

    recent = state["recent_deep_hours"]
    if recent["days"] >= 3 and recent["avg"] < 1.5:
        out.append({
            "level": "high",
            "label": f"{recent['avg']:.1f}h a day of real work, {recent['days']}-day average",
            "detail": "Nothing on this dashboard moves on that. The calendar is the "
                      "constraint, not the plan.",
        })

    order = {"critical": 0, "high": 1, "medium": 2, "good": 3, "info": 4}
    out.sort(key=lambda s: order.get(s["level"], 5))
    return out


def the_one_thing(state):
    """
    The single highest-leverage move available today.

    Deliberately blunt: overdue promises first, then the goal closest to failing.
    A list of twelve priorities is a list of zero priorities, so this returns one.
    """
    overdue = state["commitments"]["overdue"]
    if overdue:
        worst = min(overdue, key=lambda c: c["due_date"])
        return {"text": worst["text"], "why": "It's overdue. Nothing new starts until this closes.",
                "goal_id": worst["goal_id"], "commitment_id": worst["id"]}

    today_due = state["commitments"]["due_today"]
    if today_due:
        c = today_due[0]
        return {"text": c["text"], "why": "Due today. This is the promise on the clock.",
                "goal_id": c["goal_id"], "commitment_id": c["id"]}

    risk = [g for g in state["goals"]
            if g["status"] == "active" and g["health"]["status"] in ("at_risk", "watch")]
    if risk:
        g = sorted(risk, key=lambda g: (g["health"]["days_left"], -g["health"]["stale_days"] or 0))[0]
        nxt = next((m for m in g["milestones"] if not m["done_at"]), None)
        return {
            "text": nxt["title"] if nxt else f"Move {g['metric_name']} on \"{g['title']}\"",
            "why": f"\"{g['title']}\" is {g['health']['label'].lower()} with "
                   f"{g['health']['days_left']} days left. It's the goal most likely to be lost.",
            "goal_id": g["id"], "commitment_id": None,
        }

    upcoming = state["commitments"]["open"]
    if upcoming:
        c = upcoming[0]
        return {"text": c["text"], "why": "Next promise on the board. Get ahead of it.",
                "goal_id": c["goal_id"], "commitment_id": c["id"]}

    return {"text": "Set a commitment for this week", "commitment_id": None, "goal_id": None,
            "why": "There is nothing on the board with a date on it. That's the problem to fix."}


# ------------------------------------------------------------------ snapshot --

def snapshot(conn, today=None):
    """
    Assemble the whole picture once: goals with health, the commitment ledger,
    credibility, streak, signals, and the one thing.

    Every page renders from this, and every prompt is built from this, so what
    AJ says and what you see can never drift apart.
    """
    today = today or _today()
    iso = today.isoformat()

    profile = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()

    goals = []
    for row in conn.execute("""SELECT * FROM goals
                               WHERE status IN ('active','won','missed','paused')
                               ORDER BY (status != 'active'), deadline"""):
        goal = dict(row)
        goal["health"] = goal_health(row, conn, today)
        goal["milestones"] = [dict(m) for m in conn.execute(
            """SELECT * FROM milestones WHERE goal_id = ? ORDER BY done_at IS NOT NULL,
               target_date, sort""", (row["id"],))]
        goal["current_display"] = format_value(row, row["current_value"])
        goal["target_display"] = format_value(row, row["target_value"])
        goals.append(goal)

    by_id = {g["id"]: g for g in goals}

    commitments = {"overdue": [], "due_today": [], "open": [], "recent_resolved": []}
    for row in conn.execute("""SELECT * FROM commitments WHERE status = 'open'
                               ORDER BY due_date"""):
        c = dict(row)
        c["goal_title"] = by_id.get(c["goal_id"], {}).get("title", "")
        if c["due_date"] < iso:
            commitments["overdue"].append(c)
        elif c["due_date"] == iso:
            commitments["due_today"].append(c)
        else:
            commitments["open"].append(c)
    for row in conn.execute("""SELECT * FROM commitments WHERE status != 'open'
                               ORDER BY COALESCE(resolved_at, due_date) DESC LIMIT 12"""):
        c = dict(row)
        c["goal_title"] = by_id.get(c["goal_id"], {}).get("title", "")
        commitments["recent_resolved"].append(c)

    milestones_overdue = []
    for g in goals:
        if g["status"] != "active":
            continue
        for m in g["milestones"]:
            if not m["done_at"] and m["target_date"] < iso:
                milestones_overdue.append({**m, "goal_title": g["title"]})

    checkins = [dict(r) for r in conn.execute(
        "SELECT * FROM checkins ORDER BY day DESC LIMIT 14")]
    window = checkins[:7]
    recent_deep = {
        "days": len(window),
        "avg": (sum(c["deep_hours"] for c in window) / len(window)) if window else 0.0,
        "total": sum(c["deep_hours"] for c in window),
    }

    state = {
        "today": today,
        "profile": dict(profile) if profile else None,
        "goals": goals,
        "commitments": commitments,
        "milestones_overdue": milestones_overdue,
        "credibility": credibility(conn, today),
        "streak": streak(conn, today),
        "checkins": checkins,
        "today_checkin": next((c for c in checkins if c["day"] == iso), None),
        "recent_deep_hours": recent_deep,
    }
    state["signals"] = signals(state)
    state["one_thing"] = the_one_thing(state)
    return state
