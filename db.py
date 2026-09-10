"""
Database layer for the SCN2 L&D Page.

Implements the data model behind the concept paper: a central portal
with Planning / Training / Staffing sections that roll up into four
levels of scorecard (Compliance, Staffing Compliance, L&D, Senior
Leadership), plus a weekly training plan, an escalation/callout log,
trainer success metrics, and an L&D Operations structure.

Backed by Postgres (via RDS) when DATABASE_URL or Elastic Beanstalk's
RDS_* environment variables are present — this is the supported
production setup, since RDS is shared across every instance behind the
load balancer, unlike a local SQLite file. Falls back to a local SQLite
file only when no RDS connection info is configured (local dev without
a database attached). The get_db()/get_conn_kind() wrapper below hides
the differences so the rest of this file (121 call sites) never needs to
know or care which backend is live — see PGConnection/PGCursor.
"""
import sqlite3
import os
import csv
import io
import uuid
import re
import threading
import functools
from datetime import datetime, date, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "app.db")
GLOBAL_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "sites_registry.db")
DEFAULT_SITE_CODE = "SCN2"

# Every request sets this once (see application.py's before_request
# hook) based on the session's current site, and every get_db() call
# for the rest of that request reads it — a thread-local rather than a
# plain module global so two requests being handled concurrently on
# different threads never see each other's site. Defaults to SCN2 (the
# site this app already had all its data under before multi-site
# support existed) so nothing that doesn't call set_current_site
# explicitly — a script, a test, a background job — silently loses
# access to the original data.
_site_context = threading.local()


def set_current_site(site_code):
    """Sets which site's database subsequent get_db() calls on this
    thread should connect to, for the rest of the current request."""
    _site_context.site = (site_code or DEFAULT_SITE_CODE).strip().upper()


def get_current_site():
    return getattr(_site_context, "site", None) or DEFAULT_SITE_CODE


def _safe_site_slug(site_code):
    """Lowercase, alnum/underscore-only version of a site code, safe to
    interpolate into a Postgres schema name or SQLite filename. Site
    codes are also validated at creation time (see create_site), but
    this is the last line of defense against anything reaching a raw
    SQL statement unsanitized."""
    return re.sub(r"[^a-z0-9_]", "", (site_code or "").strip().lower())


def _site_schema_name(site_code):
    """Postgres schema for one site. SCN2 stays in the default 'public'
    schema — that's where every table already lived before multi-site
    support existed, so this needs no migration for existing data.
    Every other site gets its own schema, created by create_site()."""
    site_code = (site_code or DEFAULT_SITE_CODE).strip().upper()
    if site_code == DEFAULT_SITE_CODE:
        return "public"
    return "site_" + _safe_site_slug(site_code)


def _site_db_path(site_code):
    """SQLite file for one site — same reasoning as _site_schema_name:
    SCN2 keeps using the original app.db so existing local/dev
    databases keep working with zero migration."""
    site_code = (site_code or DEFAULT_SITE_CODE).strip().upper()
    if site_code == DEFAULT_SITE_CODE:
        return DB_PATH
    return os.path.join(os.path.dirname(DB_PATH), f"app_{_safe_site_slug(site_code)}.db")


def _pg_dsn():
    """Returns a psycopg2-ready connection string if RDS/Postgres is
    configured via environment variables, else None (meaning: use local
    SQLite). Supports both a plain DATABASE_URL and the RDS_* variables
    Elastic Beanstalk auto-injects when an RDS instance is attached to
    the environment. RDS enforces SSL by default (rds.force_ssl) — a DSN
    with no sslmode gets rejected with a slightly cryptic "no pg_hba.conf
    entry... no encryption" error, so sslmode=require is always added
    unless the DSN already specifies one."""
    url = os.environ.get("DATABASE_URL")
    if url:
        # Some providers hand out "postgres://"; psycopg2 wants "postgresql://".
        url = url.replace("postgres://", "postgresql://", 1)
    else:
        host = os.environ.get("RDS_HOSTNAME")
        if not host:
            return None
        port = os.environ.get("RDS_PORT", "5432")
        dbname = os.environ.get("RDS_DB_NAME", "ebdb")
        user = os.environ.get("RDS_USERNAME")
        password = os.environ.get("RDS_PASSWORD")
        url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


USE_POSTGRES = _pg_dsn() is not None

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    # A fresh connection to RDS costs a real network round-trip (TCP +
    # SSL + auth) — cheap to ignore against a local SQLite file, but with
    # ~30-90 get_db() calls on some pages that adds up fast over the
    # network. Pooling keeps a handful of warm connections open per
    # gunicorn worker process instead of paying that cost on every call;
    # PGConnection.close() below returns a connection to the pool rather
    # than actually closing it, so none of the ~100 existing call sites
    # need to change. Each gunicorn worker gets its own pool because this
    # module is imported fresh in each forked worker process (the
    # Procfile doesn't use --preload) — if that ever changes, this pool
    # would need to move to per-worker post-fork setup instead of
    # module-level init, or every worker would share one pool's
    # connections across process boundaries, which is unsafe.
    #
    # Deliberately NOT created here at import time: if RDS is briefly
    # unreachable (still provisioning, a security group not open yet, a
    # network blip), eagerly connecting during module import would take
    # the whole app down at boot — gunicorn workers would fail to start
    # at all, which is a much worse failure than a handful of requests
    # getting a clear database error while the app itself stays up and
    # keeps serving everything else. _get_pg_pool() below creates it
    # lazily on first actual use instead, with a short connect_timeout
    # so a genuine outage fails fast rather than hanging the worker.
    _pg_pool = None
    _pg_pool_lock = threading.Lock()

    def _get_pg_pool():
        global _pg_pool
        if _pg_pool is None:
            with _pg_pool_lock:
                if _pg_pool is None:
                    _pg_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, _pg_dsn(), connect_timeout=5)
        return _pg_pool

# Tables whose primary key is a SERIAL/AUTOINCREMENT 'id' column — only
# these are safe to auto-append RETURNING id to on INSERT (see PGCursor
# below). A few tables use a natural-key primary key instead (login,
# employee_login) and have no 'id' column at all.
_TABLES_WITH_ID_PK = {
    "tracked_items", "weekly_plan", "escalations", "record_escalations",
    "trainer_metrics", "ops_structure", "uploads", "trainings",
    "training_topic_links", "xt_hours", "training_slots", "slot_attendees",
    "trainer_am_assignments", "role_requests", "ambassadors",
    "ambassador_meetings", "ambassador_meeting_attendance",
    "ambassador_meeting_documents", "escalation_tickets", "escalation_ticket_records",
    "escalation_ticket_comments", "ir_roster", "ir_dashboard", "ir_umbrella", "ir_learn",
    "ir_role_config", "xt_standards", "ambassador_targets", "olr_weekly_metrics", "internal_xt_targets",
    "user_department_assignments", "ambassador_indirect_roles", "de_tech_role_mapping",
    "xt_exclusions", "ambassador_process_definitions", "ambassador_training_sessions",
}
_INSERT_TABLE_RE = re.compile(r"INSERT\s+INTO\s+(\w+)", re.IGNORECASE)


class PGCursor:
    """Wraps a psycopg2 cursor so call sites written for sqlite3 keep
    working untouched: translates '?' placeholders to '%s', and — since
    Postgres has no cursor.lastrowid — appends RETURNING id to INSERTs
    against a table known to have an 'id' PK (see _TABLES_WITH_ID_PK)
    and exposes the returned value as .lastrowid, same as sqlite3. The
    connection runs in autocommit mode (see PGConnection), so a bad
    statement never poisons a later one in the same request the way a
    manual transaction would."""

    def __init__(self, cursor):
        self._cur = cursor
        self.lastrowid = None

    def _translate(self, query):
        return query.replace("?", "%s")

    def execute(self, query, params=()):
        q = self._translate(query)
        table_match = _INSERT_TABLE_RE.search(q)
        is_insert = (
            table_match is not None
            and table_match.group(1).lower() in _TABLES_WITH_ID_PK
            and "RETURNING" not in q.upper()
        )
        if is_insert:
            q = q.rstrip().rstrip(";") + " RETURNING id"
        self._cur.execute(q, params)
        if is_insert:
            row = self._cur.fetchone()
            self.lastrowid = row["id"] if row else None
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


