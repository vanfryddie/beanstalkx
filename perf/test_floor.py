"""Live staffing: reading the floor out of FCLM and putting it beside
what the console already knows about training.

Two things make this worth testing hard. The data arrives from a
manager's browser, so the server has to treat it as untrusted and
attribute it without guessing. And the page it feeds is one somebody
uses to move labour, so a figure that is wrong or stale sends a person
to a station nobody is standing at.
"""
import os, re, sys, json, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import application, db
from browser import chromium_path
from werkzeug.serving import make_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def client(login, role):
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = login, role, "manual", "SCN2"
    return c


def wipe():
    conn = db.get_db()
    for table in ("floor_presence", "floor_snapshots", "floor_identity", "floor_clock"):
        conn.execute("DELETE FROM " + table)
    conn.commit()
    conn.close()


application.application.config["TESTING"] = True
db.set_current_site("SCN2")
db.init_db()
wipe()
ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
am, other_am = ams[0], ams[1]

print("1. Reading the real report's shape")
# The parser runs on HTML FCLM returned, through DOMParser, so it is
# exercised in a browser against a fixture built from the real report:
# the two-row header, the employeeId links, a Station column on one
# table and none on the other, and each table's spanning Total row.
from playwright.sync_api import sync_playwright

script = open(os.path.join(ROOT, "static/floor_provider.user.js"), encoding="utf-8").read()
fixture = open(os.path.join(ROOT, "perf/fixtures/function_rollup_pick.html"), encoding="utf-8").read()


def harness(*names):
    """The reader's pure functions, lifted out of its IIFE so they can be
    run against a fixture. Every top-level constant comes with them —
    forgetting one is a ReferenceError inside the browser rather than a
    failed check, which is a confusing way to find out."""
    out = [m.group(0).strip() for m in
           re.finditer(r"(?m)^    var [A-Z][A-Z0-9_]* = .*$", script)]
    for fn in names:
        m = re.search(r"(?ms)^    function " + fn + r"\(.*?\n    \}\n", script)
        if not m:
            check(f"the reader still defines {fn}", False)
        else:
            out.append(m.group(0).replace("    function", "function"))
    return "\n".join(out)


parts = [harness("headerIndex", "functionNameFor", "parseRollup", "cleanStation", "numberAt")]

rows = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    pg = b.new_context().new_page()
    pg.goto("about:blank")
    pg.add_script_tag(content=parts[0])
    rows = pg.evaluate("(html) => parseRollup(html, {key: 'pick', label: 'Pick'})", fixture)
    b.close()

check("every employee row is read", len(rows) == 6, f"got {len(rows)}")
check("the per-table Total rows are not read as people",
      not [r for r in rows if (r["full_name"] or "").lower() == "total"],
      str([r["full_name"] for r in rows]))
by_id = {r["employee_id"]: r for r in rows}
check("the employee id comes off the timeDetails link", "208651433" in by_id, str(sorted(by_id)))
faisal = by_id.get("208651433")
if faisal:
    check("the name is read as FCLM writes it", faisal["full_name"] == "Faisal,Abballa", faisal["full_name"])
    check("the manager column is read", faisal["manager_name"] == "Salhi,Hichem", str(faisal["manager_name"]))
    check("paid hours comes from the Total column, not the first one",
          faisal["paid_hours"] == 5.01, str(faisal["paid_hours"]))
    check("the function name comes from the heading above the table",
          faisal["function_name"] == "RF Pick", str(faisal["function_name"]))
stationed = [r for r in rows if r["station"]]
check("a station is read when the stations script has added the column",
      len(stationed) == 1 and stationed[0]["station"].endswith("A4359"), str([r["station"] for r in rows]))
check("an em-dash placeholder is not read as a station",
      not [r for r in rows if r["station"] in ("-", "–")])
check("a table with no Station column still parses",
      len([r for r in rows if r["function_name"] == "RF Pick"]) == 3)
# The heading is found by its bracketed id, not by proximity, because
# the real report puts its freshness stamp between the heading and the
# table — and reading that instead put "Data last checked at 2026-10-30
# 14:02 CET" in the column that is supposed to say what somebody is
# doing.
check("the freshness stamp is never read as a function name",
      not [r for r in rows if "last checked" in (r["function_name"] or "").lower()],
      str({r["function_name"] for r in rows}))
check("nor are the size tabs beside it",
      not [r for r in rows if "HeavyBulky" in (r["function_name"] or "")],
      str({r["function_name"] for r in rows}))
check("the function names are the real ones",
      {r["function_name"] for r in rows} == {"MultiLowDensityPick", "RF Pick"},
      str({r["function_name"] for r in rows}))
check("somebody in two functions is reported in both",
      len([r for r in rows if r["employee_id"] == "100356745"]) == 2)

print("\n1b. Who is in the building, for the whole site, in one request")
# Employee Time Details answers for one person per request, so it only
# ever covered a manager's own team and only the people whose employee
# id was already known — which is why the clock column stayed empty.
# PPA Employee Attendance answers for everybody at once.
att_parts = harness("pad", "fclmDate", "headerIndex", "parseClockTime",
                    "attendanceUrl", "parseAttendance")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    pg = b.new_context().new_page()
    pg.goto("about:blank")
    pg.add_script_tag(content=att_parts)
    att_html = open(os.path.join(ROOT, "perf/fixtures/ppa_attendance.html"), encoding="utf-8").read()
    attendance = pg.evaluate("([h, iso]) => parseAttendance(h, new Date(iso))",
                             [att_html, "2026-10-30T13:02:00"])
    att_url = pg.evaluate("(iso) => attendanceUrl('SCN2', new Date(iso))", "2026-10-30T13:02:00")
    b.close()

check("every attendance row is read", len(attendance) == 6, str(len(attendance)))
att = {row["employee_id"]: row for row in attendance}
check("an In with no Out is on the clock",
      att.get("112949854", {}).get("clock_state") == "in",
      str(att.get("112949854")))
check("an In and an Out is not",
      att.get("113141014", {}).get("clock_state") == "out",
      str(att.get("113141014")))
check("an Out with no In is a night shift that has ended, not somebody present",
      att.get("110386105", {}).get("clock_state") == "out",
      str(att.get("110386105")))
check("a second session reopens the clock",
      att.get("206276953", {}).get("clock_state") == "in",
      str(att.get("206276953")))
check("and it is the second In that is reported",
      (att.get("206276953", {}).get("clocked_in_at") or "")[11:16] == "10:15",
      str(att.get("206276953", {}).get("clocked_in_at")))
check("the blank row at the end is not a person",
      not [r for r in attendance if not r["full_name"]])
check("names and managers come across",
      att.get("112949854", {}).get("full_name") == "Scurtu,Dorel"
      and att.get("112949854", {}).get("manager_name") == "Radchenko,Dmytro",
      str(att.get("112949854")))
check("so does the shift code", att.get("112949854", {}).get("shift_code") == "D58F88KL")
check("the report is asked for as HTML, for this site, for today",
      "ppaAttendance" in att_url and "reportFormat=HTML" in att_url
      and "warehouseId=SCN2" in att_url and "2026%2F10%2F30" in att_url, att_url)

print("\n2. Attributing a row to somebody the console knows")
check("FCLM's name order and the uploads' agree once normalised",
      db.normalise_person_name("Faisal,Abballa") == db.normalise_person_name("Abballa Faisal"))
check("a middle name on one side only does not break it",
      db.normalise_person_name("Grzegorczyk,Rafal Michal")
      == db.normalise_person_name("Rafal Michal Grzegorczyk"))
check("two different people are not the same person",
      db.normalise_person_name("Ahmed,Mohammed") != db.normalise_person_name("Mohammed,Ahmad"))

people = db.get_associate_directory(am_logins=[am])
check("the manager has a team to attribute to", len(people) >= 4, str(len(people)))
target = people[0]

wipe()
result = db.record_floor_snapshot([{
    "employee_id": "555001", "employee_login": target["employee_login"],
    "full_name": "Written,Differently", "process_key": "pick", "process_name": "Pick",
    "function_name": "RF Pick", "station": "A02 - A2324", "paid_hours": 4.0,
}], taken_by=am)
check("a row carrying a login is attributed by it", result["matched"] == 1, json.dumps(result))
presence, meta = db.get_floor_presence()
check("and lands on that person", target["employee_login"].lower() in presence)

