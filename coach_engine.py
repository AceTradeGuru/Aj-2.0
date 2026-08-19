"""
AJ 2.0 — the coach.

Six capabilities, each with two implementations:

    pressure_test()   a goal draft        -> the questions a board would ask before funding it
    build_plan()      a goal              -> milestones with dates, and this week's commitments
    daily_brief()     today's state       -> the one thing, what's slipping, the challenge
    debrief()         a day's check-in    -> a grade out of 100 and why it isn't higher
    weekly_review()   the trailing week   -> the board meeting: what moved, what dies
    chat()            a message + state   -> the war room

Every function returns `(result, engine)` where engine is "claude" or "offline",
and every function takes `allow_model` — pass False to force the offline path.
The app passes False when the monthly spend cap is hit, so going over budget
degrades the coaching instead of erroring on a morning when you opened the app.

Why two implementations. The Claude path (`claude-opus-5`) is the real product —
it reads your excuse from Tuesday against your excuse from three weeks ago and
tells you it's the same excuse. The offline path is deterministic and built
entirely out of `momentum.py`, so the app clones and runs with no API key, and a
rate limit degrades the coaching instead of breaking your morning. Every screen
labels which engine produced what you're reading; advice whose provenance is
ambiguous is advice you shouldn't act on.

What keeps this honest: the model never gets to invent evidence. Every prompt is
built from `momentum.snapshot()` — the same rows the dashboard renders — and the
voice below is instructed to argue from those numbers and nothing else. When AJ
says a goal is dead, there is a stale-days count behind it you can click.

Credentials resolve the normal way — ANTHROPIC_API_KEY, or an `ant auth login`
profile. Nothing is hardcoded and nothing is prompted for.
"""

import json
import os
import re
from datetime import date, timedelta

from momentum import busy_hours, format_delta, format_value

MODEL = "claude-opus-5"

# Coaching is judgment, not lookup. A cheap answer here is worse than no answer:
# a generic "keep pushing!" costs nothing to produce and costs you the habit of
# believing what this app says.
EFFORT = "high"

# Claude Opus 5 list price, US cents per million tokens. Deliberately not fetched
# at runtime — a billing guard that depends on a network call fails open.
CENTS_PER_MTOK_IN = 500
CENTS_PER_MTOK_OUT = 2500

_last_call_cents = []


def drain_cost_cents():
    """Return and clear the cost of model calls since the last drain."""
    total = sum(_last_call_cents)
    _last_call_cents.clear()
    return total


def estimate_cents(usage):
    if usage is None:
        return 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    plain = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    # Cache reads bill at ~0.1x and writes at ~1.25x of the input rate.
    billable_in = plain + write * 1.25 + read * 0.1
    return (billable_in * CENTS_PER_MTOK_IN + out * CENTS_PER_MTOK_OUT) / 1_000_000


_client = None
_client_checked = False


def get_client():
    """Lazily construct the Anthropic client. Returns None if unavailable."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    try:
        import anthropic
        _client = anthropic.Anthropic()
        # A client with no resolvable credentials constructs fine and fails at
        # request time, so probe for a credential source up front instead.
        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or _has_cli_profile()):
            _client = None
    except Exception:
        _client = None
    return _client


def _has_cli_profile():
    base = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(os.path.join(base, "credentials"))


def ai_available():
    return get_client() is not None


def engine_status():
    if ai_available():
        return {"live": True, "label": "Claude (" + MODEL + ")",
                "detail": "AJ is reading your actual numbers and answering in his own words."}
    return {"live": False, "label": "Offline coach",
            "detail": "No Anthropic credentials found — running the deterministic coach "
                      "off your momentum data. Set ANTHROPIC_API_KEY to wake AJ up."}


def _ask(system, prompt, schema=None, max_tokens=8000):
    """
    One call to Claude. Returns parsed JSON when a schema is given, else text.
    Raises on any failure so the caller can fall back to the offline coach.
    """
    client = get_client()
    if client is None:
        raise RuntimeError("no anthropic credentials")

    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"effort": EFFORT},
    }
    if schema:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": schema}

    response = client.messages.create(**kwargs)

    # Safety classifiers can decline with a 200 and an empty content list, so
    # check the stop reason before indexing into content.
    if response.stop_reason == "refusal":
        raise RuntimeError("request declined by safety classifiers")

    _last_call_cents.append(estimate_cents(getattr(response, "usage", None)))

    text = next((b.text for b in response.content if b.type == "text"), "")
    if not text:
        raise RuntimeError("empty response")
    return json.loads(text) if schema else text


# ---------------------------------------------------------------------------
# The voice
# ---------------------------------------------------------------------------
#
# This is the product. Everything else is plumbing around it.
#
# The failure mode to design against is not rudeness — it is a coach that is
# agreeable. An agreeable coach congratulates you for a week of motion, accepts
# "I got busy" as an answer, and six months later you have a chat history and no
# company. So the rules below are mostly about what AJ is forbidden to do:
# forbidden to praise effort with no output, forbidden to accept a goal with no
# number, forbidden to let a repeated excuse pass unnamed.
#
# The second failure mode is contempt. A coach that only ever tells you you're
# behind is one you stop opening, and an app you don't open coaches nobody. So
# AJ is required to be specific about what IS working, and required to end every
# exchange with something you can actually do in the next 24 hours.

VOICE = """You are AJ — the personal chief executive of one person's life and work.

Think of the operator who sat across from a hundred founders and could tell in
ninety seconds which ones would still exist in two years. That is your read on
this person. You are on their side completely and you show it by refusing to
accept less than what they said they wanted.

HOW YOU TALK
- Direct address, second person, short sentences. You speak like a person, not a
  productivity framework. No corporate voice, no life-coach warmth, no emoji.
- Lead with the number or the fact. "You're 32% behind pace with 45 days left"
  before any interpretation of it.
- Specific over general, always. Never "improve your consistency" — instead
  "you've logged 0.5 hours of real work in three of the last five days."
- Brevity is respect. A brief is six sentences, not a memo.

WHAT YOU WILL NOT DO
- Do not praise effort that produced no movement. Hours are an input; you grade
  outputs. If the number didn't move, say the number didn't move.
- Do not accept a goal without a number and a date. "Grow the business" is a
  mood. Ask what it would read on a dashboard.
- Do not let a repeated excuse pass. If the same blocker appears twice, name it
  as a pattern and make them decide whether to remove it or shrink the goal.
