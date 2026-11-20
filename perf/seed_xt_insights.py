"""Seed the Cross-Training Insights tab with a plausible history.

Three things the live tables cannot produce on their own: a run of
census rollups to measure movement against, a target change log, and a
volume file. Written as a seeder rather than a fixture inside a test so
the page can be looked at, not only asserted about.
"""
import os, random, sys, zlib
from datetime import date, timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

random.seed(7)
WEEKS = 14


def seed(weeks=WEEKS):
    db.set_current_site("SCN2")
    db.init_db()
    conn = db.get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM xt_rollup_counts")
    cur.execute("DELETE FROM xt_rollup_snapshots")
    cur.execute("DELETE FROM xt_target_changes")
    cur.execute("DELETE FROM process_volume")
    conn.commit()

    # 1. A census a week, drifting the way a real one does: a slow climb
    #    in trained records and a lapse rate that worsens when training
    #    outruns the time people get on the process.
    today = date.today()
    shape = conn.execute(
        "SELECT fclm_mapped AS department, merged_function AS process, shift, "
        "trained_status, proficiency_status, COUNT(*) AS n FROM xt_hours "
        "GROUP BY fclm_mapped, merged_function, shift, trained_status, proficiency_status"
    ).fetchall()
    shape = [dict(r) for r in shape]
    snapshots = 0
    for back in range(weeks, 0, -1):
        taken = today - timedelta(weeks=back - 1, days=1)
        drift = 1.0 + (weeks - back) * 0.012
        cur.execute(
            "INSERT INTO xt_rollup_snapshots (taken_at, filename, row_count, taken_by) "
            "VALUES (?,?,?,?)",
            (taken.isoformat() + "T06:30:00", f"hours_on_function_{taken.isoformat()}.csv",
             0, "trainer01"))
        snapshot_id = cur.lastrowid
        total = 0
        for row in shape:
            n = row["n"]
            if row["proficiency_status"] == "Lapsed":
                # Lapse grows faster than the population does, which is
                # the pattern the page is meant to catch.
                n = max(0, round(n * (1.0 + (weeks - back) * 0.028)))
            elif row["trained_status"] == "Trained":
                n = max(0, round(n * drift))
            n = max(0, n + random.randint(-1, 1))
            if not n:
                continue
            total += n
            cur.execute(
                "INSERT INTO xt_rollup_counts (snapshot_id, department, process, shift, "
                "trained_status, proficiency_status, n) VALUES (?,?,?,?,?,?,?)",
                (snapshot_id, row["department"], row["process"], row["shift"],
                 row["trained_status"], row["proficiency_status"], n))
        cur.execute("UPDATE xt_rollup_snapshots SET row_count=? WHERE id=?", (total, snapshot_id))
        snapshots += 1
    conn.commit()
    conn.close()

    # 2. A handful of target changes, some with a reason and some
    #    without, because that is how a log actually looks.
    defs = db.get_xt_standard_defs("general")
    notes = [
        "Peak ramp: Pick nights needs deeper cover until week 52.",
        None,
        "Rebalanced after the Decant line moved to Inbound.",
        None,
        "SOM review: target was never achievable with this headcount.",
    ]
    moved = 0
    for i, std in enumerate(defs[:5]):
        step = 0.05 if i % 2 == 0 else -0.05
        db.bulk_update_xt_standards([{
            "id": std["id"], "label": std["label"], "group": std["group"],
            "source": std["source"], "target": std["target"],
            "pct_early": max(0, std["pct"]["early"] + step),
            "pct_late": std["pct"]["late"],
            "pct_night": max(0, std["pct"]["night"] + (step if i % 3 == 0 else 0)),
        }], changed_by="som00" if i % 2 == 0 else "trainer01", note=notes[i])
        moved += 1

    # 3. Volume: one row per day, process and shift, for the processes
    #    the standards actually target, trending up into peak.
    # Every process anything targets, plus the ones the hours file
    # actually carries — the two vocabularies only partly overlap in the
    # development data, and the point of seeding both is to see what the
    # page does with a path it can match and one it cannot.
    processes = sorted(
        {d["target"] for d in defs if d["target"]}
        | {t["target_process"] for t in db.get_internal_xt_targets() if t["target_process"]}
        | {r["merged_function"] for r in db.get_db().execute(
            "SELECT DISTINCT merged_function FROM xt_hours WHERE merged_function IS NOT NULL")
           .fetchall()})
    conn = db.get_db()
    cur = conn.cursor()
    rows = 0
    for back in range(weeks * 7, 0, -1):
        day = today - timedelta(days=back)
        ramp = 1.0 + (weeks * 7 - back) * 0.0022
        for process in processes:
            # Sized like a real FC shift rather than a toy: tens of
            # thousands of units, a hundred-odd paid hours, which is the
            # magnitude that makes "heads on shift" come out sane.
            base = 22000 + (zlib.crc32(process.encode()) % 9000)
            for shift, share in (("early", 0.45), ("late", 0.35), ("night", 0.20)):
                units = round(base * share * ramp * random.uniform(0.88, 1.12))
                rate = 110 + (zlib.crc32((process + shift).encode()) % 40)
                hours = round(units / rate, 1)
                cur.execute(
                    """INSERT INTO process_volume (fc, work_date, shift, process, units, hours,
                                                   source, uploaded_by, uploaded_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (fc, work_date, shift, process) DO UPDATE SET
                         units=excluded.units, hours=excluded.hours""",
                    ("SCN2", day.isoformat(), shift, process, units, hours,
                     "seed_xt_insights.py", "trainer01", db._now()))
                rows += 1
    conn.commit()
    conn.close()
    return {"snapshots": snapshots, "target_changes": moved, "volume_rows": rows,
            "processes": len(processes)}


if __name__ == "__main__":
    print("seeded:", seed())
