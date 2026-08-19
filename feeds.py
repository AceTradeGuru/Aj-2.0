"""
AJ 2.0 — the life feeds.

Goals are half a day. The other half is her recital, the science fair, the
tuition draft, and the thing due Friday — and that half decides whether the
goals get worked at all. This module is how the rest of your life gets in.

Two readers, and a deliberate choice behind each:

    read_ics()          a calendar subscription URL -> events
    read_bank_csv()     the export your bank already gives you -> transactions

**Calendars are subscriptions, not integrations.** BAND publishes one
(Calendar tab -> Manage Events -> Export Band Calendars -> copy URL), and so do
Canvas, PowerSchool, Google Calendar, and every studio management app worth
using. One reader therefore covers dance, school, and the family calendar at
once, with no API keys, no OAuth consent screen, and no partnership to apply
for. The URL is a live subscription: re-read it and you get the studio's
schedule change.

**Money comes from the file you can already download.** An aggregator wants your
banking credentials and a monthly fee to show you what a CSV export shows for
free. The importer below is deliberately forgiving about column names because
every bank names them differently, and it is idempotent so re-importing an
overlapping export never double-counts. See LIVE_BANK_NOTE for what a live
connection would actually take.

Nothing here calls a model. Feeds produce rows; `momentum.py` decides what they
mean; `coach_engine.py` decides what to say about them.
"""

import csv
import io
import re
import urllib.request
from datetime import date, datetime, time, timedelta

# How far ahead a repeating event is expanded. A weekly dance class has no end
# date, so "every Tuesday forever" has to be cut off somewhere; 120 days is past
# any recital you'd be planning around and keeps the table small.
HORIZON_DAYS = 120

# Feeds are fetched from a URL you pasted, so the guards are about a hung request
# and a runaway download, not about a hostile server.
FETCH_TIMEOUT = 20
MAX_FEED_BYTES = 8 * 1024 * 1024

LIVE_BANK_NOTE = """\
Live bank sync means an aggregator — Plaid is the usual one. It needs a Plaid
account, a client ID and secret, their Link flow to capture the bank login, and
production access is an application, not a signup. Until that's worth it, the
CSV import here reads the same data from a file you already have."""


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch(url):
    """Pull a feed. Raises on anything that isn't a clean read."""
    if not re.match(r"^https?://", url or "", re.I):
        raise ValueError("A calendar feed needs an http(s) URL. Copy it out of the "
                         "app's calendar export screen.")
    # Some calendar hosts hand out webcal:// links; they are http(s) underneath.
    request = urllib.request.Request(url, headers={"User-Agent": "AJ2.0/1.0"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read(MAX_FEED_BYTES + 1)
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("That feed is larger than 8MB, which means it isn't a calendar.")
    return raw.decode("utf-8", "replace")


def normalize_feed_url(url):
    """webcal:// is an http(s) URL wearing a hat. Chrome won't fetch it; we can."""
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        return "https://" + url[9:]
    return url


# ---------------------------------------------------------------------------
# iCalendar
# ---------------------------------------------------------------------------
#
# A hand-written parser rather than a dependency. The format is line-oriented and
# the subset that calendar apps actually emit is small: VEVENT blocks with a UID,
# a SUMMARY, a DTSTART, and — the one that matters for a dance schedule — an
# RRULE saying it repeats. What is NOT supported is stated in expand_rrule().

_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _unfold(text):
    """
    iCalendar wraps long lines by starting the continuation with a space or tab.
    Un-wrap before parsing or every long event title parses as a bad property.
    """
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unescape(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\;", ";").replace("\\\\", "\\"))


def _parse_dt(value, params, tz=None):
    """
    Return (date, 'HH:MM' or ''), handling the three forms feeds actually use:

        DTSTART;VALUE=DATE:20260819                  all-day
        DTSTART:20260819T173000Z                     UTC
        DTSTART;TZID=America/New_York:20260819T173000  local to a zone

    Times are converted into `tz` — the zone YOU live in, from AJ_TZ — not the
    server's. A box in UTC would otherwise turn a 5:30pm dance class into a
    9:30pm one, and a schedule that is off by four hours is worse than none.
    """
    value = value.strip()
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8])), ""

    match = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$", value)
    if not match:
        return None, ""
    year, month, day, hour, minute, second, zulu = match.groups()
    stamp = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))

    tzid = params.get("TZID")
    try:
        from zoneinfo import ZoneInfo
        target = ZoneInfo(tz) if tz else None
        if zulu:
            stamp = stamp.replace(tzinfo=ZoneInfo("UTC"))
            stamp = stamp.astimezone(target) if target else stamp.astimezone()
        elif tzid:
            stamp = stamp.replace(tzinfo=ZoneInfo(tzid))
            stamp = stamp.astimezone(target) if target else stamp.astimezone()
    except Exception:
        # An unknown zone name is not a reason to lose the event. Treat the
        # timestamp as local wall-clock, which is right far more often than not.
        pass
    return stamp.date(), stamp.strftime("%H:%M")