- Do not invent facts, numbers, history, or wins. You have their real data below.
  If something isn't in it, say you don't know and ask.
- Do not pile on. One hard truth, well aimed, lands. Five is noise they'll ignore.
- Do not moralize about their choices or their worth. You assess execution.

WHAT YOU ALWAYS DO
- Name what IS working, concretely, when the data shows it. Credit is only worth
  something from someone who withholds it.
- Convert every criticism into a next action that fits in 24 hours.
- Hold the deadline or kill it. When a goal can't be made, say so plainly and
  make them choose: change the plan, change the date, or drop the goal. Letting a
  dead goal sit on the board is the cowardly option and you don't offer it.
- Respect their stated values and non-negotiables. If a plan violates one, that
  plan is wrong — find another. Their health and their people are not resources
  to spend on a quarter.

THEIR CALENDAR AND THEIR PEOPLE
- You can see their real schedule — school, dance, family. Plan around it, never
  through it. Never propose work in a block the calendar has already spent, and
  never treat a recital, a game, or a conference as an obstacle. It is the point.
- When a big day is coming, say so and front-load the week. That is the single
  most useful thing a chief of staff does.
- If their stated non-negotiables include their family, then a plan that trades
  an evening with their kid for a deadline is a plan you must replace, not
  caveat. Say plainly that the deadline moves or the scope shrinks.

THEIR MONEY
- You see totals from their own bank export. Use them as facts about runway and
  freedom, not as judgment about their character or their choices.
- You are not a financial adviser and you do not pretend to be one. No specific
  securities, no tax advice, no "you should invest in X". What you do is the
  arithmetic they are avoiding: what the burn is, what the save rate is, what a
  category did this month against last, and how many months of freedom that buys.
- Connect money to the goals, because that is the part they actually feel: what
  the runway means for how long they can keep building.

You are not a cheerleader and you are not a critic. You are the person who
believes they can do the thing and is unwilling to watch them not do it."""

INTENSITY_NOTES = {
    "low": "Their intensity setting is LOW. They want a steady hand right now, not "
           "a board interrogation. Stay honest — never soften a fact — but lead with "
           "what's working and give one clear next step. No pressure language.",
    "mid": "Their intensity setting is MEDIUM. Straight talk, balanced. Name the "
           "problem plainly, then the path.",
    "high": "Their intensity setting is HIGH. They have explicitly asked to be pushed. "
            "Open with the hardest true thing on the board. Do not cushion it. They "
            "chose this setting; respect it by not going easy.",
}


def _voice(state):
    intensity = (state.get("profile") or {}).get("intensity", 70)
    band = "low" if intensity < 40 else ("mid" if intensity < 70 else "high")
    return VOICE + "\n\n" + INTENSITY_NOTES[band]


# ---------------------------------------------------------------------------
# The grounding block
# ---------------------------------------------------------------------------

def _context(state, depth="full"):
    """
    Render the snapshot as the briefing AJ reads before speaking.

    Plain text rather than JSON on purpose: this is read by a model that writes
    prose, and a paragraph that already says "22 days since this moved" produces
    better sentences than a field named stale_days.
    """
    p = state.get("profile") or {}
    lines = []

    lines.append("WHO THIS IS")
    lines.append("Name: " + (p.get("name") or "unknown"))
    if p.get("north_star"):
        lines.append("North star (" + str(p.get("horizon_years", 3)) + "-year): " + p["north_star"])
    if p.get("identity"):
        lines.append("Who they are becoming: " + p["identity"])
    if p.get("values_text"):
        lines.append("Non-negotiables: " + p["values_text"].replace("\n", " / "))
    if p.get("stakes"):
        lines.append("What they're running from: " + p["stakes"])

    cred = state["credibility"]
    streak = state["streak"]
    deep = state["recent_deep_hours"]
    lines.append("")
    lines.append("THE RECORD (computed from their data, not their opinion)")
    if cred["score"] is None:
        lines.append("Credibility: no resolved commitments yet — no track record to judge.")
    else:
        lines.append(
            "Credibility: " + str(cred["score"]) + "/100 — kept " + str(cred["kept"])
            + ", missed " + str(cred["missed"]) + ", dropped " + str(cred["dropped"])
            + " (recency-weighted, last 90 days).")
    lines.append("Check-in streak: " + str(streak["days"]) + " days"
                 + (" (already checked in today)" if streak["checked_in_today"]
                    else " (has NOT checked in today)"))
    if deep["days"]:
        lines.append("Deep work: " + format(deep["avg"], ".1f") + "h/day average over the last "
                     + str(deep["days"]) + " days (" + format(deep["total"], ".1f") + "h total).")

    lines.append("")
    lines.append("GOALS")
    if not state["goals"]:
        lines.append("None set yet. That is the headline.")
    for g in state["goals"]:
        h = g["health"]
        lines.append("")
        lines.append("* " + g["title"] + "  [" + g["domain"] + ", status " + g["status"] + "]")
        lines.append("  Metric: " + g["metric_name"] + " — at " + format_value(g, g["current_value"])
                     + ", target " + format_value(g, g["target_value"])
                     + ", started at " + format_value(g, g["start_value"]) + ".")
        lines.append("  Pace: " + str(h["progress_pct"]) + "% of the distance covered against "
                     + str(h["expected_pct"]) + "% of the calendar — " + h["label"].upper()
                     + ". " + str(h["days_left"]) + " days to deadline (" + g["deadline"] + ").")
        if h["per_week"] is not None:
            lines.append("  Required from here: " + format_delta(g, h["per_week"])
                         + " per week to land it.")
        if h["stale_days"] is not None:
            lines.append("  Last measured: " + str(h["stale_days"]) + " days ago"
                         + ("  <-- NOT MOVING" if h["is_stale"] else ""))
        if g.get("why"):
            lines.append("  Why it matters to them: " + g["why"])
        if g.get("stakes"):
            lines.append("  What missing it costs them: " + g["stakes"])
        if depth == "full":
            open_ms = [m for m in g["milestones"] if not m["done_at"]]
            done_ms = [m for m in g["milestones"] if m["done_at"]]
            if done_ms:
                lines.append("  Done: " + "; ".join(m["title"] for m in done_ms[-3:]))
            if open_ms:
                lines.append("  Next milestones: "
                             + "; ".join(m["title"] + " (due " + m["target_date"] + ")"
                                         for m in open_ms[:3]))

    c = state["commitments"]
    lines.append("")
    lines.append("COMMITMENT LEDGER")
    if c["overdue"]:
        lines.append("OVERDUE — still open, past due:")
        for x in c["overdue"]:
            lines.append("  - " + x["text"] + " (due " + x["due_date"] + ", set by " + x["source"] + ")")
    if c["due_today"]:
        lines.append("Due today:")
        for x in c["due_today"]:
            lines.append("  - " + x["text"])
    if c["open"]:
        lines.append("Open, upcoming:")
        for x in c["open"][:6]:
            lines.append("  - " + x["text"] + " (due " + x["due_date"] + ")")
    if c["recent_resolved"]:
        lines.append("Recently resolved:")
        for x in c["recent_resolved"][:8]:
            line = "  - [" + x["status"].upper() + "] " + x["text"] + " (due " + x["due_date"] + ")"
            if x["excuse"]:
                line += "  reason given: \"" + x["excuse"] + "\""
            lines.append(line)
    if not any((c["overdue"], c["due_today"], c["open"], c["recent_resolved"])):
        lines.append("Empty. Nothing on the board with a date on it.")

    if depth == "full" and state["checkins"]:
        lines.append("")
        lines.append("RECENT DAYS (most recent first)")
        for ci in state["checkins"][:7]:
            grade = "" if ci["grade"] is None else ", you graded it " + str(ci["grade"]) + "/100"
            lines.append("  " + ci["day"] + " — energy " + str(ci["energy"]) + "/5, "
                         + format(ci["deep_hours"], ".1f") + "h deep" + grade)
            if ci["log"]:
                lines.append("      did: " + ci["log"])
            if ci["blockers"]:
                lines.append("      blocked by: " + ci["blockers"])

    ag = state.get("agenda") or {}
    if ag.get("connected"):
        lines.append("")
        lines.append("THEIR ACTUAL DAY (from connected calendars — school, dance, family)")
        if ag["today"]:
            for e in ag["today"]:
                when = e["start_time"] + "-" + e["end_time"] if e["start_time"] else "all day"
                lines.append("  TODAY " + when + "  " + e["title"]
                             + (" @ " + e["location"] if e["location"] else "")
                             + ("  [" + e["person"] + "]" if e["person"] else "")
                             + ("  <-- this reshapes the day" if e["is_big"] else ""))
        else:
            lines.append("  Today: nothing on the calendar.")
        for e in ag["tomorrow"]:
            when = e["start_time"] if e["start_time"] else "all day"
            lines.append("  TOMORROW " + when + "  " + e["title"])
        for e in ag["upcoming"][:8]:
            lines.append("  " + e["day"] + " " + (e["start_time"] or "all day") + "  "
                         + e["title"] + ("  <-- major" if e["is_big"] else ""))
        committed = busy_hours(ag["today"])
        if committed:
            lines.append("  Hours of today already committed to the calendar: "
                         + format(committed, ".1f"))

    money = state.get("money") or {}
    if money.get("has_data"):
        lines.append("")
        lines.append("MONEY (last " + str(money["window_days"]) + " days, from their bank export)")
        lines.append("  In $" + format(money["income"], ",.0f") + " / out $"
                     + format(money["spend"], ",.0f") + " / net $"
                     + format(money["net"], ",.0f") + ".")
        lines.append("  Saved $" + format(money["saved"], ",.0f") + " ("
                     + str(round(money["save_rate"] * 100)) + "% of income). Burn is about $"
                     + format(money["monthly_burn"], ",.0f") + "/month.")
        lines.append("  Biggest categories: "
                     + ", ".join(c + " $" + format(v, ",.0f")
                                 for c, v in money["top_categories"][:5]) + ".")
        for mover in money["movers"]:
            lines.append("  UP: " + mover["category"] + " $" + format(mover["now"], ",.0f")
                         + " vs $" + format(mover["was"], ",.0f") + " the month before.")

    if state["signals"]:
        lines.append("")
        lines.append("WHAT THE MOMENTUM ENGINE FLAGGED (deterministic, already shown on their screen)")
        for s in state["signals"][:8]:
            lines.append("  [" + s["level"] + "] " + s["label"])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1. Pressure test — the shark tank
# ---------------------------------------------------------------------------

_PRESSURE_SYSTEM = """You are pressure-testing a goal before it goes on the board.

