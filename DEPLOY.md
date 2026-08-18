# Running AJ 2.0 somewhere you can reach it

Local is the default and it's fine — the app runs on your laptop with no key, no
account, and no network. Deploy only when you want it on your phone at 6am.

## Locally

```bash
pip install -r requirements.txt
python3 aj_db.py          # add --demo for a life to click through first
python3 app.py            # http://localhost:5004
```

Turn on the real coach:

```bash
export ANTHROPIC_API_KEY=sk-ant-...    # or: ant auth login
python3 app.py
```

The sidebar tells you which engine you're on. No key means the offline coach —
the app never silently pretends otherwise.

## Deployed

`render.yaml` is a Render blueprint: **New → Blueprint**, point it at this repo.

Three things you must set by hand in the dashboard (none are read from the repo):

| Variable | Why |
|---|---|
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `AJ_PASSCODE` | The gate on the whole app. Production refuses to boot without it. |
| `ANTHROPIC_API_KEY` | Optional. Without it the offline coach runs. |

**The disk is not optional.** Everything lives in one SQLite file. Without the
persistent disk in `render.yaml` (mounted at `/var/data`, with `AJ_DB` pointing
into it), every restart wipes your goals. That's the whole failure mode of
deploying a file-backed app, and it's why the blueprint provisions the disk and
the starter plan that allows one.

Create the schema once, from a Render shell:

```bash
python3 -c "from aj_db import create_tables; create_tables()"
```

Then open the app and answer the five onboarding questions.

## The production guards

`config.validate()` refuses to start and prints every problem at once if, with
`AJ_ENV=production`:

- `SECRET_KEY` is missing
- `AJ_PASSCODE` is missing — this database holds your goals, your misses, and
  your excuses
- debug is on (the Werkzeug debugger is remote code execution)
- secure cookies are off

A misconfigured deploy fails loudly instead of quietly serving an open app.

## Backups

One file. Copy it:

```bash
scp you@host:/var/data/aj.db ./aj-backup-$(date +%F).db
```

Do this before you rely on it, not after.

## Cost

| | |
|---|---|
| Render starter + 1GB disk | ~$7/mo |
| Claude | usage, capped by `AI_MONTHLY_CAP_USD` (default $25) |
| Running it locally | $0 |

Past the cap, AJ answers from the offline coach instead of erroring. Check
**Mandate & spend** for the running total by feature.
