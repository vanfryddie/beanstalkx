"""Run the app against a real PostgreSQL server, not SQLite.

This exists because a schema change that worked perfectly on SQLite took
production down on Postgres: the PGConnection compatibility layer splits
scripts on ';', so a SQL comment containing one was torn in half. Nothing
in a SQLite-only test suite can catch that class of bug.

Point DATABASE_URL at a scratch Postgres and run this.
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if not os.environ.get("DATABASE_URL"):
    sys.exit("set DATABASE_URL to a scratch Postgres first")

import db
import application

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


print("engine in use:", "POSTGRES" if db.USE_POSTGRES else "SQLITE")
check("running against Postgres, not SQLite", db.USE_POSTGRES)

print("\n1. Schema creation (the exact thing that broke production)")
db.set_current_site("SCN2")
try:
    db.init_db()
    db.init_global_db()
    check("init_db() + init_global_db() succeed", True)
except Exception as e:
    check("init_db() + init_global_db() succeed", False, f"{type(e).__name__}: {e}")

try:
    db.init_db()
    check("init_db() is safely re-runnable", True)
except Exception as e:
    check("init_db() is safely re-runnable", False, f"{type(e).__name__}: {e}")

print("\n2. Every index the app declares actually exists")
conn = db.get_db()
rows = conn.execute(
    "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
).fetchall()
conn.close()
names = {r["indexname"] for r in rows}
for want in ("idx_ti_am_section", "idx_xt_hours_supervisor_lower",
             "idx_ti_section", "idx_ti_am", "idx_olr_login_week"):
    check(f"index {want} created", want in names)

print("\n3. Seed a small org and render every route the change touched")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seed import seed
seed(n_som=1, n_om_per_som=2, n_am_per_om=3, n_assoc_per_am=8, weeks=4)

application.application.config["TESTING"] = True
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"

ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
routes = [
    ("login", "/"),
    ("AM Overview", f"/overview?am={ams[0]}"),
    ("AM Overview (switch)", f"/overview?am={ams[1]}"),
    ("Reporting", "/scorecards?tab=reporting"),
    ("Rankings", "/scorecards?tab=manager_rankings"),
    ("SOM/OM tab", "/scorecards?tab=som_om"),
    ("OLR index", "/ld-management/olr"),
    ("OLR detail", f"/ld-management/olr/{ams[0]}"),
    ("Indirect Roles", "/ld-management/indirect-roles"),
    ("IR overview tab", "/ld-management/indirect-roles?tab=overview"),
    ("Trainer Overview", "/ld-management/trainer-overview"),
    ("L&D Settings", "/ld-management/settings"),
    ("Regional Overview", "/regional-overview"),
    ("Weekly plan", "/weekly-plan"),
]
for name, url in routes:
    t = time.perf_counter()
    try:
        r = c.get(url)
        ok = r.status_code in (200, 302)
        check(f"{name} -> {r.status_code} ({time.perf_counter()-t:.3f}s)", ok)
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {str(e)[:200]}")

print("\n4. Write path, and a read of it in a later request")
probe = "pgprobe"
r = c.post("/ld-management/settings/roles/assign",
           data={"login": probe, "role": "am", "shift": "early", "department": "Inbound"},
           follow_redirects=True)
check("role assignment POST", r.status_code == 200, f"got {r.status_code}")
check("the write is readable afterwards", db.get_user_role(probe) == "am",
      f"got {db.get_user_role(probe)!r}")
conn = db.get_db(); conn.execute("DELETE FROM user_roles WHERE login=?", (probe,)); conn.commit(); conn.close()

print("\n5. The connection pool survives repeated requests (no leak)")
pool = db._get_pg_pool()
for i in range(40):
    resp = c.get("/scorecards?tab=reporting")
    if resp.status_code != 200:
        check(f"request {i} ok", False, f"status {resp.status_code}")
        break
else:
    check("40 consecutive requests all served", True)
used = len(getattr(pool, "_used", {}))
check("no connections left checked out after teardown", used == 0, f"{used} still held")

print("\n" + "=" * 62)
print("ALL POSTGRES CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