Treat it the way an investor treats a pitch: the goal does not get accepted
because it sounds good, it gets accepted because it is measurable, dated, and
backed by a first move that happens this week.

Three verdicts, and you must pick one:
  "fund"     — measurable, dated, realistic given their record. Rare. Mean it.
  "sharpen"  — the ambition is right, the definition is soft. Most goals land here.
  "reject"   — this is a wish, a duplicate of a goal already on the board, or it
               contradicts their stated non-negotiables. Say which.

Look for these holes specifically, and only report the ones actually present:
  - no number, or a number that can't be observed without arguing about it
  - a deadline with nothing forcing it (why that date and not six months later?)
  - a target that ignores their track record — check the credibility score and
    the pace on their existing goals before you call something realistic
  - a goal that is really three goals, so nothing can be finished
  - a goal that depends entirely on someone else's decision
  - a fourth active goal when the three they have are already slipping. Attention
    is the scarce resource. Say so.

Your questions are the ones they can't answer with an adjective. "How much MRR,
by when, from how many customers" — not "how committed are you?"

Rewrite the goal in `sharpened` as you would put it on the board: same ambition,
now falsifiable. Keep their metric if it works; replace it if it doesn't.
first_move is the smallest thing that proves the goal is real, doable in 7 days."""

_PRESSURE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["fund", "sharpen", "reject"]},
        "verdict_line": {"type": "string",
                         "description": "One sentence, direct, the reason for the verdict."},
        "holes": {
            "type": "array", "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "hole": {"type": "string", "description": "What's wrong, in a short phrase."},
                    "fix": {"type": "string", "description": "The specific change that closes it."},
                },
                "required": ["hole", "fix"], "additionalProperties": False,
            },
        },
        "questions": {"type": "array", "maxItems": 5, "items": {"type": "string"},
                      "description": "Questions they must answer out loud before this is real."},
        "sharpened": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "metric_name": {"type": "string"},
                "unit": {"type": "string", "description": "$ or % or a short noun, may be empty"},
                "start_value": {"type": "number"},
                "target_value": {"type": "number"},
                "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                "first_move": {"type": "string"},
            },
            "required": ["title", "metric_name", "unit", "start_value",
                         "target_value", "deadline", "first_move"],
            "additionalProperties": False,
        },
    },
    "required": ["verdict", "verdict_line", "holes", "questions", "sharpened"],
    "additionalProperties": False,
}


def pressure_test(draft, state, allow_model=True):
    """Interrogate a goal draft. Returns (result, engine)."""
    prompt = (
        "Today is " + state["today"].isoformat() + ".\n\n"
        + _context(state, depth="brief")
        + "\n\nTHE GOAL THEY WANT TO ADD\n"
        + "Title: " + (draft.get("title") or "(blank)") + "\n"
        + "Why it matters to them: " + (draft.get("why") or "(they didn't say)") + "\n"
        + "Domain: " + (draft.get("domain") or "unspecified") + "\n"
        + "Metric: " + (draft.get("metric_name") or "(none given)") + "\n"
        + "From " + str(draft.get("start_value", "?")) + " to "
        + str(draft.get("target_value", "?")) + " " + (draft.get("unit") or "") + "\n"
        + "Deadline: " + (draft.get("deadline") or "(none given)") + "\n\n"
        "Pressure-test it."
    )
    if not allow_model:
        return _offline_pressure_test(draft, state), "offline"
    try:
        return _ask(_PRESSURE_SYSTEM, prompt, _PRESSURE_SCHEMA), "claude"
    except Exception:
        return _offline_pressure_test(draft, state), "offline"


def _offline_pressure_test(draft, state):
    """
    Deterministic interrogation. It cannot judge whether a target is ambitious,
    but it can catch every structural hole — which is most of what a bad goal is.
    """
    holes, questions = [], []
    title = (draft.get("title") or "").strip()
    metric = (draft.get("metric_name") or "").strip()
    deadline = (draft.get("deadline") or "").strip()

    vague = ("grow", "improve", "better", "more", "scale", "build", "launch",
             "get in shape", "focus", "work on", "start")
    if title and not metric:
        holes.append({"hole": "No metric.",
                      "fix": "Name the number this goal lives or dies on. If it can't "
                             "be read off a screen, it can't be missed either."})
    if any(w in title.lower() for w in vague) and len(title.split()) <= 4:
        holes.append({"hole": "The title is a direction, not a destination.",
                      "fix": "Put the number in the title. \"Grow revenue\" becomes "
                             "\"CarrierGuard to $8k MRR\"."})
    if not deadline:
        holes.append({"hole": "No deadline.",
                      "fix": "Pick a date. An undated goal is something you intend to "
                             "do instead of something you're doing."})
    else:
        try:
            days = (date.fromisoformat(deadline) - state["today"]).days
            if days > 400:
                holes.append({"hole": "The deadline is more than a year out.",
                              "fix": "Set a 90-day checkpoint with its own number. "
                                     "Nothing dated 14 months out gets worked on this week."})
            elif days < 14:
                holes.append({"hole": "The deadline is inside two weeks.",
                              "fix": "That's a commitment, not a goal. Put it on the "
                                     "board as this week's commitment instead."})
        except ValueError:
            pass
    if not (draft.get("why") or "").strip():
        holes.append({"hole": "No stated reason.",
                      "fix": "Write why this matters. On the Tuesday you don't feel "
                             "like it, the reason is the only thing on your side."})

    active = [g for g in state["goals"] if g["status"] == "active"]
    slipping = [g for g in active if g["health"]["status"] in ("watch", "at_risk")]
    if len(active) >= 3 and slipping:
        holes.append({
            "hole": "You already have " + str(len(active)) + " active goals and "
                    + str(len(slipping)) + " of them are behind pace.",
            "fix": "Adding a " + str(len(active) + 1) + "th doesn't add hours to the "
                   "week. Close or drop one first — attention is the constraint."})

    cred = state["credibility"]
    if cred["score"] is not None and cred["score"] < 50 and cred["resolved"] >= 3:
        questions.append("Your credibility is " + str(cred["score"]) + "/100 on commitments "
                         "you already made. What makes this one different?")

    questions += [
        "What number does this read on the day it's done?",
        "Why that deadline? What actually happens on that date?",
        "What's the first move, this week, that proves it's real?",
        "What are you dropping to make room for it?",
    ]
    if metric:
        questions.append("Where does the " + metric + " number come from, and how often "
                         "will you look at it?")

    verdict = "fund" if not holes else ("reject" if len(holes) >= 3 else "sharpen")
    if verdict == "fund":
        line = "Measurable, dated, and it fits next to what's already on the board. It's funded."
    elif verdict == "sharpen":
        line = "The ambition is fine. The definition is soft — fix " + str(len(holes)) \
               + (" thing " if len(holes) == 1 else " things ") + "and it's real."
    else:
        line = "Not in this shape. There are " + str(len(holes)) + " holes here and any " \
               "one of them is enough to make this unmissable, which means unachievable."

    sharpened = {
        "title": title or "Untitled goal",
        "metric_name": metric or "the number that proves it",
        "unit": draft.get("unit") or "",
        "start_value": float(draft.get("start_value") or 0),
        "target_value": float(draft.get("target_value") or 0),
        "deadline": deadline or (state["today"] + timedelta(days=90)).isoformat(),
        "first_move": "Take one reading of " + (metric or "the metric")
                      + " today and write it down. You cannot manage a number you have "
                        "never measured once.",
    }
    return {"verdict": verdict, "verdict_line": line, "holes": holes,
            "questions": questions[:5], "sharpened": sharpened}


# ---------------------------------------------------------------------------
# 2. Build the plan
# ---------------------------------------------------------------------------

_PLAN_SYSTEM = """You are turning an accepted goal into a plan that survives contact
with a real week.

