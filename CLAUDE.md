# LDIS — Learning & Development Intelligence Systems

A Flask console for Amazon's EU12 fulfillment-centre network, built around
site SCN2. It tracks training compliance, cross-training coverage, indirect
roles, floor staffing and ambassador programmes for a site's management
chain (AM → OM → SOM), on data uploaded as CSV/XLSX exports.

Three files hold nearly all of it: `application.py` (~8k lines, routes and
view-building), `db.py` (~10.5k lines, schema and every query), and
`static/style.css` (~5k lines). 46 Jinja templates. There is no build step
and no front-end framework.

## Running it

```bash
python3 application.py                   # dev server, SQLite under data/
python3 perf/run_all.py                  # seed, then every suite, on SQLite
DATABASE_URL=postgresql:///scratch python3 perf/run_all.py    # same, on Postgres
python3 perf/test_<name>.py              # one suite
```

`perf/run_all.py` seeds first on purpose: several suites compare counts
against the seed, so running one against a database a previous pass has
already written to fails for reasons that have nothing to do with the code.

Production is Elastic Beanstalk behind an ALB doing OIDC (`oidc_auth.py`
verifies the ALB-injected claims; reached directly, the app falls back to
a manual login).

## The two engines

SQLite locally, PostgreSQL (RDS) in production, switched on `DATABASE_URL`
or `RDS_HOSTNAME` — `db.USE_POSTGRES` is the flag. `PGConnection` /
`PGCursor` wrap psycopg2 so the ~120 call sites written against sqlite3's
API work unchanged: they translate `?` → `%s`, append `RETURNING id` for
the tables in `_TABLES_WITH_ID_PK`, and force autocommit.

**Anything you touch in `get_db()`, `get_global_db()` or the connection
pool needs reasoning about a real pooled Postgres connection, not just
what SQLite does — say so explicitly in your explanation.** Pooled
connections are reused across requests and sites, which is why
`search_path` is re-set on every checkout rather than once.

Hazards that have each cost a production incident or a long debugging
session:

- **Never put a `--` comment inside a script passed to `executescript`.**
  The compatibility layer splits on `;`, so a comment containing one is
  torn in half.
- **A literal `%` in SQL is psycopg2's placeholder marker.** `LIKE 'x%'`
  inline raises `IndexError: tuple index out of range`; bind the pattern
  as a parameter instead — `LIKE ?` with `("x%",)`.
- **Postgres accepts a `search_path` naming a schema that does not
  exist** and silently resolves to the next entry. That is why `init_db()`
  creates a site's schema under the advisory lock before creating tables:
  without it every non-default site read and wrote the default site's data.
- **A new column goes in the migration list** in `init_db()`
  (`[("table", "column", "TYPE"), ...]`), not only in the `CREATE TABLE`,
  or existing deployments never get it.
- **A new table with an `id` primary key goes in `_TABLES_WITH_ID_PK`**,
  or `lastrowid` is wrong on Postgres. Tables with natural keys must not.

## Multi-site

A site is a separate database: its own file on SQLite, its own schema on
Postgres. `set_current_site()` / `get_current_site()` drive it per request
(`establish_site`, a `before_request`). A site must be **registered** via
`create_site()` to be switchable — a request resolves its site from the
registry and silently falls back to the default for anything it does not
find there, so an unregistered scratch site sends writes at the real one.

**Anything that loops across sites — Regional Overview does — must restore
the original site in a `finally`, even if one site's computation raises.**

## Performance

Pages were slow from round trips, not slow SQL: the overview issued 736
statements. Two mechanisms fixed it, and new code should use them rather
than reintroducing per-row queries.

- `request_memoize` caches a read-only lookup for one request, keyed by
  site plus arguments. Only for slowly-changing org/profile data that
  callers do not mutate. `_memo_key()` normalises list/set/dict arguments —
  without it, calls with list arguments silently miss the cache.
- `bulk_read_after(name, loader, threshold=None)` answers the first N calls
  with a targeted query and reads the whole table after. `BULK_READ_AFTER`
  is read at call time so a test can force either path.

**Every bulk read is tested against its targeted equivalent**, with
`db.BULK_READ_AFTER` forced to 10⁹ and to 0, so both paths provably agree.
Keep that discipline — see `perf/test_bulk_reads.py` and
`perf/test_scorecard_equiv.py`.

Request-scoped connection reuse (`begin_request_scope` /
`end_request_scope`) makes `close()` a no-op inside a request so a page
that calls a hundred helpers opens one connection.

## Uploads

