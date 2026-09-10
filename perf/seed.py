"""Seed a realistic SCN2 site database for performance work.

Builds an org shaped like a real FC: a few SOMs, OMs under them, and a
larger AM layer under those, each AM owning a team of associates with
tracked_items across every scorecard section, plus xt_hours rows and
OLR weekly snapshots so trend queries have something to walk.
"""
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

random.seed(1234)

import db

DEPARTMENTS = ["Inbound", "Outbound", "ICQA", "Ship Dock", "Sortation"]
SHIFT_KEYS = list(db.SHIFTS.keys())
SECTIONS = ["planning_compliance", "compliance_safety", "planning_de_tech",
            "planning_indirect_roles", "planning_bts"]
STATUSES = ["Compliant", "Overdue", "Due Soon", "Not Started", "In Progress", "Complete"]
TOPICS = ["FSRI", "Robotic Arm Palletizer", "Powered Industrial Truck", "Fire Safety",
          "Manual Handling", "Lockout Tagout", "Conveyor Safety", "First Aid"]
PROCESSES = ["Pack", "Pick", "Stow", "Receive", "Water Spider", "Problem Solve", "Decant"]
PROFICIENCY = ["Proficient", "Practice", "Refresh", "Lapsed"]


def seed(n_som=2, n_om_per_som=3, n_am_per_om=6, n_assoc_per_am=25, weeks=10):
    db.init_db()
    conn = db.get_db()
    for table in ("tracked_items", "user_roles", "xt_hours", "olr_weekly_metrics",
                  "ambassadors", "user_department_assignments"):
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    managers = []          # (login, role, reports_to, dept, shift)
    for s in range(n_som):
        som = f"som{s:02d}"
        managers.append((som, "som", None, None, None))
        for o in range(n_om_per_som):
            om = f"om{s:02d}{o:02d}"
            managers.append((om, "om", som, None, None))
            for a in range(n_am_per_om):
                am = f"am{s:02d}{o:02d}{a:02d}"
                dept = DEPARTMENTS[(s + o + a) % len(DEPARTMENTS)]
                shift = SHIFT_KEYS[(o + a) % len(SHIFT_KEYS)]
                managers.append((am, "am", om, dept, shift))

    conn = db.get_db()
    now = db._now()
    for login, role, reports_to, dept, shift in managers:
        conn.execute(
            "INSERT INTO user_roles (login, role, shift, department, full_name, title, "
            "assigned_by, assigned_at, reports_to) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(login) DO UPDATE SET role=excluded.role, reports_to=excluded.reports_to",
            (login, role, shift, dept, login.upper(), role.upper(), "seed", now, reports_to),
        )
    conn.commit()
    conn.close()

    ams = [m for m in managers if m[1] == "am"]

    conn = db.get_db()
    cur = conn.cursor()
    n_items = 0
    for am_login, _role, _rt, dept, shift in ams:
        for i in range(n_assoc_per_am):
            emp = f"{am_login}e{i:03d}"
            for section in SECTIONS:
                status = STATUSES[(i + len(section)) % len(STATUSES)]
                cur.execute(
                    "INSERT INTO tracked_items (section, employee_login, full_name, fc, am_login, "
                    "subcategory, status, due_date, value, notes, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (section, emp, emp.upper(), "SCN2", am_login,
                     TOPICS[(i + SECTIONS.index(section)) % len(TOPICS)], status,
                     "2026-10-01", None, "", now),
                )
                n_items += 1
    conn.commit()
    conn.close()

    conn = db.get_db()
    cur = conn.cursor()
    n_xt = 0
    for am_login, _role, _rt, dept, shift in ams:
        for i in range(n_assoc_per_am):
            emp = f"{am_login}e{i:03d}"
            for p in PROCESSES[: 3 + (i % 3)]:
                status = PROFICIENCY[(i + len(p)) % len(PROFICIENCY)]
                cur.execute(
                    "INSERT INTO xt_hours (fc, employee_login, full_name, supervisor_login, "
                    "proficiency_status, merged_function, active_status, trained_status, "
                    "fclm_area, shift, hours_60, hours_90, hours_180, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("SCN2", emp, emp.upper(), am_login, status, p, "Active", "Trained",
                     dept, shift, 25.0, 40.0, 80.0, now),
                )
                n_xt += 1
    conn.commit()
    conn.close()

    from datetime import date, timedelta
    conn = db.get_db()
    cur = conn.cursor()
    n_snap = 0
    monday = date.today() - timedelta(days=date.today().weekday())
    for login, role, _rt, _d, _s in managers:
        for w in range(weeks):
            ws = (monday - timedelta(weeks=w)).isoformat()
            for key, _label, _sec in db.SCORECARD_CATEGORIES:
                cur.execute(
                    "INSERT INTO olr_weekly_metrics (login, week_start, metric_key, value, uploaded_at) "
                    "VALUES (?,?,?,?,?)",
                    (login, ws, key, 60.0 + ((w + len(key)) % 35), now),
                )
                n_snap += 1
    conn.commit()
    conn.close()

    print(f"seeded: {len(managers)} managers ({len(ams)} AMs), "
          f"{n_items} tracked_items, {n_xt} xt_hours, {n_snap} olr snapshots")
    return [m[0] for m in ams], [m[0] for m in managers]


if __name__ == "__main__":
    seed()