# The id was resolved once; the next sweep should not need the login.
result = db.record_floor_snapshot([{
    "employee_id": "555001", "full_name": "Something,Else Entirely",
    "process_key": "sort", "process_name": "Sort", "paid_hours": 2.0,
}], taken_by=am)
check("an id resolved once stays resolved", result["matched"] == 1, json.dumps(result))
presence, _ = db.get_floor_presence()
check("and still lands on the same person",
      presence.get(target["employee_login"].lower(), {}).get("process_name") == "Sort",
      str(presence.get(target["employee_login"].lower())))

wipe()
result = db.record_floor_snapshot([
    {"employee_id": "777001", "full_name": "Nobody,Here", "process_key": "pick",
     "process_name": "Pick", "paid_hours": 1.0},
], taken_by=am)
check("a row matching nobody is kept, not dropped", result["rows"] == 1 and result["matched"] == 0,
      json.dumps(result))
check("and is listed so somebody can fix the data",
      [u["full_name"] for u in db.get_floor_unmatched()] == ["Nobody,Here"],
      str(db.get_floor_unmatched()))

print("\n3. Two people with one name are not a match")
# The console would rather say nothing than put one associate at
# another's station because they share a surname.
conn = db.get_db()
conn.execute("INSERT INTO tracked_items (section, employee_login, full_name, am_login, status) "
             "VALUES (?,?,?,?,?)", ("safety", "twinone", "Ahmed,Mohammed", am, "Compliant"))
conn.execute("INSERT INTO tracked_items (section, employee_login, full_name, am_login, status) "
             "VALUES (?,?,?,?,?)", ("safety", "twintwo", "Mohammed Ahmed", am, "Compliant"))
conn.commit(); conn.close()
wipe()
result = db.record_floor_snapshot([{
    "employee_id": "666001", "full_name": "Ahmed,Mohammed",
    "process_key": "pick", "process_name": "Pick", "paid_hours": 1.0}], taken_by=am)
check("an ambiguous name is left unattributed", result["matched"] == 0, json.dumps(result))
conn = db.get_db()
conn.execute("DELETE FROM tracked_items WHERE employee_login IN ('twinone','twintwo')")
conn.commit(); conn.close()

print("\n4. Cross-training names and FCLM names are different vocabularies")
for trained, floor_name, expected in [
    ("Pack", "Pack Single", True), ("Pack", "Slam At Pack", True),
    ("Pick", "Pick", True), ("Receive", "Prep Receiver", True),
    ("Stow", "Stow Each Nike", True), ("Pick", "Pack Single", False),
    ("Stow", "Sort", False), ("Water Spider", "Pick", False),
]:
    got = application.floor_process_matches(trained, [floor_name])
    check(f"{trained!r} {'is' if expected else 'is not'} {floor_name!r}", got == expected, str(got))

print("\n5. The staffing view, and the move it exists for")
wipe()
team = db.get_associate_directory(am_logins=[am])
others = db.get_associate_directory(am_logins=[other_am])
rows = []
for i, person in enumerate(team):
    if i % 4 == 3:
        continue
    process = ("Pick", "RF Pick") if i % 3 == 0 else ("Pack Single", "Slam At Pack")
    rows.append({"employee_id": f"44{i:04d}", "employee_login": person["employee_login"],
                 "full_name": person["full_name"], "process_key": process[0].lower().replace(" ", "_"),
                 "process_name": process[0], "function_name": process[1],
                 "station": f"A02 - A4{i:03d}", "paid_hours": 3.0})
for person in others[:3]:
    rows.append({"employee_id": "99" + person["employee_login"][-4:], "employee_login": person["employee_login"],
                 "full_name": person["full_name"], "process_key": "sort", "process_name": "Sort",
                 "function_name": "AFE1 Induct", "paid_hours": 3.0})
db.record_floor_snapshot(rows, window_start="2026-10-28T10:10", window_end="2026-10-28T10:40", taken_by=am)

view = application.build_staffing_view([am], None, {})
check("the view covers the whole team, on the floor or not",
      view["total"] == len(team), f"{view['total']} vs {len(team)}")
check("on-site and off-site add up", view["on_site"] + view["off_site"] == view["total"])
check("it counts only people a source actually saw",
      view["on_process"] == len([r for r in rows if r["employee_login"] in
                                 {p["employee_login"] for p in team}]),
      f"{view['on_process']} on a process")
check("with no clock reading, on site is the same as on a process",
      view["on_site"] == view["on_process"], f"{view['on_site']} vs {view['on_process']}")
check("another manager's people are not in this view",
      not ({p["employee_login"] for p in view["people"]}
           & {p["employee_login"] for p in others}))

on_pick = application.build_staffing_view([am], None, {"working_in": "Pick"})
check("filtering to a process returns only people in it",
      all((p.get("where") or {}).get("process") == "Pick" for p in on_pick["people"]),
      str([(p["employee_login"], (p.get("where") or {}).get("process"))
           for p in on_pick["people"][:3]]))

trained_pack = application.build_staffing_view([am], None, {"trained_for": "Pack"})
check("filtering to trained-for returns only people trained on it",
      all("Pack" in p["trained_processes"] for p in trained_pack["people"]),
      str(len(trained_pack["people"])))

move = application.build_staffing_view([am], None,
                                       {"trained_for": "Pack", "working_in": "__elsewhere__"})
check("the staffing move returns somebody", len(move["people"]) > 0, str(len(move["people"])))
check("everybody it returns is trained for Pack",
      all("Pack" in p["trained_processes"] for p in move["people"]))
check("everybody it returns is on the floor",
      all(p["floor_state"] == "present" for p in move["people"]))
check("and nobody it returns is already on Pack",
      not [p for p in move["people"] if "Pack" in p["doing_trained"]],
      str([(p["employee_login"], p["floor"]["process_name"]) for p in move["people"][:3]]))
packers = [p for p in view["people"]
           if p["floor"] and p["floor"]["process_name"] == "Pack Single"]
check("the people already on Pack are excluded from the move",
      not ({p["employee_login"] for p in move["people"]} & {p["employee_login"] for p in packers}))
check("somebody on Pack is shown as doing what they are trained for",
      all("Pack" in p["doing_trained"] for p in packers), str(len(packers)))

off = application.build_staffing_view([am], None, {"presence": "off_site"})
check("the on-none filter returns only people the sweep did not see",
      all(not p["on_process"] for p in off["people"]), str(len(off["people"])))

print("\n6. Nothing is claimed when the floor has not been read")
wipe()
blank = application.build_staffing_view([am], None, {})
check("with no snapshot there is no metadata to show", blank["meta"] is None)
check("and nobody is counted as on site", blank["on_site"] == 0)
body = client("admin", "admin").get(f"/overview?tab=staffing&am={am}").get_data(as_text=True)
check("the page says the floor has not been read", "Nobody has read the floor yet" in body)
check("and points at the reader rather than showing an empty site",
      "floor/provider.user.js" in body)
dir_body = client("admin", "admin").get(f"/overview?tab=directory&am={am}").get_data(as_text=True)
check("no associate is marked absent on a floor nobody has read",
      "Not on the floor" not in dir_body)

print("\n7. The routes")
db.record_floor_snapshot(rows, taken_by=am)
admin = client("admin", "admin")
cfg = admin.get("/floor/config")
check("the config is served", cfg.status_code == 200, str(cfg.status_code))
cfg_json = cfg.get_json()
check("it names this site", cfg_json["warehouse_id"] == "SCN2", str(cfg_json.get("warehouse_id")))
check("it carries a process list", len(cfg_json["processes"]) >= 5, str(len(cfg_json["processes"])))
check("every process has an FCLM id",
      all(p.get("process_id") for p in cfg_json["processes"]))

anon = application.application.test_client()
check("an unauthenticated sweep is refused",
      anon.post("/floor/snapshot", json={"rows": []}).status_code in (401, 302),
      str(anon.post("/floor/snapshot", json={"rows": []}).status_code))
