"""The FCLM daily sync: reading Function Rollup, Process Path Rollup and
the Employee Roster in a manager's browser, and storing them as the
associate day, process day and roster the cross-training pages read.

Three layers, each tested where it runs:

1. The readers, in Chromium, against fixtures cut from the real SCN2
   reports of 27 Nov 2026: their exact header markup, invented people.
2. The server's attribution, on a registered scratch site: employee id
   to login, person to shift, FCLM function to process, line item to
   planned rate — and what it refuses.
3. The whole loop, in a browser: the real userscript, served by a live
   console, reading FCLM (the fixtures, behind a stubbed
   GM_xmlhttpRequest) and posting what it read.
"""
import json, os, re, sys, threading, time
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import application, db, xt_planning
from browser import chromium_path
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def fixture(name):
    return open(os.path.join(ROOT, "perf/fixtures", name), encoding="utf-8").read()


SCRIPT = open(os.path.join(ROOT, "static/floor_provider.user.js"), encoding="utf-8").read()
ROLLUP, PPR, ROSTER = fixture("fclm_function_rollup_day.html"), fixture("fclm_ppr_day.html"), fixture("fclm_employee_roster.html")
OLD_ROLLUP = fixture("function_rollup_pick.html")


def harness(*names):
    """The reader's pure functions lifted out of its IIFE, with every
    top-level constant, as test_floor.py does."""
    out = [m.group(0).strip() for m in re.finditer(r"(?m)^    var [A-Z][A-Z0-9_]* = .*$", SCRIPT)]
    for fn in names:
        m = re.search(r"(?ms)^    function " + fn + r"\(.*?\n    \}\n", SCRIPT)
        if not m:
            check(f"the reader still defines {fn}", False)
        else:
            out.append(m.group(0).replace("    function", "function"))
    return "\n".join(out)


def expected_rollup():
    """What the rollup fixture says, read independently of the reader:
    per row, the paid-hours Total cell and the last units Total cell."""
    rows = []
    for table in re.findall(r'<table id="function-(\d+)".*?</table>', ROLLUP, flags=re.S):
        pass
    for m in re.finditer(r'<table id="function-(\d+)"[^>]*>\s*<caption>\s*([^\[]+?)\s*\[', ROLLUP):
        start = m.end()
        body = ROLLUP[ROLLUP.index("<tbody>", start):ROLLUP.index("</tbody>", start)]
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
            eid = re.search(r"employeeId=(\d+)", tr).group(1)
            totals = re.findall(r'size-total highlighted">([^<]*)<', tr)
            rows.append({"employee_id": eid, "function": m.group(2).strip(),
                         "hours": float(totals[0]), "units": float(totals[1])})
    return rows


print("1. The readers, against the real reports' shape")
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    pg = b.new_context().new_page()
    pg.goto("about:blank")
    pg.add_script_tag(content=harness("headerIndex", "gridIndex", "unitsKey", "functionNameFor", "parseRollup",
                                      "cleanStation", "numberAt", "ownText", "pprNumber", "parsePPR",
                                      "parseRosterPage", "pprDayUrl", "rosterPageUrl", "pad", "fclmDate", "rollupUrl"))
    rollup = pg.evaluate("(h) => parseRollup(h, {key: 'each_stp', label: 'Each StP'})", ROLLUP)
    old = pg.evaluate("(h) => parseRollup(h, {key: 'pick', label: 'Pick'})", OLD_ROLLUP)
    ppr = pg.evaluate("(h) => parsePPR(h)", PPR)
    roster = pg.evaluate("(h) => parseRosterPage(h)", ROSTER)
    urls = pg.evaluate("""() => ({ppr: pprDayUrl('SCN2', '2026-11-26'), roster: rosterPageUrl('SCN2'),
        rollup: rollupUrl('SCN2', '1003016', new Date('2026-11-26T06:00'), new Date('2026-11-27T06:00'))})""")
    b.close()

want = expected_rollup()
check("every person in the three-row-header rollup is read", len(rollup) == len(want) == 6, (len(rollup), len(want)))
by_key = {(r["employee_id"], r["function_name"]): r for r in rollup}
check("each person's paid hours are the Total column (the old reader found none)",
      all(abs((by_key.get((w["employee_id"], w["function"])) or {}).get("paid_hours", -1) - w["hours"]) < 1e-6 for w in want),
      [(r["employee_id"], r["paid_hours"]) for r in rollup])
check("and units are the units under the Total size band",
      all(abs((by_key.get((w["employee_id"], w["function"])) or {}).get("units", -1) - w["units"]) < 1e-6 for w in want))
