"""Cross-training standards: the five uploads, the replay, the day plan,
and the page.

The engine is tested on a site small enough to work out on paper: one
process (Pack) owned by PackDept, five veterans at 100 an hour, a planned
6,000 units on a 10-hour shift. Veterans make 5,000, so the gap is 1,000;
a cross-trainee does 80 an hour, 800 a shift, so two are needed. Every
verdict below follows from moving one of those facts, which is the
point: the same evidence must give the same verdict, and the verdict
must move when the evidence does.
"""
import csv, io, json, os, sys
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LDIS_BEDROCK_MODEL_ID", None)
import db, application, bedrock_client, xt_planning as X

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


TODAY = date(2026, 12, 1)
HISTORY = [(date(2026, 11, 20) + timedelta(days=i)).isoformat() for i in range(10)]
FUTURE = "2026-12-02"


def world(pick_n=10, pick_need=5, ship_n=0, ship_need=0, xt_on_pack=2, vets=5, central_days=(), stranger=False):
    """The paper site. `pick_need` is how many of Pick's people its own
    work needs, so Pick can spare pick_n - pick_need."""
    hours, roster, cal, pday, aday = [], [], [], [], []

    def person(login, dept, processes):
        roster.append({"employee_login": login, "shift_pattern": "DTEST", "shift": "early"})
        for p in processes:
            hours.append({"employee_login": login, "fclm_mapped": dept, "merged_function": p,
                          "trained_status": "Trained", "shift_pattern": "DTEST", "shift": "early"})
    for i in range(vets):
        person(f"vet{i}", "PackDept", ["Pack"])
    for i in range(pick_n):
        person(f"pick{i}", "Pick", ["PickWork"] + (["Pack"] if i < xt_on_pack else []))
    for i in range(ship_n):
        person(f"ship{i}", "Ship", ["ShipWork"])
    for d in HISTORY + [FUTURE]:
        cal.append({"shift_code": "DTEST", "side": "OB", "shift": "early", "work_date": d, "hours": 10.0,
                    "pattern": "Central" if d in central_days else "Day"})
    for d in HISTORY:
        for i in range(vets):
            aday.append({"work_date": d, "shift": "early", "employee_login": f"vet{i}", "process": "Pack",
                         "units": 1000, "hours": 10})
        for i in range(pick_n):
            on_pack = i < xt_on_pack
            aday.append({"work_date": d, "shift": "early", "employee_login": f"pick{i}",
                         "process": "Pack" if on_pack else "PickWork",
                         "units": 800 if on_pack else 1000, "hours": 10})
        for i in range(ship_n):
            aday.append({"work_date": d, "shift": "early", "employee_login": f"ship{i}", "process": "ShipWork",
                         "units": 1000, "hours": 10})
        if stranger:
            # Someone with no Hours on Function row: no home department.
            aday.append({"work_date": d, "shift": "early", "employee_login": "nobody", "process": "Pack",
                         "units": 900, "hours": 10})
        units = vets * 1000 + xt_on_pack * 800
        # Planned at 90 an hour: the mix of 100s and 80s runs at about
        # 94, so a shift that makes its units also makes its rate.
        pday.append({"work_date": d, "shift": "early", "process": "Pack", "department": "PackDept",
                     "units": units, "hours": (vets + xt_on_pack) * 10, "plan_units": 6000, "plan_rate": 90})
        pday.append({"work_date": d, "shift": "early", "process": "PickWork", "department": "Pick",
                     "units": pick_need * 1000, "hours": pick_need * 10, "plan_units": pick_need * 1000,
                     "plan_rate": 100})
        if ship_n:
            pday.append({"work_date": d, "shift": "early", "process": "ShipWork", "department": "Ship",
                         "units": ship_need * 1000, "hours": ship_need * 10, "plan_units": ship_need * 1000,
                         "plan_rate": 100})
    return X.Prepared({"roster": roster, "hours": hours, "calendar": cal, "process_day": pday,
                       "associate_day": aday}, today=TODAY, window_weeks=8, shift_hours=9.5)


def cell(prep, target, sources=("Pick",)):
    paths = [{"process": "Pack", "shift": "early", "target": target / len(sources),
              "source_departments": [s]} for s in sources]
    result = X.evaluate(prep, paths, {})
    return result["cells"][("Pack", "early")]


print("1. The replay, on paper")
prep = world()
check("veterans run at 100 an hour", prep.rate("pack", "vet")[0] == 100.0)
check("cross-trainees at 80", prep.rate("pack", "xt")[0] == 80.0)
check("Pick can spare 5 a day", all(prep.dept_spare("pick", d, "early") == 5 for d in HISTORY))
c = cell(prep, 2)
check("two cross-trainees cover every shift: keep", c["verdict"] == "keep" and c["coverage"] == 1.0, c["verdict"])
check("the smallest target that does is 2", c["best"] == 2, c.get("best"))
c = cell(prep, 1)
check("one is not enough: increase to 2", c["verdict"] == "increase" and c["new_target"] == 2
      and c["coverage"] == 0.0, (c["verdict"], c.get("new_target")))
c = cell(prep, 10)
check("ten is more than the work needs: decrease to 2", c["verdict"] == "decrease" and c["new_target"] == 2,
      (c["verdict"], c.get("new_target")))
c = cell(world(xt_on_pack=4), 10)
check("but never below the most actually used on one shift (4)", c["verdict"] == "decrease"
      and c["new_target"] == 4 and c["peak_xt"] == 4, (c["verdict"], c.get("new_target"), c.get("peak_xt")))
curve = [cov for _t, cov in cell(prep, 2)["curve"]]
check("coverage never falls as the target rises", curve == sorted(curve), curve)

