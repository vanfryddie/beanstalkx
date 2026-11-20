"""The adhoc refresher planner: who is due a safety refresher, now.

Three things here were wrong in ways that only show up with data on the
page, which is why they are tested with data on the page rather than by
reading the view function's return value.
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


application.application.config["TESTING"] = True
db.set_current_site("SCN2")
db.init_db()

ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
am = ams[0]

# The planner only reads safety rows the latest import refreshed, so a
# matching uploads row is part of the fixture, not an optional extra.
stamp = db._now()
soon = (date.today() + timedelta(days=10)).isoformat()
conn = db.get_db()
# Put the rows back as they were on the way out: this is the shared
# development database, not a scratch one, and a test that leaves every
# safety record due in ten days changes what the next test sees.
before = [(r["id"], r["updated_at"], r["due_date"]) for r in conn.execute(
    "SELECT id, updated_at, due_date FROM tracked_items WHERE section='compliance_safety'").fetchall()]
uploads_before = [dict(r) for r in conn.execute(
    "SELECT filename, uploaded_at, row_count FROM uploads WHERE section='compliance_safety'").fetchall()]
conn.execute("DELETE FROM uploads WHERE section='compliance_safety'")
conn.execute("UPDATE tracked_items SET updated_at=?, due_date=? WHERE section='compliance_safety'",
             (stamp, soon))
conn.execute("INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
             ("safety.csv", "compliance_safety", stamp, 100))
conn.commit()
conn.close()


def restore():
    conn = db.get_db()
    cur = conn.cursor()
    for row_id, updated_at, due_date in before:
        cur.execute("UPDATE tracked_items SET updated_at=?, due_date=? WHERE id=?",
                    (updated_at, due_date, row_id))
    cur.execute("DELETE FROM uploads WHERE section='compliance_safety'")
    for row in uploads_before:
        cur.execute("INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
                    (row["filename"], "compliance_safety", row["uploaded_at"], row["row_count"]))
    conn.commit()
    conn.close()

team = db.get_associate_directory(am_logins=[am])
db.record_floor_clock([{"employee_login": p["employee_login"], "employee_id": f"91{i:04d}",
                        "clock_state": "in", "clocked_in_at": "2026-11-20T06:00:00"}
                       for i, p in enumerate(team)], checked_by=am)
shift = next((p.get("shift") for p in team if p.get("shift")), "early")
until = (date.today() + timedelta(days=30)).isoformat()

def fresh():
    """A client with no remembered filters.

    Filters are sticky now, deliberately — so a test that wants to know
    what an unfiltered page looks like has to ask for one rather than
    assume it, exactly as a person would have to click Clear all."""
    client = application.application.test_client()
    with client.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"
    return client


c = fresh()
BASE = f"/overview?tab=adhoc_refresher&am={am}&shift={shift}&until={until}"


def page(url, client=None):
    return (client or c).get(url, follow_redirects=True).get_data(as_text=True)


def rows(url, client=None):
    return page(url, client).count("data-ar-filter-0=")


body = page(BASE)
everyone = rows(BASE, fresh())
check("the planner has people on it to test with", everyone > 3, str(everyone))
topics = sorted({t for t in ("Fire Safety", "Lockout Tagout", "First Aid", "FSRI")
                 if t in body})
check("and more than one safety topic among them", len(topics) >= 2, str(topics))

print("\n1. The planner's own search box searches what it says it does")
# It offers "Search name, login or topic". The topic half did nothing:
# the same `q` was handed to the Staffing view first, which searches
# names, logins and stations and knows nothing about safety topics, so
# it emptied the page before the planner ever looked at it.
for topic in topics[:2]:
    word = topic.split()[0]
    found = rows(BASE + "&_fields=q&q=" + word, fresh())
    check(f"searching {word!r} finds its records", found > 0, f"{found} rows")
    check(f"and searching {word!r} narrows rather than showing everything",
          0 < found < everyone, f"{found} of {everyone}")
by_login = rows(BASE + "&_fields=q&q=" + team[0]["employee_login"], fresh())
check("searching a login still works", 0 < by_login <= everyone, f"{by_login} rows")
check("a search matching nothing is empty, not everything",
      rows(BASE + "&_fields=q&q=zzzznotathing", fresh()) == 0)

print("\n2. The checkbox filters can actually be opened")
# They are plain markup until the filter script binds them. Without it
# "Trained for" and "Indirect role" rendered and did nothing at all.
check("the panels are on the page", 'data-xf' in body)
check("and so is the script that opens them", "ldisBindFilters" in body)
check("the staffing tab still has its copy",
      "ldisBindFilters" in page(f"/overview?tab=staffing&am={am}"))

print("\n3. It opens on the shift the team is actually on")
# Defaulting to Early meant a Late-shift manager's planner opened empty
# and they had to work out why. The team's own shift is used rather than
# the manager's profile, because where the two disagree it is the team's
# records the page is about.
def opens_on(markup, wanted):
    marker = f'data-ar-shift="{wanted}"'
    i = markup.find(marker)
    return i > 0 and 'class="active"' in markup[i:i + 120]


team_shift = (shift or "early")
opened = page(f"/overview?tab=adhoc_refresher&am={am}&until={until}", fresh())
check(f"it opens on the team's own shift ({team_shift})", opens_on(opened, team_shift))
check("with the team on it, not an empty page",
      opened.count("data-ar-filter-0=") > 0, str(opened.count("data-ar-filter-0=")))

# The manager's own profile only decides it when nothing records the
# team's shift at all.
original_shift = db.get_user_shift_and_department(am)[0]
for wanted in ("late", "night", "early"):
    conn = db.get_db()
    conn.execute("UPDATE user_roles SET shift=? WHERE login=?", (wanted, am))
    conn.commit()
    conn.close()
    check(f"a profile saying {wanted} does not move it off the team's shift",
          opens_on(page(f"/overview?tab=adhoc_refresher&am={am}&until={until}", fresh()),
                   team_shift), wanted)
check("and the manager's own shift is the fallback when the team has none",
      application._refresher_default_shift({"people": []}, "late") == "late")
check("with Early the last resort",
      application._refresher_default_shift({"people": []}, None) == "early")
check("the team's shift wins over the profile's",
      application._refresher_default_shift(
          {"people": [{"shift": "night"}, {"shift": "night"}, {"shift": "late"}]},
          "early") == "night")

other = next(k for k in ("early", "late", "night") if k != team_shift)
check("an explicit shift in the link still wins",
      opens_on(page(f"/overview?tab=adhoc_refresher&am={am}&shift={other}&until={until}", fresh()),
               other), other)
check("and nonsense in the link falls back rather than failing",
      fresh().get(f"/overview?tab=adhoc_refresher&am={am}&shift=sideways",
                  follow_redirects=True).status_code == 200)

# A shift the manager picked themselves is theirs until they clear it —
# the default only applies before they have said anything.
sticky = fresh()
page(f"/overview?tab=adhoc_refresher&am={am}&shift={other}&until={until}", sticky)
check("a shift you picked is still there when you come back",
      opens_on(page(f"/overview?tab=adhoc_refresher&am={am}", sticky), other), other)

conn = db.get_db()
conn.execute("UPDATE user_roles SET shift=? WHERE login=?", (original_shift, am))
conn.commit()
conn.close()

restore()
after = db.get_items(section="compliance_safety")
check("the fixture left the safety records as it found them",
      len({(i["id"], i["due_date"]) for i in after}
          & {(row_id, due) for row_id, _u, due in before}) == len(before),
      f"{len(before)} rows")

print("\n4. The empty state does not break its own page")
# The fallback for "no group selected" was the literal string '""',
# which Jinja escaped into the markup as &#34;&#34; — a syntax error
# that killed the whole inline script, and with it the copy buttons,
# the column filters and the group links, on exactly the page where
# there is nothing to plan.
empty = page(f"/overview?tab=adhoc_refresher&am={am}&until=2000-01-01", fresh())
check("there is genuinely nothing to plan", "ar-no-selection" in empty)
check("and the script is still valid JavaScript",
      "var groupLogins = &#34;" not in empty and 'var groupLogins = "";' in empty,
      empty[empty.find("var groupLogins"):][:60])

print("\n" + "=" * 62)
print("ALL REFRESHER CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