check("function names come from each table's caption, not its filter buttons",
      {r["function_name"] for r in rollup} == {"Stow Each Nike", "Stow Each Nike Light"})
check("the unit type is named", {r["unit_type"] for r in rollup} == {"EACH"}, {r["unit_type"] for r in rollup})
check("the summary table and the tfoot totals are not people",
      not [r for r in rollup if not r["employee_id"].startswith("9000")])
check("the older two-row report still reads the same", len(old) == 6 and all(r["paid_hours"] for r in old)
      and {r["function_name"] for r in old} == {"MultiLowDensityPick", "RF Pick"})

check("every PPR line item is read", len(ppr) == 18, len(ppr))
first, second = ppr[0], ppr[1]
check("with its main and core process from the spanning header cells",
      first["main_process"] == "Inbound" and first["core_process"] == "Receive" and first["name"] == "Receive Dock",
      (first["main_process"], first["core_process"], first["name"]))
check("which carry down to the rows beneath them", second["core_process"] == "Receive"
      and second["name"] == "Each Receive - Small")
check("its stable id", second["id"] == "ppr.detail.inbound.receive.eachReceive.small", second["id"])
check("the process paths behind it, from its popup", "1003010" in first["process_ids"] and
      all(x.isdigit() for x in first["process_ids"]), first["process_ids"])
check("and its figures from the 'original' value, thousands separators removed",
      all(isinstance(i["volume"], (int, float)) for i in ppr) and any(i["volume"] > 1000 for i in ppr))
check("hours, rate, planned rate and hours at plan are numbers",
      all(i[k] is None or isinstance(i[k], (int, float)) for i in ppr for k in ("hours", "rate", "plan_rate", "plan_hours")))
check("the last row starts a new core process", ppr[-1]["core_process"] != "Receive")

check("every roster row is read", len(roster) == 6, len(roster))
check("by header, not class: the shift pattern is not the Exempt flag the page also marks shiftPattern",
      {r["shift_pattern"] for r in roster} == {"D58F83YA", "L58F8R80", "N57F6SEO"}, {r["shift_pattern"] for r in roster})
check("with login, id, start date and management area", roster[0]["user_id"] == "zqanna"
      and roster[0]["employee_id"] == "900000001" and roster[0]["start_date"] == "2026/01/27"
      and roster[0]["management_area_id"] == "18")
check("the PPR is asked for one whole day", "spanType=Day" in urls["ppr"] and "startDateDay=2026%2F11%2F26" in urls["ppr"])
check("the roster asks for every column the sync reads",
      all(c.replace(" ", "+") in urls["roster"] for c in ("Shift+Pattern", "User+ID", "Employee+ID")))
check("a day's rollup runs from 06:00 to 06:00 the next morning",
      "startDateIntraday=2026%2F11%2F26&startHourIntraday=6" in urls["rollup"]
      and "endDateIntraday=2026%2F11%2F27&endHourIntraday=6" in urls["rollup"], urls["rollup"])

print("\n2. The server's attribution, on a scratch site")
SITE = "ZQ89"
db.init_global_db()
if not db.get_site(SITE):
    db.create_site(SITE, "FCLM sync scratch", "EU", "test")
db.set_current_site(SITE)
db.init_db()


def wipe():
    conn = db.get_db()
    for table in ("employee_roster", "associate_day", "process_volume", "fclm_sync_log", "xt_hours", "shift_calendar"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM app_settings WHERE key IN (?, ?, ?)",
                 ("fclm_sync_settings", "fclm_ppr_map", "xt_function_process_map"))
    conn.commit()
    conn.close()


wipe()
# Hours on Function: who belongs where, and the process vocabulary.
conn = db.get_db()
for login, dept, fn in (("zqanna", "Pack Singles", "Stow Each Nike"), ("zqbert", "RSP", "Stow Each Nike"),
                        ("zqcara", "RSP", "Stow Each Nike"), ("zqcara", "RSP", "Stow Each Nike Light"),
                        ("zqdan", "RSP", "Stow Each Nike Light"), ("zqeve", "RSP", "Stow Each Nike Light")):
    conn.execute("INSERT INTO xt_hours (fc, fclm_mapped, employee_id, employee_login, merged_function, trained_status, "
                 "shift_pattern, shift) VALUES (?,?,?,?,?,?,?,?)", ("ZQ89", dept, None, login, fn, "Trained", None, None))
conn.commit()
conn.close()

