"""End-to-end: the multi-site loop and a real write-then-read request."""
import os, sys, time
sys.path.insert(0, "/home/user/beanstalkx")
import db, application

application.application.config["TESTING"] = True
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("  " + detail if detail and not cond else ""))
    if not cond: fails.append(name)

print("\nRegional Overview (the one route that switches site mid-request)")
t = time.perf_counter()
r = c.get("/regional-overview")
print(f"  -> {r.status_code} in {time.perf_counter()-t:.3f}s")
check("regional overview renders", r.status_code == 200)
check("site context restored to SCN2 after the loop", db.get_current_site() == "SCN2",
      f"got {db.get_current_site()}")

print("\nA second Regional Overview request still works (connections were released)")
r2 = c.get("/regional-overview")
check("repeatable", r2.status_code == 200)
check("byte-identical to the first render", r2.get_data() == r.get_data())

print("\nWrite-then-read through a real request (role assignment)")
probe = "e2eprobe"
r = c.post("/ld-management/settings/roles/assign", data={
    "login": probe, "role": "am", "shift": "early", "department": "Inbound",
}, follow_redirects=True)
check("role POST accepted", r.status_code == 200, f"got {r.status_code}")
check("the write is visible on a later request", db.get_user_role(probe) == "am",
      f"got {db.get_user_role(probe)!r}")

r = c.get(f"/overview?am={probe}")
check("newly created AM's overview renders", r.status_code == 200)

conn = db.get_db(); conn.execute("DELETE FROM user_roles WHERE login=?", (probe,)); conn.commit(); conn.close()

print("\n" + "=" * 55)
print("ALL E2E CHECKS PASSED" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
