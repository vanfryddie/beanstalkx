"""The Cross Learning Model: what it reads, what it concludes, and why.

Every check here is about a conclusion rather than a number — the point
of the tab is that a senior can argue with it, so the thing worth
testing is whether the same evidence always produces the same verdict,
and whether the verdict changes when the evidence does.
"""
import os, sys
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, application

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def client(login="admin", role="admin"):
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = login, role, "manual", "SCN2"
    return c


COSTS = None


def situation(target, trained, pct_to_plan, constellation=None):
    """One path's verdict, from the three things that decide it."""
    return application._clm_situation(
        {"target": target, "trained": trained, "process": "Pick"},
        {"pct_to_plan": pct_to_plan}, constellation, COSTS)


db.set_current_site("SCN2")
db.init_db()
db.begin_request_scope()
COSTS = db.get_xt_cost_model()

print("1. The decision table")
# The corners are the point: the same headcount gap means opposite
# things depending on whether the work got done.
cases = [
    ((50, 30, 102), "under", "met", "Target is above what the work needs"),
    ((50, 30, 84), "under", "missed", "Genuinely short"),
    ((50, 30, None), "under", "unknown", "Short, with no plan to judge it against"),
    ((50, 50, 84), "at", "missed", "Enough people, not enough rate"),
    ((50, 50, 101), "at", "met", "Settled"),
    ((50, 50, None), "at", "unknown", "At target, with no plan to judge it against"),
    ((50, 80, 103), "over", "met", "Carrying more than the work needs"),
    ((50, 80, 70), "over", "missed", "Deep pool, missing plan"),
    ((50, 80, None), "over", "unknown", "Above target, with no plan to judge it against"),
    # A target of nothing is not a target somebody is meeting.
    ((0, 4, 102), "none", "met", "No target set, and the work is getting done"),
    ((0, 4, 80), "none", "missed", "No target set, and behind plan"),
    ((0, 4, None), "none", "unknown", "No target and no plan"),
]
for (target, trained, pct), standing, attainment, title in cases:
    got = situation(target, trained, pct)
    check(f"{trained} of {target} at {pct if pct is not None else 'no'} plan -> {title}",
          got["standing"] == standing and got["attainment"] == attainment and got["title"] == title,
          f"{got['standing']}/{got['attainment']}: {got['title']}")

# The bands, so a path a hair under does not read as a crisis.
check("96% of plan still counts as met", situation(50, 50, 96)["attainment"] == "met")
check("94% does not", situation(50, 50, 94)["attainment"] == "missed")
check("one short of target is still at target", situation(50, 49, 100)["standing"] == "at")
check("two short is under", situation(50, 48, 100)["standing"] == "under")

print("\n2. The actions that follow")
short = situation(50, 30, 84)
kinds = {a["kind"] for a in short["actions"]}
check("a genuine shortfall is costed", "close_gap" in kinds, str(kinds))
check("and the cost is in the words", any("costs about" in a["text"] for a in short["actions"]))
at_target = situation(50, 50, 84)
check("at target and behind plan says it is not a training problem",
      "not_training" in {a["kind"] for a in at_target["actions"]})
over = situation(50, 80, 103)
check("a surplus is offered for redeployment", "redeploy" in {a["kind"] for a in over["actions"]})
noplan = situation(50, 30, None)
check("no plan asks for one", "set_plan" in {a["kind"] for a in noplan["actions"]})
# An unsized path is the quietest kind of gap: nothing reads as wrong.
notarget = situation(0, 4, 102, {"people": 9, "hours": 400.0, "top": []})
check("no target asks for one", "set_target" in {a["kind"] for a in notarget["actions"]},
      str({a["kind"] for a in notarget["actions"]}))
check("and says what size it already looks",
      "9 people" in next(a["text"] for a in notarget["actions"] if a["kind"] == "set_target"))
check("an unsized path is never called settled",
      situation(0, 4, 102)["title"] != "Settled")

print("\n3. Under target, on plan — the case the page exists for")
# Somebody is doing the work. Who, and from where, decides whether the
# target is wrong or the standard is.
outside = {
    "people": 40, "hours": 2000.0, "home_hours": 200.0, "home_share": 0.1,
    "outside_hours": 1800.0, "outside_share": 0.9,
    "outside_by_department": [{"department": "RSP", "hours": 1200.0, "share": 0.6},
                              {"department": "Chutings", "hours": 600.0, "share": 0.3}],
    "top": [],
}
covered = situation(50, 30, 102, outside)
kinds = {a["kind"] for a in covered["actions"]}
check("it proposes adding the department that is actually covering it",
      "add_source" in kinds, str(kinds))
add = next(a for a in covered["actions"] if a["kind"] == "add_source")
check("and names it", "RSP" in add["text"], add["text"][:90])
check("it also offers lowering the target", "lower_target" in kinds)
check("with the training that would release", any("releas" in a["text"] for a in covered["actions"]))