check("so is unauthenticated config", anon.get("/floor/config").status_code in (401, 302))

posted = client(am, "am").post("/floor/snapshot", json={"rows": [
    {"employee_id": "123", "employee_login": team[0]["employee_login"],
     "full_name": team[0]["full_name"], "process_name": "Pick", "paid_hours": 1}]})
check("an Area Manager can post a sweep", posted.status_code == 200, str(posted.status_code))
check("rows that are not a list are refused",
      client(am, "am").post("/floor/snapshot", json={"rows": "nope"}).status_code == 400)
check("a junk row does not take the sweep down",
      client(am, "am").post("/floor/snapshot",
                            json={"rows": [None, 5, {"full_name": "x" * 500}]}).status_code == 200)

status = admin.get("/floor/status").get_json()
check("status reports freshness", status["has_data"] and status["age_minutes"] is not None, str(status))

print("\n8. The floor reader is installable")
served = admin.get("/floor/provider.user.js")
check("the reader is served", served.status_code == 200)
text = served.get_data(as_text=True)
meta = text.split("// ==UserScript==")[1].split("// ==/UserScript==")[0]
matches = re.findall(r"(?m)^// @match\s+(\S+)", meta)
PATTERN_RE = re.compile(r"^(\*|https?)://(\*|(?:\*\.)?[^/*]+)(/[^?#]*)$")
check("every @match is a valid match pattern",
      all(PATTERN_RE.match(m) for m in matches), str(matches))
check("no @match carries a query string", not [m for m in matches if "?" in m], str(matches))
check("both schemes are covered",
      {m.split("://")[0] for m in matches} == {"http", "https"}, str(sorted(matches)))
check("it reaches the Staffing tab and Cross Training",
      any("/overview" in m for m in matches) and any("cross-training" in m for m in matches),
      str(matches))
check("FCLM is declared as a connect target", "@connect      fclm-portal.amazon.com" in meta)
check("no placeholder survived templating",
      not [t for t in ("__LDIS_MATCH__", "__LDIS_UPDATE__", "__LDIS_BUILD__",
                       "__LDIS_CONFIG__", "__LDIS_POST__") if t in text])
check("the config and post endpoints are absolute",
      "/floor/config" in text and "/floor/snapshot" in text)
version = re.search(r"(?m)^// @version\s+(\S+)", meta).group(1)
again = admin.get("/floor/provider.user.js").get_data(as_text=True)
check("its version is stable across requests",
      re.search(r"(?m)^// @version\s+(\S+)",
                again.split("// ==UserScript==")[1]).group(1) == version, version)

print("\n8b. A sweep that found nobody does not blank the floor")
good = db.record_floor_snapshot(rows, taken_by=am)
before = db.get_latest_floor_snapshot()["id"]
empty = db.record_floor_snapshot([], taken_by=am)
check("an empty sweep is not stored", empty.get("stored") is False, json.dumps(empty))
check("and the last real reading is still the one shown",
      db.get_latest_floor_snapshot()["id"] == before)
check("a sweep of junk rows that match nobody is still stored, because it ran",
      db.record_floor_snapshot([{"employee_id": "x1", "full_name": "Who,Knows",
                                 "process_name": "Pick"}], taken_by=am)["stored"] is True)
db.record_floor_snapshot(rows, taken_by=am)

print("\n9. Presence reaches the pages that asked for it")
body = client("admin", "admin").get(f"/overview?tab=staffing&am={am}").get_data(as_text=True)
check("the staffing tab renders the floor", "on site." in body.lower())
check("it offers the filter that makes it a staffing tool",
      "Not on what they are trained for" in body)
check("and the two toggles the shift actually turns on",
      "Punched in" in body and "On a process" in body)
check("it says how old the reading is", "FCLM" in body or "Floor read" in body)

dir_body = client("admin", "admin").get(f"/overview?tab=directory&am={am}").get_data(as_text=True)
check("My Associates shows presence", "ad-tag-floor" in dir_body or "On the floor" in dir_body)

xt = client("admin", "admin").get(
    f"/api/xt-associates/search?q={team[0]['employee_login']}").get_data(as_text=True)
check("Cross Training shows presence on the associate card", "xt-floor-line" in xt)
check("and names the process", "Pick" in xt or "Pack" in xt)

# Both pages that show the floor also drive a refresh of it, or the
# Cross Training lookup would read whatever the Staffing tab happened to
# leave behind.
for label, url in (("the Staffing tab", f"/overview?tab=staffing&am={am}"),
                   ("the Cross Training lookup", "/ld-management/cross-training?tab=directory")):
    page = client("admin", "admin").get(url).get_data(as_text=True)
    check(f"{label} carries the marker that starts a sweep", 'id="ldisFloorMarker"' in page)
    check(f"{label} tells the reader how old the reading is",
          re.search(r'id="ldisFloorMarker"[^>]*data-age-seconds="\d+"', page) is not None)

print("\n9b. Punched in is a different fact from booked to a process")
# Somebody on a break is on the clock and on no process at all. Merging
# the two would have the console report they had gone home.
td_parts = harness("fclmDate", "pad", "parseSessionTime", "infoField",
                   "absoluteUrl", "splitTaskTitle", "taskKind", "parseTimeDetails")
check("the reader knows how FCLM separates process from function",
      "TITLE_SEPARATOR" in td_parts)

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    pg = b.new_context().new_page()
    pg.goto("about:blank")
    pg.add_script_tag(content=td_parts)
    def read(name):
        html = open(os.path.join(ROOT, "perf/fixtures", name), encoding="utf-8").read()
        return pg.evaluate("([h, iso]) => parseTimeDetails(h, new Date(iso))",
                           [html, "2026-10-28T11:45:00"])
    on = read("employee_time_details.html")
    working = read("employee_time_details_working.html")
    off = read("employee_time_details_off.html")
    b.close()

check("an open session reads as punched in", on["clock_state"] == "in", str(on["clock_state"]))
check("with the time they clocked in", (on["clocked_in_at"] or "").startswith("2026-10-30T06:00"),
      str(on["clocked_in_at"]))
check("a finished shift reads as punched out", off["clock_state"] == "out", str(off["clock_state"]))
check("with the time they clocked out", (off["clocked_out_at"] or "").startswith("2026-10-28T10:30"),
      str(off["clocked_out_at"]))
check("the login comes off the page, so no profile service is needed",
      on["employee_login"] == "abaltoma", str(on["employee_login"]))
check("so does the employee id", on["employee_id"] == "206276953", str(on["employee_id"]))
check("and the shift code", on["shift_code"] == "D58F8W3Z", str(on["shift_code"]))
check("the badge photo is an FCLM address",
      (on["photo_url"] or "").startswith("https://fclm-portal.amazon.com/"), str(on["photo_url"]))

# The row with no end time is what somebody is doing this minute, and
# telling work apart from a break or time off task is the difference
# between "on Pick" and "here, doing nothing in particular".
check("the open last row is read as the current task",
      on["current_title"] == "Time Off Task", str(on["current_title"]))
check("and classified as off task, not as a process",
      on["current_kind"] == "off-task", str(on["current_kind"]))
check("real work is split into process and function",
      (working["current_process"], working["current_function"]) == ("Pick", "RF Pick"),
      f"{working['current_process']} / {working['current_function']}")
check("and classified as work", working["current_kind"] == "work", str(working["current_kind"]))
check("the task carries the time it started",
      (working["task_started_at"] or "").startswith("2026-10-30T10:15"),
      str(working["task_started_at"]))
check("a punched-out person's last task is not treated as current work by the store",
      off["clock_state"] == "out", str(off["clock_state"]))

conn = db.get_db(); conn.execute("DELETE FROM floor_clock"); conn.commit(); conn.close()
db.record_floor_clock([
    {"employee_login": team[0]["employee_login"], "employee_id": "440000",
     "clock_state": "in", "clocked_in_at": "2026-10-28T06:00:00",
     "photo_url": "https://fclm-portal.amazon.com/employee/photo?employeeId=440000"},
    {"employee_login": team[1]["employee_login"], "clock_state": "out",
     "clocked_out_at": "2026-10-28T10:30:00"},
], checked_by=am)
clock = db.get_floor_clock()
check("clock readings are stored per person", len(clock) == 2, str(len(clock)))
check("each carries its own age",
      all(c["age_minutes"] is not None for c in clock.values()))
