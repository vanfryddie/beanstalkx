# LDIS — Learning & Development Intelligence Systems
### Complete Technical Reference: Purpose, Architecture, Logic, and Features

---

## 1. Purpose and Scope

LDIS is a web application that unifies Learning & Development compliance tracking for Amazon's EU12 fulfillment center network. It was built to solve a specific, recurring problem: L&D data arrives as disconnected CSV exports from separate systems (Safety Compliance, DE Technical Briefing, BTS, Cross-Training hours, Indirect Roles rosters), each on its own upload cadence, with no shared view and no org-aware rollup. A manager two or three levels up an org chart had no accurate way to see their whole team's numbers without manually assembling them from raw exports.

LDIS ingests those same raw exports, computes every compliance metric live from whatever is currently loaded, and presents it through views scoped correctly to whoever is looking — an Area Manager sees their own team; an Operations Manager sees their entire reporting tree, walked automatically; a Trainer sees every Area Manager explicitly assigned to them; a Regional/Global Admin sees every site in the network at once.

The application has grown, across an extended build process, from a single-site compliance dashboard into a multi-site operations platform with its own scheduling engine, org-hierarchy resolution, and cross-site aggregation layer. As of this writing it spans roughly 11,400 lines of Python across two files (`application.py` — routing and business logic; `db.py` — all data access, ingestion, and computation) and 108 distinct routes.

---

## 2. Architecture