inside = dict(outside, home_share=0.95, outside_share=0.05,
              outside_by_department=[{"department": "RSP", "hours": 40.0, "share": 0.05}])
own = situation(50, 30, 102, inside)
check("when its own departments are covering it, no new standard is proposed",
      "add_source" not in {a["kind"] for a in own["actions"]},
      str({a["kind"] for a in own["actions"]}))

print("\n4. Who is carrying it, and whether they should be")
carrying = dict(outside, top=[
    {"login": "a1", "name": "A One", "hours": 90, "home_department": "RSP",
     "from_outside": True, "proficiency": "Lapsed", "trained": True},
    {"login": "a2", "name": "A Two", "hours": 80, "home_department": "RSP",
     "from_outside": True, "proficiency": "Proficient", "trained": False},
])
flagged = situation(50, 30, 102, carrying)
kinds = {a["kind"] for a in flagged["actions"]}
check("somebody Lapsed doing the work is called out", "lapsed_carrying" in kinds, str(kinds))
check("and somebody not recorded as trained at all", "untrained_carrying" in kinds, str(kinds))

print("\n5. The constellation itself")
path = {"source_departments": ["Receive", "RSP"]}
people = [
    {"employee_login": "p1", "full_name": "P One", "home_department": "Receive", "hours": 100,
     "trained_status": "Trained", "proficiency_status": "Proficient"},
    {"employee_login": "p2", "full_name": "P Two", "home_department": "Chutings", "hours": 300,
     "trained_status": "Trained", "proficiency_status": "Practice"},
    {"employee_login": "p3", "full_name": "P Three", "home_department": "Pack Multis", "hours": 100,
     "trained_status": "Trained", "proficiency_status": "Proficient"},
]
c = application._clm_constellation(path, people)
check("hours add up", c["hours"] == 500.0, str(c["hours"]))
check("the path's own departments are counted as home", c["home_hours"] == 100.0, str(c["home_hours"]))
check("everyone else is counted as cross-trained in", c["outside_hours"] == 400.0)
check("the split is a share of the whole", c["outside_share"] == 0.8, str(c["outside_share"]))
check("and the outside departments are ranked",
      [d["department"] for d in c["outside_by_department"]] == ["Chutings", "Pack Multis"],
      str([d["department"] for d in c["outside_by_department"]]))
check("the biggest contributor is first", c["top"][0]["login"] == "p2", str(c["top"][0]))
check("and is marked as from outside", c["top"][0]["from_outside"] is True)
check("while somebody from a source department is not",
      next(t for t in c["top"] if t["login"] == "p1")["from_outside"] is False)
# Case should not decide whether somebody counts as home.
loud = application._clm_constellation({"source_departments": ["receive"]}, people[:1])
check("matching a department ignores case", loud["home_hours"] == 100.0, str(loud["home_hours"]))

print("\n6. Routes the floor is running that nothing describes")
paths = [{"process": "Pick", "source_departments": ["RSP"]}]
contributors = {
    ("Pick", "early"): [
        {"employee_login": "x1", "home_department": "RSP", "hours": 500,
         "trained_status": "Trained"},
        {"employee_login": "x2", "home_department": "Chutings", "hours": 400,
         "trained_status": "Trained"},
        {"employee_login": "x3", "home_department": "Receive", "hours": 10,
         "trained_status": "Trained"},
    ],
}
found = application._clm_discovered_paths(contributors, paths, min_hours=40)
names = {(r["department"], r["process"]) for r in found}
check("a department working somebody else's process is proposed",
      ("Chutings", "Pick") in names, str(names))
check("the department the standard already names is not",
      ("RSP", "Pick") not in names, str(names))
check("and a handful of incidental hours is not",
      ("Receive", "Pick") not in names, str(names))
proposal = next(r for r in found if r["department"] == "Chutings")
check("the proposal carries the hours behind it", proposal["hours"] == 400.0)
check("and the share of the process they are doing",
      0.4 < proposal["share"] < 0.5, str(proposal["share"]))
check("and how many people", proposal["people"] == 1)

print("\n7. The model end to end")
db.end_request_scope()
with application.application.test_request_context("/ld-management/cross-training"):
    db.begin_request_scope()
    model = application.build_cross_learning_model("month")
    db.end_request_scope()
check("it produces a row per defined path", len(model["rows"]) > 0, str(len(model["rows"])))
check("every row has a verdict", all(r["situation"]["title"] for r in model["rows"]))
check("the inputs it reads are declared", len(model["inputs"]) >= 5, str(len(model["inputs"])))
check("each input says whether it is ready",
      all("ready" in i for i in model["inputs"]))
import itertools
every = set(itertools.product(("under", "at", "over", "none"), ("met", "missed", "unknown")))
check("the rules are published with it", len(model["rules"]) == len(every),
      str(len(model["rules"])))