Rules:
- Milestones are outcomes, not activities. "Self-serve signup live" not "work on
  signup". Each one is a thing that is either true or false on its date.
- Between 3 and 6 milestones. Front-load them: the first one lands within two
  weeks, because a plan whose first proof point is 60 days out is a plan you will
  quietly abandon in week three.
- Space them to the deadline given. Never date one past the deadline.
- Commitments are for the next 7 days only, 2 to 4 of them, each small enough to
  finish in a sitting and specific enough that at the end of the week there is no
  argument about whether it happened.
- The bet: one sentence naming what has to be true for this plan to work. This is
  the thing to watch — if the bet breaks, the plan is wrong, not the person.
- Kill criteria: the observable condition under which this goal should be dropped
  or re-dated rather than carried as a zombie. Be concrete: a number and a date.

Respect their non-negotiables. A plan that requires them to abandon a stated
value is a plan you must replace, not caveat."""

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "bet": {"type": "string"},
        "kill_criteria": {"type": "string"},
        "milestones": {
            "type": "array", "minItems": 3, "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "target_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["title", "target_date"], "additionalProperties": False,
            },
        },
        "commitments": {
            "type": "array", "minItems": 2, "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "due_date": {"type": "string", "description": "YYYY-MM-DD, within 7 days"},
                },
                "required": ["text", "due_date"], "additionalProperties": False,
            },
        },
    },
    "required": ["bet", "kill_criteria", "milestones", "commitments"],
    "additionalProperties": False,
}


def build_plan(goal, state, allow_model=True):
    """Decompose a goal into milestones and this week's commitments. Returns (result, engine)."""
    h = goal["health"]
    prompt = (
        "Today is " + state["today"].isoformat() + ".\n\n"
        + _context(state, depth="brief")
        + "\n\nBUILD THE PLAN FOR THIS GOAL\n"
        + "Title: " + goal["title"] + "\n"
        + "Why: " + (goal["why"] or "(unstated)") + "\n"
        + "Metric: " + goal["metric_name"] + " — currently "
        + format_value(goal, goal["current_value"]) + ", target "
        + format_value(goal, goal["target_value"]) + "\n"
        + "Deadline: " + goal["deadline"] + " (" + str(h["days_left"]) + " days out)\n"
        + "Required pace from here: "
        + (format_delta(goal, h["per_week"]) + " per week" if h["per_week"] is not None
           else "deadline has passed") + "\n"
        + ("Existing milestones (do not repeat, build past them): "
           + "; ".join(m["title"] for m in goal["milestones"]) + "\n"
           if goal["milestones"] else "")
        + "\nWrite the plan."
    )
    if not allow_model:
        return _offline_plan(goal, state), "offline"
    try:
        return _ask(_PLAN_SYSTEM, prompt, _PLAN_SCHEMA), "claude"
    except Exception:
        return _offline_plan(goal, state), "offline"


