"""The bulk reads must answer exactly what the per-row queries answered.

Several lookups now have two implementations: the targeted query, which
is right for a page that asks once, and a whole-table read, which is
right for a page that asks two hundred times. Which one runs depends on
how many times the request has already asked — so on any given page
either could be live, and a disagreement between them would show up as
two numbers for the same thing on the same screen.

Every check below runs the same question through both and compares.
Both paths are forced rather than waited for: the switch is a call
count, and a test that happened to stay on one side of it would prove
nothing.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import application  # noqa: F401  (imports the helpers under test)

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def both_ways(fn):
    """(targeted answer, bulk answer) for one call."""
    original = db.BULK_READ_AFTER
    try:
        db.end_request_scope()
        db.begin_request_scope()
        db.BULK_READ_AFTER = 10 ** 9      # never bulk
        targeted = fn()
        db.end_request_scope()
        db.begin_request_scope()
        db.BULK_READ_AFTER = 0            # always bulk
        bulk = fn()
        db.end_request_scope()
        return targeted, bulk
    finally:
        db.BULK_READ_AFTER = original
        db.end_request_scope()


db.set_current_site("SCN2")
db.init_db()
ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
oms = [r["login"] for r in db.get_roster_by_roles(["om"])]
soms = [r["login"] for r in db.get_roster_by_roles(["som"])]
check("the seeded site has managers to compare over", len(ams) >= 3, str(len(ams)))

print("\n1. Cross-training proficiency counts")
for scope in ([ams[0]] if ams else []), ams[:4], None, [], ["nobody_at_all"]:
    label = "everyone" if scope is None else (f"{len(scope)} manager(s)" if scope else "empty scope")
    targeted, bulk = both_ways(lambda s=scope: db.get_xt_proficiency_counts(am_login=s))
    check(f"same counts for {label}", targeted == bulk, f"{targeted} vs {bulk}")
# A single login rather than a list takes a different branch in both.
if ams:
    targeted, bulk = both_ways(lambda: db.get_xt_proficiency_counts(am_login=ams[0]))
    check("same counts for a bare login, not a list", targeted == bulk, f"{targeted} vs {bulk}")
    targeted, bulk = both_ways(lambda: db.get_xt_proficiency_counts(am_login=ams[0], fc="SCN2"))
    check("same counts narrowed to one FC", targeted == bulk, f"{targeted} vs {bulk}")
    targeted, bulk = both_ways(lambda: db.get_xt_proficiency_counts(am_login=ams[0], fc="NO_SUCH_FC"))
    check("and an FC with nothing in it is empty either way",
          targeted == bulk and not targeted, f"{targeted} vs {bulk}")
# The supervisor match is on lower(supervisor_login): the same manager
# asked for in another casing has to come back the same.
if ams:
    lower, upper = both_ways(lambda: db.get_xt_proficiency_counts(am_login=ams[0]))[1], None
    _t, upper = both_ways(lambda: db.get_xt_proficiency_counts(am_login=ams[0].upper()))
    check("case of the manager login does not change the answer", lower == upper,
          f"{lower} vs {upper}")

print("\n1b. Tracked items")
ITEM_CASES = [
    ("compliance_safety", None, None, None),
    ("compliance_safety", ams[0], None, None),
    ("compliance_safety", ams[:4], None, None),
    (["compliance_safety", "planning_de_tech"], ams[:2], None, None),
    (["compliance_safety"], None, None, "SCN2"),
    ("compliance_safety", None, None, "NO_SUCH_FC"),
    ("compliance_safety", "nobody_at_all", None, None),
    ("compliance_safety", [], None, None),
    ([], None, None, None),
    ("no_such_section", None, None, None),
]
for section, am_login, employee_login, fc in ITEM_CASES:
    targeted, bulk = both_ways(
        lambda s=section, a=am_login, e=employee_login, f=fc:
        sorted((i["id"] for i in db.get_items(section=s, am_login=a, employee_login=e, fc=f))))
    check(f"same items for section={section!r} am={am_login!r} fc={fc!r}",
          targeted == bulk, f"{len(targeted)} vs {len(bulk)}")
# Every field, not just the ids: the ranking reads status and due_date
# off these rows.
targeted, bulk = both_ways(
    lambda: sorted((tuple(sorted(i.items(), key=lambda kv: kv[0]))
                    for i in db.get_items(section="compliance_safety", am_login=ams[0]))))
check("and every column on them matches", targeted == bulk)
# One person's own record takes the employee_login branch.
somebody = next((i["employee_login"] for i in db.get_items(section="compliance_safety")
                 if i.get("employee_login")), None)
if somebody:
    targeted, bulk = both_ways(
        lambda: sorted(i["id"] for i in db.get_items(
            section=db.PERSONAL_TRAINING_SECTIONS, employee_login=somebody)))
    check("same personal record either way", targeted == bulk,
          f"{len(targeted)} vs {len(bulk)}")

# The bulk rows are shared for the rest of the request, so what comes
# back has to be a copy — several callers annotate these in place.
db.end_request_scope()
db.begin_request_scope()
original_threshold = db.BULK_READ_AFTER
db.BULK_READ_AFTER = 0
first = db.get_items(section="compliance_safety", am_login=ams[0])
if first:
    first[0]["scratch"] = "mine"
    again = db.get_items(section="compliance_safety", am_login=ams[0])
    check("one caller's annotation does not reach the next",
          "scratch" not in again[0], str(sorted(again[0].keys()))[:120])
db.BULK_READ_AFTER = original_threshold
db.end_request_scope()

print("\n2. The cross-training gap and retention number")
for login in (ams[:3] + oms[:1] + soms[:1]):
    targeted, bulk = both_ways(lambda l=login: application.get_xt_gap_and_retention_for_login(l))
    check(f"same gap and retention for {login}", targeted == bulk, f"{targeted} vs {bulk}")

print("\n3. Weekly OLR series")
for login in (ams[:3] + oms[:1]):
    targeted, bulk = both_ways(lambda l=login: db.get_olr_weekly_series(l))
    check(f"same series for {login}", targeted == bulk,
          f"{len(targeted)} weeks vs {len(bulk)} weeks")
targeted, bulk = both_ways(lambda: db.get_olr_weekly_series("nobody_at_all"))
check("somebody with no history is empty either way", targeted == bulk == {},
      f"{targeted} vs {bulk}")

print("\n4. The whole scorecard, both ways")
# The end-to-end version of the above: every ranking on the page is
# built from all of them at once.
for login in ams[:3]:
    targeted, bulk = both_ways(
        lambda l=login: application._compute_ld_metrics_full(l))
    check(f"same metrics for {login}", targeted == bulk)

print("\n5. The org tree, read whole instead of walked a level at a time")
# get_descendant_ams used to issue one statement per level of the tree.
# It now walks a map of the whole table, which has to give the same
# answer for a leaf, a middle manager and the top of a chain.
def walk_by_query(login):
    """The original level-at-a-time walk, kept here as the oracle."""
    conn = db.get_db()
    result, frontier, seen = {}, [login], {login}
    while frontier:
        placeholders = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"SELECT login, reports_to FROM user_roles WHERE reports_to IN ({placeholders})",
            frontier).fetchall()
        nxt = []
        for r in rows:
            if r["login"] in seen:
                continue
            seen.add(r["login"])
            result[r["login"]] = (r["reports_to"] == login)
            nxt.append(r["login"])
        frontier = nxt
    conn.close()
    return result


for login in (soms[:1] + oms[:2] + ams[:2] + ["nobody_at_all"]):
    db.end_request_scope()
    db.begin_request_scope()
    got = db.get_descendant_ams(login)
    db.end_request_scope()
    want = walk_by_query(login)
    check(f"same descendants for {login}", got == want,
          f"{len(got)} vs {len(want)}")
som_tree = db.get_descendant_ams(soms[0]) if soms else {}
check("a senior manager's tree really is more than one level deep",
      any(not direct for direct in som_tree.values()), str(len(som_tree)))

print("\n6. A memo key can be made from the arguments this app actually passes")
# Scopes are lists and section sets are lists, and the memo used to give
# up on both — so the busiest lookups in the app were never cached at
# all, however many times a page asked them.
check("a list argument is keyable", db._memo_key(["a", "b"]) == ("seq", ("a", "b")))
check("a nested list is too",
      db._memo_key([["a"], ("b",)]) == ("seq", (("seq", ("a",)), ("seq", ("b",)))))
check("a set is keyed in a stable order",
      db._memo_key({"b", "a"}) == db._memo_key({"a", "b"}))
check("a dict is keyed by its items",
      db._memo_key({"x": 1}) == ("map", (("x", 1),)))
check("and order is kept for a list, because ordering can be the question",
      db._memo_key(["a", "b"]) != db._memo_key(["b", "a"]))

db.end_request_scope()
db.begin_request_scope()
first = db.get_roster_by_roles(["am", "om", "som"])
second = db.get_roster_by_roles(["am", "om", "som"])
check("asking twice for the same roster is served from the memo",
      first == second)
check("but each caller gets its own rows to annotate",
      first is not second and (not first or first[0] is not second[0]))
if first:
    first[0]["scratch"] = "mine"
    third = db.get_roster_by_roles(["am", "om", "som"])
    check("so one caller's annotation does not reach the next",
          "scratch" not in third[0], str(sorted(third[0].keys())))
db.end_request_scope()

print("\n" + "=" * 62)
print("ALL BULK-READ CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
