"""The seven scorecard drill-downs.

Each must open with the same header, answer "who is flagging and why"
before showing a table, and never contradict its own score — an "All
clear" banner on a failing metric is the specific bug this guards, and
it shipped once because Cross-Training is scored from xt_hours while
the flagging list was built from tracked_items.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import application, db

KEYS = ["safety_compliance", "de_tech", "indirect_roles", "cross_training",
        "instructor_mgmt", "ambassador_readiness", "bts_compliance"]

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: fails.append(name)

application.application.config["TESTING"] = True
client = application.application.test_client()
with client.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"

db.set_current_site("SCN2")
am = [r["login"] for r in db.get_roster_by_roles(["am"])][0]

print("Every drill-down opens, and opens the same way")
bodies = {}
for key in KEYS:
    resp = client.get(f"/api/category-detail/{key}?am={am}")
    body = resp.get_data(as_text=True)
    bodies[key] = body
    check(f"{key} responds 200", resp.status_code == 200, str(resp.status_code))
    check(f"{key} uses the shared header", body.count('<header class="dd-head">') == 1,
          f'{body.count(chr(60) + "header class=" + chr(34) + "dd-head" + chr(34) + chr(62))} headers')

print("\nA drill-down never contradicts its own score")
for key in KEYS:
    body = bodies[key]
    match = re.search(r'dash-figure dash-figure-lg">([0-9.]+)', body)
    if not match:
        continue
    score = float(match.group(1))
    all_clear = "All clear" in body
    if all_clear:
        check(f"{key} claims all-clear only at 100%", score >= 100.0,
              f"says All clear at {score}%")
    else:
        check(f"{key} at {score}% shows what is flagging", '"dd-group' in body or score >= 100.0,
              "below target with nothing listed")

print("\nThe behaviour attached to these fragments is still wired")
safety = bodies["safety_compliance"]
check("record table kept its id", 'id="catRecordTable"' in safety)
check("rows kept their selection hooks", 'class="record-row' in safety and "data-login=" in safety)
xt = bodies["cross_training"]
for hook in ("xtFilterSearch", "xtRecordTable", "xt-record-row", "xt-drop-preset"):
    check(f"cross-training kept {hook}", hook in xt)

print("\nNo light-theme colours are hardcoded back into the fragments")
import glob
for path in glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "*.html")):
    text = open(path, encoding="utf-8").read()
    bad = re.findall(r"background:\s*#(?:f|e)[0-9a-fA-F]{5}", text)
    check(f"{os.path.basename(path)} uses tokens, not fixed light tints", not bad, str(bad[:3]))

print("\n" + "=" * 60)
print("ALL DRILL-DOWN CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
