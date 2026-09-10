"""Profile the routes the user reports as slow, through the real Flask
test client, counting get_db() calls and SQL statements per request."""
import os, sys, time, sqlite3, cProfile, pstats, io as _io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db

# --- instrumentation -------------------------------------------------
STATS = {"get_db": 0, "sql": 0, "sql_time": 0.0, "connect_time": 0.0}

_real_get_db = db.get_db
_real_get_global_db = db.get_global_db


class _CountingConn:
    """Proxy so sqlite3.Connection's read-only attributes can still be
    counted — delegates everything except execute()."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, q, p=()):
        t = time.perf_counter()
        try:
            return self._conn.execute(q, p)
        finally:
            STATS["sql"] += 1
            STATS["sql_time"] += time.perf_counter() - t

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _wrap_conn(conn):
    return _CountingConn(conn)


def counting_get_db():
    STATS["get_db"] += 1
    t = time.perf_counter()
    c = _real_get_db()
    STATS["connect_time"] += time.perf_counter() - t
    return _wrap_conn(c)


def counting_get_global_db():
    STATS["get_db"] += 1
    t = time.perf_counter()
    c = _real_get_global_db()
    STATS["connect_time"] += time.perf_counter() - t
    return _wrap_conn(c)


def instrument():
    db.get_db = counting_get_db
    db.get_global_db = counting_get_global_db


def reset():
    for k in STATS:
        STATS[k] = 0 if k in ("get_db", "sql") else 0.0


# --- client ----------------------------------------------------------
def make_client():
    import application
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"] = "admin"
        s["role"] = "admin"
        s["auth_source"] = "manual"
        s["site"] = "SCN2"
    return c


def timed(client, label, url, profile=False):
    reset()
    pr = cProfile.Profile() if profile else None
    if pr:
        pr.enable()
    t0 = time.perf_counter()
    resp = client.get(url)
    elapsed = time.perf_counter() - t0
    if pr:
        pr.disable()
    print(f"{label:<34} {resp.status_code}  {elapsed:7.3f}s  "
          f"get_db={STATS['get_db']:>6}  sql={STATS['sql']:>7}  "
          f"connect={STATS['connect_time']:6.3f}s  sqlexec={STATS['sql_time']:6.3f}s")
    if pr:
        s = _io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(28)
        print(s.getvalue()[:5000])
    return elapsed, dict(STATS)


if __name__ == "__main__":
    instrument()
    client = make_client()
    profile = "--profile" in sys.argv
    print("=" * 110)
    ams = [r["login"] for r in db.get_roster_by_roles(["am"])][:3]
    oms = [r["login"] for r in db.get_roster_by_roles(["om"])][:1]
    results = {}
    results["overview_am1"] = timed(client, "AM Overview (am1)", f"/overview?am={ams[0]}")
    results["overview_am2"] = timed(client, "AM Overview (am2, switch)", f"/overview?am={ams[1]}")
    results["overview_om"] = timed(client, "OM Overview (whole tree)", f"/overview?am={oms[0]}")
    results["olr_detail"] = timed(client, "OLR detail (am1)", f"/ld-management/olr/{ams[0]}", profile=profile)
    results["olr_index"] = timed(client, "OLR index", "/ld-management/olr")
    results["rankings"] = timed(client, "Rankings tab", "/scorecards?tab=manager_rankings", profile=profile)
    results["reporting"] = timed(client, "Reporting tab", "/scorecards?tab=reporting")
    print("=" * 110)