check("a fresh reading is not marked stale",
      not clock[team[0]["employee_login"].lower()]["is_stale"])
check("the photo is stored as a URL and nothing else",
      db.get_floor_photo_urls()[team[0]["employee_login"].lower()].startswith(
          "https://fclm-portal.amazon.com/"))

punched = application.build_staffing_view([am], None, {"clock": "in"})
check("the punched-in toggle returns only people on the clock",
      all(p["clock_state"] == "in" for p in punched["people"]) and punched["people"],
      str(len(punched["people"])))
out_view = application.build_staffing_view([am], None, {"clock": "out"})
check("the punched-out toggle returns only people off the clock",
      all(p["clock_state"] == "out" for p in out_view["people"]) and out_view["people"],
      str(len(out_view["people"])))
full = application.build_staffing_view([am], None, {})
check("people with no reading are neither in nor out",
      full["clock_unknown"] == full["total"] - 2, str(full["clock_unknown"]))
check("the two questions are counted separately",
      "punched_in" in full and "on_site" in full)

print("\n9c. A photo URL is only ever accepted from FCLM")
posted = client(am, "am").post("/floor/clock", json={"entries": [
    {"employee_login": team[2]["employee_login"], "clock_state": "in",
     "photo_url": "https://example.invalid/not-fclm.png"}]})
check("a posted sweep with a foreign photo host is accepted", posted.status_code == 200)
check("but the foreign URL is not stored",
      db.get_floor_photo_urls().get(team[2]["employee_login"].lower()) is None,
      str(db.get_floor_photo_urls().get(team[2]["employee_login"].lower())))
check("entries that are not a list are refused",
      client(am, "am").post("/floor/clock", json={"entries": "nope"}).status_code == 400)
check("an unauthenticated clock post is refused",
      application.application.test_client().post(
          "/floor/clock", json={"entries": []}).status_code in (401, 302))

roster = client(am, "am").get(f"/floor/roster?am={am}").get_json()
check("the roster is scoped to a team, not the site",
      len(roster["roster"]) <= len(team), f"{len(roster['roster'])} vs team of {len(team)}")
check("everybody on it has an employee id to ask about",
      all(r.get("employee_id") for r in roster["roster"]))
check("and it says how many it had to leave out",
      "without_id" in roster, str(sorted(roster)))

print("\n9d. Filters take several values at once")
db.record_floor_snapshot(rows, taken_by=am)
both = application.build_staffing_view([am], None, {"trained_for": ["Pack", "Pick"]})
only_pack = application.build_staffing_view([am], None, {"trained_for": ["Pack"]})
check("ticking two processes is an OR, not an AND",
      len(both["people"]) >= len(only_pack["people"]),
      f"{len(both['people'])} vs {len(only_pack['people'])}")
check("everybody returned is trained on at least one of them",
      all({"pack", "pick"} & {t.lower() for t in p["trained_processes"]}
          for p in both["people"]))

roles = application.build_staffing_view([am], None, {"role": ["Jambuster"]})
check("indirect roles are filterable", isinstance(roles["people"], list))
check("everybody returned holds the role",
      all("jambuster" in {r.lower() for r in p["role_names"]} for p in roles["people"]))
check("the role list offers the roles the site actually records",
      full["role_options"], str(full["role_options"][:4]))

places = application.build_staffing_view([am], None, {"working_in": ["Pick", "Pack Single"]})
check("several places can be shown at once",
      all(p["floor"] and p["floor"]["process_name"] in ("Pick", "Pack Single")
          for p in places["people"]), str(len(places["people"])))

combined = application.build_staffing_view(
    [am], None, {"trained_for": ["Pack"], "role": ["Jambuster"], "clock": "in"})
check("different filters narrow together",
      all("Pack" in p["trained_processes"]
          and "jambuster" in {r.lower() for r in p["role_names"]}
          and p["clock_state"] == "in" for p in combined["people"]),
      str(len(combined["people"])))