c = cell(world(pick_need=10, ship_n=10, ship_need=5), 2)
check("when Pick can spare nobody and Ship can: switch to Ship", c["verdict"] == "switch"
      and c["alternative"]["department"] == "Ship" and c["new_target"] == 2,
      (c["verdict"], c.get("alternative")))
check("and the evidence says Pick could lend on 0 of 10 shifts",
      any("Pick could lend on 0 of 10" in line for line in c["evidence"]), c["evidence"])
c = cell(world(pick_need=10), 2)
check("when nobody can spare anyone: hire, not train", c["verdict"] == "hire", c["verdict"])
check("one more veteran closes a 1,000-unit gap at 100 an hour over 10 hours", c.get("hire") == 1, c.get("hire"))
# Four veterans leave a 2,000 gap: 2.5 cross-trainees' worth. Split
# between Pick (never spares anyone) and Ship, 5 are needed to get 2.5
# from Ship; Ship alone needs 3. Saving 2 trained people is a switch.
c = cell(world(pick_need=10, ship_n=10, ship_need=5, vets=4), 2, sources=("Pick", "Ship"))
check("two sources where one can never lend: train only from the one that can",
      c["verdict"] == "switch" and c["alternative"]["department"] == "Ship"
      and application._xt_change_headline(c).startswith("Train only Ship"), (c["verdict"], c.get("alternative")))
paths = [{"process": "Nothing", "shift": "early", "target": 3, "source_departments": ["Pick"]}]
check("a standard with no volume reads 'no data', not a verdict",
      X.evaluate(prep, paths, {})["cells"][("Nothing", "early")]["verdict"] == "no_data")
r = X.evaluate(prep, [{"process": "Pack", "shift": "early", "target": 2, "source_departments": ["Pick"]}],
               {"coverage_bar": 1.0})
check("the bar is a setting, and raising it changes nothing that already meets 100%",
      r["cells"][("Pack", "early")]["verdict"] == "keep")
check("filters: a department that owns nothing here filters everything out",
      not X.evaluate(prep, [{"process": "Pack", "shift": "early", "target": 2, "source_departments": ["Pick"]}],
                     {}, department="Ship")["cells"])

print("\n2. The day plan, on paper")
dp = X.day_plan(prep, "Pack", "early", FUTURE, 6000, absence_pct=20)
check("veterans: 5 scheduled, less 20% = 4 available", dp["vets"]["scheduled"] == 5 and dp["vets"]["available"] == 4.0,
      dp["vets"])
check("they cover 4 x 10h x 100 = 4,000", dp["vet_units"] == 4000, dp["vet_units"])
pick = next(l for l in dp["lenders"] if l["department"] == "Pick")
check("Pick's own work that day defaults to its same-weekday average (5,000)",
      pick["own_work"][0]["volume"] == 5000 and pick["own_work"][0]["basis"] == "same-weekday average", pick["own_work"])
check("which needs 5 of its 8 available people, leaving 3 spare", pick["own_need_heads"] == 5.0
      and pick["people_available"] == 8.0 and pick["spare"] == 3.0, pick)
check("2 trained, 1.6 available: 1 can move", pick["available"] == 1.6 and pick["deployable"] == 1, pick)
check("so 1 moves, 1,200 units stay uncovered, and that is 2 veterans to hire",
      dp["xt_heads"] == 1 and dp["shortfall_units"] == 1200 and dp["hire"] == 2,
      (dp["xt_heads"], dp["shortfall_units"], dp["hire"]))
dp = X.day_plan(prep, "Pack", "early", FUTURE, 6000, absence_pct=0)
check("with nobody absent: 5 veterans, 2 moved, nothing short", dp["vet_units"] == 5000 and dp["xt_heads"] == 2
      and dp["shortfall_units"] == 0 and dp["hire"] == 0, (dp["vet_units"], dp["xt_heads"], dp["shortfall_units"]))
dp = X.day_plan(prep, "Pack", "early", FUTURE, 6000, absence_pct=0, other_volumes={"pick|pickwork": 10000})
check("an entered volume for Pick's own work leaves it nobody to lend", next(
    l for l in dp["lenders"] if l["department"] == "Pick")["deployable"] == 0 and dp["shortfall_units"] == 1000,
      dp["shortfall_units"])
check("history: 10 past shifts at this volume, all met plan, with 5 veterans and 2 from Pick",
      len(dp["history"]) == 10 and dp["history_met"] == 10 and dp["typical"] == {"vets": 5.0, "xt": {"Pick": 2.0}},
      (len(dp["history"]), dp["history_met"], dp["typical"]))
check("absence is measured where it can be: everyone came in, so 0%",
      X.measured_absence(prep, "packdept", "early")[0] in (None, 0.0))

print("\n2b. The labour mix, on paper")
mix = X.labour_mix(prep, HISTORY[0], HISTORY[-1])
pack = next(r for r in mix["detail"] if r["process"] == "Pack")
check("veterans: 500 hours, 71% of Pack's", pack["vet"]["hours"] == 500 and pack["vet_share"] == 0.714,
      (pack["vet"]["hours"], pack["vet_share"]))
check("cross-trainees: 200 hours, 29%", pack["xt"]["hours"] == 200 and pack["xt_share"] == 0.286)
check("veterans at 100 an hour are 111% of a 90 plan", pack["vet"]["rate"] == 100 and pack["vet"]["pct"] == 111.1,
      pack["vet"])
check("cross-trainees at 80 are 89%", pack["xt"]["rate"] == 80 and pack["xt"]["pct"] == 88.9, pack["xt"])
check("and they came from Pick: 2 people, 200 hours",
      pack["by_dept"] == [{"department": "Pick", "hours": 200.0, "people": 2, "rate": 80.0, "pct": 88.9}], pack["by_dept"])
