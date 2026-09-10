# LDIS: A Unified Operations Console for Learning & Development

### White Paper — EU12 Fulfillment Center Network

---

## Executive Summary

Learning & Development compliance across a fulfillment center network is, by nature, fragmented. Safety training lives in one export. Cross-training hours live in another. Indirect role coverage, DE Technical Briefing status, BTS compliance, and Ambassador program health each arrive as their own spreadsheet, on their own cadence, in their own format — and none of them talk to each other. An Area Manager trying to answer "how healthy is my team's training right now" has historically had to open five or six files, cross-reference logins by hand, and trust that nothing was stale.

**LDIS (Learning & Development Intelligence Systems)** was built to close that gap. It is a single web application that ingests the raw exports L&D teams already receive, computes every compliance metric live from that data, and presents it through role-aware views — so an Area Manager sees their own team, an Operations Manager sees their whole reporting tree rolled up correctly, and a Trainer sees every Area Manager assigned to them, all from the same underlying numbers.

This paper describes what LDIS does, how it is built, and the design principles that shaped it — including where it is deliberately incomplete, and why.

---

## 1. The Problem

Before LDIS, tracking L&D health at a site meant:

- **Manual cross-referencing.** Safety compliance, DE Tech, BTS, Cross-Training, Indirect Roles, and Ambassador data each existed as separate exports with no shared view. Answering "is this specific person on track" required opening multiple files and matching logins by hand.
- **No org-aware rollups.** A raw export knows an associate's direct supervisor. It has no concept of an Operations Manager's or Senior Operations Manager's *entire* reporting tree — so a manager two or three levels up had no accurate way to see their whole team's numbers combined, only whatever they could painstakingly assemble themselves.
- **No trend visibility.** A spreadsheet is a snapshot. Without a system capturing the same figures week over week, there was no way to see whether a metric was improving, plateauing, or sliding — only what it looked like today.
- **No forward-looking risk signal.** Cross-training proficiency degrades over time without practice. Nothing in the raw data flagged *who* was about to lose a qualification before it actually lapsed — by the time it showed up as a gap, it was already too late to act on.
- **No accountability trail.** Rankings, if they existed at all, were assembled ad hoc, with no consistent tie-breaking logic and no way to see how a manager's standing had moved over time.

None of this is a data problem. The data already exists. What was missing was a system to unify it, compute from it live, and present it to the right person at the right level of aggregation.

---

## 2. The Solution

LDIS is a Flask-based web application that:

1. **Ingests** the same raw CSV exports L&D already works with — Safety Compliance, DE Technical Briefing, BTS, Cross-Training Hours-on-Function, and Indirect Roles rosters — through a single Data Upload interface.
2. **Computes every metric live**, on every page load, from whatever data is currently loaded. Nothing is pre-baked or manually maintained; upload a fresher file and every scorecard, ranking, and drill-down reflects it immediately.
3. **Scopes every view to the person looking at it.** An Area Manager's numbers are their own team's. An Operations Manager's numbers are the combined total of every Area Manager reporting to them, walked automatically through the org hierarchy — not a number anyone has to build manually.
4. **Surfaces risk before it becomes a gap**, projecting when a Cross-Training qualification is likely to lapse based on how long it's been since someone last worked that process, so managers can intervene while there's still time.
5. **Tracks trend over time**, snapshotting every core metric weekly so a manager can see whether their team's Safety Compliance, Cross-Training breadth, or Indirect Roles coverage is moving in the right direction.

---

## 3. Core Capabilities

### 3.1 The Seven L&D Metrics

LDIS organizes every downstream feature around seven scorecard categories:

| Metric | What it measures |
|---|---|
| **Safety Compliance** | Training completion status against Safety Compliance requirements, with overdue-day tracking and topic-level breakdowns |
| **DE Technical Briefing** | Technical briefing completion, derived from the source system's own priority-tier logic |
| **Indirect Roles** | Coverage of ICQA, Inbound, and other indirect operational roles against defined per-shift targets |
| **Cross-Training** | Associate proficiency (Proficient / Practice / Refresh / Lapsed) across every approved cross-training path |
| **Ambassador Availability** | Headcount of trained process Ambassadors against department-level targets, per shift |
| **Ambassador Readiness** | Whether existing Ambassadors are keeping their practice hours current |
| **BTS Compliance** | Completion tracking against BTS training requirements |

