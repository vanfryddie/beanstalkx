"""Shift Pulse, hosted in the console.

The port carries the standalone dashboard over unchanged — its element
ids are a contract with the Tampermonkey data provider — and moves only
the shared configuration from each manager's localStorage into the
site's database. These checks cover that seam: the ids survived, the
provider script is served pointing here, shared settings round-trip
through the server while personal view state stays local, and a reader
cannot write.
"""
import io, os, re, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import application, db
from werkzeug.serving import make_server

fails = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond: fails.append(name)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print("1. The data provider's contract survived the port")
body = io.open(os.path.join(ROOT, "templates", "_shift_pulse_body.html"), encoding="utf-8").read()
provider = io.open(os.path.join(ROOT, "static", "shift_pulse_provider.user.js"), encoding="utf-8").read()
page_ids = set(re.findall(r'id="([^"]+)"', body))
wanted = set(re.findall(r"getElementById\(['\"]([A-Za-z0-9_-]+)['\"]", provider))
# ids the provider creates itself rather than finding
created = set(re.findall(r"id\s*=\s*['\"]([A-Za-z0-9_-]+)['\"]", provider))
created |= set(re.findall(r'id="([A-Za-z0-9_-]+)"', provider))
# "icon-" is a template-literal prefix, not an id. "switchText" is absent
# from the standalone Shift-Pulse.html too — a leftover from the Master
# Eagle Eye tool this provider was derived from, which it null-guards —
# so requiring it would assert something the original never had either.
NOT_IN_THE_ORIGINAL_EITHER = {"icon-", "switchText"}
missing = sorted(wanted - page_ids - created - NOT_IN_THE_ORIGINAL_EITHER)
check("every element the provider writes into exists on the page", not missing, str(missing))
check("the page still defines getWarehouseId/getBucketConfig for the provider",
      "getWarehouseId" in io.open(os.path.join(ROOT, "static", "shift_pulse.js"), encoding="utf-8").read()
      and "getBucketConfig" in body + io.open(os.path.join(ROOT, "static", "shift_pulse.js"), encoding="utf-8").read())

print("\n2. The ported stylesheet cannot escape the dashboard")
css = io.open(os.path.join(ROOT, "static", "shift_pulse.css"), encoding="utf-8").read()
unscoped = [m.strip() for m in re.findall(r"(?m)^([^@/\s][^{]{0,160})\{", css) if ".sp-root" not in m]
check("every ported rule is scoped under .sp-root", not unscoped, str(unscoped[:3]))

db.set_current_site("SCN2")
db.init_db()
conn = db.get_db(); conn.execute("DELETE FROM shift_pulse_settings"); conn.commit(); conn.close()