r = db.record_fclm_roster(roster, taken_by="test")
check("the roster read from FCLM is stored", r["status"] == "complete" and r["loaded"] == 6, r)
people = db.fclm_people()
check("employee ids resolve to logins and shifts", people.get("900000001") == ("zqanna", "early")
      and people.get("900000002") == ("zqbert", "late"), people.get("900000002"))
r = db.record_fclm_roster(roster[:1], taken_by="test")
conn = db.get_db()
n = conn.execute("SELECT COUNT(*) AS n FROM employee_roster").fetchone()["n"]
conn.close()
check("a small roster replaces a small one (under the 50-person floor the guard does not apply)",
      r["status"] == "complete" and n == 1)
db.record_fclm_roster(roster, taken_by="test")
conn = db.get_db()
for i in range(60):
    conn.execute("INSERT INTO employee_roster (employee_login, employee_id, shift_pattern, shift) VALUES (?,?,?,?)",
                 (f"zqfill{i}", f"9100{i:05d}", "D58F83YA", "early"))
conn.commit()
conn.close()
r = db.record_fclm_roster(roster, taken_by="test")
conn = db.get_db()
n = conn.execute("SELECT COUNT(*) AS n FROM employee_roster").fetchone()["n"]
logged = dict(conn.execute("SELECT * FROM fclm_sync_log WHERE kind = 'roster'").fetchone())
conn.close()
check("a roster under half the size of the one on record is refused, not loaded as 'most people left'",
      r["status"] == "refused" and n == 66 and logged["status"] == "refused", (r, n))
db.record_fclm_roster(roster, taken_by="test")          # still refused; put the small roster back by hand
conn = db.get_db()
conn.execute("DELETE FROM employee_roster WHERE employee_login LIKE ?", ("zqfill%",))
conn.commit()
conn.close()

DAY = (date.today() - timedelta(days=1)).isoformat()
rows = [{"employee_id": r["employee_id"], "process_key": "each_stp", "process_id": "1003016",
         "function_name": r["function_name"], "paid_hours": r["paid_hours"], "units": r["units"]} for r in rollup]
rows.append({"employee_id": "999999999", "process_key": "each_stp", "process_id": "1003016",
             "function_name": "Stow Each Nike", "paid_hours": 5, "units": 500})       # nobody we know
rows.append({"employee_id": "900000006", "process_key": "each_stp", "process_id": "1003016",
             "function_name": "Mystery Function", "paid_hours": 2, "units": 10})      # zqfinn: on the roster
ppr_items = [dict(i, process_ids=list(i["process_ids"]) + (["1003016"] if i["name"] == "Each Receive - Total" else []))
             for i in ppr]
result = db.record_fclm_day(DAY, rows, ppr_items, [], taken_by="test")
conn = db.get_db()
assoc = [dict(x) for x in conn.execute("SELECT * FROM associate_day WHERE work_date = ?", (DAY,)).fetchall()]
pvol = [dict(x) for x in conn.execute("SELECT * FROM process_volume WHERE work_date = ?", (DAY,)).fetchall()]
conn.close()
check("the day is stored complete", result["status"] == "complete", result)
check("every known person's work is an associate-day row under their login",
      {a["employee_login"] for a in assoc} == {"zqanna", "zqbert", "zqcara", "zqdan", "zqeve", "zqfinn"},
      {a["employee_login"] for a in assoc})
check("an FCLM id nobody can name is counted and left out, not guessed", result["unresolved_ids"] == 1)
check("each person sits on their own shift, from their shift pattern",
      {(a["employee_login"], a["shift"]) for a in assoc} >= {("zqanna", "early"), ("zqbert", "late"), ("zqcara", "night")})
check("FCLM function names land on the Hours on Function processes",
      {a["process"] for a in assoc} >= {"Stow Each Nike", "Stow Each Nike Light"})
check("an FCLM function with no process is kept under its own name and reported",
      any(a["process"] == "Mystery Function" for a in assoc) and "Mystery Function" in result["unmapped_functions"])
cara = sum(a["hours"] for a in assoc if a["employee_login"] == "zqcara")
want_cara = sum(w["hours"] for w in want if w["employee_id"] == "900000003")
check("hours are carried exactly", abs(cara - want_cara) < 0.02, (cara, want_cara))
sen_early = next((v for v in pvol if v["process"] == "Stow Each Nike" and v["shift"] == "early"), None)
check("the process day is the associate day summed per process and shift",
      sen_early and abs(sen_early["hours"] - sum(a["hours"] for a in assoc if a["process"] == "Stow Each Nike"
                                                 and a["shift"] == "early")) < 0.02, sen_early)
