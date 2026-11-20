"""Filters have to survive walking away from a page and coming back.

The complaint: set up the Staffing filters, go and look at something
else, come back, and every box is empty again. Links between pages carry
the tab and the manager and nothing else, so navigating threw the rest
away and a manager had to pick it all again.

What matters as much as remembering is forgetting on purpose: clearing a
filter has to stick, picking a different manager has to win over the
remembered one, and Clear-all has to actually clear. A memory that
cannot be overruled is worse than none.
"""
import os, sys
from urllib.parse import urlsplit, parse_qs
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, application

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def client():
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"
    return c


def land(c, url, hops=4):
    """Follow the restore redirects and report where we ended up."""
    seen = [url]
    for _ in range(hops):
        r = c.get(url)
        if r.status_code != 302:
            return url, parse_qs(urlsplit(url).query), r
        url = r.headers["Location"]
        if url in seen:
            raise AssertionError("redirect loop: " + " -> ".join(seen + [url]))
        seen.append(url)
    raise AssertionError("too many redirects: " + " -> ".join(seen))


db.set_current_site("SCN2")
db.init_db()
am = [r["login"] for r in db.get_roster_by_roles(["am"])][0]
other_am = [r["login"] for r in db.get_roster_by_roles(["am"])][1]
OWNS = "q,trained_for,role,working_in,clock,presence"

print("1. A page remembers what you last asked it")
c = client()
land(c, f"/overview?tab=staffing&am={am}&_fields={OWNS}&trained_for=Pick&clock=in")
land(c, "/scorecards?tab=reporting")
url, args, _r = land(c, f"/overview?tab=staffing&am={am}")
check("the filters come back", args.get("trained_for") == ["Pick"], url)
check("all of them, not just the first", args.get("clock") == ["in"], url)
check("and the manager you were looking at", args.get("am") == [am], url)

print("\n2. Each tab keeps its own")
land(c, f"/overview?tab=directory&am={am}&_fields=q,holds&q=smith")
url, args, _r = land(c, f"/overview?tab=staffing&am={am}")
check("the refresher's filters do not leak into Staffing", "q" not in args, url)
url, args, _r = land(c, f"/overview?tab=directory&am={am}")
check("and the directory still has its own", args.get("q") == ["smith"], url)

print("\n3. What you ask for now wins over what you asked before")
url, args, _r = land(c, f"/overview?tab=staffing&am={other_am}")
check("picking another manager overrides the remembered one",
      args.get("am") == [other_am], url)
check("while the filters you had still come with you",
      args.get("trained_for") == ["Pick"], url)

print("\n4. Clearing a filter sticks")
# A checkbox panel with nothing ticked submits nothing at all, which is
# why the form says which inputs it owns. Without that the merge would
# helpfully put the old selection back and unticking would do nothing.
url, args, _r = land(c, f"/overview?tab=staffing&am={am}&_fields={OWNS}&clock=in")
check("the unticked filter is gone", "trained_for" not in args, url)
check("the one still set is kept", args.get("clock") == ["in"], url)
url, args, _r = land(c, f"/overview?tab=staffing&am={am}")
check("and it stays gone on the way back", "trained_for" not in args, url)

print("\n5. Clear all clears")
land(c, f"/overview?tab=staffing&am={am}&reset=1")
url, args, _r = land(c, f"/overview?tab=staffing&am={am}")
check("nothing is restored after a reset",
      not ({"trained_for", "clock", "q"} & set(args)), url)
check("and reset is not itself remembered", "reset" not in args, url)

print("\n6. Nothing bounces")
# land() raises on a loop, so reaching here at all is the check. These
# are the shapes that could bounce: an empty page, a page whose memory
# holds exactly what the request already has, and a reset.
c2 = client()
for url in (f"/overview?tab=staffing&am={am}",
            "/overview",
            f"/overview?tab=staffing&am={am}&reset=1",
            "/scorecards",
            "/ld-management/cross-training",
            "/ld-management/indirect-roles"):
    try:
        final, _args, resp = land(c2, url)
        check(f"{url} settles", resp.status_code == 200, f"{final} -> {resp.status_code}")
    except AssertionError as e:
        check(f"{url} settles", False, str(e))

print("\n7. The memory cannot grow the session cookie past a browser's limit")
# The session is a signed cookie and a browser drops one over about 4KB,
# which would sign the person out rather than merely forget a filter.
c3 = client()
for i in range(40):
    land(c3, f"/overview?tab=staffing&am={am}&_fields={OWNS}&q=search-term-number-{i}")
    land(c3, f"/overview?tab=t{i}&am={am}&_fields=q&q=" + ("x" * 40))
with c3.session_transaction() as s:
    views = s.get("views") or {}
    budget = sum(len(k) + len(v) for k, v in views.items())
check("only a few pages are remembered",
      len(views) <= application.VIEW_MEMORY_PAGES, str(len(views)))
check("and the whole memory stays small",
      budget <= application.VIEW_MEMORY_TOTAL, f"{budget} chars")
cookie = next((c for c in c3.cookie_jar if c.name == "session"), None) if hasattr(c3, "cookie_jar") else None
if cookie is not None:
    check("so the cookie stays under the browser limit", len(cookie.value) < 3500,
          f"{len(cookie.value)} bytes")

print("\n8. A filter too long to be worth a quarter of the cookie is dropped, not kept")
c4 = client()
land(c4, f"/overview?tab=staffing&am={am}&_fields=q&q=" + ("z" * 800))
with c4.session_transaction() as s:
    check("an oversized query is not stored",
          "overview:staffing" not in (s.get("views") or {}),
          str(list((s.get("views") or {}).keys())))

print("\n9. The live refresh is not redirected")
# The Staffing tab re-renders itself by fetching its own fragment with
# the page's query string. A restore redirect there would hand the swap
# a redirect instead of markup, so the fragment reads the filters it is
# given and never restores.
c5 = client()
land(c5, f"/overview?tab=staffing&am={am}&_fields={OWNS}&trained_for=Pick")
frag = c5.get(f"/staffing/fragment?tab=staffing&am={am}")
check("the fragment answers directly", frag.status_code == 200, str(frag.status_code))
check("with the panel the page swaps in",
      'id="staffingPanel"' in frag.get_data(as_text=True))

print("\n" + "=" * 62)
print("ALL STICKY-FILTER CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