class PGConnection:
    """Wraps a psycopg2 connection so the rest of db.py — written against
    sqlite3's conn.execute()/conn.executescript() API — works unchanged
    against Postgres. Runs in autocommit mode: this app's call pattern is
    short-lived per-request connections doing a handful of statements
    followed by conn.commit()/conn.close(), never a rollback — autocommit
    makes each statement durable immediately and, importantly, means one
    bad statement can't abort a whole transaction and block every
    statement after it (Postgres's default behavior otherwise)."""

    def __init__(self, conn):
        conn.autocommit = True
        self._conn = conn

    def execute(self, query, params=()):
        cur = PGCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        return cur.execute(query, params)

    def cursor(self):
        # A handful of CSV-import functions grab one cursor and reuse it
        # across a row loop (rather than conn.execute() per row) — same
        # PGCursor wrapper either way.
        return PGCursor(self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor))

    def executescript(self, script):
        # Postgres has no PRAGMA statements and no AUTOINCREMENT keyword;
        # translate the SQLite schema syntax to Postgres equivalents, then
        # run each statement in turn (psycopg2 doesn't support executing
        # several ;-separated statements in one call the way sqlite3 does).
        #
        # NEVER put a "--" comment inside a script passed to this method.
        # Splitting on ";" is naive about what a semicolon means: sqlite3's
        # own executescript() parses the whole script and handles comments
        # properly, so a comment here works locally and then breaks against
        # Postgres only. A comment holding a semicolon is split in half —
        # the front becomes a comment-only statement ("can't execute an
        # empty query") and the tail becomes prose in front of real SQL (a
        # syntax error). Either one aborts init_db(), which runs from
        # before_request, so every request 500s. This has cost a
        # production outage once; keep schema comments in Python, above
        # the call. test_schema_scripts.py enforces it.
        script = re.sub(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b", "SERIAL PRIMARY KEY", script, flags=re.IGNORECASE)
        cur = self._conn.cursor()
        for statement in script.split(";"):
            statement = statement.strip()
            if not statement or statement.upper().startswith("PRAGMA"):
                continue
            # Defense in depth for the first half of the failure above: a
            # chunk with nothing executable left in it is skipped rather
            # than sent to psycopg2 as an empty query.
            if all(not line.strip() or line.strip().startswith("--")
                   for line in statement.split("\n")):
                continue
            cur.execute(statement)

    def commit(self):
        pass  # autocommit is on — nothing to flush

    def close(self):
        _get_pg_pool().putconn(self._conn)  # returned to the pool, not actually closed


def _new_site_connection(site_code):
    """Opens a genuinely new connection to one site's data. This is the
    original, unpooled-at-the-request-level get_db() body — every
    caller now goes through get_db() below, which reuses one of these
    for the whole request instead of opening one per call."""
    if USE_POSTGRES:
        conn = _get_pg_pool().getconn()
        # autocommit must be set before running anything on this
        # connection, not after — a pooled connection may come back
        # from getconn() already mid-transaction from however it was
        # last used, and psycopg2 refuses to change autocommit while a
        # transaction is open ("set_session cannot be used inside a
        # transaction"). Setting it first means the SET search_path
        # below never itself opens a transaction in the first place.
        conn.autocommit = True
        # Pooled connections are reused across requests for potentially
        # different sites, so the search_path has to be (re-)set on
        # every checkout, not just once — a connection that last served
        # an STR1 request would otherwise still be pointed at STR1's
        # schema on the next request that borrows it, even for SCN2.
        schema = _site_schema_name(site_code)
        cur = conn.cursor()
        cur.execute(f'SET search_path TO "{schema}", public')
        cur.close()
        return PGConnection(conn)
    conn = sqlite3.connect(_site_db_path(site_code), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _new_global_connection():
    """Opens a genuinely new connection to the site-independent registry
    — the original get_global_db() body, same relationship to
    get_global_db() as _new_site_connection has to get_db()."""
    if USE_POSTGRES:
        conn = _get_pg_pool().getconn()
        # See the matching comment in _new_site_connection() — autocommit
        # must be set before the SET search_path query runs, not after.
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute('SET search_path TO "public"')
        cur.close()
        return PGConnection(conn)
    os.makedirs(os.path.dirname(GLOBAL_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(GLOBAL_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


# Statements that only read. Anything else invalidates the per-request
# lookup memo below — see _RequestConnection.execute.
_READ_ONLY_SQL_RE = re.compile(r"^\s*(SELECT|PRAGMA|WITH|EXPLAIN)\b", re.IGNORECASE)


class _RequestCursor:
    """Wraps a cursor taken from a request-scoped connection purely so
    writes issued through it (the CSV importers take one cursor and
    reuse it across a row loop) invalidate the lookup memo the same way
    writes issued through conn.execute() do."""

    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, query, params=()):
        if not _READ_ONLY_SQL_RE.match(query):
            _invalidate_request_memo()
        return self._cur.execute(query, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _RequestConnection:
    """One real connection, shared by every get_db() call in a single
    request.

    The app's ~120 data-access helpers are each written as a
    self-contained `conn = get_db() ... conn.close()` block, which is
    clear to read but meant one real connection open (and, on Postgres,
    one `SET search_path` network round-trip) per helper call — and the
    scorecard/ranking pages call those helpers thousands of times to
    render a single page. This wrapper keeps that call style working
    untouched while making close() a no-op: the connection is opened
    once per request and genuinely released in end_request_scope(),
    which Flask's teardown_request guarantees runs even when the view
    raises.

    Holding a connection for a whole request is only safe because
    PGConnection forces autocommit: an autocommit connection sitting
    idle between statements holds no open transaction, so it pins no
    MVCC snapshot, holds no row locks, and never shows up as 'idle in
    transaction'. Were this app using default (transactional) psycopg2
    connections, this change would trade a connection-churn problem for
    a much worse long-transaction one.
    """

    def __init__(self, conn, site_code):
        self._conn = conn
        self.site_code = site_code

    def execute(self, query, params=()):
        if not _READ_ONLY_SQL_RE.match(query):
            _invalidate_request_memo()
        return self._conn.execute(query, params)

    def cursor(self):
        return _RequestCursor(self._conn.cursor())

    def executescript(self, script):
        _invalidate_request_memo()
        return self._conn.executescript(script)

    def commit(self):
        # Still a real commit: SQLite needs it for durability, and
        # PGConnection.commit() is already a no-op under autocommit.
        return self._conn.commit()

    def close(self):
        # Deliberately does nothing. end_request_scope() owns the real
        # close for the connection's whole request-long lifetime.
        return None

    def _really_close(self):
        try:
            self._conn.close()
        except Exception:
            # A connection already broken by an earlier error must not
            # stop the rest of teardown from releasing everything else —
            # a leak here is exactly what exhausted the pool before.
            pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


# Per-request connection reuse and lookup memoization. Thread-local for
# the same reason the site context is (see _site_context): two requests
# handled concurrently on different threads must never share either.
_request_state = threading.local()


def _scope():
    return getattr(_request_state, "scope", None)


def begin_request_scope():
    """Starts a request's connection scope — called from application.py's
    before_request. Until this is called (a script, a test, a background
    job), get_db() behaves exactly as it always did and opens a fresh
    connection per call, so nothing outside a request context changes."""
    _request_state.scope = {"site": None, "global": None, "memo": {}}


def end_request_scope():
    """Releases whatever this request opened — called from
    teardown_request, which Flask runs even when the view raised, so a
    failing request can never leak a pooled connection."""
    scope = _scope()
    _request_state.scope = None
    if not scope:
        return
    for key in ("site", "global"):
        conn = scope.get(key)
        if conn is not None:
            conn._really_close()


def _invalidate_request_memo():
    """Any write through a request-scoped connection drops the whole
    lookup memo. Coarse on purpose: a route that changes a role and then
    re-reads it in the same request must see the new value, and dropping
    everything is the version of that rule with no per-function
    exceptions to get wrong."""
    scope = _scope()
    if scope is not None:
        scope["memo"].clear()


def request_memoize(fn):
    """Memoizes one read-only lookup for the duration of a single
    request, keyed by the current site plus the call's arguments.

    Only for functions that read slowly-changing org/profile data and
    whose return value callers don't mutate. Outside a request scope
    this is a straight pass-through, and any write through a
    request-scoped connection clears the memo (see
    _invalidate_request_memo), so a cached value can't outlive a change
    to the rows behind it.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        scope = _scope()
        if scope is None:
            return fn(*args, **kwargs)
        try:
            key = (fn.__name__, get_current_site(), args,
                   tuple(sorted(kwargs.items())) if kwargs else ())
            hash(key)
        except TypeError:
            # Unhashable argument (a list scope, say) — just don't cache.
            return fn(*args, **kwargs)
        memo = scope["memo"]
        if key in memo:
            return memo[key]
        value = fn(*args, **kwargs)
        memo[key] = value
        return value

    wrapper.__wrapped__ = fn
    return wrapper


def get_db():
    """Connection scoped to the current site (see set_current_site) —
    Postgres via a schema-scoped search_path, SQLite via a per-site
    file. Every one of this file's ~120 existing call sites keeps
    working unchanged: whichever site was set for this request, every
    query on this connection transparently only sees that site's data.

    Inside a request (see begin_request_scope) the same connection is
    handed back to every caller and close() on it is a no-op, so a page
    that calls a hundred helpers opens one connection rather than a
    hundred. The scope holds a single site connection at a time: when
    the site context changes mid-request — which only Regional
    Overview's per-site loop does — the previous site's connection is
    released before the new one is opened, so that loop costs one
    connection per site in sequence rather than holding all of them at
    once against a pool that maxes out at 10.
    """
    site_code = get_current_site()
    scope = _scope()
    if scope is None:
        return _new_site_connection(site_code)
    existing = scope["site"]
    if existing is not None:
        if existing.site_code == site_code:
            return existing
        existing._really_close()
        # Connections are per-site; anything memoized for the old site
        # must not be read back under the new one. (The memo key includes
        # the site too — this is the belt to that braces.)
        scope["memo"].clear()
        scope["site"] = None
    conn = _RequestConnection(_new_site_connection(site_code), site_code)
    scope["site"] = conn
    return conn


def get_global_db():
    """Connection to the small, site-independent registry: the list of
    sites themselves, and who has cross-site (regional) access — this
    is what the site switcher and Weekly Business Review read, and it
    must be reachable no matter which site is 'current', so it's never
    routed through get_db()/set_current_site. Postgres: always the
    'public' schema explicitly, regardless of the current site's
    search_path. SQLite: a dedicated file, separate from any site's
    per-site database file.

    Request-scoped in the same way as get_db(), in its own slot — it
    stays valid across a site switch, since it doesn't depend on which
    site is current.
    """
    scope = _scope()
    if scope is None:
        return _new_global_connection()
    existing = scope["global"]
    if existing is not None:
        return existing
    conn = _RequestConnection(_new_global_connection(), None)
    scope["global"] = conn
    return conn

def get_storage_diagnostics():
    """Self-check for the exact 'data disappears on refresh' failure mode
    this is meant to prevent: local SQLite/local disk only exist on ONE
    EB instance, so if the environment is running more than one, only
    the instance that received a given write has it — a different
    instance serving the next request won't. Surfaced on L&D Settings so
    an admin can tell at a glance whether the environment variables that
    switch this over to shared storage (RDS, S3) are actually picked up,
    without needing AWS console access to check."""
    pg_dsn = _pg_dsn()
    db_detail = None
    if USE_POSTGRES and pg_dsn:
        # Don't leak the password — show host/db only.
        try:
            after_at = pg_dsn.split("@", 1)[1]
            db_detail = after_at.split("?")[0]
        except IndexError:
            db_detail = None
    return {
        "db_backend": "postgres" if USE_POSTGRES else "sqlite",
        "db_detail": db_detail,
        "storage_backend": "s3" if USE_S3 else "local",
        "storage_detail": S3_BUCKET if USE_S3 else None,
    }

# Sections match the concept paper's structure exactly, so the UI labels
# and the data model never drift apart.
SECTIONS = {
    "planning_compliance": "Compliance & Refresher Training",
    "planning_de_tech": "DE Technical Briefing",
    "planning_indirect_roles": "Indirect Roles Compliance",
    "planning_bts": "BTS Compliance",
    "training_learn": "Self-Learning — Learn",
    "training_curiosity": "Self-Learning — Curiosity",
    "training_compliance_self": "Self-Learning — Compliance",
    "staffing_xt": "Cross-Training (XT) Status",
    "staffing_instructor": "Ambassador / Peer Availability",
    "staffing_indirect_coverage": "Indirect Role Coverage",
    "compliance_safety": "Safety Training Compliance",
    "xt_hours": "Cross-Training — Hours on Function",
}

# The only sections L&D/Trainers can push data into via the Upload page.
# Everything else in SECTIONS still exists and still feeds Reporting/AM
# Overview — it's just sourced some other way, not through manual upload.
UPLOADABLE_SECTIONS = ["compliance_safety", "planning_de_tech", "planning_indirect_roles", "planning_bts"]

# CSV import specifically also offers xt_hours (a different table shape —
# long-format proficiency data, not a tracked_items compliance record) —
# it's CSV-only, so it's kept out of UPLOADABLE_SECTIONS (which also
# drives the single-record manual-entry form and the per-section Clear
# buttons, neither of which make sense for that shape of data).
CSV_IMPORT_SECTIONS = UPLOADABLE_SECTIONS + ["xt_hours"]

PLANNING_SECTIONS = ["planning_compliance", "planning_de_tech", "planning_indirect_roles", "planning_bts"]
TRAINING_SECTIONS = ["training_learn", "training_curiosity", "training_compliance_self"]
STAFFING_SECTIONS = ["staffing_xt", "staffing_instructor", "staffing_indirect_coverage"]
COMPLIANCE_SECTIONS = ["compliance_safety"]

# L&D Management shift tabs
SHIFTS = {"early": "Early Shift", "late": "Late Shift", "night": "Night Shift"}
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# Ambassador Management — departments that carry their own ambassador roster
# per shift.
AMBASSADOR_DEPARTMENTS = ["Receive", "RSP", "Pack Singles", "Pack Multis", "AFE", "Ship Dock", "ICQA"]

# The same physical area goes by more than one name depending on which
# data source you're looking at — the Indirect Role Helper table (and
# the raw IR Dashboard/Umbrella exports it's built from) call AFE's area
# "Chutings", never "AFE"; other exports may use combined shorthand like
# "IC/QA/CS" for ICQA. Ambassador targets, process lists, and any
# AM-department comparison need to treat these as the same department,
# not silently fail to match because of which system a name came from.
DEPARTMENT_ALIAS_GROUPS = {
    "AFE": {"AFE", "Chutings"},
    "ICQA": {"ICQA", "IC/QA/CS", "IC", "QA", "CS"},
}


def department_group(name):
    """Every known name-variant for this department, so matching can
    treat informal/alternate names (Chutings for AFE, IC/QA/CS for
    ICQA) as the same department. Returns {name} unchanged for anything
    with no known aliases."""
    if not name:
        return {name}
    name = name.strip()
    for canonical, group in DEPARTMENT_ALIAS_GROUPS.items():
        if name == canonical or name in group:
            return group
    return {name}


def normalize_department(name):
    """The canonical department name (AFE, ICQA, ...) for a raw value
    that might be an informal alias — used wherever a department needs
    to be compared against AMBASSADOR_DEPARTMENTS or looked up in
    AMBASSADOR_DEPT_PROCESSES, both of which are keyed by canonical
    name only."""
    if not name:
        return name
    name = name.strip()
    for canonical, group in DEPARTMENT_ALIAS_GROUPS.items():
        if name == canonical or name in group:
            return canonical
    return name


def get_app_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_app_setting(key, value, updated_by):
    conn = get_db()
    conn.execute(
        "INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (key, str(value), updated_by, _now()),
    )
    conn.commit()
    conn.close()


# How many days a person can go without being staffed on a process
# before their proficiency status is considered to become Lapsed. This
# is a single uniform threshold applied the same way regardless of
# their CURRENT status (Proficient, Refresh, or Practice) — it is not
# a separate per-tier countdown, so "days until expiry" always means
# "days until this person is projected to become Lapsed on this
# process," never "days until they drop one step down the ladder."
# This is a genuine operational policy value this app has no
# authoritative source for —
# it isn't defined anywhere in the data or any spec seen so far — so
# it's a configurable setting (app_settings key
# 'xt_proficiency_expiry_days'), not a hardcoded constant. The 90-day
# default below is only a placeholder until the real policy is
# confirmed and set via Cross-Training Definitions.
XT_PROFICIENCY_EXPIRY_DAYS_DEFAULT = 90


def get_xt_proficiency_expiry_days():
    val = get_app_setting("xt_proficiency_expiry_days")
    try:
        return int(val) if val is not None else XT_PROFICIENCY_EXPIRY_DAYS_DEFAULT
    except ValueError:
        return XT_PROFICIENCY_EXPIRY_DAYS_DEFAULT


# The smallest group size a Trainer-generated draft slot will be created
# for — running a session for one or two people isn't worth the
# instructor's time, so anyone left over after grouping by topic in
# chunks under this size stays unscheduled rather than getting a slot
# of their own; they're picked up automatically next time the draft
# generator runs and there are enough of them. Trainer-editable via
# Weekly Training Plan, not a hardcoded constant, since this is a
# staffing/cost judgment call, not a fact this app can derive.
TRAINER_DRAFT_MIN_CAPACITY_DEFAULT = 4


def get_trainer_draft_min_capacity():
    val = get_app_setting("trainer_draft_min_capacity")
    try:
        return max(1, int(val)) if val is not None else TRAINER_DRAFT_MIN_CAPACITY_DEFAULT
    except ValueError:
        return TRAINER_DRAFT_MIN_CAPACITY_DEFAULT


def set_trainer_draft_min_capacity(value, updated_by):
    value = max(1, int(value))
    set_app_setting("trainer_draft_min_capacity", value, updated_by)
    return value


def find_training_id_for_topic(sections, topic):
    """The trainings catalog entry that covers this topic, if any is
    linked (see training_topic_links / L&D Settings) — checked across
    every section a metric can appear under, since Safety Compliance's
    topics can be linked from either planning_compliance or
    compliance_safety. Used so a generated draft slot shows the real
    training name instead of 'Untitled training.'"""
    conn = get_db()
    ph = ",".join("?" * len(sections))
    row = conn.execute(
        f"SELECT training_id FROM training_topic_links WHERE section IN ({ph}) AND topic=? LIMIT 1",
        list(sections) + [topic],
    ).fetchone()
    conn.close()
    return row["training_id"] if row else None


def compute_xt_expiry(last_date_on_process, proficiency_status=None, threshold_days=None):
    """Given a last-worked-the-process date, the date this person is
    projected to become Lapsed on this process if they never get
    staffed on it again, and how many days from today that is
    (negative = already past it). This is a single uniform threshold —
    it applies the same way whether they're currently Proficient,
    Refresh, or Practice, so the result always means 'days until
    Lapsed,' never 'days until dropping one step down the ladder.'
    Returns (expiry_date, days_until) or (None, None) if there's no
    last-date on record, OR if their status is already Lapsed — Lapsed
    is the bottom of the ladder, there's nothing further to project
    (and if last_date_on_process happens to be recent, showing a
    projected date would nonsensically land in the FUTURE, which is
    exactly backwards)."""
    if not last_date_on_process:
        return None, None
    if proficiency_status == "Lapsed":
        return None, None
    if threshold_days is None:
        threshold_days = get_xt_proficiency_expiry_days()
    try:
        last_dt = datetime.strptime(last_date_on_process[:10], "%Y-%m-%d").date()
    except ValueError:
        return None, None
    expiry = last_dt + timedelta(days=threshold_days)
    days_until = (expiry - date.today()).days
    return expiry.isoformat(), days_until


@request_memoize
def get_assigned_departments(login):
    """Departments explicitly assigned to this login — used for Senior
    Operations Managers, who don't have one personal department the way
    an Area Manager does but still need real Indirect Roles, Cross-
    Training, and Ambassador metrics on their own scorecard. Returned
    in canonical form (aliases resolved)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT department FROM user_department_assignments WHERE login=? ORDER BY department", (login,)
    ).fetchall()
    conn.close()
    return [normalize_department(r["department"]) for r in rows]


def add_assigned_department(login, department, assigned_by):
    conn = get_db()
    conn.execute(
        "INSERT INTO user_department_assignments (login, department, assigned_by, assigned_at) VALUES (?,?,?,?) "
        "ON CONFLICT (login, department) DO NOTHING",
        (login, normalize_department(department), assigned_by, _now()),
    )
    conn.commit()
    conn.close()


def remove_assigned_department(login, department):
    conn = get_db()
    conn.execute(
        "DELETE FROM user_department_assignments WHERE login=? AND department=?",
        (login, normalize_department(department)),
    )
    conn.commit()
    conn.close()

# Ambassador Meetings — the weekly meeting groupings. Pack Multis, Pack
# Singles, and AFE share one meeting; everyone else meets on their own.
# ICQA doesn't have a standing weekly meeting, so it's deliberately left
# out here even though it has its own ambassador roster above.
AMBASSADOR_MEETING_GROUPS = [
    ("receive", "Receive", ["Receive"]),
    ("rsp", "RSP", ["RSP"]),
    ("pack", "Pack Multis, Pack Singles & AFE", ["Pack Multis", "Pack Singles", "AFE"]),
    ("ship_dock", "Ship Dock", ["Ship Dock"]),
]
AMBASSADOR_MEETING_GROUP_MAP = {key: (label, depts) for key, label, depts in AMBASSADOR_MEETING_GROUPS}

# The 3-card grouping for the Reporting page's Attendance/Hours tabs —
# a coarser rollup than AMBASSADOR_MEETING_GROUPS above (which drives
# the actual meeting schedule): Receive and RSP combined into one card
# here, Pack stays as-is, Ship Dock stays as-is. Note ICQA isn't in any
# of these three, same as it isn't in AMBASSADOR_MEETING_GROUPS either —
# it has no ambassador meeting cadence in this app currently.
AMBASSADOR_REPORT_CARD_GROUPS = [
    ("receive_rsp", "Receive & RSP", ["Receive", "RSP"]),
    ("pack", "Pack Multis, Pack Singles & AFE/Chutings", ["Pack Multis", "Pack Singles", "AFE"]),
    ("ship_dock", "Ship Dock", ["Ship Dock"]),
]

# Ambassador Management — which Merged Function process(es) to show hours
# for, per department, and the flag threshold. Matched case-insensitively
# against xt_hours.merged_function; a process with no matching row at all
# (never trained in it) counts as 0 hours, not missing.
AMBASSADOR_DEPT_PROCESSES = {
    "Receive": ["Each Receive", "Decant", "Pallet Decant", "PAX"],
    "RSP": ["Stow Each Nike", "Stow Each Nike Light", "Pick"],
    "Pack Multis": ["Pack Multis", "Pick to Rebin"],
    "AFE": ["Rebin", "Induct", "Chutings"],
    "Pack Singles": ["Pack Singles", "SmartPaper"],
}
# ^ Only used once, to seed ambassador_process_definitions on a fresh
# database (see init_db) — the live source of truth is that table from
# then on, editable by admins via Ambassador Definitions. Don't read
# this dict directly anywhere else; use get_ambassador_processes_by_department().
AMBASSADOR_HOURS_FLAG_THRESHOLD = 20
AMBASSADOR_PRACTICE_HEALTH_TARGET = 95


def ambassador_process_threshold(process):
    """Every tracked ambassador process uses the same 20-hour standard."""
    return AMBASSADOR_HOURS_FLAG_THRESHOLD


def get_ambassador_processes_by_department():
    """{department: [process, ...]} from ambassador_process_definitions —
    the admin-editable live source of truth for which processes count
    toward Ambassador Practice Health in which department. A department
    with no rows here simply isn't tracked at all (shown as such on the
    Ambassador Practice page, not as a misleading 0)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT department, process FROM ambassador_process_definitions ORDER BY department, process"
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["department"], []).append(r["process"])
    return out


def get_ambassador_process_definitions():
    """Every process definition row, for the Ambassador Definitions admin
    page — includes id (for deletion) and who added it and when."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM ambassador_process_definitions ORDER BY department, process"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_ambassador_process_definition(department, process, added_by):
    """Adds one (department, process) pair to be tracked for Ambassador
    Practice Health. Re-adding the same pair is a harmless no-op via
    ON CONFLICT, not a duplicate row."""
    department = (department or "").strip()
    process = (process or "").strip()
    if not department or not process:
        raise ValueError("department and process are both required")
    if department not in AMBASSADOR_DEPARTMENTS:
        raise ValueError(f"'{department}' isn't a recognized Ambassador department")
    conn = get_db()
    conn.execute(
        """INSERT INTO ambassador_process_definitions (department, process, added_by, added_at)
           VALUES (?,?,?,?)
           ON CONFLICT (department, process) DO NOTHING""",
        (department, process, added_by, _now()),
    )
    conn.commit()
    conn.close()


def remove_ambassador_process_definition(definition_id):
    """Removes one process definition — the department stops being
    tracked for that specific process going forward (existing xt_hours
    data isn't touched; this only changes what's tracked from now on)."""
    conn = get_db()
    conn.execute("DELETE FROM ambassador_process_definitions WHERE id=?", (definition_id,))
    conn.commit()
    conn.close()

AMBASSADOR_ATTENDANCE_STATUSES = [
    ("present", "Present"),
    ("absent", "Absent"),
    ("onsite_no_attend", "Onsite, did not attend"),
    ("excused", "Excused"),
]
AMBASSADOR_ATTENDANCE_LABELS = dict(AMBASSADOR_ATTENDANCE_STATUSES)

AMBASSADOR_MEETING_STATUSES = {"held": "Held", "cancelled": "Cancelled", "biweekly_skip": "Biweekly — no meeting this week"}

# Meeting documents (agendas, slide decks, photos) are stored in S3 when
# an S3_BUCKET is configured — required for a multi-instance deployment,
# same reason as RDS for the database: local instance disk isn't shared
# across EB instances behind the load balancer, so an upload one instance
# receives would be invisible to requests any other instance serves.
# Falls back to local disk (data/ambassador_docs/) when no bucket is
# configured, for local dev without S3 attached.
AMBASSADOR_DOCS_DIR = os.path.join(os.path.dirname(__file__), "data", "ambassador_docs")
AMBASSADOR_DOC_ALLOWED_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
AMBASSADOR_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

S3_BUCKET = os.environ.get("S3_BUCKET") or os.environ.get("AWS_S3_BUCKET")
USE_S3 = bool(S3_BUCKET)

if USE_S3:
    import boto3
    _s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def storage_save(stored_name, file_obj):
    """Saves an uploaded file (a werkzeug FileStorage, already at its
    start) under stored_name — to S3 if configured, else local disk."""
    if USE_S3:
        _s3_client.upload_fileobj(file_obj, S3_BUCKET, stored_name)
    else:
        os.makedirs(AMBASSADOR_DOCS_DIR, exist_ok=True)
        file_obj.save(os.path.join(AMBASSADOR_DOCS_DIR, stored_name))


def storage_delete(stored_name):
    if USE_S3:
        _s3_client.delete_object(Bucket=S3_BUCKET, Key=stored_name)
    else:
        path = os.path.join(AMBASSADOR_DOCS_DIR, stored_name)
        if os.path.exists(path):
            os.remove(path)


def storage_presigned_url(stored_name, original_name, content_type, inline, expires_in=300):
    """S3 mode only: a short-lived signed URL the browser can be
    redirected straight to, with the same inline-vs-download and
    filename behavior the local-disk route provides — so S3 mode is a
    drop-in replacement, not a different experience."""
    disposition = "inline" if inline else "attachment"
    return _s3_client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": S3_BUCKET, "Key": stored_name,
            "ResponseContentType": content_type or "application/octet-stream",
            "ResponseContentDisposition": f'{disposition}; filename="{original_name}"',
        },
        ExpiresIn=expires_in,
    )


def storage_local_path(stored_name):
    """Local-disk mode only — the on-disk path for a stored file."""
    return os.path.join(AMBASSADOR_DOCS_DIR, stored_name)


def storage_exists(stored_name):
    if USE_S3:
        try:
            _s3_client.head_object(Bucket=S3_BUCKET, Key=stored_name)
            return True
        except Exception:
            return False
    return os.path.exists(storage_local_path(stored_name))

# Roles that have a specific shift assignment (Team Lead / Trainer / Area
# Manager / Operations Manager). Learning Manager and Senior Operations
# Manager are exempt — they're responsible for their whole org across all
# shifts, not one in particular.
SHIFT_BOUND_ROLES = {"team_lead", "trainer", "am", "om"}

# DE Technical Briefing is normally the managers' own responsibility on the
# floor, not something L&D centrally schedules — except these two, which
# DO get offered via the Weekly Training Plan same as Safety Compliance.
DE_TECH_CENTRALLY_SCHEDULED = ["FSRI", "Robotic Arm Palletizer"]


def _derive_shift(shift_pattern):
    """Shift codes in the operational data are prefixed D/L/N for
    early/late/night (e.g. 'D08A5CPY', 'L58F8R80', 'N58FC5JI')."""
    if not shift_pattern:
        return None
    return {"D": "early", "L": "late", "N": "night"}.get(shift_pattern[0].upper())


def _ir_shift(raw):
    """The Indirect Role tool's own 'shift' column uses F/S/N or
    ES/LS/NS — a different convention from _derive_shift's D/L/N shift
    PATTERN codes, so it gets its own mapping."""
    t = (raw or "").strip().upper()
    if t in ("F", "ES"):
        return "early"
    if t in ("S", "LS"):
        return "late"
    if t in ("N", "NS"):
        return "night"
    return None


def get_employee_shift(employee_login):
    """Best-effort shift lookup for one employee, checked across every
    source that happens to carry it — ir_dashboard first (most direct
    for Indirect Roles work), then xt_hours. Returns None if the
    employee isn't in either, which callers must handle explicitly
    rather than silently defaulting to a shift they were never
    confirmed to work."""
    if not employee_login:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT shift_pattern FROM ir_dashboard WHERE lower(login)=? AND shift_pattern IS NOT NULL LIMIT 1",
        (employee_login.lower(),),
    ).fetchone()
    if row and row["shift_pattern"]:
        shift = _derive_shift(row["shift_pattern"])
        if shift:
            conn.close()
            return shift
    row = conn.execute(
        "SELECT shift FROM xt_hours WHERE lower(employee_login)=? AND shift IS NOT NULL LIMIT 1",
        (employee_login.lower(),),
    ).fetchone()
    conn.close()
    return row["shift"] if row else None


def get_de_tech_centrally_scheduled_gaps():
    """Everyone currently overdue or gapped on FSRI or the Robotic Arm
    Palletizer briefing — the two DE Technical Briefing topics that are
    centrally scheduled by Trainers rather than planned by an Area
    Manager/Team Lead and executed by an Indirect Roles Ambassador (see
    DE_TECH_CENTRALLY_SCHEDULED). Grouped by shift, with each person's
    shift resolved via get_employee_shift — anyone whose shift can't be
    determined is kept in its own 'unknown' bucket rather than silently
    dropped or guessed into a shift."""
    return get_trainer_planned_gaps(["planning_de_tech"], topic_filter=DE_TECH_CENTRALLY_SCHEDULED)


# Which of the 7 L&D metrics a Trainer plans directly, versus which are
# planned by an Area Manager/Team Lead and executed by an Indirect
# Roles Ambassador. Trainers plan all of Safety Compliance, plus DE
# Tech's two centrally-scheduled topics specifically (FSRI, Robotic Arm
# Palletizer — everything else in DE Tech, and all of Indirect Roles,
# is Ambassador territory, not Trainer territory). Each entry maps a
# metric_key to (sections, topic_filter) — topic_filter=None means
# every topic in those sections counts, not just a named subset.
TRAINER_PLANNED_METRICS = {
    "safety_compliance": (["planning_compliance", "compliance_safety"], None),
    "de_tech": (["planning_de_tech"], DE_TECH_CENTRALLY_SCHEDULED),
}


def get_de_tech_ambassador_gaps():
    """Everyone currently overdue or gapped on any DE Technical Briefing
    topic OTHER than FSRI/Robotic Arm Palletizer — the topics an Area
    Manager/Team Lead plans directly, executed by an Indirect Roles
    Ambassador qualified for that topic (see
    get_ambassadors_qualified_for_topic). Same shape and shift-grouping
    as get_trainer_planned_gaps."""
    conn = get_db()
    exclude_ph = ",".join("?" * len(DE_TECH_CENTRALLY_SCHEDULED))
    status_ph = ",".join("?" * len(STATUS_GAP))
    rows = conn.execute(
        f"""SELECT employee_login, full_name, am_login, subcategory, status, due_date
            FROM tracked_items
            WHERE section='planning_de_tech' AND status IN ({status_ph})
              AND subcategory NOT IN ({exclude_ph})""",
        list(STATUS_GAP) + list(DE_TECH_CENTRALLY_SCHEDULED),
    ).fetchall()
    conn.close()
    by_shift = {"early": [], "late": [], "night": [], "unknown": []}
    for r in rows:
        shift = get_employee_shift(r["employee_login"]) or "unknown"
        by_shift.setdefault(shift, []).append({
            "employee_login": r["employee_login"],
            "full_name": r["full_name"] or r["employee_login"],
            "am_login": r["am_login"],
            "topic": r["subcategory"],
            "status": r["status"],
            "due_date": r["due_date"],
        })
    return by_shift


def get_ambassadors_qualified_for_topic(metric_key, topic, shift=None):
    """Every active Indirect Roles Ambassador set up to train the given
    topic — for metric_key='de_tech', via the chain ambassador →
    ambassador_indirect_roles → de_tech_role_mapping → this topic; for
    metric_key='indirect_roles', the same chain minus the last hop
    (topic is itself an ir_role_config.role name). Optionally narrowed
    to one shift, since an Area Manager planning a session needs an
    ambassador who's actually working that shift."""
    conn = get_db()
    if metric_key == "de_tech":
        q = """SELECT DISTINCT a.id, a.login, a.full_name, a.shift, a.department
               FROM ambassadors a
               JOIN ambassador_indirect_roles air ON air.ambassador_id = a.id
               JOIN de_tech_role_mapping dtm ON dtm.ir_role_config_id = air.ir_role_config_id
               WHERE a.active=1 AND a.is_indirect_roles_ambassador=1 AND dtm.de_tech_topic=?"""
        params = [topic]
    elif metric_key == "indirect_roles":
        q = """SELECT DISTINCT a.id, a.login, a.full_name, a.shift, a.department
               FROM ambassadors a
               JOIN ambassador_indirect_roles air ON air.ambassador_id = a.id
               JOIN ir_role_config irc ON irc.id = air.ir_role_config_id
               WHERE a.active=1 AND a.is_indirect_roles_ambassador=1 AND irc.role=?"""
        params = [topic]
    else:
        conn.close()
        return []
    if shift:
        q += " AND a.shift=?"
        params.append(shift)
    q += " ORDER BY a.full_name, a.login"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return rows


def add_ambassador_training_session(metric_key, topic, employee_login, employee_name,
                                     ambassador_login, ambassador_name, shift,
                                     planned_date, planned_time, planned_by):
    """An Area Manager/Team Lead planning one associate into a session
    with a specific Ambassador — for the topics Ambassadors execute
    (regular DE Tech topics, and all of Indirect Roles), not the two
    Trainer-led DE Tech topics (see TRAINER_PLANNED_METRICS for those
    instead)."""
    conn = get_db()
    conn.execute(
        """INSERT INTO ambassador_training_sessions
           (metric_key, topic, employee_login, employee_name, ambassador_login, ambassador_name,
            shift, planned_date, planned_time, planned_by, planned_at, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'planned')""",
        (metric_key, topic, employee_login, employee_name, ambassador_login, ambassador_name,
         shift, planned_date, planned_time, planned_by, _now()),
    )
    conn.commit()
    conn.close()


def get_ambassador_training_sessions(planned_by=None, week_start=None):
    """Sessions an Area Manager/Team Lead has already planned — narrowed
    to one planner and/or one calendar week (Sunday-Saturday, matching
    week_dates elsewhere in this app) when given."""
    conn = get_db()
    q = "SELECT * FROM ambassador_training_sessions WHERE status != 'cancelled'"
    params = []
    if planned_by:
        q += " AND planned_by=?"
        params.append(planned_by)
    if week_start:
        dates = week_dates(week_start)
        q += f" AND planned_date IN ({','.join('?' * len(dates))})"
        params += dates
    q += " ORDER BY planned_date, planned_time"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return rows


def cancel_ambassador_training_session(session_id, cancelled_by):
    """Soft-cancel — keeps the row (so a planner can see it was
    cancelled rather than just vanishing) but excludes it from
    get_ambassador_training_sessions and no longer counts as 'already
    planned' for duplicate-booking checks."""
    conn = get_db()
    conn.execute("UPDATE ambassador_training_sessions SET status='cancelled' WHERE id=?", (session_id,))
    conn.commit()
    conn.close()


def get_trainer_planned_gaps(sections, topic_filter=None):
    """Everyone currently overdue or gapped within the given
    tracked_items section(s) — optionally narrowed to a specific set of
    topics (subcategory values); pass None to include every topic in
    those sections. Grouped by shift, with each person's shift resolved
    via get_employee_shift — anyone whose shift can't be determined is
    kept in its own 'unknown' bucket rather than silently dropped or
    guessed into a shift. This is the general-purpose version behind
    every Trainer-planned metric (see TRAINER_PLANNED_METRICS) — DE
    Tech's FSRI/Robotic-Arm-Palletizer carve-out is just one call into
    this with a topic_filter; Safety Compliance is the same call with
    none, since a Trainer plans every Safety topic, not a named subset."""
    conn = get_db()
    section_ph = ",".join("?" * len(sections))
    status_ph = ",".join("?" * len(STATUS_GAP))
    q = f"""SELECT employee_login, full_name, am_login, subcategory, status, due_date
            FROM tracked_items
            WHERE section IN ({section_ph}) AND status IN ({status_ph})"""
    params = list(sections) + list(STATUS_GAP)
    if topic_filter:
        q += f" AND subcategory IN ({','.join('?' * len(topic_filter))})"
        params += list(topic_filter)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    by_shift = {"early": [], "late": [], "night": [], "unknown": []}
    for r in rows:
        shift = get_employee_shift(r["employee_login"]) or "unknown"
        by_shift.setdefault(shift, []).append({
            "employee_login": r["employee_login"],
            "full_name": r["full_name"] or r["employee_login"],
            "am_login": r["am_login"],
            "topic": r["subcategory"],
            "status": r["status"],
            "due_date": r["due_date"],
        })
    return by_shift


def _ci_row(row):
    """Case-insensitive header lookup for a csv.DictReader row — the
    Indirect Role tool's four source exports don't consistently agree on
    header casing across sheets ('shift' vs 'Shift'), so every lookup
    here is by lowercased header name."""
    return {(k or "").strip().lower(): v for k, v in row.items() if k}


def _ir_rows_from_upload(file_bytes, filename):
    """Yields case-insensitive-keyed row dicts from either a CSV or an
    .xlsx upload — the Indirect Role tool's four sources are equally
    likely to arrive as either, since they're usually just an export
    someone re-saves. Detects by filename extension, falling back to
    CSV if it doesn't look like an Excel file."""
    if (filename or "").lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
        except StopIteration:
            return
        for row in rows_iter:
            if row is None or all(v is None for v in row):
                continue
            d = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
            yield _ci_row(d)
        wb.close()
    else:
        text = file_bytes.decode("utf-8-sig")
        for row in csv.DictReader(io.StringIO(text)):
            yield _ci_row(row)


def ingest_ir_roster_csv(file_bytes, filename):
    """employeeList-SCN2 — the roster export (CSV or .xlsx). Only used
    for one thing: flagging Management Area ID 20, which the Jambuster
    role excludes from counting (matches the source tool's dA20
    exclusion list)."""
    conn = get_db()
    cur = conn.cursor()
    now = _now()
    cur.execute("DELETE FROM ir_roster")
    n = 0
    for row in _ir_rows_from_upload(file_bytes, filename):
        login = _clean(row.get("user id") or row.get("employee login") or row.get("login"))
        if not login:
            continue
        cur.execute(
            "INSERT INTO ir_roster (login, management_area_id, full_name, updated_at) VALUES (?,?,?,?)",
            (login, _clean(row.get("management area id")), _clean(row.get("employee name")), now),
        )
        n += 1
    cur.execute("INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
                (filename, "ir_roster", now, n))
    conn.commit()
    conn.close()
    return n


def ingest_ir_dashboard_csv(file_bytes, filename):
    """IR Dashboard — the main Indirect Role assignment + status export
    (CSV or .xlsx). Raw data only — no Shift/Area/Home Process columns
    needed; those get derived at report time from the raw Shift Code
    plus the roster upload, the same way the source workbook's A/B/C
    formula columns did."""
    conn = get_db()
    cur = conn.cursor()
    now = _now()
    cur.execute("DELETE FROM ir_dashboard")
    n = 0
    for row in _ir_rows_from_upload(file_bytes, filename):
        login = _clean(row.get("employee login"))
        if not login:
            continue
        cur.execute(
            """INSERT INTO ir_dashboard (login, full_name, shift_pattern, manager_login, hours_90, indirect_role, indirect_role_status, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (login, _clean(row.get("full name")), _clean(row.get("shift code")), _clean(row.get("manager login")),
             _clean(row.get("hours in last 3 months")),
             _clean(row.get("indirect role")), _clean(row.get("indirect role status")), now),
        )
        n += 1
    cur.execute("INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
                (filename, "ir_dashboard", now, n))
    conn.commit()
    conn.close()
    return n


def ingest_ir_umbrella_csv(file_bytes, filename):
    """Umbrella — certificate-based role tracking export (CSV or .xlsx).
    Raw data only — see ingest_ir_dashboard_csv."""
    conn = get_db()
    cur = conn.cursor()
    now = _now()
    cur.execute("DELETE FROM ir_umbrella")
    n = 0
    for row in _ir_rows_from_upload(file_bytes, filename):
        login = _clean(row.get("employee login"))
        if not login:
            continue
        cur.execute(
            """INSERT INTO ir_umbrella (login, full_name, shift_pattern, manager_login, certificate_title, certificate_status, certificate_earned_date, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (login, _clean(row.get("full name")), _clean(row.get("shift pattern")), _clean(row.get("manager login")),
             _clean(row.get("certificate title")),
             _clean(row.get("certificate status")), _clean(row.get("certificate earned date")), now),
        )
        n += 1
    cur.execute("INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
                (filename, "ir_umbrella", now, n))
    conn.commit()
    conn.close()
    return n


def ingest_ir_learn_csv(file_bytes, filename):
    """Learn — training-completion-based role tracking export (CSV or
    .xlsx). Raw data only — see ingest_ir_dashboard_csv."""
    conn = get_db()
    cur = conn.cursor()
    now = _now()
    cur.execute("DELETE FROM ir_learn")
    n = 0
    for row in _ir_rows_from_upload(file_bytes, filename):
        login = _clean(row.get("employee login"))
        if not login:
            continue
        cur.execute(
            "INSERT INTO ir_learn (login, full_name, shift_pattern, manager_login, training_name, status, updated_at) VALUES (?,?,?,?,?,?,?)",
            (login, _clean(row.get("full name")), _clean(row.get("shift pattern")), _clean(row.get("manager login")),
             _clean(row.get("training name")), _clean(row.get("status")), now),
        )
        n += 1
    cur.execute("INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
                (filename, "ir_learn", now, n))
    conn.commit()
    conn.close()
    return n

# Indirect Role tool — role catalog, ported faithfully from the source
# workbook's IR_Config sheet (built by SetupConfigSheet in the VBA). This
# is only the SEED data — it's loaded into the ir_role_config table once
# on first init, and from then on the DB table (editable by Senior Ops
# Managers and Trainers on the Definitions tab) is the source of truth,
# not this constant.
# row: (section, role, src_type, area, home_process, match_vals, es, ls,
# ns, special). src_type picks which of the 4 imports (ir_dashboard/
# ir_umbrella/ir_learn) to match against and how; es/ls/ns are the target
# headcount per shift (Early/Late/Night); special flags a handful of
# roles needing extra logic beyond plain matching (see
# compute_ir_overview).
IR_ROLE_CONFIG_CORRECTIONS = [
    # (section, role, old_src, old_match_vals, old_home_process, new_src, new_match_vals, new_home_process)
    ("ICQA", "POD Banding Operator", "UMB", "EUCF_ ILT_ARS_Pod Band and Weaving", "ICQA",
     "IR", "POD Banding Operator", "ICQA"),
    ("INBOUND", "OmniScan", "UMB", "EUCF_ILT_ALL_Omniscan", "RSP",
     "UMB", "EUCF_ILT_ALL_Omniscan", "RSP|Receive"),
    ("CPT", "Loader", "LEARN", "SUBSTR:loader|SUBSTR:loading", "Ship Dock",
     "LEARN", "SCN2 Ship Load Securing PMV DE|SCN2 Ship Load Securing PMV EN", "Ship Dock"),
    ("Quality", "Recirc Auditor", "LEARN", "SUBSTR:recirc", "Ship Dock",
     "LEARN", "SCN2 Ship Recirc Audit PMVs", "Ship Dock"),
    ("Operator", "Jackpot Operator", "IR", "Jackpot Operator", "Ship Dock",
     "LEARN", "SCN2 Ship Jackpot Operator PMVs EN|SCN2 Ship Jackpot Operator PMVs DE", "Ship Dock"),
]
IR_ROLE_CONFIG = [
    ("ICQA", "ISS Problem Solver", "IR", "ICQA", "ICQA", "ISS Problem Solver", 5, 5, 5, ""),
    ("ICQA", "Sweeper", "IR", "ICQA", "ICQA", "Sweeper", 20, 20, 20, ""),
    ("ICQA", "ICQA Problem Solver", "IR", "ICQA", "ICQA", "ICQA Problem Solver", 5, 5, 5, ""),
    ("ICQA", "Blitz Cleaner", "UMB", "ICQA", "ICQA", "AR Floor Clean Blitz Certification", 8, 6, 8, ""),
    ("ICQA", "Receive Problem Solver", "IR", "ICQA", "ICQA", "Receive Problem Solver", 5, 5, 5, ""),
    ("ICQA", "POD Banding Operator", "IR", "ICQA", "ICQA", "POD Banding Operator", 12, 0, 0, ""),
    ("ICQA", "ICQA Peer Trainer", "PT", "ICQA", "ICQA", "PREFIX:EUCF_NTU_PT_", 0, 0, 0, "PT_DEDUP"),
    ("INBOUND", "Amnesty Floor Monitor", "IR", "IB", "RSP", "Amnesty Floor Monitor", 18, 18, 18, ""),
    ("INBOUND", "Blitz Cleaner", "UMB", "IB", "RSP", "AR Floor Clean Blitz Certification", 36, 33, 36, ""),
    ("INBOUND", "Jambuster Plus", "JAMIB", "IB", "Receive",
     "Prereq: Indoor Marshall|Prereq: Dock Clerk|Prereq: Amnesty Floor Monitor|Prereq-Zert: EUCF_NTU_AMB_Receive|Jambuster-Zert: EUCF_ILT_ALL_Jambuster_Plus",
     0, 0, 0, "JAMBUSTER"),
    ("INBOUND", "Ergo Pack machine operator", "IR", "IB", "Receive", "Ergo Pack machine operator", 5, 5, 5, ""),
    ("INBOUND", "Dock Clerk", "IR", "IB", "Receive", "Dock Clerk", 3, 3, 3, ""),
    ("INBOUND", "Indoor Marshall", "IR", "IB", "Receive", "Indoor Marshall", 8, 8, 8, ""),
    ("INBOUND", "TUG Walkie Stow", "IR", "IB", "RSP", "TUG Walkie Stow", 20, 20, 20, ""),
    ("INBOUND", "OmniScan", "UMB", "IB", "RSP|Receive", "EUCF_ILT_ALL_Omniscan", -1, -1, -1, ""),
    ("INBOUND", "Vernier Phase 1 (Weight)", "UMB", "IB", "RSP", "EUCF_ILT_ALL_Weights Measurement Mettler Toledo", 4, 4, 4, ""),
    ("INBOUND", "Vernier Phase 2 (Boxfit)", "UMB", "IB", "RSP", "EUCF_ILT_ALL_Dimensions Measurement Box Fit", 0, 4, 0, ""),
    ("INBOUND", "Vernier Phase 3 (Caliper)", "UMB", "IB", "RSP", "EUCF_ILT_ALL_Dimensions Measurement Caliper", 0, 4, 0, ""),
    ("INBOUND", "IB PIT Driver", "IR", "IB", "Receive", "Pedestrian Powered Pallet Jack", 20, 20, 20, ""),
    ("INBOUND", "IB Peer Trainer", "PT", "IB", "Receive",
     "EUCF_NTU_PT_Indoor Marshall|EUCF_NTU_PT_IB Water Spider|EUCF_NTU_PT_Dock Clerk|EUCF_NTU_PT_Receive PG",
     0, 0, 0, "PT_LIST"),
    ("PRE-SLAM", "Cubiscan operator", "IR", "PRE-SLAM", "", "Cubiscan operator", 10, 10, 10, ""),
    ("PRE-SLAM", "Water spider Pack -- P2R", "IR", "PRE-SLAM", "Pack Multis", "Water spider Pack", 5, 5, 5, "WATERSPIDER"),
    ("PRE-SLAM", "Water spider Pack -- SP", "IR", "PRE-SLAM", "Pack Singles", "Water spider Pack", 8, 8, 8, "WATERSPIDER"),
    ("PRE-SLAM", "Water spider Pack -- AFE", "IR", "PRE-SLAM", "Chutings", "Water spider Pack", 4, 4, 4, "WATERSPIDER"),
    ("PRE-SLAM", "E-Pump Truck P2R", "UMB", "PRE-SLAM", "Pack Multis", "EUCF_NTU_ALL_Electric Pump Truck", 4, 4, 4, ""),
    ("PRE-SLAM", "E-Pump Truck SP", "UMB", "PRE-SLAM", "Pack Singles", "EUCF_NTU_ALL_Electric Pump Truck", 4, 4, 4, ""),
    ("PRE-SLAM", "E-Pump Truck AFE", "UMB", "PRE-SLAM", "Chutings", "EUCF_NTU_ALL_Electric Pump Truck", 4, 4, 4, ""),
    ("PRE-SLAM", "SLAM Operator", "IR", "PRE-SLAM", "", "SLAM Operator", 8, 8, 8, ""),
    ("PRE-SLAM", "Pack PG -- P2R", "IR", "PRE-SLAM", "Pack Multis", "Pack PG", -1, -1, -1, ""),
    ("PRE-SLAM", "Pack PG -- SP", "IR", "PRE-SLAM", "Pack Singles", "Pack PG", -1, -1, -1, ""),
    ("PRE-SLAM", "Pack PG -- AFE", "IR", "PRE-SLAM", "Chutings", "Pack PG", -1, -1, -1, ""),
    ("PRE-SLAM", "Outbound Problem Solver", "IR", "PRE-SLAM", "", "Outbound Problem Solver", 20, 20, 20, ""),
    ("PRE-SLAM", "Jambuster Plus AFE / SP", "JAMPS", "PRE-SLAM", "PRE-SLAM/Pack Singles/Chutings",
     "Prereq: SLAM Operator|Prereq: Pack PG|Prereq-Zert: EUCF_ALL_AFE Induct|Prereq-Zert: EUCF_ILT_ARS_AFE Rebin|Prereq-Zert: EUCF_ILT_Smart Pac Paper|Prereq-Zert: EUCF_ALL_Pack Process Guide|Jambuster-Zert: EUCF_ILT_ALL_Jambuster_Plus",
     75, 73, 73, "JAMBUSTER"),
    ("OUTBOUND", "OB Peer Trainer", "PT", "", "",
     "EUCF_NTU_PT_OB Kick-Out|EUCF_NTU_PT_OB Problem Solve|EUCF_NTU_PT_TSO Problem Solver|EUCF_NTU_PT_OB Slam Operator|EUCF_NTU_PT_Ship PS|EUCF_NTU_PT_OB Water Spider|EUCF_NTU_PT_Pack PG|EUCF_NTU_PT_Cubiscan|EUCF_NTU_PT_Tote Replenisher Pick|EUCF_NTU_PT_Vendor Return Process_Guide|EUCF_NTU_PT_Vendor Return PS|EUCF_NTU_PT_Vendor Return Waterspider",
     0, 0, 0, "PT_LIST"),
    ("CPT", "Ship Clerk", "IR", "POST-SLAM", "Ship Dock", "Ship Clerk", 4, 4, 4, ""),
    ("CPT", "CPT Auditor", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship CPT Audit PMVs DE|SCN2 Ship CPT Audit PMVs EN", 10, 10, 10, "DEDUP"),
    ("CPT", "Indoor Marshall", "IR", "POST-SLAM", "Ship Dock", "Indoor Marshall", 20, 20, 20, ""),
    ("CPT", "Loader", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship Load Securing PMV DE|SCN2 Ship Load Securing PMV EN", 27, 27, 27, ""),
    ("CPT", "CPT Peer Trainer", "PT", "POST-SLAM", "Ship Dock", "PREFIX:EUCF_NTU_PT_", 3, 3, 3, "PT_DEDUP"),
    ("Quality", "Recirc Auditor", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship Recirc Audit PMVs", 6, 6, 6, ""),
    ("Quality", "VAST Auditor", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship VAST Audit PMV", 7, 7, 7, ""),
    ("Quality", "Stage Auditor", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship Stage Auditor PMVs DE|SCN2 Ship Stage Auditor PMVs", 8, 8, 8, "DEDUP"),
    ("Quality", "Netz Cleaner", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship Netz Cleaner PMVs", 5, 5, 5, ""),
    ("Quality", "Quality Peer Trainer", "PT", "POST-SLAM", "Ship Dock", "PREFIX:EUCF_NTU_PT_", 3, 3, 3, "PT_DEDUP"),
    ("Operator", "FSRI (Flat Sorter Robotic Induct)", "IR", "POST-SLAM", "Ship Dock", "FSRI (Flat Sorter Robotic Induct)", 6, 6, 6, ""),
    ("Operator", "Manual Inductor", "UMB", "POST-SLAM", "Ship Dock", "EUCF_ILT_ALL_Manual Induct", 9, 9, 9, ""),
    ("Operator", "TSO", "IR", "POST-SLAM", "Ship Dock", "Ship auditor", 13, 13, 13, "TSO_KOMBI"),
    ("Operator", "Jackpot Operator", "LEARN", "POST-SLAM", "Ship Dock", "SCN2 Ship Jackpot Operator PMVs EN|SCN2 Ship Jackpot Operator PMVs DE", 6, 6, 6, ""),
    ("Operator", "Operator Peer Trainer", "PT", "POST-SLAM", "Ship Dock", "PREFIX:EUCF_NTU_PT_", 2, 2, 2, "PT_DEDUP"),
    ("Other", "OB PIT Driver", "IR", "POST-SLAM", "Ship Dock", "Pedestrian Powered Pallet Jack", 60, 60, 60, ""),
    ("Other", "Jambuster", "IR", "POST-SLAM", "", "Jambuster", 40, 40, 40, ""),
    ("TOM", "Outdoor Marshall", "IR", "POST-SLAM", "TOM", "Outdoor Marshall", 12, 12, 12, ""),
    ("TOM", "Gatehouse Supervisor", "IR", "POST-SLAM", "TOM", "Gatehouse Supervisor", 6, 6, 6, ""),
    ("TOM", "OB Peer Trainer", "PT", "POST-SLAM", "TOM", "PREFIX:EUCF_NTU_PT_", 0, 0, 0, "PT_DEDUP"),
]

# Ported from the source workbook's Helper sheet — this is exactly what
# lets uploads skip the manually-added A/B/C columns (shift/Area/Home
# Process) on employeeList-SCN2, Learn, Umbrella, and IR Dashboard: those
# were never real data, just formulas re-deriving this same lookup. Two
# tables: shift pattern code -> F/S/N letter, and (roster-derived)
# management area id -> (home process, area).
IR_HELPER_SHIFT_PATTERNS = {'D05A5CBY': 'F', 'D06A5CJG': 'F', 'D08A5CPY': 'F', 'D09A5CXL': 'F', 'D14A5AXS': 'F', 'D16A5AFK': 'S', 'D48R1SE6': 'F', 'D58F80RF': 'F', 'D58F81ZO': 'F', 'D58F82QB': 'F', 'D58F82QE': 'F', 'D58F82YR': 'F', 'D58F83RC': 'F', 'D58F83YA': 'F', 'D58F85CC': 'F', 'D58F88KL': 'F', 'D58F88MZ': 'F', 'D58F88W2': 'F', 'D58F89AV': 'F', 'D58F89PT': 'F', 'D58F89RZ': 'F', 'D58F8IYU': 'F', 'D58F8RPV': 'F', 'D58F8SNO': 'F', 'D58F8U2L': 'F', 'D58F8W3Z': 'F', 'D58F8WHO': 'F', 'L38P18AN': 'S', 'L38P1SEK': 'S', 'L58F10MT': 'S', 'L58F10WI': 'S', 'L58F12XE': 'S', 'L58F13EN': 'S', 'L58F13VT': 'S', 'L58F1C4E': 'S', 'L58F1IX3': 'S', 'L58F1J2T': 'S', 'L58F1J3V': 'S', 'L58F1UO5': 'S', 'L58F1VJC': 'S', 'L58F8A1R': 'S', 'L58F8R80': 'S', 'N57F63TN': 'N', 'N57F642W': 'N', 'N57F645G': 'N', 'N57F68AG': 'N', 'N57F6AOW': 'N', 'N57F6C24': 'N', 'N57F6CW4': 'N', 'N57F6D8T': 'N', 'N57F6FDO': 'N', 'N57F6IVD': 'N', 'N57F6QNU': 'N', 'N57F6SEO': 'N', 'N57F6SII': 'N', 'N57F6SZ3': 'N', 'N57F6TPT': 'N', 'N57F6UF0': 'N', 'N57F6VBB': 'N', 'N57F6VOL': 'N', 'N57F6WIK': 'N', 'N57F6WP2': 'N', 'N57F6XN4': 'N', 'N58F12L0': 'N', 'N58F1SET': 'N', 'N58FC0B9': 'N', 'N58FC29V': 'N', 'N58FC454': 'N', 'N58FC5JI': 'N', 'N58FC7UI': 'N', 'N58FC7UU': 'N', 'N58FC9I6': 'N', 'N58FCD66': 'N', 'N58FCR1S': 'N', 'N58FCUJZ': 'N', 'N58FCULU': 'N', 'N58FCVLA': 'N', 'N57F6FHD': 'N', 'N57F6IZ2': 'N'}

IR_HELPER_AREA_BY_ID = {1: ('Receive', 'IB'), 2: ('Receive', 'IB'), 3: ('RSP', 'IB'), 4: (None, None), 5: ('IB TL', 'IB TL'), 6: ('IB Problem Solve', 'ICQA'), 7: ('Ship Dock', 'POST-SLAM'), 8: ('RSP', 'IB'), 9: (None, None), 10: (None, 'POST-SLAM'), 11: (None, None), 12: ('OBPS', 'PRE-SLAM'), 13: ('RSP', 'IB'), 14: ('Chutings', 'PRE-SLAM'), 15: ('Chutings', 'PRE-SLAM'), 16: ('Chutings', 'PRE-SLAM'), 17: ('Pack Multis', 'PRE-SLAM'), 18: ('Pack Singles', 'PRE-SLAM'), 19: ('OB TL', 'OB TL'), 20: ('OBPS', 'PRE-SLAM'), 21: ('Ship Dock', 'POST-SLAM'), 22: (None, None), 23: (None, None), 24: ('Vendor Returns', 'PRE-SLAM'), 25: (None, None), 26: ('Manager', 'Manager'), 27: ('ICQA', 'ICQA'), 28: (None, None), 29: ('TOM', 'POST-SLAM'), 30: ('TOM', 'POST-SLAM'), 31: ('TOM', 'POST-SLAM'), 32: (None, None), 33: ('L&D', 'L&D'), 34: ('Safety', 'Safety'), 35: (None, None), 36: (None, None), 37: (None, None), 38: (None, None), 39: (None, None), 40: (None, None), 41: (None, None), 42: (None, None), 43: (None, None), 44: (None, None)}


@request_memoize
def get_ir_role_config_rows():
    """The live, editable role catalog from the DB — same shape as
    IR_ROLE_CONFIG plus each row's id, for the Definitions tab and for
    compute_ir_overview() to read from."""
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM ir_role_config ORDER BY sort_order, id").fetchall()]
    conn.close()
    return rows


def get_real_de_tech_topics():
    """Every distinct DE Technical Briefing subcategory actually seen in
    uploaded data — excludes FSRI and the Robotic Arm Palletizer, which
    are centrally scheduled rather than tied to a specific Indirect
    Role."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT subcategory FROM tracked_items WHERE section='planning_de_tech' AND subcategory IS NOT NULL ORDER BY subcategory"
    ).fetchall()
    conn.close()
    return [r["subcategory"] for r in rows if r["subcategory"] not in DE_TECH_CENTRALLY_SCHEDULED]


def get_de_tech_role_mapping(ir_role_config_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT de_tech_topic FROM de_tech_role_mapping WHERE ir_role_config_id=? ORDER BY de_tech_topic",
        (ir_role_config_id,),
    ).fetchall()
    conn.close()
    return [r["de_tech_topic"] for r in rows]


def get_all_de_tech_role_mappings():
    """Every role's mapped DE Tech topics in one call — {role_id: [topics]}."""
    conn = get_db()
    rows = conn.execute("SELECT ir_role_config_id, de_tech_topic FROM de_tech_role_mapping ORDER BY de_tech_topic").fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["ir_role_config_id"], []).append(r["de_tech_topic"])
    return out


def set_de_tech_role_mapping(ir_role_config_id, topics, mapped_by):
    """Replaces the full set of DE Tech topics mapped to this role —
    same resubmit-the-whole-selection pattern as every other
    assignment picker in this app."""
    conn = get_db()
    conn.execute("DELETE FROM de_tech_role_mapping WHERE ir_role_config_id=?", (ir_role_config_id,))
    now = _now()
    for topic in topics:
        conn.execute(
            "INSERT INTO de_tech_role_mapping (ir_role_config_id, de_tech_topic, mapped_by, mapped_at) VALUES (?,?,?,?)",
            (ir_role_config_id, topic, mapped_by, now),
        )
    conn.commit()
    conn.close()


def search_ir_roster(query, limit=20):
    """Login-or-name search over the employeeList-SCN2 upload, for the
    'add associates to train' picker on the gap-bucket drill-down — lets
    a manager pull in anyone from the roster, not just whoever the
    matching engine happened to flag as a gap."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    conn = get_db()
    like = f"%{q.lower()}%"
    rows = conn.execute(
        "SELECT DISTINCT login, full_name FROM ir_roster WHERE LOWER(login) LIKE ? OR LOWER(full_name) LIKE ? ORDER BY full_name LIMIT ?",
        (like, like, limit),
    ).fetchall()
    conn.close()
    return [{"login": r["login"], "full_name": r["full_name"]} for r in rows]


def update_ir_role_config_row(row_id, es, ls, ns, area, home_process, match_vals, special):
    conn = get_db()
    conn.execute(
        """UPDATE ir_role_config SET es=?, ls=?, ns=?, area=?, home_process=?, match_vals=?, special=?
           WHERE id=?""",
        (es, ls, ns, area, home_process, match_vals, special, row_id),
    )
    conn.commit()
    conn.close()


# My L&D Scorecard — 6 fixed categories shown on the AM Overview, each rolling
# up one or more underlying sections into a single 0-100 compliance score.
SCORECARD_CATEGORIES = [
    ("safety_compliance", "Safety Compliance", ["planning_compliance", "compliance_safety"]),
    ("de_tech", "DE Technical Briefing", ["planning_de_tech"]),
    ("indirect_roles", "Indirect Roles", ["planning_indirect_roles", "staffing_indirect_coverage"]),
    ("cross_training", "Cross-Training", ["staffing_xt"]),
    ("instructor_mgmt", "Ambassador Availability", ["staffing_instructor"]),
    ("ambassador_readiness", "Ambassador Readiness", []),
    ("bts_compliance", "BTS Compliance", ["planning_bts"]),
]
SCORECARD_CATEGORY_MAP = {key: (label, sections) for key, label, sections in SCORECARD_CATEGORIES}

STATUS_OK = {"Compliant", "Graduated", "Complete", "Fully Covered", "On Track"}
STATUS_RISK = {"Due Soon", "In Progress", "Partially Covered"}
STATUS_GAP = {"Overdue", "Not Started", "Not Covered", "Gap"}


def init_global_db():
    """Creates the site-independent registry — the sites table itself,
    and who has cross-site (regional) access — if it doesn't exist yet.
    SCN2 is always guaranteed to exist as a registered site here, since
    it's the site every table already had data under before multi-site
    support existed; nothing needs to explicitly create it first."""
    conn = get_global_db()
    if USE_POSTGRES:
        conn.execute("SELECT pg_advisory_lock(727301727302)")
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sites (
                site_code TEXT PRIMARY KEY,
                site_name TEXT NOT NULL,
                region TEXT,
                created_by TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS global_admins (
                login TEXT PRIMARY KEY,
                added_by TEXT,
                added_at TEXT
            );
            """
        )
        existing = conn.execute("SELECT COUNT(*) as n FROM sites").fetchone()["n"]
        if existing == 0:
            conn.execute(
                "INSERT INTO sites (site_code, site_name, region, created_by, created_at) VALUES (?,?,?,?,?)",
                (DEFAULT_SITE_CODE, "SCN2", None, "system-seed", _now()),
            )
        conn.commit()
    finally:
        if USE_POSTGRES:
            conn.execute("SELECT pg_advisory_unlock(727301727302)")
        conn.close()


def get_sites():
    """Every registered site, for the site switcher and Weekly Business
    Review — always includes SCN2."""
    conn = get_global_db()
    rows = conn.execute("SELECT * FROM sites ORDER BY site_code").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_site(site_code):
    conn = get_global_db()
    row = conn.execute("SELECT * FROM sites WHERE site_code=?", ((site_code or "").strip().upper(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_site(site_code, site_name, region, created_by):
    """Registers a new site and gives it a completely empty database —
    no Indirect Role definitions, no Cross-Training standards, no
    Ambassador departments or processes, no roster, no uploaded data.
    Everything an admin sees after switching to this new site is
    exactly what init_db() creates from scratch: empty tables, ready to
    be configured through the same Definitions pages used for any
    other site (Indirect Roles, Cross-Training, Ambassador Definitions)
    — there's no separate 'setup wizard,' those pages already are one."""
    site_code = (site_code or "").strip().upper()
    site_name = (site_name or "").strip()
    if not re.match(r"^[A-Z0-9]{2,10}$", site_code):
        raise ValueError("Site code must be 2-10 letters/numbers, e.g. STR1")
    if not site_name:
        raise ValueError("Site name is required")
    if get_site(site_code):
        raise ValueError(f"'{site_code}' is already registered")

    conn = get_global_db()
    conn.execute(
        "INSERT INTO sites (site_code, site_name, region, created_by, created_at) VALUES (?,?,?,?,?)",
        (site_code, site_name, (region or "").strip() or None, created_by, _now()),
    )
    conn.commit()
    conn.close()

    # Build this site's empty tables under its own schema/file. init_db()
    # itself is written to operate on "whatever get_db() currently
    # points to," so the only thing needed here is to point it at the
    # new site before calling it — nothing about init_db() needs to
    # know multi-site support exists at all.
    previous_site = get_current_site()
    try:
        set_current_site(site_code)
        init_db()
    finally:
        set_current_site(previous_site)


def is_global_admin(login):
    """Whether this login has cross-site (regional) access — sees the
    site switcher's 'All sites' option and the Weekly Business Review,
    regardless of their per-site role at whichever site is current."""
    if not login:
        return False
    conn = get_global_db()
    row = conn.execute("SELECT 1 FROM global_admins WHERE login=?", (login.strip().lower(),)).fetchone()
    conn.close()
    return row is not None


def add_global_admin(login, added_by):
    login = (login or "").strip().lower()
    if not login:
        raise ValueError("A login is required")
    conn = get_global_db()
    conn.execute(
        "INSERT INTO global_admins (login, added_by, added_at) VALUES (?,?,?) ON CONFLICT (login) DO NOTHING",
        (login, added_by, _now()),
    )
    conn.commit()
    conn.close()


def remove_global_admin(login):
    conn = get_global_db()
    conn.execute("DELETE FROM global_admins WHERE login=?", ((login or "").strip().lower(),))
    conn.commit()
    conn.close()


def get_global_admins():
    conn = get_global_db()
    rows = conn.execute("SELECT * FROM global_admins ORDER BY login").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not USE_S3:
        os.makedirs(AMBASSADOR_DOCS_DIR, exist_ok=True)
    conn = get_db()
    if USE_POSTGRES:
        # Serializes schema setup across every gunicorn worker that might
        # call this at roughly the same moment right after a deploy (each
        # worker is a separate process with its own _db_initialized flag,
        # so several can race to run this concurrently). Without this,
        # two workers both running "CREATE INDEX IF NOT EXISTS" at the
        # same instant can still hit a duplicate-key error on Postgres's
        # internal catalog — the existence check and the creation aren't
        # atomic together under true concurrency, so IF NOT EXISTS alone
        # isn't race-proof. This blocks until free, then proceeds — by
        # which point whichever worker got there first has already
        # created everything, so every worker after it just finds
        # IF NOT EXISTS correctly no-op'ing with nothing left to race on.
        conn.execute("SELECT pg_advisory_lock(727301727301)")
    try:
        conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS org_map (
            employee_login TEXT PRIMARY KEY,
            full_name TEXT,
            fc TEXT,
            am_login TEXT,
            role_title TEXT
        );

        CREATE TABLE IF NOT EXISTS tracked_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            employee_login TEXT,
            full_name TEXT,
            fc TEXT,
            am_login TEXT,
            subcategory TEXT,
            status TEXT NOT NULL,
            due_date TEXT,
            value REAL,
            notes TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ti_section ON tracked_items(section);
        CREATE INDEX IF NOT EXISTS idx_ti_am ON tracked_items(am_login);
        CREATE INDEX IF NOT EXISTS idx_ti_fc ON tracked_items(fc);
        CREATE INDEX IF NOT EXISTS idx_ti_am_section ON tracked_items(am_login, section);

        CREATE TABLE IF NOT EXISTS weekly_plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            fc TEXT,
            planned_date TEXT NOT NULL,
            capacity INTEGER,
            attendee_count INTEGER,
            instructor TEXT,
            room TEXT,
            notes TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            fc TEXT,
            description TEXT NOT NULL,
            severity TEXT DEFAULT 'Medium',
            status TEXT DEFAULT 'Open',
            raised_by TEXT,
            created_at TEXT,
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS record_escalations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracked_item_id INTEGER NOT NULL REFERENCES tracked_items(id),
            phase INTEGER NOT NULL DEFAULT 1,
            target_login TEXT,
            target_role TEXT,
            status TEXT DEFAULT 'open',
            escalated_by TEXT,
            escalated_at TEXT,
            resolved_by TEXT,
            resolved_at TEXT,
            UNIQUE(tracked_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_rec_esc_target ON record_escalations(target_login, status);
        CREATE INDEX IF NOT EXISTS idx_rec_esc_role ON record_escalations(target_role, status);

        CREATE TABLE IF NOT EXISTS escalation_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_number TEXT NOT NULL UNIQUE,
            categories TEXT NOT NULL,
            description TEXT,
            target_login TEXT,
            stage TEXT,
            status TEXT DEFAULT 'open',
            created_by TEXT,
            created_at TEXT,
            resolved_by TEXT,
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_esc_ticket_target ON escalation_tickets(target_login, status);

        CREATE TABLE IF NOT EXISTS escalation_ticket_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES escalation_tickets(id),
            tracked_item_id INTEGER REFERENCES tracked_items(id),
            category TEXT
        );

        CREATE TABLE IF NOT EXISTS escalation_ticket_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL REFERENCES escalation_tickets(id),
            comment TEXT NOT NULL,
            comment_type TEXT DEFAULT 'comment',
            posted_by TEXT,
            posted_at TEXT,
            reviewed INTEGER DEFAULT 0,
            reviewed_by TEXT,
            reviewed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_esc_comment_ticket ON escalation_ticket_comments(ticket_id);

        CREATE TABLE IF NOT EXISTS trainer_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trainer_login TEXT NOT NULL,
            full_name TEXT,
            fc TEXT,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            period TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ops_structure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_title TEXT NOT NULL,
            person_name TEXT,
            reports_to TEXT,
            scope_note TEXT,
            sort_order INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            section TEXT,
            uploaded_at TEXT,
            row_count INTEGER
        );

        CREATE TABLE IF NOT EXISTS trainings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT,
            validity_note TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS training_topic_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            training_id INTEGER NOT NULL REFERENCES trainings(id),
            section TEXT NOT NULL,
            topic TEXT NOT NULL,
            UNIQUE(training_id, section, topic)
        );

        CREATE TABLE IF NOT EXISTS xt_hours (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fc TEXT,
            fclm_mapped TEXT,
            employee_id TEXT,
            employee_login TEXT,
            full_name TEXT,
            supervisor_login TEXT,
            proficiency_status TEXT,
            merged_function TEXT,
            job_title TEXT,
            active_status TEXT,
            trained_status TEXT,
            fclm_area TEXT,
            shift_pattern TEXT,
            shift TEXT,
            hours_60 REAL,
            hours_90 REAL,
            hours_180 REAL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ir_roster (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT, management_area_id TEXT, full_name TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ir_roster_login ON ir_roster(login);

        CREATE TABLE IF NOT EXISTS ir_dashboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT, full_name TEXT, shift TEXT, area TEXT, home_process TEXT,
            indirect_role TEXT, indirect_role_status TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ir_dash_login ON ir_dashboard(login);
        CREATE INDEX IF NOT EXISTS idx_ir_dash_role ON ir_dashboard(indirect_role);

        CREATE TABLE IF NOT EXISTS ir_umbrella (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT, full_name TEXT, shift TEXT, area TEXT, home_process TEXT,
            certificate_title TEXT, certificate_status TEXT, certificate_earned_date TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ir_umb_login ON ir_umbrella(login);
        CREATE INDEX IF NOT EXISTS idx_ir_umb_cert ON ir_umbrella(certificate_title);

        CREATE TABLE IF NOT EXISTS ir_learn (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT, full_name TEXT, shift TEXT, training_name TEXT, status TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ir_learn_login ON ir_learn(login);
        CREATE INDEX IF NOT EXISTS idx_xt_hours_lookup ON xt_hours(fclm_mapped, merged_function, shift, employee_login);
        CREATE INDEX IF NOT EXISTS idx_xt_hours_supervisor_lower ON xt_hours(lower(supervisor_login));

        CREATE TABLE IF NOT EXISTS ir_role_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sort_order INTEGER,
            section TEXT, role TEXT, src_type TEXT, area TEXT, home_process TEXT,
            match_vals TEXT, es INTEGER, ls INTEGER, ns INTEGER, special TEXT
        );

        CREATE TABLE IF NOT EXISTS xt_standards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sort_order INTEGER,
            scenario TEXT NOT NULL DEFAULT 'general',
            std_key TEXT NOT NULL,
            label TEXT, xt_group TEXT, source TEXT, target TEXT,
            pct_early REAL, pct_late REAL, pct_night REAL
        );
        CREATE INDEX IF NOT EXISTS idx_xt_standards_scenario ON xt_standards(scenario);

        CREATE TABLE IF NOT EXISTS internal_xt_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sort_order INTEGER,
            direction TEXT NOT NULL,
            label TEXT NOT NULL,
            source_dept TEXT,
            target_process TEXT,
            target_early INTEGER, target_late INTEGER, target_night INTEGER
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_by TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS xt_exclusions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_login TEXT NOT NULL,
            merged_function TEXT NOT NULL,
            reason TEXT NOT NULL,
            excluded_by TEXT,
            excluded_at TEXT,
            UNIQUE(employee_login, merged_function)
        );

        CREATE TABLE IF NOT EXISTS ambassador_process_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department TEXT NOT NULL,
            process TEXT NOT NULL,
            added_by TEXT,
            added_at TEXT,
            UNIQUE(department, process)
        );

        CREATE TABLE IF NOT EXISTS ambassador_training_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_key TEXT NOT NULL,
            topic TEXT NOT NULL,
            employee_login TEXT NOT NULL,
            employee_name TEXT,
            ambassador_login TEXT,
            ambassador_name TEXT,
            shift TEXT NOT NULL,
            planned_date TEXT NOT NULL,
            planned_time TEXT,
            planned_by TEXT,
            planned_at TEXT,
            status TEXT DEFAULT 'planned'
        );
        CREATE INDEX IF NOT EXISTS idx_amb_sessions_by_planner ON ambassador_training_sessions(planned_by, planned_date);

        CREATE TABLE IF NOT EXISTS training_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shift TEXT NOT NULL,
            week_start TEXT NOT NULL,
            day_index INTEGER NOT NULL,
            training_id INTEGER REFERENCES trainings(id),
            start_time TEXT,
            capacity INTEGER,
            instructor TEXT,
            room TEXT,
            notes TEXT,
            created_by TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_slots_week ON training_slots(week_start, shift);

        CREATE TABLE IF NOT EXISTS slot_attendees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_id INTEGER NOT NULL REFERENCES training_slots(id),
            employee_login TEXT NOT NULL,
            full_name TEXT,
            fc TEXT,
            added_by TEXT,
            added_at TEXT,
            UNIQUE(slot_id, employee_login)
        );

        CREATE TABLE IF NOT EXISTS trainers (
            login TEXT PRIMARY KEY,
            full_name TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS trainer_am_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trainer_login TEXT NOT NULL REFERENCES trainers(login),
            am_login TEXT NOT NULL,
            assigned_at TEXT,
            UNIQUE(trainer_login, am_login)
        );

        CREATE TABLE IF NOT EXISTS user_roles (
            login TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            shift TEXT,
            assigned_by TEXT,
            assigned_at TEXT
        );

        CREATE TABLE IF NOT EXISTS role_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL,
            requested_role TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            requested_at TEXT,
            decided_by TEXT,
            decided_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ambassadors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department TEXT NOT NULL,
            shift TEXT NOT NULL,
            login TEXT NOT NULL,
            full_name TEXT,
            active INTEGER DEFAULT 1,
            added_by TEXT,
            added_at TEXT,
            delisted_by TEXT,
            delisted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ambassadors_shift_dept ON ambassadors(shift, department, active);

        CREATE TABLE IF NOT EXISTS ambassador_indirect_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ambassador_id INTEGER NOT NULL,
            ir_role_config_id INTEGER NOT NULL,
            assigned_by TEXT,
            assigned_at TEXT,
            UNIQUE(ambassador_id, ir_role_config_id)
        );

        CREATE TABLE IF NOT EXISTS de_tech_role_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ir_role_config_id INTEGER NOT NULL,
            de_tech_topic TEXT NOT NULL,
            mapped_by TEXT,
            mapped_at TEXT,
            UNIQUE(ir_role_config_id, de_tech_topic)
        );

        CREATE TABLE IF NOT EXISTS user_department_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL,
            department TEXT NOT NULL,
            assigned_by TEXT,
            assigned_at TEXT,
            UNIQUE(login, department)
        );

        CREATE TABLE IF NOT EXISTS ambassador_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            department TEXT NOT NULL,
            shift TEXT NOT NULL,
            target INTEGER NOT NULL DEFAULT 0,
            UNIQUE(department, shift)
        );

        CREATE TABLE IF NOT EXISTS olr_weekly_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT NOT NULL,
            week_start TEXT NOT NULL,
            metric_key TEXT NOT NULL,
            value REAL,
            uploaded_by TEXT,
            uploaded_at TEXT,
            UNIQUE(login, week_start, metric_key)
        );
        CREATE INDEX IF NOT EXISTS idx_olr_login_week ON olr_weekly_metrics(login, week_start);

        CREATE TABLE IF NOT EXISTS ambassador_meetings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start TEXT NOT NULL,
            shift TEXT NOT NULL,
            group_key TEXT NOT NULL,
            notes TEXT,
            created_by TEXT,
            created_at TEXT,
            UNIQUE(week_start, shift, group_key)
        );

        CREATE TABLE IF NOT EXISTS ambassador_meeting_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES ambassador_meetings(id),
            ambassador_id INTEGER NOT NULL REFERENCES ambassadors(id),
            login TEXT,
            full_name TEXT,
            department TEXT,
            attended INTEGER DEFAULT 0,
            notes TEXT,
            recorded_by TEXT,
            recorded_at TEXT,
            UNIQUE(meeting_id, ambassador_id)
        );

        CREATE TABLE IF NOT EXISTS ambassador_meeting_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id INTEGER NOT NULL REFERENCES ambassador_meetings(id),
            stored_filename TEXT NOT NULL,
            original_name TEXT NOT NULL,
            content_type TEXT,
            uploaded_by TEXT,
            uploaded_at TEXT
        );
        """
    )
        # Safe migrations for columns added after the tables above first
        # shipped — CREATE TABLE IF NOT EXISTS doesn't retrofit existing
        # DBs, so these ALTERs run every startup and just no-op (via the
        # duplicate-column error) once already applied.
        for table, column, coltype in [
            ("tracked_items", "shift", "TEXT"),
            ("user_roles", "shift", "TEXT"),
            ("user_roles", "reports_to", "TEXT"),
            ("user_roles", "department", "TEXT"),
            ("user_roles", "full_name", "TEXT"),
            ("user_roles", "title", "TEXT"),
            ("user_roles", "activated_at", "TEXT"),
            ("escalation_tickets", "phase", "INTEGER DEFAULT 1"),
            ("escalation_tickets", "concerning_manager", "TEXT"),
            ("escalation_tickets", "due_date", "TEXT"),
            ("ir_dashboard", "shift_pattern", "TEXT"),
            ("ir_umbrella", "shift_pattern", "TEXT"),
            ("ir_learn", "shift_pattern", "TEXT"),
            ("ir_dashboard", "manager_login", "TEXT"),
            ("ir_umbrella", "manager_login", "TEXT"),
            ("ir_learn", "manager_login", "TEXT"),
            ("ir_dashboard", "hours_90", "TEXT"),
            ("slot_attendees", "flagged", "INTEGER DEFAULT 0"),
            ("slot_attendees", "flag_reason", "TEXT"),
            ("ambassador_meetings", "status", "TEXT DEFAULT 'held'"),
            ("ambassador_meetings", "cancel_reason", "TEXT"),
            ("ambassador_meetings", "marked_by", "TEXT"),
            ("ambassador_meetings", "marked_at", "TEXT"),
            ("ambassador_meeting_attendance", "attendance_status", "TEXT"),
            ("escalation_tickets", "tickety_ticket_id", "TEXT"),
            ("escalation_tickets", "tickety_sync_error", "TEXT"),
            ("ambassadors", "is_process_ambassador", "INTEGER DEFAULT 1"),
            ("xt_hours", "last_date_on_process", "TEXT"),
            ("xt_hours", "raw_trained_status", "TEXT"),
            ("training_slots", "status", "TEXT DEFAULT 'approved'"),
            ("training_slots", "target_topic", "TEXT"),
            ("training_slots", "metric_key", "TEXT"),
            ("training_slots", "approved_by", "TEXT"),
            ("ambassadors", "is_indirect_roles_ambassador", "INTEGER DEFAULT 0"),
        ]:
            if USE_POSTGRES:
                # Postgres supports IF NOT EXISTS directly — no exception
                # handling needed, and no risk of masking a real error.
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
            else:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                except sqlite3.OperationalError:
                    pass

        # Re-uploading the same weekly compliance/planning export used to
        # add a fresh duplicate row every time instead of updating the
        # existing one — this cleans up anything already duplicated from
        # that (safe to run every startup: a no-op once there's nothing
        # left to merge) and then locks in a uniqueness rule so it can't
        # happen again; the two CSV ingesters below now upsert against
        # this key instead of blindly inserting.
        _dedupe_tracked_items(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ti_unique_key ON tracked_items(section, employee_login, subcategory)"
        )
        row = conn.execute("SELECT COUNT(*) as n FROM ir_role_config").fetchone()
        if row["n"] == 0 and get_current_site() == DEFAULT_SITE_CODE:
            # SCN2-specific seed only — a new site starts with zero
            # Indirect Role definitions and is configured from scratch
            # through the Indirect Roles Definitions page, since its
            # roles may have nothing in common with SCN2's.
            for i, (section, role, src, area, hp, vals, es, ls, ns, special) in enumerate(IR_ROLE_CONFIG):
                conn.execute(
                    """INSERT INTO ir_role_config (sort_order, section, role, src_type, area, home_process, match_vals, es, ls, ns, special)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (i, section, role, src, area, hp, vals, es, ls, ns, special),
                )
        else:
            # Corrections to role definitions that were wrong from the
            # original seed (confirmed against real IR Dashboard/Learn/
            # Umbrella data and the source workbook's own config sheet)
            # — applied only to rows still sitting at the exact old
            # broken value, so a manual edit made since first seeding is
            # never overwritten.
            for section, role, old_src, old_vals, old_hp, new_src, new_vals, new_hp in IR_ROLE_CONFIG_CORRECTIONS:
                conn.execute(
                    """UPDATE ir_role_config SET src_type=?, match_vals=?, home_process=?
                       WHERE section=? AND role=? AND src_type=? AND match_vals=? AND home_process=?""",
                    (new_src, new_vals, new_hp, section, role, old_src, old_vals, old_hp),
                )
        # Same idea for any AM whose department got auto-filled with an
        # informal alias (Chutings, IC/QA/CS, ...) before department
        # normalization existed — canonicalize it now so Ambassador
        # matching actually finds them. Only touches rows still sitting
        # at the raw alias value, so a manually-entered department is
        # never overwritten.
        for canonical, group in DEPARTMENT_ALIAS_GROUPS.items():
            for alias in group:
                if alias == canonical:
                    continue
                conn.execute(
                    "UPDATE user_roles SET department=? WHERE department=?",
                    (canonical, alias),
                )
                # Same for ambassador records themselves — the "Add
                # ambassador" form only ever offers canonical names, but
                # an already-stored row from before this alias system
                # existed (or added some other way) could still be
                # sitting on the raw alias.
                conn.execute(
                    "UPDATE ambassadors SET department=? WHERE department=?",
                    (canonical, alias),
                )
        xt_row = conn.execute("SELECT COUNT(*) as n FROM xt_standards").fetchone()
        if xt_row["n"] == 0 and get_current_site() == DEFAULT_SITE_CODE:
            # SCN2-specific seed only — a new site starts with no
            # Cross-Training standards; its own paths are set up through
            # Cross-Training Definitions.
            for scenario in ("general", "q1", "q2", "q3", "q4"):
                for i, std in enumerate(XT_STANDARDS_SEED):
                    conn.execute(
                        """INSERT INTO xt_standards (sort_order, scenario, std_key, label, xt_group, source, target, pct_early, pct_late, pct_night)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (i, scenario, std["key"], std["label"], std["group"], ",".join(std["source"]), std["target"],
                         std["pct"]["early"], std["pct"]["late"], std["pct"]["night"]),
                    )
        internal_xt_row = conn.execute("SELECT COUNT(*) as n FROM internal_xt_targets").fetchone()
        if internal_xt_row["n"] == 0 and get_current_site() == DEFAULT_SITE_CODE:
            # SCN2-specific seed only — see the ir_role_config/xt_standards
            # seeds above for the same reasoning.
            for i, (direction, label, source_dept, target_process, es, ls, ns) in enumerate(INTERNAL_XT_TARGETS_SEED):
                conn.execute(
                    """INSERT INTO internal_xt_targets (sort_order, direction, label, source_dept, target_process, target_early, target_late, target_night)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (i, direction, label, source_dept, target_process, es, ls, ns),
                )
        amb_row = conn.execute("SELECT COUNT(*) as n FROM ambassador_targets").fetchone()
        if amb_row["n"] == 0 and get_current_site() == DEFAULT_SITE_CODE:
            # SCN2-specific seed only — AMBASSADOR_DEPARTMENTS is
            # currently a hardcoded list of SCN2's department names, so
            # pre-creating zero-target rows for them on a new site would
            # be misleading (those department names may not even exist
            # there). A new site currently has no Ambassador department
            # list of its own yet — see the "Known limitation" note on
            # create_site's docstring.
            for department in AMBASSADOR_DEPARTMENTS:
                for shift in SHIFTS:
                    conn.execute(
                        "INSERT INTO ambassador_targets (department, shift, target) VALUES (?,?,0)",
                        (department, shift),
                    )
        amb_proc_row = conn.execute("SELECT COUNT(*) as n FROM ambassador_process_definitions").fetchone()
        if amb_proc_row["n"] == 0 and get_current_site() == DEFAULT_SITE_CODE:
            # One-time seed from the processes this app originally shipped
            # with hardcoded — preserves existing behavior for departments
            # that were already being tracked, without assuming anything
            # about departments (Ship Dock, ICQA) that never had a process
            # list defined; those simply start with none, same as before,
            # until an admin adds some via Ambassador Definitions. Only
            # for SCN2 — a new site starts with zero process definitions,
            # same reasoning as the seeds above.
            for department, processes in AMBASSADOR_DEPT_PROCESSES.items():
                for process in processes:
                    conn.execute(
                        "INSERT INTO ambassador_process_definitions (department, process, added_by, added_at) VALUES (?,?,?,?)",
                        (department, process, "system-seed", _now()),
                    )
        conn.commit()
    finally:
        if USE_POSTGRES:
            conn.execute("SELECT pg_advisory_unlock(727301727301)")
        conn.close()