Every one of these rolls up into a **Total L&D Score** — a composite average across whichever of the seven a given manager has data for — and every metric, individually and in composite, is *ranked*: every Area Manager, Operations Manager, and Senior Operations Manager is ordered against their peers, tied scores share a rank, and ties are broken by scope of responsibility (more direct and indirect reports ranks higher), not arbitrarily.

### 3.2 Role-Aware Views

The same underlying data powers different views depending on who's looking:

- **AM Overview** — an Area Manager's personal scorecard: their own compliance, planning, weekly training plan, and — for Cross-Training specifically — the defined standards paths that actually apply to their department, shown in the exact same visual format as the site-wide Cross-Training page.
- **Trainer Overview** — the same scorecard, but combined across every Area Manager assigned to that Trainer (a distinct relationship from the org-reporting hierarchy, tracked separately).
- **Reporting / Compliance Rankings** — the site-wide view: every metric, ranked, filterable by role and department, exportable as an image or CSV for sharing outside the tool.
- **OLR (Operational Leadership Review)** — a personal trend dashboard: weekly score history per metric, rank movement over time, Safety's average overdue days and top overdue topics, Indirect Roles and Ambassador gap trends, and the weekly Cross-Training proficiency breakdown — all charted natively, with no external charting dependency.

### 3.3 Cross-Training Management

Cross-Training receives the deepest treatment in the system, reflecting its complexity:

- **Standards & Definitions** — the approved cross-training paths (Inbound and Outbound), each with a defined target percentage of source department headcount, per shift.
- **Retention** — a shift-first, department-second view of who is projected to lapse, broken into associates native to that department versus those cross-trained in from elsewhere.
- **Proficiency Expiry Projection** — using each associate's last-worked date on a process, LDIS projects forward to when their proficiency is likely to lapse if they aren't staffed on it again, and surfaces this as a live, adjustable "lapsing in X days" view on every relevant card.
- **Exclusions** — a governed mechanism (limited to Trainers and Admins, requiring a documented reason) to remove a specific associate from counting as Trained on a specific process, without needing to alter the underlying data. Exclusions apply immediately and survive future data uploads.
- **Associate Directory** — a searchable, filterable directory of every associate's trained processes, hours, and proficiency status.

### 3.4 Indirect Roles Overview

A department-and-shift matrix showing coverage against target for every tracked indirect role, with per-cell readiness breakdowns (trained-with-practice, no-practice, not-trained) and an automatically surfaced "priority insight" — the single worst-covered shift and section combination — so attention goes where it's most needed without requiring a manual scan of every row.

### 3.5 Ambassador Program Management

Ambassador Management, Ambassador Meetings, and the Ambassador Attendance/Practice reporting tabs together track the process-ambassador program: who is trained on which indirect role or process, whether they're keeping their practice hours current, and whether they're attending required meetings — again broken down by department and shift rather than pooled into a single site-wide number that could hide where the actual gap is.

### 3.6 Weekly Training Plan & Escalations

A drag-and-drop weekly planning board for assigning associates who need training to available slots, and an escalation ticketing workflow for tracking compliance issues that require follow-up, including an integration point for creating a corresponding ticket in Amazon's internal Tickety system.

---

## 4. Design Principles

**Compute live, never cache silently.** Every scorecard, ranking, and chart is computed from whatever data is currently loaded at the moment the page renders. This means a fresh upload is reflected everywhere, instantly, with no separate "refresh" or "recalculate" step to remember — and no risk of a stale cached number being mistaken for current.

**Scope correctly by construction, not by convention.** The same department-and-shift resolution logic underlies every role-scoped view — an Area Manager, Trainer, Operations Manager, and Senior Operations Manager are all resolved through the same code path, so a fix or improvement to how one role's data aggregates applies consistently to all of them, rather than needing to be re-implemented per role.

