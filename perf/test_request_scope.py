"""Correctness tests for the request-scoped connection + memo layer.

These target the failure modes the change could plausibly introduce:
stale reads after a write in the same request, a connection surviving
across a mid-request site switch, a leaked connection when a view
raises, unchanged behavior outside a request, and no sharing between
threads.
"""
import os, sys, threading, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, application

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def client():
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"
    return c


print("\n1. Same connection is reused within a request, and close() is a no-op")
db.begin_request_scope()
try:
    a, b = db.get_db(), db.get_db()
    check("get_db() returns the same object twice", a is b)
    a.close()
    row = b.execute("SELECT 1 AS x").fetchone()
    check("connection still usable after close()", row["x"] == 1)
    g1, g2 = db.get_global_db(), db.get_global_db()
    check("get_global_db() also reused", g1 is g2)
    check("global connection is distinct from site connection", g1 is not a)
finally:
    db.end_request_scope()

print("\n2. Outside a request scope, behavior is unchanged (fresh connection per call)")
c1, c2 = db.get_db(), db.get_db()
check("distinct connections outside a scope", c1 is not c2)
check("not wrapped outside a scope", not isinstance(c1, db._RequestConnection))
c1.close(); c2.close()

print("\n3. A write in the same request invalidates the memo (no stale reads)")
db.begin_request_scope()
try:
    probe = "perftest_user"
    conn = db.get_db()
    conn.execute("DELETE FROM user_roles WHERE login=?", (probe,))
    conn.commit()
    before = db.get_user_role(probe)
    check("memoized read of absent row returns None", before is None)
    db.set_user_role(probe, "am", "test", shift="early", department="Inbound")
    after = db.get_user_role(probe)
    check("read AFTER write in same request sees the write", after == "am",
          f"got {after!r}, expected 'am'")

    shift, dept = db.get_user_shift_and_department(probe)
    check("shift/department also fresh after write", shift == "early" and dept == "Inbound",
          f"got {shift!r}/{dept!r}")

    # And a write issued through a cursor (the CSV importers' pattern)
    _ = db.get_user_role(probe)          # re-warm the memo
    cur = db.get_db().cursor()
    cur.execute("UPDATE user_roles SET role='om' WHERE login=?", (probe,))
    db.get_db().commit()
    check("write via conn.cursor() also invalidates memo",
          db.get_user_role(probe) == "om", f"got {db.get_user_role(probe)!r}")

    db.get_db().execute("DELETE FROM user_roles WHERE login=?", (probe,))
    db.get_db().commit()
finally:
    db.end_request_scope()

print("\n4. Switching site mid-request swaps the connection and clears the memo")
db.begin_request_scope()
try:
    db.set_current_site("SCN2")
    first = db.get_db()
    check("site_code recorded on the connection", first.site_code == "SCN2")
    db.set_current_site("ZZZ9")
    second = db.get_db()
    check("a different site gets a different connection", second is not first)
    check("new connection carries the new site", second.site_code == "ZZZ9")
    db.set_current_site("SCN2")
    third = db.get_db()
    check("switching back opens again for the original site", third.site_code == "SCN2")
    check("only one site connection is held at a time",
          db._scope()["site"] is third)
finally:
    db.end_request_scope()
    db.set_current_site("SCN2")

print("\n5. Scope is released even when a view raises (no pooled-connection leak)")
app = application.application


@app.route("/__perftest_boom")
def _boom():
    db.get_db()  # open a scoped connection, then fail
    raise RuntimeError("intentional")


c = client()
# Let Flask turn the error into a 500 rather than re-raising it into the
# test, so this exercises the same path a real failing request takes.
app.config["TESTING"] = False
app.config["PROPAGATE_EXCEPTIONS"] = False
try:
    resp = c.get("/__perftest_boom")
    check("route raised a 500 as expected", resp.status_code == 500, f"got {resp.status_code}")
finally:
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = None
check("scope released after the exception", db._scope() is None)

print("\n6. Two threads never share a scope or a connection")
seen = {}


def worker(tag):
    db.begin_request_scope()
    try:
        seen[tag] = db.get_db()
        import time; time.sleep(0.05)
        seen[tag + "_again"] = db.get_db()
    finally:
        db.end_request_scope()


t1 = threading.Thread(target=worker, args=("a",))
t2 = threading.Thread(target=worker, args=("b",))
t1.start(); t2.start(); t1.join(); t2.join()
check("thread A reused its own connection", seen["a"] is seen["a_again"])
check("thread B reused its own connection", seen["b"] is seen["b_again"])
check("threads did not share a connection", seen["a"] is not seen["b"])
check("no scope leaks out of the worker threads", db._scope() is None)

print("\n7. Memo is per-site, not global")
# Give the second site a real (empty) schema, so this compares two
# working databases rather than tripping over a missing table.
db.set_current_site("ZZZ9")
db.init_db()
db.set_current_site("SCN2")

db.begin_request_scope()
try:
    db.set_current_site("SCN2")
    here = db.get_user_role("am000000")
    check("SCN2 has the seeded manager", here == "am", f"got {here!r}")
    db.set_current_site("ZZZ9")
    other = db.get_user_role("am000000")
    check("a memoized lookup does not bleed across sites", other is None,
          f"got {other!r} from ZZZ9 — should not see SCN2's row")
    db.set_current_site("SCN2")
    back = db.get_user_role("am000000")
    check("switching back reads the original site again", back == "am", f"got {back!r}")
finally:
    db.end_request_scope()
    db.set_current_site("SCN2")

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    sys.exit(1)
print("ALL CHECKS PASSED")
