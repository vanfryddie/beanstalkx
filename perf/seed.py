"""Seed a realistic SCN2 site database for performance work.

Builds an org shaped like a real FC: a few SOMs, OMs under them, and a
larger AM layer under those, each AM owning a team of associates with
tracked_items across every scorecard section, plus xt_hours rows and
OLR weekly snapshots so trend queries have something to walk.
"""
import os
import zlib, random, sys
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
# The processes the cross-training standards actually target, not a
# generic list. A seed whose process names nothing in the standards
# recognises makes every Cross-Training page read zero, which is a
# development database that hides the bugs it exists to show.
PROCESSES = ["Pick", "Pack Singles", "Pack Multis", "Stow Each Nike",
             "Pick To Rebin", "Chutings", "Rebin", "Decant"]

# The "FCLM Mapped" column is the department somebody belongs to —
# _xt_source_employees matches a standard's source departments against
# it — so these are the department names the standards name as sources.
# Each process is seeded into the department it would really sit in.
# Every department the processes above live in — one per person, since
# that is what the column means.
HOME_DEPARTMENTS = ["RSP", "Pack Singles", "Pack Multis", "Chutings", "Receive"]

FCLM_MAPPED = {
    "Pick": "RSP",
    "Pack Singles": "Pack Singles",
    "Pack Multis": "Pack Multis",
    "Stow Each Nike": "RSP",
    "Pick To Rebin": "Pack Multis",
    "Chutings": "Chutings",
    "Rebin": "Chutings",
    "Decant": "Receive",
}
PROFICIENCY = ["Proficient", "Practice", "Refresh", "Lapsed"]

# Indirect roles, which a real site records in the IR dashboard upload.
# Without them a seeded site has nobody holding a role, so every page
# and filter built on them reads empty and looks broken — and the tests
# that cover them pass only against a database somebody had uploaded a
# real file into.
INDIRECT_ROLES = ["Jambuster", "Water Spider", "Problem Solver", "Trainer",
                  "Flow Lead", "Quality Auditor"]
IR_STATUSES = ["Active", "Active", "Active", "In Training", "Lapsed"]


def seed(n_som=2, n_om_per_som=3, n_am_per_om=6, n_assoc_per_am=25, weeks=10):
    db.init_db()
    conn = db.get_db()
    for table in ("tracked_items", "user_roles", "xt_hours", "olr_weekly_metrics",
                  "ambassadors", "user_department_assignments", "ir_dashboard"):
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
            # A nine-digit badge number, as the upload carries it. Stable
            # per login, because the console learns the login-to-id
            # mapping and a seed that reshuffled it every run would make
            # that look broken.
            emp_id = str(100000000 + (zlib.crc32(emp.encode()) % 99999999))
            # One home department per person, which is what "FCLM
            # Mapped" means. Seeding it per row gave the same associate
            # a different department on every process they held, so
            # nobody was ever cross-trained in from anywhere and the
            # whole point of the data was invisible.
            home = HOME_DEPARTMENTS[i % len(HOME_DEPARTMENTS)]
            own = [proc for proc in PROCESSES if FCLM_MAPPED.get(proc) == home]
            others = [proc for proc in PROCESSES if proc not in own]
            # Their own department's work, plus one to three processes
            # cross-trained in from elsewhere — which is the shape the
            # standards exist to describe.
            held = own + [others[(i + k) % len(others)] for k in range(1 + i % 3)]
            for p in dict.fromkeys(held):
                status = PROFICIENCY[(i + len(p)) % len(PROFICIENCY)]
                cur.execute(
                    "INSERT INTO xt_hours (fc, fclm_mapped, employee_id, employee_login, "
                    "full_name, supervisor_login, "
                    "proficiency_status, merged_function, active_status, trained_status, "
                    "fclm_area, shift, hours_60, hours_90, hours_180, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("SCN2", home, emp_id, emp, emp.upper(), am_login,
                     status, p, "Active", "Trained",
                     dept, shift, 25.0, 40.0, 80.0, now),
                )
                n_xt += 1
    conn.commit()
    conn.close()

    # Roughly a third of the floor holds an indirect role, which is the
    # shape a real IR dashboard has: a minority named, everybody else
    # blank.
    conn = db.get_db()
    cur = conn.cursor()
    n_ir = 0
    for am_login, _role, _rt, dept, shift in ams:
        for i in range(n_assoc_per_am):
            if i % 3:
                continue
            emp = f"{am_login}e{i:03d}"
            role = INDIRECT_ROLES[(i + len(am_login)) % len(INDIRECT_ROLES)]
            cur.execute(
                "INSERT INTO ir_dashboard (login, full_name, manager_login, shift, area, "
                "home_process, indirect_role, indirect_role_status, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (emp, emp.upper(), am_login, shift, dept, PROCESSES[i % len(PROCESSES)],
                 role, IR_STATUSES[(i + len(role)) % len(IR_STATUSES)], now),
            )
            n_ir += 1
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
          f"{n_items} tracked_items, {n_xt} xt_hours, {n_ir} indirect roles, "
          f"{n_snap} olr snapshots")
    return [m[0] for m in ams], [m[0] for m in managers]


if __name__ == "__main__":
    seed()
