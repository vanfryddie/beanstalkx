"""Static guard on every SQL script handed to executescript().

PGConnection.executescript splits on ';' and hands each piece to
psycopg2. sqlite3's own executescript() instead parses the whole script,
so anything the splitter mishandles works locally and fails only against
Postgres — in init_db(), called from before_request, which means every
request 500s. That is not hypothetical: a '--' comment containing a
semicolon shipped exactly that outage.

This runs without a database of any kind, so it can gate every change.
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "db.py")

failures = []


def fail(msg):
    print("  FAIL  " + msg)
    failures.append(msg)


src = open(SOURCE, encoding="utf-8").read()

scripts = []
for m in re.finditer(r"executescript\(\s*\n?\s*\"\"\"", src):
    start = src.index('"""', m.start()) + 3
    end = src.index('"""', start)
    line_no = src[:start].count("\n") + 1
    scripts.append((line_no, src[start:end]))

print(f"found {len(scripts)} executescript block(s) in db.py")
if not scripts:
    fail("no executescript blocks found — did db.py move?")

for line_no, script in scripts:
    for offset, line in enumerate(script.split("\n")):
        stripped = line.strip()
        if stripped.startswith("--") or " --" in stripped:
            fail(f"db.py:{line_no + offset} SQL comment inside an executescript "
                 f"block: {stripped[:60]!r}")

    # Every ';'-separated chunk must be executable SQL on its own.
    for chunk in script.split(";"):
        chunk = chunk.strip()
        if not chunk or chunk.upper().startswith("PRAGMA"):
            continue
        first = next((l.strip() for l in chunk.split("\n") if l.strip()), "")
        if not re.match(r"^(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|SELECT|WITH)\b",
                        first, re.IGNORECASE):
            fail(f"db.py:{line_no} chunk does not begin with a SQL keyword — "
                 f"the splitter would send this to Postgres as-is: {first[:60]!r}")

    # Unbalanced quotes across a chunk boundary mean a literal was split.
    for chunk in script.split(";"):
        if chunk.count("'") % 2:
            fail(f"db.py:{line_no} ';' splits a quoted literal: {chunk.strip()[:60]!r}")

print("\nEvery table declared to have an id primary key really has one")
# The Postgres layer appends RETURNING id to an INSERT into any table in
# _TABLES_WITH_ID_PK, so a table listed there without an id column is a
# 500 that only ever appears in production. floor_identity was added to
# that set by hand and keys on employee_id; this is the check that would
# have caught it before Postgres did.
import sqlite3 as _sqlite3
sys.path.insert(0, ROOT)
import db

probe = _sqlite3.connect(":memory:")
for _line_no, block in scripts:
    try:
        probe.executescript(block)
    except Exception as exc:
        print("  note: could not apply one schema block to the probe:", exc)

declared = {row[0] for row in probe.execute(
    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
missing_id, undeclared = [], []
for table in sorted(db._TABLES_WITH_ID_PK):
    if table not in declared:
        continue
    cols = {r[1] for r in probe.execute(f"PRAGMA table_info({table})").fetchall()}
    if "id" not in cols:
        missing_id.append(table)
for table in sorted(declared):
    cols = {r[1] for r in probe.execute(f"PRAGMA table_info({table})").fetchall()}
    pk = [r[1] for r in probe.execute(f"PRAGMA table_info({table})").fetchall() if r[5]]
    if "id" in cols and pk == ["id"] and table not in db._TABLES_WITH_ID_PK:
        undeclared.append(table)
probe.close()

ok = True
if missing_id:
    ok = False
    print("  FAIL  listed as having an id primary key but has no id column: " + ", ".join(missing_id))
else:
    print("  PASS  every table listed as having an id primary key has one")
if undeclared:
    print("  note  tables with an id primary key not listed (lastrowid will not work there): "
          + ", ".join(undeclared))
if not ok:
    import sys as _sys
    print("\n" + "=" * 60)
    print("SCHEMA/ID MISMATCH — fix _TABLES_WITH_ID_PK in db.py")
    _sys.exit(1)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} PROBLEM(S) — these break Postgres but not SQLite")
    sys.exit(1)
print("ALL SCHEMA SCRIPTS SAFE for the ';'-splitting Postgres layer")