early = mix["overview"][0]
pickwork = next(r for r in mix["detail"] if r["process"] == "PickWork")
check("the shift total weighs each process by its own plan",
      early["shift"] == "early" and early["vet"]["pct"] == round(100 * (5000 * 10 + 8000 * 10) /
                                                                 (500 * 90 + 800 * 100), 1), early["vet"])
check("indirect time and unplanned work are left out", "Indirect" not in {r["process"] for r in mix["detail"]})
check("a department filter keeps only the processes it owns",
      {r["process"] for r in X.labour_mix(prep, HISTORY[0], HISTORY[-1], department="PackDept")["detail"]} == {"Pack"})
check("a shift with no work is empty, not an error", X.labour_mix(prep, HISTORY[0], HISTORY[-1], shift="late")["detail"] == [])
check("a period outside the data is empty", X.labour_mix(prep, "2025-01-01", "2025-01-07")["overview"] == [])
mix = X.labour_mix(world(central_days=HISTORY[:4]), HISTORY[0], HISTORY[-1])
shifts = {o["shift"]: o for o in mix["overview"]}
check("days on a Central code are reported as Central", set(shifts) == {"early", "central"}
      and shifts["central"]["vet"]["hours"] == 5 * 10 * 4 + 8 * 10 * 4, {k: v["vet"]["hours"] for k, v in shifts.items()})
mix = X.labour_mix(world(stranger=True), HISTORY[0], HISTORY[-1])
check("hours from someone with no home department are counted aside, not guessed",
      mix["unknown_hours"] == 100.0 and next(r for r in mix["detail"] if r["process"] == "Pack")["vet"]["hours"] == 500)
check("week label", X.period_label("2026-11-16", "week") == "16–22 Nov 2026")
check("a week across months", X.period_label("2026-10-26", "week") == "26 Oct–1 Nov 2026")
check("February in a leap year ends on the 29th", X.period_bounds("2028-02", "month") == ("2028-02-01", "2028-02-29"))
check("Q4 ends on 31 December", X.period_bounds("2026-Q4", "quarter") == ("2026-10-01", "2026-12-31"))
keys, default = X.available_periods(prep, "week")
check("the default period is the newest that has finished", default == "2026-11-23" and keys[0] == "2026-11-23",
      (keys, default))

print("\n2c. Learning from the past: the forecast")
from datetime import date as _d
base = _d(2026, 6, 1)                       # a Monday


def series(weeks, monday=1200, other=1000, growth=0.0, noise=None):
    out = []
    for i in range(weeks * 7):
        day = base + timedelta(days=i)
        value = (monday if day.weekday() == 0 else other) * (1 + growth) ** (i / 7)
        if noise:
            value *= 1 + noise[i % len(noise)]
        out.append((day.isoformat(), value))
    return out


m = X.fit_forecast(series(8))
nxt = base + timedelta(days=56)
check("a steady series forecasts its own level", abs(X.forecast_value(m, nxt + timedelta(days=1)) - 1000) < 30,
      X.forecast_value(m, nxt + timedelta(days=1)))
check("and keeps each weekday's usual share: Mondays stay busier",
      X.forecast_value(m, nxt) > X.forecast_value(m, nxt + timedelta(days=1)) * 1.1)
m = X.fit_forecast(series(8, growth=0.03))
check("a growing series forecasts growth, at half strength", 0.01 < m["trend"] <= 0.02, m["trend"])
m = X.fit_forecast(series(8, growth=0.4))
check("and a runaway trend is capped at 5% a week", m["trend"] == 0.05, m["trend"])
check("too little history forecasts nothing rather than guessing", X.fit_forecast(series(1)) is None)


class Holder:
    pass


def graded(noise):
    h = X.Prepared({"process_day": [{"work_date": d, "shift": "early", "process": "Pack", "units": v}
                                    for d, v in series(10, noise=noise)]},
                   today=base + timedelta(days=70), window_weeks=12)
    return X.backtest(h, "pack", "early")
calm, rough = graded([0.0]), graded([0.25, -0.2, 0.1, -0.15, 0.3, -0.25, 0.05])
check("the forecast is graded on weeks it did not see", calm and calm["days"] >= 20)
check("a calm history grades near-perfect", calm and calm["accuracy"] > 0.98, calm)
check("a jumpy one grades lower, and says by how much it usually misses",
      rough and rough["accuracy"] < calm["accuracy"] and rough["low"] < 0 < rough["high"], rough)

print("\n2d. Scenarios: judging the weeks ahead")


def plan(units=None, multiplier=1.0, weeks=1):
    lines = {("pack", "early"): {"planned_units": units}} if units else {}
    lines[("pickwork", "early")] = {"planned_units": 5000}
    return {"id": 1, "name": "test", "start": FUTURE, "weeks": weeks, "multiplier": multiplier, "lines": lines}


def judged(target, scenario):
    paths = [{"process": "Pack", "shift": "early", "target": target, "source_departments": ["Pick"]}]
    return X.evaluate(prep, paths, {}, scenario=scenario)["cells"][("Pack", "early")]


c = judged(2, plan(6000))
check("a scenario at the volume the past had reaches the past's verdict: keep 2", c["verdict"] == "keep"
      and c["basis"] == "scenario", (c["verdict"], c.get("best")))
c = judged(2, plan(7000))
check("7,000 a day leaves a 2,000 gap: increase to 3", c["verdict"] == "increase" and c["new_target"] == 3,
      (c["verdict"], c.get("new_target")))
