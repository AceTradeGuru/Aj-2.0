# AJ 2.0

An AI agent that runs with me and challenges me to new heights. My own personal
CEO shark tank assistant.

Not a chatbot with a motivational voice. AJ is a coach with a **ledger** — it
knows what you said you'd do, whether you did it, where every number actually
sits against its deadline, and it argues from those rows rather than from
vibes. When it tells you a goal is dead, there's a stale-days count behind it
you can click.

```bash
python3 aj_db.py --demo    # build the database with a demo life to click through
python3 app.py             # http://localhost:5004
```

Drop `--demo` to start empty; the app will onboard you on first load. No API key
required — see [Two engines](#two-engines).

---

## What it does

| Screen | What happens there |
|---|---|
| **War table** | The morning brief, the one thing that matters today, the commitment board, and every signal the momentum engine flagged. |
| **Goals** | Each goal against the calendar: how far you've come, where the pace pin says you should be, what it now takes per week to still land it. |
| **New goal** | The shark tank. Every goal gets pressure-tested before it reaches the board — holes named, questions you can't answer with an adjective, a sharpened version you can accept or override. |
| **Check-in** | Two minutes a day. AJ grades it 0–100 against what was on the board, not against how hard it felt. |
| **War room** | Think out loud, argue, bring a decision. AJ has your pace and your record in front of him. |
| **Weekly review** | The board meeting. What moved, what you avoided, and one decision — cut, re-date, or double down. |

## The idea

Most goal apps fail the same way: they measure activity, and activity is the
thing you're already good at. AJ measures three things you can't argue with.

**Your word.** Every commitment is a promise with a due date, resolved as kept,
missed, or dropped — and never deleted. Your credibility score is computed from
that ledger, recency-weighted, with anything left open past its grace period
counted as a miss. It's the only number on the dashboard that predicts the
others.

**The pace pin.** Every goal shows progress against *elapsed calendar*, not
against zero. Being 30% done sounds fine until the pin says 44% of your time is
gone. Goals that count down (body fat, debt, churn) work the same way without a
special case, because progress is measured as a fraction of the distance from
start to target.

**Silence.** The check that catches what a status column never will: a goal
whose metric hasn't been measured in two weeks gets called out no matter how
green it looks. A goal you aren't measuring is a goal you've stopped working on.

## Two engines

Every AI capability has two implementations, and every screen labels which one
produced what you're reading.

- **Claude** (`claude-opus-5`) is the real product. It reads your excuse from
  Tuesday against your excuse from three weeks ago and tells you it's the same
  excuse.
- **The offline coach** is deterministic, built entirely out of `momentum.py`.
  It runs with no API key, no network, and no cost.

So the repo clones and runs on a laptop, and a rate limit or a hit spend cap
degrades the coaching instead of breaking your morning. Credentials resolve the
normal way — `ANTHROPIC_API_KEY`, or an `ant auth login` profile. Nothing is
hardcoded and nothing is prompted for.

## The architecture, in one rule

**The model never gets to invent evidence.**

```
momentum.py     arithmetic only — credibility, pace, staleness, signals
    |           deterministic, no model calls, testable
    v
snapshot()      one dict: goals + health, the ledger, the streak, the signals
    |
    +---------> templates      what you see
    +---------> coach_engine   what AJ says
```

Both the screen and the prompt render from the same snapshot, so what AJ tells
you and what the dashboard shows cannot drift apart. `coach_engine.py` decides
what to *say* about the numbers; it never produces them.

## Files

| File | What's in it |
|---|---|
| `momentum.py` | The arithmetic of your life. Credibility, goal health, streaks, signals, the one thing. |
| `coach_engine.py` | The voice, and six capabilities: pressure test, plan, brief, debrief, weekly review, chat. |
| `app.py` | Flask routes, CSRF, the model budget, the Markdown filter. |
| `aj_db.py` | Build the database; `--demo` loads a life mid-struggle. |
| `schema.sql` | Nine tables. The commitment ledger is the important one. |
| `config.py` | Environment-driven config; production refuses to boot underspecified. |

## What it costs

Model calls are booked against a monthly cap from the token usage the API
reports (`AI_MONTHLY_CAP_USD`, default $25). Past the cap AJ keeps answering
from the offline coach rather than failing. **Mandate & spend** shows the
running total by feature.

## Privacy

This database is the most candid document you own. It stays in one SQLite file
you control, and `AJ_PASSCODE` gates the whole app — required in production,
optional locally where localhost is the auth. Nothing is sent anywhere except
the model calls you trigger.

See [DEPLOY.md](DEPLOY.md) to run it somewhere you can reach from your phone.