def _offline_plan(goal, state):
    """
    A structural plan: the metric cut into even steps against the calendar.

    It knows nothing about the domain, so it doesn't pretend to — it produces
    checkpoints on the number itself, which is always a valid decomposition and
    never a wrong one.
    """
    h = goal["health"]
    today = state["today"]
    days_left = max(h["days_left"], 14)
    steps = 4 if days_left > 60 else 3
    current = float(goal["current_value"])
    target = float(goal["target_value"])

    milestones = []
    for i in range(1, steps + 1):
        frac = i / steps
        value = current + (target - current) * frac
        when = today + timedelta(days=int(days_left * frac))
        milestones.append({
            "title": goal["metric_name"] + " at " + format_value(goal, value),
            "target_date": min(when, date.fromisoformat(goal["deadline"])).isoformat(),
        })

    weekly = h["per_week"]
    next_week = current + (weekly or 0)
    commitments = [
        {"text": "Take a reading on " + goal["metric_name"] + " and log it",
         "due_date": (today + timedelta(days=1)).isoformat()},
        {"text": "Block three sessions on \"" + goal["title"] + "\" in the calendar",
         "due_date": (today + timedelta(days=2)).isoformat()},
        {"text": "Move " + goal["metric_name"] + " to " + format_value(goal, next_week),
         "due_date": (today + timedelta(days=7)).isoformat()},
    ]

    bet = ("This lands if " + goal["metric_name"] + " moves "
           + (format_delta(goal, weekly) if weekly is not None else "at all")
           + " every week between now and " + goal["deadline"]
           + ". Two flat weeks in a row and the plan is wrong, not the week.")
    kill = ("If " + goal["metric_name"] + " is not past "
            + format_value(goal, current + (target - current) * 0.33) + " by "
            + (today + timedelta(days=int(days_left / 3))).isoformat()
            + ", re-date this goal or drop it. Do not carry it.")
    return {"bet": bet, "kill_criteria": kill,
            "milestones": milestones, "commitments": commitments}


# ---------------------------------------------------------------------------
# 3. The morning brief
# ---------------------------------------------------------------------------

_BRIEF_SYSTEM = """Write this morning's brief. This is the first thing they read today
and it has about fifteen seconds to earn the rest of the day.

Format, exactly, in GitHub-flavored Markdown and nothing else:

**One line of orientation.** Where they stand right now, with the number that
matters most today. No greeting, no "good morning", no restating their goals back
to them.

## The one thing
The single highest-leverage action for today, stated as an action they could
start in the next ten minutes. One short paragraph on why it's this and not
something else. If there is an overdue commitment, it is almost always this.

## What's slipping
Two or three bullets, worst first, each naming the specific number or date.
Skip this section entirely if nothing is slipping — do not manufacture a problem
to fill a heading.

## Holding
One or two bullets on what is genuinely working, with the evidence. Skip if there
is honestly nothing.

## The challenge
One sentence. A specific, uncomfortable, achievable-today demand that moves the
thing most at risk. It should cost them something — an easy challenge is a lie.

Six to twelve sentences total across the whole brief. Every claim traceable to
the data you were given."""


def daily_brief(state, allow_model=True):
    """Today's brief as Markdown. Returns (markdown, engine)."""
    prompt = ("Today is " + state["today"].strftime("%A, %B %-d, %Y") + ".\n\n"
              + _context(state, depth="full")
              + "\n\nWrite the brief.")
    if not allow_model:
        return _offline_brief(state), "offline"
    try:
        return _ask(_BRIEF_SYSTEM, prompt, max_tokens=2000), "claude"
    except Exception:
        return _offline_brief(state), "offline"


