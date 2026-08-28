"""
AJ 2.0 — configuration.

Same rule as the rest of the lab: anything that differs between a laptop and a
deployed box comes from the environment, and production refuses to boot without
the values it needs instead of quietly substituting a development default.

One thing here is specific to a personal app. This database is the most candid
document you own — your dreams, your excuses, your misses. If you put it on the
public internet, put a passcode on it. `validate()` treats a production boot with
no AJ_PASSCODE as a fatal misconfiguration rather than a warning nobody reads.
"""

import os
import secrets


def _bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    ENV = os.environ.get("AJ_ENV", "development")
    IS_PROD = ENV == "production"

    DEBUG = _bool("AJ_DEBUG", default=not IS_PROD)

    # Minted per-process in development, which logs you out on restart. That is
    # the correct trade against a constant that ends up committed.
    SECRET_KEY = os.environ.get("SECRET_KEY") or (None if IS_PROD else secrets.token_hex(32))

    # The single gate on the whole app. Unset locally — localhost is the auth.
    PASSCODE = os.environ.get("AJ_PASSCODE", "")

    DB_PATH = os.environ.get("AJ_DB", "aj.db")

    # The zone you actually live in. Calendar feeds carry timestamps in UTC or in
    # the studio's zone; they get converted into this one for display. Unset means
    # "whatever the machine thinks", which is right on a laptop and wrong on a
    # server in UTC — so set it before you deploy.
    TZ = os.environ.get("AJ_TZ", "")

    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", default=IS_PROD)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Long by design: re-authenticating your own coach every morning is friction
    # that ends with you not opening it.
    PERMANENT_SESSION_LIFETIME = int(os.environ.get("SESSION_DAYS", "30")) * 86400

    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_KB", "512")) * 1024

    # Mailbox, for triage only. These live here and never in the database: a
    # password in a SQLite file is a password in every backup of that file.
    # Use an app password from a mailbox you'd be willing to lose, not your
    # primary identity account — and revoke it from the same settings page.
    IMAP_HOST = os.environ.get("AJ_IMAP_HOST", "")
    IMAP_USER = os.environ.get("AJ_IMAP_USER", "")
    IMAP_PASSWORD = os.environ.get("AJ_IMAP_PASSWORD", "")
    IMAP_FOLDER = os.environ.get("AJ_IMAP_FOLDER", "INBOX")

    @classmethod
    def mail_configured(cls):
        return bool(cls.IMAP_HOST and cls.IMAP_USER and cls.IMAP_PASSWORD)

    # Model spend guards. The per-hour cap blunts a runaway loop; the monthly cap
    # is the one that keeps this a $6 habit instead of a surprise.
    AI_CALLS_PER_HOUR = int(os.environ.get("AI_CALLS_PER_HOUR", "40"))
    AI_MONTHLY_CAP_CENTS = int(float(os.environ.get("AI_MONTHLY_CAP_USD", "25")) * 100)

    # How much history the war room replays into a prompt. Enough for continuity,
    # bounded so a year of chat doesn't turn every message into a long context.
    CHAT_HISTORY_TURNS = int(os.environ.get("CHAT_HISTORY_TURNS", "12"))


def validate(config):
    """Return every production misconfiguration at once, so one restart fixes all."""
    problems = []
    if config.IS_PROD:
        if not config.SECRET_KEY:
            problems.append("SECRET_KEY is required when AJ_ENV=production.")
        if not config.PASSCODE:
            problems.append("AJ_PASSCODE is required when AJ_ENV=production — this "
                            "database holds your goals, your misses, and your excuses.")
        if config.DEBUG:
            problems.append("AJ_DEBUG must be off in production — the Werkzeug "
                            "debugger is remote code execution.")
        if not config.SESSION_COOKIE_SECURE:
            problems.append("SESSION_COOKIE_SECURE must be on in production (serve over HTTPS).")
    return problems