Every ingestion function reads headers through `_ci_row()` — **CSV column
lookups are case-insensitive**, because the source exports do not agree on
casing across sheets.

The `tracked_items` imports treat a file as a **snapshot of the population
it covers**, not a list of changes. They prune rows the file no longer
contains, bounded three ways: rows whose `source` is `'manual'` are left
alone (NULL counts as import-owned, since every row written before the
column existed came from an import); only the FCs the file actually covers
are pruned; and an import that would remove more than `PRUNE_SHARE_LIMIT`
of a section above `PRUNE_FLOOR` rows is treated as the wrong file, loaded
without pruning, and says so. Every removal is reported in the upload's own
message — this is the only place the app deletes data on its own, and it is
never silent.

`xt_hours` and the IR tables are full-replace instead. `org_map` is a
deliberately accumulating lookup that nothing clears, so it must never be
used to answer "is this person still here" — `logins_in_current_data()`
reads only the tables an upload replaces or prunes.

## Cross-training standards evaluation

The Cross Learning Model tab leads with a replay of past shifts against
every cross-training standard (`xt_planning.py`: pure arithmetic, no
Flask, no database). It reads five inputs: roster, Hours on Function, the
SSPOT shift calendar, process day (`process_volume`) and associate day.
`db.load_planning_data()` feeds it a site's tables; `xt_planning.sample_data()`
feeds it an invented site for the **Sample data** toggle.

- **Sample people are invented** (`smp00001`…). A performance figure must
  never be attached to a real login, even as a demonstration.
- **Home department comes from Hours on Function**, not the roster: the
  roster's Department ID is a cost centre spanning several departments.
- **The language model only words the "Why" panel.** Every verdict and
  number is computed; generated sentences go through `_clm_vet_reasons()`.
- The data contract for sites is `LDIS_Day_Plan_Data_Spec.md`; templates
  are in `data_templates/` and downloadable from the Data Upload tab.

## Front end

**No external runtime dependencies. No CDN libraries.** Charts, sparklines,
meters and "copy as image" exports are hand-built in native SVG/canvas —
`templates/_charts.html` holds the chart macros and one shared crosshair.
Apple-ish design language, `dash-*` component system, dark by default with
`html[data-theme="light"]`.

Chart rules that are settled and should not be relitigated:

- **Never a dual-axis chart.** Two measures of different scale become two
  charts, small multiples, or an indexed common base.
- Categorical slots 1–2 are `#2a78d6` / `#eb6834` (light) and
  `#3987e5` / `#d95926` (dark); the blue sequential ramp runs
  `#cde2fb` → `#0d366b`. Declared once as `--viz-*` tokens so the themes
  swap in one place.
- Status colours (red/amber/green) are reserved for status. A measurement
  that is merely above or below a line uses the categorical hues. A thing
  the model cannot judge reads neutral, not red.
- Run the palette validator before shipping a new categorical palette
  rather than eyeballing CVD separation.

A layout change needs a browser pass, not just a passing test.
`python3 perf/sweep_pages.py` opens every page at 430px and 1500px in both
themes and reports sideways scroll, elements wider than their box with
nothing able to scroll them, and console errors — 108 page loads, about a
minute. Chromium is pre-installed; `perf/browser.py` finds it.

One finding is known and left alone: Shift Pulse's 24-column hourly table
overflows a header cell by 3px at phone width. It is inside a container and
does not move the page.

## Testing

**Test before telling the user something works.** Write a script that
exercises it through `app.test_client()` or the real functions, run it, and
show it passing. The suites in `perf/` are plain Python — no pytest — each
printing `PASS`/`FAIL` lines and exiting non-zero on failure.

What makes a good suite here:

- Check conclusions, not just numbers. The point of a page is that a senior
  can argue with it, so test that the same evidence always produces the same
  verdict and that the verdict changes when the evidence does.
- Build the conditions it needs. A suite that only passes against whichever
  database it was pointed at is testing the data. Several did, and failed
  the moment they met a freshly seeded Postgres.
- Leave the database as it found it. A suite that wipes a section fails the
  suites after it; use a registered scratch site for destructive work.
- Prove a new test can fail. Break the thing deliberately once and watch it
  catch it.

## Writing code here

- Comments explain **why**, in full sentences, at the density of the
  existing files. A comment restating the line below it is noise; one
  recording the incident that shaped the line is why this codebase is
  maintainable.
- Follow the file's existing idiom rather than importing a new one.
- Say plainly what was verified and what was not. If a step was skipped or
  a test failed, say so with the output.
