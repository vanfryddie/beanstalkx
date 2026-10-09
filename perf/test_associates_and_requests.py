"""My Associates, training requests, and Shift Pulse as a tab.

The app could only ever surface somebody who was already late. These
cover the other direction: seeing a whole team, and asking for training
nobody is overdue on.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import application, db

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: fails.append(name)


def client(login, role):
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = login, role, "manual", "SCN2"
    return c


application.application.config["TESTING"] = True
db.set_current_site("SCN2")
db.init_db()
conn = db.get_db()
conn.execute("DELETE FROM training_requests"); conn.execute("DELETE FROM training_request_nominees")
conn.commit(); conn.close()

ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
am, other_am = ams[0], ams[1]

print("1. The directory shows a manager's own team, built from every source")
mine = db.get_associate_directory(am_logins=[am])
check("the team is not empty", len(mine) > 0, str(len(mine)))
check("everyone has a login and a name",
      all(p["employee_login"] and p["full_name"] for p in mine))
check("compliance is summarised per person",
      any(p["compliance_pct"] is not None for p in mine))
check("indirect roles are attached", any(p["roles"] for p in mine))
check("cross-training processes are attached", any(p["processes"] for p in mine))
check("a person with no compliance record still appears if another source has them",
      all(p["tracked_items"] >= 0 for p in mine))

theirs = {p["employee_login"].lower() for p in db.get_associate_directory(am_logins=[other_am])}
overlap = {p["employee_login"].lower() for p in mine} & theirs
check("two managers' teams do not overlap", not overlap, str(sorted(overlap)[:4]))

print("\n2. An Area Manager can ask for training nobody is overdue on")
nominees = [p["employee_login"] for p in mine[:2]]
am_client = client(am, "am")
resp = am_client.post("/training-requests", data={
    "topic": "Powered Industrial Truck (PIT)", "nominee": nominees,
    "reason": "Two new starters for the AFE desk", "needed_by": "2026-11-20",
}, follow_redirects=True)
check("the request is accepted", resp.status_code == 200)
open_requests = db.get_training_requests(status="open")
check("it is recorded as open", len(open_requests) == 1, str(len(open_requests)))
if open_requests:
    req = open_requests[0]
    check("with the topic as asked", req["topic"] == "Powered Industrial Truck (PIT)")
    check("with both nominees", len(req["nominees"]) == 2, str(req["nominees"]))
    check("attributed to the manager who asked", req["requested_by"] == am)

print("\n3. A manager cannot nominate somebody else's associate")
stranger = next(iter(theirs))
before = len(db.get_training_requests())
am_client.post("/training-requests", data={"topic": "Cross-team attempt", "nominee": [stranger]},
               follow_redirects=True)
after = db.get_training_requests()
check("no request is created", len(after) == before, f"{before} -> {len(after)}")
check("and the topic never reaches the table",
      not any(r["topic"] == "Cross-team attempt" for r in after))

print("\n4. Only L&D decides")
request_id = open_requests[0]["id"]
denied = am_client.post(f"/training-requests/{request_id}/decide",
                        data={"status": "scheduled"}, follow_redirects=True)
check("an Area Manager cannot schedule their own request",
      db.get_training_requests()[0]["status"] == "open",
      db.get_training_requests()[0]["status"])

trainer = client("admin", "admin")
trainer.post(f"/training-requests/{request_id}/decide",
             data={"status": "declined", "note": "Covered by next month's intake"},
             follow_redirects=True)
decided = [r for r in db.get_training_requests() if r["id"] == request_id][0]
check("L&D can decline it", decided["status"] == "declined", decided["status"])
check("and the reason is kept for the requester",
      decided["decision_note"] == "Covered by next month's intake", str(decided["decision_note"]))

print("\n5. The requester sees the outcome on their own page")
body = am_client.get(f"/overview?tab=directory&am={am}").get_data(as_text=True)
check("their request is listed", "Powered Industrial Truck (PIT)" in body)
check("with the decision shown", "Declined" in body)
check("and the reason", "Covered by next month&#39;s intake" in body or "Covered by next month's intake" in body)

print("\n5b. The summary at the top counts the same thing the cards do")
# A header that says "11 with open items" above nineteen cards each
# reading "1 open item" is the kind of thing a manager notices and then
# stops trusting the page over. The two counts are different questions —
# overdue, versus overdue or coming due — so both are named.
page_body = am_client.get(f"/overview?tab=directory&am={am}").get_data(as_text=True)
cards_with_items = len(re.findall(r"<summary>\s*\d+ open item", page_body))
people = db.get_associate_directory(am_logins=[am])
expect_open = len([p for p in people if p["open_items"]])
expect_gap = len([p for p in people if p["totals"]["gap"]])
check("cards showing an open item match the people who have one",
      cards_with_items == expect_open, f"{cards_with_items} cards vs {expect_open} people")


def stat(label):
    m = re.search(r">" + re.escape(label) + r"</span>\s*<strong[^>]*>(\d+)", page_body)
    return int(m.group(1)) if m else None


check("the 'With open items' figure is that same number",
      stat("With open items") == expect_open,
      f"header says {stat('With open items')}, {expect_open} people have one")
check("'Not compliant' counts only the overdue",
      stat("Not compliant") == expect_gap,
      f"header says {stat('Not compliant')}, {expect_gap} are overdue")
check("the two are genuinely different here, so the check means something",
      expect_open != expect_gap, f"both {expect_open}")

print("\n6. Shift Pulse is reachable as a tab, sharing the page's markup")
tab = client("admin", "admin").get("/overview?tab=shiftpulse").get_data(as_text=True)
page = client("admin", "admin").get("/shift-pulse").get_data(as_text=True)
check("the tab renders the dashboard", 'class="sp-root"' in tab)
tab_ids = set(re.findall(r'id="([^"]+)"', tab))
page_ids = set(re.findall(r'id="([^"]+)"', page))
missing = sorted(page_ids - tab_ids - {"spProviderHelp"})
check("the tab carries the same element ids as the page", not missing, str(missing[:6]))
check("the dashboard stylesheet loads on the tab", "shift_pulse.css" in tab)
check("and its script", "shift_pulse.js" in tab)

provider = client("admin", "admin").get("/shift-pulse/provider.user.js").get_data(as_text=True)
meta = provider.split("// ==UserScript==")[1].split("// ==/UserScript==")[0]
matches = re.findall(r"(?m)^// @match\s+(\S+)", meta)


def covers(url):
    """Whether any @match would fire on this URL, by the extension's own
    rules: scheme and host exact, path globbed, query ignored. Checked
    this way rather than by searching the patterns for 'tab=shiftpulse',
    because a query string is not part of a match pattern at all — a
    pattern that contained one looked right in a substring test and
    never ran in a browser."""
    parts = re.match(r"^(https?)://([^/?#]+)([^?#]*)", url)
    u_scheme, u_host, u_path = parts.groups()
    for pattern in matches:
        m = re.match(r"^(\*|https?)://(\*|[^/*]+)(/[^?#]*)$", pattern)
        if not m:
            continue
        scheme, host, path = m.groups()
        if scheme not in ("*", u_scheme) or host not in ("*", u_host):
            continue
        if re.match("^" + ".*".join(re.escape(p) for p in path.split("*")) + "$", u_path or "/"):
            return True
    return False


check("the provider covers the standalone page",
      covers("http://localhost/shift-pulse"), str(matches))
check("and the AM Overview tab",
      covers("http://localhost/overview?tab=shiftpulse&am=am000000"), str(matches))

print("\n" + "=" * 62)
print("ALL ASSOCIATE / REQUEST CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
