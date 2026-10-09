"""The OLR tab: the composite it reports, and the review built off it.

An Operational Leadership Review is a paper the manager writes about
their own performance, so the numbers here end up quoted in a document
somebody is held to. Two things are checked: that the headline figure is
the one it claims to be, and that nothing in the generated review says
anything the weekly snapshots do not support.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import application, db

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: fails.append(name)


application.application.config["TESTING"] = True
db.set_current_site("SCN2")
db.init_db()
ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
am = ams[0]

print("1. The composite counts the seven metrics and nothing else")
# A week's snapshot row carries far more than the seven scores: each
# metric's rank, the cross-training headcounts, two gap counts and
# Safety's average overdue days. Averaging those in produced a Total
# L&D Score of 33.8% for a manager sitting at 83.1%, on the OLR chart,
# on the 4-week trend arrow, and in the quarter-over-quarter figures.
week = {
    "safety_compliance": 100.0, "de_tech": 90.0, "indirect_roles": 80.0,
    "cross_training": 70.0, "instructor_mgmt": 100.0, "ambassador_readiness": 100.0,
    "bts_compliance": 90.0,
}
noise = dict(week, safety_compliance_rank=4, total_rank=6, ir_gap=11,
             ambassador_gap=-3, xt_lapsed=24, xt_proficient=62,
             safety_avg_overdue_days=63.7)
clean = db._olr_composite(week)
check("the seven alone average correctly", round(clean, 1) == 90.0, str(clean))
check("the rank, gap and day figures do not move it",
      db._olr_composite(noise) == clean,
      f"{db._olr_composite(noise)} with them, {clean} without")
check("a week missing some metrics averages the ones it has",
      db._olr_composite({"safety_compliance": 80.0, "de_tech": 90.0, "total_rank": 1}) == 85.0)
check("a week with no metric at all is no score, not zero",
      db._olr_composite({"total_rank": 3, "ir_gap": 9}) is None)

print("\n2. The review is built from the snapshots, and agrees with them")
series = db.get_olr_weekly_series(am)
weeks = sorted(series)
check("there is history to review", len(weeks) >= 2, f"{len(weeks)} weeks")
data = application._build_olr_tab_data(am)
rv = data["review"]
check("a review was produced", rv is not None)

if rv:
    last = weeks[-1]
    for row in rv["rows"]:
        stored = series[last].get(row["key"])
        if stored is None:
            continue
        check(f"{row['label']}: 'now' is the latest snapshot",
              abs(float(row["now"]) - float(stored)) < 0.05,
              f"review {row['now']}, snapshot {stored}")

    expected_total = db._olr_composite(series[last])
    check("the headline total is the composite of the latest week",
          abs(float(rv["total"]["now"]) - expected_total) < 0.05,
          f"review {rv['total']['now']}, computed {round(expected_total, 1)}")

    target = rv["target"]
    for row in rv["rows"]:
        if row["shortfall"]:
            check(f"{row['label']}: the shortfall is the distance to target",
                  abs(row["shortfall"] - (target - row["now"])) < 0.05,
                  f"{row['shortfall']} vs {round(target - row['now'], 1)}")
        else:
            check(f"{row['label']}: no shortfall claimed while at target",
                  row["now"] >= target, str(row["now"]))

    below = [r for r in rv["rows"] if r["shortfall"]]
    check("the table leads with the metric furthest below target",
          not below or rv["rows"][0]["key"] == max(below, key=lambda r: r["shortfall"])["key"],
          rv["rows"][0]["label"])

    # Weeks-under has to be a count over the window, never the streak:
    # the two were conflated in the first draft of the insight copy.
    for row in rv["rows"]:
        points = [v for _w, v in row["points"]]
        check(f"{row['label']}: weeks under target is a real count",
              row["weeks_below"] == len([v for v in points if float(v) < target]),
              f"{row['weeks_below']} claimed")
        check(f"{row['label']}: the streak never exceeds the count",
              row["streak_below"] <= row["weeks_below"])

print("\n3. Every figure in the draft appears in the review data")
if rv and rv["draft"]:
    draft = rv["draft"]
    check("the draft names the current total", f"{rv['total']['now']}%" in draft, draft[:120])
    check("it cites the number of weeks", str(rv["total"]["weeks_total"]) in draft)
    check("it names the target", f"{rv['target']}%" in draft)
    check("it leaves the actions to the manager",
          "write this part yourself" in draft)
    # Nothing invented: every percentage in the prose has to be a figure
    # the review actually holds.
    known = {f"{rv['target']}"}
    for row in rv["rows"]:
        known.update({str(row["now"]), str(row["first"]), str(row["best"]), str(row["worst"])})
    known.update({str(rv["total"]["now"]), str(rv["total"]["first"])})
    quoted = set(re.findall(r"(\d+(?:\.\d+)?)%", draft))
    invented = sorted(quoted - known)
    check("no percentage in the draft is invented", not invented, str(invented))

print("\n4. The tab renders, for a manager and for somebody with no history")
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"
body = c.get(f"/overview?tab=olr&am={am}").get_data(as_text=True)
check("the OLR tab renders", "What to write about" in body)
check("the draft is editable in place", 'id="olrDraftText"' in body)
check("the metric table is there", "Every metric, worst first" in body)
check("the charts are kept as the evidence", "The full charts" in body)
check("the headline figure appears", f"{rv['total']['now']}" in body)

blank = c.get("/overview?tab=olr&am=nobody-at-all").get_data(as_text=True)
check("somebody with no snapshots gets an explanation, not an empty review",
      "No weekly OLR snapshots yet" in blank or "Pick an Area Manager" in blank)

print("\n5. The draft can actually be taken out of the page")
# The draft only earns its place if it reaches the manager's document.
# Checked in a browser because that is where the clipboard lives.
import threading, time
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright

srv = make_server("127.0.0.1", 5640, application.application, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(1.0)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                          args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1500, "height": 950},
                        permissions=["clipboard-read", "clipboard-write"])
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:5640/", wait_until="domcontentloaded")
    pg.fill("input[name=login_id]", "admin"); pg.click("button[type=submit]"); pg.wait_for_timeout(400)
    pg.goto(f"http://127.0.0.1:5640/overview?tab=olr&am={am}", wait_until="networkidle")

    check("no uncaught JavaScript on the tab", not errors, "; ".join(errors[:2]))
    check("the copy confirmation is not showing before anything is copied",
          pg.eval_on_selector("#olrCopyState", "e => getComputedStyle(e).display") == "none",
          pg.eval_on_selector("#olrCopyState", "e => getComputedStyle(e).display"))

    in_box = pg.eval_on_selector("#olrDraftText", "e => e.value")
    check("the draft is in the box", "Total L&D Score" in in_box, in_box[:80])
    pg.click("#olrCopy")
    pg.wait_for_timeout(400)
    clipped = pg.evaluate("navigator.clipboard.readText()")
    check("clicking Copy puts the draft on the clipboard", clipped.strip() == in_box.strip(),
          f"clipboard holds {clipped[:60]!r}")
    check("and says so", pg.eval_on_selector("#olrCopyState", "e => !e.hidden"))

    # Edited text has to be what gets copied, or the button quietly
    # hands back something the manager did not write.
    pg.fill("#olrDraftText", "My own words.")
    pg.click("#olrCopy")
    pg.wait_for_timeout(400)
    check("an edit is what gets copied, not the original",
          pg.evaluate("navigator.clipboard.readText()").strip() == "My own words.",
          pg.evaluate("navigator.clipboard.readText()")[:60])

    over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("no horizontal page scroll", over <= 1, f"overflows {over}px")
    ctx.close(); b.close()
srv.shutdown()

print("\n" + "=" * 62)
print("ALL OLR REVIEW CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
