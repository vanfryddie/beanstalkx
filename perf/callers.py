"""Which db.py functions actually drive the get_db() storm, and how many
SQL statements each is responsible for."""
import os, sys, time, traceback, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

CALLERS = collections.Counter()
_real = db.get_db


def tracking_get_db():
    stack = traceback.extract_stack()
    caller = "?"
    for frame in reversed(stack[:-1]):
        if frame.filename.endswith("db.py") or frame.filename.endswith("application.py"):
            caller = f"{os.path.basename(frame.filename)}:{frame.name}"
            break
    CALLERS[caller] += 1
    return _real()


db.get_db = tracking_get_db
import application
application.application.config["TESTING"] = True
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"

url = sys.argv[1] if len(sys.argv) > 1 else "/scorecards?tab=manager_rankings"
CALLERS.clear()
t = time.perf_counter()
r = c.get(url)
print(f"{url} -> {r.status_code} in {time.perf_counter()-t:.3f}s, {sum(CALLERS.values())} get_db calls\n")
for name, n in CALLERS.most_common(18):
    print(f"  {n:>6}  {name}")