srv = make_server("127.0.0.1", 5630, application.application, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(1.2)

print("\n3. The provider is served pointing at this console, not a local file")
application.application.config["TESTING"] = True
client = application.application.test_client()
with client.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"
served = client.get("/shift-pulse/provider.user.js").get_data(as_text=True)
# Anchor to a real header line: the banner above it mentions "@match" in
# prose, and matching that instead would pass on the wrong thing.
match = re.search(r"(?m)^// @match\s+(\S+)", served.split("==/UserScript==")[0])
check("@match points at the hosted page", bool(match) and "/shift-pulse" in match.group(1), match.group(1) if match else "none")
check("no placeholder survived templating", "__LDIS_" not in served)
check("the FCLM calls are untouched", "fclm-portal.amazon.com" in served)

print("\n4. Shared settings round-trip through the server")
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    pg.goto("http://127.0.0.1:5630/", wait_until="domcontentloaded")
    pg.fill("input[name=login_id]", "admin"); pg.click("button[type=submit]"); pg.wait_for_timeout(500)
    pg.goto("http://127.0.0.1:5630/shift-pulse", wait_until="networkidle")

    pg.evaluate("localStorage.setItem('bucketConfig_SCN2', JSON.stringify({SEV:['a','b']}))")
    pg.evaluate("localStorage.setItem('collapse_deptOverview', '1')")
    pg.wait_for_timeout(900)

    stored = db.get_shift_pulse_settings()
    check("a shared key reached the database", "bucketConfig_SCN2" in stored, str(list(stored)))
    check("personal view state did not", "collapse_deptOverview" not in stored)
    real_local = pg.evaluate("Object.keys(localStorage)")
    check("personal view state stayed in localStorage", "collapse_deptOverview" in real_local, str(real_local))
    check("the shared key is not duplicated into localStorage", "bucketConfig_SCN2" not in real_local)

    pg.reload(wait_until="networkidle")
    back = pg.evaluate("localStorage.getItem('bucketConfig_SCN2')")
    check("it reads back after a reload, from the server", back and "SEV" in back, str(back))

    pg.evaluate("localStorage.clear()")
    pg.reload(wait_until="networkidle")
    survived = pg.evaluate("localStorage.getItem('bucketConfig_SCN2')")
    check("and survives the browser's storage being cleared", survived and "SEV" in survived, str(survived))

    check("the dashboard rendered inside the console shell",
          pg.query_selector(".sp-root .dashboard-container") is not None
          and pg.query_selector("nav a[href='/shift-pulse']") is not None)
    over = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check("no horizontal page scroll", over <= 1, f"overflows {over}px")
    pg.wait_for_timeout(6500)
    check("says the provider is missing rather than showing an empty dashboard",
          pg.eval_on_selector("#spProviderLabel", "e=>e.textContent").strip() == "Data provider not detected",
          pg.eval_on_selector("#spProviderLabel", "e=>e.textContent"))
    ctx.close()
    b.close()
srv.shutdown()

print("\n5. A reader cannot change a site's shared settings")
reader = application.application.test_client()
am = [r["login"] for r in db.get_roster_by_roles(["am"])][0]
with reader.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = am, "am", "manual", "SCN2"
resp = reader.post("/shift-pulse/settings", json={"key": "bucketConfig_SCN2", "value": "{}"})
check("an Area Manager is refused", resp.status_code == 403, str(resp.status_code))
check("the stored value is unchanged", "SEV" in db.get_shift_pulse_settings().get("bucketConfig_SCN2", ""))
om = [r["login"] for r in db.get_roster_by_roles(["om"])][0]
with reader.session_transaction() as s:
    s["login"], s["role"] = om, "om"
check("an Operations Manager is allowed",
      reader.post("/shift-pulse/settings", json={"key": "bucketConfig_SCN2", "value": '{"SEV":[]}'}).status_code == 200)

print("\n6. A malformed shared value does not take the dashboard down")
# The standalone tool kept this configuration in one manager's browser, so
# a bad value broke only that manager. Shared from the database it would
# break Shift Pulse for the whole FC, which is what made the loaders
# validate. The value left in the database by the sections above — a
# bucketConfig with no `buckets` at all — is exactly that case.
stored_now = db.get_shift_pulse_settings().get("bucketConfig_SCN2", "")
check("the stored value really is malformed (no buckets key)",
      "buckets" not in stored_now, stored_now[:80])

srv = make_server("127.0.0.1", 5631, application.application, threaded=True)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(1.0)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1500, "height": 950})
    pg = ctx.new_page()
    page_errors = []
    pg.on("pageerror", lambda e: page_errors.append(str(e)))
    pg.goto("http://127.0.0.1:5631/", wait_until="domcontentloaded")
    pg.fill("input[name=login_id]", "admin"); pg.click("button[type=submit]"); pg.wait_for_timeout(500)
    pg.goto("http://127.0.0.1:5631/shift-pulse", wait_until="networkidle")
    pg.wait_for_timeout(900)
    check("no uncaught JavaScript error on the page", not page_errors, "; ".join(page_errors[:3]))
    buckets = pg.eval_on_selector_all("#bucketsContainer .bucket-card, #bucketsContainer .bucket-header",
                                      "els => els.length")
    check("the default buckets rendered instead", buckets > 0, f"{buckets} rendered")
    # And the same page served as the AM Overview tab, which runs the
    # identical script through the same shim.
    pg2 = ctx.new_page()
    tab_errors = []
    pg2.on("pageerror", lambda e: tab_errors.append(str(e)))
    pg2.goto("http://127.0.0.1:5631/overview?tab=shiftpulse", wait_until="networkidle")
    pg2.wait_for_timeout(900)
    check("nor on the AM Overview tab", not tab_errors, "; ".join(tab_errors[:3]))

    # Shift Pulse brings four position:fixed elements with it — the
    # function rollup modal and the tour's overlay, backdrop and
    # highlight. The console's entry animation translates every direct
    # child of .content, and an element with a filled transform animation
    # becomes the containing block for its fixed descendants, which put
    # all four 280px off and gave the page a horizontal scrollbar. Nothing
    # in the markup says "fixed" out loud, so this is checked rather than
    # read.
    CONTAINING_BLOCK = """(() => {
      const bad = [];
      let el = document.getElementById('tourOverlay');
      while (el) {
        const cs = getComputedStyle(el);
        const reasons = [];
        if (cs.transform && cs.transform !== 'none') reasons.push('transform ' + cs.transform);
        if (cs.filter && cs.filter !== 'none') reasons.push('filter');
        if (cs.perspective && cs.perspective !== 'none') reasons.push('perspective');
        if (cs.backdropFilter && cs.backdropFilter !== 'none') reasons.push('backdrop-filter');
        if (cs.willChange && /transform|filter|perspective/.test(cs.willChange)) reasons.push('will-change');
        if (cs.contain && /paint|layout|strict|content/.test(cs.contain)) reasons.push('contain ' + cs.contain);
        if (reasons.length) bad.push((el.id || el.tagName) + ': ' + reasons.join(', '));
        el = el.parentElement;
      }
      return bad;
    })()"""
    for label, page in (("the hosted page", pg), ("the AM Overview tab", pg2)):
        offenders = page.evaluate(CONTAINING_BLOCK)
        check(f"nothing above the fixed overlays creates a containing block on {label}",
              not offenders, "; ".join(offenders[:3]))
        over = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        check(f"no horizontal page scroll on {label}", over <= 1, f"overflows {over}px")

    # And the consequence that matters to a manager: the orange ring the
    # tour draws has to sit on the thing the tour is talking about.
    landed = pg.evaluate("""(() => {
      const h = document.getElementById('tourHighlight');
      if (!h || getComputedStyle(h).display === 'none') return 'highlight not shown';
      const r = h.getBoundingClientRect();
      const mid = document.elementFromPoint(
        Math.min(Math.max(r.left + r.width / 2, 1), window.innerWidth - 1),
        Math.min(Math.max(r.top + r.height / 2, 1), window.innerHeight - 1));
      return mid && mid.closest('.sp-root') ? 'ok' : 'points outside the dashboard';
    })()""")
    check("the tour's highlight lands inside the dashboard", landed == "ok", str(landed))
    ctx.close(); b.close()
srv.shutdown()

# Leave no corrupt shared value behind: this test deliberately writes one,
# and a dev database that keeps it makes every later page load look broken.
conn = db.get_db(); conn.execute("DELETE FROM shift_pulse_settings"); conn.commit(); conn.close()
check("the test cleans up the settings it wrote", not db.get_shift_pulse_settings())

print("\n" + "=" * 62)
print("ALL SHIFT PULSE CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
