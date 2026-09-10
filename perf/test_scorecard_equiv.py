"""Differential test: the SQL-counting scorecard() must agree with the
original fetch-all-rows-and-count-in-Python version for every scope
shape and status value, including statuses in none of the known sets.
"""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

random.seed(99)


def original_scorecard(section_list, am_login=None, fc=None):
    """Verbatim pre-optimization implementation, kept here as the oracle."""
    items = db.get_items(section=section_list, am_login=am_login, fc=fc)
    out = {"ok": 0, "risk": 0, "gap": 0, "total": len(items)}
    for it in items:
        out[db.status_bucket(it["status"])] += 1
    out["pct_ok"] = round(100 * out["ok"] / out["total"], 1) if out["total"] else None
    return out


db.set_current_site("SCN2")
db.init_db()
conn = db.get_db()
conn.execute("DELETE FROM tracked_items WHERE am_login LIKE 'equiv%'")
conn.commit()

# Deliberately messy: known-ok, known-risk, known-gap, plus statuses the
# app has never heard of, odd casing, and an empty string.
STATUSES = ["Compliant", "Graduated", "Due Soon", "In Progress", "Overdue",
            "Not Started", "Gap", "Fully Covered", "Partially Covered",
            "WeirdUnknownStatus", "compliant", "", "N/A", "Waived"]
SECTIONS = ["planning_compliance", "compliance_safety", "planning_de_tech",
            "planning_bts", "planning_indirect_roles"]
AMS = [f"equiv{i}" for i in range(6)]
FCS = ["SCN2", "STR1"]

cur = conn.cursor()
for i in range(900):
    cur.execute(
        "INSERT INTO tracked_items (section, employee_login, full_name, fc, am_login, "
        "subcategory, status, due_date, value, notes, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (random.choice(SECTIONS), f"emp{i}", f"EMP{i}", random.choice(FCS),
         random.choice(AMS), "topic", random.choice(STATUSES), None, None, "", db._now()),
    )
conn.commit()
conn.close()

cases = []
for sec in SECTIONS:
    cases.append((sec, None, None))
    cases.append(([sec], None, None))
cases += [
    (SECTIONS, None, None),
    (SECTIONS, AMS[0], None),
    (SECTIONS, AMS, None),
    (SECTIONS, AMS[:3], "SCN2"),
    (SECTIONS, AMS[:3], "STR1"),
    (SECTIONS, None, "SCN2"),
    ([], None, None),                 # empty section list -> nothing
    (SECTIONS, [], None),             # empty scope       -> nothing
    ([], [], None),
    (SECTIONS, "nobody_at_all", None),
    (["no_such_section"], None, None),
    (None, AMS[0], None),
    (None, None, None),
]

fails = 0
for section_list, am_login, fc in cases:
    got = db.scorecard(section_list, am_login=am_login, fc=fc)
    want = original_scorecard(section_list, am_login=am_login, fc=fc)
    if got != want:
        fails += 1
        print(f"  MISMATCH section={section_list!r} am={am_login!r} fc={fc!r}")
        print(f"     new={got}")
        print(f"     old={want}")

conn = db.get_db()
conn.execute("DELETE FROM tracked_items WHERE am_login LIKE 'equiv%'")
conn.commit()
conn.close()

print(f"\n{len(cases)} scope combinations over 900 rows with {len(STATUSES)} distinct statuses")
print("ALL AGREE with the original implementation" if not fails else f"{fails} MISMATCHES")
sys.exit(1 if fails else 0)