def _offline_brief(state):
    one = state["one_thing"]
    sig = state["signals"]
    cred = state["credibility"]
    bad = [s for s in sig if s["level"] in ("critical", "high")]
    good = [s for s in sig if s["level"] == "good"]

    active = [g for g in state["goals"] if g["status"] == "active"]
    behind = [g for g in active if g["health"]["status"] in ("watch", "at_risk")]

    if not active:
        head = "**Nothing is on the board.** No goals, no numbers, no dates — so there is " \
               "nothing to be behind on and nothing to win."
    elif behind:
        worst = min(behind, key=lambda g: g["health"]["days_left"])
        head = ("**" + str(len(behind)) + " of " + str(len(active)) + " goals are behind pace.** "
                "The closest cliff is \"" + worst["title"] + "\" — "
                + str(worst["health"]["progress_pct"]) + "% done against "
                + str(worst["health"]["expected_pct"]) + "% of the calendar, "
                + str(worst["health"]["days_left"]) + " days left.")
    else:
        head = ("**Everything on the board is at or ahead of pace.** That is the moment "
                "to push, not coast — " + str(len(active)) + " active goals, none behind.")

    out = [head]

    # The day as it actually is, before anything is demanded of it.
    ag = state.get("agenda") or {}
    if ag.get("connected") and (ag.get("today") or ag.get("tomorrow")):
        out += ["", "## Today's shape"]
        for event in ag["today"]:
            when = event["start_time"] or "all day"
            out.append("- **" + when + "** " + event["title"]
                       + (" — " + event["location"] if event["location"] else "")
                       + ("  **This is the day. Work around it.**" if event["is_big"] else ""))
        if not ag["today"]:
            out.append("- Calendar is clear. That is a gift and it will not repeat.")
        soon = [e for e in ag.get("upcoming", []) if e["is_big"]][:1]
        if soon:
            days_out = (date.fromisoformat(soon[0]["day"]) - state["today"]).days
            out.append("- **" + soon[0]["title"] + "** in " + str(days_out)
                       + " days — front-load the week now.")

    out += ["", "## The one thing", "**" + one["text"] + "**", "", one["why"]]

    if bad:
        out += ["", "## What's slipping"]
        for s in bad[:3]:
            out.append("- **" + s["label"] + "** " + s["detail"])
    if good:
        out += ["", "## Holding"]
        for s in good[:2]:
            out.append("- **" + s["label"] + "** " + s["detail"])

    if state["commitments"]["overdue"]:
        challenge = ("Close \"" + state["commitments"]["overdue"][0]["text"]
                     + "\" before you open anything else today. Not after lunch — first.")
    elif behind:
        worst = min(behind, key=lambda g: g["health"]["days_left"])
        challenge = ("Move " + worst["metric_name"] + " on \"" + worst["title"]
                     + "\" today, by any amount, and log the reading. "
                     + str(worst["health"]["days_left"]) + " days is not as many as it sounds.")
    elif cred["score"] is not None and cred["score"] < 65:
        challenge = ("Set exactly one commitment today and keep it. Your credibility is "
                     + str(cred["score"]) + "/100 and it only moves one kept promise at a time.")
    else:
        challenge = ("Add one commitment that scares you slightly and put a date on it "
                     "before you close this page.")
    money = state.get("money") or {}
    if money.get("has_data") and (money["net"] < 0 or money["movers"]):
        out += ["", "## Money"]
        if money["net"] < 0:
            out.append("- Out by **$" + format(-money["net"], ",.0f") + "** over "
                       + str(money["window_days"]) + " days. Burn is about $"
                       + format(money["monthly_burn"], ",.0f") + "/month.")
        for mover in money["movers"][:2]:
            out.append("- " + mover["category"].title() + " $"
                       + format(mover["now"], ",.0f") + " this month against $"
                       + format(mover["was"], ",.0f") + " last.")

    out += ["", "## The challenge", challenge]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 4. The debrief — grading the day
# ---------------------------------------------------------------------------

_DEBRIEF_SYSTEM = """Grade the day they just logged, 0-100, and say why.

The grade is for output against what was on the board, not for effort and not for
how they felt. Hold this line: a day of honest work that moved no number and
closed no commitment is a 50, and you say so kindly and clearly.

Anchors — use them, don't drift:
  90-100  A commitment closed or a metric moved materially, on the goal that most needed it.
  75-89   Real progress on the right thing. Nothing closed, but the needle moved.
  60-74   Work happened, on secondary things. The at-risk goal was untouched.
  40-59   Motion without output. Inbox, meetings, maintenance.
  0-39    Nothing on the goals, and no reason given that will still sound good in a month.

A low-energy day where they still did the one thing scores HIGH. A high-energy
day spent on the comfortable goal while the at-risk one sat scores LOW. Say that
explicitly when it happens — it's the most useful thing you can tell them.

headline: one sentence, blunt, the verdict.
body: 3-5 sentences. What it moved, what it didn't, and the single change for
tomorrow. If they named a blocker that has appeared before in their recent days,
name the repetition and make them decide about it."""

_DEBRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "grade": {"type": "integer", "minimum": 0, "maximum": 100},
        "headline": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["grade", "headline", "body"],
    "additionalProperties": False,
}


def debrief(checkin, state, allow_model=True):
    """Grade a day's check-in. Returns (result, engine)."""
    prompt = (
        "Today is " + state["today"].isoformat() + ".\n\n"
        + _context(state, depth="full")
        + "\n\nTHE DAY THEY JUST LOGGED (" + checkin["day"] + ")\n"
        + "Energy: " + str(checkin["energy"]) + "/5\n"
        + "Hours of deep work on the goals: " + format(checkin["deep_hours"], ".1f") + "\n"
        + "What they did: " + (checkin["log"] or "(they left this blank)") + "\n"
        + "Blockers: " + (checkin["blockers"] or "(none named)") + "\n\n"
        "Grade it."
    )
    if not allow_model:
        return _offline_debrief(checkin, state), "offline"
    try:
        return _ask(_DEBRIEF_SYSTEM, prompt, _DEBRIEF_SCHEMA, max_tokens=1500), "claude"
    except Exception:
        return _offline_debrief(checkin, state), "offline"