c = judged(2, plan(9000))
check("9,000 a day leaves a 4,000 gap: increase to 5", c["verdict"] == "increase" and c["new_target"] == 5,
      (c["verdict"], c.get("new_target")))
c = judged(2, plan(6000, multiplier=1.5))
check("the volume % scales every line, the lenders' own work too: at 150% Pick needs 7.5 of its 10 "
      "and can spare too few, so hire", c["verdict"] == "hire", (c["verdict"], c.get("new_target")))
c = judged(2, plan(6000, multiplier=2.0))
check("12,000 is more than Pick can ever spare for: hire", c["verdict"] == "hire", c["verdict"])
c = judged(2, plan(None))
check("no plan and no forecast to fall back on is 'no data', not a guess", c["verdict"] == "no_data", c["verdict"])
past = __import__("collections").Counter(_d.fromisoformat(d).weekday() for d in HISTORY)
want_days = sum(min(8, past[(_d.fromisoformat(FUTURE) + timedelta(days=i)).weekday()]) for i in range(14))
check("every scenario day is played out once per past same weekday (two Fridays in the history, two plays)",
      judged(2, plan(6000, weeks=2))["days"] == want_days == 20, (judged(2, plan(6000, weeks=2))["days"], want_days))
check("and the evidence says these are scenario shifts, not history",
      any("scenario shifts" in line for line in judged(1, plan(6000))["evidence"]))

print("\n2e. Learning from feedback")


def with_rules(rules, **kw):
    w = world(**kw)
    for rule in rules:
        for wd in rule["weekdays"]:
            w.lend_blocks.add((rule["department"].lower(), rule.get("shift") or "*", wd))
    return w


all_days = list(range(7))
c = cell(with_rules([{"department": "Pick", "weekdays": all_days}]), 2)
check("a rule that Pick never lends turns 'keep' into 'hire'", c["verdict"] == "hire", c["verdict"])
check("and the evidence names the rule", any("set not to lend" in line for line in c["evidence"]), c["evidence"])
c = cell(with_rules([{"department": "Pick", "weekdays": all_days}], ship_n=10, ship_need=5), 2)
check("with another department able to lend, the same rule makes it a switch", c["verdict"] == "switch"
      and c["alternative"]["department"] == "Ship", (c["verdict"], c.get("alternative")))
fri = _d.fromisoformat(HISTORY[0]).weekday()
c = cell(with_rules([{"department": "Pick", "weekdays": [fri]}]), 2)
check("a rule for one weekday costs exactly that weekday's shifts", c["coverage"] == 0.8, c["coverage"])
c = cell(with_rules([{"department": "Pick", "shift": "late", "weekdays": all_days}]), 2)
check("a rule for another shift changes nothing here", c["verdict"] == "keep" and c["coverage"] == 1.0)
dp = X.day_plan(with_rules([{"department": "Pick", "weekdays": all_days}]), "Pack", "early", FUTURE, 6000, absence_pct=0)
pick = next(l for l in dp["lenders"] if l["department"] == "Pick")
check("the day plan moves nobody from a department that cannot lend", pick["deployable"] == 0 and pick["blocked"]
      and dp["xt_heads"] == 0 and dp["shortfall_units"] == 1000)
paths1 = [{"process": "Pack", "shift": "early", "target": 2, "source_departments": ["Pick"]}]
c = X.evaluate(prep, paths1, {"calibration": {"pack": 0.03}})["cells"][("Pack", "early")]
check("a calibration margin raises this process's bar", abs(c["bar"] - 0.98) < 1e-9 and c["calibration"] == 0.03)
check("and says so", any("The bar for this process is 98%" in line for line in c["evidence"]), c["evidence"])
c = X.evaluate(prep, paths1, {"calibration": {"other": 0.03}})["cells"][("Pack", "early")]
check("another process's margin leaves this one alone", c["bar"] == 0.95)
cov, n = X.coverage_since(prep, "Pack", "early", ["Pick"], 2, HISTORY[4])
check("an applied target is scored on the shifts since it was applied: 2 covers all 6", cov == 1.0 and n == 6, (cov, n))
cov, n = X.coverage_since(prep, "Pack", "early", ["Pick"], 1, HISTORY[4])
check("and 1 covers none of them", cov == 0.0)
check("too few shifts since is no score, not a guess", X.coverage_since(prep, "Pack", "early", ["Pick"], 2, HISTORY[-2])[0] is None)

print("\n3. The uploads, through a scratch site")
SITE = "ZQ88"
db.init_global_db()
if not db.get_site(SITE):
    db.create_site(SITE, "Planning-data scratch", "EU", "test")


def client(site):
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", site
    return c