def _parse_line(line):
    """'DTSTART;TZID=America/New_York:20260819T173000' -> (name, params, value)."""
    if ":" not in line:
        return None, {}, ""
    head, _, value = line.partition(":")
    parts = head.split(";")
    name = parts[0].upper()
    params = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip('"')
    return name, params, value


def expand_rrule(rule, start_day, window_start, window_end, excluded, tz=None):
    """
    Expand a repeating event across the window.

    Supported, because it is what calendar apps emit for a class schedule:
    FREQ=DAILY|WEEKLY|MONTHLY, INTERVAL, COUNT, UNTIL, BYDAY, and EXDATE
    (a cancelled class must not still be on your Tuesday).

    Not supported: BYMONTHDAY, BYSETPOS, BYMONTH, yearly rules, and the rest of
    RFC 5545's long tail. An unsupported rule degrades to the single starting
    occurrence rather than guessing — a schedule that invents a practice you
    don't have is worse than one that misses a repeat.
    """
    parts = {}
    for chunk in rule.split(";"):
        key, _, value = chunk.partition("=")
        parts[key.upper()] = value
    freq = parts.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY", "MONTHLY"):
        return [start_day]

    interval = int(parts.get("INTERVAL") or 1)
    count = int(parts["COUNT"]) if parts.get("COUNT") else None
    until = None
    if parts.get("UNTIL"):
        until, _ = _parse_dt(parts["UNTIL"], {}, tz)

    weekdays = [_DAYS[d[-2:]] for d in parts.get("BYDAY", "").split(",")
                if d and d[-2:] in _DAYS]

    days, cursor, emitted, guard = [], start_day, 0, 0
    while cursor <= window_end and guard < 2000:
        guard += 1
        if freq == "WEEKLY" and weekdays:
            week_start = cursor - timedelta(days=cursor.weekday())
            for offset in sorted(weekdays):
                day = week_start + timedelta(days=offset)
                if day < start_day or day > window_end:
                    continue
                if until and day > until:
                    continue
                if day.isoformat() in excluded:
                    continue
                if day >= window_start:
                    days.append(day)
                emitted += 1
                if count and emitted >= count:
                    return sorted(set(days))
            cursor += timedelta(weeks=interval)
            continue

        if until and cursor > until:
            break
        if cursor.isoformat() not in excluded:
            if cursor >= window_start:
                days.append(cursor)
            emitted += 1
            if count and emitted >= count:
                break
        if freq == "DAILY":
            cursor += timedelta(days=interval)
        elif freq == "WEEKLY":
            cursor += timedelta(weeks=interval)
        else:                                  # MONTHLY, same day-of-month
            month = cursor.month + interval
            year = cursor.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            try:
                cursor = cursor.replace(year=year, month=month)
            except ValueError:                 # e.g. the 31st in a 30-day month
                break
    return sorted(set(days))


