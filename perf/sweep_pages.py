"""Open every page at two widths in both themes and report what breaks.

A passing test suite says the server produced the right HTML. It says
nothing about whether the page is usable: a table with no scroll
container carries the whole page sideways on a phone, and a JavaScript
error leaves a control dead while the response is still a clean 200.
Both have shipped here. This is the pass that catches them.

What it reports, per page/theme/width:
  - a response that is not 200
  - the page scrolling sideways
  - an element wider than its box with nothing able to scroll it
  - console or page errors

It deliberately does not report an element inside a scroll container, or
an absolutely-positioned decoration whose marks sit on the edge of their
plot by design — both are correct, and flagging them trains you to
ignore the output.

    python3 perf/sweep_pages.py            # every page
    python3 perf/sweep_pages.py cross      # only paths matching "cross"
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.serving import make_server

import application
import browser
from playwright.sync_api import sync_playwright

PORT = 5171
BASE = f"http://127.0.0.1:{PORT}"

# Every page a manager or admin can open. POST-only routes are not here
# (/ops-structure and /shift-pulse/settings among them) — a GET at one
# is a 405 and says nothing about the page.
PAGES = [
    "/overview", "/scorecards", "/scorecards?tab=manager_rankings",
    "/data", "/weekly-plan",
    "/shift-pulse", "/escalations", "/trainer-metrics",
    "/ld-management",
    "/ld-management/cross-training",
    "/ld-management/cross-training?tab=directory",
    "/ld-management/cross-training?tab=retention",
    "/ld-management/cross-training?tab=exclusions",
    "/ld-management/cross-training?tab=insights",
    "/ld-management/cross-training?tab=model",
    "/ld-management/cross-training?tab=definitions",
    "/ld-management/exclusions", "/ld-management/indirect-roles",
    "/ld-management/olr", "/ld-management/ambassadors/definitions",
    "/ld-management/ambassador-meetings", "/ld-management/trainer-overview",
    "/ld-management/settings",
    "/ld-management/settings?tab=shifts",
    "/ld-management/settings?tab=data",
    "/ld-management/settings?tab=users",
    "/regional-overview",
]

# Not pages: /floor/roster, /floor/config and /floor/status return JSON
# for the browser extension. A browser shown raw JSON asks for a bare
# /favicon.ico, because the response declares no icon, and the 404 that
# comes back looks like a broken page until you work out what it is.

WIDTHS = [("phone", 430, 1000), ("wide", 1500, 1000)]

# An element is only a problem when nothing between it and the page can
# scroll it. A table inside a scroll container is meant to be wide.
OVERFLOW_PROBE = """() => {
    const out = [];
    document.querySelectorAll('main *').forEach(el => {
        if (el.scrollWidth <= el.clientWidth + 2) return;
        const cs = getComputedStyle(el);
        if (cs.position === 'absolute') return;      // a decoration layer
        if (cs.overflowX !== 'visible') return;      // it scrolls itself
        out.push((el.className || el.tagName).toString().slice(0, 44)
                 + ' ' + el.scrollWidth + '>' + el.clientWidth);
    });
    return [...new Set(out)].slice(0, 3);
}"""


def main():
    needle = sys.argv[1] if len(sys.argv) > 1 else None
    pages = [p for p in PAGES if not needle or needle in p]
    if not pages:
        sys.exit(f"no page matches {needle!r}")

    srv = make_server("127.0.0.1", PORT, application.application, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    problems, loads = [], 0
    with sync_playwright() as pw:
        bro = pw.chromium.launch(executable_path=browser.chromium_path(),
                                 args=["--no-sandbox"])
        for theme in ("dark", "light"):
            for wname, width, height in WIDTHS:
                ctx = bro.new_context(viewport={"width": width, "height": height},
                                      color_scheme=theme)
                page = ctx.new_page()
                # Console errors can arrive after the next navigation has
                # started, so each one is tagged with the page that was
                # open when it fired. Without that, a late error from one
                # page is reported against the page after it, and you go
                # looking for a fault that is not there.
                errors = []
                page.on("console", lambda m: errors.append((page.url, m.text))
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append((page.url, str(e))))
                # "Failed to load resource" on its own sends you hunting;
                # the URL that failed is the whole finding.
                page.on("response", lambda r: errors.append(
                    (page.url, f"{r.status} on {r.url}")) if r.status >= 400 else None)

                page.goto(BASE + "/")
                page.fill("input[name=login_id]", "admin")
                page.select_option("select[name=role]", "admin")
                page.click("button[type=submit]")
                page.wait_for_load_state("networkidle")

                for url in pages:
                    errors.clear()
                    loads += 1
                    where = f"{url} {theme} {wname}"
                    expect = BASE + url
                    try:
                        response = page.goto(BASE + url, wait_until="networkidle")
                    except Exception as e:
                        problems.append(f"{where}: {type(e).__name__}")
                        continue
                    if response.status >= 400:
                        problems.append(f"{where}: HTTP {response.status}")
                        continue
                    # The toggle sets this attribute; emulation alone does
                    # not, and the two must agree or the shot is a hybrid.
                    page.evaluate(
                        "t => document.documentElement.setAttribute('data-theme', t)", theme)
                    page.wait_for_timeout(120)

                    scroll = page.evaluate(
                        "() => ({x: document.documentElement.scrollWidth,"
                        " w: document.documentElement.clientWidth})")
                    if scroll["x"] > scroll["w"] + 1:
                        problems.append(f"{where}: page scrolls sideways "
                                        f"{scroll['x']}>{scroll['w']}")
                    stuck = page.evaluate(OVERFLOW_PROBE)
                    if stuck:
                        problems.append(f"{where}: unscrollable {stuck}")
                    mine = [text for at, text in errors if at == expect]
                    if mine:
                        problems.append(f"{where}: console {mine[:2]}")
                ctx.close()
        bro.close()
    srv.shutdown()

    print(f"{loads} page loads")
    print(json.dumps(problems, indent=2) if problems else "every page clean")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