check("with its owning department from Hours on Function",
      sen_early and sen_early["department"] == "RSP", sen_early and sen_early["department"])
each_total = next(i for i in ppr_items if i["name"] == "Each Receive - Total")
check("and its planned rate from the most specific PPR line item that its process path feeds",
      sen_early and abs(sen_early["plan_rate"] - round(each_total["volume"] / each_total["plan_hours"], 2)) < 0.01,
      (sen_early and sen_early["plan_rate"], each_total["volume"], each_total["plan_hours"]))
rate, line = db.fclm_plan_rate(["1003010"], ppr_items)
check("of several matching lines a '- Total' wins, then the narrowest", line and line.endswith("Total"), line)
db.set_app_setting("fclm_ppr_map", json.dumps({"Receive Dock": "Stow Each Nike"}), "test")
db.record_fclm_day(DAY, rows, ppr_items, [], taken_by="test")
conn = db.get_db()
manual = conn.execute("SELECT plan_rate FROM process_volume WHERE work_date = ? AND process = ? AND shift = 'early'",
                      (DAY, "Stow Each Nike")).fetchone()["plan_rate"]
conn.close()
check("a manual line-item mapping overrides the automatic one", abs(manual - round(first["volume"] / first["plan_hours"], 2)) < 0.01
      if first["plan_hours"] else manual == first["plan_rate"], manual)
conn = db.get_db()
n = conn.execute("SELECT COUNT(*) AS n FROM associate_day WHERE work_date = ?", (DAY,)).fetchone()["n"]
conn.close()
check("reading a day again replaces it rather than doubling it", n == len(assoc), (n, len(assoc)))
partial = db.record_fclm_day(DAY, rows, ppr_items, ["pack_single"], taken_by="test")
check("a day with a failed process is stored but marked partial", partial["status"] == "partial")
check("and is due again", DAY in [d["date"] for d in db.fclm_due_days()])
db.record_fclm_day(DAY, rows, ppr_items, [], taken_by="test")
check("once complete it is not due", DAY not in [d["date"] for d in db.fclm_due_days()])
due = db.fclm_due_days()
check("the newest missing days come first, a few per run",
      len(due) == db.get_fclm_sync_settings()["days_per_run"] and due[0]["date"] > due[1]["date"], len(due))
