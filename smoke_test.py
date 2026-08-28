"""
AJ 2.0 — smoke test.

    python3 smoke_test.py

Exercises every route against a freshly seeded demo database and asserts the
status codes, including the ones that are supposed to fail. Not a unit-test
suite: it is the check that answers "does the app still work end to end", which
is the question that actually matters for a single-file-database app that one
person depends on every morning.

Three things it deliberately covers beyond the happy path:

  * the 400s — CSRF, unknown status, a metric that isn't a number, a feed kind
    that doesn't exist. A form that silently accepts garbage is worse than one
    that rejects it.
  * the cold boot — an empty database must onboard, not 500. This is the path a
    fresh deploy takes, and the one nobody exercises until it breaks in public.
  * the feed failure paths — an unreachable calendar and an unparseable CSV must
    report themselves rather than throwing.

Exits non-zero on the first category of failure, so CI can gate on it.
"""

import os
import sys
import tempfile

# A scratch database, so running the tests never touches a real one.
os.environ.setdefault("AJ_DB", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import aj_db          # noqa: E402  (import order matters: AJ_DB must be set first)
import app as ajapp   # noqa: E402

FAILURES = []


def check(label, response, expect=(200, 302)):
    code = response.status_code
    ok = code in expect
    print(("  ok   " if ok else "  FAIL ") + str(code) + "  " + label)
    if not ok:
        FAILURES.append((label, code, response.data[:600].decode("utf8", "replace")))
    return response


def client():
    ajapp.app.config["TESTING"] = True
    c = ajapp.app.test_client()
    with c.session_transaction() as session:
        session["_csrf"] = "test-token"
    return c


def test_cold_boot():
    """An empty database must onboard rather than 500 — the fresh-deploy path."""
    print("--- cold boot on an empty database ---")
    if aj_db.DB_FILE.exists():
        aj_db.DB_FILE.unlink()
    aj_db.create_tables()
    c = client()
    check("GET / with no profile redirects to onboarding", c.get("/"), (302,))
    check("GET /onboard", c.get("/onboard"), (200,))
    with c.session_transaction() as session:
        session["_csrf"] = "t"
    check("POST /onboard", c.post("/onboard", data={
        "_csrf": "t", "name": "Test", "north_star": "Ship the thing.",
        "identity": "The operator who ships.", "values_text": "Health first.",
        "stakes": "Wasted years.", "horizon_years": "3", "intensity": "90"}), (302,))
    check("GET / after onboarding", c.get("/"), (200,))


def test_routes():
    print("--- GET ---")
    aj_db.create_tables()
    aj_db.seed_demo()
    c = client()
    for path in ("/", "/goals", "/goals/1", "/goals/2", "/goals/3", "/goals/new",
                 "/checkin", "/warroom", "/review", "/settings", "/life"):
        check("GET " + path, c.get(path))
    check("GET /nope", c.get("/nope"), (404,))
    check("GET /goals/9999", c.get("/goals/9999"), (404,))

    print("--- POST ---")
    t = "test-token"
    check("POST /brief/refresh", c.post("/brief/refresh", data={"_csrf": t}))
    check("POST /checkin", c.post("/checkin", data={
        "_csrf": t, "log": "Shipped the driver flow.", "deep_hours": "5",
        "energy": "4", "blockers": ""}))
    check("POST /warroom", c.post("/warroom", data={
        "_csrf": t, "message": "Should I drop the rebuild?"}))
    check("POST /review", c.post("/review", data={"_csrf": t}))
    check("POST /goals/new (pressure test)", c.post("/goals/new", data={
        "_csrf": t, "action": "test", "title": "Grow the business",
        "domain": "business"}), (200,))
    check("POST /goals/new (create)", c.post("/goals/new", data={
        "_csrf": t, "action": "create", "title": "Land 3 enterprise carriers",
        "domain": "business", "metric_name": "carriers", "unit": "",
        "start_value": "0", "target_value": "3", "deadline": "2026-12-01",
        "why": "Proof."}))
    check("POST /goals/5/metric", c.post("/goals/5/metric", data={
        "_csrf": t, "value": "1", "note": "First one."}))
    check("POST /goals/5/plan", c.post("/goals/5/plan", data={"_csrf": t}))
    check("POST /goals/5/status", c.post("/goals/5/status", data={
        "_csrf": t, "status": "paused"}))
    check("POST /commitments/add", c.post("/commitments/add", data={
        "_csrf": t, "text": "Call ten brokers", "due_date": "2026-12-01",
        "goal_id": "1"}))
    check("POST /commitments/1/resolve", c.post("/commitments/1/resolve", data={
        "_csrf": t, "status": "kept"}))
    check("POST /milestones/add", c.post("/milestones/add", data={
        "_csrf": t, "goal_id": "1", "title": "First partner signed",
        "target_date": "2026-09-30"}))
    check("POST /milestones/1/toggle", c.post("/milestones/1/toggle", data={"_csrf": t}))
    check("POST /settings", c.post("/settings", data={
        "_csrf": t, "intensity": "85", "north_star": "Same.", "identity": "Operator",
        "values_text": "Health first.", "stakes": "Wasted years.",
        "horizon_years": "3"}))

    print("--- rejections (these are supposed to fail) ---")
    check("POST with no CSRF token", c.post("/brief/refresh", data={}), (400,))
    check("POST an unknown goal status", c.post("/goals/1/status", data={
        "_csrf": t, "status": "haha"}), (400,))
    check("POST a metric that isn't a number", c.post("/goals/1/metric", data={
        "_csrf": t, "value": "banana"}), (400,))
    check("POST an unknown feed kind", c.post("/life/sources/add", data={
        "_csrf": t, "kind": "telepathy", "name": "x"}), (400,))


def test_feeds():
    """Feed failures must report themselves, not throw."""
    print("--- feeds ---")
    c = client()
    t = "test-token"
    check("import a bank CSV", c.post("/life/bank/import", data={
        "_csrf": t, "pasted": "Date,Description,Amount\n08/15/2026,COFFEE,-4.50\n"}))
    check("re-import the same rows (must not double-count)", c.post(
        "/life/bank/import", data={
            "_csrf": t, "pasted": "Date,Description,Amount\n08/15/2026,COFFEE,-4.50\n"}))
    check("import a file that isn't a bank export", c.post("/life/bank/import", data={
        "_csrf": t, "pasted": "not,a,bank,file\n1,2,3,4\n"}))
    check("add a calendar with a malformed URL", c.post("/life/sources/add", data={
        "_csrf": t, "kind": "ics", "name": "Broken", "url": "notaurl"}))
    check("add a calendar that can't be reached", c.post("/life/sources/add", data={
        "_csrf": t, "kind": "ics", "name": "Dance",
        "url": "https://example.invalid/cal.ics"}))
    check("sync every calendar", c.post("/life/sync", data={"_csrf": t}))
    check("GET /life after the failures", c.get("/life"), (200,))
    check("GET / after the failures", c.get("/"), (200,))


def main():
    test_cold_boot()
    test_routes()
    test_feeds()
    print()
    if FAILURES:
        print(str(len(FAILURES)) + " FAILED")
        for label, code, body in FAILURES:
            print("=" * 70)
            print(label, "->", code)
            print(body)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
