"""Indirect Roles and the two Ambassador metrics must score at site and
regional scope, not only per manager.

Those three have no tracked_items importer — they are computed by
dedicated engines keyed on (department, shift) pairs. A site-wide call
used to pass no pairs, so it fell through to db.scorecard() over
staffing_indirect_coverage / staffing_instructor, which nothing has ever
written. The result was "no data" on Reporting and Regional Overview for
metrics that showed real numbers on a manager's own card.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db, application

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: fails.append(name)

db.set_current_site("SCN2")
db.init_db()

print("1. The staffing sections these metrics used to fall back to are empty")
conn = db.get_db()
for section in ("staffing_indirect_coverage", "staffing_instructor", "staffing_xt"):
    n = conn.execute("SELECT COUNT(*) AS n FROM tracked_items WHERE section=?", (section,)).fetchone()["n"]
    check(f"{section} has no rows (no importer writes it)", n == 0, f"{n} rows")
conn.close()

print("\n2. With ambassador and Indirect Roles data loaded, site scope scores them")
from seed_ambassadors_ir import seed as seed_amb_ir
seed_amb_ir("SCN2")

with application.application.test_request_context("/"):
    categories, _overall = application._build_scorecard_categories(None, None)
    by_key = {c["key"]: c["card"] for c in categories}

    for key, label in (("indirect_roles", "Indirect Roles"),
                       ("instructor_mgmt", "Ambassador Availability"),
                       ("ambassador_readiness", "Ambassador Readiness")):
        score = by_key[key]["pct_ok"]
        check(f"{label} is scored at site scope", score is not None,
              "still None — the engine did not run")

    # Independent cross-check: the site's Indirect Roles number must agree
    # with the Indirect Role engine's own site-wide summary, which is
    # computed by a completely separate function.
    engine_pct = db.summarize_ir_overview(db.compute_ir_overview())["overall_pct"]
    site_pct = by_key["indirect_roles"]["pct_ok"]
    check("site Indirect Roles agrees with summarize_ir_overview",
          engine_pct is not None and abs(site_pct - engine_pct) <= 1,
          f"dashboard {site_pct} vs engine {engine_pct}")

print("\n3. A metric with nothing configured reads as no data, never a perfect score")
conn = db.get_db()
conn.execute("DELETE FROM ambassadors")
conn.execute("UPDATE ambassador_targets SET target=0")
conn.commit(); conn.close()

with application.application.test_request_context("/"):
    categories, _ = application._build_scorecard_categories(None, None)
    by_key = {c["key"]: c["card"] for c in categories}
    avail = by_key["instructor_mgmt"]["pct_ok"]
    check("Ambassador Availability with no ambassadors and no target is None",
          avail is None, f"got {avail} — a false perfect score inflates Total L&D")

print("\n4. Manager scope still works and is unchanged in kind")
seed_amb_ir("SCN2")
with application.application.test_request_context("/"):
    managers = [r["login"] for r in db.get_roster_by_roles(["am"])][:3]
    for login in managers:
        scope = application._resolve_am_scope(login)
        categories, _ = application._build_scorecard_categories(scope, None, single_am_login=login)
        keys = {c["key"] for c in categories}
        check(f"{login} still gets all seven categories", len(keys) == 7, str(sorted(keys)))

print("\n" + "=" * 60)
print("ALL SITE-SCOPE CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