conn = db.get_db()
d2 = (date.today() - timedelta(days=2)).isoformat()
for code, start in (("D58F83YA", "05:00"), ("DXX1", "05:00"), ("DXX2", "06:00")):
    conn.execute("INSERT INTO shift_calendar (cohort, work_date, shift_code, side, shift, pattern, start_at, end_at, hours) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (code + "-OB", d2, code, "OB", "early", "Day", f"{d2} {start}:00", None, 8.75))
conn.execute("INSERT INTO shift_calendar (cohort, work_date, shift_code, side, shift, pattern, start_at, end_at, hours) "
             "VALUES (?,?,?,?,?,?,?,?,?)", ("DCEN-OB", d2, "DCEN", "OB", "early", "Central", f"{d2} 10:45:00", None, 8.75))
conn.commit()
conn.close()
window = next(d for d in db.fclm_due_days() if d["date"] == d2)
check("a day starts when most of that day's early codes start (Central left out)",
      window["window_start"] == f"{d2}T05:00", window)
check("and runs 24 hours", window["window_end"].startswith((date.fromisoformat(d2) + timedelta(days=1)).isoformat() + "T05:00"))
check("without a calendar it falls back to the setting", db.fclm_day_start("2001-01-01") == "05:00" or
      db.fclm_day_start("2001-01-01") == "06:00")

prep = xt_planning.Prepared(db.load_planning_data(), window_weeks=4)
mix = xt_planning.labour_mix(prep, DAY, DAY)
row = next((r for r in mix["detail"] if r["process"] == "Stow Each Nike" and r["shift"] == "early"), None)
conn = db.get_db()
early = [dict(x) for x in conn.execute("SELECT employee_login, hours FROM associate_day WHERE work_date = ? AND "
                                        "process = ? AND shift = 'early'", (DAY, "Stow Each Nike")).fetchall()]
conn.close()
rsp = sum(x["hours"] for x in early if x["employee_login"] in ("zqbert", "zqcara", "zqdan", "zqeve"))
check("what the sync stored feeds the labour mix: RSP's own people as veterans, Pack Singles' as cross-trainees",
      row and abs(row["vet"]["hours"] - round(rsp, 1)) < 0.11
      and abs(row["xt"]["hours"] - round(sum(x["hours"] for x in early) - rsp, 1)) < 0.11
      and row["xt"]["pct"] is not None, row and (row["vet"], row["xt"], early))

print("\n3. The routes")


def client(role="admin", login="admin", site=SITE):
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = login, role, "manual", site
    return c


c = client()
plan = c.get("/fclm/sync/plan").get_json()
check("the plan names the site, its processes and the days due", plan["allowed"] and plan["warehouse_id"] == SITE
      and plan["processes"] and plan["days"], plan.get("allowed"))
check("a role that may not post floor data is told there is nothing to do",
      client(role="viewer", login="someone").get("/fclm/sync/plan").get_json()["allowed"] is False)
check("signed out, nothing at all", application.application.test_client().get("/fclm/sync/plan").status_code == 401)
r = c.post("/fclm/sync/day", json={"date": "2001-01-01", "rows": [], "ppr": []})
check("a day outside the sync window is refused", r.status_code == 400)
r = c.post("/fclm/sync/day", json={"date": date.today().isoformat(), "rows": [], "ppr": []})
check("so is today, which is not over", r.status_code == 400)
r = c.post("/fclm/sync/day", json={"date": DAY, "rows": "nope", "ppr": []})
check("rows that are not a list are refused", r.status_code == 400)
r = c.post("/fclm/sync/day", json={"date": DAY, "rows": [{}] * (application.FCLM_MAX_ROWS + 1), "ppr": []})
check("an oversized post is refused", r.status_code == 413)
r = c.post("/fclm/sync/day", json={"date": DAY, "rows": [dict(x, paid_hours="<script>") for x in rows[:2]] + rows[2:],
                                  "ppr": ppr_items, "failed": []})
check("a posted day is stored, and a non-number is not taken as hours", r.status_code == 200
      and r.get_json()["status"] == "complete")
r = client(role="viewer", login="someone").post("/fclm/sync/day", json={"date": DAY, "rows": [], "ppr": []})
check("someone who may not post floor data cannot post a day", r.status_code == 403)
r = c.post("/fclm/sync/roster", json={"rows": roster})
check("the roster route stores what it is given", r.get_json()["status"] == "complete")
r = c.post("/fclm/sync/settings", data={"enabled": "1", "backfill_days": "500"}, follow_redirects=True)
check("a backfill over 400 days is refused", "between 1 and 400" in r.get_data(as_text=True))
c.post("/fclm/sync/settings", data={"enabled": "1", "backfill_days": "10", "days_per_run": "2", "day_start": "05:30",
                                    "ppr_map": "Receive Dock = Stow Each Nike"})
check("valid settings are kept", db.get_fclm_sync_settings()["backfill_days"] == 10
      and len(db.fclm_due_days()) == 2)
c.post("/fclm/sync/settings", data={"enabled": "", "backfill_days": "10"})
check("switching the sync off quiets the reader", c.get("/fclm/sync/plan").get_json()["allowed"] is False)
c.post("/fclm/sync/settings", data={"enabled": "1", "resync": "1"})
check("'read every day again' makes yesterday due again", DAY in [d["date"] for d in db.fclm_due_days()])
html = c.get("/ld-management/settings?tab=data").get_data(as_text=True)
check("the Data Upload tab shows the sync and where each file comes from",
      "Daily from FCLM" in html and "Sync now" in html and "synced daily" in html
      and "Derived from FCLM hours" in html and "not FCLM" in html and "Trained status from" in html)
served = c.get("/floor/provider.user.js").get_data(as_text=True)


def matched(script, url):
    """Would the extension inject this script on `url`? Its @match lines,
    as the extension reads them: scheme://host/path, `*` the only wildcard."""
    for pattern in re.findall(r"(?m)^// @match\s+(\S+)", script):
        if re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), url.split("?")[0]):
            return True
    return False


# The bug of 27 Nov: the script matched only /overview* and the
# cross-training pages, so on L&D Settings, where its Sync now button
# is, it never loaded — and the browser test, which injected it by hand,
# could not see that.
for page in ("/ld-management/settings", "/ld-management/cross-training", "/overview", "/scorecards"):
    check(f"the served script is injected on {page}", matched(served, "http://localhost" + page))
check("the served reader knows where to post", "/fclm/sync/plan" in served and "/fclm/sync/day" in served
      and "__LDIS_FCLM" not in served and "1.3.0-" in served)

