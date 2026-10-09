"""An Indirect Roles percentage must never contradict its own headcount.

A role showing "11 / 12" was reading 100%, because compliance was scored
only against the people the system knew about — an unmet target simply
left the denominator. These are the real rows from the report that
exposed it.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: fails.append(name)


def pct(trained, no_practice, not_trained, target):
    """The rule compute_ir_overview applies, isolated so the cases below
    read as the table does."""
    total_people = trained + no_practice + not_trained
    denominator = max(total_people, target or 0)
    return round(100 * trained / denominator) if denominator > 0 else None


print("Rows from the report that showed the bug")
# role,                  trained, no_practice, not_trained, target, expected
CASES = [
    ("Pod Banding Operator 11/12", 11, 0, 0, 12, 92),   # was 100% — the reported bug
    ("Blitz Cleaner 9/8",           9, 0, 0,  8, 100),  # over target, all ready
    ("Blitz Cleaner 11/8",         11, 0, 0,  8, 100),
    ("ISS Problem Solver 5/5",      4, 1, 1,  5, 67),   # unchanged: quality shortfall
    ("Sweeper 0/20",                0, 0, 0, 20, 0),    # target set, nobody tracked
    ("ICQA Peer Trainer, no target",7, 0, 0,  0, 100),  # no target, everyone ready
    ("nothing at all",              0, 0, 0,  0, None),
]
for label, t, np_, nt, target, expected in CASES:
    got = pct(t, np_, nt, target)
    check(f"{label} -> {expected}", got == expected, f"got {got}")

print("\nThe invariant behind all of them")
check("a headcount short of target can never read 100%",
      all(pct(t, 0, 0, t + 1) < 100 for t in range(1, 40)))
check("full target with everyone ready reads 100%",
      all(pct(t, 0, 0, t) == 100 for t in range(1, 40)))
check("a percentage never exceeds 100",
      all(pct(t, 0, 0, max(1, t - 5)) <= 100 for t in range(1, 40)))
check("anyone not ready still pulls it below 100",
      pct(10, 1, 0, 10) < 100 and pct(10, 0, 1, 10) < 100)

print("\nThe scorecard tile agrees with the report")
import db, application
db.set_current_site("SCN2")
with application.application.test_request_context("/"):
    overview = db.compute_ir_overview()
    rows = [(r["role"], s) for sec in overview for r in sec["roles"] for s in r["shifts"]]
    contradictions = [
        (role, s["trained"], s["total_trained"], s["target"], s["pct"])
        for role, s in rows
        if s["pct"] == 100 and s["target"] and s["total_trained"] < s["target"]
    ]
    check("no row in the live report claims 100% while short of target",
          not contradictions, str(contradictions[:3]))

    summary = db.summarize_ir_overview(overview)
    worst = [s["pct"] for _r, s in rows if s["pct"] is not None]
    if worst and summary["overall_pct"] is not None:
        check("the pooled site figure sits inside the per-row range",
              min(worst) <= summary["overall_pct"] <= max(worst),
              f"{summary['overall_pct']} vs {min(worst)}..{max(worst)}")

print("\n" + "=" * 60)
print("ALL INDIRECT ROLES PERCENTAGE CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
