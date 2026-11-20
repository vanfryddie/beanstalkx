"""Static assets: one URL per version of a file, cached hard.

Pages were shipping a quarter-megabyte stylesheet with a hand-typed
?v= that had to be remembered and bumped on every change — and two
scripts carried no version at all, so a browser holding an old copy had
no reason to ask for a new one. The URL now carries a hash of the file,
which is what makes it safe to tell browsers to keep it for a year.
"""
import os, re, sys, glob
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import application

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name)


application.application.config["TESTING"] = True
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"

print("1. Every static URL names a version")
# Without this the year-long cache below would be a trap rather than a
# saving: a browser would hold a stale file and never ask again.
hand_written = []
for path in glob.glob(os.path.join(ROOT, "templates", "*.html")):
    for line in open(path, encoding="utf-8"):
        if "url_for('static'" in line or 'url_for("static"' in line:
            hand_written.append(os.path.basename(path) + ": " + line.strip()[:80])
check("no template builds a static URL by hand any more", not hand_written,
      "; ".join(hand_written[:3]))

seen = set()
for url in ("/", "/overview", "/scorecards", "/ld-management/cross-training",
            "/overview?tab=shiftpulse"):
    body = c.get(url, follow_redirects=True).get_data(as_text=True)
    seen |= set(re.findall(r'/static/[^"\'\s>]+', body))
check("the pages do reference static files", len(seen) >= 5, str(len(seen)))
unversioned = sorted(u for u in seen if "?v=" not in u)
check("and every one of them is versioned", not unversioned, str(unversioned))

print("\n2. The version is the file, so a changed file is a changed URL")
first = application.asset_version("style.css")
check("a hash is produced", first and first != "0", str(first))
check("and it is stable while the file is", first == application.asset_version("style.css"))
check("two different files get different versions",
      application.asset_version("style.css") != application.asset_version("dragdrop.js"))
check("a file that is not there does not break the page",
      application.asset_version("no_such_file.css") == "0")

changed = os.path.join(ROOT, "static", "__asset_probe.css")
with open(changed, "w", encoding="utf-8") as handle:
    handle.write("/* one */\n")
before = application.asset_version("__asset_probe.css")
os.utime(changed, (0, 0))
with open(changed, "w", encoding="utf-8") as handle:
    handle.write("/* two, which is different */\n")
after = application.asset_version("__asset_probe.css")
os.remove(changed)
check("editing a file changes its version", before != after, f"{before} -> {after}")

print("\n3. Which is what makes a year-long cache safe")
asset = sorted(seen)[0]
r = c.get(asset)
check("a static file is served", r.status_code == 200, str(r.status_code))
check("and told to be kept",
      "max-age=31536000" in (r.headers.get("Cache-Control") or ""),
      str(r.headers.get("Cache-Control")))
page = c.get("/overview", follow_redirects=True)
check("while a page is not",
      "max-age=31536000" not in (page.headers.get("Cache-Control") or ""),
      str(page.headers.get("Cache-Control")))

print("\n4. The edge compresses what it sends")
conf = os.path.join(ROOT, ".platform", "nginx", "conf.d", "compression.conf")
check("there is a compression config to deploy", os.path.exists(conf))
if os.path.exists(conf):
    text = open(conf, encoding="utf-8").read()
    check("it turns gzip on", re.search(r"^\s*gzip\s+on;", text, re.M))
    for kind in ("text/css", "application/javascript", "image/svg+xml"):
        check(f"and covers {kind}", kind in text)
    check("it varies on the encoding, so a proxy cannot serve the wrong one",
          re.search(r"^\s*gzip_vary\s+on;", text, re.M))

print("\n" + "=" * 62)
print("ALL ASSET CHECKS PASSED" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