def csv_bytes(header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


def upload(c, kind, data, name="f.csv"):
    return c.post("/ld-management/cross-training/planning-data/upload",
                  data={"kind": kind, "file": (io.BytesIO(data), name)},
                  content_type="multipart/form-data", follow_redirects=True)


db.set_current_site(SITE)
db.init_db()
# Start from nothing, whatever an interrupted earlier run left behind.
conn = db.get_db()
for table in ("employee_roster", "shift_calendar", "associate_day", "process_volume"):
    conn.execute(f"DELETE FROM {table}")
conn.execute("DELETE FROM app_settings WHERE key LIKE ? OR key = ?", ("xt_eval_%", "xt_function_process_map"))
conn.commit()
conn.close()
db.set_current_site("SCN2")
c = client(SITE)
roster = csv_bytes(["Employee ID", "User ID", "Employee Name", "Department ID", "Employment Start Date",
                    "Employment Type", "Employee Status", "Manager Name", "Job Title", "Management Area ID",
                    "Shift Pattern"],
                   [["900001", "zqa", "Test, A", "1299", "Tue Nov 03 00:00:00 UTC 2026", "AMZN", "Active", "M",
                     "FC Associate I", "18", "D58F83YA"],
                    ["900002", "ZQB", "Test, B", "1299", "Mon Aug 29 00:00:00 UTC 2022", "TEMP", "Active", "M",
                     "FC Associate I", "3", "N57F6SEO"]])
r = upload(c, "roster", roster)
db.set_current_site(SITE)
conn = db.get_db()
rows = {x["employee_login"]: dict(x) for x in conn.execute("SELECT * FROM employee_roster").fetchall()}
conn.close()
check("the roster loads, logins lower-cased", set(rows) == {"zqa", "zqb"}, rows.keys())
check("its Java-style start dates are read", rows.get("zqa", {}).get("start_date") == "2026-11-03")
check("and the shift comes from the pattern", rows.get("zqb", {}).get("shift") == "night")
upload(c, "roster", csv_bytes(["User ID", "Shift Pattern"], [["zqa", "D58F83YA"]]))
conn = db.get_db()
n = conn.execute("SELECT COUNT(*) AS n FROM employee_roster").fetchone()["n"]
conn.close()
check("a new roster replaces the old one: the leaver is gone", n == 1, n)
r = upload(c, "roster", csv_bytes(["Nothing"], [["x"]]))
conn = db.get_db()
n = conn.execute("SELECT COUNT(*) AS n FROM employee_roster").fetchone()["n"]
conn.close()
check("an unreadable roster is refused, not loaded as 'everybody left'",
      n == 1 and "import failed" in r.get_data(as_text=True), n)

sspot = csv_bytes(["Calendar Day", "FC", "Cohort", "Shift Pattern", "Main Process", "Shift Start Date",
                   "Start Time", "End Time"],
                  [["2026-11-24 00:00:00", "SCN2", "D58F83YA-OB", "Day", "OUTBOUND", "2025-05-18 00:00:00",
                    "2026-11-24 06:00:00", "2026-11-24 14:45:00"],
                   ["2026-11-24 00:00:00", "SCN2", "N57F6SEO-IB", "Night", "INBOUND", "2025-05-18 00:00:00",
                    "2026-11-24 23:30:00", "2026-11-25 06:00:00"]])
upload(c, "calendar", sspot)
conn = db.get_db()
cal = {x["cohort"]: dict(x) for x in conn.execute("SELECT * FROM shift_calendar").fetchall()}
conn.close()
check("the SSPOT calendar loads with code, side and shift split out",
      cal.get("D58F83YA-OB", {}).get("shift_code") == "D58F83YA" and cal["D58F83YA-OB"]["side"] == "OB"
      and cal["D58F83YA-OB"]["shift"] == "early")
check("a shift's length is end minus start, across midnight too",
      cal["D58F83YA-OB"]["hours"] == 8.75 and cal["N57F6SEO-IB"]["hours"] == 6.5,
      (cal["D58F83YA-OB"]["hours"], cal["N57F6SEO-IB"]["hours"]))
upload(c, "calendar", sspot)
conn = db.get_db()
n = conn.execute("SELECT COUNT(*) AS n FROM shift_calendar").fetchone()["n"]
conn.close()
check("re-sending a day replaces it rather than doubling it", n == 2, n)

template = c.get("/ld-management/cross-training/planning-data/template/process_day.csv")
check("the process-day template downloads", template.status_code == 200
      and template.get_data(as_text=True).startswith("date,shift,process,department,units,hours,plan_units,plan_rate"))
r = upload(c, "process_day", template.get_data())
conn = db.get_db()
pv = conn.execute("SELECT COUNT(*) AS n, COUNT(plan_units) AS planned, COUNT(department) AS dept "
                  "FROM process_volume").fetchone()
conn.close()
check("and loads back in as it is, with planned units and department", pv["n"] > 0
      and pv["planned"] == pv["n"] and pv["dept"] == pv["n"], dict(pv))
template = c.get("/ld-management/cross-training/planning-data/template/associate_day.csv")
r = upload(c, "associate_day", template.get_data())
conn = db.get_db()
n_assoc = conn.execute("SELECT COUNT(*) AS n FROM associate_day").fetchone()["n"]
conn.close()
check("the associate-day template loads back in too", n_assoc > 0, n_assoc)
check("its people are invented", b"smp0" in template.get_data())

aday = csv_bytes(["date", "shift", "employee_login", "fclm_function", "units", "hours"],
                 [["2026-11-24", "D", "ZQA", "RF Pick", "500", "5"],
                  ["2026-11-24", "D", "zqa", "MultiLowDensityPick", "300", "3"],
                  ["2026-11-24", "D", "zqa", "Mystery Function", "1", "1"]])
r = upload(c, "associate_day", aday)
text = r.get_data(as_text=True)
check("FCLM function names with no mapping are reported by name, not loaded",
      "Mystery Function" in text and "RF Pick" in text)
c.post("/ld-management/cross-training/model/config",
       data={"function_map": "RF Pick = Pick\nMultiLowDensityPick = Pick"}, follow_redirects=True)
r = upload(c, "associate_day", aday)
conn = db.get_db()
mine = [dict(x) for x in conn.execute("SELECT * FROM associate_day WHERE work_date = '2026-11-24'").fetchall()]
conn.close()
check("once mapped, two FCLM functions that are one process become one row of work",
      len(mine) == 1 and mine[0]["process"] == "Pick" and mine[0]["units"] == 800 and mine[0]["hours"] == 8, mine)
check("and re-sending a day dropped that day's template rows (the day is replaced)",
      all(x["employee_login"] == "zqa" for x in mine))
status = db.planning_data_status()
check("the status line counts what is loaded", status["roster"]["n"] == 1 and status["calendar"]["n"] == 2
      and status["associate_day"]["n"] >= 1, status)
r = c.get("/ld-management/settings?tab=data")
html = r.get_data(as_text=True)
check("the Data Upload tab lists the five files", all(k["label"] in html for k in application.XT_PLANNING_KINDS))
check("with templates for the two new ones", html.count("/planning-data/template/") == 2)
r = c.post("/ld-management/cross-training/model/config", data={"coverage_bar": "120"}, follow_redirects=True)
check("a coverage bar over 100% is refused", "between 50 and 100" in r.get_data(as_text=True))
c.post("/ld-management/cross-training/model/config", data={"coverage_bar": "90", "absence_pct": "5"})
check("valid settings are kept", application._xt_eval_settings()["coverage_bar"] == 0.9)
area = client(SITE)
with area.session_transaction() as s:
    s["login"], s["role"] = "am_x", "area_manager"
r = area.post("/ld-management/cross-training/planning-data/upload", data={"kind": "roster"})
check("an area manager cannot upload", r.status_code in (302, 403))
# Leave the scratch site as found: empty.
conn = db.get_db()
for table in ("employee_roster", "shift_calendar", "associate_day", "process_volume"):
    conn.execute(f"DELETE FROM {table}")
# The pattern is bound, not inlined: a literal % is psycopg2's placeholder.
conn.execute("DELETE FROM app_settings WHERE key LIKE ? OR key = ?", ("xt_eval_%", "xt_function_process_map"))
conn.commit()
conn.close()
db.set_current_site("SCN2")

print("\n4. The page")
application._XT_PREP_CACHE.clear()
c = client("SCN2")
html = c.get("/ld-management/cross-training?tab=model&_fields=sample", follow_redirects=True).get_data(as_text=True)
check("without associate-day data the page says there is nothing to replay",
      "Cross-training standards" in html and "Nothing to replay yet" in html)
check("and the old model is still there, folded below", "The path-by-path model" in html and "clm-legacy" in html)
r = c.get("/ld-management/cross-training?tab=model&sample=1", follow_redirects=True)
html = r.get_data(as_text=True)
check("sample data renders the grid", r.status_code == 200 and "Standards by shift" in html and "xs-grid" in html)
check("with the summary line and the reasons panel", "standards</span>" in html and " proposed" in html
      and "Why change these standards?" in html)
check("and says plainly that it is sample data", "made-up site" in html)
check("every verdict class is one of the six", all(v in ("keep", "increase", "decrease", "switch", "hire", "no_data")
      for v in __import__("re").findall(r'class="xs-cell xs-(\w+)"', html)))
check("without a model the reasons are the computed evidence", "Computed evidence" in html
      and "replayed shifts missed plan" in html)
d = (date.today() + timedelta(days=2)).isoformat()
r = c.get(f"/ld-management/cross-training?tab=model&sample=1&dp_process=Pack+Singles&dp_shift=early&dp_date={d}"
          "&dp_volume=30000", follow_redirects=True)
html = r.get_data(as_text=True)
check("the day plan answers", "Veterans available" in html and "Cross-trainees from" in html)
r = c.get("/ld-management/cross-training?tab=model&sample=1&dp_process=Pack+Singles&dp_date=nonsense&dp_volume=1",
          follow_redirects=True)
check("a bad date is answered, not raised", r.status_code == 200 and "Give a date" in r.get_data(as_text=True))

html = c.get("/ld-management/cross-training?tab=model&sample=1&_fields=lm_kind,lm_key,lm_dept,lm_shift,lm_all",
             follow_redirects=True).get_data(as_text=True)
check("the labour mix is the second section", html.find("Standards by shift") < html.find("Labour mix &amp; rate performance")
      < html.find("Plan one day"))
check("with the shift overview and the process detail", "Shift overview" in html and "Department &amp; process detail" in html)
check("defaulting to a whole week", 'class="active">Week' in html)
check("and Central, as the calendar marks it", ">Central</th>" in html)
check("cross-trainee rows open to their home departments", "by home department" in html and 'class="xm-toggle"' in html)
rows = html.count('<tr class="xm-sub-row"') + html.count("xm-chev")
all_html = c.get("/ld-management/cross-training?tab=model&sample=1&lm_all=1", follow_redirects=True).get_data(as_text=True)
check("View all processes shows more rows than the default", all_html.count("xm-chev") > html.count("xm-chev"),
      (html.count("xm-chev"), all_html.count("xm-chev")))
month = c.get("/ld-management/cross-training?tab=model&sample=1&lm_kind=month&lm_all=",
              follow_redirects=True).get_data(as_text=True)
check("Month switches the period", 'class="active">Month' in month and "Shift overview" in month)
bad = c.get("/ld-management/cross-training?tab=model&sample=1&lm_kind=week&lm_key=1999-01-04", follow_redirects=True)
check("a period not in the data falls back to the default", bad.status_code == 200 and "Shift overview" in bad.get_data(as_text=True))

from datetime import date as _today_cls
for sc in db.list_scenarios():
    db.delete_scenario(sc["id"])
html = c.get("/ld-management/cross-training?tab=model&sample=1&basis=forecast", follow_redirects=True).get_data(as_text=True)
check("the forecast can be judged against", "Forecast, next 4 weeks" in html and "scenario shifts" in html)
check("and the scenario editor shows how reliable each forecast has been", "Planned volume &amp; scenarios" in html
      and "% accurate" in html)
start = (date.today() + timedelta(days=1)).isoformat()
r = c.post("/ld-management/cross-training/model/scenario",
           data={"sample": "1", "name": "Peak", "start": start, "weeks": "2", "multiplier": "130",
                 "units|pack singles|early": "40000", "rate|pack singles|early": "150"}, follow_redirects=True)
saved = db.list_scenarios()
check("a scenario saves", len(saved) == 1 and saved[0]["name"] == "Peak" and abs(saved[0]["multiplier"] - 1.3) < 1e-9)
got = db.get_scenario(saved[0]["id"]) if saved else {}
check("with its lines, keyed as the evaluation reads them",
      got.get("lines", {}).get(("pack singles", "early")) == {"planned_units": 40000.0, "plan_rate": 150.0},
      got.get("lines"))
check("and the page judges against it straight away", "volume at 130%" in r.get_data(as_text=True))
bad = c.post("/ld-management/cross-training/model/scenario",
             data={"sample": "1", "name": "Bad", "start": start, "weeks": "2", "multiplier": "130",
                   "units|pack singles|early": "-5"}, follow_redirects=True)
check("a negative volume is refused", "must be numbers" in bad.get_data(as_text=True) and len(db.list_scenarios()) == 1)
bad = c.post("/ld-management/cross-training/model/scenario",
             data={"sample": "1", "name": "", "start": start, "weeks": "40"}, follow_redirects=True)
check("so is a nameless 40-week one", "Give the scenario a name" in bad.get_data(as_text=True))
c.post("/ld-management/cross-training/model/scenario", data={"sample": "1", "scenario_id": str(saved[0]["id"]), "delete": "1"})
check("and one can be deleted", not db.list_scenarios())
check("an unknown basis falls back to the past", "Judge on the past" in
      c.get("/ld-management/cross-training?tab=model&sample=1&basis=999", follow_redirects=True).get_data(as_text=True))

ctx = application.application.test_request_context(); ctx.push(); db.begin_request_scope()
paths = application._xt_paths_with_sources("general")
homes = db.xt_process_home_departments()
db.end_request_scope(); ctx.pop()
sample = X.sample_data(paths, owners={p["process"]: homes.get(p["process"]) for p in paths if p.get("process")}, weeks=18)
sprep = X.Prepared(sample, window_weeks=14)
hist = X.evaluate(sprep, paths, {})
ahead = X.evaluate(sprep, paths, {}, scenario=X.forecast_scenario(sprep))
same = sum(1 for k, cell in hist["cells"].items() if ahead["cells"][k]["verdict"] == cell["verdict"])
check("when the forecast says the weeks ahead look like the past, it reaches the past's verdicts (85%+)",
      same >= 0.85 * len(hist["cells"]), (same, len(hist["cells"])))
up = X.evaluate(sprep, paths, {}, scenario=dict(X.forecast_scenario(sprep), multiplier=1.3))
check("and more volume moves verdicts towards more people, never fewer",
      sum(1 for x in up["cells"].values() if x["verdict"] in ("increase", "hire", "switch")) >
      sum(1 for x in ahead["cells"].values() if x["verdict"] in ("increase", "hire", "switch")))

print("\n4b. Feedback, through a scratch site")
SITE2 = "ZQ91"
if not db.get_site(SITE2):
    db.create_site(SITE2, "Feedback scratch", "EU", "test")
db.set_current_site(SITE2)
db.init_db()
conn = db.get_db()
for table in ("xt_feedback", "xt_lend_rules"):
    conn.execute(f"DELETE FROM {table}")
conn.execute("DELETE FROM app_settings WHERE key = ?", ("xt_site_notes",))
conn.commit()
conn.close()
db.set_current_site("SCN2")
fc = client(SITE2)
anchor = {"back": "/ld-management/cross-training?tab=model", "anchor": "why-pack-early"}
r = fc.post("/ld-management/cross-training/model/feedback",
            data=dict(anchor, decision="disagree", process="Pack", shift="early", reason="", reason_kind=""),
            follow_redirects=True)
check("a disagreement without a reason is refused — the reason is what it learns from",
      "Say why you disagree" in r.get_data(as_text=True))
r = fc.post("/ld-management/cross-training/model/feedback",
            data=dict(anchor, decision="disagree", process="Pack", shift="early", reason_kind="cannot_lend",
                      reason="Pick is short every Friday", lend_department="Pick", lend_weekday=["4"], lend_scope="all",
                      verdict="keep", current_target="2", proposed_target="", predicted_coverage="1.0", sources="Pick"),
            follow_redirects=True)
db.set_current_site(SITE2)
rules = db.get_lend_rules()
fb = db.get_feedback()
check("a 'cannot lend' disagreement becomes a lending rule", len(rules) == 1 and rules[0]["department"] == "Pick"
      and rules[0]["weekdays"] == [4] and rules[0]["shift"] is None, rules)
check("and is kept as feedback with its reason", fb and fb[0]["reason"] == "Pick is short every Friday"
      and fb[0]["decision"] == "disagree")
check("the loader hands the rules to the evaluation", db.load_planning_data()["lend_rules"][0]["weekdays"] == [4])
db.set_current_site("SCN2")
fc.post("/ld-management/cross-training/model/feedback",
        data=dict(anchor, decision="applied", process="Pack", shift="early", verdict="increase", current_target="1",
                  proposed_target="2", predicted_coverage="1.7", sources="Pick"))
db.set_current_site(SITE2)
check("a predicted coverage outside 0-100% is not stored as one", db.get_feedback()[0]["predicted_coverage"] is None)
conn = db.get_db()
for i in range(3):
    conn.execute("INSERT INTO xt_feedback (created_at, created_by, process, shift, verdict, current_target, proposed_target, "
                 "predicted_coverage, sources, decision) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("2026-11-01T09:00:00", "test", "Pack", "early", "decrease", 2, 1, 0.97, json.dumps(["Pick"]), "applied"))
conn.commit()
conn.close()
ctx = application.application.test_request_context(); ctx.push()
learning = application._xt_learning(prep)
ctx.pop()
scored = [o for o in learning["outcomes"] if o.get("achieved") is not None]
check("applied recommendations old enough are scored against the history since", len(scored) == 3
      and all(o["achieved"] == 0.0 for o in scored), [(o.get("achieved"), o.get("status")) for o in learning["outcomes"]])
check("three that covered less than predicted raise the bar, by at most 5 points",
      learning["calibration"] == {"pack": 0.05}, learning["calibration"])
fc.post("/ld-management/cross-training/model/learning", data={"notes": "Peak starts 1 Dec; RSP layout change in week 49"})
db.set_current_site(SITE2)
check("site notes are saved", db.get_site_notes().startswith("Peak starts 1 Dec"))
fc.post("/ld-management/cross-training/model/learning", data={"delete_feedback": str(fb[0]["id"])})
db.set_current_site(SITE2)
check("removing a piece of feedback removes the rule it created", not db.get_lend_rules())
am = client(SITE2)
with am.session_transaction() as sess:
    sess["login"], sess["role"] = "am_x", "area_manager"
r = am.post("/ld-management/cross-training/model/feedback", data=dict(anchor, decision="agree", process="Pack", shift="early"))
check("an area manager cannot give feedback", r.status_code in (302, 403))
db.set_current_site("SCN2")
html = c.get("/ld-management/cross-training?tab=model&sample=1", follow_redirects=True).get_data(as_text=True)
check("on sample data, feedback is off and says why", "Feedback is off on sample data" in html and 'name="decision"' not in html)

# What the explanation model is given: on a real-site view, the feedback,
# notes, rules and track record all travel with the changes.
seen = []
real_view = application.build_standards_view


def view_with_learning(scenario="general"):
    v = real_view(scenario)
    v["learning"] = {"notes": "Peak starts 1 Dec", "rules": [{"department": "Pick", "shift": None, "weekdays": [4],
                                                              "reason": "short on Fridays"}],
                     "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
                     "outcomes": [{"process": v["changes"][0]["process"], "shift": "early", "created_at": "2026-11-01",
                                   "proposed_target": 3, "predicted_coverage": 0.96, "achieved": 0.9, "shifts": 20}]}
    v["changes"][0]["feedback"] = [{"created_at": "2026-11-20", "decision": "disagree", "verdict": "decrease",
                                    "current_target": 5, "proposed_target": 2, "reason": "Peak is coming", "reason_kind": "volume"}]
    return v


def capture(body):
    seen.append(json.loads(body["messages"][0]["content"]))
    return {"content": [{"type": "text", "text": json.dumps({"changes": []})}], "stop_reason": "end_turn"}


application.build_standards_view = view_with_learning
bedrock_client.set_transport(capture)
try:
    c.get("/ld-management/cross-training/model/standards-explain?sample=1")
finally:
    application.build_standards_view = real_view
    bedrock_client.set_transport(None)
sent = seen[0] if seen else {}
first = (sent.get("changes") or [{}])[0]
check("the model is given the site notes and the lending rules", sent.get("site_notes") == "Peak starts 1 Dec"
      and sent.get("lending_rules") == [{"department": "Pick", "shift": "all", "weekdays": ["Fri"], "reason": "short on Fridays"}])
check("and, per change, the managers' feedback and the track record",
      first.get("manager_feedback") and first["manager_feedback"][0]["reason"] == "Peak is coming"
      and first.get("track_record") and first["track_record"][0]["achieved_coverage"] == 0.9, first.keys())

print("\n5. The generated reasons")
view_changes = []


def stub(body):
    payload = json.loads(body["messages"][0]["content"])
    view_changes.extend(payload["changes"])
    return {"content": [{"type": "text", "text": json.dumps({"changes": [
        {"id": ch["id"], "explanation": f"{ch['replayed_shifts']} shifts were replayed. It needs 31337 more."}
        if i == 0 else
        {"id": ch["id"], "explanation": f"{ch['replayed_shifts']} shifts were replayed and the target is "
                                        f"{ch['current_target']}."}
        for i, ch in enumerate(payload["changes"])]})}], "stop_reason": "end_turn"}


bedrock_client.set_transport(stub)
html = c.get("/ld-management/cross-training?tab=model&sample=1", follow_redirects=True).get_data(as_text=True)
check("with a model, the panel says so and the loader is on the page",
      "Amazon Bedrock explanations" in html and "/model/standards-explain" in html)
r = c.get("/ld-management/cross-training/model/standards-explain?sample=1")
data = r.get_json()
check("the explain endpoint fills the reasons", data["status"] == "ok" and data["slots"], data.get("status"))
check("a sentence with an invented figure is dropped", data["dropped"] >= 1
      and not any("31337" in v for v in data["slots"].values()))
check("what is kept is marked as generated", all("Generated" in v for v in data["slots"].values()))
check("no login leaves the server", not any("smp0" in json.dumps(ch) for ch in view_changes))
bedrock_client.set_transport(lambda body: (_ for _ in ()).throw(RuntimeError("down")))
data = c.get("/ld-management/cross-training/model/standards-explain?sample=1").get_json()
check("when the model is down nothing is replaced and nothing errors", data["status"] == "error"
      and not data["slots"])
bedrock_client.set_transport(None)

print("\n" + "=" * 62)
if fails:
    print(f"{len(fails)} CROSS-TRAINING PLANNING CHECK(S) FAILED")
    for name in fails:
        print("  -", name)
    sys.exit(1)
print("ALL CROSS-TRAINING PLANNING CHECKS PASSED")
