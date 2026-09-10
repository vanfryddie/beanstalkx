"""Seed Ambassador and Indirect Roles data for the current site.

The base seed leaves both empty, which is a useful case (it proves the
dashboards say "no data" instead of inventing a score) but not enough to
prove the engines compute correctly. This fills them so Ambassador
Availability, Ambassador Readiness and Indirect Roles produce real
numbers at manager and site scope alike.
"""
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db


def seed(site="SCN2", seed_value=7):
    random.seed(seed_value)
    db.set_current_site(site)
    db.init_db()
    now = db._now()

    conn = db.get_db()
    conn.execute("DELETE FROM ambassadors")
    conn.execute("DELETE FROM ir_dashboard")
    conn.execute("DELETE FROM ir_roster")
    conn.commit()
    conn.close()

    # Targets: a real number per ambassador department and shift.
    conn = db.get_db()
    cur = conn.cursor()
    for department in db.AMBASSADOR_DEPARTMENTS:
        for shift in db.SHIFTS:
            cur.execute(
                "UPDATE ambassador_targets SET target=? WHERE department=? AND shift=?",
                (4, department, shift))
    conn.commit()
    conn.close()

    # Ambassadors: most departments staffed to target, two deliberately short.
    short = {"Ship Dock", "ICQA"}
    n_amb = 0
    for department in db.AMBASSADOR_DEPARTMENTS:
        for shift in db.SHIFTS:
            count = 2 if department in short else 4
            for i in range(count):
                login = f"amb{abs(hash((department, shift, i))) % 100000:05d}"
                db.add_ambassador(department, shift, login, login.upper(), "seed",
                                  is_process=True, is_indirect_roles=(i % 2 == 0))
                n_amb += 1

    # Indirect Roles: a roster carrying each configured role, most trained.
    # compute_ir_overview() does not read ir_dashboard's shift/area/
    # home_process columns at all — _ir_enrich_rows overwrites them from
    # the row's shift_pattern code and from the login's management area
    # id in ir_roster. Seeding those two is what makes the engine see
    # anybody; writing shift/home_process directly (the obvious guess)
    # produces a roster the engine silently ignores.
    shift_pattern_for = {}
    for pattern, letter in db.IR_HELPER_SHIFT_PATTERNS.items():
        shift_pattern_for.setdefault({"F": "early", "S": "late", "N": "night"}.get(letter), pattern)

    # Management area ids whose helper entry names a real home process,
    # so a seeded person lands in a department the role config matches.
    area_ids_by_home_process = {}
    for area_id, (home_process, _area) in db.IR_HELPER_AREA_BY_ID.items():
        if home_process:
            area_ids_by_home_process.setdefault(home_process, area_id)

    cfg = [r for r in db.get_ir_role_config_rows() if (r.get("src_type") or "") == "IR"]
    TRAINED = "Trained with practice"          # in IR_TRAINED_STATUSES
    OTHER = ["Trained but no practice", "Not trained but with practice"]

    conn = db.get_db()
    cur = conn.cursor()
    n_ir = n_roster = 0
    for row in cfg:
        home_process = row.get("home_process") or ""
        area_id = area_ids_by_home_process.get(home_process)
        if area_id is None:
            continue
        match_val = (row.get("match_vals") or row["role"]).split("|")[0].strip()
        for shift in db.SHIFTS:
            pattern = shift_pattern_for.get(shift)
            target = int(row.get({"early": "es", "late": "ls", "night": "ns"}[shift]) or 0)
            for i in range(max(target, 1)):
                login = f"ir{n_ir:05d}"
                status = TRAINED if i < target * 0.75 else random.choice(OTHER)
                cur.execute(
                    "INSERT INTO ir_roster (login, management_area_id, full_name, updated_at) "
                    "VALUES (?,?,?,?)", (login, str(area_id), login.upper(), now))
                n_roster += 1
                cur.execute(
                    "INSERT INTO ir_dashboard (login, full_name, shift_pattern, "
                    "indirect_role, indirect_role_status, updated_at) VALUES (?,?,?,?,?,?)",
                    (login, login.upper(), pattern, match_val, status, now))
                n_ir += 1
    conn.commit()
    conn.close()

    print(f"{site}: {n_amb} ambassadors, {n_ir} ir_dashboard rows, "
          f"{n_roster} ir_roster rows, targets on {len(db.AMBASSADOR_DEPARTMENTS)} departments")


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "SCN2")
