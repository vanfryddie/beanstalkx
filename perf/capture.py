"""Render every affected route through the real test client and dump the
HTML, so the optimized code can be diffed byte-for-byte against the
original. Output dir is argv[1]."""
import os, sys, hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, application

outdir = sys.argv[1]
os.makedirs(outdir, exist_ok=True)
application.application.config["TESTING"] = True
c = application.application.test_client()
with c.session_transaction() as s:
    s["login"], s["role"], s["auth_source"], s["site"] = "admin", "admin", "manual", "SCN2"

ams = [r["login"] for r in db.get_roster_by_roles(["am"])]
oms = [r["login"] for r in db.get_roster_by_roles(["om"])]
soms = [r["login"] for r in db.get_roster_by_roles(["som"])]

urls = [
    ("reporting", "/scorecards?tab=reporting"),
    ("rankings", "/scorecards?tab=manager_rankings"),
    ("som_om", "/scorecards?tab=som_om"),
    ("olr_index", "/ld-management/olr"),
    ("indirect_roles", "/ld-management/indirect-roles"),
    ("indirect_roles_overview", "/ld-management/indirect-roles?tab=overview"),
    ("trainer_overview", "/ld-management/trainer-overview"),
]
for i, am in enumerate(ams[:8]):
    urls.append((f"overview_am_{i}", f"/overview?am={am}"))
    urls.append((f"olr_am_{i}", f"/ld-management/olr/{am}"))
for i, om in enumerate(oms[:4]):
    urls.append((f"overview_om_{i}", f"/overview?am={om}"))
    urls.append((f"olr_om_{i}", f"/ld-management/olr/{om}"))
for i, som in enumerate(soms[:2]):
    urls.append((f"overview_som_{i}", f"/overview?am={som}"))

manifest = []
for name, url in urls:
    r = c.get(url)
    body = r.get_data()
    with open(os.path.join(outdir, f"{name}.html"), "wb") as fh:
        fh.write(body)
    manifest.append(f"{name}\t{url}\t{r.status_code}\t{len(body)}\t{hashlib.sha256(body).hexdigest()[:16]}")

with open(os.path.join(outdir, "MANIFEST.txt"), "w") as fh:
    fh.write("\n".join(manifest) + "\n")
print(f"captured {len(urls)} routes -> {outdir}")
