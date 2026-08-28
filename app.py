"""
AJ 2.0 — your personal chief executive.

Run locally:
    python3 aj_db.py --demo    # build the database (drop --demo to start empty)
    python3 app.py             # http://localhost:5004

Run deployed (see DEPLOY.md):
    AJ_ENV=production SECRET_KEY=... AJ_PASSCODE=... \
        gunicorn -w 2 -b 0.0.0.0:8000 app:app

Three rules hold everywhere in this file:

  1. **The numbers come from momentum.py, never from the model.** Every page
     renders `momentum.snapshot()`, and every prompt is built from that same
     snapshot. What AJ says and what you see cannot drift apart.

  2. **Model calls are budgeted before they're made.** `ai_allowed()` runs first;
     when the cap is hit the capability still answers, from the offline coach, and
     the page says so. A coach that returns a 500 on the morning you needed it is
     worse than a blunt one.

  3. **Every state change is a POST with a CSRF token**, including the ones that
     only touch your own data. This app is a single user's most candid record;
     it should not be changeable by a link someone sends you.
"""

import hmac
import os
import re
import secrets
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup, escape

import coach_engine
import feeds
import momentum
from aj_db import create_tables, get_connection
from config import Config, validate

app = Flask(__name__)
app.config.from_object(Config)

_problems = validate(Config)
if _problems:
    for p in _problems:
        print("FATAL CONFIG: " + p, file=sys.stderr)
    raise SystemExit(1)

app.secret_key = Config.SECRET_KEY
app.permanent_session_lifetime = timedelta(seconds=Config.PERMANENT_SESSION_LIFETIME)

# Build the schema at import rather than only under __main__.
#
# Gunicorn imports this module; it never runs __main__. Without this, a deploy
# boots against a database with no tables and 500s on every page until someone
# opens a shell — which is a setup step that should not exist. Every statement in
# schema.sql is CREATE ... IF NOT EXISTS, so this is idempotent and cheap, and it
# means a fresh disk on a new Render instance comes up working.
create_tables()

DOMAINS = {
    "business": "Business",
    "money": "Money",
    "craft": "Craft",
    "body": "Body",
    "mind": "Mind",
    "people": "People",
}

CSRF_KEY = "_csrf"

# Per-process, per-hour ceiling on model calls. A stopgap against a runaway loop,
# not a billing system — the authoritative guard is the monthly cap in the ai_spend
# table, which is shared across workers because it lives in the database.
_ai_calls = []


# ------------------------------------------------------------------ plumbing --

def db():
    """One connection per request, closed on teardown."""
    if "conn" not in g.__dict__:
        g.conn = get_connection()
    return g.conn


@app.teardown_appcontext
def close_db(_):
    conn = g.__dict__.pop("conn", None)
    if conn is not None:
        conn.close()


def csrf_token():
    if CSRF_KEY not in session:
        session[CSRF_KEY] = secrets.token_urlsafe(32)
    return session[CSRF_KEY]


def csrf_validate():
    """Constant-time check. Every state change here is a POST, so this covers it all."""
    sent = request.form.get(CSRF_KEY, "")
    held = session.get(CSRF_KEY, "")
    if not held or not sent or not hmac.compare_digest(held, sent):
        abort(400, description="CSRF token missing or invalid. Reload the page and retry.")


def unlocked():
    """With no passcode set, localhost is the auth. With one set, the session is."""
    return not Config.PASSCODE or session.get("unlocked") is True


@app.before_request
def gate():
    if request.method == "POST" and request.endpoint not in (None, "login"):
        csrf_validate()
    if request.endpoint in ("login", "static"):
        return None
    if not unlocked():
        return redirect(url_for("login", next=request.path))
    # Onboarding is the one thing that must happen before anything else works —
    # AJ with no north star is a dashboard with no subject.
    if request.endpoint not in ("onboard", "logout") and not _profile():
        return redirect(url_for("onboard"))
    return None


def _profile():
    return db().execute("SELECT * FROM profile WHERE id = 1").fetchone()


@app.context_processor
def inject_globals():
    return {
        "engine": coach_engine.engine_status(),
        "today": date.today(),
        "csrf_token": csrf_token,
        "domains": DOMAINS,
        "profile": _profile() if unlocked() else None,
        "locked": bool(Config.PASSCODE),
    }


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, title="Request rejected",
                           message=getattr(e, "description", "Malformed request.")), 400


@app.errorhandler(404)
def not_found(_):
    return render_template("error.html", code=404, title="Not found",
                           message="No such record."), 404


# ------------------------------------------------------------------- filters --