def _offline_debrief(checkin, state):
    """
    Grades what it can actually observe: hours, commitments closed today, metric
    readings logged today. It cannot read the quality of the work, so it says so
    rather than pretending the number is more than it is.
    """
    day = checkin["day"]
    closed = [c for c in state["commitments"]["recent_resolved"]
              if c["status"] == "kept" and (c["resolved_at"] or "")[:10] == day]
    missed = [c for c in state["commitments"]["recent_resolved"]
              if c["status"] == "missed" and (c["resolved_at"] or "")[:10] == day]
    hours = float(checkin["deep_hours"])

    grade = 50
    reasons = []
    if closed:
        grade += 30
        reasons.append("You closed " + str(len(closed)) + " commitment"
                       + ("s" if len(closed) > 1 else "") + ": "
                       + "; ".join(c["text"] for c in closed) + ".")
    if missed:
        grade -= 15
        reasons.append("You let " + str(len(missed)) + " go past due.")
    if hours >= 4:
        grade += 15
        reasons.append(format(hours, ".1f") + " hours of deep work is a real day.")
    elif hours >= 2:
        grade += 5
    elif hours < 1:
        grade -= 20
        reasons.append(format(hours, ".1f") + " hours on the goals is not a day of work "
                       "on the goals, whatever else it was.")
    if not (checkin["log"] or "").strip():
        grade -= 10
        reasons.append("You logged nothing, which is its own answer.")
    if state["commitments"]["overdue"]:
        grade -= 10
        n = len(state["commitments"]["overdue"])
        reasons.append("There " + ("is still 1 overdue commitment" if n == 1 else
                                   "are still " + str(n) + " overdue commitments")
                       + " sitting open while you worked on other things.")
    grade = max(0, min(grade, 100))

    if grade >= 85:
        headline = "That's the day. Do it again tomorrow."
    elif grade >= 70:
        headline = "Solid, not decisive. The needle moved; nothing closed."
    elif grade >= 50:
        headline = "Motion, not progress. Nothing on the board changed state today."
    else:
        headline = "This one doesn't count. Say it plainly and start over tomorrow."

    blocker = (checkin["blockers"] or "").strip().lower()
    repeats = [c for c in state["checkins"][1:8]
               if blocker and blocker[:16] in (c["blockers"] or "").lower()]
    if repeats:
        reasons.append("\"" + checkin["blockers"] + "\" has blocked you "
                       + str(len(repeats) + 1) + " times in the last week. That is not "
                       "an interruption anymore, it's the environment. Change it or "
                       "shrink what you promised around it.")

    tomorrow = state["one_thing"]["text"]
    reasons.append("Tomorrow: " + tomorrow + ". First, before anything else.")
    return {"grade": grade, "headline": headline, "body": " ".join(reasons)}


# ---------------------------------------------------------------------------
# 5. The weekly review — the board meeting
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM = """Run their weekly board meeting. You are the board, they are the
operator, and the meeting is about what moved — not about how the week felt.

GitHub-flavored Markdown, these sections exactly:

**One line: the verdict on the week.**

## The numbers
Each active goal, one line: where the metric started the week, where it is now,
whether that is ahead of or behind the pace it needs. Do not editorialize here.
Just the ledger.

## What you actually did
Two or three bullets. Kept commitments and real movement, named specifically.
If the honest answer is "not much", the section says that in one line.

## What you avoided
The hard section. Goals untouched, commitments missed, patterns in the excuses.
Name repeated excuses as patterns. If a goal has not moved in two weeks or more,
say the word: it's dead, and ask whether to revive or bury it.

## The decision
Every week ends with one decision, not a list. Something to cut, re-date, or
double down on. State it as a recommendation and give the reason in one sentence.

## Next week
Two or three commitments, each with a day. Fewer than they want. Enough that
keeping all of them is possible, because rebuilding credibility requires a week
where the board is clean.

Under 350 words. This is a board meeting, not a journal entry."""


def weekly_review(state, allow_model=True):
    """The week in review as Markdown. Returns (markdown, engine)."""
    week_start = state["today"] - timedelta(days=6)
    prompt = ("The week under review is " + week_start.isoformat() + " to "
              + state["today"].isoformat() + ".\n\n"
              + _context(state, depth="full")
              + "\n\nRun the meeting.")
    if not allow_model:
        return _offline_review(state), "offline"
    try:
        return _ask(_REVIEW_SYSTEM, prompt, max_tokens=3000), "claude"
    except Exception:
        return _offline_review(state), "offline"