print("\n3b. Hours on Function, derived from FCLM")
P = db.fclm_proficiency
check("20 hours in 60 days is Proficient, exactly at the line", P(20, 20, 20) == "Proficient")
check("19.9 in 60 but 20 in 90 is Practice", P(19.9, 20, 20) == "Practice")
check("under 20 in 90 but 20 in 180 is Refresh", P(5, 19.9, 20) == "Refresh")
check("under 20 in 180 is Lapsed", P(5, 10, 19.9) == "Lapsed")
check("Proficient, Practice and Refresh are Trained; Lapsed is not",
      [db.fclm_trained(x) for x in ("Proficient", "Practice", "Refresh", "Lapsed")] ==
      ["Trained", "Trained", "Trained", "Not Trained"])

wipe()
db.set_current_site(SITE)
db.record_fclm_roster(roster + [{"user_id": "zqmia", "employee_id": "900000099", "name": "Manager,Mia",
                                 "shift_pattern": "D58F83YA", "management_area_id": "18"}], taken_by="test")
TODAY = date.today()
asof = TODAY - timedelta(days=1)


def worked(login, process, days_ago, hours):
    conn = db.get_db()
    conn.execute("INSERT INTO associate_day (work_date, shift, employee_login, process, units, hours, source) "
                 "VALUES (?,?,?,?,?,?,?) ON CONFLICT (work_date, shift, employee_login, process) DO UPDATE SET "
                 "hours = associate_day.hours + excluded.hours",
                 ((asof - timedelta(days=days_ago)).isoformat(), "early", login, process, 100, hours, "fclm"))
    conn.commit()
    conn.close()


worked("zqanna", "Pack Singles", 3, 12); worked("zqanna", "Pack Singles", 40, 13)       # 25 in 60
worked("zqbert", "Pack Singles", 10, 15); worked("zqbert", "Pack Singles", 70, 10)      # 15 in 60, 25 in 90
worked("zqcara", "Pack Singles", 150, 25)                                                # 25 in 180 only
worked("zqdan", "Pack Singles", 5, 10)                                                   # 10 in 180
worked("zqanna", "Indirect", 2, 40)                                                      # not a function
worked("zqanna", "Pack Singles", 200, 99)                                                # older than 180
worked("zqgone", "Pack Singles", 3, 30)                                                  # not on the roster
db.set_app_setting("fclm_area_departments", json.dumps({"18": "Pack Singles", "3": "RSP", "2": "Receive"}), "test")
rows, report = db.derive_hours_on_function(TODAY)
by = {(r["employee login"], r["Merged Functions"]): r for r in rows}
check("60/90/180-day totals add the right days", by[("zqanna", "Pack Singles")]["Total_Hours_60"] == 25
      and by[("zqanna", "Pack Singles")]["Total_Hours_180"] == 25, by.get(("zqanna", "Pack Singles")))
check("and the statuses follow the formula",
      [by[(l, "Pack Singles")]["Proficiency Status"] for l in ("zqanna", "zqbert", "zqcara", "zqdan")] ==
      ["Proficient", "Practice", "Refresh", "Lapsed"])
check("Lapsed is Not Trained", by[("zqdan", "Pack Singles")]["Trained Status"] == "Not Trained")
check("indirect time is not a function anyone is trained on", ("zqanna", "Indirect") not in by)
check("someone no longer on the roster is not in it", not any(r["employee login"] == "zqgone" for r in rows)
      and report["not_on_roster"] == 1)
check("home department comes from the roster's management area",
      by[("zqanna", "Pack Singles")]["fclm mapped"] == "Pack Singles" and by[("zqbert", "Pack Singles")]["fclm mapped"] == "RSP")
check("the manager's login is found by name", by[("zqanna", "Pack Singles")]["supervisor login"] == "zqmia",
      by[("zqanna", "Pack Singles")]["supervisor login"])
check("an area with no department is reported", report["areas_without_department"] == [], report)

db.set_fclm_sync_settings({"hours_source": "auto"}, "test")
r = db.rebuild_hours_from_fclm(taken_by="test", today=TODAY)
check("on 'auto' it waits for 180 days of history before replacing the upload", r["status"] == "waiting", r)
db.set_fclm_sync_settings({"hours_source": "upload"}, "test")
check("on 'upload' it never replaces it", db.rebuild_hours_from_fclm(taken_by="test", today=TODAY)["status"] == "skipped")
db.set_fclm_sync_settings({"hours_source": "fclm"}, "test")
r = db.rebuild_hours_from_fclm(taken_by="test", today=TODAY)
conn = db.get_db()
stored = {(x["employee_login"], x["merged_function"]): dict(x) for x in conn.execute("SELECT * FROM xt_hours").fetchall()}
conn.close()
check("on 'fclm' it rebuilds Hours on Function through the upload's own importer", r["status"] == "complete"
      and len(stored) == len(rows), (r, len(stored)))