def _dedupe_tracked_items(conn):
    rows = conn.execute(
        "SELECT id, section, employee_login, subcategory FROM tracked_items "
        "WHERE employee_login IS NOT NULL AND subcategory IS NOT NULL"
    ).fetchall()
    groups = {}
    for r in rows:
        key = (r["section"], r["employee_login"], r["subcategory"])
        groups.setdefault(key, []).append(r["id"])
    for ids in groups.values():
        if len(ids) <= 1:
            continue
        ids.sort()
        keep_id = ids[-1]  # highest id = most recently inserted
        for dupe_id in ids[:-1]:
            # record_escalations.tracked_item_id is UNIQUE, so if the
            # survivor already has its own escalation, the duplicate's
            # can't be merged onto it — drop it rather than error.
            survivor_has_esc = conn.execute(
                "SELECT 1 FROM record_escalations WHERE tracked_item_id=?", (keep_id,)
            ).fetchone()
            if survivor_has_esc:
                conn.execute("DELETE FROM record_escalations WHERE tracked_item_id=?", (dupe_id,))
            else:
                conn.execute(
                    "UPDATE record_escalations SET tracked_item_id=? WHERE tracked_item_id=?",
                    (keep_id, dupe_id),
                )
            conn.execute("DELETE FROM tracked_items WHERE id=?", (dupe_id,))


def _now():
    return datetime.utcnow().isoformat()


def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


# --------------------------------------------------------------- items ----