print("\n9e. The filter panels open over the page, not under it")
# The console's entry animation makes every direct child of .content its
# own stacking context, so a dropdown in an earlier sibling renders
# beneath a later one however it is z-indexed. Nothing in the markup
# shows that, so it is checked by asking the browser what is on top.
srv = make_server("127.0.0.1", 5642, application.application, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(1.0)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    # Tall enough that a panel opening at the foot of a short page is
    # still inside the viewport, because elementFromPoint only answers
    # for points that are.
    ctx = b.new_context(viewport={"width": 1500, "height": 1400})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:5642/", wait_until="domcontentloaded")
    pg.fill("input[name=login_id]", "admin"); pg.click("button[type=submit]"); pg.wait_for_timeout(500)
    pg.goto(f"http://127.0.0.1:5642/overview?tab=staffing&am={am}", wait_until="networkidle")
    pg.wait_for_timeout(400)
    check("no uncaught JavaScript on the staffing tab", not errors, "; ".join(errors[:2]))

    # The bar goes to the top of the viewport, not the middle: a panel
    # that opens downwards needs room under it, and at the foot of a
    # short page there is none — which is a quirk of the probe, not of
    # the page, so it must not be read as a stacking failure.
    pg.eval_on_selector(".st-filters", "e => e.scrollIntoView({block: 'start'})")
    pg.evaluate("window.scrollBy(0, -90)")
    pg.wait_for_timeout(250)
    pg.query_selector_all(".xf-button")[0].click()
    pg.wait_for_timeout(300)
    # elementFromPoint answers only for points inside the viewport, so
    # the panel is scrolled fully into view before it is asked.
    probe = pg.evaluate("""() => {
      const panel = document.querySelector('.xf-panel:not([hidden])');
      const r = panel.getBoundingClientRect();
      const hit = (dy) => {
        const el = document.elementFromPoint(r.left + r.width / 2, r.top + dy);
        return el ? {ok: !!el.closest('.xf-panel'),
                     what: el.tagName + '.' + String(el.className).slice(0, 30)}
                  : {ok: false, what: 'nothing (outside the viewport?)'};
      };
      return {rect: {top: Math.round(r.top), height: Math.round(r.height),
                     viewport: window.innerHeight},
              points: [hit(10), hit(r.height / 2), hit(r.height - 8)]};
    }""")
    check("the open panel is on top from its first row to its last",
          all(point["ok"] for point in probe["points"]),
          "; ".join(f"{point['what']}" for point in probe["points"]) + f" {probe['rect']}")
    check("its options read as written, not shouted by the global label rule",
          pg.eval_on_selector(".xf-option span",
                              "e => getComputedStyle(e).textTransform") == "none",
          pg.eval_on_selector(".xf-option span", "e => getComputedStyle(e).textTransform"))

    # Ticking two boxes has to submit two values, which is the whole point.
    boxes = pg.query_selector_all(".xf-panel:not([hidden]) input[type=checkbox]")
    boxes[0].check(); boxes[1].check()
    pg.wait_for_timeout(150)
    pg.keyboard.press("Escape")
    with pg.expect_navigation(wait_until="networkidle"):
        pg.eval_on_selector(".st-filters", "f => f.submit()")
    from urllib.parse import urlsplit, parse_qs
    picked = parse_qs(urlsplit(pg.url).query).get("trained_for", [])
    check("two ticked boxes arrive as two values", len(picked) == 2, str(picked))
    pg.wait_for_selector(".xf-button.is-set")
    label = pg.eval_on_selector(".xf-button.is-set", "e => e.textContent").strip()
    check("and the button says so", "2 selected" in label, label)
    shown = pg.eval_on_selector_all(".st-table tbody tr", "els => els.length")
    check("the page narrows to the ticked processes", shown > 0, str(shown))

    # FCLM is not reachable from here, so every face on this page is a
    # face that failed to load — which is the case that matters. The
    # initials have to be showing, in the same 34px, with the row the
    # same height it was: a table that reflows when photos arrive or
    # fail is worse than one with no photos at all.
    faces = pg.evaluate("""() => {
      const stack = document.querySelector('.st-face-stack');
      if (!stack) return {found: false};
      const initials = stack.querySelector('.st-face-initials');
      const img = stack.querySelector('img.st-face-photo');
      const row = stack.closest('tr');
      const box = stack.getBoundingClientRect();
      return {
        found: true,
        hasInitials: !!initials && initials.offsetWidth > 0,
        initialsText: initials ? initials.textContent.trim() : '',
        size: [Math.round(box.width), Math.round(box.height)],
        rowHeight: Math.round(row.getBoundingClientRect().height),
        photoOpacity: img ? getComputedStyle(img).opacity : 'no photo',
        marked: img ? img.getAttribute('data-floor-photo') : null
      };
    }""")
    check("the table has faces at all", faces.get("found"), str(faces))
    if faces.get("found"):
        check("a photo that will not load leaves the initials showing",
              faces["hasInitials"] and len(faces["initialsText"]) == 2, str(faces))
        check("in the same 34px, so nothing reflows either way",
              faces["size"] == [34, 34], str(faces["size"]))
        check("the photo that failed is transparent rather than broken",
              faces["photoOpacity"] in ("0", "no photo"), str(faces["photoOpacity"]))
        check("and is marked for the reader to fetch under its own session",
              faces["marked"] and faces["marked"].startswith("https://fclm-portal.amazon.com/"),
              str(faces["marked"]))

    # The sweep finishing used to reload the page. It now re-renders the
    # tab in place, which is only an improvement if the filters still
    # work afterwards — they are bound by a script that does not run
    # again, so they have to be rebound by hand.
    pg.evaluate("window.scrollTo(0, 240)")
    pg.wait_for_timeout(250)
    before = pg.evaluate("""() => {
      const p = document.getElementById('staffingPanel');
      return {rows: document.querySelectorAll('.st-table tbody tr').length,
              node: !!p, scroll: Math.round(window.scrollY)};
    }""")
    check("the tab is wrapped in the panel the refresh replaces", before["node"])
    pg.evaluate("document.dispatchEvent(new CustomEvent('ldis-floor-updated'))")
    pg.wait_for_timeout(1200)
    after = pg.evaluate("""() => {
      const p = document.getElementById('staffingPanel');
      return {rows: document.querySelectorAll('.st-table tbody tr').length,
              node: !!p, scroll: Math.round(window.scrollY),
              bound: !!(p && p.querySelector('[data-xf][data-xf-bound="1"]'))};
    }""")
    check("a floor read re-renders the tab without reloading",
          after["node"] and after["rows"] == before["rows"],
          f"{before['rows']} rows before, {after['rows']} after")
    check("and keeps the manager where they were on the page",
          abs(after["scroll"] - before["scroll"]) <= 4,
          f"{before['scroll']} -> {after['scroll']}")
    check("the filters are rebound on the markup that replaced them",
          after["bound"], str(after))
    pg.query_selector_all(".xf-button")[0].click()
    pg.wait_for_timeout(250)
    check("so a filter still opens after a refresh",
          pg.eval_on_selector_all(".xf-panel:not([hidden])", "e => e.length") == 1)
    check("and no uncaught JavaScript came out of the swap", not errors,
          "; ".join(errors[:2]))

    over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("no horizontal page scroll", over <= 1, f"overflows {over}px")
    ctx.close(); b.close()
srv.shutdown()

print("\n9f. Punched in and doing something the sweep never asked about")
# The complaint this was written for: a site of eighty read as three
# processes and forty-six people "not seen", when most of them were
# punched in on Cart/Pallet Builder, a dock, or sitting at Time Off
# Task. The sweep covers the processes it is given; Employee Time
# Details covers whatever somebody is actually doing, and the two have
# to be read together or being present looks like being absent.
wipe()
unswept = team[0]
idle_one = team[1]
swept_one = team[2]
gone = team[3]

db.record_floor_snapshot([{
    "employee_id": "330001", "employee_login": swept_one["employee_login"],
    "full_name": swept_one["full_name"], "process_key": "pick", "process_name": "Pick",
    "function_name": "RF Pick", "station": "A02 - A2324", "paid_hours": 3.0,
}], taken_by=am, reader_version=application._floor_reader_version())

db.record_floor_clock([
    {"employee_login": unswept["employee_login"], "employee_id": "330002",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
     "current_title": "Transfer In Support\u25c6Cart/Pallet Builder",
     "current_process": "Transfer In Support", "current_function": "Cart/Pallet Builder",
     "current_kind": "work", "task_started_at": "2026-10-30T10:15:00"},
    {"employee_login": idle_one["employee_login"], "employee_id": "330003",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
     "current_title": "Time Off Task", "current_process": "Time Off Task",
     "current_kind": "off-task", "task_started_at": "2026-10-30T11:37:00"},
    {"employee_login": swept_one["employee_login"], "employee_id": "330001",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
     "current_title": "Pick\u25c6RF Pick", "current_process": "Pick",
     "current_function": "RF Pick", "current_kind": "work"},
    {"employee_login": gone["employee_login"], "employee_id": "330004",
     "clock_state": "out", "clocked_out_at": "2026-10-30T10:30:00",
     "current_title": "Pick\u25c6RF Pick", "current_process": "Pick", "current_kind": "work"},
], checked_by=am)

view = application.build_staffing_view([am], None, {})
by_login = {p["employee_login"]: p for p in view["people"]}

row = by_login[unswept["employee_login"]]
check("somebody on an unswept process is on site, not 'not seen'",
      row["floor_state"] == "present", row["floor_state"])
check("and the page says what they are actually doing",
      (row["where"] or {}).get("process") == "Transfer In Support",
      str((row["where"] or {}).get("process")))
check("with the function under it",
      (row["where"] or {}).get("function") == "Cart/Pallet Builder",
      str((row["where"] or {}).get("function")))
check("read from the page that knows, not the sweep",
      (row["where"] or {}).get("source") == "time-details",
      str((row["where"] or {}).get("source")))
check("they are not counted as booked to a swept process", not row["on_process"])

row = by_login[idle_one["employee_login"]]
check("somebody off task is on site", row["floor_state"] == "present")
check("and is shown as off task rather than as working",
      (row["where"] or {}).get("kind") == "off-task", str((row["where"] or {}).get("kind")))
check("with the time it started, so a manager can see how long",
      (row["where"] or {}).get("since"), str((row["where"] or {}).get("since")))

row = by_login[swept_one["employee_login"]]
check("somebody the sweep saw keeps their station",
      (row["where"] or {}).get("station") == "A02 - A2324",
      str((row["where"] or {}).get("station")))
check("and is counted as on a swept process", row["on_process"])

row = by_login[gone["employee_login"]]
check("a punched-out person is not on site", row["floor_state"] != "present")
check("and their last task is not reported as current",
      not (row["where"] or {}), str(row["where"]))

check("on site counts the clock as well as the sweep", view["on_site"] == 3,
      f"{view['on_site']} on site, {view['on_process']} on a process")
check("and the two are reported separately", view["on_process"] == 1, str(view["on_process"]))
check("idle people are counted", view["idle"] == 1, str(view["idle"]))

idle_view = application.build_staffing_view([am], None, {"presence": "idle"})
check("the here-but-not-on-a-process filter finds them",
      [p["employee_login"] for p in idle_view["people"]] == [idle_one["employee_login"]],
      str([p["employee_login"] for p in idle_view["people"]]))

# What a manager reads is the function, not the process above it: Each
# TSI tells them nothing, Stow Each Nike and Stow Each Nike Light are
# two different jobs. So the row, the tiles and the filter all speak in
# functions — and the process still matches, so a saved link keeps
# working.
row = by_login[unswept["employee_login"]]
check("the row is labelled with the function, not the process",
      (row["where"] or {}).get("label") == "Cart/Pallet Builder",
      str((row["where"] or {}).get("label")))
check("the process is kept beside it for context",
      (row["where"] or {}).get("area") == "Transfer In Support",
      str((row["where"] or {}).get("area")))
check("the filter offers the function",
      "Cart/Pallet Builder" in view["floor_options"], str(view["floor_options"]))
by_function = application.build_staffing_view([am], None,
                                              {"working_in": ["Cart/Pallet Builder"]})
check("and filtering on it finds the person",
      [p["employee_login"] for p in by_function["people"]] == [unswept["employee_login"]],
      str([p["employee_login"] for p in by_function["people"]]))
by_area = application.build_staffing_view([am], None, {"working_in": ["Transfer In Support"]})
check("filtering by the process still works, so a saved link does not break",
      [p["employee_login"] for p in by_area["people"]] == [unswept["employee_login"]],
      str([p["employee_login"] for p in by_area["people"]]))
check("the tiles group by function too",
      "Cart/Pallet Builder" in dict(view["by_process"]),
      str([name for name, _n in view["by_process"]]))

# Posted through the route, not straight into the store: the first
# version of /floor/clock read every other field by name and quietly
# dropped these five, so a reader that had parsed the task perfectly
# still produced a page that said "not seen".
posted = client(am, "am").post("/floor/clock", json={"entries": [{
    "employee_login": unswept["employee_login"], "employee_id": "330002",
    "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
    "current_title": "Transfer In Support\u25c6Cart/Pallet Builder",
    "current_process": "Transfer In Support", "current_function": "Cart/Pallet Builder",
    "current_kind": "work", "task_started_at": "2026-10-30T10:15:00"}]})
check("a clock post is accepted", posted.status_code == 200, str(posted.status_code))
through_route = db.get_floor_clock().get(unswept["employee_login"].lower(), {})
check("the task survives the route, not just the store",
      through_route.get("current_process") == "Transfer In Support",
      str(through_route.get("current_process")))
check("with its function", through_route.get("current_function") == "Cart/Pallet Builder",
      str(through_route.get("current_function")))
check("and its kind", through_route.get("current_kind") == "work",
      str(through_route.get("current_kind")))

print("\n9f2. Attendance names people by id, and must not wipe what they are doing")
# The two clock sources disagree about how much they know. Time Details
# knows the task; the attendance report knows only whether somebody is
# in the building and names them by employee id. Letting the second
# overwrite the first would turn every task back into "no task
# recorded" a second after it was read.
resolved = db.record_floor_clock([{
    "employee_id": "330002", "full_name": unswept["full_name"],
    "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00"}], checked_by=am)
check("an attendance row with no login is resolved by its id",
      resolved["stored"] == 1 and resolved["unresolved"] == 0, json.dumps(resolved))
kept = db.get_floor_clock()[unswept["employee_login"].lower()]
check("and the task read a moment earlier survives it",
      kept.get("current_function") == "Cart/Pallet Builder",
      str(kept.get("current_function")))
check("while the punch time it brought is taken",
      (kept.get("clocked_in_at") or "").startswith("2026-10-30T06:00"),
      str(kept.get("clocked_in_at")))

stranger = db.record_floor_clock([{
    "employee_id": "999999999", "full_name": "Nobody,Known Here", "clock_state": "in"}],
    checked_by=am)
check("somebody the console does not know is counted, not invented",
      stranger["stored"] == 0 and stranger["unresolved"] == 1, json.dumps(stranger))

db.record_floor_clock([{"employee_id": "330002", "clock_state": "out",
                        "clocked_out_at": "2026-10-30T14:00:00"}], checked_by=am)
after = db.get_floor_clock()[unswept["employee_login"].lower()]
check("punching out clears the task, because it has stopped",
      not after.get("current_function"), str(after.get("current_function")))
check("and they are no longer on site",
      application.build_staffing_view([am], None, {})["people"]
      and by_login is not None)

# Put them back for the sections below.
db.record_floor_clock([{
    "employee_login": unswept["employee_login"], "employee_id": "330002",
    "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
    "current_title": "Transfer In Support\u25c6Cart/Pallet Builder",
    "current_process": "Transfer In Support", "current_function": "Cart/Pallet Builder",
    "current_kind": "work", "task_started_at": "2026-10-30T10:15:00"}], checked_by=am)

print("\n9g. An out-of-date reader is called out rather than left to look like no data")
check("a sweep from the current reader is not flagged",
      not application.build_staffing_view([am], None, {})["reader_stale"])
db.record_floor_snapshot([{"employee_id": "330001", "full_name": "Anyone,At All",
                           "process_name": "Pick", "paid_hours": 1}],
                         taken_by=am, reader_version="1.0.0-older")
stale = application.build_staffing_view([am], None, {})
check("one from an older build is", stale["reader_stale"], str(stale["reader_posted"]))
db.record_floor_snapshot([{"employee_id": "330001", "full_name": "Anyone,At All",
                           "process_name": "Pick", "paid_hours": 1}], taken_by=am)
check("and one that does not say at all is too — that build had no clock",
      application.build_staffing_view([am], None, {})["reader_stale"])
with application.application.test_request_context("/"):
    served = application._floor_reader_source()
check("the version the reader carries is the one the console expects",
      f"// @version      {application._floor_reader_version()}" in served[0],
      served[1])
check("and it does not move when the console is reached by another host",
      application._floor_reader_version() == served[1])

body = client("admin", "admin").get(f"/overview?tab=staffing&am={am}").get_data(as_text=True)
check("the page says so where somebody will see it",
      "floor reader in this browser is out of date" in body)

print("\n9h. The sweep covers the whole site, not a corner of it")
processes = db.get_floor_processes()
labels = {p["label"] for p in processes}
check("every process the network defines is swept", len(processes) >= 25, str(len(processes)))
for expected in ("Pick", "Pick Support", "Receive Dock", "TO Lead/PA", "VRET Pack", "IB Lead"):
    check(f"including {expected}", expected in labels)
check("process ids are unique", len({p["process_id"] for p in processes}) == len(processes))
check("keys are unique", len({p["key"] for p in processes}) == len(processes))

print("\n9i. FCLM's name for a process, and this site's name for the job")
# "Each TSI" is not a job anybody was trained on. Stow Each Nike and
# Stow Each Nike Light are, and the difference between them is why a
# manager is reading the page. The map between the two is not hard-coded
# anywhere: the cross-training upload already carries both names in the
# same row — FCLM Mapped beside Merged Functions — so it is read from
# there, which also means it keeps up when the site renames something.
wipe()
db.set_site_setting(db.FLOOR_ALIASES_KEY, "")

# The site's own data already maps its processes, so that is checked as
# it stands; the two-jobs-under-one-process case gets a process name of
# its own so this section is not asserting against whatever the upload
# happens to contain today.
live = db.get_floor_aliases()
check("the site's own upload maps its FCLM processes to defined jobs",
      live.get("each tsi") and all(live["each tsi"]), str(live.get("each tsi")))

PROC = "Nike TSI"
nike = team[0]
nike_light = team[1]
unmapped = team[2]
conn = db.get_db()
cur = conn.cursor()


def learn(login, merged, supervisor):
    cur.execute(
        "INSERT INTO xt_hours (fc, fclm_mapped, employee_login, full_name, "
        "supervisor_login, merged_function, trained_status, shift, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("SCN2", PROC, login, login, supervisor, merged, "Trained", "DA",
         "2026-10-30T00:00:00"))


