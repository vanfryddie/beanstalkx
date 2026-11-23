"""Shift configuration: fixed working times that vary by day, and codes
whose days off rotate through a cycle.

The console used to read the first letter of a shift code and nothing
else. Everything that depends on "how many hours is this person
available" — every capacity and target figure — was dividing by one
number for the whole site, so these checks are mostly about the maths
coming out right on the days that are not a Tuesday.
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


def wipe():
    # Codes reference the sets and rotations, so they go first.
    conn = db.get_db()
    for table in ("shift_codes", "shift_rotation_days", "shift_rotations",
                  "shift_time_days", "shift_time_sets"):
        conn.execute("DELETE FROM " + table)
    conn.commit()
    conn.close()


def fresh():
    db.end_request_scope()
    db.begin_request_scope()


db.set_current_site("SCN2")
db.init_db()
wipe()
fresh()

print("1. A day's paid hours")
check("an ordinary early", db.shift_day_hours("06:00", "16:30", 30) == 10.0,
      str(db.shift_day_hours("06:00", "16:30", 30)))
check("a shorter Friday", db.shift_day_hours("06:00", "14:30", 30) == 8.0)
check("a Saturday with no break", db.shift_day_hours("06:00", "12:00", 0) == 6.0)
# An end before the start is tomorrow morning, not a mistake — get this
# wrong and every night code reads as a negative day.
check("a night shift crosses midnight", db.shift_day_hours("22:00", "06:30", 30) == 8.0,
      str(db.shift_day_hours("22:00", "06:30", 30)))
check("exactly midnight to midnight is a full day",
      db.shift_day_hours("00:00", "00:00", 0) == 24.0, str(db.shift_day_hours("00:00", "00:00", 0)))
check("a missing time is no hours", db.shift_day_hours("", "16:30", 0) == 0.0)
check("and a break longer than the shift does not go negative",
      db.shift_day_hours("06:00", "08:00", 600) == 0.0)

print("\n2. Working times vary by day")
early = db.save_shift_time_set(None, "Early A", "early", [
    {"weekday": 0, "start_time": "06:00", "end_time": "16:30", "unpaid_break_minutes": 30},
    {"weekday": 1, "start_time": "06:00", "end_time": "16:30", "unpaid_break_minutes": 30},
    {"weekday": 2, "start_time": "06:00", "end_time": "16:30", "unpaid_break_minutes": 30},
    {"weekday": 3, "start_time": "06:00", "end_time": "16:30", "unpaid_break_minutes": 30},
    {"weekday": 4, "start_time": "06:00", "end_time": "14:30", "unpaid_break_minutes": 30},
    {"weekday": 5, "start_time": "06:00", "end_time": "12:00", "unpaid_break_minutes": 0},
    {"weekday": 6, "start_time": "", "end_time": ""},
], note="Mon-Thu long, short Friday, short Saturday", updated_by="admin")
fresh()
sets = db.get_shift_time_sets()
check("the set is stored", len(sets) == 1, str(len(sets)))
stored = sets[0]
check("with six days that have times", stored["days_with_times"] == 6, str(stored["days_with_times"]))
check("Monday is ten hours", stored["hours_by_weekday"][0] == 10.0)
check("Friday is eight", stored["hours_by_weekday"][4] == 8.0)
check("Saturday is six", stored["hours_by_weekday"][5] == 6.0)
check("and Sunday is not worked", stored["hours_by_weekday"][6] == 0.0)
check("the typical day is the average of the days worked",
      stored["typical_hours"] == round((10 * 4 + 8 + 6) / 6, 2), str(stored["typical_hours"]))

night = db.save_shift_time_set(None, "Night A", "night", [
    {"weekday": i, "start_time": "22:00", "end_time": "06:30", "unpaid_break_minutes": 30}
    for i in range(5)], updated_by="admin")
fresh()
check("a night set stores eight-hour days",
      next(s for s in db.get_shift_time_sets() if s["id"] == night)["hours_by_weekday"][0] == 8.0)

print("\n3. Days off rotate")
# Six days, then four, then five: a five-day average reached by
# rotating, which is the thing a single "days per week" number cannot
# describe.
rotation = db.save_shift_rotation(None, "6-4-5", 3, {
    0: [True, True, True, True, True, True, False],
    1: [True, True, False, True, False, False, False],
    2: [True, True, True, True, True, False, False],
}, anchor_monday="2026-11-02", updated_by="admin")
fresh()
stored = db.get_shift_rotations()[0]
check("the cycle is three weeks", len(stored["weeks"]) == 3)
check("six days in the first", stored["weeks"][0]["days"] == 6)
check("three in the second", stored["weeks"][1]["days"] == 3)
check("five in the third", stored["weeks"][2]["days"] == 5)
check("averaging across the cycle", stored["average_days"] == round(14 / 3, 2),
      str(stored["average_days"]))
check("and it names the days off",
      stored["weeks"][1]["off"] == ["Wed", "Fri", "Sat", "Sun"], str(stored["weeks"][1]["off"]))
check("an anchor that is not a Monday is moved back to one",
      db.get_shift_rotations()[0]["anchor_monday"] == "2026-11-02")

print("\n4. A code is times plus rotation, on a real calendar")
db.save_shift_code("D05A5CBY", shift="early", time_set_id=early, rotation_id=rotation,
                   updated_by="admin")
fresh()
codes = db.get_shift_codes()
code = codes[0]
check("the code resolves its times", (code["time_set"] or {}).get("name") == "Early A")
check("and its rotation", (code["rotation"] or {}).get("name") == "6-4-5")
check("it is configured", code["configured"])
check("average hours a week come from both",
      code["average_hours"] == round((10 * 4 + 8 + 6 + 10 + 10 + 10 + 10 * 4 + 8) / 3, 2),
      str(code["average_hours"]))

anchor = date(2026, 11, 2)
week0 = [db.shift_code_day("D05A5CBY", anchor + timedelta(days=d), codes=codes) for d in range(7)]
check("the first cycle week works six days",
      sum(1 for d in week0 if d["working"]) == 6, str(sum(1 for d in week0 if d["working"])))
check("Saturday of it is six hours", week0[5]["hours"] == 6.0, str(week0[5]["hours"]))
check("and Sunday is off, with a reason",
      not week0[6]["working"] and week0[6]["reason"] in ("rostered off", "no times for this weekday"),
      str(week0[6]))
week1 = [db.shift_code_day("D05A5CBY", anchor + timedelta(days=7 + d), codes=codes) for d in range(7)]
check("the second works three", sum(1 for d in week1 if d["working"]) == 3)
check("and says the day off is rostered, not untimed",
      week1[2]["reason"] == "rostered off", str(week1[2]))
check("the cycle repeats after three weeks",
      db.shift_code_day("D05A5CBY", anchor + timedelta(weeks=3), codes=codes)["cycle_week"] == 0)
check("and week two of the cycle is reported as week two",
      db.shift_code_day("D05A5CBY", anchor + timedelta(weeks=1), codes=codes)["cycle_week"] == 1)

span = db.shift_code_hours_between("D05A5CBY", anchor, anchor + timedelta(days=20), codes=codes)
check("hours across a whole cycle add up",
      span["days"] == 14 and span["hours"] == round(54 + 30 + 48, 2), str(span))
check("and nothing in it was assumed", not span["assumed"])

print("\n5. An unconfigured code is assumed, and says so")
unknown = db.shift_code_day("N99XXXXX", date(2026, 11, 23), codes=codes)
check("it still answers", unknown["working"] is True)
check("at the fallback length", unknown["hours"] == db.SHIFT_FALLBACK_HOURS)
check("but is not claimed as configured", unknown["configured"] is False)
check("and says why", unknown["reason"] == "code not configured", str(unknown["reason"]))
check("a weekend is still off under the fallback",
      not db.shift_code_day("N99XXXXX", date(2026, 11, 29), codes=codes)["working"])
assumed = db.shift_code_hours_between("N99XXXXX", date(2026, 11, 23), date(2026, 11, 29), codes=codes)
check("a span built on the fallback is flagged", assumed["assumed"] is True, str(assumed))

print("\n6. A retired code works no days")
db.save_shift_code("D05A5CBY", shift="early", time_set_id=early, rotation_id=rotation,
                   active=False, updated_by="admin")
fresh()
retired = db.shift_code_day("D05A5CBY", anchor, codes=db.get_shift_codes())
check("it is off", not retired["working"] and retired["reason"] == "code retired", str(retired))
db.save_shift_code("D05A5CBY", shift="early", time_set_id=early, rotation_id=rotation,
                   active=True, updated_by="admin")
fresh()

print("\n7. Coverage, so a page can say how much of its answer is a guess")
coverage = db.shift_coverage()
check("the codes in the data are counted", coverage["codes_seen"] >= 1, str(coverage))
check("configured ones are known apart", coverage["codes_configured"] >= 1, str(coverage))
check("and the share of people is reported",
      coverage["pct"] is not None and 0 <= coverage["pct"] <= 100, str(coverage["pct"]))
check("with the unmapped codes named", isinstance(coverage["codes_unconfigured"], list))

print("\n8. The capacity maths uses the real length of the day")
# The point of all of the above: a Saturday's hours divided by a
# Tuesday's length overstates the people it took.
fresh()
codes = db.get_shift_codes()
monday = application._xt_shift_day_hours("early", date(2026, 11, 2), codes=codes)
friday = application._xt_shift_day_hours("early", date(2026, 11, 6), codes=codes)
saturday = application._xt_shift_day_hours("early", date(2026, 11, 7), codes=codes)
check("Monday is ten", monday["hours"] == 10.0 and monday["configured"], str(monday))
check("Friday is eight", friday["hours"] == 8.0, str(friday))
check("Saturday is six", saturday["hours"] == 6.0, str(saturday))
late = application._xt_shift_day_hours("late", date(2026, 11, 2), codes=codes)
check("a shift with no configured code falls back",
      late["hours"] == db.SHIFT_FALLBACK_HOURS and not late["configured"], str(late))
# Two sets on one shift: the length used is the one most of the shift is
# on, never the average of two lengths, which is a day nobody works.
other = db.save_shift_time_set(None, "Early B", "early", [
    {"weekday": i, "start_time": "06:00", "end_time": "12:30", "unpaid_break_minutes": 30}
    for i in range(6)], updated_by="admin")
db.save_shift_code("D05A5CBZ", shift="early", time_set_id=other, updated_by="admin")
db.save_shift_code("D05A5CCA", shift="early", time_set_id=other, updated_by="admin")
fresh()
mixed = application._xt_shift_day_hours("early", date(2026, 11, 2), codes=db.get_shift_codes())
check("the commonest length wins over the average",
      mixed["hours"] == 6.0, f"{mixed} (two codes on 6h, one on 10h)")

print("\n9. The settings page")
c = client()
page = c.get("/ld-management/settings?tab=shifts", follow_redirects=True)
check("it renders", page.status_code == 200, str(page.status_code))
body = page.get_data(as_text=True)
for probe in ("Working times", "Rotations", "Shift codes", "Early A", "6-4-5"):
    check(f"it shows {probe}", probe in body)
check("and previews what the configuration produces", "The next 4 weeks" in body)

print("\n10. Saving through the page")
before = len(db.get_shift_time_sets())
c.post("/ld-management/settings/shifts/times", data={
    "name": "Late A", "shift": "late",
    "start_0": "14:00", "end_0": "23:30", "break_0": "30",
    "start_1": "14:00", "end_1": "23:30", "break_1": "30",
})
fresh()
late_set = next((s for s in db.get_shift_time_sets() if s["name"] == "Late A"), None)
check("a working-time set can be added from the form", late_set is not None)
if late_set:
    check("with the hours it was given", late_set["hours_by_weekday"][0] == 9.0,
          str(late_set["hours_by_weekday"][0]))
    check("and only the days that had times", late_set["days_with_times"] == 2,
          str(late_set["days_with_times"]))
c.post("/ld-management/settings/shifts/rotation", data={
    "name": "Four on", "cycle_weeks": "2", "anchor_monday": "2026-11-04",
    "day": ["0:0", "0:1", "0:2", "0:3", "1:1", "1:2", "1:3", "1:4"],
})
fresh()
four = next((r for r in db.get_shift_rotations() if r["name"] == "Four on"), None)
check("a rotation can be added from the form", four is not None)
if four:
    check("with four days in each week",
          [w["days"] for w in four["weeks"]] == [4, 4], str([w["days"] for w in four["weeks"]]))
    check("and the anchor pulled back to its Monday",
          four["anchor_monday"] == "2026-11-02", str(four["anchor_monday"]))

seen = sorted(db.seen_shift_codes())
if seen:
    c.post("/ld-management/settings/shifts/codes", data={
        "code": seen[:1], "time_set_id": [str(late_set["id"] if late_set else "")],
        "rotation_id": [str(four["id"] if four else "")], "active": seen[:1],
    })
    fresh()
    mapped = next((x for x in db.get_shift_codes() if x["code"] == seen[0]), None)
    check("a code can be mapped from the table", mapped and mapped["configured"], str(mapped))

print("\n11. Who may configure shifts")
viewer = client("am000000", "am")
check("an area manager cannot open the tab",
      "Working times" not in viewer.get("/ld-management/settings?tab=shifts",
                                        follow_redirects=True).get_data(as_text=True))
for route in ("times", "rotation", "codes"):
    check(f"nor post to {route}",
          viewer.post(f"/ld-management/settings/shifts/{route}",
                      data={"name": "x"}).status_code in (302, 403))

wipe()
fresh()
print("\n" + "=" * 62)
print("ALL SHIFT CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
