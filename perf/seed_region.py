"""Seed a multi-site region so Regional Overview can be built and looked
at against realistic data — several sites of differing health, each with
its own org, tracked items, and weeks of OLR snapshot history."""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
from seed import seed as seed_site

SITES = [
    ("SCN2", "Barcelona BCN2",  "Spain",       1.00, 4, 26),
    ("STR1", "Stuttgart STR1",  "Germany",     0.90, 3, 22),
    ("MXP5", "Milan MXP5",      "Italy",       0.72, 3, 18),
    ("LTN4", "Luton LTN4",      "UK",          0.95, 2, 20),
    ("CDG7", "Paris CDG7",      "France",      0.60, 2, 16),
    ("DTM2", "Dortmund DTM2",   "Germany",     0.86, 3, 24),
]


def main():
    db.init_global_db()
    for code, name, region, health, oms, assoc in SITES:
        if code != db.DEFAULT_SITE_CODE and not db.get_site(code):
            db.create_site(code, name, region, "seed")
            print(f"registered {code} ({name})")
        elif code == db.DEFAULT_SITE_CODE:
            conn = db.get_global_db()
            conn.execute(
                "INSERT INTO sites (site_code, site_name, region, created_by, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(site_code) DO UPDATE SET site_name=excluded.site_name, region=excluded.region",
                (code, name, region, "seed", db._now()))
            conn.commit(); conn.close()

    for code, name, region, health, oms, assoc in SITES:
        db.set_current_site(code)
        db.init_db()
        random.seed(hash(code) % 10000)
        seed_site(n_som=1, n_om_per_som=oms, n_am_per_om=3, n_assoc_per_am=assoc, weeks=9)
        # Bias this site's statuses so the region has genuinely different
        # health rather than six identical rows.
        conn = db.get_db()
        rows = conn.execute("SELECT id FROM tracked_items").fetchall()
        ids = [r["id"] for r in rows]
        random.shuffle(ids)
        n_ok = int(len(ids) * health)
        cur = conn.cursor()
        for i, item_id in enumerate(ids):
            status = "Compliant" if i < n_ok else random.choice(["Overdue", "Not Started", "Due Soon"])
            cur.execute("UPDATE tracked_items SET status=? WHERE id=?", (status, item_id))
        # Give the snapshot history a matching level and a mild drift, so
        # the trend lines are not flat and not random.
        cur.execute("DELETE FROM olr_weekly_metrics")
        from datetime import date, timedelta
        monday = date.today() - timedelta(days=date.today().weekday())
        logins = [r["login"] for r in db.get_roster_by_roles(["am", "om", "som"])]
        base = health * 100
        for w in range(9):
            ws = (monday - timedelta(weeks=8 - w)).isoformat()
            level = base - (8 - w) * random.uniform(0.2, 1.1)
            for login in logins:
                for key, _l, _s in db.SCORECARD_CATEGORIES:
                    val = max(0, min(100, level + random.uniform(-6, 6)))
                    cur.execute(
                        "INSERT INTO olr_weekly_metrics (login, week_start, metric_key, value, uploaded_at) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(login, week_start, metric_key) DO UPDATE SET value=excluded.value",
                        (login, ws, key, round(val, 1), db._now()))
        conn.commit(); conn.close()
        print(f"  {code}: biased to ~{int(base)}% health with 9 weeks of history")

    db.set_current_site(db.DEFAULT_SITE_CODE)
    print("\nregion seeded:", ", ".join(s[0] for s in SITES))


if __name__ == "__main__":
    main()