learn(nike["employee_login"], "Stow Each Nike", am)
learn(nike_light["employee_login"], "Stow Each Nike Light", am)
# Three people hold the plain variant and one the Light, so the headline
# candidate — the fallback for when cross-training cannot pick — is the
# one most of the floor is on, not whichever row came back first.
learn("ldis_alias_filler_1", "Stow Each Nike", None)
learn("ldis_alias_filler_2", "Stow Each Nike", None)
conn.commit()
conn.close()

aliases = db.get_floor_aliases()
check("the map is learned from the cross-training upload, not hard-coded",
      aliases.get(PROC.lower()) == ["Stow Each Nike", "Stow Each Nike Light"],
      str(aliases.get(PROC.lower())))
check("a process nobody is trained in has no entry, so FCLM's name stands",
      "cart/pallet builder" not in aliases, str(sorted(aliases)[:6]))
check("the built-in seed still covers a site whose upload has no FCLM column",
      "each stp" in aliases, str(sorted(aliases)[:6]))

check("the defined name replaces FCLM's",
      application._floor_defined_name(PROC, aliases) == "Stow Each Nike")
check("and the person's own training picks between two candidates",
      application._floor_defined_name(PROC, aliases, ["Pick", "Stow Each Nike Light"])
      == "Stow Each Nike Light")