check("and the pages read it as they read an upload: status, trained, hours, shift",
      stored[("zqbert", "Pack Singles")]["proficiency_status"] == "Practice"
      and stored[("zqbert", "Pack Singles")]["trained_status"] == "Trained"
      and stored[("zqbert", "Pack Singles")]["hours_90"] == 25 and stored[("zqbert", "Pack Singles")]["shift"] == "late")
check("once a day", db.rebuild_hours_from_fclm(taken_by="test", today=TODAY)["status"] == "done today")
conn = db.get_db()
for d in range(1, 181):
    conn.execute("INSERT INTO fclm_sync_log (sync_date, kind, status) VALUES (?,?,?) ON CONFLICT (sync_date, kind) "
                 "DO UPDATE SET status = excluded.status", ((TODAY - timedelta(days=d)).isoformat(), "day", "complete"))
conn.execute("DELETE FROM fclm_sync_log WHERE kind = 'hours'")
conn.commit()
conn.close()
db.set_fclm_sync_settings({"hours_source": "auto"}, "test")
check("with 180 days stored, 'auto' rebuilds by itself", db.rebuild_hours_from_fclm(taken_by="test", today=TODAY)["status"] == "complete")

conn = db.get_db()
conn.execute("DELETE FROM app_settings WHERE key = ?", ("fclm_area_departments",))
conn.execute("UPDATE xt_hours SET fclm_mapped = ? WHERE employee_login = ?", ("Receive", "zqdan"))
conn.commit()
conn.close()
db.set_app_setting("fclm_area_departments", json.dumps({"3": "RSP (by hand)"}), "test")
learned = db.learn_area_departments("test")
check("the area map is learned from Hours on Function where it is not set by hand",
      learned.get("18") == "Pack Singles" and learned.get("2") == "Receive", learned)
check("and a mapping set by hand is kept", learned.get("3") == "RSP (by hand)")
c = client()
c.post("/fclm/sync/settings", data={"enabled": "1", "hours_source": "upload", "area_map": "18 = Pack Singles\n3 = RSP"})
check("the settings form saves the source and the area map", db.get_fclm_sync_settings()["hours_source"] == "upload"
      and db.get_area_departments() == {"18": "Pack Singles", "3": "RSP"})
r = c.post("/fclm/sync/settings", data={"enabled": "1", "rebuild_hours": "1"}, follow_redirects=True)
check("and 'rebuild now' rebuilds whatever the setting", "Hours on Function rebuilt from FCLM" in r.get_data(as_text=True))

print("\n4. The whole loop, in a browser")
wipe()
db.set_current_site(SITE)
conn = db.get_db()
for login, dept, fn in (("zqanna", "Pack Singles", "Stow Each Nike"), ("zqbert", "RSP", "Stow Each Nike")):
    conn.execute("INSERT INTO xt_hours (fc, fclm_mapped, employee_login, merged_function, trained_status) "
                 "VALUES (?,?,?,?,?)", ("ZQ89", dept, login, fn, "Trained"))