@app.template_filter("markdown")
def markdown(text):
    """
    Render the small Markdown subset the coach emits: headings, bullets, bold,
    italics, code. A full Markdown dependency isn't worth it for five constructs,
    and the input is escaped first so a brief can't inject markup.
    """
    if not text:
        return ""
    out, in_list = [], False
    for raw in text.splitlines():
        # str() matters: re.sub on a Markup runs its pieces back through
        # Markup.join, which escapes the tags this function is trying to insert.
        # Escape first, drop to a plain str, then build markup.
        line = str(escape(raw.rstrip()))
        if not line.strip():
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<em>\1</em>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + line[2:] + "</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = min(len(m.group(1)) + 1, 5)
            out.append("<h" + str(level) + ">" + m.group(2) + "</h" + str(level) + ">")
        else:
            out.append("<p>" + line + "</p>")
    if in_list:
        out.append("</ul>")
    return Markup("\n".join(out))


@app.template_filter("nicedate")
def nicedate(value):
    if not value:
        return "—"
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().strftime("%b %-d")


@app.template_filter("when")
def when(value):
    """Due dates read better as distances: 'today', 'in 3d', '5d overdue'."""
    if not value:
        return "—"
    d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    days = (d - date.today()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "1d overdue"
    if days < 0:
        return str(-days) + "d overdue"
    return "in " + str(days) + "d"


@app.template_filter("value")
def value_filter(pair):
    """Usage: (goal, number)|value — formats a metric in its goal's own units."""
    goal, number = pair
    return momentum.format_value(goal, number)


@app.template_filter("delta")
def delta_filter(pair):
    goal, number = pair
    return momentum.format_delta(goal, number)


# -------------------------------------------------------------- model budget --

def _month_key(today=None):
    return (today or date.today()).strftime("%Y-%m")


def spend_cents(conn, month=None):
    row = conn.execute("SELECT COALESCE(SUM(cents), 0) AS c FROM ai_spend WHERE month = ?",
                       (month or _month_key(),)).fetchone()
    return row["c"]


def ai_allowed(conn):
    """
    Whether a model call may be made right now. Returns (allowed, reason).

    Refusing here — rather than at the API — is what lets every capability degrade
    to the offline coach with an explanation instead of failing.
    """
    if not coach_engine.ai_available():
        return False, "No Anthropic credentials set."

    cutoff = datetime.now() - timedelta(hours=1)
    _ai_calls[:] = [t for t in _ai_calls if t > cutoff]
    if len(_ai_calls) >= Config.AI_CALLS_PER_HOUR:
        return False, ("Hourly model-call limit reached (" + str(Config.AI_CALLS_PER_HOUR)
                       + "/hour). This resets on the hour.")

    spent = spend_cents(conn)
    if spent >= Config.AI_MONTHLY_CAP_CENTS:
        return False, ("Monthly cap reached ($" + format(spent / 100, ".2f") + " of $"
                       + format(Config.AI_MONTHLY_CAP_CENTS / 100, ".2f")
                       + "). Raise AI_MONTHLY_CAP_USD or wait for the month to roll.")
    return True, ""


def record_spend(conn, feature):
    """Drain what the last call cost and book it against this month."""
    cents = coach_engine.drain_cost_cents()
    _ai_calls.append(datetime.now())
    if cents:
        conn.execute("INSERT INTO ai_spend (month, cents, feature) VALUES (?,?,?)",
                     (_month_key(), cents, feature))
        conn.commit()
    return cents


def _refresh_goal_current(conn, goal_id):
    """Keep goals.current_value in step with the newest reading."""
    row = conn.execute("""SELECT value FROM metric_log WHERE goal_id = ?
                          ORDER BY at DESC LIMIT 1""", (goal_id,)).fetchone()
    if row:
        conn.execute("UPDATE goals SET current_value = ? WHERE id = ?", (row["value"], goal_id))


def _spark_points(values):
    """
    Metric history as polyline points in a 100x30 viewBox.

    Built here rather than in the template because Jinja arithmetic for this is
    unreadable, and built by hand rather than with a charting dependency because
    it is nine lines and one <svg>.
    """
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = 100 / (len(values) - 1)
    return " ".join(
        format(i * step, ".2f") + "," + format(28 - ((v - lo) / span) * 26, ".2f")
        for i, v in enumerate(values))


def _goal_or_404(conn, goal_id):
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _f(name, default=0.0):
    """Float from the form, tolerant of '$8,000' and '18.5%' — people paste."""
    raw = (request.form.get(name) or "").strip().replace(",", "").replace("$", "").replace("%", "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        abort(400, description="\"" + raw + "\" isn't a number. Metrics need numbers.")


# --------------------------------------------------------------------- gate --

@app.route("/login", methods=["GET", "POST"])
def login():
    if not Config.PASSCODE:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        # Not CSRF-checked: there is no session to protect yet, and constant-time
        # comparison is the control that matters on this form.
        if hmac.compare_digest(request.form.get("passcode", ""), Config.PASSCODE):
            session.permanent = True
            session["unlocked"] = True
            nxt = request.form.get("next") or url_for("dashboard")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
        flash("Wrong passcode.")
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------- onboarding --

@app.route("/onboard", methods=["GET", "POST"])
def onboard():
    """
    The five questions AJ needs before it can say anything worth hearing.

    Deliberately not skippable and deliberately short. A coach that starts working
    before it knows what you're chasing is a chatbot with a dashboard.
    """
    conn = db()
    existing = _profile()
    if request.method == "POST":
        fields = (
            (request.form.get("name") or "").strip()[:80],
            (request.form.get("north_star") or "").strip()[:2000],
            (request.form.get("identity") or "").strip()[:2000],
            (request.form.get("values_text") or "").strip()[:2000],
            (request.form.get("stakes") or "").strip()[:2000],
            max(1, min(int(_f("horizon_years", 3)), 30)),
            max(0, min(int(_f("intensity", 90)), 100)),
        )
        if not fields[0] or not fields[1]:
            flash("AJ needs your name and the north star. The rest can wait.")
            return render_template("onboard.html", p=existing, form=request.form)
        if existing:
            conn.execute("""UPDATE profile SET name=?, north_star=?, identity=?,
                            values_text=?, stakes=?, horizon_years=?, intensity=?,
                            onboarded_at=COALESCE(onboarded_at, datetime('now'))
                            WHERE id=1""", fields)
        else:
            conn.execute("""INSERT INTO profile (id, name, north_star, identity,
                            values_text, stakes, horizon_years, intensity, onboarded_at)
                            VALUES (1,?,?,?,?,?,?,?,datetime('now'))""", fields)
        conn.commit()
        flash("AJ has what he needs. Now put a goal on the board.")
        return redirect(url_for("goal_new") if not conn.execute(
            "SELECT 1 FROM goals LIMIT 1").fetchone() else url_for("dashboard"))
    return render_template("onboard.html", p=existing, form={})


# ---------------------------------------------------------------- dashboard --

def _todays_brief(conn, state, force=False):
    """
    Today's brief, generated once and stored.

    Stored rather than regenerated per page load for two reasons: a brief that
    changes every time you refresh is one you learn to distrust, and each
    regeneration is a model call you didn't ask for.
    """
    today = state["today"].isoformat()
    row = conn.execute("SELECT * FROM briefs WHERE kind='daily' AND day=?", (today,)).fetchone()
    if row and not force:
        return row["body"], row["engine"]

    allowed, reason = ai_allowed(conn)
    body, engine = coach_engine.daily_brief(state, allow_model=allowed)
    if allowed:
        record_spend(conn, "daily_brief")
    conn.execute("""INSERT INTO briefs (kind, day, body, engine) VALUES ('daily',?,?,?)
                    ON CONFLICT(kind, day) DO UPDATE SET body=excluded.body,
                    engine=excluded.engine, created_at=datetime('now')""",
                 (today, body, engine))
    conn.commit()
    return body, engine


@app.route("/")
def dashboard():
    conn = db()
    state = momentum.snapshot(conn)
    brief, brief_engine = _todays_brief(conn, state)
    return render_template("dashboard.html", s=state, brief=brief,
                           brief_engine=brief_engine, spent=spend_cents(conn),
                           cap=Config.AI_MONTHLY_CAP_CENTS)


@app.route("/brief/refresh", methods=["POST"])
def brief_refresh():
    conn = db()
    _todays_brief(conn, momentum.snapshot(conn), force=True)
    return redirect(url_for("dashboard"))


# -------------------------------------------------------------------- goals --

@app.route("/goals")
def goals():
    conn = db()
    state = momentum.snapshot(conn)
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM goals WHERE status IN ('won','dropped','missed') ORDER BY closed_at DESC")]
    return render_template("goals.html", s=state, closed=closed)


@app.route("/goals/new", methods=["GET", "POST"])
def goal_new():
    """
    Two-step by design: every goal gets pressure-tested before it reaches the board.

    You can override the verdict and add it anyway — it's your life — but you
    cannot add one without having read what's wrong with it first.
    """
    conn = db()
    draft = {
        "title": (request.form.get("title") or "").strip()[:200],
        "why": (request.form.get("why") or "").strip()[:2000],
        "domain": request.form.get("domain") if request.form.get("domain") in DOMAINS else "business",
        "metric_name": (request.form.get("metric_name") or "").strip()[:80],
        "unit": (request.form.get("unit") or "").strip()[:12],
        "start_value": _f("start_value"),
        "target_value": _f("target_value"),
        "deadline": (request.form.get("deadline") or "").strip()[:10],
        "stakes": (request.form.get("stakes") or "").strip()[:2000],
    }

    if request.method == "POST" and request.form.get("action") == "test":
        if not draft["title"]:
            flash("Give it a name first, even a bad one.")
            return render_template("goal_new.html", draft=draft, test=None)
        state = momentum.snapshot(conn)
        allowed, reason = ai_allowed(conn)
        test, engine = coach_engine.pressure_test(draft, state, allow_model=allowed)
        if allowed:
            record_spend(conn, "pressure_test")
        return render_template("goal_new.html", draft=draft, test=test,
                               test_engine=engine, budget_note="" if allowed else reason)

    if request.method == "POST" and request.form.get("action") == "create":
        if not draft["title"] or not draft["metric_name"] or not draft["deadline"]:
            flash("A goal needs a title, a metric, and a deadline. That's the whole point.")
            return render_template("goal_new.html", draft=draft, test=None)
        cur = conn.execute("""
            INSERT INTO goals (title, why, domain, metric_name, unit, start_value,
                               target_value, current_value, deadline, stakes)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (draft["title"], draft["why"], draft["domain"], draft["metric_name"],
             draft["unit"], draft["start_value"], draft["target_value"],
             draft["start_value"], draft["deadline"], draft["stakes"]))
        goal_id = cur.lastrowid
        # The starting value is a reading like any other, so the trend line has a
        # first point and "days since measured" starts counting from today.
        conn.execute("INSERT INTO metric_log (goal_id, value, note) VALUES (?,?,?)",
                     (goal_id, draft["start_value"], "starting line"))
        conn.commit()
        flash("On the board. Now get a plan behind it.")
        return redirect(url_for("goal_detail", goal_id=goal_id))

    return render_template("goal_new.html", draft=draft, test=None)


@app.route("/goals/<int:goal_id>")
def goal_detail(goal_id):
    conn = db()
    row = _goal_or_404(conn, goal_id)
    state = momentum.snapshot(conn)
    goal = next((g for g in state["goals"] if g["id"] == goal_id), None)
    if goal is None:                        # dropped goals aren't in the snapshot
        goal = dict(row)
        goal["health"] = momentum.goal_health(row, conn)
        goal["milestones"] = [dict(m) for m in conn.execute(
            "SELECT * FROM milestones WHERE goal_id=? ORDER BY target_date", (goal_id,))]
    readings = [dict(r) for r in conn.execute(
        "SELECT * FROM metric_log WHERE goal_id=? ORDER BY at DESC LIMIT 40", (goal_id,))]
    commitments = [dict(c) for c in conn.execute(
        """SELECT * FROM commitments WHERE goal_id=?
           ORDER BY status != 'open', due_date DESC LIMIT 30""", (goal_id,))]
    spark = list(reversed([r["value"] for r in readings]))[-24:]
    return render_template("goal.html", g=goal, s=state, readings=readings,
                           commitments=commitments, spark=spark,
                           spark_points=_spark_points(spark))


@app.route("/goals/<int:goal_id>/metric", methods=["POST"])
def goal_metric(goal_id):
    conn = db()
    _goal_or_404(conn, goal_id)
    conn.execute("INSERT INTO metric_log (goal_id, value, note) VALUES (?,?,?)",
                 (goal_id, _f("value"), (request.form.get("note") or "").strip()[:300]))
    _refresh_goal_current(conn, goal_id)
    conn.commit()
    flash("Logged. That's the only kind of progress that counts here.")
    return redirect(url_for("goal_detail", goal_id=goal_id))


@app.route("/goals/<int:goal_id>/plan", methods=["POST"])
def goal_plan(goal_id):
    """Have AJ decompose the goal, and write the result straight onto the board."""
    conn = db()
    _goal_or_404(conn, goal_id)
    state = momentum.snapshot(conn)
    goal = next((g for g in state["goals"] if g["id"] == goal_id), None) or dict(
        _goal_or_404(conn, goal_id))
    if "health" not in goal:
        goal["health"] = momentum.goal_health(_goal_or_404(conn, goal_id), conn)
        goal["milestones"] = []

    allowed, reason = ai_allowed(conn)
    plan, engine = coach_engine.build_plan(goal, state, allow_model=allowed)
    if allowed:
        record_spend(conn, "build_plan")

    existing = {m["title"].lower() for m in goal.get("milestones", [])}
    added = 0
    for i, m in enumerate(plan["milestones"]):
        if m["title"].lower() in existing:
            continue
        conn.execute("""INSERT INTO milestones (goal_id, title, target_date, sort)
                        VALUES (?,?,?,?)""",
                     (goal_id, m["title"][:200], m["target_date"][:10], 100 + i))
        added += 1
    for c in plan["commitments"]:
        conn.execute("""INSERT INTO commitments (goal_id, text, due_date, source)
                        VALUES (?,?,?,'aj')""",
                     (goal_id, c["text"][:300], c["due_date"][:10]))
    # The bet and the kill criteria are the part of a plan people forget, so they
    # go somewhere they'll be re-read: the war room transcript.
    conn.execute("""INSERT INTO messages (role, text, kind, engine) VALUES ('aj',?,?,?)""",
                 ("Plan for \"" + goal["title"] + "\".\n\n**The bet:** " + plan["bet"]
                  + "\n\n**Kill criteria:** " + plan["kill_criteria"], "chat", engine))
    conn.commit()
    flash(str(added) + " milestones and " + str(len(plan["commitments"]))
          + " commitments added. The bet is in the war room.")
    return redirect(url_for("goal_detail", goal_id=goal_id))


@app.route("/goals/<int:goal_id>/status", methods=["POST"])
def goal_status(goal_id):
    conn = db()
    _goal_or_404(conn, goal_id)
    status = request.form.get("status")
    if status not in ("active", "won", "paused", "dropped", "missed"):
        abort(400, description="Unknown status.")
    conn.execute("""UPDATE goals SET status=?,
                    closed_at = CASE WHEN ? IN ('won','dropped','missed')
                                THEN datetime('now') ELSE NULL END
                    WHERE id=?""", (status, status, goal_id))
    conn.commit()
    flash({"won": "Won. Take the win, then raise the bar.",
           "dropped": "Dropped. Better than carrying a corpse.",
           "paused": "Paused — and a paused goal is a goal you're not doing.",
           "missed": "Missed. It goes in the record, and the record is what makes the wins real.",
           "active": "Back on the board."}[status])
    return redirect(url_for("goal_detail", goal_id=goal_id))


@app.route("/milestones/<int:ms_id>/toggle", methods=["POST"])
def milestone_toggle(ms_id):
    conn = db()
    row = conn.execute("SELECT * FROM milestones WHERE id=?", (ms_id,)).fetchone()
    if row is None:
        abort(404)
    conn.execute("UPDATE milestones SET done_at = CASE WHEN done_at IS NULL "
                 "THEN datetime('now') ELSE NULL END WHERE id=?", (ms_id,))
    conn.commit()
    return redirect(url_for("goal_detail", goal_id=row["goal_id"]))


@app.route("/milestones/add", methods=["POST"])
def milestone_add():
    conn = db()
    goal_id = int(_f("goal_id"))
    _goal_or_404(conn, goal_id)
    title = (request.form.get("title") or "").strip()[:200]
    target = (request.form.get("target_date") or "").strip()[:10]
    if title and target:
        conn.execute("INSERT INTO milestones (goal_id, title, target_date) VALUES (?,?,?)",
                     (goal_id, title, target))
        conn.commit()
    return redirect(url_for("goal_detail", goal_id=goal_id))


# -------------------------------------------------------------- commitments --

@app.route("/commitments/add", methods=["POST"])
def commitment_add():
    conn = db()
    text = (request.form.get("text") or "").strip()[:300]
    due = (request.form.get("due_date") or "").strip()[:10]
    goal_id = request.form.get("goal_id") or None
    if not text or not due:
        flash("A commitment needs words and a date. Without the date it's a wish.")
    else:
        conn.execute("""INSERT INTO commitments (goal_id, text, due_date, source)
                        VALUES (?,?,?,'you')""",
                     (int(goal_id) if goal_id else None, text, due))
        conn.commit()
    return redirect(request.form.get("back") or url_for("dashboard"))


@app.route("/commitments/<int:cid>/resolve", methods=["POST"])
def commitment_resolve(cid):
    """
    Kept, missed, or dropped — and nothing else. There is no delete.

    The whole credibility number depends on this ledger being complete, so the
    one thing you cannot do to an inconvenient commitment is make it disappear.
    """
    conn = db()
    row = conn.execute("SELECT * FROM commitments WHERE id=?", (cid,)).fetchone()
    if row is None:
        abort(404)
    status = request.form.get("status")
    if status not in ("kept", "missed", "dropped", "open"):
        abort(400, description="Unknown resolution.")
    conn.execute("""UPDATE commitments SET status=?, excuse=?,
                    resolved_at = CASE WHEN ?='open' THEN NULL ELSE datetime('now') END
                    WHERE id=?""",
                 (status, (request.form.get("excuse") or "").strip()[:500], status, cid))
    conn.commit()
    if status == "kept":
        flash("Kept. That's what the number is made of.")
    elif status == "missed":
        flash("Recorded as missed. It stays in the ledger — that's what makes the kept ones count.")
    return redirect(request.form.get("back") or url_for("dashboard"))


# ----------------------------------------------------------------- check-in --

@app.route("/checkin", methods=["GET", "POST"])
def checkin():
    conn = db()
    today = date.today().isoformat()
    if request.method == "POST":
        row = (today,
               max(1, min(int(_f("energy", 3)), 5)),
               max(0.0, min(_f("deep_hours"), 24.0)),
               (request.form.get("log") or "").strip()[:4000],
               (request.form.get("blockers") or "").strip()[:1000])
        conn.execute("""INSERT INTO checkins (day, energy, deep_hours, log, blockers)
                        VALUES (?,?,?,?,?)
                        ON CONFLICT(day) DO UPDATE SET energy=excluded.energy,
                        deep_hours=excluded.deep_hours, log=excluded.log,
                        blockers=excluded.blockers""", row)
        conn.commit()

        state = momentum.snapshot(conn)
        entry = conn.execute("SELECT * FROM checkins WHERE day=?", (today,)).fetchone()
        allowed, _reason = ai_allowed(conn)
        result, engine = coach_engine.debrief(dict(entry), state, allow_model=allowed)
        if allowed:
            record_spend(conn, "debrief")
        conn.execute("UPDATE checkins SET grade=?, debrief=?, engine=? WHERE day=?",
                     (result["grade"], result["headline"] + "\n\n" + result["body"],
                      engine, today))
        conn.commit()
        return redirect(url_for("checkin"))

    state = momentum.snapshot(conn)
    entry = state["today_checkin"]
    history = [c for c in state["checkins"] if c["day"] != today][:10]
    return render_template("checkin.html", s=state, entry=entry, history=history)


# ----------------------------------------------------------------- war room --

@app.route("/warroom", methods=["GET", "POST"])
def warroom():
    conn = db()
    if request.method == "POST":
        message = (request.form.get("message") or "").strip()[:4000]
        if not message:
            return redirect(url_for("warroom"))
        conn.execute("INSERT INTO messages (role, text) VALUES ('you', ?)", (message,))
        conn.commit()

        state = momentum.snapshot(conn)
        history = [dict(r) for r in conn.execute(
            """SELECT role, text FROM messages WHERE id < (SELECT MAX(id) FROM messages)
               ORDER BY id DESC LIMIT ?""", (Config.CHAT_HISTORY_TURNS,))][::-1]
        allowed, _reason = ai_allowed(conn)
        reply, engine = coach_engine.chat(message, state, history, allow_model=allowed)
        if allowed:
            record_spend(conn, "chat")
        conn.execute("INSERT INTO messages (role, text, engine) VALUES ('aj',?,?)",
                     (reply, engine))
        conn.commit()
        return redirect(url_for("warroom"))

    state = momentum.snapshot(conn)
    messages = [dict(r) for r in conn.execute(
        "SELECT * FROM messages ORDER BY id DESC LIMIT 60")][::-1]
    return render_template("warroom.html", s=state, messages=messages)


# ------------------------------------------------------------------- review --

def _week_ending(today=None):
    """Weeks end Sunday, so a Monday review covers the week you just finished."""
    today = today or date.today()
    return (today + timedelta(days=(6 - today.weekday()) % 7)).isoformat()


@app.route("/review", methods=["GET", "POST"])
def review():
    conn = db()
    state = momentum.snapshot(conn)
    week = _week_ending(state["today"])
    row = conn.execute("SELECT * FROM briefs WHERE kind='weekly' AND day=?", (week,)).fetchone()

    if request.method == "POST" or row is None:
        allowed, reason = ai_allowed(conn)
        body, engine = coach_engine.weekly_review(state, allow_model=allowed)
        if allowed:
            record_spend(conn, "weekly_review")
        conn.execute("""INSERT INTO briefs (kind, day, body, engine) VALUES ('weekly',?,?,?)
                        ON CONFLICT(kind, day) DO UPDATE SET body=excluded.body,
                        engine=excluded.engine, created_at=datetime('now')""",
                     (week, body, engine))
        conn.commit()
        if request.method == "POST":
            return redirect(url_for("review"))
        row = conn.execute("SELECT * FROM briefs WHERE kind='weekly' AND day=?",
                           (week,)).fetchone()

    past = [dict(r) for r in conn.execute(
        "SELECT * FROM briefs WHERE kind='weekly' AND day != ? ORDER BY day DESC LIMIT 8",
        (week,))]
    return render_template("review.html", s=state, review=dict(row), past=past)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Intensity, and an honest accounting of what the coach costs to run."""
    conn = db()
    if request.method == "POST":
        conn.execute("UPDATE profile SET intensity=?, north_star=?, identity=?, "
                     "values_text=?, stakes=?, horizon_years=? WHERE id=1",
                     (max(0, min(int(_f("intensity", 90)), 100)),
                      (request.form.get("north_star") or "").strip()[:2000],
                      (request.form.get("identity") or "").strip()[:2000],
                      (request.form.get("values_text") or "").strip()[:2000],
                      (request.form.get("stakes") or "").strip()[:2000],
                      max(1, min(int(_f("horizon_years", 3)), 30))))
        conn.commit()
        flash("Updated. AJ reads this before every word he says to you.")
        return redirect(url_for("settings"))

    months = [dict(r) for r in conn.execute(
        """SELECT month, ROUND(SUM(cents)) AS cents, COUNT(*) AS calls FROM ai_spend
           GROUP BY month ORDER BY month DESC LIMIT 6""")]
    features = [dict(r) for r in conn.execute(
        """SELECT feature, ROUND(SUM(cents)) AS cents, COUNT(*) AS calls FROM ai_spend
           WHERE month = ? GROUP BY feature ORDER BY SUM(cents) DESC""", (_month_key(),))]
    return render_template("settings.html", months=months, features=features,
                           spent=spend_cents(conn), cap=Config.AI_MONTHLY_CAP_CENTS)



# ---------------------------------------------------------------------- life --
#
# Everything that isn't a goal but decides whether goals happen: her schedule,
# school, and the money. Feeds are subscriptions with a URL — see feeds.py for
# why that beats an integration.

FEED_KINDS = {"ics": "Calendar subscription (.ics)", "csv": "Bank export (CSV)"}


@app.route("/life")
def life():
    conn = db()
    state = momentum.snapshot(conn)
    sources = [dict(r) for r in conn.execute(
        "SELECT * FROM sources ORDER BY kind, name")]
    for source in sources:
        source["event_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE source_id = ? AND day >= ?",
            (source["id"], state["today"].isoformat())).fetchone()["c"]
        source["tx_count"] = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE source_id = ?",
            (source["id"],)).fetchone()["c"]
    recent_tx = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions ORDER BY day DESC, id DESC LIMIT 25")]
    return render_template("life.html", s=state, sources=sources, recent_tx=recent_tx,
                           kinds=FEED_KINDS, bank_note=feeds.LIVE_BANK_NOTE,
                           mail_ready=Config.mail_configured(),
                           mail_user=Config.IMAP_USER)


@app.route("/life/sources/add", methods=["POST"])
def source_add():
    conn = db()
    kind = request.form.get("kind")
    if kind not in FEED_KINDS:
        abort(400, description="Unknown feed type.")
    name = (request.form.get("name") or "").strip()[:120]
    url = feeds.normalize_feed_url(request.form.get("url") or "")[:1000]
    person = (request.form.get("person") or "").strip()[:60]
    if not name:
        flash("Give the feed a name you'll recognize on a Tuesday.")
        return redirect(url_for("life"))
    if kind == "ics" and not url:
        flash("A calendar feed needs its subscription URL. In BAND: Calendar → "
              "Manage Events → Export Band Calendars → copy the address.")
        return redirect(url_for("life"))

    cur = conn.execute("""INSERT INTO sources (kind, name, url, person)
                          VALUES (?,?,?,?)""", (kind, name, url, person))
    conn.commit()
    if kind == "ics":
        return _sync_one(conn, cur.lastrowid)
    flash("Added. Now import an export into it.")
    return redirect(url_for("life"))


def _sync_one(conn, source_id):
    """Re-read one feed and report honestly. Never raises into a page."""
    source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source is None:
        abort(404)
    if source["kind"] != "ics":
        flash("Only calendar feeds sync on their own. A bank export is a file you import.")
        return redirect(url_for("life"))
    try:
        added, updated = feeds.sync_ics(conn, source, tz=Config.TZ or None)
        flash(source["name"] + ": " + str(added) + " new, " + str(updated) + " changed.")
    except Exception as exc:
        flash("Couldn't read " + source["name"] + " — " + str(exc)[:200])
    return redirect(url_for("life"))


@app.route("/life/sources/<int:source_id>/sync", methods=["POST"])
def source_sync(source_id):
    return _sync_one(db(), source_id)


@app.route("/life/sync", methods=["POST"])
def sync_all():
    conn = db()
    rows = conn.execute("SELECT * FROM sources WHERE kind='ics' AND enabled=1").fetchall()
    added = updated = failed = 0
    for source in rows:
        try:
            a, u = feeds.sync_ics(conn, source, tz=Config.TZ or None)
            added, updated = added + a, updated + u
        except Exception:
            failed += 1                 # the reason is already on the source row
    flash(str(added) + " new events, " + str(updated) + " changed"
          + (", " + str(failed) + " feed(s) failed — see below." if failed else "."))
    return redirect(url_for("life"))


@app.route("/life/sources/<int:source_id>/toggle", methods=["POST"])
def source_toggle(source_id):
    conn = db()
    conn.execute("UPDATE sources SET enabled = 1 - enabled WHERE id = ?", (source_id,))
    conn.commit()
    return redirect(url_for("life"))


@app.route("/life/sources/<int:source_id>/delete", methods=["POST"])
def source_delete(source_id):
    conn = db()
    row = conn.execute("SELECT name FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        abort(404)
    # Events cascade with the feed; transactions deliberately do not (ON DELETE
    # SET NULL), because your money history should outlive the CSV you got it from.
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    conn.commit()
    flash("Removed " + row["name"] + ". Its calendar entries went with it.")
    return redirect(url_for("life"))


@app.route("/life/bank/import", methods=["POST"])
def bank_import():
    """Take a bank CSV by upload or paste, and say exactly what came in."""
    conn = db()
    source_id = request.form.get("source_id")
    text = ""
    upload = request.files.get("file")
    if upload and upload.filename:
        text = upload.read(Config.MAX_CONTENT_LENGTH).decode("utf-8", "replace")
    if not text.strip():
        text = request.form.get("pasted") or ""
    if not text.strip():
        flash("Nothing to import — pick a file or paste the rows.")
        return redirect(url_for("life"))

    if source_id:
        source_id = int(source_id)
    else:
        cur = conn.execute("""INSERT INTO sources (kind, name, last_synced_at)
                              VALUES ('csv', 'Bank export', datetime('now'))""")
        source_id = cur.lastrowid

    rows, problems = feeds.read_bank_csv(text)
    if not rows:
        flash("Couldn't read that file. " + (problems[0] if problems else ""))
        return redirect(url_for("life"))

    added = feeds.import_transactions(conn, source_id, rows)
    conn.execute("UPDATE sources SET last_synced_at = datetime('now') WHERE id = ?",
                 (source_id,))
    conn.commit()
    note = (str(added) + " new transactions from " + str(len(rows)) + " rows"
            + " (" + str(len(rows) - added) + " already imported)" if added != len(rows)
            else str(added) + " new transactions")
    if problems:
        note += " — " + str(len(problems)) + " row(s) skipped: " + problems[0]
    flash(note)
    return redirect(url_for("life"))


@app.route("/events/<int:event_id>/protect", methods=["POST"])
def event_protect(event_id):
    """Mark an event immovable. AJ plans around protected time, never through it."""
    conn = db()
    row = conn.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        abort(404)
    conn.execute("UPDATE events SET protected = 1 - protected WHERE id = ?", (event_id,))
    conn.commit()
    return redirect(url_for("life"))



@app.route("/life/mail/sync", methods=["POST"])
def mail_sync():
    """
    Read the mailbox and re-triage. Credentials come from the environment.

    The mailbox is opened read-only and only headers plus a short snippet are
    stored — see feeds.sync_mail. Nothing here can mark your mail read.
    """
    conn = db()
    if not Config.mail_configured():
        flash("No mailbox configured. Set AJ_IMAP_HOST, AJ_IMAP_USER, and "
              "AJ_IMAP_PASSWORD (an app password), then restart.")
        return redirect(url_for("life"))

    source = conn.execute("SELECT * FROM sources WHERE kind = 'imap'").fetchone()
    if source is None:
        cur = conn.execute("""INSERT INTO sources (kind, name) VALUES ('imap', ?)""",
                           ("Mail — " + Config.IMAP_USER,))
        conn.commit()
        source = conn.execute("SELECT * FROM sources WHERE id = ?",
                              (cur.lastrowid,)).fetchone()
    try:
        added, flagged = feeds.sync_mail(
            conn, source, Config.IMAP_HOST, Config.IMAP_USER, Config.IMAP_PASSWORD,
            Config.IMAP_FOLDER, me=Config.IMAP_USER)
        flash(str(added) + " new messages read, " + str(flagged) + " waiting on you.")
    except Exception as exc:
        flash("Mail sync failed: " + str(exc)[:200])
    return redirect(url_for("life"))


@app.route("/inbox/<int:item_id>/handled", methods=["POST"])
def inbox_handled(item_id):
    conn = db()
    if conn.execute("SELECT 1 FROM inbox WHERE id = ?", (item_id,)).fetchone() is None:
        abort(404)
    conn.execute("UPDATE inbox SET handled = 1 - handled WHERE id = ?", (item_id,))
    conn.commit()
    return redirect(request.form.get("back") or url_for("life"))


if __name__ == "__main__":
    # The schema is already built at import. Nothing to do here but serve.
    app.run(debug=Config.DEBUG, port=int(os.environ.get("PORT", 5004)))