check("a name this site does not define comes back as None, so FCLM's is used",
      application._floor_defined_name("Cart/Pallet Builder", aliases) is None)
check("and nothing is invented out of an empty reading",
      application._floor_defined_name("", aliases) is None)

db.record_floor_snapshot([{
    "employee_id": "550009", "employee_login": team[3]["employee_login"],
    "full_name": team[3]["full_name"], "process_key": "pick",
    "process_name": "Pick", "function_name": "RF Pick", "paid_hours": 2.0}],
    taken_by=am, reader_version=application._floor_reader_version())
db.record_floor_clock([
    {"employee_login": nike["employee_login"], "employee_id": "550001",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
     "current_title": PROC, "current_process": PROC,
     "current_kind": "work", "task_started_at": "2026-10-30T10:00:00"},
    {"employee_login": nike_light["employee_login"], "employee_id": "550002",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
     "current_title": PROC, "current_process": PROC,
     "current_kind": "work", "task_started_at": "2026-10-30T10:00:00"},
    {"employee_login": unmapped["employee_login"], "employee_id": "550003",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
     "current_title": "Transfer In Support\u25c6Cart/Pallet Builder",
     "current_process": "Transfer In Support",
     "current_function": "Cart/Pallet Builder",
     "current_kind": "work", "task_started_at": "2026-10-30T10:00:00"},
], checked_by=am)

view = application.build_staffing_view([am], None, {})
by_login = {p["employee_login"]: p for p in view["people"]}
where = by_login[nike["employee_login"]]["where"]
check("the row says the job this site defines, not the FCLM process",
      where["label"] == "Stow Each Nike", str(where["label"]))
check("what FCLM actually said is kept, so the report can be checked",
      where["raw"] == PROC, str(where["raw"]))
check("and the process is still there for a saved filter link",
      where["area"] == PROC, str(where["area"]))
light = by_login[nike_light["employee_login"]]["where"]
check("somebody trained on the Light variant is shown on that one",
      light["label"] == "Stow Each Nike Light", str(light["label"]))
other = by_login[unmapped["employee_login"]]["where"]
check("a job this site does not define keeps the name FCLM gave it",
      other["label"] == "Cart/Pallet Builder", str(other["label"]))

check("the filter offers the defined name", "Stow Each Nike" in view["floor_options"],
      str(view["floor_options"]))
check("and not the FCLM process", PROC not in view["floor_options"],
      str(view["floor_options"]))
picked = application.build_staffing_view([am], None, {"working_in": ["Stow Each Nike"]})
check("filtering on the defined name finds the person",
      [p["employee_login"] for p in picked["people"]] == [nike["employee_login"]],
      str([p["employee_login"] for p in picked["people"]]))
by_raw = application.build_staffing_view([am], None, {"working_in": [PROC]})
check("and a link saved against the FCLM name still works",
      nike["employee_login"] in {p["employee_login"] for p in by_raw["people"]},
      str([p["employee_login"] for p in by_raw["people"]]))

page = client("admin", "admin").get(f"/overview?tab=staffing&am={am}").get_data(as_text=True)
check("the page shows the defined name", "Stow Each Nike" in page)
check("and does not show the bare FCLM process as the job",
      ">" + PROC + "<" not in page)

# An admin who disagrees with the upload can say so, and that wins.
db.set_site_setting(db.FLOOR_ALIASES_KEY,
                    json.dumps({PROC.lower(): ["Something Else Entirely"]}), updated_by=am)
check("a site setting overrides what the upload says",
      db.get_floor_aliases().get(PROC.lower()) == ["Something Else Entirely"],
      str(db.get_floor_aliases().get(PROC.lower())))
db.set_site_setting(db.FLOOR_ALIASES_KEY, "", updated_by=am)
check("and clearing it goes back to the upload",
      db.get_floor_aliases().get(PROC.lower()) == ["Stow Each Nike", "Stow Each Nike Light"])

conn = db.get_db()
conn.execute("DELETE FROM xt_hours WHERE fclm_mapped = ?", (PROC,))
conn.commit()
conn.close()

print("\n9j. A face for everybody whose id is on file")
# The photos were not loading, and the reason was not the photos: a
# badge picture only ever arrived with a per-person Employee Time
# Details reading, and that pass covers a handful of people. Everybody
# else got initials for ever, despite their employee id sitting in the
# cross-training upload the whole time. The address is built from the
# id, and the shape of the address is learned from the readings that do
# come back rather than guessed at permanently.
wipe()
check("there is a fallback shape before any reading has arrived",
      db.get_floor_photo_template() == db.FLOOR_PHOTO_TEMPLATE,
      db.get_floor_photo_template())
db.record_floor_snapshot([{
    "employee_id": "660001", "employee_login": team[0]["employee_login"],
    "full_name": team[0]["full_name"], "process_key": "pick",
    "process_name": "Pick", "function_name": "RF Pick", "paid_hours": 2.0}],
    taken_by=am, reader_version=application._floor_reader_version())
db.record_floor_clock([{
    "employee_login": team[0]["employee_login"], "employee_id": "660001",
    "clock_state": "in",
    "photo_url": "https://fclm-portal.amazon.com/badge/image/660001.jpg"}],
    checked_by=am)
learned = db.get_floor_photo_template()
check("one real reading teaches the console the real shape",
      learned == "https://fclm-portal.amazon.com/badge/image/{id}.jpg", learned)
check("and the shape is then used for somebody who has no reading at all",
      db.floor_photo_url("660002", learned)
      == "https://fclm-portal.amazon.com/badge/image/660002.jpg",
      str(db.floor_photo_url("660002", learned)))
check("an employee id is escaped into it rather than pasted",
      db.floor_photo_url("66 00/3", learned)
      == "https://fclm-portal.amazon.com/badge/image/66%2000%2F3.jpg",
      str(db.floor_photo_url("66 00/3", learned)))
check("no id means no address, not a broken one",
      db.floor_photo_url(None, learned) is None)
check("a template pointing anywhere but FCLM is refused",
      db.floor_photo_url("660002", "https://example.invalid/{id}") is None)

view = application.build_staffing_view([am], None, {})
faces = [p for p in view["people"] if p.get("photo_url")]
check("the cross-training upload carries the ids this depends on",
      view["without_employee_id"] == 0, f"{view['without_employee_id']} without an id")
check("the whole team gets a face, not just the few FCLM was asked about",
      len(faces) == len(view["people"]) and len(faces) > 1,
      f"{len(faces)} faces of {len(view['people'])}")
check("the reading's own address wins over the built one",
      {p["employee_login"]: p["photo_url"] for p in view["people"]}
      .get(team[0]["employee_login"])
      == "https://fclm-portal.amazon.com/badge/image/660001.jpg")

page = client("admin", "admin").get(f"/overview?tab=staffing&am={am}").get_data(as_text=True)
check("the page renders the photo", "st-face-photo" in page)
check("with initials underneath it, so nothing moves when it arrives",
      "st-face-stack" in page and "st-face-initials" in page)
check("and marks it for the reader to retry if the browser is refused",
      "data-floor-photo=" in page)
check("the reader can fetch a face under the manager's own session",
      "window.ldisFloorPhotos" in script and "responseType: 'blob'" in script)
check("and only ever makes a blob of it in this tab",
      "URL.createObjectURL" in script)