def read_ics(text, today=None, horizon_days=HORIZON_DAYS, tz=None):
    """
    Parse an .ics feed into event dicts.

    Returns events from a week back (so this morning's practice is still visible
    in a brief written at noon) out to the horizon. Each dict carries the feed's
    own uid, which is what makes re-syncing an update instead of a duplicate.
    """
    today = today or date.today()
    window_start = today - timedelta(days=7)
    window_end = today + timedelta(days=horizon_days)

    events, current, in_event = [], None, False
    for line in _unfold(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            in_event, current = True, {"exdate": set()}
            continue
        if upper.startswith("END:VEVENT"):
            in_event = False
            if current and current.get("day"):
                events.extend(_materialize(current, window_start, window_end, tz))
            current = None
            continue
        if not in_event or current is None:
            continue

        name, params, value = _parse_line(line)
        if name == "UID":
            current["uid"] = value.strip()
        elif name == "SUMMARY":
            current["title"] = _unescape(value).strip()
        elif name == "LOCATION":
            current["location"] = _unescape(value).strip()
        elif name == "DTSTART":
            day, clock = _parse_dt(value, params, tz)
            current["day"], current["start_time"] = day, clock
        elif name == "DTEND":
            _, clock = _parse_dt(value, params, tz)
            current["end_time"] = clock
        elif name == "RRULE":
            current["rrule"] = value.strip()
        elif name == "EXDATE":
            for chunk in value.split(","):
                day, _ = _parse_dt(chunk, params, tz)
                if day:
                    current["exdate"].add(day.isoformat())
        elif name == "STATUS" and value.strip().upper() == "CANCELLED":
            current["cancelled"] = True

    return events


def _materialize(event, window_start, window_end, tz=None):
    """One parsed VEVENT -> one row per occurrence inside the window."""
    if event.get("cancelled"):
        return []
    start_day = event["day"]
    title = event.get("title") or "(untitled)"
    uid = event.get("uid") or (title + start_day.isoformat())

    if event.get("rrule"):
        days = expand_rrule(event["rrule"], start_day, window_start,
                            window_end, event["exdate"], tz)
    else:
        days = [start_day] if window_start <= start_day <= window_end else []

    rows = []
    for day in days:
        rows.append({
            # The uid is per-occurrence, or a weekly class would collapse into
            # one row that keeps moving to the next Tuesday.
            "uid": uid if len(days) == 1 else uid + "@" + day.isoformat(),
            "title": title,
            "day": day.isoformat(),
            "start_time": event.get("start_time", ""),
            "end_time": event.get("end_time", ""),
            "location": event.get("location", ""),
        })
    return rows


def sync_ics(conn, source, today=None, tz=None):
    """
    Re-read one calendar source and upsert its events. Returns (added, updated).

    Failures are written to the source row rather than raised: a feed that
    silently stopped syncing is worse than no feed, because you would keep
    trusting a schedule that stopped being true.
    """
    try:
        text = fetch(normalize_feed_url(source["url"]))
        rows = read_ics(text, today, tz=tz)
    except Exception as exc:
        conn.execute("UPDATE sources SET last_error = ? WHERE id = ?",
                     (str(exc)[:400], source["id"]))
        conn.commit()
        raise

    added = updated = 0
    for row in rows:
        existing = conn.execute(
            "SELECT id, title, day, start_time, end_time, location FROM events "
            "WHERE source_id = ? AND uid = ?", (source["id"], row["uid"])).fetchone()
        if existing is None:
            conn.execute("""INSERT INTO events (source_id, uid, title, day, start_time,
                            end_time, location, person) VALUES (?,?,?,?,?,?,?,?)""",
                         (source["id"], row["uid"], row["title"], row["day"],
                          row["start_time"], row["end_time"], row["location"],
                          source["person"]))
            added += 1
        elif (existing["title"], existing["day"], existing["start_time"],
              existing["end_time"], existing["location"]) != (
              row["title"], row["day"], row["start_time"], row["end_time"],
              row["location"]):
            conn.execute("""UPDATE events SET title=?, day=?, start_time=?, end_time=?,
                            location=? WHERE id=?""",
                         (row["title"], row["day"], row["start_time"], row["end_time"],
                          row["location"], existing["id"]))
            updated += 1

    conn.execute("UPDATE sources SET last_synced_at = datetime('now'), last_error = '' "
                 "WHERE id = ?", (source["id"],))
    conn.commit()
    return added, updated


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

# Every bank names its columns differently and none of them ask first. Matching
# on substrings of the header beats maintaining a per-bank list forever.
_DATE_HEADERS = ("transaction date", "posted date", "post date", "date")
_DESC_HEADERS = ("description", "payee", "name", "memo", "details", "transaction")
_AMOUNT_HEADERS = ("amount", "transaction amount")
_DEBIT_HEADERS = ("debit", "withdrawal", "withdrawals", "money out")
_CREDIT_HEADERS = ("credit", "deposit", "deposits", "money in")

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y",
                 "%b %d, %Y", "%d-%b-%Y", "%Y/%m/%d")

