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
      "Trained for it, here, working elsewhere" in body)
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
