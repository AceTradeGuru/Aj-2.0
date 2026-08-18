-- AJ 2.0 — schema.
--
-- One person, one database. There is no org_id here and there never will be:
-- this is a coach that knows one life in detail, not a product with tenants.
--
-- The design rule behind every table: AJ is only allowed to challenge you with
-- facts it can point at. So the things it argues from — what you committed to,
-- when it was due, whether you did it, where the number actually is — are rows
-- with dates on them, not sentences in a chat log. A coach whose memory is a
-- transcript is a coach that can be flattered into agreeing with you.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------- profile --
-- Exactly one row, id = 1. The answers here are what AJ reads before it says
-- anything at all: who you are, where you're going, and what you're willing to
-- be held to.
CREATE TABLE IF NOT EXISTS profile (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    name            TEXT    NOT NULL,
    -- The dream, stated once, in your words. Long horizon, no metric required.
    north_star      TEXT    NOT NULL DEFAULT '',
    -- Who you're becoming. AJ quotes this back when your calendar disagrees.
    identity        TEXT    NOT NULL DEFAULT '',
    -- Non-negotiables. AJ will not propose a plan that violates one.
    values_text     TEXT    NOT NULL DEFAULT '',
    -- What you're running from. The honest half of motivation.
    stakes          TEXT    NOT NULL DEFAULT '',
    horizon_years   INTEGER NOT NULL DEFAULT 3,
    -- 0-100. How hard AJ pushes. 20 = supportive, 80 = shark tank.
    intensity       INTEGER NOT NULL DEFAULT 70,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    onboarded_at    TEXT
);

-- ----------------------------------------------------------------- goals --
-- A goal is a dream with a number and a date on it. Both columns are required
-- on purpose: "grow the business" is not a goal, it is a mood.
CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    -- Why this one matters. Feeds the voice, not the math.
    why          TEXT NOT NULL DEFAULT '',
    domain       TEXT NOT NULL DEFAULT 'business',   -- business | money | craft | body | mind | people
    metric_name  TEXT NOT NULL,                      -- "MRR", "lbs", "shipped features"
    unit         TEXT NOT NULL DEFAULT '',           -- "$", "lbs", "" — display only
    start_value  REAL NOT NULL DEFAULT 0,
    target_value REAL NOT NULL,
    -- Denormalized from the newest metric_log row so a list page is one query.
    current_value REAL NOT NULL DEFAULT 0,
    start_date   TEXT NOT NULL DEFAULT (date('now')),
    deadline     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',     -- active | won | missed | paused | dropped
    -- What it costs you to miss this. AJ uses it when you go quiet.
    stakes       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status, deadline);

-- ------------------------------------------------------------ milestones --
-- The decomposition. AJ writes these on request; you can overrule any of them.
CREATE TABLE IF NOT EXISTS milestones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    target_date TEXT    NOT NULL,
    done_at     TEXT,
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_milestones_goal ON milestones(goal_id, sort);

-- ----------------------------------------------------------- commitments --
-- The accountability ledger, and the most important table in the file.
--
-- A commitment is a promise with a due date. It is never edited into oblivion:
-- an overdue one is resolved as kept, missed, or dropped, and the record stays.
-- Your credibility score is computed from these rows, which is what makes it
-- something other than a feeling.
CREATE TABLE IF NOT EXISTS commitments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id      INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    text         TEXT    NOT NULL,
    due_date     TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',    -- open | kept | missed | dropped
    -- 'you' when you set it, 'aj' when the coach assigned it. Kept separate
    -- because follow-through on your own promises reads differently.
    source       TEXT    NOT NULL DEFAULT 'you',
    -- Your stated reason when it slips. AJ reads these back to you in reviews;
    -- the same excuse three times is a pattern, not an excuse.
    excuse       TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_commitments_status ON commitments(status, due_date);
CREATE INDEX IF NOT EXISTS idx_commitments_goal ON commitments(goal_id);

-- -------------------------------------------------------------- check-in --
-- One row per day, at most. The daily surface of the whole system.
CREATE TABLE IF NOT EXISTS checkins (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL UNIQUE,             -- date('now'), local
    energy      INTEGER NOT NULL DEFAULT 3,          -- 1-5, self-reported
    deep_hours  REAL    NOT NULL DEFAULT 0,          -- hours on the goals, not the inbox
    log         TEXT    NOT NULL DEFAULT '',         -- what you actually did
    blockers    TEXT    NOT NULL DEFAULT '',
    -- AJ's grade for the day, 0-100, and the debrief that justifies it.
    grade       INTEGER,
    debrief     TEXT    NOT NULL DEFAULT '',
    engine      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_checkins_day ON checkins(day DESC);

-- ------------------------------------------------------------ metric log --
-- Every reading of every goal's number, append-only. Pace, trend, and the
-- "you haven't moved this in 12 days" callout all read from here.
CREATE TABLE IF NOT EXISTS metric_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id  INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    value    REAL    NOT NULL,
    note     TEXT    NOT NULL DEFAULT '',
    at       TEXT    NOT NULL DEFAULT (datetime('now')),
    day      TEXT    NOT NULL DEFAULT (date('now'))
);
CREATE INDEX IF NOT EXISTS idx_metric_goal ON metric_log(goal_id, at DESC);

-- -------------------------------------------------------------- war room --
-- The conversation. Kept because continuity is most of what makes a coach feel
-- like a person, and trimmed on read so a long history can't blow the context.
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    role    TEXT NOT NULL,                            -- you | aj
    text    TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'chat',             -- chat | pressure_test
    engine  TEXT NOT NULL DEFAULT '',
    at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_at ON messages(at DESC);

-- --------------------------------------------------------------- briefs ---
-- Generated documents: the morning brief and the weekly board meeting. Stored
-- so today's brief is stable no matter how many times you reload the page, and
-- so last week's review can be quoted at you this week.
CREATE TABLE IF NOT EXISTS briefs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                         -- daily | weekly
    day        TEXT NOT NULL,                         -- daily: the day. weekly: week-ending Sunday.
    body       TEXT NOT NULL,
    engine     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, day)
);

-- ----------------------------------------------------------- ai spending --
-- What the model cost, by month, from the usage the API reports. A personal app
-- with an unbounded API bill is a personal app you turn off after one surprise.
CREATE TABLE IF NOT EXISTS ai_spend (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    month  TEXT NOT NULL,                             -- YYYY-MM
    cents  REAL NOT NULL,
    feature TEXT NOT NULL DEFAULT '',
    at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_spend_month ON ai_spend(month);