# Categories are rules, not a model call. Spending patterns are the one thing
# here where a wrong guess is expensive and a lookup table is honest — and you
# can see exactly why something landed where it did.
_CATEGORY_RULES = [
    ("income", ("payroll", "direct dep", "deposit", "salary", "stripe", "payout",
                "interest", "refund", "zelle from", "transfer from")),
    ("housing", ("rent", "mortgage", "hoa", "electric", "water", "gas company",
                 "utility", "utilities", "internet", "comcast", "xfinity")),
    ("transport", ("gas", "shell", "chevron", "exxon", "bp ", "uber", "lyft",
                   "car payment", "auto loan", "dmv", "parking", "toll")),
    ("food", ("grocer", "supermarket", "safeway", "kroger", "trader joe",
              "whole foods", "aldi", "costco", "walmart", "publix", "heb")),
    ("dining", ("restaurant", "cafe", "coffee", "starbucks", "doordash",
                "grubhub", "uber eats", "chipotle", "pizza", "mcdonald", "bar ")),
    ("kids", ("dance", "school", "tuition", "daycare", "recital", "costume",
              "pta", "lunch account", "camp")),
    ("business", ("aws", "google cloud", "github", "stripe fee", "adobe",
                  "notion", "figma", "openai", "anthropic", "linkedin", "ads")),
    ("insurance", ("insurance", "geico", "progressive", "state farm", "allstate")),
    ("savings", ("transfer to savings", "savings", "vanguard", "fidelity",
                 "schwab", "robinhood", "coinbase", "401k", "ira")),
    ("subscriptions", ("netflix", "spotify", "hulu", "disney", "apple.com/bill",
                       "prime", "subscription", "membership")),
    ("debt", ("credit card payment", "loan payment", "student loan", "card pmt")),
]


def categorize(description):
    text = (description or "").lower()
    for category, needles in _CATEGORY_RULES:
        if any(needle in text for needle in needles):
            return category
    return "other"


def _find_column(headers, candidates):
    lowered = [(h or "").strip().lower() for h in headers]
    for needle in candidates:
        for i, header in enumerate(lowered):
            if header == needle:
                return i
    for needle in candidates:
        for i, header in enumerate(lowered):
            if needle in header:
                return i
    return None


def _parse_money(raw):
    """'$1,234.56', '(45.00)', '-45.00', '45.00 DR' -> a float."""
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    if text.upper().endswith(("DR", "DB")):
        negative, text = True, text[:-2]
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    if not text or text in ("-", "."):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return -abs(value) if negative else value