**Be honest about data provenance.** Where LDIS relies on a business rule it has no authoritative source for — such as exactly how many days without practice constitute a lapsed qualification — that value is exposed as a clearly labeled, editable setting rather than a hardcoded assumption presented with false confidence. Historical trend data begins accumulating from the day a metric starts being tracked, not retroactively, and the interface says so rather than implying a longer history than actually exists.

**No external runtime dependencies.** Every chart, every "copy as image" export, and every interactive drill-down is built natively — SVG line charts and canvas-based image rendering are written from scratch rather than pulled in from a public CDN. On a network that cannot be assumed to reach the public internet reliably, a feature that silently fails for some users is worse than not having it; self-containment was treated as a hard constraint, not a preference.

**Ranking with real tie-breaking.** Where managers are compared, ties are handled with intention: equal scores share a rank (competition-style, not arbitrary ordinal ordering), and the tie-break criterion — scope of responsibility — is itself meaningful rather than alphabetical happenstance.

---

## 5. Technical Architecture

| Layer | Technology |
|---|---|
| Application framework | Python / Flask |
| Database | PostgreSQL (Amazon RDS) in production, with an automatic SQLite fallback for local development and testing |
| Hosting | AWS Elastic Beanstalk |
| Authentication | Federate / Midway OIDC via an Application Load Balancer, with LDAP Group Mapper claims resolving role assignment; a manual login path exists for local development |
| Object storage | Amazon S3, for uploaded documents |
| Frontend | Server-rendered Jinja templates with vanilla JavaScript — no frontend framework or build step |
| Rich exports | Excel-based CSV ingestion for every data source; native canvas-rendered PNG export for sharing tables and dashboards outside the tool |

The application is organized around two core modules: `db.py`, which owns all data access, ingestion, and computation logic, and `application.py`, which owns routing, permissions, and page assembly — together spanning over ten thousand lines of Python, reflecting the genuine breadth of what the tool now covers.

---

## 6. Data Governance & Access Control

Access is role-based and layered:

- **Area Managers** see their own department and shift.
- **Operations Managers and Senior Operations Managers** see their combined reporting tree, resolved automatically through the org hierarchy rather than requiring manual configuration.
- **Trainers** see every Area Manager explicitly assigned to them — a distinct relationship, separately maintained, from the reporting hierarchy.
- **Admins, Trainers, and Learning Managers** hold elevated, tiered permissions for managing definitions, targets, and — in the case of Cross-Training Exclusions specifically — a deliberately narrower permission set limited to Trainers and Admins only, reflecting that excluding someone from a compliance count is a materially stronger action than editing a target number.

Every exclusion, role assignment, and definition change is attributed to the login that made it and, where applicable, requires a stated reason — preserving an accountability trail rather than allowing silent edits to figures that feed compliance reporting.

---

## 7. Honest Limitations

A white paper that only describes strengths is not a useful one. Several things are worth stating plainly:

- **The Cross-Training proficiency expiry window is a placeholder value**, not one LDIS has an authoritative source for. It is exposed as an editable setting specifically so it can be corrected once the real policy is confirmed, rather than presented as fact.
- **OLR trend data has no retroactive history.** Rank development, gap trends, and the Cross-Training weekly breakdown only begin accumulating from the point each was added to the weekly snapshot — there is no way to reconstruct what earlier weeks looked like.
- **"Copy as image" is a hand-built approximation**, not a pixel-perfect capture of the live page. It is deliberately built without an external rendering library, which means it is maintained by hand as the interface evolves, rather than automatically staying in sync.
- **Some department-to-process mappings are empirically derived**, not drawn from a single authoritative source table — because none currently exists. Where this is the case, it is disclosed in the interface itself.

---

## 8. Conclusion

LDIS exists because L&D compliance data was never the problem — the absence of a system to unify, compute, and present it consistently was. By ingesting the same exports teams already work with, computing every metric live rather than relying on stale snapshots, and scoping every view correctly to the person looking at it, LDIS turns six disconnected spreadsheets into one console that answers, immediately and accurately: how healthy is this team's training right now, where specifically is the gap, and is it getting better or worse.
