"""Seed a site, then run every suite against it, on whichever engine
DATABASE_URL points at.

The suites assume a freshly seeded roster: several of them write, and a
few — the floor ones especially — compare counts against the seed. Run
individually against a database a previous run has already chewed on,
they fail for reasons that have nothing to do with the code. Seeding
first makes that requirement explicit instead of tribal knowledge.

    python3 perf/run_all.py                     # SQLite
    DATABASE_URL=postgresql:///scratch python3 perf/run_all.py
    python3 perf/run_all.py --no-seed           # keep the data as it is
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(script, label):
    started = time.time()
    done = subprocess.run([sys.executable, os.path.join(HERE, script)],
                          capture_output=True, text=True, cwd=ROOT)
    return done.returncode == 0, time.time() - started, done.stdout + done.stderr


def main():
    seed = "--no-seed" not in sys.argv
    engine = "Postgres" if os.environ.get("DATABASE_URL") else "SQLite"
    print(f"engine: {engine}")

    if seed:
        for script in ("seed.py", "seed_xt_insights.py"):
            ok, secs, out = run(script, script)
            print(f"  {'seeded ' if ok else 'SEED FAILED '}{script} ({secs:.0f}s)")
            if not ok:
                print(out[-2000:])
                return 1
    else:
        print("  not seeding (--no-seed)")

    suites = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "test_*.py")))
    failed, skipped = [], []
    for suite in suites:
        ok, secs, out = run(suite, suite)
        # test_postgres.py excuses itself on SQLite rather than failing.
        if not ok and "set DATABASE_URL" in out:
            print(f"  skip  {suite:<34} needs Postgres")
            skipped.append(suite)
            continue
        checks = out.count("PASS")
        print(f"  {'ok  ' if ok else 'FAIL'}  {suite:<34} {checks:>4} checks  {secs:5.0f}s")
        if not ok:
            failed.append(suite)
            for line in out.splitlines():
                if "FAIL" in line:
                    print("          " + line.strip())

    print()
    print(f"{len(suites) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
          f"{len(skipped)} skipped, on {engine}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