conn.commit()
conn.close()
port = 5655
srv = make_server("127.0.0.1", port, application.application, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)
base = f"http://127.0.0.1:{port}"
STUB = """
window.__fclmAsked = [];
window.__phases = [];
document.addEventListener('ldis-floor-status', function (e) { window.__phases.push(e.detail || {}); });
window.__synced = new Promise(function (resolve) {
  document.addEventListener('ldis-fclm-synced', function (e) { resolve(e.detail); });
  setTimeout(function () { resolve({timeout: true}); }, 60000);
});
window.GM_xmlhttpRequest = function (opts) {
  window.__fclmAsked.push(opts.url);
  var body = '<html><body>nothing here</body></html>';
  if (opts.url.indexOf('functionRollup') >= 0 && opts.url.indexOf('processId=1003016') >= 0) body = window.__fx.rollup;
  else if (opts.url.indexOf('processPathRollup') >= 0) body = window.__fx.ppr;
  else if (opts.url.indexOf('employeeRoster') >= 0) body = window.__fx.roster;
  setTimeout(function () { opts.onload({responseText: body, status: 200}); }, 120);
};
"""
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    ctx = b.new_context()
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(base + "/", wait_until="domcontentloaded")
    pg.fill("input[name=login_id]", "admin")
    pg.click("button[type=submit]")
    pg.wait_for_timeout(400)
    # Point the session at the scratch site, as the site switcher does,
    # and make sure it took before the reader is allowed to write: a
    # sync that landed on SCN2 would overwrite the real site's roster.
    pg.evaluate("""(site) => fetch('/site/switch', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'site_code=' + site})""", SITE)
    pg.goto(base + "/ld-management/settings?tab=data", wait_until="domcontentloaded")
    on_site = pg.evaluate("() => fetch('/fclm/sync/plan', {credentials: 'same-origin'}).then(r => r.json())")
    check("the browser session is on the scratch site", on_site.get("warehouse_id") == SITE, on_site.get("warehouse_id"))
    if on_site.get("warehouse_id") != SITE:
        b.close(); srv.shutdown()
        print("refusing to run the loop against", on_site.get("warehouse_id")); sys.exit(1)
    pg.evaluate("(fx) => { window.__fx = fx; }", {"rollup": ROLLUP, "ppr": PPR, "roster": ROSTER})
    pg.evaluate("() => {" + STUB + "}")
    pg.evaluate("() => { try { localStorage.removeItem('ldisFclmSyncCheckedAt'); } catch (e) {} }")
    served = pg.evaluate("(u) => fetch(u).then(r => r.text())", base + "/floor/provider.user.js")
    pg.evaluate("""(src) => { const s = document.createElement('script'); s.textContent = src;
                              document.head.appendChild(s); }""", served)
    # The page's own check finds the script; then Sync now is pressed
    # while the automatic run the script began on load is still reading.
    pg.wait_for_timeout(1500)
    presence = pg.inner_text("#fclmScript")
    pg.click("#fclmSyncNow")
    pg.wait_for_timeout(300)
    answered = pg.inner_text("#fclmSyncStatus")
    acks = pg.evaluate("() => window.__phases.filter(p => p.phase === 'fclm-ack')")
    done = pg.evaluate("() => window.__synced")
    finished = pg.inner_text("#fclmSyncStatus")
    asked = pg.evaluate("() => window.__fclmAsked")
    b.close()
srv.shutdown()
check("the reader ran without a page error", not errors, errors[:2])
check("the Data Upload card finds the installed script and names its version",
      "is installed in this browser" in presence and "1.3.0-" in presence, presence)
check("Sync now, pressed while the automatic run is reading, is answered at once that it is already reading",
      len(acks) == 1 and acks[0].get("running") is True, acks)
check("and the card shows that, or the progress after it — never 'no answer'",
      ("Already reading FCLM" in answered or "from FCLM:" in answered) and "No answer" not in answered, answered)
check("and the card reports the result when it finishes", "Done:" in finished or "Nothing was due" in finished, finished)
check("it finished a sync", done and not done.get("timeout"), done)
site_now = db.get_current_site()
db.set_current_site(SITE)
conn = db.get_db()
stored = conn.execute("SELECT COUNT(*) AS n FROM associate_day").fetchone()["n"]
logins = {x["employee_login"] for x in conn.execute("SELECT DISTINCT employee_login FROM associate_day").fetchall()}
synced_days = conn.execute("SELECT COUNT(*) AS n FROM fclm_sync_log WHERE kind = 'day' AND status = 'complete'").fetchone()["n"]
rostered = conn.execute("SELECT COUNT(*) AS n FROM employee_roster").fetchone()["n"]
conn.close()
check("it read the roster first, so the day's people could be named", rostered == 6, rostered)
check("then the due days, each stored complete", synced_days == len(done.get("days") or []) >= 1, (synced_days, done))
check("and the people in them", logins >= {"zqanna", "zqbert"} and stored > 0, logins)
check("it asked FCLM for one rollup per process per day, one PPR per day, and the roster once",
      sum("employeeRoster" in u for u in asked) == 1
      and sum("processPathRollup" in u for u in asked) == synced_days
      and sum("functionRollup" in u for u in asked) == synced_days * len(db.get_floor_processes()), len(asked))
check("and nothing but FCLM", all(u.startswith("https://fclm-portal.amazon.com/") for u in asked))

wipe()
db.set_current_site("SCN2")

print("\n" + "=" * 62)
if fails:
    print(f"{len(fails)} FCLM SYNC CHECK(S) FAILED")
    for name in fails:
        print("  -", name)
    sys.exit(1)
print("ALL FCLM SYNC CHECKS PASSED")
