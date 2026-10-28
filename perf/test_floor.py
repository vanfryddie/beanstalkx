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
    for table in ("floor_presence", "floor_snapshots", "floor_identity"):
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
parts = []
for fn in ("headerIndex", "functionNameFor", "parseRollup", "cleanStation", "numberAt"):
    m = re.search(r"(?ms)^    function " + fn + r"\(.*?\n    \}\n", script)
    if not m:
        check(f"the script still defines {fn}", False)
    else:
        parts.append(m.group(0).replace("    function", "function"))

rows = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    pg = b.new_context().new_page()
    pg.goto("about:blank")
    pg.add_script_tag(content="\n".join(parts))
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
check("somebody in two functions is reported in both",
      len([r for r in rows if r["employee_id"] == "100356745"]) == 2)

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
check("it counts only people who were actually seen",
      view["on_site"] == len([r for r in rows if r["employee_login"] in
                              {p["employee_login"] for p in team}]),
      str(view["on_site"]))
check("another manager's people are not in this view",
      not ({p["employee_login"] for p in view["people"]}
           & {p["employee_login"] for p in others}))

on_pick = application.build_staffing_view([am], None, {"working_in": "Pick"})
check("filtering to a process returns only people in it",
      all(p["floor"] and p["floor"]["process_name"] == "Pick" for p in on_pick["people"]),
      str(len(on_pick["people"])))

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
check("the off-site filter returns only people not seen",
      all(p["floor_state"] != "present" for p in off["people"]), str(len(off["people"])))

print("\n6. Nothing is claimed when the floor has not been read")
wipe()
blank = application.build_staffing_view([am], None, {})
check("with no snapshot there is no metadata to show", blank["meta"] is None)
check("and nobody is counted as on the floor", blank["on_site"] == 0)
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
check("the staffing tab renders the floor", "on the floor." in body.lower())
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
td_parts = []
for fn in ("fclmDate", "pad", "parseSessionTime", "infoField", "parseTimeDetails"):
    m = re.search(r"(?ms)^    function " + fn + r"\(.*?\n    \}\n", script)
    if not m:
        check(f"the reader still defines {fn}", False)
    else:
        td_parts.append(m.group(0).replace("    function", "function"))

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox"])
    pg = b.new_context().new_page()
    pg.goto("about:blank")
    pg.add_script_tag(content="\n".join(td_parts))
    def read(name):
        html = open(os.path.join(ROOT, "perf/fixtures", name), encoding="utf-8").read()
        return pg.evaluate("([h, iso]) => parseTimeDetails(h, new Date(iso))",
                           [html, "2026-10-28T11:45:00"])
    on = read("employee_time_details.html")
    off = read("employee_time_details_off.html")
    b.close()

check("an open session reads as punched in", on["clock_state"] == "in", str(on["clock_state"]))
check("with the time they clocked in", (on["clocked_in_at"] or "").startswith("2026-10-28T06:00"),
      str(on["clocked_in_at"]))
check("a finished shift reads as punched out", off["clock_state"] == "out", str(off["clock_state"]))
check("with the time they clocked out", (off["clocked_out_at"] or "").startswith("2026-10-28T10:30"),
      str(off["clocked_out_at"]))
check("the login comes off the page, so no profile service is needed",
      on["employee_login"] == "abbapfai", str(on["employee_login"]))
check("so does the employee id", on["employee_id"] == "208651433", str(on["employee_id"]))
check("and the shift code", on["shift_code"] == "D58F88MZ", str(on["shift_code"]))
check("the badge photo is an FCLM address",
      (on["photo_url"] or "").startswith("https://fclm-portal.amazon.com/"), str(on["photo_url"]))

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
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto("http://127.0.0.1:5642/", wait_until="domcontentloaded")
    pg.fill("input[name=login_id]", "admin"); pg.click("button[type=submit]"); pg.wait_for_timeout(500)
    pg.goto(f"http://127.0.0.1:5642/overview?tab=staffing&am={am}", wait_until="networkidle")
    pg.wait_for_timeout(400)
    check("no uncaught JavaScript on the staffing tab", not errors, "; ".join(errors[:2]))

    pg.eval_on_selector(".st-filters", "e => e.scrollIntoView({block: 'center'})")
    pg.wait_for_timeout(200)
    pg.query_selector_all(".xf-button")[0].click()
    pg.wait_for_timeout(300)
    on_top = pg.evaluate("""() => {
      const panel = document.querySelector('.xf-panel:not([hidden])');
      const r = panel.getBoundingClientRect();
      const hit = (dy) => document.elementFromPoint(r.left + r.width / 2, r.top + dy);
      return [hit(10), hit(r.height / 2), hit(r.height - 8)]
        .map(el => el ? !!el.closest('.xf-panel') : false);
    }""")
    check("the open panel is on top from its first row to its last",
          all(on_top), str(on_top))
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

    # A face that cannot load must become initials, not a broken image.
    fallback = pg.evaluate("""() => {
      const img = document.querySelector('img.st-face');
      if (!img) return 'no photo in this fixture';
      img.dispatchEvent(new Event('error'));
      return document.querySelector('.st-face-initials') ? 'initials' : 'still broken';
    }""")
    check("a photo that will not load falls back to initials",
          fallback in ("initials", "no photo in this fixture"), str(fallback))

    over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("no horizontal page scroll", over <= 1, f"overflows {over}px")
    ctx.close(); b.close()
srv.shutdown()

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