def _parse_day(raw):
    text = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def read_bank_csv(text):
    """
    Parse a bank export into transaction dicts. Returns (rows, problems).

    Handles both shapes banks ship: one signed Amount column, or separate
    Debit/Credit columns. Sign convention on the way out is always the same —
    positive is money in, negative is money out — because every downstream sum
    depends on that and a bank that flips it would silently invert your month.
    """
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)

    rows_in = [r for r in reader if any((cell or "").strip() for cell in r)]
    if not rows_in:
        return [], ["That file is empty."]

    headers = rows_in[0]
    date_col = _find_column(headers, _DATE_HEADERS)
    desc_col = _find_column(headers, _DESC_HEADERS)
    amount_col = _find_column(headers, _AMOUNT_HEADERS)
    debit_col = _find_column(headers, _DEBIT_HEADERS)
    credit_col = _find_column(headers, _CREDIT_HEADERS)

    problems = []
    if date_col is None or desc_col is None:
        return [], ["Couldn't find a date and a description column. Headers seen: "
                    + ", ".join(h for h in headers if h)]
    if amount_col is None and debit_col is None and credit_col is None:
        return [], ["Couldn't find an amount column (or a debit/credit pair). "
                    "Headers seen: " + ", ".join(h for h in headers if h)]

    out = []
    for line_no, row in enumerate(rows_in[1:], start=2):
        def cell(index):
            return row[index] if index is not None and index < len(row) else ""

        day = _parse_day(cell(date_col))
        if day is None:
            problems.append("Line " + str(line_no) + ": couldn't read the date "
                            + repr(cell(date_col)[:20]))
            continue

        if amount_col is not None:
            amount = _parse_money(cell(amount_col))
        else:
            debit = _parse_money(cell(debit_col)) or 0
            credit = _parse_money(cell(credit_col)) or 0
            amount = credit - abs(debit)
        if amount is None:
            problems.append("Line " + str(line_no) + ": couldn't read the amount.")
            continue

        description = (cell(desc_col) or "").strip()[:200]
        out.append({"day": day.isoformat(), "description": description,
                    "amount": amount, "category": categorize(description)})
    return out, problems


def import_transactions(conn, source_id, rows):
    """
    Write parsed transactions. Returns how many were new.

    INSERT OR IGNORE against the (source, day, description, amount) uniqueness in
    the schema, so re-importing a statement that overlaps last month's is a no-op
    instead of doubling your dining total.
    """
    added = 0
    for row in rows:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO transactions (source_id, day, description,
               amount, category) VALUES (?,?,?,?,?)""",
            (source_id, row["day"], row["description"], row["amount"], row["category"]))
        added += cursor.rowcount
    conn.commit()
    return added


def money_snapshot(conn, today=None, window_days=30):
    """
    What the money is doing, computed the same way the goals are: arithmetic over
    rows, no interpretation. `momentum.signals()` decides what's worth saying.
    """
    today = today or date.today()
    start = (today - timedelta(days=window_days)).isoformat()
    prior_start = (today - timedelta(days=window_days * 2)).isoformat()

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM transactions WHERE day >= ? ORDER BY day DESC", (prior_start,))]
    if not rows:
        return {"has_data": False}

    window = [r for r in rows if r["day"] >= start]
    prior = [r for r in rows if r["day"] < start]

    income = sum(r["amount"] for r in window if r["amount"] > 0)
    spend = -sum(r["amount"] for r in window if r["amount"] < 0)
    prior_spend = -sum(r["amount"] for r in prior if r["amount"] < 0)

    by_category = {}
    for r in window:
        if r["amount"] < 0:
            by_category[r["category"]] = by_category.get(r["category"], 0) - r["amount"]
    top = sorted(by_category.items(), key=lambda kv: -kv[1])

    prior_by_category = {}
    for r in prior:
        if r["amount"] < 0:
            prior_by_category[r["category"]] = prior_by_category.get(r["category"], 0) - r["amount"]

    # A category that jumped is worth a sentence; one that merely exists is not.
    movers = []
    for category, amount in top:
        was = prior_by_category.get(category, 0)
        if was >= 40 and amount >= was * 1.4 and amount - was >= 60:
            movers.append({"category": category, "now": amount, "was": was,
                           "delta": amount - was})

    saved = sum(r["amount"] for r in window
                if r["category"] == "savings" and r["amount"] < 0)
    return {
        "has_data": True,
        "window_days": window_days,
        "income": income,
        "spend": spend,
        "net": income - spend,
        "prior_spend": prior_spend,
        "saved": -saved,
        "save_rate": (-saved / income) if income else 0.0,
        "top_categories": top[:6],
        "movers": movers[:3],
        "count": len(window),
        # Months you could cover at this burn if the income stopped. The most
        # useful single number in personal finance and the least often looked at.
        "monthly_burn": spend * (30 / window_days),
    }