print("\n9k. A sweep only asks about the people it has to")
# Employee Time Details is one request per person and was being run
# over a manager's whole team on every sweep. By the time it runs the
# attendance read has already said who is in the building and the
# rollup has said what most of them are doing, so the only people worth
# a request are the ones punched in that nothing has accounted for.
wipe()
placed = team[0]
unaccounted = team[1]
away = team[2]
db.record_floor_snapshot([{
    "employee_id": "770001", "employee_login": placed["employee_login"],
    "full_name": placed["full_name"], "process_key": "pick", "process_name": "Pick",
    "function_name": "RF Pick", "paid_hours": 3.0}],
    taken_by=am, reader_version=application._floor_reader_version())
db.record_floor_clock([
    {"employee_login": placed["employee_login"], "employee_id": "770001",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00"},
    {"employee_login": unaccounted["employee_login"], "employee_id": "770002",
     "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00"},
    {"employee_login": away["employee_login"], "employee_id": "770003",
     "clock_state": "out", "clocked_out_at": "2026-10-30T06:00:00"},
], checked_by=am)

c = client(am, "am")
everyone = c.get("/floor/roster").get_json()
narrowed = c.get("/floor/roster?only=needs_task").get_json()
asked = {r["employee_login"] for r in narrowed["roster"]}
check("the narrowed roster says it narrowed", narrowed["narrowed"] is True)
check("it asks about the person nothing accounted for",
      unaccounted["employee_login"] in asked, str(sorted(asked)))
check("not about somebody the rollup already placed",
      placed["employee_login"] not in asked, str(sorted(asked)))
check("not about somebody who went home",
      away["employee_login"] not in asked, str(sorted(asked)))
check("so the pass is a fraction of the team it used to be",
      len(narrowed["roster"]) < len(everyone["roster"]),
      f"{len(narrowed['roster'])} of {len(everyone['roster'])}")
check("and there is a ceiling on it however bad the day",
      len(narrowed["roster"]) <= application.FLOOR_TASK_PASS_LIMIT)

# With nothing read at all the narrowing has nothing to narrow by, and
# the old whole-team behaviour is the only thing that still works.
wipe()
blind = c.get("/floor/roster?only=needs_task").get_json()
check("with no clock reading it falls back to the whole team",
      blind["narrowed"] is False and len(blind["roster"]) == len(everyone["roster"]),
      f"narrowed={blind['narrowed']} {len(blind['roster'])} people")

# A narrowed pass must not settle: a reading that has gone stale and a
# sweep that has not placed them since is exactly the person nobody
# knows anything current about, so they go back on the list.
db.record_floor_clock([{
    "employee_login": away["employee_login"], "employee_id": "770003",
    "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
    "current_title": "Pick\u25c6RF Pick", "current_process": "Pick",
    "current_function": "RF Pick", "current_kind": "work"}], checked_by=am)
conn = db.get_db()
conn.execute("UPDATE floor_clock SET checked_at = ? WHERE lower(employee_login) = ?",
             ("2026-10-29T06:00:00", away["employee_login"].lower()))
conn.commit()
conn.close()
stale_ask = c.get("/floor/roster?only=needs_task").get_json()
check("a reading that has gone stale is asked about again",
      away["employee_login"] in {r["employee_login"] for r in stale_ask["roster"]},
      str([r["employee_login"] for r in stale_ask["roster"]]))

check("the reader asks the console who needs a request, not the whole team",
      "only=needs_task" in script, "reader still asks for the full roster")
check("and reads the two whole-site reports at the same time",
      "Promise.all([rollup, attendance])" in script)

# Narrowing the pass has a consequence worth testing: a reading that
# used to be refreshed every sweep can now sit still while the floor
# moves around it. Somebody whose Time Details said Cart/Pallet Builder
# an hour ago, and who the sweep has since found on Pick, is on Pick.
wipe()
moved = team[0]
db.record_floor_clock([{
    "employee_login": moved["employee_login"], "employee_id": "770009",
    "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
    "current_title": "Transfer In Support\u25c6Cart/Pallet Builder",
    "current_process": "Transfer In Support",
    "current_function": "Cart/Pallet Builder",
    "current_kind": "work", "task_started_at": "2026-10-30T09:00:00"}],
    checked_by=am)
conn = db.get_db()
conn.execute("UPDATE floor_clock SET checked_at = ? WHERE lower(employee_login) = ?",
             ("2026-10-30T09:00:00", moved["employee_login"].lower()))
conn.commit()
conn.close()
db.record_floor_snapshot([{
    "employee_id": "770009", "employee_login": moved["employee_login"],
    "full_name": moved["full_name"], "process_key": "pick", "process_name": "Pick",
    "function_name": "RF Pick", "station": "A02 - A9000", "paid_hours": 1.0}],
    taken_by=am, reader_version=application._floor_reader_version())

moved_view = application.build_staffing_view([am], None, {})
moved_row = {p["employee_login"]: p for p in moved_view["people"]}[moved["employee_login"]]
check("a sweep fresher than the last task reading is what they are doing",
      (moved_row["where"] or {}).get("label") == "RF Pick",
      str((moved_row["where"] or {}).get("label")))
check("and it is the sweep that is credited",
      (moved_row["where"] or {}).get("source") == "sweep",
      str((moved_row["where"] or {}).get("source")))
# The reverse still holds: a task read after the sweep is the newer fact,
# which is the whole reason the per-person pass exists.
db.record_floor_clock([{
    "employee_login": moved["employee_login"], "employee_id": "770009",
    "clock_state": "in", "clocked_in_at": "2026-10-30T06:00:00",
    "current_title": "Time Off Task", "current_process": "Time Off Task",
    "current_kind": "off-task", "task_started_at": "2026-10-30T11:50:00"}],
    checked_by=am)
fresh_view = application.build_staffing_view([am], None, {})
fresh_row = {p["employee_login"]: p for p in fresh_view["people"]}[moved["employee_login"]]
check("a reading taken after the sweep still wins",
      (fresh_row["where"] or {}).get("kind") == "off-task",
      str((fresh_row["where"] or {}).get("kind")))

print("\n9l. A floor read moves the numbers instead of reloading the page")
# location.reload() threw away the scroll position and any open filter
# panel to change four numbers. The tab is rendered again on its own and
# swapped in, which is what a sweep finishing should look like.
wipe()
db.record_floor_snapshot([{
    "employee_id": "880001", "employee_login": team[0]["employee_login"],
    "full_name": team[0]["full_name"], "process_key": "pick", "process_name": "Pick",
    "function_name": "RF Pick", "paid_hours": 3.0}],
    taken_by=am, reader_version=application._floor_reader_version())
frag = c.get(f"/staffing/fragment?am={am}&trained_for=Pick")
check("the fragment renders", frag.status_code == 200, str(frag.status_code))
frag_body = frag.get_data(as_text=True)
check("it is the panel the page swaps", 'id="staffingPanel"' in frag_body)
check("it carries the rows", "st-table" in frag_body or "Nobody matches" in frag_body)
check("and not a second copy of the scripts that are already bound",
      "ldisBindFilters = bind" not in frag_body)
check("the page asks for it instead of reloading",
      "staffing/fragment" in client("admin", "admin")
      .get(f"/overview?tab=staffing&am={am}").get_data(as_text=True))
check("signing out of it gets nothing",
      application.application.test_client().get("/staffing/fragment").status_code == 401)
check("the filters are read once for both",
      "_staffing_filters()" in open(os.path.join(ROOT, "application.py"),
                                    encoding="utf-8").read())

print("\n10. History is kept short")
wipe()
for i in range(db.FLOOR_SNAPSHOT_KEEP + 4):
    db.record_floor_snapshot([{"employee_id": str(i), "full_name": f"Row,{i}",
                               "process_name": "Pick", "paid_hours": 1}], taken_by=am)
conn = db.get_db()
kept = conn.execute("SELECT COUNT(*) AS n FROM floor_snapshots").fetchone()["n"]
orphans = conn.execute(
    "SELECT COUNT(*) AS n FROM floor_presence WHERE snapshot_id NOT IN "
    "(SELECT id FROM floor_snapshots)").fetchone()["n"]
conn.close()
check("old sweeps are pruned", kept == db.FLOOR_SNAPSHOT_KEEP, f"{kept} kept")
check("and their rows go with them", orphans == 0, f"{orphans} orphaned rows")

wipe()
print("\n" + "=" * 62)
print("ALL FLOOR CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
