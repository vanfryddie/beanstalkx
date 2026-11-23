"""Cross-Training Insights: target history, population movement, volume,
and the target recommendations built from the three together.

Everything here is about history the live tables do not keep, so most of
these checks are about what happens between two moments rather than at
one — which is exactly the kind of thing that is easy to get subtly
wrong and impossible to notice on a page that always looks plausible.
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
    conn = db.get_db()
    for table in ("xt_rollup_counts", "xt_rollup_snapshots", "xt_target_changes",
                  "process_volume"):
        conn.execute("DELETE FROM " + table)
    # The recommendation section builds a source pool of its own. Those
    # rows are scaffolding, not data: left behind they join the roster
    # and break the suites that count the floor.
    conn.execute("DELETE FROM xt_hours WHERE employee_login LIKE ?", ("pool%",))
    conn.commit()
    conn.close()


def insights(period="week", scenario="general"):
    """Built inside a request scope, which is where it runs for real."""
    db.end_request_scope()
    db.begin_request_scope()
    try:
        return application.build_xt_insights(period, scenario=scenario)
    finally:
        db.end_request_scope()


db.set_current_site("SCN2")
db.init_db()
TODAY = date.today()

print("1. Periods")
check("an ISO week key", db.period_key(date(2026, 11, 20), "week") == "2026-W47",
      db.period_key(date(2026, 11, 20), "week"))
check("a month key", db.period_key(date(2026, 11, 20), "month") == "2026-11")
check("a quarter key", db.period_key(date(2026, 11, 20), "quarter") == "2026-Q4")
check("January 1st can belong to last year's final week, and does",
      db.period_key(date(2027, 1, 1), "week") == "2026-W53",
      db.period_key(date(2027, 1, 1), "week"))
check("the first quarter is Q1", db.period_key(date(2026, 3, 31), "quarter") == "2026-Q1")
check("a key reads back as a date", db.period_start("2026-W47") == date(2026, 11, 16))
check("a month key reads back", db.period_start("2026-11") == date(2026, 11, 1))
check("a quarter key reads back", db.period_start("2026-Q4") == date(2026, 10, 1))
check("a week is labelled by the Monday it starts",
      db.period_label("2026-W47").startswith("w/c 16 Nov"), db.period_label("2026-W47"))
check("garbage in gives a label, not an exception", db.period_label("nonsense") == "nonsense")
weeks = db.recent_periods("week", 5, date(2026, 11, 20))
check("five weeks back, oldest first, ending with this one",
      weeks == ["2026-W43", "2026-W44", "2026-W45", "2026-W46", "2026-W47"], str(weeks))
quarters = db.recent_periods("quarter", 5, date(2026, 11, 20))
check("quarters walk back across a year boundary",
      quarters == ["2025-Q4", "2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"], str(quarters))

print("\n2. Target changes are recorded, with a reason")
wipe()
defs = db.get_xt_standard_defs("general")
std = defs[0]


def save_standard(row, **over):
    payload = {"id": row["id"], "label": row["label"], "group": row["group"],
               "source": row["source"], "target": row["target"],
               "pct_early": row["pct"]["early"], "pct_late": row["pct"]["late"],
               "pct_night": row["pct"]["night"]}
    payload.update(over)
    return payload


result = db.bulk_update_xt_standards(
    [save_standard(std, pct_early=std["pct"]["early"] + 0.05)],
    changed_by="som00", note="Peak ramp")
check("a moved target is logged", result["stored"] == 1, str(result))
log = db.get_xt_target_changes()
check("only the field that moved", len(log) == 1 and log[0]["field"] == "pct_early",
      str([r["field"] for r in log]))
check("with the old value", float(log[0]["old_value"]) == round(std["pct"]["early"] * 100, 2),
      str(log[0]["old_value"]))
check("the new one", float(log[0]["new_value"]) == round((std["pct"]["early"] + 0.05) * 100, 2),
      str(log[0]["new_value"]))
check("the movement", abs(log[0]["delta"] - 5.0) < 0.01, str(log[0]["delta"]))
check("who did it", log[0]["changed_by"] == "som00")
check("and why", log[0]["note"] == "Peak ramp")
check("percentages are logged as percentages, not fractions",
      float(log[0]["new_value"]) > 1, str(log[0]["new_value"]))

again = db.bulk_update_xt_standards(
    [save_standard(std, pct_early=std["pct"]["early"] + 0.05)], changed_by="som00")
check("saving the same values again logs nothing", again["stored"] == 0, str(again))
check("so the log did not grow", len(db.get_xt_target_changes()) == 1)

multi = db.bulk_update_xt_standards([
    save_standard(std, pct_early=std["pct"]["early"], pct_night=std["pct"]["night"] + 0.1,
                  label=std["label"] + " (rev)"),
], changed_by="trainer01", note="Two things at once")
check("several fields in one save are one batch", multi["stored"] == 3, str(multi))
batch_rows = [r for r in db.get_xt_target_changes() if r["batch"] == multi["batch"]]
check("sharing a batch id", len(batch_rows) == 3, str(len(batch_rows)))
check("and one reason between them",
      {r["note"] for r in batch_rows} == {"Two things at once"})
# put it back
db.bulk_update_xt_standards([save_standard(std)], changed_by="trainer01", note="revert")

print("\n3. A reason can be added after the fact")
# The note is the part people skip in the moment and want back when the
# quarter is reviewed.
silent = db.bulk_update_xt_standards(
    [save_standard(std, pct_late=std["pct"]["late"] + 0.02)], changed_by="som00")
check("a save with no reason still records the change", silent["stored"] == 1)
check("and carries no note", not [r for r in db.get_xt_target_changes()
                                  if r["batch"] == silent["batch"] and r["note"]])
db.set_xt_change_note(silent["batch"], "Asked for by Ops after the week 46 review")
noted = [r for r in db.get_xt_target_changes() if r["batch"] == silent["batch"]]
check("until one is added later", all(r["note"] for r in noted), str(noted[0]["note"]))
db.bulk_update_xt_standards([save_standard(std)], changed_by="som00", note="revert")

print("\n4. Adding and removing a standard is in the record too")
before = len(db.get_xt_target_changes())
db.add_xt_standard("general", "probe_std", "Probe standard", "ib", ["Receive"],
                   "Pick", 0.1, 0.1, 0.1, changed_by="admin", note="probe")
created = db.get_xt_target_changes()[0]
check("creating one is logged", created["field"] == "created", created["field"])
probe = next((d for d in db.get_xt_standard_defs("general") if d["key"] == "probe_std"), None)
check("and the standard exists", probe is not None)
if probe:
    db.delete_xt_standard(probe["id"], changed_by="admin", note="probe done")
    removed = db.get_xt_target_changes()[0]
    check("removing one is logged", removed["field"] == "removed", removed["field"])
    check("with what it used to be", removed["old_value"], str(removed["old_value"]))
check("both ends of a standard's life are in the log",
      len(db.get_xt_target_changes()) == before + 2)

print("\n5. Internal targets are logged the same way")
internal = db.get_internal_xt_targets()
if internal:
    row = dict(internal[0])
    moved = db.bulk_update_internal_xt_targets([{
        "id": row["id"], "label": row["label"], "source_dept": row["source_dept"],
        "target_process": row["target_process"],
        "target_early": (row["target_early"] or 0) + 7,
        "target_late": row["target_late"], "target_night": row["target_night"],
    }], changed_by="som00", note="Night cover moved to earlies")
    check("an internal target move is logged", moved["stored"] == 1, str(moved))
    entry = db.get_xt_target_changes()[0]
    check("as an internal target", entry["kind"] == "internal", entry["kind"])
    check("with the movement", entry["delta"] == 7, str(entry["delta"]))
    db.bulk_update_internal_xt_targets([{
        "id": row["id"], "label": row["label"], "source_dept": row["source_dept"],
        "target_process": row["target_process"], "target_early": row["target_early"],
        "target_late": row["target_late"], "target_night": row["target_night"],
    }], changed_by="som00", note="revert")

print("\n6. The trained population, which no live table remembers")
wipe()
conn = db.get_db()
first = db.record_xt_rollup(conn, filename="week-one.csv", taken_by="trainer01")
conn.commit()
conn.close()
snaps = db.get_xt_rollup_snapshots()
check("an upload leaves a census behind", len(snaps) == 1, str(len(snaps)))
check("counting the rows it saw", snaps[0]["row_count"] > 0, str(snaps[0]["row_count"]))
check("as counts, not people", len(snaps[0]["counts"]) < snaps[0]["row_count"],
      f"{len(snaps[0]['counts'])} count rows for {snaps[0]['row_count']} records")
check("and the file it came from", snaps[0]["filename"] == "week-one.csv")

# Two censuses in two different weeks: the movement between them is the
# number this whole section exists for.
conn = db.get_db()
conn.execute("UPDATE xt_rollup_snapshots SET taken_at=? WHERE id=?",
             ((TODAY - timedelta(days=9)).isoformat() + "T06:00:00", first))
conn.commit()
second = db.record_xt_rollup(conn, filename="week-two.csv", taken_by="trainer01")
conn.execute("UPDATE xt_rollup_counts SET n = n + 2 WHERE snapshot_id=?", (second,))
conn.commit()
conn.close()
view = insights("week")
with_data = [p for p in view["periods"] if p["has_data"]]
check("both weeks show", len(with_data) == 2, str(len(with_data)))
check("the later one carries a net change", with_data[-1]["delta"] is not None,
      str(with_data[-1]))
check("and it is the difference between them",
      with_data[-1]["delta"] == with_data[-1]["trained"] - with_data[0]["trained"],
      f"{with_data[-1]['delta']}")
check("the first has no change to show", with_data[0]["delta"] is None)
blank = [p for p in view["periods"] if not p["has_data"]]
check("a week with no upload is left blank, not carried forward",
      blank and all(p["trained"] is None for p in blank), str(len(blank)))
check("lapsed records are counted", with_data[-1]["lapsed"] is not None)
check("and expressed as a share of trained",
      with_data[-1]["lapse_pct"] is not None and 0 <= with_data[-1]["lapse_pct"] <= 100,
      str(with_data[-1]["lapse_pct"]))

# Two uploads in the same week: the later one is the state of that week.
conn = db.get_db()
third = db.record_xt_rollup(conn, filename="week-two-again.csv")
conn.execute("UPDATE xt_rollup_counts SET n = n + 5 WHERE snapshot_id=?", (third,))
conn.commit()
conn.close()
again_view = insights("week")
latest = [p for p in again_view["periods"] if p["has_data"]][-1]
check("two uploads in one week leave the later one standing",
      latest["taken_at"] >= db.get_xt_rollup_snapshots()[-1]["taken_at"][:10],
      str(latest["taken_at"]))
check("and the week's total is the later upload's",
      latest["trained"] > with_data[-1]["trained"],
      f"{latest['trained']} vs {with_data[-1]['trained']}")

print("\n7. Volume comes in the shape the page documents")
wipe()
loaded = db.ingest_process_volume_csv(
    b"Date,SHIFT,Process,Units,Hours\n"
    b"2026-11-10,D,Pick,1000,10\n"
    b"10/11/2026,late,Pick,500,5\n"
    b"2026-11-11,N,Pick,250,2.5\n", "mixed.csv", uploaded_by="admin")
check("headers are read whatever their case", loaded["loaded"] == 3, str(loaded))
rows = db.get_process_volume()
check("the shift-code prefix this site uses is understood",
      {r["shift"] for r in rows} == {"early", "late", "night"},
      str(sorted({r["shift"] for r in rows})))
check("and a day/month/year date", {r["work_date"] for r in rows} ==
      {"2026-11-10", "2026-11-11"}, str(sorted({r["work_date"] for r in rows})))

again = db.ingest_process_volume_csv(
    b"date,shift,process,units,hours\n2026-11-10,early,Pick,1500,12\n", "correction.csv")
rows = db.get_process_volume()
check("re-uploading a day replaces it rather than doubling it",
      len(rows) == 3, str(len(rows)))
check("with the corrected figure",
      next(r["units"] for r in rows if r["work_date"] == "2026-11-10" and r["shift"] == "early") == 1500)
check("and a file for another week does not delete this one",
      len(db.get_process_volume(since="2026-11-10")) == 3)
bad = db.ingest_process_volume_csv(
    b"date,shift,process,units\n,,,\nnot-a-date,early,Pick,5\n2026-11-12,early,,9\n", "bad.csv")
check("rows with no readable date, shift or process are counted, not guessed at",
      bad["loaded"] == 0 and bad["skipped"] == 3, str(bad))
coverage = db.process_volume_coverage()
check("the page can say what it has", coverage["rows"] == 3 and coverage["processes"] == 1,
      str(coverage))

print("\n8. Recommendations")
wipe()
# A path with a real pool behind it, so the cap is not what is being
# measured here. The pool is built rather than assumed: this block ran
# green on the dev SQLite file, whose standards name departments the
# seed happens to fill, and failed on a freshly seeded Postgres, where
# the same standards name departments with two people in them. A test
# that depends on which roster it is pointed at is testing the data.
paths = application._xt_defined_paths("general")
target_path = next((p for p in paths if p["shift"] == "early" and p["process"]), None)
check("there is a defined path on early shift", target_path is not None,
      str([(p["label"], p["headcount"]) for p in paths[:4]]))

if target_path and target_path["headcount"] <= 40:
    # Who the path draws from, by the same column the pool is counted on.
    if target_path["kind"] == "standard":
        sources = next((d.get("source") or [] for d in db.get_xt_standard_defs("general")
                        if d["id"] == target_path["id"]), [])
    else:
        sources = [r.get("source_dept") for rows in (db.get_internal_xt_overview() or {}).values()
                   for r in rows if r["key"] == target_path["id"] and r.get("source_dept")]
    dept = (sources or ["Receive"])[0]
    conn = db.get_db()
    cur = conn.cursor()
    for i in range(60):
        login = f"pool{i:03d}"
        cur.execute(
            "INSERT INTO xt_hours (fc, fclm_mapped, employee_id, employee_login, full_name, "
            "proficiency_status, merged_function, active_status, trained_status, shift, "
            "hours_60, hours_90, hours_180, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("SCN2", dept, str(700000000 + i), login, login.upper(), "Proficient",
             target_path["process"], "Active", "Trained", "early", 10.0, 20.0, 40.0, db._now()))
    conn.commit(); conn.close()
    paths = application._xt_defined_paths("general")
    target_path = next((p for p in paths if p["label"] == target_path["label"]
                        and p["shift"] == "early"), None)
check("and a pool can be put behind it",
      target_path is not None and target_path["headcount"] > 40,
      str(target_path and target_path["headcount"]))

if target_path:
    process = target_path["process"]
    # Eight complete weeks, flat, with one clearly busiest day.
    conn = db.get_db()
    cur = conn.cursor()
    monday = TODAY - timedelta(days=TODAY.weekday())
    for week in range(8, 0, -1):
        start = monday - timedelta(weeks=week)
        for day in range(5):
            when = start + timedelta(days=day)
            hours = 95.0 if day else 190.0          # Monday is the peak
            cur.execute(
                """INSERT INTO process_volume (fc, work_date, shift, process, units, hours,
                                               source, uploaded_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                ("SCN2", when.isoformat(), "early", process, hours * 100, hours,
                 "test", db._now()))
    conn.commit()
    conn.close()

    view = insights("week")
    rec_path = next((p for p in view["paths"]
                     if p["process"] == process and p["shift"] == "early"
                     and p["label"] == target_path["label"]), None)
    rec = (rec_path or {}).get("recommendation")
    check("a path with volume gets a recommendation", rec is not None)
    if rec:
        shift_hours = view["cost_model"]["shift_hours"]
        cover = view["cost_model"]["cover_factor"]
        expected_peak = 190.0 / shift_hours
        check("built from the busiest day, not the average",
              abs(rec["peak_heads"] - round(expected_peak, 1)) < 0.11,
              f"{rec['peak_heads']} vs {expected_peak:.1f}")
        check("a flat eight weeks reads as flat",
              rec["trend_pct"] is not None and abs(rec["trend_pct"]) < 1.0,
              str(rec["trend_pct"]))
        check("the pool is the peak times cover",
              rec["recommended"] == application._xt_round_up(expected_peak * cover),
              f"{rec['recommended']} vs {application._xt_round_up(expected_peak * cover)}")
        check("eight weeks is enough to be confident about", rec["confident"],
              str(rec["periods_used"]))
        check("the shortfall is against who is trained, not the old target",
              rec["short_by"] == max(0, rec["recommended"] - rec["trained"]),
              f"{rec['short_by']}")
        hours = view["cost_model"]["default_train_hours"]
        rate = view["cost_model"]["hourly_cost"]
        check("and it is priced",
              rec["cost_to_close"] == round(rec["short_by"] * hours * rate),
              f"{rec['cost_to_close']}")

    # The week we are standing in is not over. Loading a part-week must
    # not read as a collapse in volume.
    conn = db.get_db()
    conn.execute(
        """INSERT INTO process_volume (fc, work_date, shift, process, units, hours, source, uploaded_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        ("SCN2", monday.isoformat(), "early", process, 9500, 95.0, "test", db._now()))
    conn.commit()
    conn.close()
    partial = insights("week")
    partial_rec = next((p["recommendation"] for p in partial["paths"]
                        if p["process"] == process and p["shift"] == "early"
                        and p["label"] == target_path["label"]), None)
    check("a part-finished week does not drag the trend down",
          partial_rec and abs(partial_rec["trend_pct"]) < 1.0, str(partial_rec and partial_rec["trend_pct"]))
    check("and the figures quoted are from a week that finished",
          partial_rec and partial_rec["latest_label"] != db.period_label(db.period_key(TODAY, "week")),
          str(partial_rec and partial_rec["latest_label"]))

print("\n9. A recommendation nobody could act on says so")
# A pool of two people cannot answer a volume asking for twenty, and a
# page that quietly says "train 20" there has wasted the reader's time.
capped = application._xt_recommendation(
    {"process": "Pick", "shift": "early", "target": 10, "trained": 1, "headcount": 3},
    [{"key": "2026-W40", "units": 10000.0, "hours": 100.0, "peak_heads": 20.0, "rate": 100.0},
     {"key": "2026-W41", "units": 10000.0, "hours": 100.0, "peak_heads": 20.0, "rate": 100.0}],
    db.get_xt_cost_model(), "week")
check("a recommendation is capped at the people the source actually holds",
      capped and capped["recommended"] == 3, str(capped and capped["recommended"]))
check("and says that is what happened", capped and capped["pool_limited"])
empty_pool = application._xt_recommendation(
    {"process": "Pick", "shift": "early", "target": 10, "trained": 0, "headcount": 0},
    [{"key": "2026-W40", "units": 10000.0, "hours": 100.0, "peak_heads": 20.0, "rate": 100.0}],
    db.get_xt_cost_model(), "week")
check("a path with nobody in its source departments is called out",
      empty_pool and empty_pool["no_pool"], str(empty_pool and empty_pool["no_pool"]))
no_volume = application._xt_recommendation(
    {"process": "Pick", "shift": "early", "target": 10, "trained": 0, "headcount": 50},
    [], db.get_xt_cost_model(), "week")
check("and no volume means no recommendation, not a guess", no_volume is None)

view = insights("week")
check("paths with no volume are named rather than silently skipped",
      any(s["title"].endswith("no volume to work from") for s in view["signals"])
      or all(p["volume_matched"] for p in view["paths"]),
      str([s["title"] for s in view["signals"]]))

print("\n10. The page")
c = client()
page = c.get("/ld-management/cross-training?tab=insights", follow_redirects=True)
check("it renders", page.status_code == 200, str(page.status_code))
body = page.get_data(as_text=True)
check("with the recap", "Recap by week" in body)
check("the change log", "Target changes" in body)
check("the defined paths", "Every defined path" in body)
check("and the cost model", "What training costs" in body)
for period in ("week", "month", "quarter"):
    r = c.get(f"/ld-management/cross-training?tab=insights&period={period}", follow_redirects=True)
    check(f"the {period} recap renders", r.status_code == 200, str(r.status_code))
check("a nonsense period falls back rather than failing",
      c.get("/ld-management/cross-training?tab=insights&period=fortnight",
            follow_redirects=True).status_code == 200)

print("\n11. Who may change what")
viewer = client("am000000", "am")
check("an area manager does not get the Insights tab",
      "Cross-training insights" not in
      viewer.get("/ld-management/cross-training?tab=insights",
                 follow_redirects=True).get_data(as_text=True))
check("nor load volume into it",
      viewer.post("/ld-management/cross-training/volume/upload").status_code in (302, 403),
      str(viewer.post("/ld-management/cross-training/volume/upload").status_code))
check("nor annotate a change",
      viewer.post("/ld-management/cross-training/insights/note",
                  data={"batch": "x", "note": "y"}).status_code in (302, 403))
check("nor rewrite what training costs",
      viewer.post("/ld-management/cross-training/insights/cost-model",
                  data={"hourly_cost": "1"}).status_code in (302, 403))
before_cost = db.get_xt_cost_model()["hourly_cost"]
c.post("/ld-management/cross-training/insights/cost-model",
       data={"hourly_cost": "27.5", "shift_hours": "9", "cover_factor": "1.4",
             "default_train_hours": "12"})
after = db.get_xt_cost_model()
check("an admin can", after["hourly_cost"] == 27.5 and after["cover_factor"] == 1.4, str(after))
check("and nonsense is refused",
      c.post("/ld-management/cross-training/insights/cost-model",
             data={"hourly_cost": "-3"}).status_code == 302
      and db.get_xt_cost_model()["hourly_cost"] == 27.5)
db.set_app_setting("xt_training_hourly_cost", before_cost, updated_by="test")

print("\n12. The note reaches the page it was written for")
wipe()
db.bulk_update_xt_standards([save_standard(std, pct_night=std["pct"]["night"] + 0.03)],
                            changed_by="som00", note="Because the chute line moved")
body = c.get("/ld-management/cross-training?tab=insights",
             follow_redirects=True).get_data(as_text=True)
check("the change is on the page", "Because the chute line moved" in body)
check("and a change without one is called out",
      "no reason given" in body or "has a reason" in body)
db.bulk_update_xt_standards([save_standard(std)], changed_by="som00", note="revert")

wipe()
wipe()
print("\n" + "=" * 62)
print("ALL CROSS-TRAINING INSIGHT CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