def add_tracked_item(section, employee_login, full_name, fc, am_login,
                      subcategory, status, due_date, value, notes):
    conn = get_db()
    conn.execute(
        """INSERT INTO tracked_items
           (section, employee_login, full_name, fc, am_login, subcategory,
            status, due_date, value, notes, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (section, employee_login, full_name, fc, am_login, subcategory,
         status, due_date, value, notes, _now()),
    )
    conn.commit()
    conn.close()


def ingest_items_csv(section, file_bytes, filename):
    """Generic CSV ingestion for tracked_items. Expected columns (case
    insensitive, extras ignored): employee_login, full_name, fc, am_login,
    subcategory, status, due_date, value, notes."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()
    cur = conn.cursor()
    n = 0
    now = _now()
    for row in reader:
        row = {k.strip().lower(): v for k, v in row.items() if k}
        status = _clean(row.get("status")) or "Not Started"
        val = row.get("value")
        try:
            val = float(val) if val not in (None, "") else None
        except ValueError:
            val = None
        cur.execute(
            """INSERT INTO tracked_items
               (section, employee_login, full_name, fc, am_login, subcategory,
                status, due_date, value, notes, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (section, employee_login, subcategory) DO UPDATE SET
                 full_name=excluded.full_name, fc=excluded.fc, am_login=excluded.am_login,
                 status=excluded.status, due_date=excluded.due_date, value=excluded.value,
                 notes=excluded.notes, updated_at=excluded.updated_at""",
            (
                section,
                _clean(row.get("employee_login")),
                _clean(row.get("full_name")),
                _clean(row.get("fc")),
                _clean(row.get("am_login")),
                _clean(row.get("subcategory")),
                status,
                _clean(row.get("due_date")),
                val,
                _clean(row.get("notes")),
                now,
            ),
        )
        emp = _clean(row.get("employee_login"))
        if emp:
            cur.execute(
                """INSERT INTO org_map (employee_login, full_name, fc, am_login, role_title)
                   VALUES (?,?,?,?,NULL)
                   ON CONFLICT(employee_login) DO UPDATE SET
                     full_name=COALESCE(excluded.full_name, org_map.full_name),
                     fc=COALESCE(excluded.fc, org_map.fc),
                     am_login=COALESCE(excluded.am_login, org_map.am_login)""",
                (emp, _clean(row.get("full_name")), _clean(row.get("fc")), _clean(row.get("am_login"))),
            )
        n += 1
    conn.execute(
        "INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
        (filename, section, now, n),
    )
    conn.commit()
    conn.close()
    return n


def ingest_safety_compliance_csv(file_bytes, filename):
    """Ingests the Amazon safety-training-compliance export directly (the
    'Emp Login' / 'Detailed Topic' / 'Expiry Date In Use' style columns),
    mapping it onto the same tracked_items model as everything else so it
    rolls up into scorecards, the AM Overview and section_detail for free.
    Compliant Yes/No -> status; Expiry Date In Use -> due_date; Supervisor
    Login -> am_login (the AM/supervisor who owns that associate)."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()
    cur = conn.cursor()
    n = 0
    now = _now()
    today = date.today().isoformat()
    for row in reader:
        row = {(k or "").strip(): v for k, v in row.items() if k}
        emp_login = _clean(row.get("Emp Login"))
        if not emp_login:
            continue
        full_name = _clean(row.get("Full Name"))
        if full_name and "," in full_name:
            last, _, first = full_name.partition(",")
            full_name = f"{first.strip()} {last.strip()}".strip()
        fc = _clean(row.get("FC"))
        am_login = _clean(row.get("Supervisor Login"))
        topic = _clean(row.get("Detailed Topic")) or _clean(row.get("Main Topic"))
        compliant = (_clean(row.get("Compliant Yes/No")) or "").lower() == "yes"
        due_raw = _clean(row.get("Expiry Date In Use"))
        due_date = due_raw[:10] if due_raw else None
        if compliant:
            status = "Compliant"
        elif due_date and due_date < today:
            status = "Overdue"
        elif due_date:
            status = "Due Soon"
        else:
            status = "Not Started"
        priority = _clean(row.get("Training Priority"))
        job_title = _clean(row.get("Job Title"))
        business_title = _clean(row.get("Business Title"))
        shift = _derive_shift(_clean(row.get("Shift Pattern")))
        notes_bits = [b for b in [business_title or job_title, f"Priority: {priority}" if priority else None] if b]
        cur.execute(
            """INSERT INTO tracked_items
               (section, employee_login, full_name, fc, am_login, subcategory,
                status, due_date, value, notes, shift, updated_at)
               VALUES ('compliance_safety',?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (section, employee_login, subcategory) DO UPDATE SET
                 full_name=excluded.full_name, fc=excluded.fc, am_login=excluded.am_login,
                 status=excluded.status, due_date=excluded.due_date, notes=excluded.notes,
                 shift=excluded.shift, updated_at=excluded.updated_at""",
            (emp_login, full_name, fc, am_login, topic, status, due_date, None,
             " · ".join(notes_bits) or None, shift, now),
        )
        cur.execute(
            """INSERT INTO org_map (employee_login, full_name, fc, am_login, role_title)
               VALUES (?,?,?,?,?)
               ON CONFLICT(employee_login) DO UPDATE SET
                 full_name=COALESCE(excluded.full_name, org_map.full_name),
                 fc=COALESCE(excluded.fc, org_map.fc),
                 am_login=COALESCE(excluded.am_login, org_map.am_login),
                 role_title=COALESCE(excluded.role_title, org_map.role_title)""",
            (emp_login, full_name, fc, am_login, business_title),
        )
        n += 1
    conn.execute(
        "INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
        (filename, "compliance_safety", now, n),
    )
    conn.commit()
    conn.close()
    return n


BTS_STATUS_MAP = {
    "completed on time": "Compliant",
    "completed late": "Compliant",
    "overdue less than a week": "Due Soon",
    "overdue more than a week": "Overdue",
}


def ingest_bts_csv(file_bytes, filename):
    """Ingests the BTS Touchpoint & Course Compliance export directly
    (the 'Login' / 'Manager Login' / 'Course Name' / 'Course Status'
    style columns) — one row per employee per course or touchpoint, so
    the same employee legitimately appears many times. Course Name is
    used as the subcategory rather than Touchpoint Name, since
    Touchpoint Name is blank for the great majority of rows (it's only
    set on the handful of milestone rows — BTS Touchpoint 1/2,
    Onboarding, Graduation) while Course Name is always populated.
    Rows where 'Is now in the FC?' is No are skipped — tracking
    compliance for someone no longer on site isn't actionable, and
    including them would drag every rollup down with stale history."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()
    cur = conn.cursor()
    n = 0
    now = _now()
    for row in reader:
        row = _ci_row(row)
        if (_clean(row.get("is now in the fc?")) or "").strip().lower() == "no":
            continue
        emp_login = _clean(row.get("login"))
        course = _clean(row.get("course name")) or _clean(row.get("touchpoint name"))
        if not emp_login or not course:
            continue
        fc = _clean(row.get("fc"))
        am_login = _clean(row.get("manager login"))
        raw_status = _clean(row.get("course status"))
        status = BTS_STATUS_MAP.get((raw_status or "").strip().lower(), raw_status or "Not Started")
        due_raw = _clean(row.get("training recommendation date"))
        due_date = due_raw[:10] if due_raw else None
        shift = _derive_shift(_clean(row.get("shift code")))
        cur.execute(
            """INSERT INTO tracked_items
               (section, employee_login, full_name, fc, am_login, subcategory,
                status, due_date, value, notes, shift, updated_at)
               VALUES ('planning_bts',?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (section, employee_login, subcategory) DO UPDATE SET
                 fc=excluded.fc, am_login=excluded.am_login, status=excluded.status,
                 due_date=excluded.due_date, notes=excluded.notes, shift=excluded.shift,
                 updated_at=excluded.updated_at""",
            (emp_login, None, fc, am_login, course, status, due_date, None, raw_status, shift, now),
        )
        cur.execute(
            """INSERT INTO org_map (employee_login, full_name, fc, am_login, role_title)
               VALUES (?,?,?,?,NULL)
               ON CONFLICT(employee_login) DO UPDATE SET
                 fc=COALESCE(excluded.fc, org_map.fc),
                 am_login=COALESCE(excluded.am_login, org_map.am_login)""",
            (emp_login, None, fc, am_login),
        )
        n += 1
    conn.execute(
        "INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
        (filename, "planning_bts", now, n),
    )
    conn.commit()
    conn.close()
    return n


def ingest_de_tech_csv(file_bytes, filename):
    """Ingests the DE Technical Briefing export directly. Handles two
    column naming generations, since the export was restructured at
    some point and both are still plausible to see: the original
    ('Employee Login' / 'Supervisor' / 'Compliant' / 'Refresher needed'
    / 'Priority' / 'Shift pattern') and the current one ('Login' /
    'Manager' / 'Compliant Yes/No' / 'Training Validity' / 'Valid Days'
    / 'Training Priority' / 'Shift'). DE Tech is normally the managers'
    own responsibility on the floor rather than something L&D centrally
    tracks, but the data still rolls up into Reporting/AM Overview the
    same as every other section — the Weekly Training Plan is what
    actually restricts itself to Safety Compliance plus the two DE Tech
    exceptions (see DE_TECH_CENTRALLY_SCHEDULED), not the ingestion here."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()
    cur = conn.cursor()
    n = 0
    now = _now()
    today_d = date.today()
    today = today_d.isoformat()
    for row in reader:
        row = _ci_row(row)
        emp_login = _clean(row.get("login")) or _clean(row.get("employee login"))
        if not emp_login:
            continue
        fc = _clean(row.get("fc"))
        am_login = _clean(row.get("manager")) or _clean(row.get("supervisor"))
        topic = _clean(row.get("technical briefing"))
        compliant_raw = _clean(row.get("compliant yes/no")) or _clean(row.get("compliant"))
        compliant = (compliant_raw or "").strip().lower() == "yes"

        # Current format: a signed day-count (negative = overdue) is the
        # most reliable way to get an actual due_date, since "Training
        # Validity" is a date RANGE ("2025-08-12 to 2026-08-12") rather
        # than a single date — parsing the range's end would work too,
        # but the day-count is already exactly what's needed with no
        # string-splitting to get wrong.
        valid_days_raw = _clean(row.get("valid days"))
        due_date = None
        if valid_days_raw is not None:
            try:
                due_date = (today_d + timedelta(days=int(float(valid_days_raw)))).isoformat()
            except ValueError:
                due_date = None
        if due_date is None:
            # Original format fallback: a single "Refresher needed" date.
            due_raw = _clean(row.get("refresher needed"))
            due_date = due_raw[:10] if due_raw else None

        priority = _clean(row.get("training priority")) or _clean(row.get("priority"))
        priority_norm = (priority or "").lower()
        # The priority tier is the most authoritative signal when present
        # (it already correctly distinguishes "compliant but expiring
        # soon" from "compliant with plenty of time left" — Compliant
        # Yes/No alone can't make that distinction, since both read
        # "Yes"), so it's checked before falling back to the Yes/No flag
        # or a raw due-date comparison for older exports without it.
        if "high" in priority_norm or "expired" in priority_norm:
            status = "Overdue"
        elif "medium" in priority_norm:
            status = "Due Soon"
        elif compliant_raw is not None:
            status = "Compliant" if compliant else "Overdue"
        elif due_date and due_date < today:
            status = "Overdue"
        elif due_date:
            status = "Due Soon"
        else:
            status = "Not Started"

        shift_raw = _clean(row.get("shift")) or _clean(row.get("shift pattern"))
        shift = _derive_shift(shift_raw)
        cur.execute(
            """INSERT INTO tracked_items
               (section, employee_login, full_name, fc, am_login, subcategory,
                status, due_date, value, notes, shift, updated_at)
               VALUES ('planning_de_tech',?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (section, employee_login, subcategory) DO UPDATE SET
                 fc=excluded.fc, am_login=excluded.am_login, status=excluded.status,
                 due_date=excluded.due_date, notes=excluded.notes, shift=excluded.shift,
                 updated_at=excluded.updated_at""",
            (emp_login, None, fc, am_login, topic, status, due_date, None,
             f"Priority: {priority}" if priority else None, shift, now),
        )
        cur.execute(
            """INSERT INTO org_map (employee_login, full_name, fc, am_login, role_title)
               VALUES (?,?,?,?,?)
               ON CONFLICT(employee_login) DO UPDATE SET
                 fc=COALESCE(excluded.fc, org_map.fc),
                 am_login=COALESCE(excluded.am_login, org_map.am_login)""",
            (emp_login, None, fc, am_login, None),
        )
        n += 1
    conn.execute(
        "INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
        (filename, "planning_de_tech", now, n),
    )
    conn.commit()
    conn.close()
    return n


# ------------------------------------------------------- Cross-Training --
# "Hours on Function" export: one row per (employee, function) they have
# proficiency/hours data for — long format, not the tracked_items shape,
# since it's about proven proficiency across many processes at once
# rather than a single compliance status.

def ingest_xt_hours_csv(file_bytes, filename):
    """Full-replace ingest — each upload is a fresh census, not an
    incremental delta, so the table is cleared and reloaded each time.
    Column lookups are case-insensitive (via _ci_row) — the source
    export's header casing isn't something this code should have to
    assume, and a mismatch here previously meant a column would fail
    to match silently rather than error, leaving that field NULL for
    every row with no indication anything was wrong."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    conn = get_db()
    cur = conn.cursor()
    now = _now()
    cur.execute("DELETE FROM xt_hours")
    excluded_pairs = {
        (r["employee_login"].lower(), r["merged_function"].lower())
        for r in conn.execute("SELECT employee_login, merged_function FROM xt_exclusions").fetchall()
    }
    n = 0
    for row in reader:
        row = _ci_row(row)
        employee_login = _clean(row.get("employee login"))
        if not employee_login:
            continue
        shift_pattern = _clean(row.get("shift"))
        merged_function = _clean(row.get("merged functions"))
        raw_trained_status = _clean(row.get("trained status"))
        # An exclusion overrides whatever the source data says — it
        # exists specifically to say "don't count this person as
        # trained here regardless of what the upload claims" — but the
        # original value is kept in raw_trained_status so removing the
        # exclusion later can restore it without needing a re-upload.
        is_excluded = merged_function and (employee_login.lower(), merged_function.lower()) in excluded_pairs
        trained_status = "Excluded" if is_excluded else raw_trained_status

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        cur.execute(
            """INSERT INTO xt_hours
               (fc, fclm_mapped, employee_id, employee_login, full_name, supervisor_login,
                proficiency_status, merged_function, job_title, active_status, trained_status, raw_trained_status,
                fclm_area, shift_pattern, shift, hours_60, hours_90, hours_180, last_date_on_process, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _clean(row.get("fc")), _clean(row.get("fclm mapped")), _clean(row.get("employee id")),
                employee_login, _clean(row.get("user name")), _clean(row.get("supervisor login")),
                _clean(row.get("proficiency status")), merged_function,
                _clean(row.get("job title")), _clean(row.get("active status")), trained_status, raw_trained_status,
                _clean(row.get("fclm area")), shift_pattern, _derive_shift(shift_pattern),
                _num(row.get("total_hours_60")), _num(row.get("total_hours_90")), _num(row.get("total_hours_180")),
                (_clean(row.get("last date on process")) or "")[:10] or None,
                now,
            ),
        )
        n += 1
    cur.execute(
        "INSERT INTO uploads (filename, section, uploaded_at, row_count) VALUES (?,?,?,?)",
        (filename, "xt_hours", now, n),
    )
    conn.commit()
    conn.close()
    return n


def get_xt_exclusions():
    """Every active exclusion, with the associate's current manager,
    home department, and shift joined in from xt_hours (their most
    recent-looking row for that process, if any — an exclusion can
    exist for someone not currently in the uploaded data at all, e.g.
    entered ahead of the next upload)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM xt_exclusions ORDER BY excluded_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        profile = conn.execute(
            "SELECT full_name, fclm_mapped, supervisor_login, shift FROM xt_hours WHERE lower(employee_login)=? AND lower(merged_function)=? LIMIT 1",
            (d["employee_login"].lower(), d["merged_function"].lower()),
        ).fetchone()
        d["full_name"] = profile["full_name"] if profile else None
        d["fclm_mapped"] = profile["fclm_mapped"] if profile else None
        d["supervisor_login"] = profile["supervisor_login"] if profile else None
        d["shift"] = profile["shift"] if profile else None
        out.append(d)
    conn.close()
    return out


def add_xt_exclusion(employee_login, merged_function, reason, excluded_by):
    """Excludes one associate from counting as Trained on one process —
    takes effect immediately against whatever's currently loaded in
    xt_hours (not just future uploads), by overwriting trained_status
    to 'Excluded' for any matching row right now. The real status is
    kept in raw_trained_status so removing the exclusion can restore
    it without needing a re-upload."""
    employee_login = (employee_login or "").strip()
    merged_function = (merged_function or "").strip()
    reason = (reason or "").strip()
    if not employee_login or not merged_function or not reason:
        raise ValueError("employee_login, merged_function, and reason are all required")
    conn = get_db()
    conn.execute(
        """INSERT INTO xt_exclusions (employee_login, merged_function, reason, excluded_by, excluded_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT (employee_login, merged_function) DO UPDATE SET
             reason=excluded.reason, excluded_by=excluded.excluded_by, excluded_at=excluded.excluded_at""",
        (employee_login, merged_function, reason, excluded_by, _now()),
    )
    conn.execute(
        "UPDATE xt_hours SET trained_status='Excluded' WHERE lower(employee_login)=? AND lower(merged_function)=?",
        (employee_login.lower(), merged_function.lower()),
    )
    conn.commit()
    conn.close()


def remove_xt_exclusion(exclusion_id):
    """Removes an exclusion and immediately restores the affected
    xt_hours row(s) to their real trained_status from raw_trained_status
    — without this, the row would stay stuck as 'Excluded' until the
    next upload happened to overwrite it."""
    conn = get_db()
    row = conn.execute("SELECT * FROM xt_exclusions WHERE id=?", (exclusion_id,)).fetchone()
    if not row:
        conn.close()
        return
    conn.execute("DELETE FROM xt_exclusions WHERE id=?", (exclusion_id,))
    conn.execute(
        "UPDATE xt_hours SET trained_status=raw_trained_status WHERE lower(employee_login)=? AND lower(merged_function)=? AND trained_status='Excluded'",
        (row["employee_login"].lower(), row["merged_function"].lower()),
    )
    conn.commit()
    conn.close()


def xt_hours_freshness():
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) n FROM xt_hours").fetchone()
    up = conn.execute(
        "SELECT uploaded_at FROM uploads WHERE section='xt_hours' ORDER BY uploaded_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {"n": row["n"], "ts": up["uploaded_at"] if up else None}


def reset_xt_hours():
    conn = get_db()
    conn.execute("DELETE FROM xt_hours")
    conn.commit()
    conn.close()


# The 10 approved cross-training paths this build covers, transcribed
# from the Cross-Training Standards reference sheet. Sources are lists of
# fclm-mapped home departments (confirmed against the real export):
# P2R = Pack Multis, SM = Pack Singles, AFE = Chutings, Ship = Ship Dock,
# Decant = Receive, STOW (as a home dept) = RSP, IB (as a home dept) =
# Receive too — Receive/Decant is Amazon's inbound intake function, so it
# legitimately shows up both standalone (Decant XT row) and folded into
# the combined "STOW/IB" source pool. Targets are Merged Functions values
# (who's actually trained into that process, regardless of home dept).
# Rows involving ICQA, a standalone IB target, and Preslam are left out —
# their definitions weren't specified precisely enough to compute without
# guessing at real headcount numbers.
# Seed data only — loaded into the DB-backed xt_standards table once
# (identically for all 5 scenarios: general, q1, q2, q3, q4) the first
# time the app starts against an empty table. After that, the DB rows
# are authoritative; this constant is never read again at runtime — see
# get_xt_standards() below, which queries the table instead.
XT_STANDARDS_SEED = [
    {"key": "p2r_stow", "label": "P2R → STOW", "pct_label": "25%/20%/30%", "group": "ib",
     "source": ["Pack Multis"], "target": "Stow Each Nike",
     "pct": {"early": 0.25, "late": 0.20, "night": 0.30}},
    {"key": "sm_stow", "label": "SM → STOW", "pct_label": "30%/25%/30%", "group": "ib",
     "source": ["Pack Singles"], "target": "Stow Each Nike",
     "pct": {"early": 0.30, "late": 0.25, "night": 0.30}},
    {"key": "afe_stow", "label": "AFE → STOW", "pct_label": "30%/30%/30%", "group": "ib",
     "source": ["Chutings"], "target": "Stow Each Nike",
     "pct": {"early": 0.30, "late": 0.30, "night": 0.30}},
    {"key": "ship_stow", "label": "SHIP XT → STOW", "pct_label": "40%/35%/35%", "group": "ib",
     "source": ["Ship Dock"], "target": "Stow Each Nike",
     "pct": {"early": 0.40, "late": 0.35, "night": 0.35}},
    {"key": "decant_stow", "label": "DECANT XT → STOW", "pct_label": "20%", "group": "ib",
     "source": ["Receive"], "target": "Stow Each Nike",
     "pct": {"early": 0.20, "late": 0.20, "night": 0.20}},
    {"key": "stowib_sm", "label": "STOW/IB → SM", "pct_label": "6.5%/3.5%/4.5%", "group": "ob",
     "source": ["RSP", "Receive"], "target": "Pack Singles",
     "pct": {"early": 0.065, "late": 0.035, "night": 0.045}},
    {"key": "stowib_afe", "label": "STOW/IB → AFE", "pct_label": "4.5%/3%/3%", "group": "ob",
     "source": ["RSP", "Receive"], "target": "Chutings",
     "pct": {"early": 0.045, "late": 0.03, "night": 0.03}},
    {"key": "stowib_p2r", "label": "STOW/IB → P2R", "pct_label": "5%/5%/5.5%", "group": "ob",
     "source": ["RSP", "Receive"], "target": "Pack Multis",
     "pct": {"early": 0.05, "late": 0.05, "night": 0.055}},
    {"key": "stow_pickarsaw", "label": "STOW → PICK ARSAW", "pct_label": "40%/40%/42%", "group": "ob",
     "source": ["RSP"], "target": "Pick",
     "pct": {"early": 0.40, "late": 0.40, "night": 0.42}},
    {"key": "stow_p2rpick", "label": "STOW → P2R PICK", "pct_label": "3.5%/1.7%/3%", "group": "ob",
     "source": ["RSP"], "target": "Pick To Rebin",
     "pct": {"early": 0.035, "late": 0.017, "night": 0.03}},
]

def _xt_source_employees(conn, source_depts, shift):
    placeholders = ",".join("?" * len(source_depts))
    rows = conn.execute(
        f"SELECT DISTINCT employee_login FROM xt_hours WHERE fclm_mapped IN ({placeholders}) AND shift=?",
        list(source_depts) + [shift],
    ).fetchall()
    return {r["employee_login"] for r in rows}


XT_SCENARIOS = [("general", "General"), ("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"), ("q4", "Q4")]


# A second, separate internal reporting table alongside the scenario-
# based standards above — same live headcount-vs-target computation,
# but its own fixed set of rows (not scenario-toggled) matching an
# existing internal spreadsheet. Source departments/target process
# names are BEST-EFFORT GUESSES transcribed from that spreadsheet, not
# verified against real xt_hours data the way the rest of this file's
# department names were — "AFE" -> "Chutings" and the Induct/Rebin
# process names are confirmed (same ones AMBASSADOR_DEPT_PROCESSES
# already uses), but Pax/Minion Stow/Stow from Pallet/P2R Pick/P2R
# Pack/SNS/SPP are not confirmed and may need correcting once checked
# against real data — that's exactly why this is editable rather than
# baked in as a constant like most of the rest of this file.
INTERNAL_XT_TARGETS_SEED = [
    ("Inbound", "Pax", "Receive", "Pax", 25, 25, 25),
    ("Inbound", "Minion Stow", "Receive", "Minion Stow", 50, 50, 50),
    ("Inbound", "Stow from Pallet", "Receive", "Stow from Pallet", 10, 10, 10),
    ("Outbound", "AFE Induct", "Chutings", "Induct", 20, 20, 20),
    ("Outbound", "AFE Rebin", "Chutings", "Rebin", 35, 35, 35),
    ("Outbound", "P2R Pick", "Pack Multis", "Pick", 47, 46, 52),
    ("Outbound", "P2R Pack", "Pack Multis", "Pack", 62, 60, 67),
    ("Outbound", "SNS", "Ship Dock", "SNS", 30, 30, 35),
    ("Outbound", "SPP", "Ship Dock", "SPP", 12, 10, 10),
]


@request_memoize
def get_internal_xt_targets():
    """The live, editable row list for the Internal Cross-Training
    Overview table — same shape as INTERNAL_XT_TARGETS_SEED plus each
    row's id."""
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM internal_xt_targets ORDER BY sort_order, id").fetchall()]
    conn.close()
    return rows


def bulk_update_internal_xt_targets(rows):
    """rows: list of dicts, each {id, label, source_dept, target_process,
    target_early, target_late, target_night} — one 'Save all changes'
    button for the whole table, matching the pattern used everywhere
    else in this app."""
    conn = get_db()
    for r in rows:
        conn.execute(
            """UPDATE internal_xt_targets SET label=?, source_dept=?, target_process=?,
               target_early=?, target_late=?, target_night=? WHERE id=?""",
            (r["label"], r["source_dept"], r["target_process"],
             r["target_early"], r["target_late"], r["target_night"], r["id"]),
        )
    conn.commit()
    conn.close()


def get_internal_xt_overview():
    """Live Home HC / Target / Net Actuals / Gap per shift for the
    Internal Cross-Training Overview, grouped by direction (Inbound/
    Outbound) the way the source spreadsheet groups them — same live
    headcount computation as the scenario-based Cross-Training
    Standards above, and shaped to match exactly (key, label,
    pct_label, per-shift target/actual/gap/headcount/expiring_records)
    so it can render with the exact same card macro, not a separate
    table layout."""
    rows = get_internal_xt_targets()
    conn = get_db()
    threshold_days = get_xt_proficiency_expiry_days()
    out = {"Inbound": [], "Outbound": []}
    for r in rows:
        entry = {
            "key": f"internal_{r['id']}", "label": r["label"],
            "pct_label": f"{r['target_early']}/{r['target_late']}/{r['target_night']}",
            "source_dept": r["source_dept"], "target_process": r["target_process"], "shifts": {},
        }
        for shift, target_key in (("early", "target_early"), ("late", "target_late"), ("night", "target_night")):
            target = r[target_key] or 0
            source_emps = _xt_source_employees(conn, [r["source_dept"]], shift) if r["source_dept"] else set()
            headcount = len(source_emps)
            expiring_records = []
            if source_emps:
                placeholders = ",".join("?" * len(source_emps))
                trained_rows = conn.execute(
                    f"""SELECT DISTINCT employee_login FROM xt_hours
                        WHERE employee_login IN ({placeholders}) AND merged_function=? AND trained_status='Trained' AND shift=?""",
                    list(source_emps) + [r["target_process"], shift],
                ).fetchall()
                actual = len(trained_rows)
                trained_logins = [tr["employee_login"] for tr in trained_rows]
                if trained_logins:
                    placeholders2 = ",".join("?" * len(trained_logins))
                    profile_rows = conn.execute(
                        f"""SELECT employee_login, full_name, proficiency_status, last_date_on_process,
                                   supervisor_login, hours_180 FROM xt_hours
                            WHERE employee_login IN ({placeholders2}) AND merged_function=? AND shift=?""",
                        trained_logins + [r["target_process"], shift],
                    ).fetchall()
                    for pr in profile_rows:
                        expiry_date, days_until = compute_xt_expiry(pr["last_date_on_process"], pr["proficiency_status"], threshold_days)
                        if days_until is not None:
                            expiring_records.append({
                                "login": pr["employee_login"], "name": pr["full_name"] or pr["employee_login"],
                                "days_until": days_until, "expiry_date": expiry_date,
                                "manager_login": pr["supervisor_login"], "hours_180": pr["hours_180"],
                            })
            else:
                actual = 0
            entry["shifts"][shift] = {
                "headcount": headcount, "target": target, "actual": actual, "gap": actual - target,
                "expiring_records": expiring_records,
            }
        out.setdefault(r["direction"], []).append(entry)
    conn.close()
    return out


def get_internal_xt_trained_employees(target_id, shift=None):
    """The people behind an Internal Cross-Training target's Net
    Actuals number — same shape and logic as get_xt_trained_employees,
    for one internal_xt_targets row instead of a scenario standard."""
    conn = get_db()
    row = conn.execute("SELECT * FROM internal_xt_targets WHERE id=?", (target_id,)).fetchone()
    if not row or not row["source_dept"]:
        conn.close()
        return []
    shifts = [shift] if shift else ["early", "late", "night"]
    out = []
    for sh in shifts:
        source_emps = _xt_source_employees(conn, [row["source_dept"]], sh)
        if not source_emps:
            continue
        placeholders = ",".join("?" * len(source_emps))
        rows = conn.execute(
            f"""SELECT DISTINCT employee_login, full_name, fclm_mapped, job_title, supervisor_login, hours_60, hours_90, hours_180
                FROM xt_hours
                WHERE employee_login IN ({placeholders}) AND merged_function=? AND trained_status='Trained' AND shift=?
                ORDER BY full_name""",
            list(source_emps) + [row["target_process"], sh],
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["shift"] = sh
            out.append(d)
    conn.close()
    out.sort(key=lambda r: (r["shift"], r.get("full_name") or ""))
    return out


@request_memoize
def get_xt_standard_defs(scenario="general"):
    """The live, editable standards catalog for one scenario (General or
    a specific quarter) — same shape XT_STANDARDS_SEED used to be, plus
    each row's id, for the Definitions tab and for get_xt_standards() to
    read from."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM xt_standards WHERE scenario=? ORDER BY sort_order, id", (scenario,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["source"] = [s.strip() for s in (d["source"] or "").split(",") if s.strip()]
        d["pct"] = {"early": d.pop("pct_early") or 0, "late": d.pop("pct_late") or 0, "night": d.pop("pct_night") or 0}
        d["key"] = d.pop("std_key")
        d["group"] = d.pop("xt_group")
        out.append(d)
    return out


def add_xt_standard(scenario, key, label, group, source_depts, target, pct_early, pct_late, pct_night):
    conn = get_db()
    max_sort = conn.execute("SELECT MAX(sort_order) as m FROM xt_standards WHERE scenario=?", (scenario,)).fetchone()
    sort_order = (max_sort["m"] or 0) + 1
    conn.execute(
        """INSERT INTO xt_standards (sort_order, scenario, std_key, label, xt_group, source, target, pct_early, pct_late, pct_night)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (sort_order, scenario, key, label, group, ",".join(source_depts), target, pct_early, pct_late, pct_night),
    )
    conn.commit()
    conn.close()


def update_xt_standard(row_id, label, group, source_depts, target, pct_early, pct_late, pct_night):
    conn = get_db()
    conn.execute(
        "UPDATE xt_standards SET label=?, xt_group=?, source=?, target=?, pct_early=?, pct_late=?, pct_night=? WHERE id=?",
        (label, group, ",".join(source_depts), target, pct_early, pct_late, pct_night, row_id),
    )
    conn.commit()
    conn.close()


def bulk_update_xt_standards(rows):
    """rows: list of dicts, each {id, label, group, source (list),
    target, pct_early, pct_late, pct_night} — the single 'Save all
    changes' button on the Definitions table updates every row in one
    request instead of needing a Save per row."""
    conn = get_db()
    for r in rows:
        conn.execute(
            "UPDATE xt_standards SET label=?, xt_group=?, source=?, target=?, pct_early=?, pct_late=?, pct_night=? WHERE id=?",
            (r["label"], r["group"], ",".join(r["source"]), r["target"],
             r["pct_early"], r["pct_late"], r["pct_night"], r["id"]),
        )
    conn.commit()
    conn.close()


def delete_xt_standard(row_id):
    conn = get_db()
    conn.execute("DELETE FROM xt_standards WHERE id=?", (row_id,))
    conn.commit()
    conn.close()


def get_safety_compliance_health():
    """Site-wide Safety Training Compliance — every record, not scoped
    to any one manager: overall on-track %, how many are currently
    Overdue, and how many distinct managers have at least one overdue
    record. The 'Safety Compliance Health' card on Reporting."""
    conn = get_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM tracked_items WHERE section='compliance_safety' GROUP BY status"
    ).fetchall()
    overdue_managers = conn.execute(
        "SELECT COUNT(DISTINCT am_login) as n FROM tracked_items WHERE section='compliance_safety' AND am_login IS NOT NULL AND status IN ({})".format(
            ",".join("?" * len(STATUS_GAP))
        ),
        list(STATUS_GAP),
    ).fetchone()
    conn.close()
    total = sum(r["n"] for r in rows)
    ok = sum(r["n"] for r in rows if r["status"] in STATUS_OK)
    overdue = sum(r["n"] for r in rows if r["status"] in STATUS_GAP)
    pct = round(100 * ok / total) if total else None
    return {"pct": pct, "total": total, "overdue": overdue, "overdue_managers": overdue_managers["n"]}


def get_xt_site_compliance(scenario="general"):
    """Site-wide Cross-Training compliance across every approved path
    and shift: how many of each path+shift's target are actually
    trained, capped at 100% per path+shift — meeting every target
    reads 100%, and overshooting a target never pushes the overall
    rate past 100%, it only adds to 'excess' — the total headcount
    trained ABOVE target, summed across every path+shift."""
    standards = get_xt_standards(scenario)
    capped_sum = 0
    target_sum = 0
    excess = 0
    for std in standards:
        for s in std["shifts"].values():
            if s["target"] <= 0:
                continue
            capped_sum += min(s["actual"], s["target"])
            target_sum += s["target"]
            if s["actual"] > s["target"]:
                excess += s["actual"] - s["target"]
    pct = round(100 * capped_sum / target_sum) if target_sum > 0 else None
    return {"pct": pct, "excess": excess, "target_total": target_sum}


def get_xt_standards(scenario="general"):
    """Live-computed Target / Net Actuals / Gap per shift for each
    approved cross-training path in the given scenario, straight from
    the currently-loaded xt_hours data — recalculates automatically on
    every fresh upload, nothing cached. Each shift also carries a
    record (login, name, days-until-expiry) for every counted associate
    who isn't already Lapsed, so the page can show a live 'N becoming
    Lapsed in X days' projection — and let clicking that number list
    exactly who they are — without another round trip."""
    defs = get_xt_standard_defs(scenario)
    conn = get_db()
    threshold_days = get_xt_proficiency_expiry_days()
    out = []
    for std in defs:
        row = {
            "id": std["id"], "key": std["key"], "label": std["label"], "group": std["group"],
            "pct_label": f"{round(std['pct']['early']*100)}%/{round(std['pct']['late']*100)}%/{round(std['pct']['night']*100)}%",
            "shifts": {},
        }
        for shift in ["early", "late", "night"]:
            source_emps = _xt_source_employees(conn, std["source"], shift) if std["source"] else set()
            headcount = len(source_emps)
            target = round(headcount * std["pct"][shift])
            expiring_records = []
            if source_emps:
                placeholders = ",".join("?" * len(source_emps))
                trained_rows = conn.execute(
                    f"""SELECT DISTINCT employee_login FROM xt_hours
                        WHERE employee_login IN ({placeholders}) AND merged_function=? AND trained_status='Trained' AND shift=?""",
                    list(source_emps) + [std["target"], shift],
                ).fetchall()
                actual = len(trained_rows)
                trained_logins = [r["employee_login"] for r in trained_rows]
                if trained_logins:
                    placeholders2 = ",".join("?" * len(trained_logins))
                    profile_rows = conn.execute(
                        f"""SELECT employee_login, full_name, proficiency_status, last_date_on_process,
                                   supervisor_login, hours_180 FROM xt_hours
                            WHERE employee_login IN ({placeholders2}) AND merged_function=? AND shift=?""",
                        trained_logins + [std["target"], shift],
                    ).fetchall()
                    for pr in profile_rows:
                        expiry_date, days_until = compute_xt_expiry(pr["last_date_on_process"], pr["proficiency_status"], threshold_days)
                        if days_until is not None:
                            expiring_records.append({
                                "login": pr["employee_login"], "name": pr["full_name"] or pr["employee_login"],
                                "days_until": days_until, "expiry_date": expiry_date,
                                "manager_login": pr["supervisor_login"], "hours_180": pr["hours_180"],
                            })
            else:
                actual = 0
            row["shifts"][shift] = {
                "headcount": headcount, "target": target, "actual": actual, "gap": actual - target,
                "expiring_records": expiring_records,
            }
        out.append(row)
    conn.close()
    return out


def get_xt_standards_for_departments(departments, scenario="general"):
    """The subset of get_xt_standards()'s rows relevant to ANY of the
    given departments — same numbers, same computation, just filtered
    to paths whose source pool includes at least one of them (matched
    against every known alias, not just the exact name). A path that
    applies to two of the given departments still appears once, not
    twice. Used for the Cross-Training drill-down section on AM
    Overview and Trainer Overview alike — an AM passes their one
    department, a Trainer/OM/SOM passes every department across their
    assigned or reporting AMs."""
    if not departments:
        return []
    all_rows = get_xt_standards(scenario)
    wanted = set()
    for d in departments:
        wanted |= department_group(normalize_department(d))
    defs_by_key = {d["key"]: d for d in get_xt_standard_defs(scenario)}
    out = []
    for row in all_rows:
        std = defs_by_key.get(row["key"])
        if not std:
            continue
        row_depts = {normalize_department(s) for s in std["source"]}
        if row_depts & wanted:
            out.append(row)
    return out


def get_xt_standards_for_department(department, scenario="general"):
    """Single-department convenience wrapper around
    get_xt_standards_for_departments — kept separate since most callers
    (an AM viewing their own drill-down) only ever have the one."""
    return get_xt_standards_for_departments([department] if department else [], scenario)


def get_xt_trained_employees(key, shift=None, scenario="general"):
    """The actual people behind a Net Actuals number — everyone in a
    path's source pool who's Trained in its target function. Pass shift
    for one shift's list (what clicking an Actuals cell shows); omit it
    for all three shifts at once (what clicking the path label shows)."""
    defs = get_xt_standard_defs(scenario)
    std = next((s for s in defs if s["key"] == key), None)
    if not std or not std["source"]:
        return []
    shifts = [shift] if shift else ["early", "late", "night"]
    conn = get_db()
    out = []
    for sh in shifts:
        source_emps = _xt_source_employees(conn, std["source"], sh)
        if not source_emps:
            continue
        placeholders = ",".join("?" * len(source_emps))
        rows = conn.execute(
            f"""SELECT DISTINCT employee_login, full_name, fclm_mapped, job_title, supervisor_login, hours_60, hours_90, hours_180
                FROM xt_hours
                WHERE employee_login IN ({placeholders}) AND merged_function=? AND trained_status='Trained' AND shift=?
                ORDER BY full_name""",
            list(source_emps) + [std["target"], sh],
        ).fetchall()
        for r in rows:
            d = dict(r)
            d["shift"] = sh
            out.append(d)
    conn.close()
    out.sort(key=lambda r: (r["shift"], r.get("full_name") or ""))
    return out


def get_xt_dept_process_matrix(shift=None):
    """Home department × process grid — how many distinct employees are
    Trained in each Merged Function, by their fclm-mapped home
    department. This is the whole raw picture, not just the ten approved
    standard paths — it's what surfaces cross-training that's actually
    happening but isn't one of the tracked standards. Pass shift to see
    one shift's coverage instead of all three combined."""
    conn = get_db()
    q = """SELECT fclm_mapped, merged_function, COUNT(DISTINCT employee_login) as n
           FROM xt_hours
           WHERE trained_status='Trained' AND fclm_mapped IS NOT NULL AND merged_function IS NOT NULL"""
    params = []
    if shift:
        q += " AND shift=?"
        params.append(shift)
    q += " GROUP BY fclm_mapped, merged_function"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    depts = sorted({r["fclm_mapped"] for r in rows})
    procs = sorted({r["merged_function"] for r in rows})
    grid = {(r["fclm_mapped"], r["merged_function"]): r["n"] for r in rows}
    return {"depts": depts, "procs": procs, "grid": grid}


def _xt_employee_profiles(logins):
    """Shared helper: given a list of employee_logins, returns each one's
    full list of function rows (process, trained status, shift, hours,
    last date on the process, and its projected expiry — see
    compute_xt_expiry)."""
    if not logins:
        return []
    conn = get_db()
    placeholders = ",".join("?" * len(logins))
    rows = conn.execute(
        f"""SELECT employee_login, full_name, fclm_mapped, merged_function, trained_status,
                   proficiency_status, shift, hours_60, hours_90, hours_180, last_date_on_process
            FROM xt_hours WHERE employee_login IN ({placeholders})
            ORDER BY full_name, merged_function""",
        logins,
    ).fetchall()
    conn.close()
    threshold_days = get_xt_proficiency_expiry_days()
    by_emp = {}
    for r in rows:
        d = dict(r)
        expiry_date, days_until = compute_xt_expiry(d.get("last_date_on_process"), d.get("proficiency_status"), threshold_days)
        d["expiry_date"] = expiry_date
        d["days_until_expiry"] = days_until
        emp = by_emp.setdefault(d["employee_login"], {
            "employee_login": d["employee_login"], "full_name": d["full_name"],
            "fclm_mapped": d["fclm_mapped"], "functions": [],
        })
        emp["functions"].append(d)
    return list(by_emp.values())


def search_xt_employees(query, limit=25):
    """Employees matching a name/login search, each carrying their full
    list of function rows — the 'what is this associate trained for'
    lookup. Accepts several comma-separated names/logins at once, e.g.
    'denicata, tadeberh, John Smith' — each term matched independently
    and the results combined."""
    query = (query or "").strip()
    terms = [t.strip() for t in query.split(",") if t.strip() and len(t.strip()) >= 2]
    if not terms:
        return []
    conn = get_db()
    logins = []
    seen = set()
    for term in terms:
        q = f"%{term.lower()}%"
        rows = conn.execute(
            """SELECT DISTINCT employee_login, full_name FROM xt_hours
               WHERE lower(full_name) LIKE ? OR lower(employee_login) LIKE ?
               ORDER BY full_name LIMIT ?""",
            (q, q, limit),
        ).fetchall()
        for r in rows:
            if r["employee_login"] not in seen:
                seen.add(r["employee_login"])
                logins.append(r["employee_login"])
    conn.close()
    return _xt_employee_profiles(logins)


def get_xt_coverage_table(shift=None, manager_login=None, department=None, process=None):
    """Combined filter: a shift, together with a manager OR a
    department (either one matches — not both required), further
    narrowed to only associates actually Trained in a specific process
    if one's given. Requires at least one of manager_login/department/
    process; shift alone would just be the whole workforce on that
    shift, which isn't a useful lookup here."""
    conn = get_db()
    conditions, params = [], []
    if shift:
        conditions.append("shift=?")
        params.append(shift)
    or_parts, or_params = [], []
    if manager_login:
        or_parts.append("lower(supervisor_login)=?")
        or_params.append(manager_login.strip().lower())
    if department:
        or_parts.append("fclm_mapped=?")
        or_params.append(department)
    if not or_parts and not process:
        conn.close()
        return []
    if or_parts:
        conditions.append("(" + " OR ".join(or_parts) + ")")
        params += or_params
    if process:
        conditions.append("merged_function=? AND trained_status='Trained'")
        params.append(process)
    q = "SELECT DISTINCT employee_login FROM xt_hours WHERE " + " AND ".join(conditions)
    logins = [r["employee_login"] for r in conn.execute(q, params).fetchall()]
    conn.close()
    return _xt_employee_profiles(logins)


def get_xt_team(manager_login):
    """Direct reports of a manager, per the supervisor_login field in the
    hours-on-function export — each with their full function profile.
    Only one level deep: this data doesn't carry a manager-of-managers
    chain the way user_roles/reports_to does."""
    manager_login = (manager_login or "").strip()
    if not manager_login:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT employee_login FROM xt_hours WHERE lower(supervisor_login)=?",
        (manager_login.lower(),),
    ).fetchall()
    conn.close()
    logins = [r["employee_login"] for r in rows]
    return _xt_employee_profiles(logins)


def get_xt_retention_report():
    """The Retention tab's data: for every shift, every process's
    empirically-determined 'home' department (whichever fclm_mapped is
    most common among everyone who does that process — there's no
    single authoritative process-to-department table in this app, so
    this is derived from the data itself rather than guessed), and
    within each department, every non-Lapsed associate with a
    projected expiry, split into Home (their own fclm_mapped matches
    that department) vs XT/cross-trained (it doesn't). Each record
    carries its own days-until-expiry so the page can recompute counts
    entirely client-side as the day-window input changes.
    Returns {shift: {department: {"home": [...], "xt": [...]}}}."""
    conn = get_db()
    threshold_days = get_xt_proficiency_expiry_days()

    all_rows = conn.execute(
        "SELECT employee_login, full_name, fclm_mapped, merged_function, proficiency_status, shift, last_date_on_process "
        "FROM xt_hours WHERE proficiency_status IS NOT NULL AND fclm_mapped IS NOT NULL AND merged_function IS NOT NULL"
    ).fetchall()

    process_dept_counts = {}
    for r in all_rows:
        process_dept_counts.setdefault(r["merged_function"], {}).setdefault(r["fclm_mapped"], 0)
        process_dept_counts[r["merged_function"]][r["fclm_mapped"]] += 1
    process_home = {p: max(counts, key=counts.get) for p, counts in process_dept_counts.items()}

    grid = {s: {} for s in SHIFTS}
    for r in all_rows:
        shift = r["shift"]
        if shift not in grid:
            continue
        expiry_date, days_until = compute_xt_expiry(r["last_date_on_process"], r["proficiency_status"], threshold_days)
        if days_until is None:
            continue
        home_dept = process_home.get(r["merged_function"])
        if not home_dept:
            continue
        bucket = "home" if r["fclm_mapped"] == home_dept else "xt"
        dept_entry = grid[shift].setdefault(home_dept, {"home": [], "xt": []})
        dept_entry[bucket].append({
            "login": r["employee_login"], "name": r["full_name"] or r["employee_login"],
            "process": r["merged_function"], "days_until": days_until,
        })
    conn.close()
    return grid


def get_xt_home_departments():
    """Every distinct home department (fclm_mapped) seen in the
    hours-on-function data — for the department picker on the Associate
    Directory."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT fclm_mapped FROM xt_hours WHERE fclm_mapped IS NOT NULL ORDER BY fclm_mapped"
    ).fetchall()
    conn.close()
    return [r["fclm_mapped"] for r in rows]


def get_xt_processes():
    """Every distinct process (merged_function) seen in the
    hours-on-function data — for the source/target pickers on the
    Cross-Training Definitions 'add a new path' form."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT merged_function FROM xt_hours WHERE merged_function IS NOT NULL ORDER BY merged_function"
    ).fetchall()
    conn.close()
    return [r["merged_function"] for r in rows]


def get_xt_employees_by_department(department, limit=200):
    """Every employee whose home department (fclm_mapped) matches, each
    carrying their full function profile — the department-wide lookup on
    the Associate Directory."""
    department = (department or "").strip()
    if not department:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT employee_login FROM xt_hours WHERE fclm_mapped=? ORDER BY employee_login LIMIT ?",
        (department, limit),
    ).fetchall()
    conn.close()
    logins = [r["employee_login"] for r in rows]
    return _xt_employee_profiles(logins)


def get_items(section=None, am_login=None, fc=None, employee_login=None):
    # Empty list means "scoped to nobody" (e.g. a trainer with no AMs
    # assigned yet) — short-circuit rather than emitting invalid SQL
    # ("IN ()") or, worse, silently matching everything.
    if isinstance(section, (list, tuple)) and len(section) == 0:
        return []
    if isinstance(am_login, (list, tuple)) and len(am_login) == 0:
        return []
    if isinstance(employee_login, (list, tuple)) and len(employee_login) == 0:
        return []

    conn = get_db()
    q = "SELECT * FROM tracked_items WHERE 1=1"
    params = []
    if section:
        if isinstance(section, (list, tuple)):
            q += f" AND section IN ({','.join('?' * len(section))})"
            params.extend(section)
        else:
            q += " AND section = ?"
            params.append(section)
    if am_login:
        if isinstance(am_login, (list, tuple)):
            q += f" AND am_login IN ({','.join('?' * len(am_login))})"
            params.extend(am_login)
        else:
            q += " AND am_login = ?"
            params.append(am_login)
    if employee_login:
        if isinstance(employee_login, (list, tuple)):
            q += f" AND employee_login IN ({','.join('?' * len(employee_login))})"
            params.extend(employee_login)
        else:
            q += " AND employee_login = ?"
            params.append(employee_login)
    if fc:
        q += " AND fc = ?"
        params.append(fc)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Sections that represent an individual's own training/compliance record
# (as opposed to staffing/coverage metrics about their team) — what "My
# Trainings" on the AM Overview pulls from, matched on employee_login
# rather than am_login.
PERSONAL_TRAINING_SECTIONS = ["compliance_safety", "planning_compliance", "planning_de_tech", "planning_indirect_roles", "planning_bts"]


def get_my_trainings(employee_login, fc=None):
    """Every training/compliance record belonging to this person themselves
    (an AM or Team Lead's own requirements) — not their team's data."""
    items = get_items(section=PERSONAL_TRAINING_SECTIONS, employee_login=employee_login, fc=fc)
    today = date.today()
    for it in items:
        it["days_to_due"] = None
        if it.get("due_date"):
            try:
                d = datetime.strptime(it["due_date"][:10], "%Y-%m-%d").date()
                it["days_to_due"] = (d - today).days
            except ValueError:
                pass
    # Most urgent first: overdue/soonest due dates, then no-due-date items last.
    items.sort(key=lambda r: (r["days_to_due"] if r["days_to_due"] is not None else 999999))
    return items


def status_bucket(status):
    if status in STATUS_OK:
        return "ok"
    if status in STATUS_RISK:
        return "risk"
    return "gap"


def scorecard(section_list, am_login=None, fc=None):
    """Roll counts of ok/risk/gap up for a set of sections.

    Counts in SQL rather than pulling the rows back and counting them in
    Python: only `status` is ever looked at here, so a grouped count
    returns a handful of rows where SELECT * returned one per tracked
    item. That matters because this is the single hottest query in the
    app — every scorecard tile is one call, and a ranking page makes one
    per manager per metric, so the old version shipped tens of thousands
    of full rows across the wire to render one page.

    Served by idx_ti_am_section (am_login, section), which exists
    because this filters on both together — the separate
    single-column indexes can each serve only one half, leaving the
    other as a filter over everything that matched.
    """
    # Same "scoped to nobody" short-circuits as get_items — an empty
    # scope list must mean no rows, never "IN ()" and never everything.
    if isinstance(section_list, (list, tuple)) and len(section_list) == 0:
        return {"ok": 0, "risk": 0, "gap": 0, "total": 0, "pct_ok": None}
    if isinstance(am_login, (list, tuple)) and len(am_login) == 0:
        return {"ok": 0, "risk": 0, "gap": 0, "total": 0, "pct_ok": None}

    q = "SELECT status, COUNT(*) AS n FROM tracked_items WHERE 1=1"
    params = []
    if section_list:
        if isinstance(section_list, (list, tuple)):
            q += f" AND section IN ({','.join('?' * len(section_list))})"
            params.extend(section_list)
        else:
            q += " AND section = ?"
            params.append(section_list)
    if am_login:
        if isinstance(am_login, (list, tuple)):
            q += f" AND am_login IN ({','.join('?' * len(am_login))})"
            params.extend(am_login)
        else:
            q += " AND am_login = ?"
            params.append(am_login)
    if fc:
        q += " AND fc = ?"
        params.append(fc)
    q += " GROUP BY status"

    conn = get_db()
    rows = conn.execute(q, params).fetchall()
    conn.close()

    out = {"ok": 0, "risk": 0, "gap": 0, "total": 0}
    for r in rows:
        n = r["n"]
        out[status_bucket(r["status"])] += n
        out["total"] += n
    out["pct_ok"] = round(100 * out["ok"] / out["total"], 1) if out["total"] else None
    return out


def all_ams():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT am_login FROM tracked_items WHERE am_login IS NOT NULL ORDER BY am_login"
    ).fetchall()
    conn.close()
    return [r["am_login"] for r in rows]


def all_fcs():
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT fc FROM tracked_items WHERE fc IS NOT NULL ORDER BY fc"
    ).fetchall()
    conn.close()
    return [r["fc"] for r in rows]


def data_freshness():
    conn = get_db()
    row = conn.execute("SELECT MAX(uploaded_at) as ts, SUM(row_count) as n FROM uploads").fetchone()
    conn.close()
    return dict(row) if row else {"ts": None, "n": 0}


DATA_STALE_AFTER_DAYS = 8