| Layer | Technology |
|---|---|
| Application framework | Python / Flask |
| Database | PostgreSQL (Amazon RDS) in production; automatic SQLite fallback for local development, keyed off whether `DATABASE_URL`/`RDS_HOSTNAME` env vars are set |
| Hosting | AWS Elastic Beanstalk |
| Authentication | Federate/Midway OIDC via an Application Load Balancer, resolving role from an LDAP Group Mapper claim; a manual login form exists for local/dev use when no ALB headers are present |
| Object storage | Amazon S3 for uploaded meeting documents (falls back to local disk if `S3_BUCKET` isn't configured) |
| Frontend | Server-rendered Jinja templates, vanilla JavaScript, no build step, no frontend framework |
| Exports | Native canvas-rendered PNG for "copy as image" features (deliberately built without any external rendering library) |

### 2.1 Multi-site architecture

LDIS is not a single-tenant app scoped to one fulfillment center. Each registered "site" (fulfillment center) gets its own fully isolated data: on Postgres, via a dedicated schema per site with `search_path` switched per request; on SQLite, via a dedicated database file per site. A small, separate "global" database/schema (`sites`, `global_admins` tables) tracks the registry of sites and who has cross-site ("regional") access — this is never routed through the per-site connection, since it must be reachable regardless of which site is currently selected.

`db.set_current_site(code)` / `db.get_current_site()` manage a thread-local site context for the duration of a request. `db.get_db()` returns a connection scoped to whatever site is currently set; `db.get_global_db()` always returns the site-independent registry connection. A site switcher in the top navigation bar lets a user with access change which site's data they're viewing. The **Regional Overview** page (Section 4.11) is the only feature that deliberately breaks out of one-site-at-a-time: it temporarily switches the site context in a loop, computing each site's numbers with the exact same site-scoped functions every other page uses, then restores the original site context in a `finally` block — even if a specific site's computation raises, so a mid-loop failure can never leave the rest of that request pointed at the wrong site's data.

### 2.2 Postgres/SQLite compatibility layer

Because the app is written once against SQLite's `conn.execute()` API but must run against Postgres in production, `db.py` includes `PGConnection`/`PGCursor` wrapper classes that translate `?` placeholders to `%s`, auto-append `RETURNING id` to inserts against tables with an autoincrement primary key (since Postgres has no `cursor.lastrowid`), and force autocommit mode so one bad statement can't abort a whole transaction and silently block everything queued after it in the same request.

A real production incident traced to this layer is worth recording: `get_db()`/`get_global_db()` were setting a connection's `search_path` via a query *before* putting that connection into autocommit mode. A freshly-pooled psycopg2 connection isn't in autocommit mode by default, so that query silently opened a transaction — and psycopg2 refuses to change the autocommit setting while a transaction is open, throwing `set_session cannot be used inside a transaction`. Because the failure happened before the connection was cleanly returned to the pool, every failed request leaked a connection, and because site-registry initialization only marked itself "done" after succeeding, every subsequent request retried and leaked again — exhausting the entire connection pool within seconds of the first request after deploy. The fix: set autocommit immediately after acquiring a pooled connection, before running any query on it at all.

---

## 3. Roles and Access Model

| Role | Scope |
|---|---|
| **Admin** | Full access, plus bootstrap logins (hardcoded, always-admin regardless of assigned role) |
| **Trainer** | Full L&D-management access; plans Safety Compliance and the two centrally-scheduled DE Tech topics (Section 4.7); reviews/approves the Weekly Training Plan |
| **Learning Manager** | Same admin-tier access as Trainer for most features |
| **Senior Operations Manager (SOM)** | Sees their entire descendant org tree's rollup |
| **Operations Manager (OM)** | Sees their descendant org tree (Area Managers reporting up through Team Leads, etc.) |
| **Team Lead** | Shift-bound; co-plans DE Tech topics with the Area Manager for their department |
| **Area Manager (AM)** | Sees their own team; plans DE Tech (ambassador-executed topics) and Indirect Roles training for their associates |
| *(unassigned / pending)* | Read-only, minimal access, prompted to request a role |

Org hierarchy is resolved via `user_roles.reports_to` — `_resolve_am_scope(login)` walks a manager's entire descendant chain (not just people whose `am_login` literally equals their own login), so an OM/SOM's numbers reflect their whole tree.

**Regional/Global access** is a separate, additive permission (`global_admins` table) layered on top of any role — it grants access to the Regional Overview and site-creation/management, independent of whether someone is also an Admin, Trainer, or Operations-side role.

**Cross-Training Exclusions** carry a deliberately narrower permission than the rest of L&D-management: Admin and Trainer only, not Learning Manager or SOM — excluding someone from counting as "Trained" on a process is treated as a stronger action than editing a target number.

---

## 4. The Seven L&D Metrics

Every scorecard, ranking, and trend view in the app is organized around seven categories (`db.SCORECARD_CATEGORIES`):

| Key | Label | Primary data source |
|---|---|---|
| `safety_compliance` | Safety Compliance | `tracked_items` (sections `planning_compliance`, `compliance_safety`) |
| `de_tech` | DE Technical Briefing | `tracked_items` (section `planning_de_tech`) |
| `indirect_roles` | Indirect Roles | `ir_dashboard` / `ir_role_config` headcount-vs-target model |
| `cross_training` | Cross-Training | `xt_hours` proficiency status |
| `instructor_mgmt` | Ambassador Availability | `ambassadors` + `xt_hours` process-hours model |
| `ambassador_readiness` | Ambassador Readiness | Same Ambassador data, readiness-specific view |
| `bts_compliance` | BTS Compliance | `tracked_items` (section `planning_bts`) |

Each manager's per-metric score, and a **Total L&D Score** (the average of whichever of the seven they have data for), are ranked against every other Area/Operations/Senior Operations Manager — competition-style ranking (tied scores share a rank), tie-broken by scope of responsibility (more direct/indirect reports ranks first), not arbitrary ordering.

### 4.1 `_build_scorecard_categories()` — the shared computation

This single function in `application.py` computes all seven metrics for a given scope (an AM's own login, an OM/SOM's descendant tree, or `None` for a full site-wide rollup) and is the one code path every scorecard, ranking, and trend view calls into. Cross-Training, Ambassador Availability, and Ambassador Readiness are special-cased inside it — they're computed from `xt_hours`/`ambassadors` directly rather than from `tracked_items`, since no CSV importer feeds a generic staffing section for them. Critically, `single_am_login` is **always** passed as the login itself, never conditionally set to `None` based on whether that person has descendants — the underlying per-am functions (`_indirect_roles_card_for_am`, `_ambassador_scorecard_for_am`, etc.) already correctly walk an OM/SOM's whole descendant tree internally; gating this out for anyone with descendants was a real bug found and fixed mid-session, since it silently discarded the correct combined view in favor of a much narrower, usually-empty generic scorer.

---

## 5. Feature Reference

### 5.1 AM Overview ("My L&D Scorecard")

An Area Manager's (or Trainer's, viewing an assigned AM's) personal landing page: a compliance-ring showing their average score, a rank badge (Total L&D Rank) next to it, seven metric tiles each showing that metric's own rank, a weekly training plan preview, and tabs for Planning, My Trainings, and OLR.

### 5.2 Reporting / Compliance Rankings

Site-wide (or, for OM/SOM, scope-wide) rollups across every metric, plus the full manager-by-manager ranking table for each of the seven metrics and the Total L&D composite — switchable via a tab bar, each rendered from the same underlying ranking function, with "copy as image" export built for every one of them.

### 5.3 Cross-Training

The most heavily developed single feature area:

- **Standards** — approved cross-training paths (Inbound/Outbound), each with a target percentage of source-department headcount, per shift, shown as card grids matching a specific dark "glass" visual treatment.
- **Proficiency model** — every associate's status (Proficient / Practice / Refresh / Lapsed) is derived from hours-on-process across rolling 60/90/180-day windows: `Total_Hours_60 ≥ 20` → Proficient; else `Total_Hours_90 ≥ 20` → Practice; else `Total_Hours_180 ≥ 20` → Refresh; else Lapsed.
- **Expiry projection** — `compute_xt_expiry()` projects forward from an associate's last-worked date on a process to when they're likely to lapse if not re-staffed; the exact threshold (days without practice before projected lapse) is a configurable `app_settings` value, not a hardcoded constant, since it's a genuine policy value the app has no authoritative source for.
- **Retention** — a shift-first, department-second view of who's projected to lapse, split into associates native to that department versus cross-trained in from elsewhere.
- **Exclusions** — a governed mechanism (Trainer/Admin only, reason required) to stop one associate from counting as Trained on one specific process, without altering underlying data. Implemented at the data layer: `xt_hours.raw_trained_status` preserves the real ingested value, while `trained_status` (the column every existing query already checks) gets overridden to `'Excluded'` — so every downstream computation keeps working unmodified. Takes effect immediately against currently-loaded data, not just future uploads, and survives re-ingestion (a fresh upload re-applies any still-active exclusion).
- **Associate Directory** — searchable, filterable per-associate process/hours view.
- **Definitions** — admin-editable path/target/process-mapping catalog, including which processes count toward Ambassador Availability per department (moved from a hardcoded dict to a database table so gaps like "Ship Dock and ICQA have no processes tracked" are both fixable by an admin and, until fixed, shown honestly rather than as a misleading zero).

### 5.4 Indirect Roles

A department × shift coverage matrix against defined per-role targets, with per-cell readiness breakdown (trained-with-practice / no-practice / not-trained) and an automatically surfaced "priority insight" — the single worst-covered section/shift combination, so attention goes where it's needed without a manual scan.

### 5.5 Ambassador Program

- **Ambassador Management** — the roster of process Ambassadors and Indirect-Roles Ambassadors, by department and shift; an ambassador can be either or both.
- **Ambassador Meetings / Attendance** — per-shift meeting-status and individual-attendance tracking, with document upload (S3-backed) for agendas/decks.
- **Ambassador Hours / Practice Health** — per-process hours-on-function against a flag threshold, aggregated to a department-level Practice Health percentage.
- **The qualification chain**: an Ambassador → their assigned Indirect Roles (`ambassador_indirect_roles`) → the DE Tech topics those roles are mapped to cover (`de_tech_role_mapping`), or the Indirect Role itself. This chain is what determines who is actually qualified to execute a given DE Tech briefing or Indirect Role training session (Section 5.7).

### 5.6 DE Technical Briefing — the Trainer/Ambassador ownership split

This is a specific, load-bearing business rule threaded through several features:

- **FSRI** and **Robotic Arm Palletizer** (`db.DE_TECH_CENTRALLY_SCHEDULED`) are centrally scheduled by **Trainers**.
- **Every other DE Tech topic** is planned by an **Area Manager or Team Lead** and executed by a qualified **Indirect Roles Ambassador**.
- **All of Indirect Roles** training is likewise Ambassador-executed, AM/Team-Lead planned.
- **Safety Compliance**, in full, is also Trainer-planned — not carved out at all.

`db.TRAINER_PLANNED_METRICS` encodes the Trainer-owned side (`safety_compliance` with no topic restriction; `de_tech` restricted to the two centrally-scheduled topics). The Ambassador-owned side (regular DE Tech topics, Indirect Roles) has its own gap-finding function (`get_de_tech_ambassador_gaps()`) and its own planning table (`ambassador_training_sessions`) — see Section 5.7.

### 5.7 Weekly Training Plan

Two genuinely different workflows live on this one page, reflecting the ownership split above.

**Trainer-led plan (built, working end to end):**
1. **Generate** — one button computes who's currently overdue across both Trainer-owned metrics for a given shift, groups by topic into slots up to a configurable capacity, and creates them as unapproved drafts with no time set. A configurable **minimum group size** prevents a session being drafted for too few people to be worth running — anyone below the minimum is held back and picked up automatically once enough accumulate. Where a matching `trainings` catalog entry exists for the topic, the slot links to it (so it displays a real name instead of "Untitled training").
2. **Review** — a Trainer sees each draft, can remove an individual associate, or discard the whole slot, entirely via AJAX (no page reload, no confirmation dialog).
3. **Approve** — requires setting an actual time (the one thing the algorithm deliberately leaves blank); flips the slot from draft to approved, at which point it becomes visible on the normal board and to any AM-facing view.
4. **Edit** — an approved slot's time, capacity, instructor, room, and linked training can all be edited afterward via the same slot-detail modal used everywhere else on the board.

Re-running the generator never duplicates anyone already scheduled (draft or approved) for that week/shift — it only adds newly-overdue people.

**Ambassador-executed planning (backend only — no UI yet):** `get_de_tech_ambassador_gaps()` finds who's overdue on any non-FSRI/RAP DE Tech topic; `get_ambassadors_qualified_for_topic()` walks the qualification chain to find who can actually teach it, filterable by shift; `add_ambassador_training_session()` / `get_ambassador_training_sessions()` / `cancel_ambassador_training_session()` provide the plan/view/cancel lifecycle. **No page exists yet for an Area Manager to actually use this**, and Indirect Roles' own gap-finding (a headcount/readiness model, not an individual-overdue-record model like DE Tech) hasn't been built at all. See Section 7.

### 5.8 OLR (Operational Leadership Review)

A personal trend dashboard: weekly score history per metric (native SVG line charts, no external charting library), rank movement over time, Safety's average-overdue-days trend and current top overdue topics, Indirect Roles/Ambassador gap trends, and the weekly Cross-Training proficiency breakdown. Populated by a weekly snapshot (`olr_weekly_metrics`) taken automatically whenever new data is uploaded — trend history begins accumulating from whenever a given figure started being snapshotted, with no retroactive backfill possible for anything that wasn't captured before.

### 5.9 Escalations

A ticketing workflow (phased: 1/2/3) for compliance issues needing follow-up, with an integration point for creating a corresponding ticket in Amazon's internal Tickety system (SigV4A-signed, via a vendored service model — not yet fully wired to a real Tickety endpoint at time of writing).

### 5.10 Regional Overview

The cross-site view described in Section 2.1 — a summary banner (sites stable vs. needing attention, average Total L&D Score across the region, total open escalations) and a per-site table across all seven metrics plus Total, with a "focus first" column naming each site's single worst metric.

### 5.11 Trainer Overview

The same seven-metric scorecard format as AM Overview, but combined across every Area Manager explicitly assigned to a Trainer (`trainer_am_assignments`) — a relationship maintained separately from the reporting hierarchy.

### 5.12 L&D Settings / Data Upload

The single ingestion point for every raw CSV export (Safety Compliance, DE Tech, BTS, Cross-Training Hours, Indirect Roles rosters). Every ingestion function uses case-insensitive column matching (`_ci_row`) against the source file's headers — a real production bug was traced to one ingestion path (`ingest_xt_hours_csv`) being the sole exception, using exact-case string matching that silently failed (nulling a field, or in the worst case skipping the row entirely) whenever a real export's header casing didn't match what the code assumed.

---

## 6. Design Principles

- **Compute live, never cache silently.** Every view reflects whatever data is currently loaded at render time — no separate "refresh" step, no risk of a stale number being mistaken for current.
- **Scope correctly by construction.** The same org-resolution and per-am computation functions underlie every role's view, so a fix to one applies consistently everywhere, rather than needing separate logic per role.
- **Be honest about data provenance.** Where the app has no authoritative source for a business rule (the Cross-Training expiry threshold; the exact process list for a given department's Ambassadors), that value is an editable setting, exposed as such, rather than a hardcoded assumption presented with false confidence — and where data genuinely isn't tracked at all (Ship Dock/ICQA's Ambassador processes, before being configured), the UI says so explicitly rather than showing a misleading zero.
- **No external runtime dependencies.** Every chart and "copy as image" export is hand-built (SVG/canvas), not pulled from a CDN — a deliberate choice given the app cannot assume reliable access to the public internet from every deployment environment.
- **Test before shipping, not after.** Every feature described above was verified against real data through actual HTTP routes (not just unit-level function calls) before being packaged — this is the standard this whole codebase has been held to, and the standard any continuation of it should be held to as well.

---

## 7. Known Limitations and Unfinished Work

This section exists to prevent an accurate picture from becoming an overclaimed one.

- **The Ambassador-executed planning UI does not exist.** The backend (Section 5.7) is built and tested; there is no page for an Area Manager or Team Lead to actually use it.
- **Indirect Roles has no gap-finding function for the planning workflow.** Its data model (headcount/readiness against a target) is structurally different from DE Tech's (individual overdue records), and nothing has been built to bridge that difference yet.
- **No "personal calendar" view exists** combining an AM's own planned sessions with visibility into Trainer-approved slots — this was explicitly requested and not yet started.
- **The Cross-Training proficiency expiry threshold is a placeholder default**, pending confirmation of the real policy value.
- **Some department-to-process mappings were empirically derived**, not drawn from a single authoritative source, because none currently exists.
- **The Tickety escalation integration is not connected to a real endpoint** — the signing/vendoring infrastructure exists, but the actual service call is unverified against production Tickety.
- **"Copy as image" exports are hand-built approximations** of each page's visual design, maintained by hand as the interface evolves, not an automated capture — they can drift from the live page's exact appearance over time.
