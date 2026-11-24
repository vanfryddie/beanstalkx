"""An upload is a snapshot of a population, not a list of changes.

The bug this covers: the compliance exports were upserted and nothing
was ever removed, so somebody who left kept their records at whatever
status they had on their last day, forever — and their supervisor kept
a Safety score built from records nobody could still affect, identical
after every update. The role assignment behind that supervisor never
cleared either, so they stayed on the ranking even once the records
were gone.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, application

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


SAFETY_HEADER = ("Emp Login,Full Name,FC,Supervisor Login,Detailed Topic,"
                 "Compliant Yes/No,Expiry Date In Use\n")


def safety(rows, name="safety.csv"):
    return db.ingest_safety_compliance_csv((SAFETY_HEADER + "".join(rows)).encode(), name)


def row(emp, sup, topic="Fire Safety", compliant="No", fc="SCN2", due="2026-01-01"):
    return f"{emp},{emp.upper()},{fc},{sup},{topic},{compliant},{due}\n"


def items(**kw):
    db.end_request_scope(); db.begin_request_scope()
    try:
        return db.get_items(section="compliance_safety", **kw)
    finally:
        db.end_request_scope()


def ranking():
    db.end_request_scope(); db.begin_request_scope()
    try:
        with application.application.test_request_context("/"):
            return application.get_safety_training_compliance_ranking()
    finally:
        db.end_request_scope()


def wipe(*logins):
    conn = db.get_db()
    conn.execute("DELETE FROM tracked_items WHERE section='compliance_safety'")
    conn.execute("DELETE FROM tracked_items WHERE section='planning_bts'")
    if logins:
        marks = ",".join("?" * len(logins))
        conn.execute(f"DELETE FROM user_roles WHERE login IN ({marks})", logins)
    conn.commit(); conn.close()


def roles(*pairs):
    conn = db.get_db()
    for login, name in pairs:
        conn.execute("INSERT INTO user_roles (login, role, full_name, assigned_by, assigned_at) "
                     "VALUES (?,?,?,?,?) ON CONFLICT(login) DO UPDATE SET role=excluded.role",
                     (login, "am", name, "test", db._now()))
    conn.commit(); conn.close()


# On a scratch site of its own. This suite uploads, prunes and wipes
# whole sections, and doing that to the seeded site would fail the
# suites that run after it for reasons that are nothing to do with the
# code. A site is a separate database (a file on SQLite, a schema on
# Postgres), so nothing here can reach the real one.
SITE = "ZQ97"
# Registered, not just switched to: a request resolves its site from the
# registry and falls back to the default for anything it does not find
# there, so an unregistered scratch site would send section 11's upload
# straight at the real one — which it did, and pruned 900 seeded rows
# before this line existed.
db.init_global_db()
if not db.get_site(SITE):
    db.create_site(SITE, "Stale-record scratch", "EU", "test")
db.set_current_site(SITE)
db.init_db()
NAMES = ("tsr_leaver", "tsr_stayer", "tsr_new")
wipe(*NAMES)
roles(("tsr_leaver", "Leaver, Len"), ("tsr_stayer", "Stayer, Sam"))

print("1. The first export")
r = safety([row("tsr_a1", "tsr_leaver"), row("tsr_a2", "tsr_leaver"),
            row("tsr_b1", "tsr_stayer", compliant="Yes", due="2027-01-01")])
check("it reports what it loaded", r["loaded"] == 3, str(r))
check("and removes nothing on a first load", r["removed"] == 0, str(r))
check("the records are there", len(items()) == 3, str(len(items())))

print("\n2. The next export no longer contains one manager's team")
r = safety([row("tsr_b1", "tsr_stayer", compliant="Yes", due="2027-01-01"),
            row("tsr_b2", "tsr_stayer")])
check("the records it no longer contains are removed", r["removed"] == 2, str(r))
check("and only those", len(items()) == 2, str([i["employee_login"] for i in items()]))
left = [i for i in items() if i["employee_login"] in ("tsr_a1", "tsr_a2")]
check("the people who left are gone", not left, str(left))

print("\n3. Which is the bug: a score that never moved")
rows_by_login = {r["login"]: r for r in ranking()["rows"]}
check("the departed manager is not scored from records nobody can affect",
      "tsr_leaver" not in rows_by_login or rows_by_login["tsr_leaver"]["record_count"] == 0,
      str(rows_by_login.get("tsr_leaver")))
check("and the manager still here is scored from the current file",
      rows_by_login["tsr_stayer"]["record_count"] == 2,
      str(rows_by_login["tsr_stayer"]))

print("\n4. Running the same file again changes nothing")
r = safety([row("tsr_b1", "tsr_stayer", compliant="Yes", due="2027-01-01"),
            row("tsr_b2", "tsr_stayer")])
check("nothing is removed the second time", r["removed"] == 0, str(r))
check("and nothing is lost", len(items()) == 2, str(len(items())))

print("\n5. A file for one building does not empty another")
safety([row("tsr_c1", "tsr_stayer", fc="OTH1")], "other.csv")
check("the other building's rows are added", len(items()) == 3, str(len(items())))
r = safety([row("tsr_b1", "tsr_stayer", compliant="Yes", due="2027-01-01")], "scn2.csv")
check("an SCN2 file prunes SCN2", r["removed"] == 1, str(r))
remaining = {i["employee_login"] for i in items()}
check("and leaves OTH1 alone", "tsr_c1" in remaining, str(remaining))

print("\n6. A row somebody typed in is not an upload's to delete")
db.add_tracked_item(section="compliance_safety", employee_login="tsr_manual",
                    full_name="Manual Entry", fc="SCN2", am_login="tsr_stayer",
                    subcategory="Fire Safety", status="Overdue",
                    due_date="2026-01-01", value=None, notes="added by hand")
before = {i["employee_login"] for i in items()}
check("it is there", "tsr_manual" in before)
safety([row("tsr_b1", "tsr_stayer", compliant="Yes", due="2027-01-01")], "scn2b.csv")
after = {i["employee_login"] for i in items()}
check("and the next upload leaves it alone", "tsr_manual" in after, str(after))

print("\n7. The stale role assignment behind it")
rank = ranking()
stale = {s["login"] for s in rank["stale_assignments"]}
listed = {r["login"] for r in rank["rows"]}
check("a manager the data no longer mentions is off the ranking",
      "tsr_leaver" not in listed, str("tsr_leaver" in listed))
check("but is named as an assignment to revoke", "tsr_leaver" in stale, str(stale))
check("the manager still in the data is ranked", "tsr_stayer" in listed)
check("and is not called stale", "tsr_stayer" not in stale)
check("the headline count no longer includes the leaver",
      rank["manager_count"] == len(rank["rows"]),
      f"{rank['manager_count']} vs {len(rank['rows'])}")

print("\n8. A manager who is in the data but has no safety records is NOT stale")
# The distinction the page turns on: no records is a gap worth seeing,
# no presence at all is somebody who has gone.
roles(("tsr_new", "New, Nina"))
conn = db.get_db()
conn.execute("INSERT INTO tracked_items (section, employee_login, full_name, fc, am_login, "
             "subcategory, status, updated_at, source) VALUES (?,?,?,?,?,?,?,?,'upload')",
             ("planning_bts", "tsr_n1", "N One", "SCN2", "tsr_new", "Course", "Compliant", db._now()))
conn.commit(); conn.close()
rank = ranking()
check("they are ranked, not written off",
      "tsr_new" in {r["login"] for r in rank["rows"]},
      str([r["login"] for r in rank["rows"]][:5]))
check("with no score rather than a wrong one",
      next(r["score"] for r in rank["rows"] if r["login"] == "tsr_new") is None)
check("and are not in the revoke list",
      "tsr_new" not in {s["login"] for s in rank["stale_assignments"]})

print("\n9. With no data at all, nobody is declared departed")
# On its own empty site rather than by emptying this one: a suite that
# deletes the data the suites after it compare against fails them for
# reasons that have nothing to do with the code.
db.set_current_site("ZQ98")
db.init_db()
roles(("tsr_leaver", "Leaver, Len"), ("tsr_stayer", "Stayer, Sam"))
rank = ranking()
check("an empty site ranks everyone it knows about",
      not rank["stale_assignments"], str(rank["stale_assignments"]))
check("rather than ranking nobody", len(rank["rows"]) >= 2, str(len(rank["rows"])))
wipe(*NAMES)
db.set_current_site(SITE)

print("\n10. The other rankings got the same treatment")
db.end_request_scope(); db.begin_request_scope()
with application.application.test_request_context("/"):
    metric = application.get_ld_metric_ranking("total")
db.end_request_scope()
check("they report stale assignments too", "stale_assignments" in metric, str(sorted(metric)))

print("\n11. What the person uploading is told")
# Rows leaving on their own is the one thing in this app that removes
# data without anybody asking, so the upload says so.
import io, re
application.application.config["TESTING"] = True
client = application.application.test_client()
with client.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", SITE
db.set_current_site(SITE)


def upload(body, name):
    r = client.post("/data", data={
        "section": "compliance_safety", "mode": "csv",
        "file": (io.BytesIO((SAFETY_HEADER + body).encode()), name)},
        content_type="multipart/form-data", follow_redirects=True)
    return " ".join(m.strip() for m in re.findall(r"Imported[^<]*", r.get_data(as_text=True)))


# From empty, so "first upload" means what it says: the sections above
# left rows behind, and removing those would be correct but not what is
# being checked here.
conn = db.get_db()
conn.execute("DELETE FROM tracked_items WHERE section='compliance_safety'")
conn.commit(); conn.close()
first = upload(row("tsr_u1", "tsr_stayer") + row("tsr_u2", "tsr_stayer"), "u1.csv")
check("a first upload just reports what it loaded",
      "Imported 2 rows" in first and "Removed" not in first, first)
second = upload(row("tsr_u1", "tsr_stayer"), "u2.csv")
check("and one that drops somebody says what it removed",
      "Removed 1 record" in second, second)
check("in words that say why rather than just a number",
      "no longer contains" in second, second)

print("\n12. A file that would empty the section is treated as the wrong file")
# The cost of this being wrong is not symmetric: a prune that should
# not have happened cannot be undone, and an upload that did not prune
# costs one more cycle and a message.
conn = db.get_db()
conn.execute("DELETE FROM tracked_items WHERE section='compliance_safety'")
conn.commit(); conn.close()
full = "".join(row(f"tsr_p{i:03d}", "tsr_stayer") for i in range(100))
upload(full, "full.csv")
check("a full export loads", len(items()) == 100, str(len(items())))

partial = upload(row("tsr_p000", "tsr_stayer"), "partial.csv")
check("a file naming one of a hundred people removes nothing",
      len(items()) == 100, str(len(items())))
check("and says why rather than doing it quietly",
      "nothing was removed" in partial and "partial export" in partial, partial)
check("while still loading what it contained", "Imported 1 rows" in partial, partial)

normal = upload("".join(row(f"tsr_p{i:03d}", "tsr_stayer") for i in range(90)), "normal.csv")
check("an ordinary week's turnover still prunes",
      len(items()) == 90, str(len(items())))
check("and reports it", "Removed 10 record" in normal, normal)

print("\n13. The limit does not fire on a handful of records")
conn = db.get_db()
conn.execute("DELETE FROM tracked_items WHERE section='compliance_safety'")
conn.commit(); conn.close()
upload(row("tsr_s1", "tsr_stayer") + row("tsr_s2", "tsr_stayer") + row("tsr_s3", "tsr_stayer"),
       "small.csv")
small = upload(row("tsr_s1", "tsr_stayer"), "small2.csv")
check("two of three going is not evidence of a bad file",
      len(items()) == 1, str([i["employee_login"] for i in items()]))
check("so they are removed", "Removed 2 record" in small, small)

wipe(*NAMES)
print("\n" + "=" * 62)
print("ALL STALE-RECORD CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