check("and the table has no holes to fall through",
      set(application.CLM_RULE_MAP) == every,
      str(sorted(every - set(application.CLM_RULE_MAP))))
check("judged and unjudged add up to the rows",
      model["judged"] + model["unjudged"] == len(model["rows"]),
      f"{model['judged']} + {model['unjudged']} vs {len(model['rows'])}")
# Whatever cannot be judged sorts last: a page that leads with "no plan"
# wastes the reader's first screen.
judged_positions = [i for i, r in enumerate(model["rows"])
                    if r["situation"]["attainment"] != "unknown"]
unjudged_positions = [i for i, r in enumerate(model["rows"])
                      if r["situation"]["attainment"] == "unknown"]
check("the paths it could judge come first",
      not judged_positions or not unjudged_positions
      or max(judged_positions) < min(unjudged_positions),
      f"judged up to {max(judged_positions) if judged_positions else '-'}, "
      f"unjudged from {min(unjudged_positions) if unjudged_positions else '-'}")

print("\n8. Rate and plan")
db.begin_request_scope()
plans = {("Pick", ""): {"plan_rate": 120.0}}
entries = [{"units": 12000, "hours": 120, "plan_rate": None, "plan_hours": None}]
plan = application._clm_plan(entries, plans, "Pick", "early")
check("the rate is units over hours", plan["rate"] == 100.0, str(plan["rate"]))
check("the plan comes from the setting when the file has none",
      plan["plan_rate"] == 120.0 and plan["plan_source"] == "setting", str(plan))
check("attainment is the actual rate against the planned one",
      plan["pct_to_plan"] == 83.3, str(plan["pct_to_plan"]))
check("and the planned hours follow from it", plan["plan_hours"] == 100.0, str(plan["plan_hours"]))
check("so the hours over plan are reported", plan["hours_vs_plan"] == 20.0, str(plan["hours_vs_plan"]))
from_file = application._clm_plan(
    [{"units": 12000, "hours": 120, "plan_rate": 150.0, "plan_hours": None}], plans, "Pick", "early")
check("a plan in the file beats the setting",
      from_file["plan_rate"] == 150.0 and from_file["plan_source"] == "file", str(from_file))
none_at_all = application._clm_plan(
    [{"units": 1, "hours": 1}], {}, "Nothing Planned", "early")
check("and with neither, attainment is unknown rather than invented",
      none_at_all["pct_to_plan"] is None and none_at_all["plan_source"] is None, str(none_at_all))
db.end_request_scope()

print("\n9. The page")
c = client()
page = c.get("/ld-management/cross-training?tab=model", follow_redirects=True)
check("it renders", page.status_code == 200, str(page.status_code))
body = page.get_data(as_text=True)
for probe in ("Cross learning model", "What it is learning from", "What it concluded",
              "What to do about it", "The rules it is applying", "Who carried it"):
    check(f"it shows {probe}", probe in body)
# A site has dozens of paths. The page leads with the ones that need a
# decision and keeps the rest on the page as rows rather than as a
# hundred screens of cards.
check("it caps the cards", body.count("clm-card clm-") <= application.CLM_CARDS,
      str(body.count("clm-card clm-")))
check("and still accounts for every path",
      "The other" in body or body.count("clm-card clm-") == len(model["rows"]))
for period in ("week", "month", "quarter"):
    check(f"the {period} view renders",
          c.get(f"/ld-management/cross-training?tab=model&period={period}",
                follow_redirects=True).status_code == 200)
check("a nonsense period falls back rather than failing",
      c.get("/ld-management/cross-training?tab=model&period=sideways",
            follow_redirects=True).status_code == 200)

print("\n10. Setting the plan")
viewer = client("am000000", "am")
check("an area manager does not get the tab",
      "Cross learning model" not in
      viewer.get("/ld-management/cross-training?tab=model",
                 follow_redirects=True).get_data(as_text=True))
check("nor set the plan",
      viewer.post("/ld-management/cross-training/model/plan",
                  data={"plan_process": "Pick", "plan_rate": "9"}).status_code in (302, 403))
c.post("/ld-management/cross-training/model/plan",
       data={"plan_process": ["Pick"], "plan_rate": ["137.5"], "period": "month"})
db.begin_request_scope()
check("an admin can", db.plan_rate_for("Pick", "early") == 137.5,
      str(db.plan_rate_for("Pick", "early")))
db.end_request_scope()
c.post("/ld-management/cross-training/model/plan",
       data={"plan_process": ["Pick"], "plan_rate": ["nonsense"], "period": "month"})
db.begin_request_scope()
check("and nonsense is refused rather than stored",
      db.plan_rate_for("Pick", "early") == 137.5)
db.end_request_scope()

print("\n" + "=" * 62)
print("ALL CROSS LEARNING MODEL CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