def get_upload_status_by_section(sections):
    """For each of the given sections: last upload time, total rows last
    imported, and whether it's stale (no upload in DATA_STALE_AFTER_DAYS)
    — OLR needs a fresh weekly snapshot to track WoW accurately, so a
    section that's gone quiet needs to be visibly flagged, not just
    silently missing."""
    conn = get_db()
    out = []
    now = datetime.utcnow()
    for key in sections:
        last_upload = conn.execute(
            "SELECT filename, uploaded_at, row_count FROM uploads WHERE section=? ORDER BY uploaded_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        ts = last_upload["uploaded_at"] if last_upload else None
        stale = True
        days_ago = None
        if ts:
            try:
                uploaded_dt = datetime.fromisoformat(ts)
                days_ago = (now - uploaded_dt).days
                stale = days_ago > DATA_STALE_AFTER_DAYS
            except ValueError:
                pass
        out.append({
            "section": key, "label": SECTIONS.get(key, key),
            "last_uploaded_at": ts, "last_filename": last_upload["filename"] if last_upload else None,
            "last_row_count": last_upload["row_count"] if last_upload else None,
            "days_ago": days_ago, "stale": stale,
        })
    conn.close()
    return out


def reset_section(section):
    conn = get_db()
    conn.execute("DELETE FROM tracked_items WHERE section = ?", (section,))
    conn.commit()
    conn.close()


# ------------------------------------------------------------ escalations --

def add_escalation(category, fc, description, severity, raised_by):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO escalations (category, fc, description, severity, status, raised_by, created_at)
           VALUES (?,?,?,?, 'Open', ?, ?)""",
        (category, fc, description, severity, raised_by, _now()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def resolve_escalation(escalation_id):
    conn = get_db()
    conn.execute(
        "UPDATE escalations SET status='Resolved', resolved_at=? WHERE id=?",
        (_now(), escalation_id),
    )
    conn.commit()
    conn.close()


def get_escalations(status=None):
    conn = get_db()
    if status:
        rows = conn.execute(
            "SELECT * FROM escalations WHERE status=? ORDER BY created_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM escalations ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_escalation(escalation_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM escalations WHERE id=?", (escalation_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ------------------------------------------------- record escalations -----
# Phase 1: the record's own AM. Phase 2: that AM's direct manager
# (user_roles.reports_to). Phase 3: automatically walks the rest of the
# chain looking for whoever holds the senior-ops position — matched by
# role ('som') OR a fuzzy match against their free-text job title, since
# real titles vary ("Sr Ops Manager", "Snr Operations", "Senior
# Operations Manager", with or without an "Interim"/"Acting" prefix). If
# nothing in the chain reads as senior ops, it looks for a General
# Manager / Site Lead title instead. If neither title turns up anywhere
# (the org chart isn't filled in that far), it falls back to a
# broadcast to everyone holding the SOM role, so an escalation never
# just vanishes into a dead end.
ESCALATION_MAX_PHASE = 3

# The general "Log Escalation" ticket system (Trainer Overview) — distinct
# from the record_escalations auto-phase-routing above: here a trainer
# manually picks the category(ies), optionally attaches specific records,
# and assigns a specific manager, rather than the system auto-walking the
# reports_to chain. section is None for 'Other', which takes a free-text
# description instead of a record picker.
ESCALATION_TICKET_CATEGORIES = [
    ("safety_compliance", "Safety Compliance", "compliance_safety"),
    ("indirect_roles", "Indirect Roles", "planning_indirect_roles"),
    ("bts", "BTS", "planning_bts"),
    ("cross_training", "Cross Training", "staffing_xt"),
    ("instructor_mgmt", "Instructor Management", "staffing_instructor"),
    ("de_tech", "DE Tech", "planning_de_tech"),
    ("other", "Other", None),
]
ESCALATION_TICKET_CATEGORY_MAP = {key: (label, section) for key, label, section in ESCALATION_TICKET_CATEGORIES}

_SENIOR_OPS_SENIORITY_RE = re.compile(r"\b(sr|snr|senior)\b")
_SENIOR_OPS_OPS_RE = re.compile(r"\b(ops|operations|operation)\b")
_INTERIM_PREFIX_RE = re.compile(r"\b(interim|acting)\b")


def _normalize_title(title):
    t = (title or "").lower()
    t = _INTERIM_PREFIX_RE.sub(" ", t)
    t = re.sub(r"[^a-z ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _is_senior_ops_title(title):
    t = _normalize_title(title)
    return bool(t) and bool(_SENIOR_OPS_SENIORITY_RE.search(t)) and bool(_SENIOR_OPS_OPS_RE.search(t))


def _is_gm_or_site_lead_title(title):
    t = _normalize_title(title)
    return bool(t) and ("general manager" in t or "site lead" in t or "site leader" in t)


def get_manager_of(login):
    if not login:
        return None
    conn = get_db()
    row = conn.execute("SELECT reports_to FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    return row["reports_to"] if row and row["reports_to"] else None


def _find_by_title(start_login, matcher):
    """Walks the reports_to chain starting at start_login (inclusive),
    returning the first login whose role/title satisfies matcher(role,
    title), or None if the chain ends (or cycles) without a match."""
    conn = get_db()
    cur = start_login
    seen = set()
    result = None
    for _ in range(15):  # safety cap against a cyclical reports_to chain
        if not cur or cur in seen:
            break
        seen.add(cur)
        row = conn.execute("SELECT role, title, reports_to FROM user_roles WHERE login=?", (cur,)).fetchone()
        if row and matcher(row["role"], row["title"]):
            result = cur
            break
        cur = row["reports_to"] if row and row["reports_to"] else None
    conn.close()
    return result


def find_senior_ops_for_login(login):
    """The automatic senior-ops lookup: walks the chain above (and
    including) login, matching role=='som' or a fuzzy senior-ops title."""
    return _find_by_title(login, lambda role, title: role == "som" or _is_senior_ops_title(title))


def find_gm_or_site_lead_for_login(login):
    """Fallback lookup when no senior-ops title is found — General
    Manager / Site Lead, matched purely on title since there's no
    dedicated role for it."""
    return _find_by_title(login, lambda role, title: _is_gm_or_site_lead_title(title))


# ------------------------------------------- role auto-detection ---------
# Builds role assignments automatically from org_map (populated by every
# CSV import — most usefully Safety Training Compliance, which is the only
# one that captures a Job Title — with employee_login, am_login as their
# supervisor, and role_title). Every login's OWN title is scanned directly
# first (so an Operations Manager or Senior Operations Manager is found
# even if no Area Manager's supervisor chain happens to reach them);
# reports_to is then wired up for anyone whose supervisor was also
# detected. If a detected Area Manager's supervisor has no title data of
# their own at all (common — OMs/SOMs don't always get an individual
# compliance record the way associates and AMs do), they're still assumed
# to be the Operations Manager purely from being on record as that AM's
# supervisor — flagged as unconfirmed via title_confirmed=False, since
# that's a weaker signal than an actual title match.

def _title_has_area_manager(title):
    return "area manager" in (title or "").lower()


def _title_has_operations_manager_non_senior(title):
    t = (title or "").lower()
    has_ops_manager = "operations manager" in t or "ops manager" in t
    return has_ops_manager and not _is_senior_ops_title(title)


def _title_has_trainer(title):
    return "trainer" in (title or "").lower()


def _title_has_team_lead(title):
    t = (title or "").lower()
    # "Operations Lead" is the equivalent shop-floor lead title in the
    # real compliance export (Amazon FC-speak) — "Team Lead" itself
    # doesn't appear as a literal Business Title at all in that data.
    return "team lead" in t or "teamlead" in t or "operations lead" in t or "ops lead" in t


def _title_has_learning_manager(title):
    return "learning manager" in (title or "").lower()


# ------------------------------------------------- Indirect Role engine ---
# Faithful port of the source workbook's GenerateIROverview_v10 macro. Buckets
# are the same 3-way split the VBA uses: 0 = Trained w/ Practice (the goal),
# 1 = Trained, No Practice, 2 = Not Trained (but may show practice/progress).
IR_BUCKET_TRAINED, IR_BUCKET_NO_PRACTICE, IR_BUCKET_GAP = 0, 1, 2


def _ir_matches(val, filt):
    """Exact match against one or more '|'-separated allowed values; a
    blank or '*' filter accepts anything."""
    if not filt or filt == "*":
        return True
    return val in [p.strip() for p in filt.split("|")]


def _ir_has_earned(row):
    """Matches the source workbook's HasEarned exactly: case-insensitive
    'earned' status, OR — if the status field doesn't literally say that
    — a valid-looking earned date counts too (VBA's IsDate check). The
    real export uses lowercase 'earned', which is why this needs to be
    case-insensitive rather than an exact match."""
    status = (row.get("certificate_status") or "").strip().lower()
    if status == "earned":
        return True
    ed = (row.get("certificate_earned_date") or "").strip()
    if not ed:
        return False
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%B %d, %Y"):
        try:
            datetime.strptime(ed, fmt)
            return True
        except ValueError:
            continue
    return bool(re.search(r"\d", ed))  # permissive fallback, mirrors IsDate's leniency


def _ir_matches2(val, filt, mode):
    """Like _ir_matches, but mode can force substring matching ('substr')
    or accept-anything ('any') instead of exact."""
    m = (mode or "").strip().lower()
    if m == "any" or not filt or filt == "*":
        return True
    parts = [p.strip() for p in filt.split("|")]
    if m == "substr":
        return any(p and p.lower() in (val or "").lower() for p in parts)
    return val in parts


def _ir_enrich_rows(rows, area_id_by_login):
    """Fills in shift/area/home_process on each row from its raw
    shift_pattern code and the roster-derived management area id, using
    the embedded Helper lookup tables above — this is exactly what lets
    ir_dashboard/ir_umbrella/ir_learn uploads be raw data only, with no
    Shift/Area/Home Process columns required."""
    for r in rows:
        letter = IR_HELPER_SHIFT_PATTERNS.get(r.get("shift_pattern"))
        r["shift"] = _ir_shift(letter)
        area_id = area_id_by_login.get(r.get("login"))
        home_process, area = IR_HELPER_AREA_BY_ID.get(area_id, (None, None))
        r["area"] = area
        r["home_process"] = home_process
    return rows


@request_memoize
def compute_ir_overview(include_members=False):
    """Runs the whole Indirect Role coverage computation over whatever's
    currently in ir_dashboard/ir_umbrella/ir_learn/ir_roster, and returns
    it shaped for the report page: a list of sections, each with its
    roles, each role carrying an early/late/night breakdown of
    {target, trained, gap_to_target, no_practice, not_trained, pct}."""
    cfg_rows = get_ir_role_config_rows()
    conn = get_db()
    ir_rows = [dict(r) for r in conn.execute("SELECT * FROM ir_dashboard").fetchall()]
    umb_rows = [dict(r) for r in conn.execute("SELECT * FROM ir_umbrella").fetchall()]
    learn_rows = [dict(r) for r in conn.execute("SELECT * FROM ir_learn").fetchall()]
    roster_rows = conn.execute("SELECT login, management_area_id FROM ir_roster").fetchall()
    a20_logins = {r["login"] for r in conn.execute(
        "SELECT DISTINCT login FROM ir_roster WHERE management_area_id=\'20\'"
    ).fetchall()}
    conn.close()

    area_id_by_login = {}
    for r in roster_rows:
        try:
            area_id_by_login[r["login"]] = int(r["management_area_id"])
        except (TypeError, ValueError):
            pass
    ir_rows = _ir_enrich_rows(ir_rows, area_id_by_login)
    umb_rows = _ir_enrich_rows(umb_rows, area_id_by_login)
    learn_rows = _ir_enrich_rows(learn_rows, area_id_by_login)

    SHIFT_IDX = {"early": 0, "late": 1, "night": 2}
    IR_TRAINED_STATUSES = {"Trained with practice", "Newly Trained"}

    # res[role_config_id][shift_idx][bucket] = count
    # people[role_config_id][shift_idx][bucket] = [{login, full_name, detail}, ...]
    res = {}
    people = {}

    def bump(key, shift, bucket, login=None, full_name=None, detail=None, n=1,
             manager_login=None, hours=None, department=None):
        if shift not in SHIFT_IDX:
            return
        si = SHIFT_IDX[shift]
        cell = res.setdefault(key, [[0, 0, 0], [0, 0, 0], [0, 0, 0]])
        cell[si][bucket] += n
        if login:
            plist = people.setdefault(key, [[[], [], []], [[], [], []], [[], [], []]])
            plist[si][bucket].append({
                "login": login, "full_name": full_name, "detail": detail,
                "manager_login": manager_login, "hours": hours, "department": department,
            })

    # Pre-index: E-Pump Truck holders (for the Water Spider dependency)
    epump_logins = {r["login"] for r in umb_rows
                     if r["certificate_title"] == "EUCF_NTU_ALL_Electric Pump Truck" and _ir_has_earned(r)}
    # Pre-index: Ergo/Robotic IR roles + TSO Learn completion, for TSO_KOMBI
    ergo_keys, robotic_keys = set(), set()
    for r in ir_rows:
        if r["indirect_role_status"] in ("Trained with practice", "Newly Trained") and r["shift"]:
            k = (r["login"], r["shift"])
            if r["indirect_role"] == "Ergo Pack machine operator":
                ergo_keys.add(k)
            if r["indirect_role"] == "Robotic palletize operator":
                robotic_keys.add(k)
    tso_learn_keys = {(r["login"], r["shift"]) for r in learn_rows
                       if r["shift"] and "completed" in (r["status"] or "").lower()
                       and (r["training_name"] or "").strip().lower() == "scn2 ship tso pmvs"}
    # login -> {full_name, manager_login, department}, for the branches
    # (Jambuster, TSO) that only carry a bare login in their working sets
    # and need this extra detail for the drill-down.
    info_by_login = {}
    for r in ir_rows + umb_rows + learn_rows:
        if r["login"] and r["login"] not in info_by_login and r.get("full_name"):
            info_by_login[r["login"]] = {
                "full_name": r["full_name"], "manager_login": r.get("manager_login"), "department": r.get("home_process"),
            }
    name_by_login = {k: v["full_name"] for k, v in info_by_login.items()}

    for cfg_row in cfg_rows:
        idx = cfg_row["id"]; section = cfg_row["section"]; role = cfg_row["role"]; src = cfg_row["src_type"]
        area = cfg_row["area"] or ""; hp = cfg_row["home_process"] or ""; vals = cfg_row["match_vals"] or ""
        es, ls, ns = cfg_row["es"], cfg_row["ls"], cfg_row["ns"]; special = cfg_row["special"] or ""
        key = (idx, section, role)
        val_list = vals.split("|")

        if src == "IR":
            seen_ir = set()
            for r in ir_rows:
                if not r["login"] or not r["shift"] or r["indirect_role"] != val_list[0]:
                    continue
                if not _ir_matches(r["area"] or "", area) or not _ir_matches(r["home_process"] or "", hp):
                    continue
                status = r["indirect_role_status"]
                if status in IR_TRAINED_STATUSES:
                    bucket = IR_BUCKET_TRAINED
                elif status == "Trained but no practice":
                    bucket = IR_BUCKET_NO_PRACTICE
                elif status == "Not trained but with practice":
                    bucket = IR_BUCKET_GAP
                else:
                    continue
                if special == "WATERSPIDER" and hp in ("Pack Singles", "Pack Multis"):
                    if bucket == IR_BUCKET_TRAINED and r["login"] not in epump_logins:
                        bucket = IR_BUCKET_GAP  # trained on the role but no E-Pump cert -> doesn't count as fully covered
                # A person only ever counts once per role/shift/bucket —
                # duplicate export rows for the same person (a renewed
                # record, a re-export) must never inflate the count.
                dk = (r["login"], r["shift"], bucket)
                if dk in seen_ir:
                    continue
                seen_ir.add(dk)
                bump(key, r["shift"], bucket, login=r["login"], full_name=r["full_name"], detail=status,
                     manager_login=r.get("manager_login"), hours=r.get("hours_90"), department=r.get("home_process"))

        elif src == "UMB":
            dedup = set()
            for r in umb_rows:
                if not r["shift"] or not _ir_matches2(r["area"] or "", area, None) or not _ir_matches(r["home_process"] or "", hp):
                    continue
                if r["certificate_title"] != val_list[0] or not _ir_has_earned(r):
                    continue
                # Same principle as the IR branch above: a person counts
                # once, no matter how many qualifying certificate rows
                # they have (multiple issues/renewals are common in the
                # real Umbrella export). This is now unconditional, not
                # only when the role happened to be flagged DEDUP.
                dk = (r["login"], r["shift"])
                if dk in dedup:
                    continue
                dedup.add(dk)
                bump(key, r["shift"], IR_BUCKET_TRAINED, login=r["login"], full_name=r["full_name"], detail=r["certificate_title"],
                     manager_login=r.get("manager_login"), department=r.get("home_process"))

        elif src == "LEARN":
            # Grouped per person (not per matching row): a role listing
            # more than one required training (e.g. CPT Auditor's DE/EN
            # PMVs, Loader's loader/loading patterns) only counts someone
            # as trained once they've COMPLETED every one of them, not
            # just any single one. A role with only one required value
            # behaves exactly as before — this only changes outcomes for
            # multi-value roles.
            required_values = val_list
            required_set = set(required_values)
            by_person = {}
            for r in learn_rows:
                if not r["shift"] or not r["login"]:
                    continue
                tn = (r["training_name"] or "")
                matched = None
                for v in required_values:
                    if v.startswith("SUBSTR:"):
                        if v[7:].lower() in tn.lower():
                            matched = v
                            break
                    elif tn == v:
                        matched = v
                        break
                if matched is None:
                    continue
                status = (r["status"] or "").lower()
                is_completed = "completed" in status
                is_gap_status = any(s in status for s in ("registered", "in_progress", "missing"))
                if not is_completed and not is_gap_status:
                    continue  # unrecognized status — ignore this row entirely
                pkey = (r["login"], r["shift"])
                entry = by_person.setdefault(pkey, {
                    "full_name": r["full_name"], "manager_login": r.get("manager_login"),
                    "department": r.get("home_process"), "completed": set(), "any_recognized": False,
                })
                entry["any_recognized"] = True
                if is_completed:
                    entry["completed"].add(matched)

            dedup = set()
            is_dedup = special.upper() == "DEDUP"
            for (login, shift), info in by_person.items():
                if not info["any_recognized"]:
                    continue
                if info["completed"] >= required_set:
                    bucket = IR_BUCKET_TRAINED
                    detail = "Completed" if len(required_set) == 1 else f"Completed all {len(required_set)} required trainings"
                else:
                    bucket = IR_BUCKET_GAP
                    if len(required_set) > 1 and info["completed"]:
                        detail = f"Missing {len(required_set) - len(info['completed'])} of {len(required_set)} required trainings"
                    else:
                        detail = "Not completed"
                if is_dedup:
                    dk = (login, shift, bucket)
                    if dk in dedup:
                        continue
                    dedup.add(dk)
                bump(key, shift, bucket, login=login, full_name=info["full_name"], detail=detail,
                     manager_login=info["manager_login"], department=info["department"])

        elif src == "PT":
            dedup = set()
            is_pt_list = special == "PT_LIST"
            for r in umb_rows:
                if not r["shift"] or not _ir_has_earned(r):
                    continue
                title = r["certificate_title"] or ""
                if is_pt_list:
                    if not _ir_matches(r["area"] or "", area) or not _ir_matches(r["home_process"] or "", hp):
                        continue
                    if title not in val_list:
                        continue
                    person = r["login"] or r["full_name"] or ""
                    dk = (person, r["shift"])
                else:
                    if not _ir_matches(r["home_process"] or "", hp) or not title.startswith("EUCF_NTU_PT_"):
                        continue
                    dk = (r["login"], r["shift"])
                if dk in dedup:
                    continue
                dedup.add(dk)
                bump(key, r["shift"], IR_BUCKET_TRAINED, login=r["login"] or dk[0], full_name=r["full_name"], detail=title,
                     manager_login=r.get("manager_login"), department=r.get("home_process"))

        elif src in ("JAMIB", "JAMPS"):
            is_ib = src == "JAMIB"
            pre, jam, ex = set(), set(), set()
            for r in ir_rows:
                if not r["login"] or r["login"] in a20_logins or not r["shift"]:
                    continue
                sti = {"Trained with practice": 0, "Newly Trained": 0, "Trained but no practice": 1}.get(r["indirect_role_status"], -1)
                kk = (r["login"], r["shift"])
                if is_ib:
                    if r["area"] == "IB" and r["indirect_role"] in ("Indoor Marshall", "Dock Clerk", "Amnesty Floor Monitor") and 0 <= sti <= 1:
                        pre.add(kk)
                else:
                    if r["area"] == "PRE-SLAM" and r["home_process"] in ("PRE-SLAM", "Pack Singles", "Chutings") \
                            and r["indirect_role"] in ("SLAM Operator", "Pack PG") and 0 <= sti <= 1:
                        pre.add(kk)
            for r in umb_rows:
                if not r["login"] or r["login"] in a20_logins or not r["shift"] or not _ir_has_earned(r):
                    continue
                ct = r["certificate_title"] or ""
                kk = (r["login"], r["shift"])
                if ct == "EUCF_ILT_ALL_Jambuster_Plus Exemption":
                    ex.add(kk)
                if is_ib:
                    if ct == "EUCF_NTU_AMB_Receive" and r["area"] == "IB":
                        pre.add(kk)
                    if ct == "EUCF_ILT_ALL_Jambuster_Plus" and r["area"] == "IB":
                        jam.add(kk)
                else:
                    if ct in ("EUCF_ALL_AFE Induct", "EUCF_ILT_ARS_AFE Rebin", "EUCF_ILT_Smart Pac Paper", "EUCF_ALL_Pack Process Guide"):
                        pre.add(kk)
                    if ct == "EUCF_ILT_ALL_Jambuster_Plus" and (r["area"] == "PRE-SLAM" or kk in pre):
                        jam.add(kk)
            no_prereq = {k for k in jam if k not in pre and k not in ex}
            for kk in pre:
                if kk in ex:
                    continue
                bump(key, kk[1], IR_BUCKET_TRAINED if kk in jam else IR_BUCKET_GAP,
                     login=kk[0], full_name=name_by_login.get(kk[0]), detail="Jambuster",
                     manager_login=info_by_login.get(kk[0], {}).get("manager_login"),
                     department=info_by_login.get(kk[0], {}).get("department"))
            for kk in no_prereq:
                bump(key, kk[1], IR_BUCKET_NO_PRACTICE, login=kk[0], full_name=name_by_login.get(kk[0]), detail="No prereq",
                     manager_login=info_by_login.get(kk[0], {}).get("manager_login"),
                     department=info_by_login.get(kk[0], {}).get("department"))

    # TSO_KOMBI: needs Ergo AND Robotic IR-trained AND Learn "SCN2 Ship TSO PMVs" completed
    for cfg_row in cfg_rows:
        idx = cfg_row["id"]; section = cfg_row["section"]; role = cfg_row["role"]; src = cfg_row["src_type"]
        area = cfg_row["area"] or ""; hp = cfg_row["home_process"] or ""; vals = cfg_row["match_vals"] or ""
        es, ls, ns = cfg_row["es"], cfg_row["ls"], cfg_row["ns"]; special = cfg_row["special"] or ""
        if special != "TSO_KOMBI":
            continue
        key = (idx, section, role)
        for kk in tso_learn_keys:
            if kk in ergo_keys and kk in robotic_keys:
                bump(key, kk[1], IR_BUCKET_TRAINED, login=kk[0], full_name=name_by_login.get(kk[0]), detail="TSO",
                     manager_login=info_by_login.get(kk[0], {}).get("manager_login"),
                     department=info_by_login.get(kk[0], {}).get("department"))

    # ---- shape into section/role/shift rows with target comparison ----
    sections = {}
    for cfg_row in cfg_rows:
        idx = cfg_row["id"]; section = cfg_row["section"]; role = cfg_row["role"]; src = cfg_row["src_type"]
        area = cfg_row["area"] or ""; hp = cfg_row["home_process"] or ""; vals = cfg_row["match_vals"] or ""
        es, ls, ns = cfg_row["es"], cfg_row["ls"], cfg_row["ns"]; special = cfg_row["special"] or ""
        key = (idx, section, role)
        cell = res.get(key, [[0, 0, 0], [0, 0, 0], [0, 0, 0]])
        plist = people.get(key)
        targets = [es, ls, ns]
        shift_data = []
        for si, shift_name in enumerate(["early", "late", "night"]):
            target = targets[si]
            trained, no_practice, gap = cell[si]
            # Two distinct questions get answered separately here:
            # headcount ("do we have enough bodies doing this role at
            # all" — total_trained counts no-practice people too, since
            # they ARE doing the role, just need more reps) and
            # compliance ("of the people we know about, how many are
            # actually fully ready" — pct below, unaffected by this).
            total_trained = trained + no_practice
            gap_to_target = max(0, target - total_trained) if target > 0 else 0
            # Compliance is about READINESS quality, not raw headcount:
            # trained-with-practice is the only thing that counts toward
            # it. No-practice and not-trained-with-practice both count
            # against it even if the with-practice headcount alone would
            # already clear target — so this can never exceed 100%, and
            # can never read 100% while either of those is non-zero.
            total_people = trained + no_practice + gap
            if total_people > 0:
                pct = round(100 * trained / total_people)
            elif target > 0:
                pct = 0  # a target exists but literally nobody's tracked against this role/shift yet
            else:
                pct = None  # no target, no one tracked -- nothing to report
            entry = {
                "shift": shift_name, "target": target, "trained": trained, "total_trained": total_trained,
                "gap_to_target": gap_to_target, "no_practice": no_practice, "not_trained": gap, "pct": pct,
            }
            if include_members:
                bucket_lists = plist[si] if plist else [[], [], []]
                entry["members"] = {
                    "trained": bucket_lists[IR_BUCKET_TRAINED],
                    "no_practice": bucket_lists[IR_BUCKET_NO_PRACTICE],
                    "not_trained": bucket_lists[IR_BUCKET_GAP],
                }
            shift_data.append(entry)
        sections.setdefault(section, []).append({
            "id": idx, "role": role, "src_type": src, "special": special, "home_process": hp, "shifts": shift_data,
        })
    return [{"section": s, "roles": roles} for s, roles in sections.items()]


def summarize_ir_overview(overview):
    """Roll-up stats for the Indirect Roles Overview header: how many
    roles are actually tracked, how many of THOSE roles are fully on
    target (every shift with a target set is at healthy compliance —
    not just headcount-met), the overall pooled compliance % across
    every role and shift combined, and how many people still need
    training or practice hours somewhere to reach 100%."""
    roles_tracked = 0
    roles_on_target = 0
    total_trained_wp = 0
    total_people = 0
    people_needing_action = 0
    section_stats = {}
    risk_groups = {}
    for sec in overview:
        section_on_target = 0
        for role in sec["roles"]:
            roles_tracked += 1
            targeted_shifts = [s for s in role["shifts"] if s["target"] > 0]
            if targeted_shifts and all(s["pct"] is not None and s["pct"] >= 95 for s in targeted_shifts):
                roles_on_target += 1
                section_on_target += 1
            for s in role["shifts"]:
                total_trained_wp += s["trained"]
                total_people += s["trained"] + s["no_practice"] + s["not_trained"]
                action_count = s["no_practice"] + s["not_trained"]
                people_needing_action += action_count
                if s["target"] > 0 and action_count > 0:
                    key = (sec["section"], s["shift"])
                    risk = risk_groups.setdefault(key, {
                        "section": sec["section"], "shift": s["shift"],
                        "people_needing_action": 0, "affected_roles": 0,
                    })
                    risk["people_needing_action"] += action_count
                    risk["affected_roles"] += 1
        section_stats[sec["section"]] = {
            "roles": len(sec["roles"]),
            "roles_on_target": section_on_target,
        }
    overall_pct = round(100 * total_trained_wp / total_people) if total_people > 0 else None
    priority = max(
        risk_groups.values(),
        key=lambda item: (item["people_needing_action"], item["affected_roles"]),
        default=None,
    )
    return {
        "roles_tracked": roles_tracked, "roles_on_target": roles_on_target,
        "overall_pct": overall_pct, "people_needing_action": people_needing_action,
        "section_stats": section_stats, "priority": priority,
    }


def get_role_detection_proposals():
    """Returns a list of {login, role, title, reports_to, full_name,
    title_confirmed} proposals. Doesn't write anything — see
    apply_detected_roles for that."""
    conn = get_db()
    rows = conn.execute(
        "SELECT employee_login, am_login, role_title, full_name FROM org_map WHERE role_title IS NOT NULL"
    ).fetchall()
    conn.close()
    by_login = {r["employee_login"]: dict(r) for r in rows}

    proposals = {}

    def propose(login, role, title, full_name, confirmed=True):
        if login in proposals:
            return
        proposals[login] = {
            "login": login, "role": role, "title": title, "reports_to": None,
            "full_name": full_name, "title_confirmed": confirmed,
        }

    # Pass 1 — direct title match, independent of anyone's supervisor
    # chain: this is what catches an OM/SOM/Trainer/Team Lead/Learning
    # Manager who has their own compliance record, even if no detected AM
    # happens to report to them.
    for login, info in by_login.items():
        title = info["role_title"]
        if _title_has_area_manager(title):
            propose(login, "am", title, info["full_name"])
        elif _is_senior_ops_title(title):
            propose(login, "som", title, info["full_name"])
        elif _title_has_operations_manager_non_senior(title):
            propose(login, "om", title, info["full_name"])
        elif _title_has_learning_manager(title):
            propose(login, "learning_manager", title, info["full_name"])
        elif _title_has_trainer(title):
            propose(login, "trainer", title, info["full_name"])
        elif _title_has_team_lead(title):
            propose(login, "team_lead", title, info["full_name"])

    # Pass 2 — wire up reports_to for every detected AM (to their OM) and
    # detected OM (to their SOM), using each one's am_login (supervisor)
    # from org_map. If the supervisor wasn't caught by Pass 1's direct
    # title match (no title data of their own), an AM's supervisor is
    # still assumed to be the OM from the hierarchy relationship alone —
    # a weaker, unconfirmed signal, so it's flagged as such. The same
    # assumption is NOT made for SOM: mislabeling someone "Senior
    # Operations Manager" with zero title evidence is a bigger overreach
    # than assuming plain "Operations Manager", so an OM's supervisor
    # only becomes a SOM proposal when their own title actually confirms
    # it (from Pass 1).
    for login, info in list(by_login.items()):
        if login not in proposals or proposals[login]["role"] != "am":
            continue
        supervisor_login = info["am_login"]
        if not supervisor_login:
            continue
        if supervisor_login in proposals and proposals[supervisor_login]["role"] == "om":
            proposals[login]["reports_to"] = supervisor_login
        elif supervisor_login not in proposals:
            sup_info = by_login.get(supervisor_login)
            propose(supervisor_login, "om", sup_info["role_title"] if sup_info else None,
                    sup_info["full_name"] if sup_info else None, confirmed=False)
            proposals[login]["reports_to"] = supervisor_login

    for login, info in list(by_login.items()):
        if login not in proposals or proposals[login]["role"] != "om":
            continue
        supervisor_login = info["am_login"]
        if not supervisor_login:
            continue
        if supervisor_login in proposals and proposals[supervisor_login]["role"] == "som":
            proposals[login]["reports_to"] = supervisor_login

    # Pass 3 — for anyone still without a reports_to (Trainers, Team
    # Leads, Learning Managers, or an AM/OM whose chain wasn't otherwise
    # confirmed), fill in their raw supervisor login from org_map as-is.
    # This one doesn't require the supervisor to have a detected role
    # themselves — it's just recording who the data says they report to,
    # which is useful even if that person isn't (yet) a user of this app.
    for login, info in by_login.items():
        if login not in proposals or proposals[login]["reports_to"]:
            continue
        supervisor_login = info["am_login"]
        if supervisor_login and supervisor_login != login:
            proposals[login]["reports_to"] = supervisor_login

    return list(proposals.values())


AUTO_DETECT_MARKER = "auto-detect"


def get_shift_from_own_record(login):
    """The login's own most-recent compliance_safety record's shift —
    already decoded from their raw Shift Pattern (D/L/N prefix -> early/
    late/night) at CSV ingest time. Used for direct shift auto-fill on
    detected AM/Team Lead/Trainer accounts — a more direct signal than
    inferring from their reports, since it's their own shift, not a
    majority vote of someone else's."""
    conn = get_db()
    row = conn.execute(
        "SELECT shift FROM tracked_items WHERE employee_login=? AND section='compliance_safety' AND shift IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (login,),
    ).fetchone()
    conn.close()
    return row["shift"] if row else None


def _apply_shift_fill(login):
    """Auto-fills shift for a detected AM/Team Lead/Trainer directly from
    their own compliance record's shift pattern (D=early/L=late/N=night)
    — never overwrites a shift already set, by hand or a previous run."""
    conn = get_db()
    row = conn.execute("SELECT shift FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    if row and row["shift"]:
        return
    shift = get_shift_from_own_record(login)
    if shift:
        conn = get_db()
        conn.execute("UPDATE user_roles SET shift=COALESCE(shift, ?) WHERE login=?", (shift, login))
        conn.commit()
        conn.close()


def get_department_from_own_ir_roster(login):
    """This login's own department, derived the same way the Indirect
    Role engine derives Area/Home Process for anyone else: their
    Management Area ID from the employeeList-SCN2 (ir_roster) upload,
    mapped through the same embedded Helper table used there. Far more
    direct than inferring department from a majority vote of their
    reports' Cross-Training hours — and doesn't depend on that separate
    upload existing at all, just the roster one."""
    conn = get_db()
    row = conn.execute("SELECT management_area_id FROM ir_roster WHERE login=?", (login,)).fetchone()
    conn.close()
    if not row or not row["management_area_id"]:
        return None
    try:
        area_id = int(row["management_area_id"])
    except (TypeError, ValueError):
        return None
    home_process, area = IR_HELPER_AREA_BY_ID.get(area_id, (None, None))
    return home_process


def _apply_am_profile_fill(login):
    """Auto-fills department for a detected Area Manager. Tries their
    own Management Area ID from the roster upload first (see
    get_department_from_own_ir_roster) — falls back to the older,
    weaker inference (majority department among their direct reports'
    Cross-Training hours) only if that roster data isn't available.
    Only ever fills in a blank. Shift is filled separately via
    _apply_shift_fill (the AM's own compliance record)."""
    conn = get_db()
    row = conn.execute("SELECT department FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    if row and row["department"]:
        return  # already set — nothing to fill
    department = get_department_from_own_ir_roster(login)
    if not department:
        profile = suggest_am_profile(login)
        department = profile.get("department")
    if department:
        department = normalize_department(department)
        conn = get_db()
        conn.execute("UPDATE user_roles SET department=COALESCE(department, ?) WHERE login=?", (department, login))
        conn.commit()
        conn.close()


def apply_detected_roles(assigned_by):
    """Applies every proposal from get_role_detection_proposals(): assigns
    the detected role + title (and full name, if not already set) to
    each login, and wires up reports_to wherever the chain was
    confirmed.

    Never touches a role an admin assigned by hand (assigned_by isn't the
    AUTO_DETECT_MARKER). But a role THIS function assigned previously is
    fair game to correct on a later run — re-detecting a different role
    for that login updates it, and a login that no longer matches
    anything at all (stale evidence, or a bug like matching the wrong
    title field) gets its auto-assigned role revoked back to pending,
    rather than leaving a wrong assignment permanently stuck just
    because "they already have a role."

    Returns counts for the confirmation message."""
    proposals = get_role_detection_proposals()
    proposals_by_login = {p["login"]: p for p in proposals}
    counts = {"am": 0, "om": 0, "som": 0, "trainer": 0, "team_lead": 0, "learning_manager": 0,
              "reports_to": 0, "unconfirmed_om": 0, "corrected": 0, "revoked": 0}
    conn = get_db()
    existing = {
        r["login"]: {"role": r["role"], "assigned_by": r["assigned_by"]}
        for r in conn.execute("SELECT login, role, assigned_by FROM user_roles").fetchall()
    }
    conn.close()

    for p in proposals:
        ex = existing.get(p["login"])
        if ex is None:
            set_user_role(p["login"], p["role"], assigned_by=AUTO_DETECT_MARKER, full_name=p["full_name"], title=p["title"])
            counts[p["role"]] += 1
            if p["role"] == "om" and not p["title_confirmed"]:
                counts["unconfirmed_om"] += 1
            if p["role"] == "am":
                _apply_am_profile_fill(p["login"])
            if p["role"] in ("am", "team_lead", "trainer"):
                _apply_shift_fill(p["login"])
        elif ex["assigned_by"] == AUTO_DETECT_MARKER:
            if ex["role"] != p["role"]:
                set_user_role(p["login"], p["role"], assigned_by=AUTO_DETECT_MARKER, full_name=p["full_name"], title=p["title"])
                counts["corrected"] += 1
            elif p["title"]:
                set_title(p["login"], p["title"])
            if p["role"] == "am":
                _apply_am_profile_fill(p["login"])
            if p["role"] in ("am", "team_lead", "trainer"):
                _apply_shift_fill(p["login"])
        elif p["title"]:
            set_title(p["login"], p["title"])
        if p["reports_to"]:
            set_reports_to(p["login"], p["reports_to"])
            counts["reports_to"] += 1

    # Anything this tool assigned before that no longer matches any
    # current proposal (data changed, or a matching bug got fixed) loses
    # its auto-assigned role rather than staying wrong forever.
    for login, ex in existing.items():
        if ex["assigned_by"] == AUTO_DETECT_MARKER and login not in proposals_by_login:
            revoke_user_role(login)
            counts["revoked"] += 1

    # Every detected Trainer needs a matching row in the separate
    # trainers table (get_trainers() requires it) to actually function
    # as a trainer elsewhere in the app — set_user_role alone doesn't
    # create that, and without it a newly-detected trainer wouldn't even
    # show up as an option in the AM-to-trainer assignment dropdown.
    # Deliberately NOT auto-assigning any AMs to them — that's a manual
    # step now.
    for p in proposals:
        if p["role"] == "trainer":
            add_trainer(p["login"], p["full_name"])

    # A final pass for Team Lead department sync: within the loop above,
    # set_reports_to() already syncs each Team Lead's department the
    # moment their reports_to is set — but if their AM's OWN department
    # got filled later in the same run (proposals aren't guaranteed to
    # process AMs before their Team Leads), that sync would have found
    # nothing to copy yet. Re-running it now, once everyone's department
    # is settled, catches those regardless of processing order.
    for p in proposals:
        if p["role"] == "team_lead":
            sync_team_lead_department(p["login"])

    return counts


def _escalation_target_for_phase(am_login, phase):
    """Returns (target_login, target_role) for a given phase. target_role
    is only set (and target_login left None) for the last-resort
    broadcast-to-SOMs fallback, when the chain has no one titled senior
    ops OR GM/Site Lead at all."""
    if phase == 1:
        return am_login, None
    if phase == 2:
        return get_manager_of(am_login), None
    # phase 3 — auto-find the senior ops (or GM/Site Lead) above this AM.
    start = get_manager_of(am_login) or am_login
    senior = find_senior_ops_for_login(start)
    if senior:
        return senior, None
    gm = find_gm_or_site_lead_for_login(start)
    if gm:
        return gm, None
    return None, "som"


def escalate_records(tracked_item_ids, escalated_by):
    """Escalates each given tracked_item: creates a fresh phase-1
    escalation if none exists yet, or bumps an existing open one to the
    next phase (capped at ESCALATION_MAX_PHASE). Returns a summary dict
    so the caller can report what happened."""
    conn = get_db()
    created, bumped, at_max, no_am = 0, 0, 0, 0
    now = _now()
    for tid in tracked_item_ids:
        item = conn.execute("SELECT * FROM tracked_items WHERE id=?", (tid,)).fetchone()
        if not item:
            continue
        am_login = item["am_login"]
        existing = conn.execute(
            "SELECT * FROM record_escalations WHERE tracked_item_id=?", (tid,)
        ).fetchone()
        if existing and existing["status"] == "open":
            if existing["phase"] >= ESCALATION_MAX_PHASE:
                at_max += 1
                continue
            next_phase = existing["phase"] + 1
            target_login, target_role = _escalation_target_for_phase(am_login, next_phase)
            conn.execute(
                "UPDATE record_escalations SET phase=?, target_login=?, target_role=?, escalated_by=?, escalated_at=? WHERE id=?",
                (next_phase, target_login, target_role, escalated_by, now, existing["id"]),
            )
            bumped += 1
        else:
            if not am_login:
                no_am += 1
                continue
            target_login, target_role = _escalation_target_for_phase(am_login, 1)
            conn.execute(
                """INSERT INTO record_escalations (tracked_item_id, phase, target_login, target_role, status, escalated_by, escalated_at)
                   VALUES (?,1,?,?,'open',?,?)
                   ON CONFLICT(tracked_item_id) DO UPDATE SET
                       phase=1, target_login=excluded.target_login, target_role=excluded.target_role,
                       status='open', escalated_by=excluded.escalated_by, escalated_at=excluded.escalated_at,
                       resolved_by=NULL, resolved_at=NULL""",
                (tid, target_login, target_role, escalated_by, now),
            )
            created += 1
    conn.commit()
    conn.close()
    return {"created": created, "bumped": bumped, "at_max": at_max, "no_am": no_am}


def resolve_record_escalation(escalation_id, resolved_by):
    conn = get_db()
    conn.execute(
        "UPDATE record_escalations SET status='resolved', resolved_by=?, resolved_at=? WHERE id=?",
        (resolved_by, _now(), escalation_id),
    )
    conn.commit()
    conn.close()


def get_record_escalations_for_target(login=None, role=None, status="open"):
    """Escalations aimed at a specific manager login, or broadcast to a
    role (the phase-3 SOM fallback) — each with its tracked_item's
    details joined in, for a dashboard panel or the Escalations tab."""
    conn = get_db()
    q = """SELECT re.*, ti.employee_login, ti.full_name, ti.fc, ti.am_login, ti.subcategory,
                  ti.status as item_status, ti.due_date, ti.section
           FROM record_escalations re JOIN tracked_items ti ON ti.id = re.tracked_item_id WHERE 1=1"""
    params = []
    if status:
        q += " AND re.status=?"
        params.append(status)
    if login and role:
        q += " AND (re.target_login=? OR re.target_role=?)"
        params += [login, role]
    elif login:
        q += " AND re.target_login=?"
        params.append(login)
    elif role:
        q += " AND re.target_role=?"
        params.append(role)
    q += " ORDER BY re.phase DESC, re.escalated_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_open_record_escalations():
    conn = get_db()
    rows = conn.execute(
        """SELECT re.*, ti.employee_login, ti.full_name, ti.fc, ti.am_login, ti.subcategory,
                  ti.status as item_status, ti.due_date, ti.section
           FROM record_escalations re JOIN tracked_items ti ON ti.id = re.tracked_item_id
           WHERE re.status='open' ORDER BY re.phase DESC, re.escalated_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_my_escalations(login):
    """Every open escalation — record escalations and Log Escalation
    tickets both — where this login is either who it's about (am_login /
    concerning_manager) or who it's currently assigned to (target_login).
    For an individual's own Overview panel — 'concerning them or
    assigned to them' — broader than the hierarchy-scoped visibility
    used on the Escalations reporting tab, since this is specifically
    theirs, not their whole team's. Each row carries a due date: the
    underlying compliance item's due date for a record escalation, or
    the ticket's own due date for a Log Escalation ticket."""
    conn = get_db()
    record_rows = conn.execute(
        """SELECT re.*, ti.employee_login, ti.full_name, ti.fc, ti.am_login, ti.subcategory,
                  ti.status as item_status, ti.due_date, ti.section
           FROM record_escalations re JOIN tracked_items ti ON ti.id = re.tracked_item_id
           WHERE re.status='open' AND (ti.am_login=? OR re.target_login=?)
           ORDER BY re.phase DESC, re.escalated_at DESC""",
        (login, login),
    ).fetchall()
    ticket_rows = conn.execute(
        "SELECT * FROM escalation_tickets WHERE status='open' AND (concerning_manager=? OR target_login=?) ORDER BY created_at DESC",
        (login, login),
    ).fetchall()
    conn.close()
    return {"record_escalations": [dict(r) for r in record_rows], "tickets": [dict(r) for r in ticket_rows]}


def get_visible_record_escalations(scope, include_som_broadcast, status="open"):
    """scope=None means unrestricted (admin-tier or a GM/Site Lead title —
    sees every escalation). Otherwise scope is the list of logins this
    viewer is allowed to see (themselves + everyone under them in the
    reports_to tree) — include_som_broadcast additionally surfaces the
    phase-3 broadcast-to-SOMs escalations for an actual SOM-role viewer,
    since those have no single target_login to match against scope."""
    conn = get_db()
    q = """SELECT re.*, ti.employee_login, ti.full_name, ti.fc, ti.am_login, ti.subcategory,
                  ti.status as item_status, ti.due_date, ti.section
           FROM record_escalations re JOIN tracked_items ti ON ti.id = re.tracked_item_id WHERE re.status=?"""
    params = [status]
    if scope is not None:
        placeholders = ",".join("?" * len(scope)) if scope else "NULL"
        cond = f"re.target_login IN ({placeholders})"
        cond_params = list(scope)
        if include_som_broadcast:
            cond += " OR re.target_role='som'"
        q += f" AND ({cond})"
        params += cond_params
    q += " ORDER BY re.phase DESC, re.escalated_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user_title(login):
    conn = get_db()
    row = conn.execute("SELECT title FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    return row["title"] if row else None


def compute_escalation_stage(target_login):
    """Human-readable org level for the manager an escalation ticket is
    assigned to — shown as the ticket's 'stage'. Derived from role first,
    falling back to a fuzzy title match for people using SOM-equivalent
    or GM/Site Lead titles without the matching formal role."""
    role = get_effective_role_for_login(target_login)
    title = get_user_title(target_login)
    if role == "am":
        return "Area Manager"
    if role == "om":
        return "Operations Manager"
    if role == "som" or _is_senior_ops_title(title):
        return "Senior Operations"
    if _is_gm_or_site_lead_title(title):
        return "General Manager / Site Lead"
    return title or (role.replace("_", " ").title() if role else "Unassigned")


def get_escalation_assignable_managers():
    """Every AM/OM/SOM, for the 'assign to' dropdown on the Log Escalation
    page — each carrying the same stage label the created ticket will
    show, so the picker itself previews where the ticket will land."""
    conn = get_db()
    rows = conn.execute(
        "SELECT login, role, title, full_name FROM user_roles WHERE role IN ('am','om','som') ORDER BY role, login"
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "login": r["login"], "role": r["role"], "title": r["title"], "full_name": r["full_name"],
            "stage": compute_escalation_stage(r["login"]),
        })
    return out


def generate_ticket_number():
    year = datetime.now().year
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as n FROM escalation_tickets WHERE ticket_number LIKE ?", (f"ESC-{year}-%",)
    ).fetchone()
    conn.close()
    seq = (row["n"] if row else 0) + 1
    return f"ESC-{year}-{seq:04d}"


def create_escalation_ticket(categories, description, concerning_manager, assignee, records_by_category, created_by):
    """categories: list of category keys (db.ESCALATION_TICKET_CATEGORIES).
    records_by_category: {category_key: [tracked_item_id, ...]} for any
    category the trainer optionally attached specific records to.
    concerning_manager is who the escalation is about (fixed for the
    ticket's lifetime — the anchor for who's allowed to view/comment on
    it); assignee is who currently owns actioning it, and can be a
    different person from the start. Every new ticket starts at Phase 1
    regardless. Returns the new ticket number."""
    stage = compute_escalation_stage(assignee)
    conn = get_db()
    ticket_id = None
    for _attempt in range(5):
        ticket_number = generate_ticket_number()
        try:
            cur = conn.execute(
                """INSERT INTO escalation_tickets
                       (ticket_number, categories, description, target_login, concerning_manager,
                        phase, stage, status, created_by, created_at)
                   VALUES (?,?,?,?,?,1,?,'open',?,?)""",
                (ticket_number, ",".join(categories), description, assignee, concerning_manager,
                 stage, created_by, _now()),
            )
            ticket_id = cur.lastrowid
            break
        except Exception:
            continue  # ticket number collision (rare) — try the next sequence number
    if ticket_id is None:
        conn.close()
        raise RuntimeError("Could not generate a unique escalation ticket number")
    for cat_key, item_ids in (records_by_category or {}).items():
        for tid in item_ids:
            conn.execute(
                "INSERT INTO escalation_ticket_records (ticket_id, tracked_item_id, category) VALUES (?,?,?)",
                (ticket_id, tid, cat_key),
            )
    conn.commit()
    conn.close()
    return ticket_number


def set_escalation_tickety_sync(ticket_number, tickety_ticket_id, error):
    """Records the result of trying to mirror this ticket into Tickety —
    either the real Tickety ticket id on success, or the error on
    failure, so it's visible on the ticket rather than silently lost."""
    conn = get_db()
    conn.execute(
        "UPDATE escalation_tickets SET tickety_ticket_id=?, tickety_sync_error=? WHERE ticket_number=?",
        (tickety_ticket_id, error, ticket_number),
    )
    conn.commit()
    conn.close()


def get_escalation_tickets(scope=None, status=None):
    """scope=None means unrestricted (admin-tier or GM/Site Lead) — every
    ticket. Otherwise only tickets whose concerning_manager is someone in
    scope (themselves, or an ancestor of theirs in the reports_to tree —
    i.e. the concerning manager, their manager, and skip-level managers
    all see it, exactly as the manager they're concerned about is either
    the viewer or a descendant of the viewer)."""
    conn = get_db()
    q = "SELECT * FROM escalation_tickets WHERE 1=1"
    params = []
    if status:
        q += " AND status=?"
        params.append(status)
    if scope is not None:
        placeholders = ",".join("?" * len(scope)) if scope else "NULL"
        q += f" AND concerning_manager IN ({placeholders})"
        params += list(scope)
    q += " ORDER BY created_at DESC"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return rows


def get_escalation_ticket(ticket_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM escalation_tickets WHERE id=?", (ticket_id,)).fetchone()
    if not row:
        conn.close()
        return None
    ticket = dict(row)
    rec_rows = conn.execute(
        """SELECT etr.category as ticket_category, ti.* FROM escalation_ticket_records etr
           JOIN tracked_items ti ON ti.id = etr.tracked_item_id WHERE etr.ticket_id=?""",
        (ticket_id,),
    ).fetchall()
    comment_rows = conn.execute(
        "SELECT * FROM escalation_ticket_comments WHERE ticket_id=? ORDER BY posted_at ASC", (ticket_id,)
    ).fetchall()
    conn.close()
    ticket["records"] = [dict(r) for r in rec_rows]
    ticket["comments"] = [dict(r) for r in comment_rows]
    ticket["category_list"] = [c for c in (ticket["categories"] or "").split(",") if c]
    return ticket


def resolve_escalation_ticket(ticket_id, resolved_by):
    conn = get_db()
    conn.execute(
        "UPDATE escalation_tickets SET status='resolved', resolved_by=?, resolved_at=? WHERE id=?",
        (resolved_by, _now(), ticket_id),
    )
    conn.commit()
    conn.close()


def escalate_ticket_phase(ticket_id, new_target_login, escalated_by):
    """Trainer-only action: bumps the ticket to the next phase (capped at
    ESCALATION_MAX_PHASE) and reassigns target_login to a different
    manager. concerning_manager — and therefore who can view/comment on
    it — is unchanged; only who currently owns actioning it changes."""
    conn = get_db()
    row = conn.execute("SELECT phase FROM escalation_tickets WHERE id=?", (ticket_id,)).fetchone()
    if not row:
        conn.close()
        return False
    if row["phase"] >= ESCALATION_MAX_PHASE:
        conn.close()
        return False
    next_phase = row["phase"] + 1
    stage = compute_escalation_stage(new_target_login)
    conn.execute(
        "UPDATE escalation_tickets SET phase=?, target_login=?, stage=? WHERE id=?",
        (next_phase, new_target_login, stage, ticket_id),
    )
    conn.commit()
    conn.close()
    return True


def add_escalation_comment(ticket_id, comment, comment_type, posted_by):
    conn = get_db()
    conn.execute(
        "INSERT INTO escalation_ticket_comments (ticket_id, comment, comment_type, posted_by, posted_at) VALUES (?,?,?,?,?)",
        (ticket_id, comment, comment_type, posted_by, _now()),
    )
    conn.commit()
    conn.close()


LDOC_SYSTEM_NAME = "LDOC"


def set_escalation_ticket_due_date(ticket_id, due_date, set_by):
    """Sets (or clears) a ticket's due date and posts an automatic
    system update announcing it, from the app's own identity (LDOC) —
    e.g. 'Due date has been set for Thursday.' Clearing it (due_date=
    None) doesn't post an update; there's nothing to announce."""
    conn = get_db()
    conn.execute("UPDATE escalation_tickets SET due_date=? WHERE id=?", (due_date, ticket_id))
    conn.commit()
    conn.close()
    if due_date:
        try:
            weekday = datetime.strptime(due_date, "%Y-%m-%d").strftime("%A")
            msg = f"Due date has been set for {weekday}."
        except ValueError:
            msg = f"Due date has been set for {due_date}."
        add_escalation_comment(ticket_id, msg, "system", posted_by=LDOC_SYSTEM_NAME)


def ensure_due_today_reminders():
    """Scans every open ticket with a due date on or before today and,
    for any that hasn't already gotten today's LDOC reminder, posts one
    — 'Due date is today please review' the day it's actually due, or a
    plain still-overdue note on any day after. Safe to call repeatedly
    (called whenever the Updates page loads) since it only ever posts
    once per ticket per calendar day — the LIKE filter is scoped to
    reminder-type messages specifically, so it can't collide with the
    differently-worded 'due date has been set' announcement above."""
    today = date.today().isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, due_date FROM escalation_tickets WHERE status='open' AND due_date IS NOT NULL AND due_date <= ?",
        (today,),
    ).fetchall()
    for row in rows:
        ticket_id = row["id"]
        already = conn.execute(
            "SELECT 1 FROM escalation_ticket_comments WHERE ticket_id=? AND posted_by=? "
            "AND comment_type='system' AND substr(posted_at,1,10)=? AND comment LIKE ?",
            (ticket_id, LDOC_SYSTEM_NAME, today, "Due date is%"),
        ).fetchone()
        if already:
            continue
        if row["due_date"] == today:
            msg = "Due date is today please review"
        else:
            msg = f"Due date was {row['due_date']} — still needs review."
        conn.execute(
            "INSERT INTO escalation_ticket_comments (ticket_id, comment, comment_type, posted_by, posted_at) VALUES (?,?,?,?,?)",
            (ticket_id, msg, "system", LDOC_SYSTEM_NAME, _now()),
        )
    conn.commit()
    conn.close()


def get_all_escalation_updates(unreviewed_only=False):
    """Every comment/verification-request across every ticket, newest
    first — the Trainer Overview 'Updates' page. Joined with the ticket's
    number/stage/concerning manager so a trainer doesn't need to open the
    ticket just to see what it's about."""
    conn = get_db()
    q = """SELECT c.*, t.ticket_number, t.stage, t.concerning_manager, t.categories, t.status as ticket_status
           FROM escalation_ticket_comments c JOIN escalation_tickets t ON t.id = c.ticket_id"""
    params = []
    if unreviewed_only:
        q += " WHERE c.reviewed=0"
    q += " ORDER BY c.posted_at DESC"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return rows


def mark_escalation_comment_reviewed(comment_id, reviewed_by):
    conn = get_db()
    conn.execute(
        "UPDATE escalation_ticket_comments SET reviewed=1, reviewed_by=?, reviewed_at=? WHERE id=?",
        (reviewed_by, _now(), comment_id),
    )
    conn.commit()
    conn.close()


def get_effective_role_for_login(login):
    conn = get_db()
    row = conn.execute("SELECT role FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    return row["role"] if row else None


def find_senior_for_am(am_login):
    """Same automatic senior-ops lookup used for Phase 3 escalation
    targeting, applied here to group the Escalations reporting tab —
    returns None (the 'Other' bucket) if the chain has nobody titled
    senior ops (role 'som' or a fuzzy-matched title)."""
    return find_senior_ops_for_login(am_login)


def get_all_seniors():
    """Every account that's an actual senior — role 'som', or a title
    that fuzzy-matches General Manager / Site Lead — regardless of
    whether they currently have any open escalations. This is the base
    list the Escalations reporting tab now enumerates from, so a senior
    with zero escalations still shows a card instead of being invisible."""
    conn = get_db()
    rows = conn.execute("SELECT login, role, title, full_name FROM user_roles").fetchall()
    conn.close()
    seniors, seen = [], set()
    for r in rows:
        if r["login"] in seen:
            continue
        if r["role"] == "som" or _is_gm_or_site_lead_title(r["title"]):
            seniors.append({"login": r["login"], "role": r["role"], "title": r["title"], "full_name": r["full_name"]})
            seen.add(r["login"])
    return seniors


def get_escalation_summary_by_senior():
    """Every Senior Operations Manager and General Manager/Site Lead
    account, each with the Phase 1/2/3 counts of every open escalation —
    record escalations AND Log Escalation tickets both — assigned to
    them or anywhere in their direct-or-indirect reporting tree. An
    'Other' bucket catches anything whose chain doesn't resolve up to a
    configured senior at all."""
    seniors = get_all_seniors()
    by_senior = {}
    for s in seniors:
        scope = {s["login"]} | set(get_descendant_ams(s["login"]).keys())
        by_senior[s["login"]] = {
            "senior_login": s["login"], "senior_name": s["full_name"], "senior_title": s["title"],
            "phase_1": 0, "phase_2": 0, "phase_3": 0, "total": 0,
            "record_escalations": [], "tickets": [], "_scope": scope,
        }
    other = {
        "senior_login": "other", "senior_name": None, "senior_title": None,
        "phase_1": 0, "phase_2": 0, "phase_3": 0, "total": 0,
        "record_escalations": [], "tickets": [],
    }

    def place(item, login_field, list_field):
        target_login = item[login_field]
        for bucket in by_senior.values():
            if target_login in bucket["_scope"]:
                bucket[f"phase_{item['phase']}"] += 1
                bucket["total"] += 1
                bucket[list_field].append(item)
                return
        other[f"phase_{item['phase']}"] += 1
        other["total"] += 1
        other[list_field].append(item)

    for e in get_all_open_record_escalations():
        place(e, "am_login", "record_escalations")
    for t in get_escalation_tickets(status="open"):
        place(t, "concerning_manager", "tickets")

    result = list(by_senior.values())
    for b in result:
        del b["_scope"]
    result.append(other)
    return result


def get_phase_3_escalations():
    """Every open Phase 3 escalation, both types, org-wide — the highest
    tier, so this is the dedicated 'needs senior attention now' overview
    on the Escalations reporting tab."""
    return {
        "record_escalations": [e for e in get_all_open_record_escalations() if e["phase"] == 3],
        "tickets": [t for t in get_escalation_tickets(status="open") if t["phase"] == 3],
    }


# --------------------------------------------------------- trainer metrics -

def add_trainer_metric(trainer_login, full_name, fc, metric_name, metric_value, period):
    conn = get_db()
    conn.execute(
        """INSERT INTO trainer_metrics (trainer_login, full_name, fc, metric_name, metric_value, period, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (trainer_login, full_name, fc, metric_name, metric_value, period, _now()),
    )
    conn.commit()
    conn.close()


def get_trainer_metrics(period=None):
    conn = get_db()
    if period:
        rows = conn.execute(
            "SELECT * FROM trainer_metrics WHERE period=? ORDER BY trainer_login, metric_name", (period,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM trainer_metrics ORDER BY period DESC, trainer_login").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ ops structure

def add_ops_role(role_title, person_name, reports_to, scope_note, sort_order=0):
    conn = get_db()
    conn.execute(
        """INSERT INTO ops_structure (role_title, person_name, reports_to, scope_note, sort_order)
           VALUES (?,?,?,?,?)""",
        (role_title, person_name, reports_to, scope_note, sort_order),
    )
    conn.commit()
    conn.close()


def get_ops_structure():
    conn = get_db()
    rows = conn.execute("SELECT * FROM ops_structure ORDER BY sort_order, role_title").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_ops_role(role_id):
    conn = get_db()
    conn.execute("DELETE FROM ops_structure WHERE id=?", (role_id,))
    conn.commit()
    conn.close()


# ------------------------------------------------------ L&D Management ----
# Shift-based weekly training plans (Early / Late / Night), the trainings
# catalogue behind them (L&D Settings), and the compliance-driven
# recommendation engine that feeds both the shift boards and the AM Overview.

def week_start_for(anchor=None):
    """Sunday (ISO date string) of the week containing `anchor` (an ISO
    date string, or today if omitted)."""
    if anchor:
        try:
            d = datetime.strptime(anchor[:10], "%Y-%m-%d").date()
        except ValueError:
            d = date.today()
    else:
        d = date.today()
    # Python's weekday(): Mon=0..Sun=6. We want weeks starting Sunday.
    offset = (d.weekday() + 1) % 7
    sunday = d.fromordinal(d.toordinal() - offset)
    return sunday.isoformat()


def week_dates(week_start):
    d = datetime.strptime(week_start, "%Y-%m-%d").date()
    return [d.fromordinal(d.toordinal() + i).isoformat() for i in range(7)]


def iso_calendar_week(week_start):
    """The 'CWnn' label for a Sunday-anchored week_start — uses the
    Thursday of that week to resolve the ISO week number, since ISO
    8601 weeks are Monday-anchored and the Sunday/Monday boundary would
    otherwise occasionally put week_start's own date in the wrong ISO
    week right at a year boundary."""
    d = datetime.strptime(week_start, "%Y-%m-%d").date()
    thursday = d.fromordinal(d.toordinal() + 4)
    return f"CW{thursday.isocalendar()[1]:02d}"


def adjacent_week(week_start, delta_weeks):
    d = datetime.strptime(week_start, "%Y-%m-%d").date()
    return d.fromordinal(d.toordinal() + 7 * delta_weeks).isoformat()


def calendar_week_label(week_start):
    """Friendly calendar-week label — 'Week 33 · Aug 9–15, 2026' — for a
    Sunday-start week_start string. Uses the week's Wednesday to derive
    the ISO week number so it stays stable even though our weeks start
    Sunday rather than the ISO Monday."""
    d = datetime.strptime(week_start, "%Y-%m-%d").date()
    end = d.fromordinal(d.toordinal() + 6)
    week_num = d.fromordinal(d.toordinal() + 3).isocalendar()[1]
    if d.month == end.month:
        span = f"{d.strftime('%b %-d')}\u2013{end.strftime('%-d, %Y')}"
    else:
        span = f"{d.strftime('%b %-d')} \u2013 {end.strftime('%b %-d, %Y')}"
    return f"Week {week_num} \u00b7 {span}"


# --------------------------------------------------------- trainings ------

def add_training(name, category, validity_note):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO trainings (name, category, validity_note, active, created_at) VALUES (?,?,?,1,?)",
        (name, category, validity_note, _now()),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def get_trainings(active_only=False):
    conn = get_db()
    q = "SELECT * FROM trainings"
    if active_only:
        q += " WHERE active=1"
    rows = conn.execute(q + " ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_training_active(training_id, active):
    conn = get_db()
    conn.execute("UPDATE trainings SET active=? WHERE id=?", (1 if active else 0, training_id))
    conn.commit()
    conn.close()


def update_training(training_id, name, category, validity_note):
    conn = get_db()
    conn.execute(
        "UPDATE trainings SET name=?, category=?, validity_note=? WHERE id=?",
        (name, category, validity_note, training_id),
    )
    conn.commit()
    conn.close()


# ------------------------------------------- training <-> compliance topics
# Which Detailed Topics (from Safety Training Compliance) or DE Tech
# exceptions a given Training session actually serves. Drives both the
# "needs this training" panel and the fit-check when someone's added.

def get_available_topics():
    """Every topic a training can be linked to: Safety Compliance's
    Detailed Topics (from whatever data is loaded) plus the two DE Tech
    exceptions — each tagged with its section so ambiguous topic names
    across sections never collide."""
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT subcategory FROM tracked_items WHERE section='compliance_safety' AND subcategory IS NOT NULL ORDER BY subcategory"
    ).fetchall()
    conn.close()
    topics = [{"section": "compliance_safety", "topic": r["subcategory"]} for r in rows]
    topics += [{"section": "planning_de_tech", "topic": t} for t in DE_TECH_CENTRALLY_SCHEDULED]
    return topics


def set_training_topics(training_id, links):
    """links: list of (section, topic) tuples — replaces whatever was
    linked before."""
    conn = get_db()
    conn.execute("DELETE FROM training_topic_links WHERE training_id=?", (training_id,))
    for section, topic in links:
        conn.execute(
            "INSERT INTO training_topic_links (training_id, section, topic) VALUES (?,?,?) ON CONFLICT (training_id, section, topic) DO NOTHING",
            (training_id, section, topic),
        )
    conn.commit()
    conn.close()


def get_training_topics(training_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT section, topic FROM training_topic_links WHERE training_id=? ORDER BY topic",
        (training_id,),
    ).fetchall()
    conn.close()
    return [(r["section"], r["topic"]) for r in rows]


def check_attendee_fit(training_id, employee_login):
    """Does adding this associate to this training actually make sense,
    given what the training is linked to serve? Three outcomes:
    - No topics linked at all -> nothing to check, silently fine.
    - Associate has an open (gap/risk) record for a linked topic -> fine.
    - Associate's only record(s) for linked topics are already compliant
      -> a soft warning (they might not need it).
    - Associate has NO record at all for any linked topic -> a mismatch:
      this training doesn't appear to serve anything they actually need."""
    topics = get_training_topics(training_id)
    if not topics:
        return {"warning": None, "mismatch": False, "reason": None}

    sections = sorted({s for s, _ in topics})
    topic_set = set(topics)
    rows = get_items(section=sections, employee_login=employee_login)
    rows = [r for r in rows if (r["section"], r.get("subcategory")) in topic_set]

    if not rows:
        topic_names = ", ".join(t for _, t in topics)
        return {
            "warning": None,
            "mismatch": True,
            "reason": f"No record shows this associate needs any of: {topic_names}.",
        }

    gap_rows = [r for r in rows if status_bucket(r["status"]) in ("risk", "gap")]
    if gap_rows:
        return {"warning": None, "mismatch": False, "reason": None}

    compliant = rows[0]
    due = compliant.get("due_date")
    msg = f"This associate might not need this training — already compliant on '{compliant.get('subcategory')}'"
    msg += f", valid until {due}." if due else "."
    return {"warning": msg, "mismatch": False, "reason": None}


# ------------------------------------------------------ training slots ----

def add_training_slot(shift, week_start, day_index, training_id, start_time,
                       capacity, instructor, room, notes, created_by, status="approved", target_topic=None, metric_key=None):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO training_slots
           (shift, week_start, day_index, training_id, start_time, capacity,
            instructor, room, notes, created_by, created_at, status, target_topic, metric_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (shift, week_start, day_index, training_id or None, start_time,
         capacity or None, instructor, room, notes, created_by, _now(), status, target_topic, metric_key),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def generate_trainer_draft(metric_key, shift, week_start, created_by, capacity=8, min_capacity=None):
    """Builds a DRAFT weekly plan for one Trainer-planned metric (see
    TRAINER_PLANNED_METRICS — Safety Compliance in full, or DE Tech's
    two centrally-scheduled topics) for one shift — groups everyone
    currently overdue/gapped on each topic into slots of up to
    `capacity` people, spread round-robin across Monday-Friday so no
    single day is overloaded. A chunk smaller than min_capacity (see
    get_trainer_draft_min_capacity) is NOT turned into its own slot —
    running a session for one or two people isn't worth an instructor's
    time, so those people stay unscheduled and get picked up
    automatically the next time this runs, once enough of them have
    accumulated. Each slot is linked to the trainings catalog entry
    covering its topic when one exists (see find_training_id_for_topic),
    so it shows a real name instead of 'Untitled training.' No
    start_time is set — that's deliberately left for the Trainer to
    decide during review (see approve_draft_slot). Only ever adds new
    draft slots for people not already scheduled somewhere this
    week/shift (draft or approved) — re-running this after a Trainer
    has already started editing a previous draft, or after new people
    become overdue mid-week, adds only the new gaps rather than
    duplicating anyone. Discarding an unwanted previous draft is a
    separate, explicit action.
    Returns (created_slot_ids, held_back_count) — held_back_count is how
    many people were left unscheduled for being under min_capacity."""
    min_capacity = get_trainer_draft_min_capacity() if min_capacity is None else min_capacity
    sections, topic_filter = TRAINER_PLANNED_METRICS[metric_key]
    people = get_trainer_planned_gaps(sections, topic_filter=topic_filter).get(shift, [])
    if not people:
        return [], 0

    # Exclude anyone already scheduled (draft or approved) this week/shift
    # — otherwise re-running this after new people become overdue would
    # duplicate everyone already in a slot from the first run.
    conn = get_db()
    already_scheduled = {
        r["employee_login"] for r in conn.execute(
            """SELECT DISTINCT sa.employee_login FROM slot_attendees sa
               JOIN training_slots ts ON ts.id = sa.slot_id
               WHERE ts.week_start=? AND ts.shift=?""",
            (week_start, shift),
        ).fetchall()
    }
    conn.close()
    people = [p for p in people if p["employee_login"] not in already_scheduled]
    if not people:
        return [], 0

    by_topic = {}
    for p in people:
        by_topic.setdefault(p["topic"], []).append(p)

    created_slot_ids = []
    held_back_count = 0
    day_cursor = 0
    for topic, topic_people in sorted(by_topic.items()):
        training_id = find_training_id_for_topic(sections, topic)
        for i in range(0, len(topic_people), capacity):
            chunk = topic_people[i:i + capacity]
            if len(chunk) < min_capacity:
                held_back_count += len(chunk)
                continue
            day_index = day_cursor % 5  # Monday-Friday only, by default
            day_cursor += 1
            slot_id = add_training_slot(
                shift=shift, week_start=week_start, day_index=day_index,
                training_id=training_id, start_time=None, capacity=capacity,
                instructor=None, room=None,
                notes=f"Auto-generated draft — {len(chunk)} associate(s) overdue on {topic}",
                created_by=created_by, status="draft", target_topic=topic, metric_key=metric_key,
            )
            created_slot_ids.append(slot_id)
            conn = get_db()
            for person in chunk:
                conn.execute(
                    """INSERT INTO slot_attendees (slot_id, employee_login, full_name, fc, added_by, added_at)
                       VALUES (?,?,?,?,?,?)""",
                    (slot_id, person["employee_login"], person["full_name"], None, created_by, _now()),
                )
            conn.commit()
            conn.close()
    return created_slot_ids, held_back_count



def get_draft_training_slots(week_start, shift=None, metric_key=None):
    """Draft (not yet Trainer-approved) slots for a week, same shape as
    get_training_slots — for the Trainer review screen. Pass metric_key
    to narrow to one Trainer-planned metric (e.g. 'safety_compliance'
    vs 'de_tech') so each metric's review section only shows its own
    drafts."""
    conn = get_db()
    q = """SELECT ts.* FROM training_slots ts WHERE ts.week_start=? AND ts.status='draft'"""
    params = [week_start]
    if shift:
        q += " AND ts.shift=?"
        params.append(shift)
    if metric_key:
        q += " AND ts.metric_key=?"
        params.append(metric_key)
    q += " ORDER BY ts.day_index, ts.id"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    for r in rows:
        r["attendees"] = [dict(a) for a in conn.execute(
            "SELECT * FROM slot_attendees WHERE slot_id=? ORDER BY full_name", (r["id"],)
        ).fetchall()]
    conn.close()
    return rows


def approve_draft_slot(slot_id, start_time, approved_by, instructor=None, capacity=None, room=None):
    """A Trainer approving one draft slot — requires the actual time to
    be set (the one piece of this the algorithm deliberately leaves
    blank), and flips it from 'draft' to 'approved' so it becomes
    visible everywhere a normal training slot is (the Weekly Training
    Plan board, and any Area Manager-facing view of available Trainer
    slots). Who approved it is tracked separately in approved_by — it
    must never overwrite instructor, which is a different person (the
    actual Ambassador/instructor running the session, if the Trainer
    sets one), not whoever clicked Approve."""
    if not (start_time or "").strip():
        raise ValueError("A start time is required to approve a slot.")
    conn = get_db()
    sets = ["status='approved'", "start_time=?", "approved_by=?"]
    params = [start_time.strip(), approved_by]
    if instructor is not None:
        sets.append("instructor=?")
        params.append(instructor or None)
    if capacity is not None:
        sets.append("capacity=?")
        params.append(capacity)
    if room is not None:
        sets.append("room=?")
        params.append(room or None)
    params.append(slot_id)
    conn.execute(
        f"UPDATE training_slots SET {', '.join(sets)} WHERE id=? AND status='draft'",
        params,
    )
    conn.commit()
    conn.close()


def discard_draft_slot(slot_id):
    """Removes one draft slot (and its attendees) entirely — for a
    Trainer who edits a draft down to nothing useful, e.g. splitting an
    over-full slot by discarding it and creating two smaller ones by
    hand instead."""
    conn = get_db()
    conn.execute("DELETE FROM slot_attendees WHERE slot_id=?", (slot_id,))
    conn.execute("DELETE FROM training_slots WHERE id=? AND status='draft'", (slot_id,))
    conn.commit()
    conn.close()


def get_training_slots(week_start, shift=None):
    """Slots for a week (optionally one shift), each with its training name
    and its enrolled attendees attached. Only ever returns 'approved'
    slots — a Trainer's still-in-review draft must never show up on the
    normal Weekly Training Plan board or any Area Manager-facing view;
    see get_draft_training_slots for the Trainer's own review screen."""
    conn = get_db()
    q = """SELECT ts.*, t.name as training_name, t.category as training_category
           FROM training_slots ts LEFT JOIN trainings t ON t.id = ts.training_id
           WHERE ts.week_start = ? AND ts.status = 'approved'"""
    params = [week_start]
    if shift:
        q += " AND ts.shift = ?"
        params.append(shift)
    rows = conn.execute(q + " ORDER BY ts.day_index, ts.start_time", params).fetchall()
    slots = [dict(r) for r in rows]
    if slots:
        ids = [s["id"] for s in slots]
        att_rows = conn.execute(
            f"SELECT * FROM slot_attendees WHERE slot_id IN ({','.join('?'*len(ids))}) ORDER BY full_name",
            ids,
        ).fetchall()
        by_slot = {}
        for r in att_rows:
            by_slot.setdefault(r["slot_id"], []).append(dict(r))
        for s in slots:
            s["attendees"] = by_slot.get(s["id"], [])
    conn.close()
    return slots


def get_training_slot(slot_id):
    conn = get_db()
    row = conn.execute(
        """SELECT ts.*, t.name as training_name FROM training_slots ts
           LEFT JOIN trainings t ON t.id = ts.training_id WHERE ts.id=?""",
        (slot_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def edit_training_slot(slot_id, training_id=None, start_time=None, capacity=None, instructor=None, room=None, notes=None):
    """Updates an existing slot's own details in place — any argument
    left as None is left unchanged, so a caller only needs to pass the
    fields actually being edited. Works on a slot in any status
    (draft or approved); editing a draft this way is a valid
    alternative to discarding and letting the next generator run
    recreate it."""
    conn = get_db()
    sets, params = [], []
    if training_id is not None:
        sets.append("training_id=?"); params.append(training_id or None)
    if start_time is not None:
        sets.append("start_time=?"); params.append(start_time or None)
    if capacity is not None:
        sets.append("capacity=?"); params.append(capacity or None)
    if instructor is not None:
        sets.append("instructor=?"); params.append(instructor or None)
    if room is not None:
        sets.append("room=?"); params.append(room or None)
    if notes is not None:
        sets.append("notes=?"); params.append(notes or None)
    if not sets:
        conn.close()
        return
    params.append(slot_id)
    conn.execute(f"UPDATE training_slots SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()
    conn.close()


def delete_training_slot(slot_id):
    conn = get_db()
    conn.execute("DELETE FROM slot_attendees WHERE slot_id=?", (slot_id,))
    conn.execute("DELETE FROM training_slots WHERE id=?", (slot_id,))
    conn.commit()
    conn.close()


def add_slot_attendee(slot_id, employee_login, full_name, fc, added_by, flagged=False, flag_reason=None):
    """Returns (attendee_id, was_new) — was_new is False when this person
    was already enrolled in this exact slot, so callers can avoid drawing
    a second chip/row for a no-op duplicate add (the actual bug behind
    'double enrol doesn't show until refresh': the DB correctly ignored
    the duplicate, but the UI didn't know that and drew a phantom chip
    anyway)."""
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM slot_attendees WHERE slot_id=? AND employee_login=?",
        (slot_id, employee_login),
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"], False
    conn.execute(
        """INSERT INTO slot_attendees
           (slot_id, employee_login, full_name, fc, added_by, added_at, flagged, flag_reason)
           VALUES (?,?,?,?,?,?,?,?)""",
        (slot_id, employee_login, full_name, fc, added_by, _now(), 1 if flagged else 0, flag_reason),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM slot_attendees WHERE slot_id=? AND employee_login=?",
        (slot_id, employee_login),
    ).fetchone()
    conn.close()
    return (row["id"] if row else None), True


def get_planned_lookup(week_start):
    """(employee_login, section, topic) -> {attendee_id, day_index,
    start_time, flagged, flag_reason} for everyone already enrolled
    somewhere this week in a slot whose training serves that topic.

    This is the single source of truth 'planned' state everywhere reads
    from — the AM Overview needs-list, the Safety Compliance drill-down,
    and a slot's own needs-training panel all key off the same lookup, so
    enrolling someone from any one of them is immediately reflected in
    all the others (previously each screen had its own bespoke check, so
    an enrolment made in one place silently didn't show up in another
    until a full page reload)."""
    conn = get_db()
    rows = conn.execute(
        """SELECT sa.id as attendee_id, sa.employee_login, ts.day_index, ts.start_time,
                  sa.flagged, sa.flag_reason, ttl.section, ttl.topic
           FROM slot_attendees sa
           JOIN training_slots ts ON ts.id = sa.slot_id
           JOIN training_topic_links ttl ON ttl.training_id = ts.training_id
           WHERE ts.week_start = ?""",
        (week_start,),
    ).fetchall()
    conn.close()
    lookup = {}
    for r in rows:
        key = (r["employee_login"], r["section"], r["topic"])
        lookup[key] = {
            "attendee_id": r["attendee_id"], "day_index": r["day_index"], "start_time": r["start_time"],
            "flagged": bool(r["flagged"]), "flag_reason": r["flag_reason"],
        }
    return lookup


def attach_planned_state(items, week_start, lookup=None):
    """Enriches a list of tracked_items-like dicts (each needs employee_login,
    section, subcategory) with planned_* fields from get_planned_lookup."""
    lookup = lookup if lookup is not None else get_planned_lookup(week_start)
    for it in items:
        key = (it.get("employee_login"), it.get("section"), it.get("subcategory"))
        p = lookup.get(key)
        it["planned_attendee_id"] = p["attendee_id"] if p else None
        it["planned_day_index"] = p["day_index"] if p else None
        it["planned_start_time"] = p["start_time"] if p else None
        it["planned_flagged"] = p["flagged"] if p else False
        it["planned_flag_reason"] = p["flag_reason"] if p else None
    return items


def get_needs_training_for_slot(slot_id, scope_am=None, scope_shift=None, within_days=60):
    """Associates who need the training a given slot is offering, most
    overdue first, capped to a 60-day-out horizon. Scoped either to one
    manager's own team (scope_am) or to a whole shift regardless of
    manager (scope_shift) — never both. Each result also carries whether
    they're already scheduled somewhere this week for this same training,
    and if so, whether that placement was flagged."""
    slot = get_training_slot(slot_id)
    if not slot or not slot.get("training_id"):
        return []
    topics = get_training_topics(slot["training_id"])
    if not topics:
        return []

    sections = sorted({s for s, _ in topics})
    topic_set = set(topics)
    today = date.today()
    horizon_days = within_days

    rows = get_items(section=sections, am_login=scope_am)
    if scope_shift:
        rows = [r for r in rows if r.get("shift") == scope_shift]
    rows = [r for r in rows if (r["section"], r.get("subcategory")) in topic_set]

    out = []
    for r in rows:
        if status_bucket(r["status"]) not in ("risk", "gap"):
            continue
        days = None
        if r.get("due_date"):
            try:
                d = datetime.strptime(r["due_date"][:10], "%Y-%m-%d").date()
                days = (d - today).days
            except ValueError:
                days = None
        if days is not None and days > horizon_days:
            continue
        r = dict(r)
        r["days_to_due"] = days
        out.append(r)
    out.sort(key=lambda r: (r["days_to_due"] if r["days_to_due"] is not None else 999999))

    return attach_planned_state(out, slot["week_start"])


def get_planned_calendar(week_start, am_login=None):
    """Sun–Sat view of who's planned into what this week — the AM Overview
    Planning tab's calendar. Scoped to one manager's own associates via a
    join against org_map (which is where an associate's am_login actually
    lives — slot_attendees itself doesn't store it), or org-wide if no
    am_login is given (OM/SOM/admin with nothing picked)."""
    conn = get_db()
    q = """SELECT ts.day_index, ts.start_time, ts.shift, ts.training_id,
                  t.name as training_name, sa.employee_login, sa.full_name,
                  sa.flagged, sa.flag_reason, om.am_login
           FROM training_slots ts
           LEFT JOIN trainings t ON t.id = ts.training_id
           JOIN slot_attendees sa ON sa.slot_id = ts.id
           LEFT JOIN org_map om ON om.employee_login = sa.employee_login
           WHERE ts.week_start = ?"""
    params = [week_start]
    if am_login:
        if isinstance(am_login, (list, tuple)):
            q += f" AND om.am_login IN ({','.join('?' * len(am_login))})"
            params.extend(am_login)
        else:
            q += " AND om.am_login = ?"
            params.append(am_login)
    rows = conn.execute(q + " ORDER BY ts.day_index, ts.start_time", params).fetchall()
    conn.close()

    calendar = {i: {} for i in range(7)}
    for r in rows:
        day = calendar[r["day_index"]]
        key = (r["training_id"], r["start_time"], r["shift"])
        session = day.setdefault(key, {
            "training_name": r["training_name"] or "Untitled training",
            "start_time": r["start_time"],
            "shift": r["shift"],
            "attendees": [],
        })
        session["attendees"].append({
            "employee_login": r["employee_login"],
            "full_name": r["full_name"],
            "flagged": bool(r["flagged"]),
            "flag_reason": r["flag_reason"],
        })
    return {day: sorted(sessions.values(), key=lambda s: s["start_time"] or "") for day, sessions in calendar.items()}


def remove_slot_attendee(attendee_id):
    conn = get_db()
    conn.execute("DELETE FROM slot_attendees WHERE id=?", (attendee_id,))
    conn.commit()
    conn.close()


def remove_slot_attendee_by_login(slot_id, employee_login):
    """Same as remove_slot_attendee, but keyed by (slot_id, login) rather
    than the attendee row's own id — for the draft-review screen, where
    the login is what's already on hand from the roster list."""
    conn = get_db()
    conn.execute(
        "DELETE FROM slot_attendees WHERE slot_id=? AND employee_login=?",
        (slot_id, employee_login),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------- compliance gaps / recs -

def get_compliance_gaps(am_login=None, fc=None, limit=None, week_start=None):
    """Associates with an at-risk or gapped record in something L&D
    actually schedules via the Weekly Training Plan — Safety Compliance,
    plus the two DE Tech trainings that are centrally offered despite DE
    Tech generally being the managers' own responsibility on the floor.
    Most urgent (soonest/most-overdue expiry) first. Pass week_start to
    also get each item's planned_* fields (see attach_planned_state) so
    UIs built on this can show "Planned for X" instead of a raw due date
    for anyone already enrolled somewhere this week."""
    items = get_items(section="compliance_safety", am_login=am_login, fc=fc)
    de_tech_items = [
        it for it in get_items(section="planning_de_tech", am_login=am_login, fc=fc)
        if it.get("subcategory") in DE_TECH_CENTRALLY_SCHEDULED
    ]
    items = items + de_tech_items
    today = date.today()
    out = []
    for it in items:
        if status_bucket(it["status"]) not in ("risk", "gap"):
            continue
        days = None
        if it.get("due_date"):
            try:
                d = datetime.strptime(it["due_date"][:10], "%Y-%m-%d").date()
                days = (d - today).days
            except ValueError:
                days = None
        it = dict(it)
        it["days_to_expiry"] = days
        out.append(it)
    out.sort(key=lambda r: (r["days_to_expiry"] if r["days_to_expiry"] is not None else 999999))
    if limit:
        out = out[:limit]
    if week_start:
        attach_planned_state(out, week_start)
    return out


def get_recommended_for_week(week_start, am_login=None, fc=None, top_n=15):
    """Compliance gaps not yet enrolled in any slot this week for a matching
    topic — i.e. who L&D should actually plan this week to stay compliant."""
    gaps = get_compliance_gaps(am_login=am_login, fc=fc)
    conn = get_db()
    rows = conn.execute(
        """SELECT sa.employee_login, t.name as training_name
           FROM slot_attendees sa
           JOIN training_slots ts ON ts.id = sa.slot_id
           LEFT JOIN trainings t ON t.id = ts.training_id
           WHERE ts.week_start = ?""",
        (week_start,),
    ).fetchall()
    conn.close()
    scheduled = {(r["employee_login"], (r["training_name"] or "").strip().lower()) for r in rows}
    recs = [
        g for g in gaps
        if (g["employee_login"], (g.get("subcategory") or "").strip().lower()) not in scheduled
    ]
    return recs[:top_n]


def get_weekly_training_recommendations(week_start, fc=None, top_n=5):
    """Per shift, which trainings L&D should offer that week — ranked by
    how many people currently have an open (gap/risk) record for it,
    excluding anything already scheduled that week for that shift. Each
    recommendation also breaks down how many of those people are already
    expired/not compliant vs. still within the 60-day runway."""
    gaps = get_compliance_gaps(fc=fc)
    groups = {}
    for g in gaps:
        shift = g.get("shift")
        if not shift:
            continue
        key = (shift, g.get("subcategory"))
        grp = groups.setdefault(key, {"employees": set(), "expired": 0, "due_60": 0, "no_date": 0})
        if g["employee_login"] in grp["employees"]:
            continue
        grp["employees"].add(g["employee_login"])
        days = g.get("days_to_expiry")
        if days is None:
            grp["no_date"] += 1
        elif days < 0:
            grp["expired"] += 1
        elif days <= 60:
            grp["due_60"] += 1

    conn = get_db()
    rows = conn.execute(
        """SELECT ts.shift, t.name as training_name
           FROM training_slots ts LEFT JOIN trainings t ON t.id = ts.training_id
           WHERE ts.week_start = ?""",
        (week_start,),
    ).fetchall()
    conn.close()
    scheduled = {(r["shift"], (r["training_name"] or "").strip().lower()) for r in rows}

    recs = {}
    for shift_key in SHIFTS:
        shift_items = [
            {
                "training": topic,
                "open_count": len(grp["employees"]),
                "expired_count": grp["expired"],
                "due_60_count": grp["due_60"],
                "no_date_count": grp["no_date"],
            }
            for (shift, topic), grp in groups.items()
            if shift == shift_key and (shift_key, (topic or "").strip().lower()) not in scheduled
        ]
        shift_items.sort(key=lambda r: -r["open_count"])
        recs[shift_key] = shift_items[:top_n]
    return recs


# ---------------------------------------------------- trainers / AM assignment ----
# Trainers are L&D's own org (an external consulting function relative to
# Operations). Each trainer is responsible for a set of Area Managers, who
# sit in the separate Operations org (AM / OM / Senior OM). This mapping
# drives the Trainer Overview roll-up.

def add_trainer(login, full_name):
    conn = get_db()
    conn.execute(
        "INSERT INTO trainers (login, full_name, created_at) VALUES (?,?,?) "
        "ON CONFLICT(login) DO UPDATE SET full_name=excluded.full_name",
        (login, full_name, _now()),
    )
    conn.commit()
    conn.close()


def delete_trainer(login):
    conn = get_db()
    conn.execute("DELETE FROM trainer_am_assignments WHERE trainer_login=?", (login,))
    conn.execute("DELETE FROM trainers WHERE login=?", (login,))
    conn.commit()
    conn.close()


def get_trainers():
    """Only ever returns logins that currently hold the Trainer role — a
    role revoke or change to something else quietly drops them from this
    roster (and from being assignable) without deleting their historical
    trainers-table row or past AM assignments."""
    conn = get_db()
    rows = conn.execute(
        """SELECT t.login, t.full_name FROM trainers t
           JOIN user_roles ur ON ur.login = t.login AND ur.role = 'trainer'
           ORDER BY t.login"""
    ).fetchall()
    trainers = [dict(r) for r in rows]
    for t in trainers:
        t["ams"] = get_ams_for_trainer(t["login"])
    conn.close()
    return trainers


def get_all_assigned_ams():
    """Every AM login that has ANY trainer at all, across the whole roster
    — used to compute who's still uncovered."""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT am_login FROM trainer_am_assignments").fetchall()
    conn.close()
    return {r["am_login"] for r in rows}


# ------------------------------------------------------ ambassadors -------

def add_ambassador(department, shift, login, full_name, added_by, is_process=True, is_indirect_roles=False):
    department = normalize_department(department)
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO ambassadors (department, shift, login, full_name, active, added_by, added_at,
                                     is_process_ambassador, is_indirect_roles_ambassador)
           VALUES (?,?,?,?,1,?,?,?,?)""",
        (department, shift, login.strip(), (full_name or "").strip() or None, added_by, _now(),
         1 if is_process else 0, 1 if is_indirect_roles else 0),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def set_ambassador_types(ambassador_id, is_process, is_indirect_roles):
    conn = get_db()
    conn.execute(
        "UPDATE ambassadors SET is_process_ambassador=?, is_indirect_roles_ambassador=? WHERE id=?",
        (1 if is_process else 0, 1 if is_indirect_roles else 0, ambassador_id),
    )
    conn.commit()
    conn.close()


def get_ambassador_indirect_roles(ambassador_id):
    """The specific Indirect Roles this ambassador is set up to train —
    each with the role's section/label from ir_role_config, since a bare
    ID isn't meaningful to show anyone."""
    conn = get_db()
    rows = conn.execute(
        """SELECT air.ir_role_config_id as role_id, irc.section, irc.role
           FROM ambassador_indirect_roles air
           JOIN ir_role_config irc ON irc.id = air.ir_role_config_id
           WHERE air.ambassador_id=? ORDER BY irc.section, irc.role""",
        (ambassador_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_ambassador_indirect_roles(ambassador_id, role_config_ids, assigned_by):
    """Replaces the full set of Indirect Roles this ambassador trains —
    the assignment UI resubmits the whole selection each time, matching
    how every other multi-select assignment in this app works."""
    conn = get_db()
    conn.execute("DELETE FROM ambassador_indirect_roles WHERE ambassador_id=?", (ambassador_id,))
    now = _now()
    for role_id in role_config_ids:
        conn.execute(
            "INSERT INTO ambassador_indirect_roles (ambassador_id, ir_role_config_id, assigned_by, assigned_at) VALUES (?,?,?,?)",
            (ambassador_id, role_id, assigned_by, now),
        )
    conn.commit()
    conn.close()


def get_indirect_roles_ambassadors(department=None, shift=None):
    """Every active ambassador flagged as an Indirect Roles ambassador,
    with the specific roles each is set up to train — the data behind
    the 'Indirect Roles Ambassadors' tab. Optionally narrowed to one
    department and/or shift."""
    conn = get_db()
    q = "SELECT * FROM ambassadors WHERE active=1 AND is_indirect_roles_ambassador=1"
    params = []
    if department:
        q += " AND department=?"
        params.append(normalize_department(department))
    if shift:
        q += " AND shift=?"
        params.append(shift)
    q += " ORDER BY department, full_name, login"
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    for a in rows:
        a["ir_roles"] = get_ambassador_indirect_roles(a["id"])
    return rows


def get_xt_employee_name(login):
    """Most recent known full name for a login, from the hours-on-function
    data — used to auto-fill the Add Ambassador form as soon as a login
    matching someone in that data is typed in."""
    login = (login or "").strip()
    if not login:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT full_name FROM xt_hours WHERE lower(employee_login)=? AND full_name IS NOT NULL "
        "ORDER BY updated_at DESC LIMIT 1",
        (login.lower(),),
    ).fetchone()
    conn.close()
    return row["full_name"] if row else None


def get_ambassador_process_hours(login, department):
    """This ambassador's last-60-day hours on each process tracked for
    their department (ambassador_process_definitions, admin-editable via
    Ambassador Definitions) — a process they were never trained on at
    all shows as 0, not missing, so it's flagged the same as a too-low
    number. Returns [] for a department with no processes defined."""
    processes = get_ambassador_processes_by_department().get(normalize_department(department))
    if not processes or not login:
        return []
    conn = get_db()
    placeholders = ",".join("?" * len(processes))
    rows = conn.execute(
        f"""SELECT merged_function, hours_60 FROM xt_hours
            WHERE lower(employee_login)=? AND lower(merged_function) IN ({placeholders})""",
        [login.strip().lower()] + [p.lower() for p in processes],
    ).fetchall()
    conn.close()
    by_lower = {}
    for r in rows:
        key = r["merged_function"].lower()
        by_lower[key] = (by_lower.get(key) or 0) + (r["hours_60"] or 0)
    out = []
    for p in processes:
        hrs = by_lower.get(p.lower())
        threshold = ambassador_process_threshold(p)
        out.append({
            "process": p,
            "hours_60": hrs,
            "required_hours": threshold,
            "flagged": hrs is None or hrs < threshold,
        })
    return out


def get_ambassador_readiness_summary(department, shift):
    """Whether each active ambassador in this department+shift has
    reached 20 hours on EVERY one of their tracked processes.
    An ambassador whose department has no tracked process list
    (AMBASSADOR_DEPT_PROCESSES has no entry — Ship Dock, ICQA) isn't
    counted either way, since there's nothing to check them against.
    Each ambassador carries their own meeting attendance record, and the
    summary carries the department+shift's pooled average attendance
    rate (attended / recorded meetings, across everyone with at least
    one recorded meeting). Scoped to Process Ambassadors specifically,
    same reasoning as get_ambassador_gap_summary — this is about
    process-training readiness."""
    department = normalize_department(department)
    roster = get_ambassadors(shift, department=department, ambassador_type="process")
    ambassadors = roster.get(department, [])
    ready, not_ready = [], []
    total_attended = total_meetings = 0
    for a in ambassadors:
        procs = a.get("process_hours") or get_ambassador_process_hours(a["login"], department)
        if not procs:
            continue
        a = dict(a)
        a["processes"] = procs
        attendance = get_ambassador_attendance_summary(a["id"])
        a["attendance"] = attendance
        total_attended += attendance["attended"]
        total_meetings += attendance["total"]
        if all(not p["flagged"] for p in procs):
            ready.append(a)
        else:
            not_ready.append(a)
    avg_attendance_pct = round(100 * total_attended / total_meetings) if total_meetings > 0 else None
    return {
        "ready": ready, "not_ready": not_ready, "total": len(ready) + len(not_ready),
        "avg_attendance_pct": avg_attendance_pct,
    }


def delist_ambassador(ambassador_id, delisted_by):
    """Soft-delist — keeps the row (and any meeting attendance history
    tied to it) rather than deleting it outright."""
    conn = get_db()
    conn.execute(
        "UPDATE ambassadors SET active=0, delisted_by=?, delisted_at=? WHERE id=?",
        (delisted_by, _now(), ambassador_id),
    )
    conn.commit()
    conn.close()


def relist_ambassador(ambassador_id, added_by):
    conn = get_db()
    conn.execute(
        "UPDATE ambassadors SET active=1, delisted_by=NULL, delisted_at=NULL, added_by=?, added_at=? WHERE id=?",
        (added_by, _now(), ambassador_id),
    )
    conn.commit()
    conn.close()


def get_ambassador_targets():
    """department -> {shift: target}, for the Definitions page's editable
    table — every department/shift combo always has a row after seeding,
    so this is never sparse."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM ambassador_targets").fetchall()
    conn.close()
    out = {d: {} for d in AMBASSADOR_DEPARTMENTS}
    for r in rows:
        out.setdefault(r["department"], {})[r["shift"]] = r["target"]
    return out


def bulk_set_ambassador_targets(rows):
    """rows: list of (department, shift, target) — the Definitions
    page\'s single \'Save all changes\' button writes every cell in one
    call."""
    conn = get_db()
    for department, shift, target in rows:
        conn.execute(
            "INSERT INTO ambassador_targets (department, shift, target) VALUES (?,?,?) "
            "ON CONFLICT (department, shift) DO UPDATE SET target=excluded.target",
            (department, shift, target),
        )
    conn.commit()
    conn.close()


@request_memoize
def get_ambassador_gap_summary(shift):
    """Every department's current active Process-Ambassador headcount
    vs its target for this shift, plus the gap (current - target;
    negative = short). The gap-summary strip under the shift toggle,
    and the basis for the Ambassador Availability scorecard tile —
    scoped to Process Ambassadors specifically, since this target is
    about process-training coverage; an Indirect-Roles-only ambassador
    doesn't count toward it. Ambassador rows stored under an informal
    department alias (Chutings, IC/QA/CS, ...) still get counted
    toward their canonical department here."""
    conn = get_db()
    counts = {}
    for r in conn.execute(
        "SELECT department, COUNT(*) as n FROM ambassadors WHERE shift=? AND active=1 AND is_process_ambassador=1 GROUP BY department", (shift,)
    ).fetchall():
        canonical = normalize_department(r["department"])
        counts[canonical] = counts.get(canonical, 0) + r["n"]
    targets = {r["department"]: r["target"] for r in conn.execute(
        "SELECT department, target FROM ambassador_targets WHERE shift=?", (shift,)
    ).fetchall()}
    conn.close()
    out = []
    for d in AMBASSADOR_DEPARTMENTS:
        current = counts.get(d, 0)
        target = targets.get(d, 0)
        out.append({"department": d, "shift": shift, "current": current, "target": target, "gap": current - target})
    return out


def get_ambassador_attendance_summary(ambassador_id):
    """\'attended X of Y recorded meetings\' for one ambassador — the
    Meeting Attendance column on the Ambassador Availability drill-down."""
    conn = get_db()
    rows = conn.execute(
        "SELECT attended FROM ambassador_meeting_attendance WHERE ambassador_id=? ORDER BY recorded_at DESC",
        (ambassador_id,),
    ).fetchall()
    conn.close()
    total = len(rows)
    attended = sum(1 for r in rows if r["attended"])
    return {"attended": attended, "total": total}


def get_ambassadors(shift, department=None, active_only=True, ambassador_type=None):
    """Ambassador roster for a shift, grouped by department (in the
    fixed AMBASSADOR_DEPARTMENTS order) — each entry the list of
    ambassadors for that department. department is normalized before
    querying, and matches against every known alias of it too (an
    ambassador stored under an informal name like 'Chutings' still
    needs to show up when someone asks for 'AFE'). ambassador_type
    narrows to 'process' or 'indirect_roles' — an ambassador can be
    both, so this only excludes someone who is genuinely neither."""
    conn = get_db()
    hp_group = department_group(normalize_department(department)) if department else None
    q = "SELECT * FROM ambassadors WHERE shift=?"
    params = [shift]
    if hp_group:
        q += f" AND department IN ({','.join('?' * len(hp_group))})"
        params += list(hp_group)
    if active_only:
        q += " AND active=1"
    if ambassador_type == "process":
        q += " AND is_process_ambassador=1"
    elif ambassador_type == "indirect_roles":
        q += " AND is_indirect_roles_ambassador=1"
    q += " ORDER BY department, full_name, login"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    by_dept = {d: [] for d in AMBASSADOR_DEPARTMENTS}
    for r in rows:
        a = dict(r)
        a["process_hours"] = get_ambassador_process_hours(a["login"], a["department"])
        by_dept.setdefault(normalize_department(a["department"]), []).append(a)
    return by_dept


def get_ambassadors_for_departments(shift, departments, active_only=True):
    """Flat list (not grouped) of active ambassadors across a set of
    departments for one shift — what a combined meeting group needs."""
    conn = get_db()
    placeholders = ",".join("?" * len(departments))
    q = f"SELECT * FROM ambassadors WHERE shift=? AND department IN ({placeholders})"
    params = [shift] + list(departments)
    if active_only:
        q += " AND active=1"
    q += " ORDER BY department, full_name, login"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_ambassador(ambassador_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM ambassadors WHERE id=?", (ambassador_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_or_create_ambassador_meeting(week_start, shift, group_key, created_by):
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM ambassador_meetings WHERE week_start=? AND shift=? AND group_key=?",
        (week_start, shift, group_key),
    ).fetchone()
    if row:
        conn.close()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO ambassador_meetings (week_start, shift, group_key, created_by, created_at, status) VALUES (?,?,?,?,?,'held')",
        (week_start, shift, group_key, created_by, _now()),
    )
    meeting_id = cur.lastrowid
    conn.commit()
    conn.close()
    return meeting_id


def get_ambassador_meeting_roster(week_start, shift, group_key):
    """Every active ambassador in this meeting group's departments (for
    this shift), each carrying their attendance row for this specific
    week if one's been recorded yet — the shape the attendance-marking
    UI needs. Doesn't write anything; the meeting row itself is only
    created when attendance, notes, cancellation, or a document is
    actually saved."""
    label, depts = AMBASSADOR_MEETING_GROUP_MAP[group_key]
    ambassadors = get_ambassadors_for_departments(shift, depts)
    conn = get_db()
    meeting_row = conn.execute(
        "SELECT * FROM ambassador_meetings WHERE week_start=? AND shift=? AND group_key=?",
        (week_start, shift, group_key),
    ).fetchone()
    attendance_by_amb = {}
    meeting = None
    documents = []
    if meeting_row:
        meeting = dict(meeting_row)
        rows = conn.execute(
            "SELECT * FROM ambassador_meeting_attendance WHERE meeting_id=?", (meeting["id"],)
        ).fetchall()
        attendance_by_amb = {r["ambassador_id"]: dict(r) for r in rows}
        doc_rows = conn.execute(
            "SELECT * FROM ambassador_meeting_documents WHERE meeting_id=? ORDER BY uploaded_at DESC", (meeting["id"],)
        ).fetchall()
        documents = [dict(r) for r in doc_rows]
    conn.close()
    for a in ambassadors:
        a["attendance"] = attendance_by_amb.get(a["id"])
    return {
        "label": label, "departments": depts, "meeting": meeting,
        "meeting_id": meeting["id"] if meeting else None,
        "status": meeting["status"] if meeting else "held",
        "ambassadors": ambassadors, "documents": documents,
    }


def set_ambassador_attendance(week_start, shift, group_key, ambassador_id, attendance_status, notes, recorded_by):
    ambassador = get_ambassador(ambassador_id)
    if not ambassador:
        return
    meeting_id = get_or_create_ambassador_meeting(week_start, shift, group_key, recorded_by)
    conn = get_db()
    conn.execute(
        """INSERT INTO ambassador_meeting_attendance
               (meeting_id, ambassador_id, login, full_name, department, attended, attendance_status, notes, recorded_by, recorded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(meeting_id, ambassador_id) DO UPDATE SET
               attended=excluded.attended, attendance_status=excluded.attendance_status, notes=excluded.notes,
               recorded_by=excluded.recorded_by, recorded_at=excluded.recorded_at""",
        (meeting_id, ambassador_id, ambassador["login"], ambassador["full_name"], ambassador["department"],
         1 if attendance_status == "present" else 0, attendance_status, notes, recorded_by, _now()),
    )
    conn.commit()
    conn.close()


def set_ambassador_meeting_notes(week_start, shift, group_key, notes, marked_by):
    meeting_id = get_or_create_ambassador_meeting(week_start, shift, group_key, marked_by)
    conn = get_db()
    conn.execute("UPDATE ambassador_meetings SET notes=?, marked_by=?, marked_at=? WHERE id=?",
                 (notes, marked_by, _now(), meeting_id))
    conn.commit()
    conn.close()


def set_ambassador_meeting_status(week_start, shift, group_key, status, reason, marked_by):
    """status is 'held' (reopen/normal), 'cancelled' (reason required by
    the route layer), or 'biweekly_skip' (no meeting this week under a
    biweekly cadence — reason optional)."""
    meeting_id = get_or_create_ambassador_meeting(week_start, shift, group_key, marked_by)
    conn = get_db()
    conn.execute(
        "UPDATE ambassador_meetings SET status=?, cancel_reason=?, marked_by=?, marked_at=? WHERE id=?",
        (status, reason if status == "cancelled" else (reason or None), marked_by, _now(), meeting_id),
    )
    conn.commit()
    conn.close()


def add_ambassador_meeting_document(week_start, shift, group_key, stored_filename, original_name, content_type, uploaded_by):
    meeting_id = get_or_create_ambassador_meeting(week_start, shift, group_key, uploaded_by)
    conn = get_db()
    conn.execute(
        """INSERT INTO ambassador_meeting_documents
               (meeting_id, stored_filename, original_name, content_type, uploaded_by, uploaded_at)
           VALUES (?,?,?,?,?,?)""",
        (meeting_id, stored_filename, original_name, content_type, uploaded_by, _now()),
    )
    conn.commit()
    conn.close()


def get_ambassador_meeting_document(doc_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM ambassador_meeting_documents WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_ambassador_meeting_document(doc_id):
    doc = get_ambassador_meeting_document(doc_id)
    if not doc:
        return None
    conn = get_db()
    conn.execute("DELETE FROM ambassador_meeting_documents WHERE id=?", (doc_id,))
    conn.commit()
    conn.close()
    return doc


def get_ambassador_attendance_report(shift=None, limit=60):
    """Every meeting record that's had any activity (attendance, notes,
    cancellation, or a biweekly mark) — most recent first — with an
    attendance tally per status. This intentionally only lists meetings
    that actually have a row; a week/group nobody has touched yet simply
    doesn't appear rather than being synthesized as a blank entry."""
    conn = get_db()
    q = "SELECT * FROM ambassador_meetings"
    params = []
    if shift:
        q += " WHERE shift=?"
        params.append(shift)
    q += " ORDER BY week_start DESC, shift, group_key LIMIT ?"
    params.append(limit)
    meetings = [dict(r) for r in conn.execute(q, params).fetchall()]
    for m in meetings:
        label, depts = AMBASSADOR_MEETING_GROUP_MAP.get(m["group_key"], (m["group_key"], []))
        m["group_label"] = label
        att_rows = conn.execute(
            "SELECT attendance_status, COUNT(*) as n FROM ambassador_meeting_attendance WHERE meeting_id=? GROUP BY attendance_status",
            (m["id"],),
        ).fetchall()
        counts = {key: 0 for key, _ in AMBASSADOR_ATTENDANCE_STATUSES}
        for r in att_rows:
            if r["attendance_status"] in counts:
                counts[r["attendance_status"]] = r["n"]
        m["attendance_counts"] = counts
        m["attendance_total"] = sum(counts.values())
        doc_count = conn.execute(
            "SELECT COUNT(*) as n FROM ambassador_meeting_documents WHERE meeting_id=?", (m["id"],)
        ).fetchone()["n"]
        m["document_count"] = doc_count
    conn.close()
    return meetings


def get_ambassador_hours_report():
    """For every department that has a tracked process list, per shift:
    the active ambassador roster and how many of them have at least one
    process under the flag threshold — the shape the Ambassador Hours
    reporting tab needs to highlight problem department/shift
    combinations at a glance."""
    report = []
    for dept, processes in get_ambassador_processes_by_department().items():
        for shift_key, shift_label in SHIFTS.items():
            ambassadors = get_ambassadors(shift_key, department=dept)[dept]
            flagged = [a for a in ambassadors if any(p["flagged"] for p in a["process_hours"])]
            report.append({
                "department": dept, "shift": shift_key, "shift_label": shift_label,
                "processes": processes, "total": len(ambassadors),
                "flagged_ambassadors": flagged, "flagged_count": len(flagged),
            })
    return report


def get_ambassador_hours_overview():
    """Ambassador Hours matrix, arranged by department and shift.

    Every active Process Ambassador is assigned exactly one status from their
    lowest tracked process: On Target (20h+), At Risk (10–19.9h), or Critical
    (under 10h, including zero). The same three-state model is used by every
    roll-up so totals and individual rows cannot disagree.
    """
    severity_rank = {"critical": 2, "at_risk": 1, "on_target": 0}
    severity_labels = {
        "critical": "Critical",
        "at_risk": "At Risk",
        "on_target": "On Target",
    }

    def risk_for(hours, required):
        if hours < 10:
            return "critical"
        if hours < required:
            return "at_risk"
        return "on_target"

    departments = []
    all_ambassadors = []
    monitored_count = 0
    status_counts = {"on_target": 0, "at_risk": 0, "critical": 0}
    targets_by_department = get_ambassador_targets()
    overall_credited_hours = 0.0
    overall_required_hours = 0.0
    dept_processes = get_ambassador_processes_by_department()

    for department in AMBASSADOR_DEPARTMENTS:
        department_data = {
            "department": department,
            "shifts": {},
            "total": 0,
            "target": 0,
            "flagged_count": 0,
            "zero_count": 0,
            "severity": "on_target",
            "status_counts": {"on_target": 0, "at_risk": 0, "critical": 0},
            "credited_hours": 0.0,
            "required_hours": 0.0,
            "no_processes_defined": normalize_department(department) not in dept_processes,
        }

        for shift_key, shift_label in SHIFTS.items():
            target = int(targets_by_department.get(department, {}).get(shift_key) or 0)
            ambassadors = get_ambassadors(
                shift_key,
                department=department,
                ambassador_type="process",
            )[department]
            shift_ambassadors = []
            shift_severity = "on_target"
            shift_zero_count = 0
            shift_status_counts = {"on_target": 0, "at_risk": 0, "critical": 0}
            shift_credited_hours = 0.0
            shift_required_hours = 0.0

            for ambassador in ambassadors:
                monitored_count += 1
                process_rows = []
                for process in ambassador.get("process_hours") or []:
                    hours = float(process.get("hours_60") or 0)
                    required = float(process.get("required_hours") or AMBASSADOR_HOURS_FLAG_THRESHOLD)
                    gap = max(0.0, required - hours)
                    credited_hours = min(max(0.0, hours), required)
                    severity = risk_for(hours, required)
                    process_rows.append({
                        "process": process.get("process"),
                        "hours": round(hours, 2),
                        "required_hours": round(required, 2),
                        "gap": round(gap, 2),
                        "credited_hours": round(credited_hours, 2),
                        "progress_pct": round(min(100, (hours / required * 100) if required else 100), 1),
                        "severity": severity,
                        "severity_label": severity_labels[severity],
                    })
                    shift_credited_hours += credited_hours
                    shift_required_hours += required

                if not process_rows:
                    continue

                process_rows.sort(key=lambda p: (-p["gap"], p["hours"], p["process"] or ""))
                worst = process_rows[0]
                low_processes = [p for p in process_rows if p["severity"] != "on_target"]
                has_zero = any(p["hours"] <= 0 for p in process_rows)
                shift_zero_count += 1 if has_zero else 0
                shift_status_counts[worst["severity"]] += 1
                status_counts[worst["severity"]] += 1
                shift_severity = max(
                    (shift_severity, worst["severity"]),
                    key=lambda level: severity_rank[level],
                )
                row = {
                    "id": ambassador.get("id"),
                    "login": ambassador.get("login") or "",
                    "full_name": ambassador.get("full_name") or ambassador.get("login") or "Unknown",
                    "department": department,
                    "shift": shift_key,
                    "shift_label": shift_label,
                    "processes": process_rows,
                    "low_processes": low_processes,
                    "worst_process": worst,
                    "max_gap": worst["gap"],
                    "min_hours": worst["hours"],
                    "severity": worst["severity"],
                    "severity_label": worst["severity_label"],
                    "has_zero": has_zero,
                }
                shift_ambassadors.append(row)
                all_ambassadors.append(row)

            shift_ambassadors.sort(
                key=lambda a: (-severity_rank[a["severity"]], -a["max_gap"], a["full_name"].lower(), a["login"].lower())
            )
            total = len(shift_ambassadors)
            flagged_count = shift_status_counts["critical"] + shift_status_counts["at_risk"]
            practice_health_pct = round(100 * shift_credited_hours / shift_required_hours) if shift_required_hours else None
            shift_data = {
                "shift": shift_key,
                "shift_label": shift_label,
                "total": total,
                "target": target,
                "flagged_count": flagged_count,
                "flag_rate": round((flagged_count / total * 100) if total else 0),
                "zero_count": shift_zero_count,
                "severity": shift_severity,
                "severity_label": severity_labels[shift_severity],
                "status_counts": shift_status_counts,
                "credited_hours": round(shift_credited_hours, 1),
                "required_hours": round(shift_required_hours, 1),
                "practice_health_pct": practice_health_pct,
                "practice_health_met": practice_health_pct is not None and practice_health_pct >= AMBASSADOR_PRACTICE_HEALTH_TARGET,
                "ambassadors": shift_ambassadors,
                "flagged_ambassadors": [a for a in shift_ambassadors if a["severity"] != "on_target"],
            }
            department_data["shifts"][shift_key] = shift_data
            department_data["total"] += total
            department_data["target"] += target
            department_data["credited_hours"] += shift_credited_hours
            department_data["required_hours"] += shift_required_hours
            overall_credited_hours += shift_credited_hours
            overall_required_hours += shift_required_hours
            department_data["flagged_count"] += flagged_count
            department_data["zero_count"] += shift_zero_count
            department_data["severity"] = max(
                (department_data["severity"], shift_severity),
                key=lambda level: severity_rank[level],
            )
            for status, count in shift_status_counts.items():
                department_data["status_counts"][status] += count

        department_data["flag_rate"] = round(
            (department_data["flagged_count"] / department_data["total"] * 100)
            if department_data["total"] else 0
        )
        department_data["severity_label"] = severity_labels[department_data["severity"]]
        department_data["credited_hours"] = round(department_data["credited_hours"], 1)
        department_data["required_hours"] = round(department_data["required_hours"], 1)
        department_data["practice_health_pct"] = (
            round(100 * department_data["credited_hours"] / department_data["required_hours"])
            if department_data["required_hours"] else None
        )
        department_data["practice_health_met"] = (
            department_data["practice_health_pct"] is not None
            and department_data["practice_health_pct"] >= AMBASSADOR_PRACTICE_HEALTH_TARGET
        )
        departments.append(department_data)

    flagged_count = status_counts["critical"] + status_counts["at_risk"]
    zero_count = sum(1 for ambassador in all_ambassadors if ambassador["has_zero"])
    overall_practice_health_pct = (
        round(100 * overall_credited_hours / overall_required_hours)
        if overall_required_hours else None
    )

    candidates = [
        shift
        for department in departments
        for shift in department["shifts"].values()
        if shift["flagged_count"]
    ]
    priority = None
    if candidates:
        priority_shift = max(
            candidates,
            key=lambda row: (
                row["status_counts"]["critical"], row["flagged_count"],
                row["flag_rate"], row["zero_count"], row["total"],
            ),
        )
        priority_department = next(
            department for department in departments
            if priority_shift["shift"] in department["shifts"]
            and department["shifts"][priority_shift["shift"]] is priority_shift
        )
        priority = {
            "department": priority_department["department"],
            "shift": priority_shift["shift"],
            "shift_label": priority_shift["shift_label"],
            "flagged_count": priority_shift["flagged_count"],
            "total": priority_shift["total"],
            "flag_rate": priority_shift["flag_rate"],
            "zero_count": priority_shift["zero_count"],
            "critical_count": priority_shift["status_counts"]["critical"],
            "at_risk_count": priority_shift["status_counts"]["at_risk"],
        }

    return {
        "target_hours": AMBASSADOR_HOURS_FLAG_THRESHOLD,
        "departments": departments,
        "department_names": [department["department"] for department in departments],
        "monitored_count": monitored_count,
        "flagged_count": flagged_count,
        "zero_count": zero_count,
        "critical_count": status_counts["critical"],
        "at_risk_count": status_counts["at_risk"],
        "on_target_count": status_counts["on_target"],
        "under_ten_count": status_counts["critical"],
        "meeting_standard_count": status_counts["on_target"],
        "status_counts": status_counts,
        "practice_health": {
            "pct": overall_practice_health_pct,
            "credited_hours": round(overall_credited_hours, 1),
            "required_hours": round(overall_required_hours, 1),
            "target_pct": AMBASSADOR_PRACTICE_HEALTH_TARGET,
            "meets_target": (
                overall_practice_health_pct is not None
                and overall_practice_health_pct >= AMBASSADOR_PRACTICE_HEALTH_TARGET
            ),
        },
        "priority": priority,
    }


def get_ambassador_hours_summary_cards():
    """The 3-card rollup for the Ambassador Hours reporting tab — each
    card combines every department in its group across all three
    shifts, rather than one small card per department+shift the way
    get_ambassador_hours_report does."""
    cards = []
    dept_processes = get_ambassador_processes_by_department()
    for key, label, depts in AMBASSADOR_REPORT_CARD_GROUPS:
        total = 0
        flagged_ambassadors = []
        for dept in depts:
            if dept not in dept_processes:
                continue
            for shift_key, shift_label in SHIFTS.items():
                ambassadors = get_ambassadors(shift_key, department=dept)[dept]
                total += len(ambassadors)
                for a in ambassadors:
                    if any(p["flagged"] for p in a["process_hours"]):
                        a2 = dict(a)
                        a2["department"] = dept
                        a2["shift_label"] = shift_label
                        flagged_ambassadors.append(a2)
        cards.append({
            "key": key, "label": label, "departments": depts,
            "total": total, "flagged_count": len(flagged_ambassadors),
            "flagged_ambassadors": flagged_ambassadors,
        })
    return cards


def get_ambassador_attendance_summary_cards():
    """The 3-card rollup for the Ambassador Attendance reporting tab —
    every meeting across every shift, pooled per card group: how many
    were actually held vs cancelled/skipped, and a pooled attendance
    rate (present / everyone recorded)."""
    group_key_to_card = {}
    for card_key, _card_label, card_depts in AMBASSADOR_REPORT_CARD_GROUPS:
        for mg_key, _mg_label, mg_depts in AMBASSADOR_MEETING_GROUPS:
            if set(mg_depts) & set(card_depts):
                group_key_to_card[mg_key] = card_key

    conn = get_db()
    meetings = [dict(r) for r in conn.execute("SELECT * FROM ambassador_meetings").fetchall()]
    cards = {
        key: {"key": key, "label": label, "departments": depts, "meetings_held": 0,
              "cancelled": 0, "biweekly_skip": 0, "present": 0, "absent": 0,
              "onsite_no_attend": 0, "excused": 0}
        for key, label, depts in AMBASSADOR_REPORT_CARD_GROUPS
    }
    for m in meetings:
        card_key = group_key_to_card.get(m["group_key"])
        if not card_key:
            continue
        c = cards[card_key]
        if m["status"] == "cancelled":
            c["cancelled"] += 1
            continue
        if m["status"] == "biweekly_skip":
            c["biweekly_skip"] += 1
            continue
        c["meetings_held"] += 1
        att_rows = conn.execute(
            "SELECT attendance_status, COUNT(*) as n FROM ambassador_meeting_attendance WHERE meeting_id=? GROUP BY attendance_status",
            (m["id"],),
        ).fetchall()
        for r in att_rows:
            if r["attendance_status"] in c:
                c[r["attendance_status"]] += r["n"]
    conn.close()
    out = []
    for key, label, depts in AMBASSADOR_REPORT_CARD_GROUPS:
        c = cards[key]
        total_recorded = c["present"] + c["absent"] + c["onsite_no_attend"] + c["excused"]
        c["attendance_pct"] = round(100 * c["present"] / total_recorded) if total_recorded else None
        out.append(c)
    return out


def get_ambassador_meeting_shift_grid():
    """Latest attendance in a department-row, shift-column matrix.

    Pack's shared meeting remains one meeting record, but its ambassadors are
    separated into Pack Multis, Pack Singles, and AFE rows. Every active roster
    member is included; an ambassador without a mark on the latest held meeting
    is explicitly labelled Not recorded.
    """
    conn = get_db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM ambassador_meetings ORDER BY week_start DESC"
    ).fetchall()]
    latest_by_shift_group = {}
    for m in rows:
        key = (m["shift"], m["group_key"])
        if key not in latest_by_shift_group:
            latest_by_shift_group[key] = m
    status_labels = dict(AMBASSADOR_ATTENDANCE_LABELS)
    status_labels["not_recorded"] = "Not recorded"
    summary = {key: 0 for key in status_labels}
    departments = []

    for group_key, group_label, group_departments in AMBASSADOR_MEETING_GROUPS:
        for department in group_departments:
            department_data = {
                "department": department,
                "group_key": group_key,
                "group_label": group_label,
                "shifts": {},
            }
            aliases = list(department_group(department))
            placeholders = ",".join("?" * len(aliases))

            for shift_key, shift_label in SHIFTS.items():
                meeting = latest_by_shift_group.get((shift_key, group_key))
                roster_rows = [dict(r) for r in conn.execute(
                    f"SELECT id, login, full_name, department FROM ambassadors "
                    f"WHERE shift=? AND active=1 AND department IN ({placeholders}) "
                    "ORDER BY full_name, login",
                    [shift_key] + aliases,
                ).fetchall()]

                people = {
                    ("id", row["id"]): {
                        "id": row["id"],
                        "login": row.get("login") or "",
                        "full_name": row.get("full_name") or row.get("login") or "Unknown",
                        "attendance_status": "not_recorded",
                        "attendance_label": status_labels["not_recorded"],
                    }
                    for row in roster_rows
                }

                state = "no_record"
                week_start = None
                cancel_reason = None
                if meeting:
                    week_start = meeting["week_start"]
                    cancel_reason = meeting.get("cancel_reason")
                    if meeting["status"] == "cancelled":
                        state = "cancelled"
                    elif meeting["status"] == "biweekly_skip":
                        state = "biweekly_skip"
                    else:
                        attendance_rows = [dict(r) for r in conn.execute(
                            "SELECT ambassador_id, login, full_name, department, attendance_status "
                            "FROM ambassador_meeting_attendance WHERE meeting_id=?",
                            (meeting["id"],),
                        ).fetchall()]
                        for row in attendance_rows:
                            if normalize_department(row.get("department")) != normalize_department(department):
                                continue
                            status = row.get("attendance_status") or "not_recorded"
                            if status not in status_labels:
                                status = "not_recorded"
                            key = ("id", row.get("ambassador_id")) if row.get("ambassador_id") is not None else ("login", (row.get("login") or "").lower())
                            people[key] = {
                                "id": row.get("ambassador_id"),
                                "login": row.get("login") or "",
                                "full_name": row.get("full_name") or row.get("login") or "Unknown",
                                "attendance_status": status,
                                "attendance_label": status_labels[status],
                            }
                        state = "attended" if any(
                            person["attendance_status"] == "present" for person in people.values()
                        ) else "no_show"

                ambassadors = sorted(
                    people.values(),
                    key=lambda row: (row["full_name"].lower(), row["login"].lower()),
                )
                counts = {key: 0 for key in status_labels}
                for ambassador in ambassadors:
                    counts[ambassador["attendance_status"]] += 1
                    summary[ambassador["attendance_status"]] += 1

                department_data["shifts"][shift_key] = {
                    "shift": shift_key,
                    "shift_label": shift_label,
                    "state": state,
                    "week_start": week_start,
                    "cancel_reason": cancel_reason,
                    "ambassadors": ambassadors,
                    "counts": counts,
                    "total": len(ambassadors),
                }
            departments.append(department_data)
    conn.close()
    return {"departments": departments, "summary": summary, "status_labels": status_labels}


# Cross-Training Compliance scoring — proficiency_status ladder, worst to
# best: Lapsed (0 hours in the last 180 days, out of process too long,
# permission revoked) < Refresh (has at least 20 hours somewhere in the
# last 180 days, but not within 90 — coming due for renewal) < Practice
# (at least 20 hours within the last 90 days, actively current but not
# within 60) < Proficient (at least 20 hours within the last 60 days —
# fully qualified, the goal). This follows the actual upstream
# determination logic (a cascading hours-in-window check, checked most-
# recent-window first) rather than the two middle labels' plain-English
# reading, which suggests the opposite order. Each state earns points
# toward a 0-100 score; the score is the average points per record, so
# it reads like a percentage: 100 means every record is Proficient, 0
# means every record has Lapsed.
XT_PROFICIENCY_WEIGHTS = {"Proficient": 100, "Practice": 60, "Refresh": 30, "Lapsed": 0}


@request_memoize
def get_xt_proficiency_counts(am_login=None, fc=None):
    """Count of xt_hours records (one per employee+process, not per
    employee) by proficiency_status, scoped to an AM (or list of AMs via
    supervisor_login — the hours file's own reporting-line field) and/or
    FC.

    The supervisor match is on lower(supervisor_login), which no
    plain column index can serve; idx_xt_hours_supervisor_lower
    indexes that same expression, so the Cross-Training tile does a
    lookup instead of scanning the whole hours table once per
    manager.
    """
    conn = get_db()
    q = "SELECT proficiency_status, COUNT(*) as n FROM xt_hours WHERE proficiency_status IS NOT NULL"
    params = []
    if am_login:
        if isinstance(am_login, (list, tuple)):
            if not am_login:
                conn.close()
                return {}
            q += f" AND lower(supervisor_login) IN ({','.join('?' * len(am_login))})"
            params += [a.lower() for a in am_login]
        else:
            q += " AND lower(supervisor_login)=?"
            params.append(am_login.lower())
    if fc:
        q += " AND fc=?"
        params.append(fc)
    q += " GROUP BY proficiency_status"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return {r["proficiency_status"]: r["n"] for r in rows}


def get_xt_compliance_score(am_login=None, fc=None):
    """The single Cross-Training Compliance number for the AM Overview
    scorecard, plus the raw counts behind it (including Refresh, called
    out separately since it's the state worth watching — still compliant
    today, but coming due). Unrecognized status values (anything besides
    the four tracked here) count for half credit rather than being
    dropped, so a data hiccup doesn't silently vanish from the total."""
    counts = get_xt_proficiency_counts(am_login=am_login, fc=fc)
    total = sum(counts.values())
    if not total:
        return {"score": None, "total": 0, "proficient": 0, "refresh": 0, "practice": 0, "lapsed": 0, "other": 0}
    weighted = sum(XT_PROFICIENCY_WEIGHTS.get(status, 50) * n for status, n in counts.items())
    known = set(XT_PROFICIENCY_WEIGHTS)
    return {
        "score": round(weighted / total, 1),
        "total": total,
        "proficient": counts.get("Proficient", 0),
        "refresh": counts.get("Refresh", 0),
        "practice": counts.get("Practice", 0),
        "lapsed": counts.get("Lapsed", 0),
        "other": sum(n for status, n in counts.items() if status not in known),
    }


def get_xt_proficiency_records(am_login=None, fc=None):
    """Every xt_hours record in scope, for the Cross-Training Compliance
    drill-down — one row per employee+process, each with its proficiency
    state and a projected expiry date/days-until (see compute_xt_expiry)
    based on last_date_on_process, using the current
    xt_proficiency_expiry_days setting."""
    conn = get_db()
    q = """SELECT employee_login, full_name, fclm_mapped, merged_function, proficiency_status,
                  trained_status, shift, supervisor_login, fc, last_date_on_process
           FROM xt_hours WHERE proficiency_status IS NOT NULL"""
    params = []
    if am_login:
        if isinstance(am_login, (list, tuple)):
            if not am_login:
                conn.close()
                return []
            q += f" AND lower(supervisor_login) IN ({','.join('?' * len(am_login))})"
            params += [a.lower() for a in am_login]
        else:
            q += " AND lower(supervisor_login)=?"
            params.append(am_login.lower())
    if fc:
        q += " AND fc=?"
        params.append(fc)
    q += " ORDER BY full_name, merged_function"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    threshold_days = get_xt_proficiency_expiry_days()
    out = []
    for r in rows:
        rec = dict(r)
        expiry_date, days_until = compute_xt_expiry(rec.get("last_date_on_process"), rec.get("proficiency_status"), threshold_days)
        rec["expiry_date"] = expiry_date
        rec["days_until_expiry"] = days_until
        out.append(rec)
    return out


def get_am_pool():
    """Every login holding the Area Manager role specifically — used
    everywhere an AM needs to be picked for trainer assignment. Used to
    also fall back to raw supervisor logins seen in the compliance data
    when no role was assigned yet, but that let Operations Managers and
    Senior Operations Managers leak in whenever they happened to appear
    as someone's direct supervisor — role='am' is the correct, precise
    signal now that auto-detect can assign roles reliably from the same
    data."""
    return [r["login"] for r in get_roster_by_roles(["am"])]


def get_unassigned_ams():
    """Area Manager logins (role='am' specifically — not Team Leads, and
    not the raw supervisor logins seen in the compliance data, which used
    to leak in Operations Managers and Senior Operations Managers
    whenever they happened to appear as someone's direct supervisor)
    with no trainer covering them yet."""
    covered = get_all_assigned_ams()
    return [am for am in get_am_pool() if am not in covered]


def get_am_to_trainers_map():
    """am_login -> [trainer_logins currently responsible for them], for
    showing a 'Trainer' column on the AM & Team Lead roster."""
    conn = get_db()
    rows = conn.execute("SELECT am_login, trainer_login FROM trainer_am_assignments").fetchall()
    conn.close()
    mapping = {}
    for r in rows:
        mapping.setdefault(r["am_login"], []).append(r["trainer_login"])
    return mapping


# ------------------------------------------------------------- OLR ----
# Operational Leadership Review — a yearly performance review every Area
# Manager and Operations Manager goes through, built from weekly L&D
# metric snapshots (uploaded, not live-computed — the scorecard tiles
# elsewhere are always "right now"; OLR needs a 12-month history, which
# nothing else in the app stores over time) plus how many Phase 1/2/3
# escalations have concerned them.
OLR_METRIC_KEYS = [k for k, _, _ in SCORECARD_CATEGORIES]
OLR_METRIC_LABELS = {k: label for k, label, _ in SCORECARD_CATEGORIES}


def get_olr_reviewees():
    """Every Area Manager and Operations Manager — the population this
    review applies to."""
    return get_roster_by_roles(["am", "om"])


def ingest_olr_weekly_csv(file_bytes, filename, uploaded_by):
    """Wide-format weekly upload: one row per person per week, one
    column per L&D metric (the same keys as the My L&D Scorecard tiles).
    Header: Login, Week Start, then any of Safety Compliance, DE
    Technical Briefing, Indirect Roles, Cross-Training, Ambassador
    Availability, Ambassador Readiness, BTS Compliance — case/spacing
    insensitive, matched against OLR_METRIC_LABELS. Re-uploading the
    same person+week+metric replaces the prior value (upsert), so a
    corrected re-upload for a past week is safe."""
    label_to_key = {v.lower(): k for k, v in OLR_METRIC_LABELS.items()}
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return 0
    col_to_key = {}
    for col in reader.fieldnames:
        norm = (col or "").strip().lower()
        if norm in label_to_key:
            col_to_key[col] = label_to_key[norm]
        elif norm.replace(" ", "_") in OLR_METRIC_KEYS:
            col_to_key[col] = norm.replace(" ", "_")
    conn = get_db()
    cur = conn.cursor()
    now = _now()
    n = 0
    for row in reader:
        row = _ci_row(row)
        login = _clean(row.get("login"))
        week_raw = _clean(row.get("week start") or row.get("week"))
        if not login or not week_raw:
            continue
        week_start = week_start_for(week_raw)
        for col, key in col_to_key.items():
            val = _clean(row.get(col.strip().lower()))
            if val is None or val == "":
                continue
            try:
                val = float(str(val).replace("%", "").strip())
            except ValueError:
                continue
            cur.execute(
                """INSERT INTO olr_weekly_metrics (login, week_start, metric_key, value, uploaded_by, uploaded_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (login, week_start, metric_key) DO UPDATE SET value=excluded.value, uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at""",
                (login, week_start, key, val, uploaded_by, now),
            )
            n += 1
    conn.commit()
    conn.close()
    return n


@request_memoize
def get_olr_weekly_series(login, since=None):
    """Every weekly metric row for this person, since a given ISO date
    (defaults to 12 months back) — shaped {week_start: {metric_key: value}},
    weeks in ascending order."""
    if not since:
        since = (date.today() - timedelta(days=365)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT week_start, metric_key, value FROM olr_weekly_metrics WHERE login=? AND week_start>=? ORDER BY week_start",
        (login, since),
    ).fetchall()
    conn.close()
    series = {}
    for r in rows:
        series.setdefault(r["week_start"], {})[r["metric_key"]] = r["value"]
    return series


def _olr_composite(week_values):
    """A week's overall score — the mean of whichever metrics were
    actually uploaded for it, not assuming all 7 are always present."""
    vals = [v for v in week_values.values() if v is not None]
    return sum(vals) / len(vals) if vals else None


def get_site_weekly_composite_series(weeks=8):
    """The current site's Total L&D score, week by week, from the OLR
    weekly snapshots — the trend line behind each site card on Regional
    Overview.

    Built as the mean of each manager's own composite for that week,
    which is NOT the same quantity as the live site-wide rollup shown as
    the site's headline score (that one is computed across every tracked
    item at once, not averaged per manager). The two can differ, so the
    page labels this series for what it is rather than implying the last
    point should equal the headline. Weeks with no snapshot are absent
    rather than zero — a site that was not being snapshotted yet has no
    history, and drawing that as 0% would read as a catastrophic score.
    """
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    placeholders = ",".join("?" * len(OLR_METRIC_KEYS))
    conn = get_db()
    rows = conn.execute(
        f"SELECT week_start, login, metric_key, value FROM olr_weekly_metrics "
        f"WHERE week_start >= ? AND metric_key IN ({placeholders}) AND value IS NOT NULL "
        f"ORDER BY week_start",
        [cutoff] + list(OLR_METRIC_KEYS),
    ).fetchall()
    conn.close()

    by_week = {}
    for r in rows:
        by_week.setdefault(r["week_start"], {}).setdefault(r["login"], {})[r["metric_key"]] = r["value"]

    series = []
    for week_start in sorted(by_week):
        composites = [c for c in (_olr_composite(v) for v in by_week[week_start].values())
                      if c is not None]
        if composites:
            series.append((week_start, round(sum(composites) / len(composites), 1)))
    return series


def get_site_weekly_metric_series(weeks=8):
    """Per-metric weekly history for the current site — {metric_key:
    [(week_start, value)]} — the sparkline behind each metric card on
    Reporting.

    Same basis and same caveat as get_site_weekly_composite_series: each
    point is the mean across whichever managers were snapshotted that
    week, which is not the same quantity as the live site-wide rollup
    shown as the card's headline figure. Weeks with no snapshot for a
    metric are absent from that metric's series rather than plotted as
    zero.
    """
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    placeholders = ",".join("?" * len(OLR_METRIC_KEYS))
    conn = get_db()
    rows = conn.execute(
        f"SELECT week_start, metric_key, AVG(value) AS avg_value FROM olr_weekly_metrics "
        f"WHERE week_start >= ? AND metric_key IN ({placeholders}) AND value IS NOT NULL "
        f"GROUP BY week_start, metric_key ORDER BY week_start",
        [cutoff] + list(OLR_METRIC_KEYS),
    ).fetchall()
    conn.close()

    series = {key: [] for key in OLR_METRIC_KEYS}
    for r in rows:
        series[r["metric_key"]].append((r["week_start"], round(r["avg_value"], 1)))
    return series


def get_login_weekly_composite_series(login, weeks=8):
    """One manager's own Total L&D composite week by week, for the trend
    on their card in SOM/OM Overview. Reads the same snapshots as the
    OLR page, so a manager's trend here and on their OLR agree."""
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    weekly = get_olr_weekly_series(login, since=cutoff)
    out = []
    for week_start in sorted(weekly):
        composite = _olr_composite({k: v for k, v in weekly[week_start].items()
                                    if k in OLR_METRIC_KEYS})
        if composite is not None:
            out.append((week_start, round(composite, 1)))
    return out


def get_week_plan_utilisation(week_start):
    """Seats booked against seats offered on the current site's approved
    training plan for one week. Slots with no capacity set contribute
    their booked attendees but no seats, so utilisation can never be
    inflated by a slot nobody sized."""
    slots = get_training_slots(week_start)
    capacity = sum(s["capacity"] or 0 for s in slots)
    booked = sum(len(s.get("attendees") or []) for s in slots)
    return {
        "slots": len(slots),
        "capacity": capacity,
        "booked": booked,
        "seats_free": max(capacity - booked, 0),
        "pct": round(100 * booked / capacity) if capacity else None,
    }


def get_olr_averages(login):
    """WoW average (mean of every uploaded week in the last 12 months),
    month averages, quarter averages, and a simple development trend —
    most recent quarter's average vs the quarter before it. Each
    average is computed per metric, plus an overall composite (mean
    across whichever metrics are present that period)."""
    series = get_olr_weekly_series(login)
    weeks_sorted = sorted(series.keys())

    per_metric_values = {k: [] for k in OLR_METRIC_KEYS}
    composite_by_week = {}
    for wk in weeks_sorted:
        for k, v in series[wk].items():
            if v is not None and k in per_metric_values:
                per_metric_values[k].append(v)
        composite_by_week[wk] = _olr_composite(series[wk])

    wow_avg = {k: (sum(v) / len(v) if v else None) for k, v in per_metric_values.items()}
    composite_values = [v for v in composite_by_week.values() if v is not None]
    wow_composite_avg = sum(composite_values) / len(composite_values) if composite_values else None

    def period_key_month(wk):
        return wk[:7]  # YYYY-MM

    def period_key_quarter(wk):
        y, m = wk[:4], int(wk[5:7])
        q = (m - 1) // 3 + 1
        return f"{y}-Q{q}"

    def grouped_composite_avg(keyfunc):
        buckets = {}
        for wk, comp in composite_by_week.items():
            if comp is None:
                continue
            buckets.setdefault(keyfunc(wk), []).append(comp)
        return {k: sum(v) / len(v) for k, v in buckets.items()}

    month_avgs = grouped_composite_avg(period_key_month)
    quarter_avgs = grouped_composite_avg(period_key_quarter)

    quarters_sorted = sorted(quarter_avgs.keys())
    development = None
    if len(quarters_sorted) >= 2:
        latest, prior = quarter_avgs[quarters_sorted[-1]], quarter_avgs[quarters_sorted[-2]]
        diff = latest - prior
        if abs(diff) < 1:
            development = {"direction": "flat", "diff": round(diff, 1)}
        elif diff > 0:
            development = {"direction": "up", "diff": round(diff, 1)}
        else:
            development = {"direction": "down", "diff": round(diff, 1)}

    return {
        "weeks_recorded": len(weeks_sorted),
        "wow_avg_by_metric": wow_avg,
        "wow_composite_avg": round(wow_composite_avg, 1) if wow_composite_avg is not None else None,
        "month_avgs": {k: round(v, 1) for k, v in month_avgs.items()},
        "quarter_avgs": {k: round(v, 1) for k, v in quarter_avgs.items()},
        "latest_quarter": quarters_sorted[-1] if quarters_sorted else None,
        "latest_quarter_avg": round(quarter_avgs[quarters_sorted[-1]], 1) if quarters_sorted else None,
        "development": development,
    }


def get_olr_escalation_counts(login, since=None):
    """Phase 1/2/3 escalation counts — record escalations and Log
    Escalation tickets both — CONCERNING this person (am_login /
    concerning_manager), created/escalated in the last 12 months by
    default. Deliberately concerning-only, not assigned-to — OLR is
    reviewing THEIR performance, so what's assigned to them to help
    someone else isn't relevant here, only what's been raised about
    their own team."""
    if not since:
        since = (date.today() - timedelta(days=365)).isoformat()
    conn = get_db()
    record_rows = conn.execute(
        """SELECT re.phase, COUNT(*) as n FROM record_escalations re
           JOIN tracked_items ti ON ti.id = re.tracked_item_id
           WHERE ti.am_login=? AND re.escalated_at>=? GROUP BY re.phase""",
        (login, since),
    ).fetchall()
    ticket_rows = conn.execute(
        "SELECT phase, COUNT(*) as n FROM escalation_tickets WHERE concerning_manager=? AND created_at>=? GROUP BY phase",
        (login, since),
    ).fetchall()
    conn.close()
    counts = {1: 0, 2: 0, 3: 0}
    for r in record_rows:
        counts[r["phase"]] = counts.get(r["phase"], 0) + r["n"]
    for r in ticket_rows:
        counts[r["phase"]] = counts.get(r["phase"], 0) + r["n"]
    return counts


def get_roster_by_roles(roles):
    """user_roles rows whose role is in the given list, for grouping the
    User Management roster by category (Admins/Trainers, SOM/OM, AM/Team
    Lead, etc.)."""
    if not roles:
        return []
    conn = get_db()
    rows = conn.execute(
        f"SELECT * FROM user_roles WHERE role IN ({','.join('?' * len(roles))}) ORDER BY login",
        roles,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@request_memoize
def get_ams_for_trainer(trainer_login):
    conn = get_db()
    rows = conn.execute(
        "SELECT am_login FROM trainer_am_assignments WHERE trainer_login=? ORDER BY am_login",
        (trainer_login,),
    ).fetchall()
    conn.close()
    return [r["am_login"] for r in rows]


def assign_am_to_trainer(trainer_login, am_login):
    conn = get_db()
    conn.execute(
        "INSERT INTO trainer_am_assignments (trainer_login, am_login, assigned_at) VALUES (?,?,?) ON CONFLICT (trainer_login, am_login) DO NOTHING",
        (trainer_login, am_login, _now()),
    )
    conn.commit()
    conn.close()


def assign_ams_to_trainer(trainer_login, am_logins):
    """Bulk version — assign several AMs to a trainer in one go."""
    for am_login in am_logins:
        assign_am_to_trainer(trainer_login, am_login)


def reassign_am(am_login, from_trainer, to_trainer):
    """Move a single AM from one trainer to another in one action, instead
    of unassign-then-reassign as two separate steps."""
    if from_trainer == to_trainer:
        return
    conn = get_db()
    conn.execute(
        "DELETE FROM trainer_am_assignments WHERE trainer_login=? AND am_login=?",
        (from_trainer, am_login),
    )
    conn.execute(
        "INSERT INTO trainer_am_assignments (trainer_login, am_login, assigned_at) VALUES (?,?,?) ON CONFLICT (trainer_login, am_login) DO NOTHING",
        (to_trainer, am_login, _now()),
    )
    conn.commit()
    conn.close()


def unassign_am_from_trainer(trainer_login, am_login):
    conn = get_db()
    conn.execute(
        "DELETE FROM trainer_am_assignments WHERE trainer_login=? AND am_login=?",
        (trainer_login, am_login),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ roles --
# Role is assigned per-login by an admin-tier user (or requested by the
# user and approved). Unassigned logins get None ("pending").

@request_memoize
def get_user_role(login):
    if not login:
        return None
    conn = get_db()
    row = conn.execute("SELECT role FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    return row["role"] if row else None


@request_memoize
def get_user_shift_and_department(login):
    if not login:
        return None, None
    conn = get_db()
    row = conn.execute("SELECT shift, department FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    if not row:
        return None, None
    return row["shift"], row["department"]


def set_user_role(login, role, assigned_by, shift=None, department=None, full_name=None, title=None):
    # Shift only means anything for the shift-bound roles; storing it for
    # an exempt role (Learning Manager, Senior Operations Manager, Admin)
    # would just be misleading data, so it's dropped.
    if role not in SHIFT_BOUND_ROLES:
        shift = None
    conn = get_db()
    conn.execute(
        "INSERT INTO user_roles (login, role, shift, department, full_name, title, assigned_by, assigned_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(login) DO UPDATE SET role=excluded.role, shift=excluded.shift, "
        "department=COALESCE(excluded.department, user_roles.department), "
        "full_name=COALESCE(excluded.full_name, user_roles.full_name), "
        "title=COALESCE(excluded.title, user_roles.title), "
        "assigned_by=excluded.assigned_by, assigned_at=excluded.assigned_at",
        (login, role, shift, department, full_name, title, assigned_by, _now()),
    )
    conn.commit()
    conn.close()
    # An approved request for this login is now resolved — clear any others
    # left pending so they don't linger in the queue.
    conn = get_db()
    conn.execute(
        "UPDATE role_requests SET status='approved', decided_by=?, decided_at=? WHERE login=? AND status='pending'",
        (assigned_by, _now(), login),
    )
    conn.commit()
    conn.close()
    # Being given the Trainer role is the ONLY thing that makes someone
    # eligible to have AMs assigned to them — no separate "add trainer"
    # step. This just satisfies the trainer_am_assignments FK invisibly;
    # get_trainers() below only ever surfaces logins that currently hold
    # the role, so a stale trainers row from a past role never resurfaces.
    if role == "trainer":
        add_trainer(login, full_name=None)


def set_reports_to(login, manager_login):
    """Who this person's manager is, for walking the org tree (used to
    find a manager's indirect reports too, not just their direct AMs).
    A Team Lead's department is kept in sync with their Area Manager's
    whenever this relationship is set or changes — see
    sync_team_lead_department below."""
    conn = get_db()
    conn.execute("UPDATE user_roles SET reports_to=? WHERE login=?", (manager_login or None, login))
    conn.commit()
    conn.close()
    sync_team_lead_department(login)


def sync_team_lead_department(login):
    """A Team Lead's department should always match their Area Manager's
    — actively kept in sync (not just filled once if blank) whenever
    their reports-to relationship is set or changes, whether that's from
    auto-detect or a manual edit. No-op for anyone who isn't a Team
    Lead, has no manager set, or whose manager isn't an AM with a
    department of their own yet."""
    conn = get_db()
    row = conn.execute("SELECT role, reports_to FROM user_roles WHERE login=?", (login,)).fetchone()
    if not row or row["role"] != "team_lead" or not row["reports_to"]:
        conn.close()
        return
    manager = conn.execute("SELECT role, department FROM user_roles WHERE login=?", (row["reports_to"],)).fetchone()
    conn.close()
    if manager and manager["role"] == "am" and manager["department"]:
        conn = get_db()
        conn.execute("UPDATE user_roles SET department=? WHERE login=?", (manager["department"], login))
        conn.commit()
        conn.close()


def set_department(login, department):
    """Direct, unconditional set (can clear it back to blank) — unlike
    set_user_role's department handling, which COALESCEs and so can
    never be used to clear an existing value. Editable from User
    Management regardless of how it first got filled in (auto-detect's
    roster lookup, or by hand here)."""
    conn = get_db()
    conn.execute("UPDATE user_roles SET department=? WHERE login=?", (department or None, login))
    conn.commit()
    conn.close()


def set_title(login, title):
    """Free-text job title, separate from the fixed role dropdown — this
    is what the Phase 3 escalation auto-lookup fuzzy-matches against
    ('Sr Ops Manager', 'Snr Operations', 'Interim Senior Operations
    Manager', 'General Manager', 'Site Lead', etc.)."""
    conn = get_db()
    conn.execute("UPDATE user_roles SET title=? WHERE login=?", (title or None, login))
    conn.commit()
    conn.close()


def mark_login_activated(login):
    """First-ever login marker — 'activated' means they've actually
    signed into the tool at least once, as distinct from just having a
    role assigned by an admin. Only writes the first time (WHERE
    activated_at IS NULL), so this is cheap to call on every request."""
    conn = get_db()
    conn.execute(
        "UPDATE user_roles SET activated_at=? WHERE login=? AND activated_at IS NULL",
        (_now(), login),
    )
    conn.commit()
    conn.close()


def is_login_activated(login):
    conn = get_db()
    row = conn.execute("SELECT activated_at FROM user_roles WHERE login=?", (login,)).fetchone()
    conn.close()
    return bool(row and row["activated_at"])



def suggest_am_profile(login):
    """Best-guess profile for an AM being assigned a role, sourced from
    data L&D already has rather than typed in by hand:
    - full_name: the AM's own row in Safety Training Compliance, if they
      have one (AMs are Amazon employees too, so they often show up in
      that export under their own login same as any associate).
    - shift: the most common shift among this AM's direct reports
      (tracked_items.am_login = this login), across every section.
    - department: the most common home department (fclm_mapped) among
      those same reports, looked up in the cross-training hours file.
    Any piece with no data to infer from comes back None — the caller
    decides whether to leave the field blank or keep what's already
    there."""
    login = (login or "").strip()
    if not login:
        return {"full_name": None, "shift": None, "department": None}
    conn = get_db()
    name_row = conn.execute(
        "SELECT full_name FROM tracked_items WHERE section='compliance_safety' AND lower(employee_login)=? "
        "AND full_name IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
        (login.lower(),),
    ).fetchone()
    full_name = name_row["full_name"] if name_row else None

    shift_row = conn.execute(
        "SELECT shift, COUNT(*) as n FROM tracked_items WHERE lower(am_login)=? AND shift IS NOT NULL "
        "GROUP BY shift ORDER BY n DESC LIMIT 1",
        (login.lower(),),
    ).fetchone()
    shift = shift_row["shift"] if shift_row else None

    report_logins = [r["employee_login"] for r in conn.execute(
        "SELECT DISTINCT employee_login FROM tracked_items WHERE lower(am_login)=? AND employee_login IS NOT NULL",
        (login.lower(),),
    ).fetchall()]
    department = None
    if report_logins:
        placeholders = ",".join("?" * len(report_logins))
        dept_row = conn.execute(
            f"SELECT fclm_mapped, COUNT(DISTINCT employee_login) as n FROM xt_hours "
            f"WHERE employee_login IN ({placeholders}) AND fclm_mapped IS NOT NULL "
            f"GROUP BY fclm_mapped ORDER BY n DESC LIMIT 1",
            report_logins,
        ).fetchone()
        department = dept_row["fclm_mapped"] if dept_row else None
    conn.close()
    return {"full_name": full_name, "shift": shift, "department": department}


@request_memoize
def get_descendant_ams(login):
    """Every login (direct or indirect) that reports up to this manager
    via the reports_to chain — {login: is_direct}. Empty dict for a leaf
    manager with nobody reporting to them."""
    conn = get_db()
    result = {}
    frontier = [login]
    seen = {login}
    while frontier:
        placeholders = ",".join("?" * len(frontier))
        rows = conn.execute(
            f"SELECT login, reports_to FROM user_roles WHERE reports_to IN ({placeholders})", frontier
        ).fetchall()
        next_frontier = []
        for r in rows:
            l = r["login"]
            if l in seen:
                continue
            seen.add(l)
            result[l] = (r["reports_to"] == login)
            next_frontier.append(l)
        frontier = next_frontier
    conn.close()
    return result


def get_am_breakdown_for_scope(sections, am_logins, fc=None):
    """For each manager login in scope, their own compliance gap count —
    surfaces which one is actually dragging a rolled-up number down."""
    out = []
    for am in am_logins:
        items = get_items(section=sections, am_login=am, fc=fc)
        if not items:
            continue
        gap_count = sum(1 for it in items if status_bucket(it["status"]) in ("risk", "gap"))
        out.append({
            "am_login": am, "total": len(items), "gap_count": gap_count,
            "pct_ok": round(100 * (len(items) - gap_count) / len(items)) if items else None,
        })
    out.sort(key=lambda r: -r["gap_count"])
    return out


def get_all_user_roles():
    conn = get_db()
    rows = conn.execute("SELECT * FROM user_roles ORDER BY login").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_user_role(login):
    """Removes a login's assigned role entirely — they go back to pending
    (no role) rather than being deleted as a user; if a fresh role request
    comes in later that's handled the normal way."""
    conn = get_db()
    conn.execute("DELETE FROM user_roles WHERE login=?", (login,))
    conn.commit()
    conn.close()


def request_role(login, requested_role):
    conn = get_db()
    conn.execute(
        "INSERT INTO role_requests (login, requested_role, status, requested_at) VALUES (?,?, 'pending', ?)",
        (login, requested_role, _now()),
    )
    conn.commit()
    conn.close()


def get_pending_role_requests():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM role_requests WHERE status='pending' ORDER BY requested_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def decide_role_request(request_id, approve, decided_by, role_if_approved=None):
    conn = get_db()
    row = conn.execute("SELECT * FROM role_requests WHERE id=?", (request_id,)).fetchone()
    conn.close()
    if not row:
        return
    if approve:
        set_user_role(row["login"], role_if_approved or row["requested_role"], decided_by)
    else:
        conn = get_db()
        conn.execute(
            "UPDATE role_requests SET status='denied', decided_by=?, decided_at=? WHERE id=?",
            (decided_by, _now(), request_id),
        )
        conn.commit()
        conn.close()