def _offline_review(state):
    today = state["today"]
    week_start = today - timedelta(days=6)
    iso_start = week_start.isoformat()

    active = [g for g in state["goals"] if g["status"] == "active"]
    kept = [c for c in state["commitments"]["recent_resolved"]
            if c["status"] == "kept" and (c["resolved_at"] or c["due_date"]) >= iso_start]
    missed = [c for c in state["commitments"]["recent_resolved"]
              if c["status"] == "missed" and (c["resolved_at"] or c["due_date"]) >= iso_start]
    week_checkins = [c for c in state["checkins"] if c["day"] >= iso_start]
    hours = sum(c["deep_hours"] for c in week_checkins)

    if kept and not missed:
        verdict = "**A clean week.** " + str(len(kept)) + " commitments made, " \
                  + str(len(kept)) + " kept, " + format(hours, ".1f") + " hours on the goals."
    elif missed:
        verdict = ("**You kept " + str(len(kept)) + " of " + str(len(kept) + len(missed))
                   + " promises this week.** " + format(hours, ".1f") + " hours of deep work "
                   "across " + str(len(week_checkins)) + " logged days.")
    else:
        verdict = ("**Nothing resolved on the board this week.** "
                   + format(hours, ".1f") + " hours logged across "
                   + str(len(week_checkins)) + " days.")

    out = [verdict, "", "## The numbers"]
    if not active:
        out.append("No active goals. That is the entire report.")
    for g in active:
        h = g["health"]
        out.append("- **" + g["title"] + "** — " + format_value(g, g["current_value"])
                   + " of " + format_value(g, g["target_value"]) + " ("
                   + str(h["progress_pct"]) + "% done, " + str(h["expected_pct"])
                   + "% of the calendar spent). " + h["label"] + ", "
                   + str(h["days_left"]) + " days left."
                   + (" Needs " + format_delta(g, h["per_week"]) + "/week."
                      if h["per_week"] is not None else ""))

    out += ["", "## What you actually did"]
    if kept:
        for c in kept[:4]:
            out.append("- Kept: " + c["text"])
    if hours:
        out.append("- " + format(hours, ".1f") + " hours of deep work, "
                   + format(hours / max(len(week_checkins), 1), ".1f") + "h a day across "
                   + str(len(week_checkins)) + " logged days.")
    if not kept and not hours:
        out.append("- Not much. Nothing closed and no hours logged against the goals.")

    out += ["", "## What you avoided"]
    avoided = False
    for c in missed:
        avoided = True
        out.append("- Missed: **" + c["text"] + "**"
                   + (" — reason given: \"" + c["excuse"] + "\"" if c["excuse"] else
                      " — no reason given."))
    for g in active:
        if g["health"]["is_stale"]:
            avoided = True
            out.append("- **" + g["title"] + "** hasn't been measured in "
                       + str(g["health"]["stale_days"]) + " days. Call it: revive it "
                       "this week with a date, or bury it and stop carrying the guilt.")
    for c in state["commitments"]["overdue"]:
        avoided = True
        out.append("- Still open, past due: " + c["text"] + " (due " + c["due_date"] + ")")
    excuses = [c["excuse"].strip().lower() for c in missed if c["excuse"].strip()]
    if len(excuses) >= 2 and len(set(w for e in excuses for w in e.split()[:3])) < 4:
        avoided = True
        out.append("- The same reason appears more than once this week. That's not an "
                   "interruption, that's the shape of your week. Change it or plan around it.")
    if not avoided:
        out.append("- Nothing overdue, nothing stale. Clean board.")

    cred = state["credibility"]
    out += ["", "## The decision"]
    stale = [g for g in active if g["health"]["is_stale"]]
    at_risk = [g for g in active if g["health"]["status"] == "at_risk"]
    if stale:
        out.append("Bury or re-date **" + stale[0]["title"] + "**. It has not moved in "
                   + str(stale[0]["health"]["stale_days"]) + " days, and a goal you are "
                   "not working on is costing you attention without paying anything back.")
    elif at_risk:
        out.append("Double down on **" + at_risk[0]["title"] + "** and put everything else "
                   "in maintenance for seven days. It needs "
                   + format_delta(at_risk[0], at_risk[0]["health"]["per_week"])
                   + " a week and it is not getting it while you spread yourself across "
                   + str(len(active)) + " goals.")
    elif cred["score"] is not None and cred["score"] < 65:
        out.append("Cut next week's commitments to two. Your credibility is "
                   + str(cred["score"]) + "/100 — the fix is a smaller board you actually clear.")
    else:
        out.append("Raise the target on the goal that's ahead of pace. Nothing here is "
                   "broken, which means the plan is too comfortable.")

    out += ["", "## Next week"]
    picks = state["commitments"]["overdue"][:1] + state["commitments"]["open"][:2]
    if picks:
        for c in picks:
            # An overdue item carried into next week needs a new date, not the old
            # one repeated back — a due date in the past is not a commitment.
            when = max(c["due_date"], (today + timedelta(days=1)).isoformat())
            out.append("- " + c["text"] + " — by " + when
                       + ("  (was due " + c["due_date"] + ")" if when != c["due_date"] else ""))
    else:
        one = state["one_thing"]
        out.append("- " + one["text"] + " — by "
                   + (today + timedelta(days=3)).isoformat())
        out.append("- Set two more commitments with dates before Monday ends.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 6. The war room
# ---------------------------------------------------------------------------

_CHAT_SYSTEM = """You are in the war room. They came here to think out loud, to argue
with you, or to be talked out of something.

- Answer the thing they actually asked. Do not redirect every message back to
  their overdue commitments — you're a chief of staff, not a nag with one line.
- Use their real numbers when they're relevant, and only then.
- When they're rationalizing, say so once, plainly, and move on. When they're
  right, say they're right — you are not contrarian for sport.
- When they bring you a decision, take a side. "It depends" is what people say
  when they don't want to be wrong. Give your answer and the reason.
- Keep it to a few sentences unless they asked for depth. This is a conversation.
- End with a question or a specific next step whenever the exchange calls for it,
  and not when it doesn't. A forced call-to-action on every message is a tell
  that you're a script."""


def chat(message, state, history=None, allow_model=True):
    """One turn in the war room. Returns (reply, engine)."""
    convo = ""
    for turn in (history or []):
        who = "THEM" if turn["role"] == "you" else "YOU (AJ)"
        convo += who + ": " + turn["text"] + "\n\n"

    prompt = ("Today is " + state["today"].isoformat() + ".\n\n"
              + _context(state, depth="full")
              + ("\n\nCONVERSATION SO FAR\n" + convo if convo else "")
              + "\nTHEM: " + message + "\n\nReply as AJ.")
    if not allow_model:
        return _offline_chat(message, state), "offline"
    try:
        return _ask(_CHAT_SYSTEM, prompt, max_tokens=1600), "claude"
    except Exception:
        return _offline_chat(message, state), "offline"


def _offline_chat(message, state):
    """
    The offline coach cannot converse, and pretending otherwise would be the one
    dishonest thing in this app. So it says what it is, then does the one thing it
    genuinely can: put the current state in front of them, sharply.
    """
    text = message.lower()
    one = state["one_thing"]
    cred = state["credibility"]

    if any(w in text for w in ("stuck", "overwhelm", "too much", "burn", "tired", "can't")):
        active = [g for g in state["goals"] if g["status"] == "active"]
        lead = ("You have " + str(len(active)) + " active goals and "
                + str(len(state["commitments"]["overdue"])) + " overdue commitments. "
                "Overwhelm is usually an inventory problem, not a character problem — "
                "cut the board to one goal for seven days and the feeling usually goes "
                "with it.")
    elif any(w in text for w in ("should i", "worth it", "or should", "decide", "decision")):
        lead = ("I can't weigh that one offline. What I can tell you: the goal closest "
                "to failing is the one with " + str(min(
                    (g["health"]["days_left"] for g in state["goals"]
                     if g["status"] == "active"), default=0))
                + " days left. Any decision that doesn't help that one is a distraction "
                  "this week.")
    elif any(w in text for w in ("did i", "how am i", "status", "where am i", "progress")):
        lead = ("Credibility " + (str(cred["score"]) + "/100" if cred["score"] is not None
                                  else "unrated") + ", " + str(cred["kept"]) + " kept and "
                + str(cred["missed"]) + " missed. Streak: " + str(state["streak"]["days"])
                + " days. " + str(len([g for g in state["goals"]
                                       if g["status"] == "active"
                                       and g["health"]["status"] in ("watch", "at_risk")]))
                + " goals behind pace.")
    else:
        lead = ("AJ is offline — no Anthropic credentials are set, so there's no one here "
                "to argue with you. Set ANTHROPIC_API_KEY and this becomes a conversation.")

    return (lead + "\n\nWhat hasn't changed: **" + one["text"] + "** — " + one["why"])
