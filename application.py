import os
import json
import re
import uuid
import csv
import io
from datetime import date, datetime, timedelta

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, flash, send_file, Response
)

import db
import oidc_auth
import tickety_client

application = Flask(__name__)
application.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app = application

ROLES = {
    "admin": "Admin",
    "trainer": "Trainer",
    "learning_manager": "Learning Manager",
    "som": "Senior Operations Manager",
    "om": "Operations Manager",
    "team_lead": "Team Lead",
    "am": "Area Manager",
}

# "Admin roles" per the org model: L&D staff (Trainer / Learning Manager)
# plus the two bootstrap logins — these see and configure everything.
# Operations roles are a separate org entirely (AM/OM/SOM/Team Lead) that
# L&D serves as an external function.
ADMIN_TIER_ROLES = {"admin", "trainer", "learning_manager"}
OPS_MANAGER_ROLES = {"om", "som"}       # pick any AM to view
OPS_SELF_ROLES = {"am", "team_lead"}    # own-scope only
BOOTSTRAP_ADMIN_LOGINS = {"admin", "mohafryd", "ofadfall", "swimuham"}


def _role_from_groups(groups):
    """Maps Cognito group names onto our role keys, tolerant of naming
    style (spaces/hyphens/underscores/case all normalize the same way) —
    so a group called 'Learning Manager', 'learning-manager', or
    'learning_manager' all match the same role. If someone's in multiple
    matching groups, the most privileged one wins."""
    if not groups:
        return None

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())

    normalized_targets = {}
    for key, label in ROLES.items():
        normalized_targets[norm(key)] = key
        normalized_targets[norm(label)] = key

    matched = {normalized_targets[norm(g)] for g in groups if norm(g) in normalized_targets}
    if not matched:
        return None
    priority = ["admin", "learning_manager", "trainer", "som", "om", "team_lead", "am"]
    for role in priority:
        if role in matched:
            return role
    return None


def _resolve_role(login, groups=None):
    """The assigned role for a login: bootstrap admins always resolve to
    'admin' regardless of what's stored. Next, a matching group wins —
    checked against both cognito:groups (if ALB is still fronted by
    Cognito somewhere) and Federate's LDAP Group Mapper claim (the
    'admin' / 'area manager' / 'team lead' / 'senior operations manager'
    values configured in the Federate console). Otherwise falls back to
    the user_roles table, or None if nobody's assigned them a role yet."""
    if login and login.lower() in BOOTSTRAP_ADMIN_LOGINS:
        return "admin"
    from_groups = _role_from_groups(groups)
    if from_groups:
        return from_groups
    return db.get_user_role(login)


def _effective_role():
    """The real assigned role, unless an admin-tier user has an active
    'view as' override for UI preview — never lets a non-admin escalate."""
    real = session.get("role")
    if real in ADMIN_TIER_ROLES:
        preview = session.get("view_as")
        if preview:
            return preview
    return real


def _deny(msg):
    flash(msg, "warning")
    return redirect(url_for("scorecards"))


def _days_overdue(due_date_str):
    """Days past a record's due date, for the Log Escalation record
    picker — None if there's no due date or it isn't a parseable date,
    negative if it's not due yet (not shown as 'overdue' in the UI)."""
    if not due_date_str:
        return None
    try:
        due = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return (date.today() - due).days


def _safety_overdue_detail_for_login(login):
    """Current-state detail behind a login's Safety Compliance number:
    average overdue days across their overdue records, and the topics
    (subcategory) with the most overdue records right now. This is a
    live snapshot of today, not a stored weekly history — see the OLR
    tab's note about which figures have trend data versus current-only."""
    scope = _resolve_am_scope(login)
    items = db.get_items(section="compliance_safety", am_login=scope)
    overdue_items = [
        item for item in items
        if item.get("status") in db.STATUS_GAP
        and _days_overdue(item.get("due_date")) is not None
        and _days_overdue(item.get("due_date")) > 0
    ]
    if not overdue_items:
        return {"avg_overdue_days": None, "overdue_count": 0, "top_topics": []}
    avg_days = round(sum(_days_overdue(i.get("due_date")) for i in overdue_items) / len(overdue_items), 1)
    topic_counts = {}
    for item in overdue_items:
        topic = item.get("subcategory") or "Unspecified"
        topic_counts[topic] = topic_counts.get(topic, 0) + 1
    top_topics = sorted(topic_counts.items(), key=lambda t: -t[1])[:5]
    return {
        "avg_overdue_days": avg_days, "overdue_count": len(overdue_items),
        "top_topics": [{"topic": t, "count": n} for t, n in top_topics],
    }


def _svg_line_chart(series_by_label, colors, width=680, height=200, y_min=0, y_max=100, y_suffix="%"):
    """Renders a simple multi-line chart as raw SVG markup, server-side —
    no charting library, matching this app's deliberate zero-external-
    dependency pattern. series_by_label: {label: [(week_start, value), ...]}
    ordered ascending by week, values already in [y_min, y_max] (or None
    to skip a point without breaking the line). colors: {label: '#hex'}.
    Returns (svg_markup, weeks) — weeks is the sorted list of week_start
    strings actually present, for building an x-axis legend outside the
    SVG since rotated/small text renders inconsistently across SVG
    viewers."""
    all_weeks = sorted({w for points in series_by_label.values() for w, v in points})
    if not all_weeks:
        return None, []
    pad_l, pad_r, pad_t, pad_b = 36, 12, 12, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    n = len(all_weeks)

    def x_for(i):
        return pad_l + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_for(value):
        value = max(y_min, min(y_max, value))
        return pad_t + plot_h - (plot_h * (value - y_min) / (y_max - y_min) if y_max > y_min else 0)

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%; height:auto;">']
    for frac in (0, 0.25, 0.5, 0.75, 1):
        gy = pad_t + plot_h * (1 - frac)
        gval = round(y_min + (y_max - y_min) * frac)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" stroke="#e5e5ea" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{gy + 3:.1f}" font-size="9" fill="#8e8e93" text-anchor="end">{gval}{y_suffix}</text>')

    week_index = {w: i for i, w in enumerate(all_weeks)}
    for label, points in series_by_label.items():
        color = colors.get(label, "#0b5cd7")
        by_week = {w: v for w, v in points if v is not None}
        segs = []
        current = []
        for w in all_weeks:
            if w in by_week:
                current.append((x_for(week_index[w]), y_for(by_week[w])))
            else:
                if len(current) > 1:
                    segs.append(current)
                current = []
        if len(current) > 1:
            segs.append(current)
        for seg in segs:
            d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in seg)
            parts.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')
        for w, v in points:
            if v is None:
                continue
            parts.append(f'<circle cx="{x_for(week_index[w]):.1f}" cy="{y_for(v):.1f}" r="2.6" fill="{color}"><title>{label}: {w} — {v}{y_suffix}</title></circle>')
    parts.append("</svg>")
    return "".join(parts), all_weeks


OLR_METRIC_COLORS = {
    "Safety Compliance": "#d70015", "DE Technical Briefing": "#a85b00", "Indirect Roles": "#8944ab",
    "Cross-Training": "#0b5cd7", "Ambassador Availability": "#248a3d", "Ambassador Readiness": "#00838f",
    "BTS Compliance": "#c2185b", "Total L&D Score": "#101828",
}


def _build_olr_tab_data(login):
    """Everything the OLR tab shows for one person: the weekly score
    trend across all 7 metrics + the total composite, each metric's
    rank trend, Safety's average-overdue-days trend plus today's live
    top overdue topics (not trended — see snapshot_olr_metrics_for_all_reviewees
    for why), Indirect Roles' and Ambassador Availability's weekly gap
    counts, and Cross-Training's weekly Proficient/Practice/Refresh/
    Lapsed breakdown. Trend data only exists from whenever this app
    started snapshotting each figure — there's no way to backfill
    history for anything that wasn't being captured before."""
    series = db.get_olr_weekly_series(login)
    weeks_sorted = sorted(series.keys())

    def points_for(metric_key):
        return [(wk, series[wk].get(metric_key)) for wk in weeks_sorted]

    score_series = {label: points_for(key) for key, label, _s in LD_METRICS}
    composite_points = [(wk, db._olr_composite(series[wk])) for wk in weeks_sorted]
    score_series["Total L&D Score"] = composite_points
    score_chart, score_weeks = _svg_line_chart(score_series, OLR_METRIC_COLORS, y_min=0, y_max=100)

    manager_count = max(1, len(db.get_roster_by_roles(["am", "om", "som"])))
    rank_series = {label: points_for(f"{key}_rank") for key, label, _s in LD_METRICS}
    rank_series["Total L&D Score"] = points_for("total_rank")
    rank_chart, rank_weeks = _svg_line_chart(
        rank_series, OLR_METRIC_COLORS, y_min=1, y_max=manager_count, y_suffix="", height=200,
    )

    safety_overdue_days_series = {"Avg overdue days": points_for("safety_avg_overdue_days")}
    max_days = max([v for _w, v in safety_overdue_days_series["Avg overdue days"] if v is not None], default=30)
    overdue_days_chart, _weeks = _svg_line_chart(
        safety_overdue_days_series, {"Avg overdue days": "#d70015"}, y_min=0, y_max=max(max_days, 1), y_suffix="d", height=140,
    )

    ir_gap_series = {"Indirect Roles gaps": points_for("ir_gap")}
    max_ir_gap = max([v for _w, v in ir_gap_series["Indirect Roles gaps"] if v is not None], default=5)
    ir_gap_chart, _weeks = _svg_line_chart(
        ir_gap_series, {"Indirect Roles gaps": "#8944ab"}, y_min=0, y_max=max(max_ir_gap, 1), y_suffix="", height=140,
    )

    ambassador_gap_series = {"Ambassador gaps": points_for("ambassador_gap")}
    max_amb_gap = max([abs(v) for _w, v in ambassador_gap_series["Ambassador gaps"] if v is not None], default=5)
    ambassador_gap_chart, _weeks = _svg_line_chart(
        ambassador_gap_series, {"Ambassador gaps": "#248a3d"}, y_min=-max(max_amb_gap, 1), y_max=max(max_amb_gap, 1), y_suffix="", height=140,
    )

    xt_series = {
        "Proficient": points_for("xt_proficient"), "Practice": points_for("xt_practice"),
        "Refresh": points_for("xt_refresh"), "Lapsed": points_for("xt_lapsed"),
    }
    max_xt = max([v for pts in xt_series.values() for _w, v in pts if v is not None], default=10)
    xt_colors = {"Proficient": "#248a3d", "Practice": "#0b5cd7", "Refresh": "#a85b00", "Lapsed": "#d70015"}
    xt_chart, _weeks = _svg_line_chart(xt_series, xt_colors, y_min=0, y_max=max(max_xt, 1), y_suffix="", height=160)

    return {
        "score_chart": score_chart, "score_weeks": score_weeks, "score_colors": OLR_METRIC_COLORS,
        "rank_chart": rank_chart, "rank_weeks": rank_weeks,
        "overdue_days_chart": overdue_days_chart,
        "ir_gap_chart": ir_gap_chart,
        "ambassador_gap_chart": ambassador_gap_chart,
        "xt_chart": xt_chart, "xt_colors": xt_colors,
        "safety_overdue_detail": _safety_overdue_detail_for_login(login),
        "has_history": bool(weeks_sorted),
        "week_count": len(weeks_sorted),
    }


def _resolve_am_scope(base_login):
    """Broadens a single manager login into their whole org tree — self
    plus every direct AND indirect report walked via user_roles.reports_to
    — so a manager's numbers reflect their whole chain, not just people
    whose am_login happens to literally equal their own login. Returns
    the plain login unchanged if they have nobody reporting up to them
    (the common case — most AMs are leaves)."""
    if not base_login:
        return base_login
    descendants = db.get_descendant_ams(base_login)
    if not descendants:
        return base_login
    return [base_login] + list(descendants.keys())


def _escalation_visibility_scope(login, role):
    """Who this viewer is allowed to see escalations for, on the
    Escalations tab and their Overview panel: admin-tier and anyone
    holding a General Manager / Site Lead title see everything (scope
    None). An AM sees only escalations assigned to themselves. An OM
    sees themselves plus every AM reporting to them. A Senior Ops
    Manager sees themselves plus their whole tree — every OM and AM
    under them, direct or indirect. Returns (scope, include_som_broadcast)
    — the second flags whether this viewer should also see the phase-3
    broadcast-to-SOMs record escalations."""
    if role in ADMIN_TIER_ROLES:
        return None, False
    title = db.get_user_title(login)
    if db._is_gm_or_site_lead_title(title):
        return None, False
    descendants = db.get_descendant_ams(login)
    scope = [login] + list(descendants.keys())
    return scope, role == "som"


def _department_shift_pairs_for_login(login):
    """Every unique (department, shift) combination this login is
    responsible for — their own, if they have one set directly (the
    normal AM case), every department manually assigned to them (the
    SOM case — covering all three shifts for each, since a manual
    department assignment isn't shift-specific the way an AM's own
    profile is), every Area Manager anywhere in their reporting tree
    (the OM/SOM case, whose own login has no department of its own),
    and every Area Manager assigned to them as a Trainer (a completely
    separate relationship from the reports_to chain — trainer_am_assignments,
    not org hierarchy — so it needs its own lookup, not reuse of
    get_descendant_ams). A set, not a list — two managers sharing the
    same department+shift (a job-share, or simply both assigned to the
    same coverage) collapse to one entry, since the underlying coverage
    is the same regardless of who's counted as responsible for it, and
    must never be counted twice."""
    pairs = set()
    own_shift, own_department = db.get_user_shift_and_department(login)
    if own_shift and own_department:
        pairs.add((db.normalize_department(own_department), own_shift))
    for department in db.get_assigned_departments(login):
        for shift_key in db.SHIFTS:
            pairs.add((department, shift_key))
    descendant_logins = set(db.get_descendant_ams(login).keys())
    descendant_logins.update(db.get_ams_for_trainer(login))
    am_logins = [l for l in descendant_logins if db.get_user_role(l) == "am"]
    for am_login in am_logins:
        am_shift, am_department = db.get_user_shift_and_department(am_login)
        if am_shift and am_department:
            pairs.add((db.normalize_department(am_department), am_shift))
    return pairs


def _indirect_roles_card_for_am(login):
    """Builds the Indirect Roles tile from every department+shift this
    login is responsible for — their own, or (for an OM/SOM) the
    deduplicated set across their whole reporting tree — using the
    actual Indirect Role engine, not the generic tracked_items sections,
    which have no importer feeding them and are always empty.
    Trained-with-practice counts toward compliance; trained-without-
    practice ('risk') and not-trained-but-with-practice ('gap') both
    count against it, even when the raw headcount already meets target
    — hitting a number isn't enough if the people behind it aren't
    actually ready."""
    pairs = _department_shift_pairs_for_login(login)
    if not pairs:
        return None
    overview = db.compute_ir_overview()
    ok = risk = gap = target_sum = 0
    matched_roles = []
    for department, shift in pairs:
        # The role's home_process might be stored under an informal
        # alias (Chutings for AFE, etc.) even when the department here
        # is the canonical name, or vice versa — match against every
        # known name for this department, not just the exact string.
        hp_group = db.department_group(department)
        for sec in overview:
            for role in sec["roles"]:
                if role["home_process"] not in hp_group:
                    continue
                s = next((x for x in role["shifts"] if x["shift"] == shift), None)
                if not s:
                    continue
                if s["target"] <= 0 and s["trained"] == 0 and s["no_practice"] == 0 and s["not_trained"] == 0:
                    continue
                ok += s["trained"]
                risk += s["no_practice"]
                gap += s["not_trained"]
                target_sum += max(s["target"], 0)
                matched_roles.append({"role_id": role["id"], "role": role["role"], "section": sec["section"],
                                       "shift": s, "department": department})
    if not matched_roles:
        return None
    total_people = ok + risk + gap
    # Same rule as the per-role pct above: only trained-with-practice
    # counts toward compliance, so this is naturally capped at 100% and
    # can't read 100% while risk or gap is non-zero.
    if total_people > 0:
        pct_ok = round(100 * ok / total_people)
    elif target_sum > 0:
        pct_ok = 0
    else:
        pct_ok = None
    departments = sorted({p[0] for p in pairs})
    shifts = sorted({p[1] for p in pairs})
    breakdown = []
    for department, shift in sorted(pairs):
        pair_roles = [r for r in matched_roles if r["department"] == department and r["shift"]["shift"] == shift]
        if not pair_roles:
            continue
        p_ok = sum(r["shift"]["trained"] for r in pair_roles)
        p_risk = sum(r["shift"]["no_practice"] for r in pair_roles)
        p_gap = sum(r["shift"]["not_trained"] for r in pair_roles)
        p_total = p_ok + p_risk + p_gap
        breakdown.append({
            "department": department, "shift": shift, "ok": p_ok, "risk": p_risk, "gap": p_gap,
            "total": p_total, "role_count": len(pair_roles),
            "pct_ok": round(100 * p_ok / p_total) if p_total > 0 else None,
        })
    breakdown.sort(key=lambda b: (b["gap"] + b["risk"]), reverse=True)
    return {
        "ok": ok, "risk": risk, "gap": gap, "total": total_people, "pct_ok": pct_ok,
        "department": departments[0] if len(departments) == 1 else None,
        "shift": shifts[0] if len(shifts) == 1 else None,
        "departments": departments, "pairs": sorted(pairs), "roles": matched_roles, "breakdown": breakdown,
    }


def get_xt_gap_and_retention_for_login(login, scenario="general"):
    """Two distinct Cross-Training readouts for one AM/OM/SOM, scoped
    ONLY to processes that are actually defined cross-training targets
    for their department(s) — external standards (db.get_xt_standard_defs)
    and internal targets (db.get_internal_xt_targets) both count; a
    lapsed record on a process that was never a defined target for
    their home department doesn't count toward either number, since it
    was never something they were supposed to be staffing in the first
    place.

    - gap: how many of their associates still need to be trained,
      summed across every defined path sourced from their department(s)
      — their own share of (target headcount) minus (actually trained
      headcount) per path per shift, floored at 0 so a surplus on one
      path never offsets a shortfall on another. External standards'
      target is already a percentage of source headcount; internal
      targets are a fixed headcount for the whole department+shift, so
      that fixed number is allocated proportionally to this login's
      share of that department+shift's total headcount.
    - retention_pct: of the associates who ARE trained on a defined
      path, what percentage are still current (not Lapsed) — i.e. among
      the population already counted as trained, how much of that
      training is actually holding. None if nobody's trained on any
      defined path yet, not 0 — there's nothing to retain.

    Scope is resolved via _department_shift_pairs_for_login, the same
    function every other per-AM Ambassador/Indirect-Roles card already
    uses, so an OM/SOM's numbers correctly cover their whole descendant
    tree's departments, not just a department set directly on their own
    profile."""
    dept_shift_pairs = _department_shift_pairs_for_login(login)
    if not dept_shift_pairs:
        return {"gap": 0, "retention_pct": None, "retained": 0, "trained_total": 0}

    conn = db.get_db()
    ext_defs = [d for d in db.get_xt_standard_defs(scenario) if d["source"]]
    internal_defs = db.get_internal_xt_targets()

    total_gap = 0
    retained_count = 0
    trained_total = 0

    for department, shift in dept_shift_pairs:
        am_emps = {
            r["employee_login"] for r in conn.execute(
                "SELECT employee_login FROM xt_hours WHERE fclm_mapped=? AND shift=? AND lower(supervisor_login)=?",
                (department, shift, login.lower()),
            ).fetchall()
        }
        if not am_emps:
            continue
        dept_headcount = len(db._xt_source_employees(conn, [department], shift))
        am_share = (len(am_emps) / dept_headcount) if dept_headcount else 0

        relevant = []
        for std in ext_defs:
            if department in std["source"]:
                pct = std["pct"].get(shift, 0)
                relevant.append((std["target"], round(len(am_emps) * pct)))
        for it in internal_defs:
            if it["source_dept"] == department:
                fixed_target = it.get(f"target_{shift}") or 0
                relevant.append((it["target_process"], round(fixed_target * am_share)))

        for process, am_target in relevant:
            placeholders = ",".join("?" * len(am_emps))
            trained_rows = conn.execute(
                f"""SELECT employee_login, proficiency_status FROM xt_hours
                    WHERE employee_login IN ({placeholders}) AND merged_function=?
                      AND trained_status='Trained' AND shift=?""",
                list(am_emps) + [process, shift],
            ).fetchall()
            am_actual = len(trained_rows)
            total_gap += max(0, am_target - am_actual)
            trained_total += am_actual
            retained_count += sum(1 for r in trained_rows if r["proficiency_status"] != "Lapsed")

    conn.close()
    retention_pct = round(100 * retained_count / trained_total, 1) if trained_total else None
    return {"gap": total_gap, "retention_pct": retention_pct, "retained": retained_count, "trained_total": trained_total}


def _ambassador_scorecard_for_am(login):
    """Builds the Ambassador Availability tile from every department+
    shift this login is responsible for (see _department_shift_pairs_for_login),
    aggregated across the deduplicated set — instead of the generic
    tracked_items staffing_instructor scorer. Returns None if none of
    those department+shift pairs land on a tracked Ambassador
    department — the caller falls back to the old generic scorer in
    that case."""
    pairs = _department_shift_pairs_for_login(login)
    relevant = [(d, s) for d, s in pairs if d in db.AMBASSADOR_DEPARTMENTS]
    if not relevant:
        return None
    current_sum = target_sum = 0
    by_shift_cache = {}
    matched = []
    breakdown = []
    for department, shift in sorted(relevant):
        if shift not in by_shift_cache:
            by_shift_cache[shift] = db.get_ambassador_gap_summary(shift)
        row = next((r for r in by_shift_cache[shift] if r["department"] == department), None)
        if not row:
            continue
        current_sum += row["current"]
        target_sum += row["target"]
        matched.append(row)
        breakdown.append({
            "department": department, "shift": shift, "current": row["current"],
            "target": row["target"], "gap": row["current"] - row["target"],
        })
    if not matched:
        return None
    gap_sum = current_sum - target_sum
    pct_ok = 100 if target_sum <= 0 else max(0, min(100, round(100 * current_sum / target_sum)))
    departments = sorted({d for d, _ in relevant})
    shifts = sorted({s for _, s in relevant})
    breakdown.sort(key=lambda b: b["gap"])
    return {
        "ok": current_sum, "risk": 0, "gap": max(0, -gap_sum), "total": max(current_sum, target_sum), "pct_ok": pct_ok,
        "department": departments[0] if len(departments) == 1 else None,
        "shift": shifts[0] if len(shifts) == 1 else None,
        "departments": departments, "pairs": sorted(relevant), "breakdown": breakdown,
        "current": current_sum, "target": target_sum, "ambassador_gap": gap_sum,
    }


def _ambassador_readiness_card_for_am(login):
    """Builds the Ambassador Readiness tile: what fraction of ambassadors
    across every department+shift this login is responsible for have
    reached 20 hours on EVERY one of their tracked processes. Returns None under the same
    conditions as _ambassador_scorecard_for_am, plus when there's
    nobody with a trackable process list to evaluate."""
    pairs = _department_shift_pairs_for_login(login)
    relevant = [(d, s) for d, s in pairs if d in db.AMBASSADOR_DEPARTMENTS]
    if not relevant:
        return None
    ready_n = not_ready_n = 0
    breakdown = []
    for department, shift in sorted(relevant):
        summary = db.get_ambassador_readiness_summary(department, shift)
        r_count, nr_count = len(summary["ready"]), len(summary["not_ready"])
        ready_n += r_count
        not_ready_n += nr_count
        breakdown.append({
            "department": department, "shift": shift, "ready_count": r_count, "not_ready_count": nr_count,
            "not_ready": summary["not_ready"], "total": r_count + nr_count,
        })
    total = ready_n + not_ready_n
    if total == 0:
        return None
    # Recompute a genuine pooled attendance rate across every relevant
    # department+shift, rather than averaging each one's already-pooled
    # rate (which would weight a small team the same as a large one).
    attended_sum = meetings_sum = 0
    for department, shift in relevant:
        roster = db.get_ambassadors(shift, department=department).get(department, [])
        for a in roster:
            att = db.get_ambassador_attendance_summary(a["id"])
            attended_sum += att["attended"]
            meetings_sum += att["total"]
    avg_attendance_pct = round(100 * attended_sum / meetings_sum) if meetings_sum > 0 else None
    pct_ok = round(100 * ready_n / total)
    departments = sorted({d for d, _ in relevant})
    shifts = sorted({s for _, s in relevant})
    breakdown.sort(key=lambda b: -b["not_ready_count"])
    return {
        "ok": ready_n, "risk": 0, "gap": not_ready_n, "total": total, "pct_ok": pct_ok,
        "department": departments[0] if len(departments) == 1 else None,
        "shift": shifts[0] if len(shifts) == 1 else None,
        "departments": departments, "pairs": sorted(relevant), "breakdown": breakdown,
        "ready_count": ready_n, "not_ready_count": not_ready_n, "avg_attendance_pct": avg_attendance_pct,
    }


def _build_scorecard_categories(am_scope, fc_scope, single_am_login=None):
    """The seven My L&D Scorecard tiles. Cross-Training is special-cased:
    it's scored from xt_hours.proficiency_status (see
    db.get_xt_compliance_score) rather than the tracked_items staffing_xt
    section, which has no CSV importer feeding it and is always empty.
    Ambassador Availability and Ambassador Readiness are special-cased
    too, when single_am_login is given and resolves to a tracked
    Ambassador department: scored from that AM's own department+shift
    ambassador data instead of the generic staffing_instructor section.
    Every other category, and these two when no single-AM context
    applies (a trainer's multi-AM view, org-wide), still comes from
    db.scorecard() as before."""
    categories = []
    scores_for_avg = []
    for key, label, sections in db.SCORECARD_CATEGORIES:
        if key == "cross_training":
            xt = db.get_xt_compliance_score(am_login=am_scope, fc=fc_scope)
            card = {
                "ok": xt["proficient"], "risk": xt["refresh"] + xt["practice"], "gap": xt["lapsed"],
                "total": xt["total"], "pct_ok": xt["score"],
                "proficient": xt["proficient"], "refresh": xt["refresh"],
                "practice": xt["practice"], "lapsed": xt["lapsed"],
            }
        elif key == "indirect_roles" and single_am_login:
            card = _indirect_roles_card_for_am(single_am_login) or db.scorecard(sections, am_login=am_scope, fc=fc_scope)
        elif key == "instructor_mgmt" and single_am_login:
            card = _ambassador_scorecard_for_am(single_am_login) or db.scorecard(sections, am_login=am_scope, fc=fc_scope)
        elif key == "ambassador_readiness" and single_am_login:
            card = _ambassador_readiness_card_for_am(single_am_login) or {"ok": 0, "risk": 0, "gap": 0, "total": 0, "pct_ok": None}
        elif sections:
            card = db.scorecard(sections, am_login=am_scope, fc=fc_scope)
        else:
            card = {"ok": 0, "risk": 0, "gap": 0, "total": 0, "pct_ok": None}
        categories.append({"key": key, "label": label, "card": card})
        if card["pct_ok"] is not None:
            scores_for_avg.append(card["pct_ok"])
    overall_score = round(sum(scores_for_avg) / len(scores_for_avg)) if scores_for_avg else None
    return categories, overall_score


def _compute_olr_metrics_for_login(login):
    """The same 7 scorecard metric values shown on someone's own AM
    Overview, computed for one specific login — used both by the OLR
    snapshot below and (indirectly, via the shared helpers above) by
    the live tiles themselves, so a review always matches what that
    metric looked like on the actual scorecard at the time. Always
    passes single_am_login=login itself (never gated on whether they
    personally have descendants) — the special-cased Indirect
    Roles/Ambassador functions this triggers already correctly walk an
    OM/SOM's whole descendant tree internally via
    _department_shift_pairs_for_login, so gating this out for anyone
    with descendants only threw away that correct combined view in
    favor of the much narrower (and usually empty) generic scorer."""
    scope = _resolve_am_scope(login)
    categories, _ = _build_scorecard_categories(scope, None, single_am_login=login)
    return {c["key"]: c["card"]["pct_ok"] for c in categories}


def snapshot_olr_metrics_for_all_reviewees(week_start=None):
    """Recomputes every AM/OM's 7 scorecard metrics right now and
    upserts them into this calendar week's OLR record — this is what
    replaces a manual OLR upload: any time relevant data comes in
    through L&D Settings' Data Upload (compliance, DE Tech, Cross-
    Training hours, Indirect Roles), this runs and the week the upload
    happened in becomes that week's reporting snapshot. Safe to call
    repeatedly in the same week — it just refreshes that week's values
    each time, which is the right behavior as more data arrives.

    Beyond the 7 raw scores, this also captures each metric's rank (as
    '{key}_rank', plus 'total_rank' for the composite), the Cross-
    Training proficiency breakdown ('xt_proficient'/'xt_refresh'/
    'xt_practice'/'xt_lapsed'), and Safety's average overdue days
    ('safety_avg_overdue_days') — all as plain scalar values, so the
    OLR tab's trend charts have something to plot. Only the numeric
    figures are captured this way; things like Safety's top overdue
    topics aren't a single number and are shown live/current-only on
    the OLR tab instead, not trended."""
    week_start = week_start or db.week_start_for()
    conn = db.get_db()
    now = datetime.utcnow().isoformat()
    reviewees = db.get_olr_reviewees()
    reviewee_logins = {r["login"] for r in reviewees}
    all_rankings = get_all_ld_metric_rankings()
    ranks_by_login = {}
    for key, ranking in all_rankings.items():
        for row in ranking["rows"]:
            ranks_by_login.setdefault(row["login"], {})[key] = row["rank"]

    def upsert(login, metric_key, value):
        if value is None:
            return
        conn.execute(
            """INSERT INTO olr_weekly_metrics (login, week_start, metric_key, value, uploaded_by, uploaded_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (login, week_start, metric_key) DO UPDATE SET value=excluded.value, uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at""",
            (login, week_start, metric_key, value, "auto-snapshot", now),
        )

    for r in reviewees:
        login = r["login"]
        full = _compute_ld_metrics_full(login)
        for key, entry in full.items():
            upsert(login, key, entry["pct_ok"])
        xt = full.get("cross_training", {})
        upsert(login, "xt_proficient", xt.get("proficient"))
        upsert(login, "xt_refresh", xt.get("refresh"))
        upsert(login, "xt_practice", xt.get("practice"))
        upsert(login, "xt_lapsed", xt.get("lapsed"))
        upsert(login, "ir_gap", full.get("indirect_roles", {}).get("gap"))
        upsert(login, "ambassador_gap", full.get("instructor_mgmt", {}).get("gap"))
        safety_detail = _safety_overdue_detail_for_login(login)
        upsert(login, "safety_avg_overdue_days", safety_detail["avg_overdue_days"])
        my_ranks = ranks_by_login.get(login, {})
        for key, label, _sections in LD_METRICS:
            upsert(login, f"{key}_rank", my_ranks.get(key))
        upsert(login, "total_rank", my_ranks.get("total"))
    conn.commit()
    conn.close()


MANAGER_RANKING_METRICS = ["safety_compliance", "de_tech", "indirect_roles", "cross_training"]
SAFETY_COMPLIANCE_TARGET = 95


def get_manager_compliance_rankings():
    """Every AM, OM, and SOM — anyone with people (associates or other
    managers) reporting to them — ranked best-to-worst on each of the
    four requested metrics. Reuses the same per-login metric computation
    as OLR, so an OM/SOM's score is correctly rolled up across their
    whole descendant tree, not just their own literal login."""
    managers = db.get_roster_by_roles(["am", "om", "som"])
    people = []
    for m in managers:
        metrics = _compute_olr_metrics_for_login(m["login"])
        people.append({
            "login": m["login"], "full_name": m["full_name"], "role": m["role"], "title": m["title"],
            "metrics": metrics,
        })
    rankings = {}
    for key in MANAGER_RANKING_METRICS:
        scored = [p for p in people if p["metrics"].get(key) is not None]
        scored.sort(key=lambda p: p["metrics"][key], reverse=True)
        unscored = [p for p in people if p["metrics"].get(key) is None]
        rankings[key] = {"scored": scored, "unscored": unscored}
    return rankings


def _is_critical_safety_item(item):
    """Return whether an overdue Safety record is explicitly critical."""
    notes = (item.get("notes") or "").lower()
    topic = (item.get("subcategory") or "").lower()
    return (
        "priority: critical" in notes
        or "priority: high" in notes
        or "critical" in topic
    )


def _metric_trend_4_weeks(login, current_score, metric_key):
    """Return the current score for one metric_key (or 'total' for the
    composite across whichever metrics are present) versus the nearest
    4-week-old snapshot."""
    if current_score is None:
        return None
    since = (date.today() - timedelta(days=42)).isoformat()
    series = db.get_olr_weekly_series(login, since=since)
    history = []
    for week_start, values in series.items():
        value = db._olr_composite(values) if metric_key == "total" else values.get(metric_key)
        if value is not None:
            history.append((week_start, float(value)))
    if not history:
        return None
    history.sort(key=lambda row: row[0])
    target_day = (date.today() - timedelta(days=28)).isoformat()
    older = [row for row in history if row[0] <= target_day]
    baseline = older[-1][1] if older else history[0][1]
    change = round(float(current_score) - baseline, 1)
    return 0.0 if change == -0.0 else change


def _safety_trend_4_weeks(login, current_score):
    """Return the current Safety score versus the nearest 4-week snapshot."""
    return _metric_trend_4_weeks(login, current_score, "safety_compliance")


def _compute_ld_metrics_full(login):
    """Like _compute_olr_metrics_for_login, but keeps the gap/risk counts
    too, not just pct_ok — used by the generic per-metric ranking so each
    metric's ranking table can show a meaningful 'gap' count without
    recomputing _build_scorecard_categories a second time. Same
    single_am_login=login rule as _compute_olr_metrics_for_login — see
    that docstring for why it's never gated on descendants.

    Cross-Training's 'gap' is deliberately NOT the generic card's value
    (which is just a raw Lapsed-record count, including records for
    processes never defined as a target for this person's department at
    all — misleading, since it made gaps look meaningful for training
    nobody was ever supposed to be staffing). It's overridden here with
    get_xt_gap_and_retention_for_login's staffing-shortfall definition,
    scoped strictly to defined cross-training targets (external
    standards + internal targets) for this person's own department(s).
    retention_pct — the % of the trained-on-a-defined-path population
    that's still current, not Lapsed — is added alongside it as a
    genuinely separate readout, not a restatement of the gap count."""
    scope = _resolve_am_scope(login)
    categories, _ = _build_scorecard_categories(scope, None, single_am_login=login)
    out = {}
    for c in categories:
        entry = {"pct_ok": c["card"]["pct_ok"], "gap": c["card"]["gap"], "risk": c["card"]["risk"]}
        if c["key"] == "cross_training":
            entry["proficient"] = c["card"].get("proficient")
            entry["refresh"] = c["card"].get("refresh")
            entry["practice"] = c["card"].get("practice")
            entry["lapsed"] = c["card"].get("lapsed")
            xt_gap_retention = get_xt_gap_and_retention_for_login(login)
            entry["gap"] = xt_gap_retention["gap"]
            entry["retention_pct"] = xt_gap_retention["retention_pct"]
        out[c["key"]] = entry
    return out


LD_METRICS = db.SCORECARD_CATEGORIES  # [(key, label, sections), ...] — the 7 tracked L&D metrics


def get_ld_metric_ranking(metric_key, full_by_login=None):
    """Generalized version of get_safety_training_compliance_ranking —
    ranks every AM/OM/SOM on one L&D metric (or 'total', the composite
    across whichever of the 7 they have a score for), same competition-
    ranking + report-count tie-break, and a metric-appropriate gap count
    instead of Safety's specific critical/overdue columns. metric_key
    must be one of the 7 SCORECARD_CATEGORIES keys, or 'total'.
    Pass full_by_login (from get_all_ld_metric_rankings) to reuse an
    already-computed pass across managers rather than recomputing it —
    building all 8 rankings independently would otherwise redo the same
    expensive per-manager computation 8 times over."""
    managers = db.get_roster_by_roles(["am", "om", "som"])
    rows = []
    for manager in managers:
        full = full_by_login[manager["login"]] if full_by_login is not None else _compute_ld_metrics_full(manager["login"])
        if metric_key == "total":
            pct_values = {k: v["pct_ok"] for k, v in full.items()}
            score = db._olr_composite(pct_values)
            gap = sum(v["gap"] for v in full.values())
            retention_pct = None
        else:
            entry = full.get(metric_key)
            score = entry["pct_ok"] if entry else None
            gap = entry["gap"] if entry else 0
            retention_pct = entry.get("retention_pct") if entry else None

        department = db.normalize_department(manager.get("department")) if manager.get("department") else None
        if not department:
            assigned = db.get_assigned_departments(manager["login"])
            if len(assigned) == 1:
                department = assigned[0]
            elif len(assigned) > 1:
                department = "Multiple departments"
        if not department:
            department = "L&D" if "learning" in (manager.get("title") or "").lower() else "Operations"

        rows.append({
            "login": manager["login"],
            "full_name": manager.get("full_name") or manager["login"],
            "role": manager["role"],
            "role_label": ROLES.get(manager["role"], manager["role"]),
            "department": department,
            "score": round(score, 1) if score is not None else None,
            "gap": gap,
            "retention_pct": retention_pct,
            "trend": _metric_trend_4_weeks(manager["login"], score, metric_key),
            "status": "on_target" if score is not None and score >= SAFETY_COMPLIANCE_TARGET else "needs_attention",
            "report_count": len(db.get_descendant_ams(manager["login"])),
        })

    scored = [row for row in rows if row["score"] is not None]
    scored.sort(key=lambda row: (-row["score"], -row["report_count"], row["gap"], row["full_name"].lower()))
    last_score = None
    current_rank = 0
    for position, row in enumerate(scored, start=1):
        if last_score is None or row["score"] != last_score:
            current_rank = position
            last_score = row["score"]
        row["rank"] = current_rank

    unscored = [row for row in rows if row["score"] is None]
    unscored.sort(key=lambda row: row["full_name"].lower())
    for row in unscored:
        row["rank"] = None
    rows = scored + unscored

    top_score = scored[0]["score"] if scored else None
    top_tie_count = sum(1 for row in scored if row["score"] == top_score) if top_score is not None else 0
    on_target_count = sum(1 for row in rows if row["score"] is not None and row["score"] >= SAFETY_COMPLIANCE_TARGET)

    return {
        "metric_key": metric_key,
        "rows": rows,
        "manager_count": len(rows),
        "scored_count": len(scored),
        "on_target_count": on_target_count,
        "attention_count": len(rows) - on_target_count,
        "top_score": top_score,
        "top_tie_count": top_tie_count,
    }


def get_all_ld_metric_rankings():
    """All 7 metric rankings plus 'total', computed in a single pass over
    managers (each manager's underlying scorecard is computed once, not
    once per metric) — this is the efficient entry point; prefer it over
    calling get_ld_metric_ranking in a loop."""
    managers = db.get_roster_by_roles(["am", "om", "som"])
    full_by_login = {m["login"]: _compute_ld_metrics_full(m["login"]) for m in managers}
    result = {key: get_ld_metric_ranking(key, full_by_login=full_by_login) for key, _, _ in LD_METRICS}
    result["total"] = get_ld_metric_ranking("total", full_by_login=full_by_login)
    return result


def get_safety_training_compliance_ranking():
    """Build the decision-focused Safety ranking for every AM, OM, and SOM.

    Compliance remains the primary score. Equal scores share a competition
    rank, while critical overdue requirements and accumulated overdue days
    provide a separate operational action priority.
    """
    managers = db.get_roster_by_roles(["am", "om", "som"])
    rows = []

    for manager in managers:
        scope = _resolve_am_scope(manager["login"])
        items = db.get_items(section="compliance_safety", am_login=scope)
        total = len(items)
        compliant = sum(1 for item in items if item.get("status") in db.STATUS_OK)
        score = round(100 * compliant / total, 1) if total else None

        overdue_items = [
            item for item in items
            if item.get("status") in db.STATUS_GAP
            and _days_overdue(item.get("due_date")) is not None
            and _days_overdue(item.get("due_date")) > 0
        ]
        critical_overdue = sum(1 for item in overdue_items if _is_critical_safety_item(item))
        overdue_days = sum(_days_overdue(item.get("due_date")) or 0 for item in overdue_items)

        department = db.normalize_department(manager.get("department")) if manager.get("department") else None
        if not department:
            assigned = db.get_assigned_departments(manager["login"])
            if len(assigned) == 1:
                department = assigned[0]
            elif len(assigned) > 1:
                department = "Multiple departments"
        if not department:
            department = "L&D" if "learning" in (manager.get("title") or "").lower() else "Operations"

        rows.append({
            "login": manager["login"],
            "full_name": manager.get("full_name") or manager["login"],
            "role": manager["role"],
            "role_label": ROLES.get(manager["role"], manager["role"]),
            "department": department,
            "score": score,
            "critical_overdue": critical_overdue,
            "overdue_days": overdue_days,
            "trend": _safety_trend_4_weeks(manager["login"], score),
            "status": "on_target" if score is not None and score >= SAFETY_COMPLIANCE_TARGET else "needs_attention",
            "record_count": total,
            "report_count": len(db.get_descendant_ams(manager["login"])),
        })

    scored = [row for row in rows if row["score"] is not None]
    scored.sort(key=lambda row: (
        -row["score"], -row["report_count"], row["critical_overdue"], row["overdue_days"], row["full_name"].lower()
    ))
    last_score = None
    current_rank = 0
    for position, row in enumerate(scored, start=1):
        if last_score is None or row["score"] != last_score:
            current_rank = position
            last_score = row["score"]
        row["rank"] = current_rank

    unscored = [row for row in rows if row["score"] is None]
    unscored.sort(key=lambda row: row["full_name"].lower())
    for row in unscored:
        row["rank"] = None
    rows = scored + unscored

    site_items = db.get_items(section="compliance_safety")
    site_total = len(site_items)
    site_compliant = sum(1 for item in site_items if item.get("status") in db.STATUS_OK)
    overall_score = round(100 * site_compliant / site_total, 1) if site_total else None
    site_overdue = [
        item for item in site_items
        if item.get("status") in db.STATUS_GAP
        and _days_overdue(item.get("due_date")) is not None
        and _days_overdue(item.get("due_date")) > 0
    ]

    top_score = scored[0]["score"] if scored else None
    top_tie_count = sum(1 for row in scored if row["score"] == top_score) if top_score is not None else 0
    on_target_count = sum(
        1 for row in rows if row["score"] is not None and row["score"] >= SAFETY_COMPLIANCE_TARGET
    )

    return {
        "target": SAFETY_COMPLIANCE_TARGET,
        "rows": rows,
        "manager_count": len(rows),
        "scored_count": len(scored),
        "on_target_count": on_target_count,
        "attention_count": len(rows) - on_target_count,
        "overall_score": overall_score,
        "critical_overdue": sum(1 for item in site_overdue if _is_critical_safety_item(item)),
        "overdue_days": sum(_days_overdue(item.get("due_date")) or 0 for item in site_overdue),
        "top_score": top_score,
        "top_tie_count": top_tie_count,
        "roles": sorted({row["role"] for row in rows}),
        "departments": sorted({row["department"] for row in rows}),
    }


def _identity_from_request():
    """Reads and verifies the ALB-injected claims. Returns an identity
    dict, or None if the app is reached directly without going through the
    authenticated listener (e.g. local dev).

    Claim priority matters here: Cognito's own 'sub' is always an opaque
    UUID (never the human username), so it's checked last — the readable
    username lives in 'cognito:username' for Cognito, or in 'sub' itself
    for Federate/Midway (which puts the alias there directly)."""
    header = request.headers.get("x-amzn-oidc-data")
    claims = oidc_auth.decode_oidc_header(header)
    if not claims:
        return None
    sub = claims.get("sub")
    # Cognito's 'sub' is always a UUID; Federate/Midway puts the readable
    # alias directly in 'sub'. Use the shape to tell them apart instead of
    # guessing by claim presence alone.
    sub_is_uuid = bool(sub) and bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sub, re.IGNORECASE))

    login = (
        claims.get("cognito:username")
        or claims.get("preferred_username")
        or claims.get("username")
        or claims.get("login")
        or (sub if sub and not sub_is_uuid else None)
    )
    if not login and claims.get("email"):
        # ALB is a generic OIDC client and doesn't reliably forward Cognito's
        # proprietary 'cognito:username' claim even when present — 'email'
        # is the one claim that's virtually guaranteed once the 'email'
        # scope is requested, so fall back to its local-part as the login.
        login = claims["email"].split("@")[0]
    login = login or sub or "unknown"
    # Every login already in the system — from CSV uploads, from
    # user_roles, from every tracked_items/xt_hours row — is lowercase,
    # because every real export this app has ever ingested already comes
    # that way. Nothing else in this function guarantees that Federate's
    # 'sub' or 'email' claim will be, though, and a case mismatch here
    # wouldn't fail loudly — the person would just authenticate
    # successfully and land with no department, no shift, and no
    # visible history, because none of it would match their differently-
    # cased login. Normalizing once, here, keeps it matching regardless
    # of how the IdP happens to case it.
    login = login.lower()

    # cognito:groups arrives as a JSON list normally; guard against a
    # comma-separated string variant too, but split only on commas — a
    # group name can legitimately contain spaces ("Operations Manager").
    cognito_groups = claims.get("cognito:groups")
    if isinstance(cognito_groups, str):
        cognito_groups = [g.strip() for g in cognito_groups.split(",") if g.strip()]

    # Federate's "Group mapper" claim type (configured directly in the
    # Federate console under User Identity Custom Claims, not something
    # this app controls) resolves LDAP group membership to ONE value
    # server-side — e.g. a member of scn2-area gets the literal string
    # "area manager" — rather than forwarding the raw list of LDAP group
    # names the way cognito:groups does. Its configured default for
    # "not a member of any mapped group" is the string "0", which is a
    # real value here (not empty/missing) and must NOT be treated as a
    # role name.
    ldap_role_claim = claims.get("LDAP") or claims.get("LDAP Group") or claims.get("ldap_groups")
    ldap_groups = []
    if isinstance(ldap_role_claim, str):
        if ldap_role_claim.strip() not in ("", "0"):
            ldap_groups = [ldap_role_claim.strip()]
    elif isinstance(ldap_role_claim, list):
        ldap_groups = [g for g in ldap_role_claim if g and str(g).strip() not in ("", "0")]

    return {
        "login": login,
        "job_title": claims.get("job title") or claims.get("jobTitle"),
        "ldap_groups": ldap_groups,
        "location": claims.get("Location") or claims.get("location"),
        "role": _resolve_role(login, groups=(cognito_groups or []) + ldap_groups),
    }


_db_initialized_sites = set()
_global_db_initialized = False


@application.before_request
def open_db_scope():
    """Opens this request's database scope before anything touches the
    database, so every get_db() call for the rest of the request shares
    one connection instead of opening its own. Registered before
    establish_site() below, which itself queries the site registry."""
    db.begin_request_scope()


@application.teardown_request
def close_db_scope(exc=None):
    """Releases whatever this request opened. teardown_request runs even
    when the view raised, which is what keeps a failing request from
    leaking a pooled connection the way the pool-exhaustion incident
    did."""
    db.end_request_scope()


@application.before_request
def establish_site():
    """Sets which site's database this request's queries should use —
    registered before ensure_db() below so the site is already known
    by the time anything touches the database."""
    global _global_db_initialized
    if not _global_db_initialized:
        db.init_global_db()
        _global_db_initialized = True
    site_code = session.get("site") or db.DEFAULT_SITE_CODE
    # Guards against a stale session pointing at a site that's since
    # been removed from the registry — shouldn't normally happen (there's
    # no "delete site" feature), but a stale session cookie shouldn't be
    # able to point at a schema/file that no longer has a registry entry.
    if site_code != db.DEFAULT_SITE_CODE and not db.get_site(site_code):
        site_code = db.DEFAULT_SITE_CODE
        session["site"] = site_code
    db.set_current_site(site_code)


@application.before_request
def ensure_db():
    site_code = db.get_current_site()
    if site_code in _db_initialized_sites:
        return
    # Always run init_db(), even when the file/schema already exists —
    # it's written to be safe to re-run (CREATE TABLE IF NOT EXISTS plus
    # explicit, idempotent migrations), and skipping it for an existing
    # file meant an older local database missing a newer table or
    # column (added by this app since that file was first created)
    # would never get it, and every request would 500 with
    # "no such table" instead. The _db_initialized_sites guard above
    # already prevents this from re-running more than once per site per
    # worker, so there's no real startup-time cost to removing the
    # file-exists shortcut.
    db.init_db()
    if site_code == db.DEFAULT_SITE_CODE:
        _seed_if_empty()
    _db_initialized_sites.add(site_code)


@application.before_request
def establish_identity():
    """Reads the ALB-injected, Federate-signed claims on every request and
    refreshes the session from them. In production, behind the authenticated
    HTTPS:443 listener, this header is always present. It's absent for local
    dev (running application.py directly with no ALB in front) — the manual
    login form on / is the fallback for that case only."""
    identity = _identity_from_request()
    if identity:
        session["login"] = identity["login"]
        session["role"] = identity["role"]
        session["job_title"] = identity["job_title"]
        session["ldap_groups"] = identity["ldap_groups"]
        session["location"] = identity["location"]
        session["auth_source"] = "federate"
        if identity["role"] and not session.get("_activation_checked"):
            db.mark_login_activated(identity["login"])
            session["_activation_checked"] = True


def _seed_if_empty():
    """First-boot seed so the page isn't empty before real data is loaded.
    Pulled from the concept paper's own structure, not invented numbers —
    this is clearly a starting scaffold, not live data, and every number
    on every page is computed from whatever's actually in the database."""
    conn = db.get_db()
    n = conn.execute("SELECT COUNT(*) c FROM ops_structure").fetchone()["c"]
    conn.close()
    if n:
        return
    db.add_ops_role("Senior Operations Manager", None, None, "Owns the L&D page and escalation review", 0)
    db.add_ops_role("L&D Operations Lead", None, "Senior Operations Manager", "Maintains the page, scorecards and Asana integration", 1)
    db.add_ops_role("Trainer", None, "L&D Operations Lead", "Maintains Planning/Training/Staffing data for their FC", 2)
    db.add_ops_role("Area Manager", None, "L&D Operations Lead", "Consumes the AM Overview for their area", 3)


def _require_login():
    return session.get("login")


def _login_context():
    # Manual/dev logins re-resolve live so a role change made in Settings
    # shows up without re-logging in; Federate identities already do this
    # on every request inside establish_identity().
    if session.get("login") and session.get("auth_source") == "manual":
        session["role"] = _resolve_role(session["login"])
    effective = _effective_role()
    return {
        "login": session.get("login"),
        "role": session.get("role"),
        "role_label": ROLES.get(session.get("role"), "No role assigned"),
        "effective_role": effective,
        "effective_role_label": ROLES.get(effective, "No role assigned"),
        "is_admin_tier": session.get("role") in ADMIN_TIER_ROLES,
        "is_pending": session.get("role") is None,
        "view_as": session.get("view_as"),
        "all_roles": ROLES,
        "can_admin": effective in ADMIN_TIER_ROLES,
        "can_ops_manage": effective in OPS_MANAGER_ROLES,
        "can_access_core": effective is not None,
        "job_title": session.get("job_title"),
        "location": session.get("location"),
        "auth_source": session.get("auth_source"),
        "current_site": db.get_current_site(),
        "all_sites": db.get_sites(),
        "is_global_admin": db.is_global_admin(session.get("login")) or session.get("login") in BOOTSTRAP_ADMIN_LOGINS,
    }


# ---------------------------------------------------------------- login ----

@application.route("/", methods=["GET", "POST"])
def login():
    # establish_identity() already ran as a before_request hook — if Federate
    # claims were present, the person is already signed in and belongs on
    # the overview page, not a login form.
    if session.get("login") and session.get("auth_source") == "federate":
        return redirect(url_for("overview"))

    # Fallback path: only reachable when there's no Federate identity, i.e.
    # local development without an ALB in front. Not used in production.
    # The role dropdown here only takes effect the *first* time a given
    # login is seen — same as production, role changes after that only
    # come from L&D Settings, not by picking a different option here.
    if request.method == "POST":
        login_id = request.form.get("login_id", "").strip().lower()
        chosen = request.form.get("role") or None
        if not login_id:
            flash("Enter your login to continue.", "warning")
            return redirect(url_for("login"))
        if _resolve_role(login_id) is None and chosen in ROLES:
            db.set_user_role(login_id, chosen, assigned_by="dev-login")
        session["login"] = login_id
        session["role"] = _resolve_role(login_id)
        session["auth_source"] = "manual"
        if session["role"]:
            db.mark_login_activated(login_id)
        return redirect(url_for("overview") if session["role"] else url_for("scorecards"))

    freshness = db.data_freshness()
    return render_template("login.html", **_login_context(), roles=ROLES, freshness=freshness)


@application.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@application.route("/debug/claims")
def debug_claims():
    """Dumps exactly what the ALB is (and isn't) forwarding for the current
    request — deliberately open to any signed-in user (even pending/no-role
    ones), not just admins, because it shows only their own claims and
    pending users are exactly who needs this to debug why no role landed."""
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))

    header = request.headers.get("x-amzn-oidc-data")
    if not header:
        return jsonify({
            "note": "No x-amzn-oidc-data header on this request — you're on "
                    "the manual/local-dev login path, not going through the "
                    "ALB's Cognito listener. This only shows real data when "
                    "hit through the actual authenticated ALB.",
            "session_login": session.get("login"),
            "session_role": session.get("role"),
        })
    claims = oidc_auth.decode_oidc_header(header)
    if claims is None:
        return jsonify({"note": "Header present but failed to decode/verify — see server logs."})
    return jsonify({
        "note": "Raw claims ALB actually forwarded for this request.",
        "claims": claims,
        "resolved_login": session.get("login"),
        "resolved_role": session.get("role"),
    })


@application.route("/request-role", methods=["POST"])
def request_role_route():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    desired = request.form.get("desired_role")
    if desired not in ROLES:
        flash("Pick a role to request.", "warning")
        return redirect(request.referrer or url_for("scorecards"))
    db.request_role(login_id, desired)
    flash(f"Requested '{ROLES[desired]}' — an admin will review it.", "success")
    return redirect(request.referrer or url_for("scorecards"))


@application.route("/view-as", methods=["POST"])
def view_as():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if session.get("role") not in ADMIN_TIER_ROLES:
        return _deny("Only admin-tier roles can preview as another role.")
    choice = request.form.get("role") or ""
    if choice and choice not in ROLES:
        flash("Unknown role.", "warning")
    elif choice:
        session["view_as"] = choice
        flash(f"Viewing as {ROLES[choice]}. This is just a preview — your actual role hasn't changed.", "info")
    else:
        session.pop("view_as", None)
        flash("Back to your own role.", "info")
    return redirect(request.referrer or url_for("overview"))


# ------------------------------------------------------------- overview ----

@application.route("/overview")
def overview():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))

    role = _effective_role()
    if role is None:
        return _deny("Request a role to access AM Overview — you can still use Reporting and the Weekly training plan.")

    base_login = login_id if role in OPS_SELF_ROLES else request.args.get("am")
    if role in OPS_MANAGER_ROLES and not base_login:
        # Default an OM/SOM to their own org tree once it's configured
        # (reports_to set up in L&D Settings), so their numbers reflect
        # their whole chain without having to pick an AM first. Falls
        # back to the pre-existing org-wide view if nothing's configured
        # yet, so nothing breaks for anyone who hasn't set it up.
        base_login = login_id if db.get_descendant_ams(login_id) else None
    query_am_scope = _resolve_am_scope(base_login) if base_login else None
    fc_scope = request.args.get("fc")

    planning = {s: db.scorecard([s], am_login=query_am_scope, fc=fc_scope) for s in db.PLANNING_SECTIONS}
    staffing = {s: db.scorecard([s], am_login=query_am_scope, fc=fc_scope) for s in db.STAFFING_SECTIONS}

    # Planning tab is now a week calendar of who's actually scheduled into
    # what — org-wide if nobody's picked a specific AM (OM/SOM/admin).
    planning_week = db.week_start_for(request.args.get("pweek"))
    planning_calendar = db.get_planned_calendar(planning_week, am_login=query_am_scope)

    # My Trainings — this person's own training/compliance record (an AM or
    # Team Lead's personal requirements), not their team's — always the one
    # base login, never the broadened team scope, even if they have
    # descendants reporting to them.
    my_trainings = db.get_my_trainings(base_login, fc=fc_scope) if base_login else []

    # My L&D Scorecard — 6 fixed categories, 0-100 compliance score each.
    scorecard_categories, overall_score = _build_scorecard_categories(query_am_scope, fc_scope, single_am_login=base_login)

    tab = request.args.get("tab", "myscorecard")

    # Compliance gaps + this week's training plan, for the drag-to-plan panel.
    current_week = db.week_start_for()
    week_dates = db.week_dates(current_week)
    recommended = db.get_recommended_for_week(current_week, am_login=query_am_scope, fc=fc_scope, top_n=10)
    recommended_keys = {(r["employee_login"], (r.get("subcategory") or "").strip().lower()) for r in recommended}
    gaps = db.get_compliance_gaps(am_login=query_am_scope, fc=fc_scope, limit=30, week_start=current_week)
    for g in gaps:
        g["recommended"] = (g["employee_login"], (g.get("subcategory") or "").strip().lower()) in recommended_keys

    shift_slots_all = {s: db.get_training_slots(current_week, shift=s) for s in db.SHIFTS}
    # A specific AM's own shift narrows the training plan to just that
    # shift — no single "their shift" to filter to for an org-wide view
    # with no AM picked, so that still shows all three.
    base_shift, _base_department = db.get_user_shift_and_department(base_login) if base_login else (None, None)
    if base_shift and base_shift in shift_slots_all:
        shift_slots = {base_shift: shift_slots_all[base_shift]}
        training_plan_shifts = {base_shift: db.SHIFTS[base_shift]}
    else:
        shift_slots = shift_slots_all
        training_plan_shifts = db.SHIFTS

    my_safety_rank = None
    my_ld_rankings = None
    if base_login and db.get_user_role(base_login) in ("am", "om", "som"):
        for row in get_safety_training_compliance_ranking()["rows"]:
            if row["login"] == base_login:
                my_safety_rank = row
                break
        all_rankings = get_all_ld_metric_rankings()
        my_ld_rankings = {}
        for key, label, _sections in LD_METRICS + [("total", "Total L&D Score", [])]:
            for row in all_rankings[key]["rows"]:
                if row["login"] == base_login:
                    my_ld_rankings[key] = {"label": label, **row}
                    break

    olr_data = None
    if tab == "olr" and base_login:
        olr_data = _build_olr_tab_data(base_login)

    return render_template(
        "overview.html",
        **_login_context(),
        my_safety_rank=my_safety_rank,
        my_ld_rankings=my_ld_rankings,
        olr_data=olr_data,
        training_plan_shifts=training_plan_shifts,
        section_labels=db.SECTIONS,
        planning=planning,
        planning_calendar=planning_calendar,
        planning_week=planning_week,
        planning_prev_week=db.adjacent_week(planning_week, -1),
        planning_next_week=db.adjacent_week(planning_week, 1),
        planning_this_week=db.week_start_for(),
        planning_week_dates=db.week_dates(planning_week),
        my_trainings=my_trainings,
        staffing=staffing,
        tab=tab,
        ams=db.all_ams(),
        fcs=db.all_fcs(),
        am_scope=base_login,
        fc_scope=fc_scope,
        my_escalations=db.get_my_escalations(base_login or login_id),
        compliance_gaps=gaps,
        current_week=current_week,
        week_dates=week_dates,
        day_names=db.DAY_NAMES,
        shifts=db.SHIFTS,
        shift_slots=shift_slots,
        scorecard_categories=scorecard_categories,
        overall_score=overall_score,
    )


@application.route("/api/category-detail/<key>")
def api_category_detail(key):
    login_id = _require_login()
    if not login_id:
        return "", 401
    if key not in db.SCORECARD_CATEGORY_MAP:
        return "", 404

    role = _effective_role()
    if role is None:
        return "", 403
    fc_scope = request.args.get("fc")
    trainer_login = request.args.get("trainer")

    if trainer_login:
        am_filter = db.get_ams_for_trainer(trainer_login)  # already the full assigned-AM scope; no further broadening
        single_am = trainer_login
    elif role in OPS_SELF_ROLES:
        am_filter = _resolve_am_scope(login_id)
        single_am = login_id
    else:
        picked = request.args.get("am")
        if not picked and role in OPS_MANAGER_ROLES:
            picked = login_id if db.get_descendant_ams(login_id) else None
        am_filter = _resolve_am_scope(picked) if picked else None
        single_am = picked

    if key == "indirect_roles" and single_am:
        ir_card = _indirect_roles_card_for_am(single_am)
        if ir_card:
            return render_template("indirect_roles_readiness_fragment.html", card=ir_card)

    if key == "instructor_mgmt" and single_am:
        amb_card = _ambassador_scorecard_for_am(single_am)
        if amb_card:
            roster = []
            for department, shift in amb_card["pairs"]:
                ambassadors = db.get_ambassadors(shift, department=department)
                for a in ambassadors.get(department, []):
                    a["attendance"] = db.get_ambassador_attendance_summary(a["id"])
                    a["department"] = department
                    roster.append(a)
            return render_template(
                "ambassador_availability_fragment.html",
                card=amb_card, ambassadors=roster,
            )

    if key == "ambassador_readiness" and single_am:
        readiness_card = _ambassador_readiness_card_for_am(single_am)
        if readiness_card:
            ready, not_ready = [], []
            for department, shift in readiness_card["pairs"]:
                summary = db.get_ambassador_readiness_summary(department, shift)
                for a in summary["ready"]:
                    a["department"] = department
                ready.extend(summary["ready"])
                for a in summary["not_ready"]:
                    a["department"] = department
                not_ready.extend(summary["not_ready"])
            return render_template(
                "ambassador_readiness_fragment.html",
                card=readiness_card, ready=ready, not_ready=not_ready,
            )

    label, sections = db.SCORECARD_CATEGORY_MAP[key]
    xt_records = None
    xt_refresh_at_risk = None
    xt_am_standards = None
    xt_am_department = None
    if key == "cross_training":
        xt_records = db.get_xt_proficiency_records(am_login=am_filter, fc=fc_scope)
        xt_refresh_at_risk = [
            r for r in xt_records
            if r["proficiency_status"] == "Refresh" and r["days_until_expiry"] is not None and r["days_until_expiry"] <= 60
        ]
        if single_am:
            xt_am_pairs = _department_shift_pairs_for_login(single_am)
            xt_am_departments = sorted({d for d, _ in xt_am_pairs})
            if xt_am_departments:
                xt_am_department = xt_am_departments[0] if len(xt_am_departments) == 1 else ", ".join(xt_am_departments)
                xt_am_standards = db.get_xt_standards_for_departments(xt_am_departments)
        xt = db.get_xt_compliance_score(am_login=am_filter, fc=fc_scope)
        items = []
        card = {
            "ok": xt["proficient"], "risk": xt["refresh"] + xt["practice"], "gap": xt["lapsed"],
            "total": xt["total"], "pct_ok": xt["score"],
            "proficient": xt["proficient"], "refresh": xt["refresh"],
            "practice": xt["practice"], "lapsed": xt["lapsed"],
        }
    else:
        items = db.get_items(section=sections, am_login=am_filter, fc=fc_scope)
        items.sort(key=lambda r: (r.get("due_date") or "9999-99-99"))
        card = db.scorecard(sections, am_login=am_filter, fc=fc_scope)

    # Safety Compliance and DE Tech are the two categories that get the
    # full org-tree treatment: every record from direct AND indirect
    # reports, each one labeled so a manager overseeing several AMs can
    # see exactly whose team a given associate actually belongs to, plus
    # a breakdown of which manager under them is driving the number down.
    am_breakdown = None
    if key in ("safety_compliance", "de_tech", "bts_compliance") and isinstance(am_filter, (list, tuple)) and len(am_filter) > 1:
        for it in items:
            it["report_type"] = "Direct" if it.get("am_login") == am_filter[0] else "Indirect"
        am_breakdown = db.get_am_breakdown_for_scope(sections, am_filter, fc=fc_scope)

    # Safety Compliance is the one category L&D actually schedules centrally
    # (plus the two DE Tech exceptions) — so its drill-down also shows this
    # week's plan above the records, letting you plan straight from here.
    plan_week = shift_slots = None
    if key == "safety_compliance":
        plan_week = db.week_start_for()
        shift_slots = {s: db.get_training_slots(plan_week, shift=s) for s in db.SHIFTS}
        db.attach_planned_state(items, plan_week)

    return render_template(
        "category_detail_fragment.html",
        category_label=label,
        category_key=key,
        items=items,
        card=card,
        am_breakdown=am_breakdown,
        xt_records=xt_records,
        xt_refresh_at_risk=xt_refresh_at_risk,
        xt_am_standards=xt_am_standards,
        xt_am_department=xt_am_department,
        xt_drop_days=30,
        xt_proficiency_expiry_days=db.get_xt_proficiency_expiry_days() if key == "cross_training" else None,
        plan_week=plan_week,
        shift_slots=shift_slots,
        shifts=db.SHIFTS,
        day_names=db.DAY_NAMES,
        week_dates=db.week_dates(plan_week) if plan_week else None,
    )


@application.route("/ld-management/trainer-overview")
def trainer_overview():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = session.get("role")
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Trainer overview is an L&D tool — you don't have access to it.")

    trainers = db.get_trainers()
    trainer_login = request.args.get("trainer") or (login_id if role == "trainer" else None)
    fc_scope = request.args.get("fc")
    tab = request.args.get("tab", "updates")

    ams = db.get_ams_for_trainer(trainer_login) if trainer_login else []

    scorecard_categories, overall_score = _build_scorecard_categories(ams, fc_scope, single_am_login=trainer_login)

    week_start = db.week_start_for(request.args.get("week"))
    recommendations = db.get_weekly_training_recommendations(week_start, fc=fc_scope)

    updates = None
    if tab == "updates":
        db.ensure_due_today_reminders()
        updates = db.get_all_escalation_updates()

    safety_records = None
    if tab == "safety_compliance":
        safety_records = db.get_items(section="compliance_safety", fc=fc_scope)
        safety_records.sort(key=lambda r: (r.get("due_date") or "9999-99-99"))
        esc_by_item = {e["tracked_item_id"]: e for e in db.get_all_open_record_escalations()}
        for r in safety_records:
            r["escalation"] = esc_by_item.get(r["id"])

    selected_categories = []
    category_records = {}
    assignable_managers = []
    if tab == "log_escalation":
        selected_categories = request.args.getlist("categories")
        for cat_key, _label, section in db.ESCALATION_TICKET_CATEGORIES:
            if cat_key in selected_categories and section:
                records = db.get_items(section=section)
                for r in records:
                    r["days_overdue"] = _days_overdue(r.get("due_date"))
                category_records[cat_key] = records
        assignable_managers = db.get_escalation_assignable_managers()

    return render_template(
        "trainer_overview.html",
        **_login_context(),
        trainers=trainers,
        trainer_login=trainer_login,
        assigned_ams=ams,
        fcs=db.all_fcs(),
        fc_scope=fc_scope,
        scorecard_categories=scorecard_categories,
        overall_score=overall_score,
        tab=tab,
        shifts=db.SHIFTS,
        week_start=week_start,
        prev_week=db.adjacent_week(week_start, -1),
        next_week=db.adjacent_week(week_start, 1),
        this_week=db.week_start_for(),
        recommendations=recommendations,
        safety_records=safety_records,
        escalation_categories=db.ESCALATION_TICKET_CATEGORIES,
        selected_categories=selected_categories,
        category_records=category_records,
        assignable_managers=assignable_managers,
        updates=updates,
    )


@application.route("/ld-management/trainer-overview/updates/<int:comment_id>/reviewed", methods=["POST"])
def trainer_overview_mark_update_reviewed(comment_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only trainers can review updates.")
    db.mark_escalation_comment_reviewed(comment_id, reviewed_by=login_id)
    return redirect(url_for("trainer_overview", tab="updates"))


def _sync_ticket_to_tickety(ticket_number, categories, description, concerning_manager):
    """Mirrors a newly-created escalation ticket into Tickety, best-
    effort — a Tickety-side failure (client unavailable, API error,
    account not onboarded, etc.) never blocks or unwinds the app's own
    ticket, it's just recorded on the ticket for visibility. The
    category/type/item values below are placeholders — they need to
    match your organization's actual Tickety categorization taxonomy,
    not whatever's guessed here."""
    label = ", ".join(db.ESCALATION_TICKET_CATEGORY_MAP.get(c, (c, None))[0] for c in categories)
    title = f"L&D Escalation {ticket_number}: {label} — {concerning_manager}"
    tickety_id, error = tickety_client.create_tickety_ticket(
        title=title, description=description or "",
        category="Learning and Development", type_="Escalation", item=label,
    )
    db.set_escalation_tickety_sync(ticket_number, tickety_id, error)
    return tickety_id, error


@application.route("/ld-management/trainer-overview/log-escalation", methods=["POST"])
def trainer_overview_log_escalation():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can log escalations.")

    categories = request.form.getlist("categories")
    concerning_manager = (request.form.get("concerning_manager") or "").strip()
    assignee = (request.form.get("assignee") or "").strip()
    description = (request.form.get("description") or "").strip() or None

    if not categories or not concerning_manager or not assignee:
        flash("Pick at least one category, the concerning manager, and who to assign the ticket to.", "warning")
        return redirect(url_for("trainer_overview", tab="log_escalation", categories=categories))

    records_by_category = {}
    for cat_key, _label, section in db.ESCALATION_TICKET_CATEGORIES:
        if cat_key in categories and section:
            ids = request.form.getlist(f"records_{cat_key}")
            if ids:
                records_by_category[cat_key] = [int(i) for i in ids if i.isdigit()]

    ticket_number = db.create_escalation_ticket(categories, description, concerning_manager, assignee, records_by_category, created_by=login_id)
    tickety_id, tickety_error = _sync_ticket_to_tickety(ticket_number, categories, description, concerning_manager)
    if tickety_id:
        flash(f"Escalation {ticket_number} logged at Phase 1, concerning '{concerning_manager}', assigned to '{assignee}'. Linked to Tickety as {tickety_id}.", "success")
    else:
        flash(f"Escalation {ticket_number} logged at Phase 1, concerning '{concerning_manager}', assigned to '{assignee}'. Tickety sync failed: {tickety_error}", "warning")
    return redirect(url_for("escalations"))


@application.route("/ld-management/trainer-overview/escalate", methods=["POST"])
def trainer_overview_escalate():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can escalate records.")
    ids = request.form.getlist("tracked_item_id")
    ids = [int(i) for i in ids if i.isdigit()]
    fc_scope = request.form.get("fc") or None
    if not ids:
        flash("Select at least one record to escalate.", "warning")
    else:
        result = db.escalate_records(ids, escalated_by=login_id)
        parts = []
        if result["created"]:
            parts.append(f"{result['created']} escalated to Phase 1")
        if result["bumped"]:
            parts.append(f"{result['bumped']} bumped to the next phase")
        if result["at_max"]:
            parts.append(f"{result['at_max']} already at Phase {db.ESCALATION_MAX_PHASE} (max)")
        if result["no_am"]:
            parts.append(f"{result['no_am']} skipped (no AM on record)")
        flash("; ".join(parts) or "Nothing to escalate.", "success" if (result["created"] or result["bumped"]) else "warning")
    return redirect(url_for("trainer_overview", tab="safety_compliance", fc=fc_scope, trainer=request.form.get("trainer") or None))


def _can_manage_ir_definitions(login, role):
    """Only Senior Operations Managers and Trainers can edit Indirect
    Role targets/definitions — narrower than the usual ADMIN_TIER_ROLES
    gate, per an explicit request to restrict this specific page.
    Bootstrap admins still get in, consistent with their blanket access
    everywhere else in the app."""
    return login in BOOTSTRAP_ADMIN_LOGINS or role in ("som", "trainer")


def _can_manage_xt_definitions(login, role):
    """Admin-tier roles (admin/trainer/learning_manager) and Senior
    Operations Managers can edit Cross-Training targets/paths — per an
    explicit request for this specific permission set (a slightly wider
    group than the Indirect Role Definitions page above). Bootstrap
    admins always get in regardless."""
    return login in BOOTSTRAP_ADMIN_LOGINS or role in ADMIN_TIER_ROLES or role == "som"


def _can_manage_xt_exclusions(login, role):
    """Cross-Training exclusions are deliberately narrower than most
    admin-tier features — explicitly limited to trainers and admins
    only (not learning_manager or SOM), since excluding someone from
    counting as Trained is a stronger action than editing a target
    number. Bootstrap admins always get in regardless."""
    return login in BOOTSTRAP_ADMIN_LOGINS or role in ("admin", "trainer")


REGIONAL_TREND_WEEKS = 8

# Two different thresholds are in play on this page and they are not
# interchangeable, so both are named rather than written as bare numbers.
# A whole site's composite is held to 90 (what Regional Overview has
# always flagged on); a single metric is held to the same 95 every
# scorecard and ranking judges "on target" by. A site sitting at 92 is
# therefore genuinely "stable overall, with metrics to fix" — which is
# the real state, not a contradiction.
SITE_STABLE_AT = 90
SITE_SERIOUS_BELOW = 75


def _site_status(score):
    """good / watch / serious for a whole site's composite score, or None
    when there is no score at all — which is deliberately not the same
    as a bad score, and must not be coloured like one."""
    if score is None:
        return None
    if score >= SITE_STABLE_AT:
        return "good"
    if score >= SITE_SERIOUS_BELOW:
        return "watch"
    return "serious"


def _metric_status(score):
    """Same three states for one metric, against the metric-level target
    every other page already uses."""
    if score is None:
        return None
    if score >= SAFETY_COMPLIANCE_TARGET:
        return "good"
    if score >= SITE_SERIOUS_BELOW:
        return "watch"
    return "serious"


def _series_delta(series):
    """Change between the last two points of a [(week, value)] series —
    None when there aren't two points to compare, so the UI can say
    "no comparison yet" instead of showing a fabricated 0.0."""
    if not series or len(series) < 2:
        return None
    change = round(series[-1][1] - series[-2][1], 1)
    return 0.0 if change == -0.0 else change


def get_regional_overview(region=None, status=None):
    """One row per registered site: its site-wide score on each of the 7
    L&D metrics, the Total L&D composite, open escalation count, and
    which single metric most needs attention there — for the Regional
    Overview a global admin sees across every site at once. Temporarily
    switches the site context to compute each site's numbers (the same
    site-scoped functions every other page uses, unmodified) and always
    restores the original site afterward, even if a site's computation
    raises — leaving the site context pointed at the wrong site for the
    rest of the request would corrupt everything else on the page."""
    original_site = db.get_current_site()
    current_week = db.week_start_for()
    rows = []
    all_regions = []
    try:
        for site in db.get_sites():
            db.set_current_site(site["site_code"])
            categories, overall_score = _build_scorecard_categories(None, None)
            metrics = {c["key"]: c["card"]["pct_ok"] for c in categories}
            open_escalations = len(db.get_all_open_record_escalations())
            manager_count = len(db.get_roster_by_roles(["am", "om", "som"]))

            scored = [(key, label, metrics.get(key)) for key, label, _s in LD_METRICS if metrics.get(key) is not None]
            focus_metric = min(scored, key=lambda t: t[2]) if scored else None

            trend = db.get_site_weekly_composite_series(weeks=REGIONAL_TREND_WEEKS)
            plan = db.get_week_plan_utilisation(current_week)

            rows.append({
                "site_code": site["site_code"],
                "site_name": site["site_name"],
                "region": site.get("region"),
                "metrics": metrics,
                "total_score": round(overall_score, 1) if overall_score is not None else None,
                "open_escalations": open_escalations,
                "manager_count": manager_count,
                "focus_metric_label": focus_metric[1] if focus_metric else None,
                "focus_metric_score": focus_metric[2] if focus_metric else None,
                "focus_metric_key": focus_metric[0] if focus_metric else None,
                "trend": trend,
                "trend_delta": _series_delta(trend),
                "plan": plan,
                "status": _site_status(overall_score),
                "metrics_below_target": sum(
                    1 for _k, _l, _s in LD_METRICS
                    if metrics.get(_k) is not None and metrics[_k] < SAFETY_COMPLIANCE_TARGET
                ),
            })
    finally:
        db.set_current_site(original_site)

    # Every region present before filtering, so the filter control can
    # still offer the option that's currently filtering them out.
    all_regions = sorted({r["region"] for r in rows if r.get("region")})
    if region:
        rows = [r for r in rows if (r.get("region") or "") == region]
    if status:
        rows = [r for r in rows if r["status"] == status]

    scored_totals = [r["total_score"] for r in rows if r["total_score"] is not None]

    # One list of "what's actually wrong in the region", every site's
    # below-target metrics pooled and ordered worst-first, so attention
    # goes to the worst metric anywhere rather than to whichever site
    # happens to sort first.
    attention = []
    for row in rows:
        for key, label, _sections in LD_METRICS:
            value = row["metrics"].get(key)
            if value is not None and value < SAFETY_COMPLIANCE_TARGET:
                attention.append({
                    "site_code": row["site_code"],
                    "site_name": row["site_name"],
                    "metric_key": key,
                    "metric_label": label,
                    "score": value,
                    "shortfall": round(SAFETY_COMPLIANCE_TARGET - value, 1),
                    "severity": _metric_status(value),
                })
    attention.sort(key=lambda a: (a["score"], a["site_code"]))

    # Region-wide metric averages, so the pulse row reflects the region
    # rather than repeating one site's numbers.
    metric_rollup = []
    for key, label, _sections in LD_METRICS:
        values = [r["metrics"][key] for r in rows if r["metrics"].get(key) is not None]
        metric_rollup.append({
            "key": key,
            "label": label,
            "avg": round(sum(values) / len(values), 1) if values else None,
            "sites_reporting": len(values),
            "sites_below": sum(1 for v in values if v < SAFETY_COMPLIANCE_TARGET),
            "worst": min(values) if values else None,
            "best": max(values) if values else None,
        })

    total_capacity = sum(r["plan"]["capacity"] for r in rows)
    total_booked = sum(r["plan"]["booked"] for r in rows)

    # Region trend: each week's mean across whichever sites have a
    # snapshot for it, so one site's missing history doesn't drag the
    # regional line down.
    weeks = {}
    for row in rows:
        for week_start, value in row["trend"]:
            weeks.setdefault(week_start, []).append(value)
    region_trend = [(w, round(sum(v) / len(v), 1)) for w, v in sorted(weeks.items())]

    return {
        "sites": rows,
        "site_count": len(rows),
        "avg_total_score": round(sum(scored_totals) / len(scored_totals), 1) if scored_totals else None,
        "total_open_escalations": sum(r["open_escalations"] for r in rows),
        "sites_needing_attention": sum(1 for r in rows if r["total_score"] is not None and r["total_score"] < SITE_STABLE_AT),
        "sites_stable": sum(1 for r in rows if r["total_score"] is not None and r["total_score"] >= SITE_STABLE_AT),
        "sites_without_data": sum(1 for r in rows if r["total_score"] is None),
        "manager_count": sum(r["manager_count"] for r in rows),
        "attention": attention,
        "metric_rollup": metric_rollup,
        "region_trend": region_trend,
        "region_trend_delta": _series_delta(region_trend),
        "plan": {
            "capacity": total_capacity,
            "booked": total_booked,
            "seats_free": max(total_capacity - total_booked, 0),
            "pct": round(100 * total_booked / total_capacity) if total_capacity else None,
        },
        "week_start": current_week,
        "all_regions": all_regions,
        "active_region": region or "",
        "active_status": status or "",
        "is_filtered": bool(region or status),
        "trend_weeks": REGIONAL_TREND_WEEKS,
        "site_stable_at": SITE_STABLE_AT,
        "metric_target": SAFETY_COMPLIANCE_TARGET,
    }


@application.route("/ld-management/indirect-roles")
def indirect_roles_overview():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = _effective_role()
    if role is None:
        return _deny("Request a role to access Indirect Roles.")

    tab = request.args.get("tab", "overview")
    can_manage = _can_manage_ir_definitions(login_id, role)
    if tab == "definitions" and not can_manage:
        tab = "overview"

    ir_overview = db.compute_ir_overview() if tab == "overview" else None
    return render_template(
        "indirect_roles.html",
        **_login_context(),
        tab=tab,
        can_manage_ir=can_manage,
        ir_overview=ir_overview,
        ir_summary=db.summarize_ir_overview(ir_overview) if ir_overview is not None else None,
        ir_role_config=db.get_ir_role_config_rows() if tab == "definitions" else None,
        de_tech_topics=db.get_real_de_tech_topics() if tab == "definitions" else None,
        de_tech_mappings=db.get_all_de_tech_role_mappings() if tab == "definitions" else None,
        ir_ambassadors=db.get_indirect_roles_ambassadors() if tab == "ambassadors" else None,
        shifts=db.SHIFTS,
    )


@application.route("/ld-management/indirect-roles/definitions/<int:row_id>", methods=["POST"])
def indirect_roles_update_definition(row_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_ir_definitions(login_id, _effective_role()):
        return _deny("Only Senior Operations Managers and Trainers can edit Indirect Role definitions.")
    try:
        es = int(request.form.get("es") or 0)
        ls = int(request.form.get("ls") or 0)
        ns = int(request.form.get("ns") or 0)
    except ValueError:
        flash("Targets must be whole numbers.", "warning")
        return redirect(url_for("indirect_roles_overview", tab="definitions"))
    db.update_ir_role_config_row(
        row_id, es, ls, ns,
        area=request.form.get("area") or "",
        home_process=request.form.get("home_process") or "",
        match_vals=request.form.get("match_vals") or "",
        special=request.form.get("special") or "",
    )
    flash("Definition updated.", "success")
    return redirect(url_for("indirect_roles_overview", tab="definitions"))


@application.route("/api/de-tech-role-mapping-form/<int:role_id>")
def api_de_tech_role_mapping_form(role_id):
    if _effective_role() is None:
        return "", 403
    conn = db.get_db()
    role = conn.execute("SELECT * FROM ir_role_config WHERE id=?", (role_id,)).fetchone()
    conn.close()
    if not role:
        return '<div class="empty-state">Role not found.</div>'
    return render_template(
        "de_tech_role_mapping_fragment.html",
        role_id=role_id, role_name=role["role"],
        topics=db.get_real_de_tech_topics(),
        mapped_topics=set(db.get_de_tech_role_mapping(role_id)),
    )


@application.route("/ld-management/indirect-roles/definitions/<int:row_id>/de-tech-topics", methods=["POST"])
def indirect_roles_de_tech_topics_update(row_id):
    login_id = _require_login()
    if not login_id:
        return jsonify({"ok": False, "error": "Please log in again."}), 401
    role = _effective_role()
    if not _can_manage_ir_definitions(login_id, role):
        return jsonify({"ok": False, "error": "Only Senior Operations Managers and Trainers can edit Indirect Role definitions."}), 403
    topics = request.form.getlist("de_tech_topic")
    db.set_de_tech_role_mapping(row_id, topics, mapped_by=login_id)
    return jsonify({"ok": True, "topics": topics})


def _can_access_olr():
    """Operational Leadership Review is a performance-review tool about
    AMs/OMs, not a self-service page for them — access is the same
    reviewer tier as Indirect Role Definitions (Senior Ops + Trainers),
    plus the rest of admin-tier, since it's the kind of thing an
    Learning Manager would also need to see."""
    role = _effective_role()
    login_id = session.get("login_id")
    return login_id in BOOTSTRAP_ADMIN_LOGINS or role in ADMIN_TIER_ROLES or role == "som"


@application.route("/ld-management/olr")
def olr_overview():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_access_olr():
        return _deny("Operational Leadership Review is limited to Senior Operations Managers and L&D.")

    reviewees = db.get_olr_reviewees()
    rows = []
    for r in reviewees:
        averages = db.get_olr_averages(r["login"])
        esc = db.get_olr_escalation_counts(r["login"])
        rows.append({
            "login": r["login"], "full_name": r["full_name"], "role": r["role"], "title": r["title"],
            "averages": averages, "escalations": esc,
        })
    rows.sort(key=lambda x: (x["averages"]["wow_composite_avg"] is None, x["averages"]["wow_composite_avg"] or 0))

    return render_template(
        "olr.html",
        **_login_context(),
        rows=rows,
        role_labels=ROLES,
        metric_labels=db.OLR_METRIC_LABELS,
    )


@application.route("/ld-management/olr/upload", methods=["POST"])
def olr_upload():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_access_olr():
        return _deny("Operational Leadership Review is limited to Senior Operations Managers and L&D.")
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a CSV file first.", "warning")
        return redirect(url_for("olr_overview"))
    try:
        n = db.ingest_olr_weekly_csv(f.read(), f.filename, uploaded_by=login_id)
        flash(f"Imported {n} weekly metric value(s).", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "danger")
    return redirect(url_for("olr_overview"))


@application.route("/ld-management/olr/<login>")
def olr_detail(login):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_access_olr():
        return _deny("Operational Leadership Review is limited to Senior Operations Managers and L&D.")

    role_row = db.get_all_user_roles()
    person = next((r for r in role_row if r["login"] == login), None)
    if not person:
        flash(f"No AM/OM found for login '{login}'.", "warning")
        return redirect(url_for("olr_overview"))

    series = db.get_olr_weekly_series(login)
    weeks_sorted = sorted(series.keys(), reverse=True)
    averages = db.get_olr_averages(login)
    esc = db.get_olr_escalation_counts(login)

    return render_template(
        "olr_detail.html",
        **_login_context(),
        person=person,
        weeks=weeks_sorted,
        series=series,
        averages=averages,
        escalations=esc,
        metric_keys=db.OLR_METRIC_KEYS,
        metric_labels=db.OLR_METRIC_LABELS,
    )


@application.route("/api/ir-role-members/<int:role_id>/<shift>/<bucket>")
def api_ir_role_members(role_id, shift, bucket):
    if _effective_role() is None:
        return "", 403
    if shift not in db.SHIFTS or bucket not in ("trained", "no_practice", "not_trained"):
        return "", 404
    overview = db.compute_ir_overview(include_members=True)
    for sec in overview:
        for r in sec["roles"]:
            if r["id"] != role_id:
                continue
            shift_data = next((s for s in r["shifts"] if s["shift"] == shift), None)
            if not shift_data:
                return "", 404
            members = shift_data["members"][bucket]
            return render_template(
                "ir_role_members_fragment.html",
                section=sec["section"], role=r["role"], shift_label=db.SHIFTS[shift],
                bucket=bucket, members=members, role_id=role_id, shift=shift,
                can_escalate=_effective_role() in ADMIN_TIER_ROLES,
                assignable_managers=db.get_escalation_assignable_managers(),
                escalation_categories=db.ESCALATION_TICKET_CATEGORIES,
            )
    return "", 404


@application.route("/api/ir-roster-search")
def api_ir_roster_search():
    if _effective_role() is None:
        return jsonify([])
    q = request.args.get("q", "")
    return jsonify(db.search_ir_roster(q))


@application.route("/ld-management/indirect-roles/escalate", methods=["POST"])
def indirect_roles_escalate():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can log escalations.")
    logins = list(request.form.getlist("member_login"))
    additional_raw = request.form.get("additional_logins") or ""
    for piece in additional_raw.replace("\n", ",").split(","):
        piece = piece.strip()
        if piece and piece not in logins:
            logins.append(piece)
    concerning_manager = (request.form.get("concerning_manager") or "").strip()
    assignee = (request.form.get("assignee") or "").strip()
    role_label = request.form.get("role_label") or ""
    shift_label = request.form.get("shift_label") or ""
    is_training_request = bool(request.form.get("is_training_request"))
    if not logins or not concerning_manager or not assignee:
        flash("Add at least one associate, the concerning manager, and who to assign the ticket to.", "warning")
        return redirect(request.referrer or url_for("indirect_roles_overview"))
    if is_training_request:
        description = f"Training request — {role_label} ({shift_label}). Associates to train: {', '.join(logins)}"
    else:
        description = f"Indirect Role gap — {role_label} ({shift_label}). Associates: {', '.join(logins)}"
    ticket_number = db.create_escalation_ticket(["indirect_roles"], description, concerning_manager, assignee, {}, created_by=login_id)
    _sync_ticket_to_tickety(ticket_number, ["indirect_roles"], description, concerning_manager)
    if is_training_request:
        flash(f"Training request {ticket_number} logged for {len(logins)} associate(s).", "success")
    else:
        flash(f"Escalation {ticket_number} logged for {len(logins)} associate(s).", "success")
    return redirect(url_for("escalations"))


@application.route("/ld-management/cross-training")
def cross_training():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = _effective_role()
    if role is None:
        return _deny("Request a role to access Cross Training.")

    tab = request.args.get("tab", "standards")
    scenario = request.args.get("scenario", "general")
    if scenario not in dict(db.XT_SCENARIOS):
        scenario = "general"
    can_manage_xt = _can_manage_xt_definitions(login_id, role)
    can_manage_xt_exclusions = _can_manage_xt_exclusions(login_id, role)
    if tab == "definitions" and not can_manage_xt:
        tab = "standards"
    if tab == "exclusions" and not can_manage_xt_exclusions:
        tab = "standards"
    matrix_shift = request.args.get("matrix_shift") or "early"
    matrix = db.get_xt_dept_process_matrix(shift=matrix_shift) if tab == "directory" else None
    try:
        xt_drop_days = int(request.args.get("drop_days", 30))
    except ValueError:
        xt_drop_days = 30

    exclusions = None
    exclusion_associates = None
    if tab == "exclusions":
        exclusions = db.get_xt_exclusions()
        conn = db.get_db()
        exclusion_associates = conn.execute(
            "SELECT DISTINCT employee_login, full_name FROM xt_hours WHERE employee_login IS NOT NULL ORDER BY full_name"
        ).fetchall()
        conn.close()

    return render_template(
        "cross_training.html",
        **_login_context(),
        tab=tab,
        scenario=scenario,
        xt_scenarios=db.XT_SCENARIOS,
        xt_scenario_labels=dict(db.XT_SCENARIOS),
        can_manage_xt=can_manage_xt,
        can_manage_xt_exclusions=can_manage_xt_exclusions,
        exclusions=exclusions,
        exclusion_associates=exclusion_associates,
        standards=db.get_xt_standards(scenario=scenario) if tab == "standards" else None,
        internal_xt_overview=db.get_internal_xt_overview() if tab == "standards" else None,
        internal_xt_targets=db.get_internal_xt_targets() if tab == "definitions" else None,
        xt_definitions=db.get_xt_standard_defs(scenario=scenario) if tab == "definitions" else None,
        xt_processes=db.get_xt_processes() if tab in ("definitions", "directory", "exclusions") else None,
        freshness=db.xt_hours_freshness(),
        matrix=matrix,
        matrix_shift=matrix_shift,
        shifts=db.SHIFTS,
        home_departments=db.get_xt_home_departments(),
        xt_proficiency_expiry_days=db.get_xt_proficiency_expiry_days(),
        xt_drop_days=xt_drop_days,
        xt_retention_report=db.get_xt_retention_report() if tab == "retention" else None,
    )


@application.route("/ld-management/cross-training/definitions/expiry-days", methods=["POST"])
def cross_training_set_expiry_days():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_definitions(login_id, _effective_role()):
        return _deny("Only admins, trainers, learning managers, and Senior Operations Managers can edit Cross-Training definitions.")
    try:
        days = int(request.form.get("days"))
        if days < 1:
            raise ValueError
    except (TypeError, ValueError):
        flash("Enter a whole number of days greater than 0.", "warning")
        return redirect(url_for("cross_training", tab="definitions"))
    db.set_app_setting("xt_proficiency_expiry_days", days, updated_by=login_id)
    flash(f"Proficiency expiry window set to {days} days without process.", "success")
    return redirect(url_for("cross_training", tab="definitions"))


@application.route("/api/xt-departments/<department>")
def api_xt_department(department):
    if _effective_role() is None:
        return "", 403
    try:
        employees = db.get_xt_employees_by_department(department)
        return render_template("xt_associate_search_fragment.html", employees=employees, query=department, is_department_lookup=True, department=department)
    except Exception as e:
        return f'<div class="empty-state" style="padding:16px; color:var(--red); text-align:left;"><strong>Lookup failed:</strong> {e}</div>'


@application.route("/api/xt-coverage-table")
def api_xt_coverage_table():
    if _effective_role() is None:
        return "", 403
    shift = request.args.get("shift") or None
    manager_login = request.args.get("manager") or None
    department = request.args.get("department") or None
    process = request.args.get("process") or None
    if not manager_login and not department and not process:
        return render_template(
            "xt_associate_search_fragment.html", employees=[], query="",
            is_department_lookup=True, department="a manager, a department, or a process",
        )
    try:
        employees = db.get_xt_coverage_table(shift=shift, manager_login=manager_login, department=department, process=process)
        label_bits = []
        if shift:
            label_bits.append(db.SHIFTS.get(shift, shift) + " shift")
        if manager_login:
            label_bits.append(f"manager '{manager_login}'")
        if department:
            label_bits.append(f"department '{department}'")
        if process:
            label_bits.append(f"trained in '{process}'")
        return render_template(
            "xt_associate_search_fragment.html", employees=employees, query=" + ".join(label_bits),
            is_department_lookup=True, department=" + ".join(label_bits),
        )
    except Exception as e:
        return f'<div class="empty-state" style="padding:16px; color:var(--red); text-align:left;"><strong>Lookup failed:</strong> {e}</div>'


@application.route("/api/xt-team/<manager_login>")
def api_xt_team(manager_login):
    if _effective_role() is None:
        return "", 403
    try:
        employees = db.get_xt_team(manager_login)
        return render_template("xt_associate_search_fragment.html", employees=employees, query=manager_login, is_team_lookup=True, manager_login=manager_login)
    except Exception as e:
        return f'<div class="empty-state" style="padding:16px; color:var(--red); text-align:left;"><strong>Lookup failed:</strong> {e}</div>'


@application.route("/api/xt-employee-lookup")
def api_xt_employee_lookup():
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    login = request.args.get("login", "")
    return jsonify({"full_name": db.get_xt_employee_name(login)})


@application.route("/api/xt-associates/search")
def api_xt_associate_search():
    if _effective_role() is None:
        return "", 403
    query = request.args.get("q", "")
    try:
        employees = db.search_xt_employees(query)
        return render_template("xt_associate_search_fragment.html", employees=employees, query=query)
    except Exception as e:
        # Surfacing the real error here (instead of letting it become a
        # bare 500) is deliberate and temporary — this exact class of
        # bug has twice now only reproduced against the real Postgres
        # database in production, never locally against SQLite, so
        # seeing the actual message is the fastest way to find it.
        return f'<div class="empty-state" style="padding:16px; color:var(--red); text-align:left;"><strong>Search failed:</strong> {e}</div>'


@application.route("/api/xt-standards/<key>/trained")
def api_xt_trained(key):
    if _effective_role() is None:
        return "", 403
    shift = request.args.get("shift") or None
    if shift not in (None, "early", "late", "night"):
        return "", 400
    scenario = request.args.get("scenario", "general")
    if scenario not in dict(db.XT_SCENARIOS):
        scenario = "general"
    employees = db.get_xt_trained_employees(key, shift=shift, scenario=scenario)
    return render_template("xt_trained_fragment.html", employees=employees)


@application.route("/api/internal-xt-targets/<int:target_id>/trained")
def api_internal_xt_trained(target_id):
    if _effective_role() is None:
        return "", 403
    shift = request.args.get("shift") or None
    if shift not in (None, "early", "late", "night"):
        return "", 400
    employees = db.get_internal_xt_trained_employees(target_id, shift=shift)
    return render_template("xt_trained_fragment.html", employees=employees)


@application.route("/ld-management/cross-training/definitions/add", methods=["POST"])
def xt_definitions_add():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_definitions(login_id, _effective_role()):
        return _deny("Only admins, trainers, learning managers, and Senior Operations Managers can edit Cross-Training definitions.")
    scenario = request.form.get("scenario", "general")
    if scenario not in dict(db.XT_SCENARIOS):
        scenario = "general"
    key = (request.form.get("key") or "").strip()
    label = (request.form.get("label") or "").strip()
    if not key or not label:
        flash("A key and a label are required.", "warning")
        return redirect(url_for("cross_training", tab="definitions", scenario=scenario))
    source_depts = request.form.getlist("source")
    try:
        pct_early = float(request.form.get("pct_early") or 0) / 100
        pct_late = float(request.form.get("pct_late") or 0) / 100
        pct_night = float(request.form.get("pct_night") or 0) / 100
    except ValueError:
        flash("Percentages must be numbers.", "warning")
        return redirect(url_for("cross_training", tab="definitions", scenario=scenario))
    db.add_xt_standard(scenario, key, label, request.form.get("group") or "ib", source_depts,
                        request.form.get("target") or "", pct_early, pct_late, pct_night)
    flash(f"Added '{label}' to {dict(db.XT_SCENARIOS)[scenario]}.", "success")
    return redirect(url_for("cross_training", tab="definitions", scenario=scenario))


@application.route("/ld-management/cross-training/definitions/bulk-update", methods=["POST"])
def xt_definitions_bulk_update():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_definitions(login_id, _effective_role()):
        return _deny("Only admins, trainers, learning managers, and Senior Operations Managers can edit Cross-Training definitions.")
    scenario = request.form.get("scenario", "general")
    if scenario not in dict(db.XT_SCENARIOS):
        scenario = "general"
    ids = request.form.getlist("row_id")
    labels = request.form.getlist("label")
    groups = request.form.getlist("group")
    sources = request.form.getlist("source")
    targets = request.form.getlist("target")
    pcts_early = request.form.getlist("pct_early")
    pcts_late = request.form.getlist("pct_late")
    pcts_night = request.form.getlist("pct_night")
    rows = []
    try:
        for i, row_id in enumerate(ids):
            rows.append({
                "id": int(row_id), "label": labels[i].strip(), "group": groups[i],
                "source": [s.strip() for s in sources[i].split(",") if s.strip()],
                "target": targets[i].strip(),
                "pct_early": float(pcts_early[i] or 0) / 100,
                "pct_late": float(pcts_late[i] or 0) / 100,
                "pct_night": float(pcts_night[i] or 0) / 100,
            })
    except (ValueError, IndexError):
        flash("Something in the table didn't parse right — check the percentage columns are numbers.", "warning")
        return redirect(url_for("cross_training", tab="definitions", scenario=scenario))
    db.bulk_update_xt_standards(rows)
    flash(f"Saved {len(rows)} standard(s).", "success")
    return redirect(url_for("cross_training", tab="definitions", scenario=scenario))


@application.route("/ld-management/cross-training/internal-targets/bulk-update", methods=["POST"])
def internal_xt_targets_bulk_update():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_definitions(login_id, _effective_role()):
        return _deny("Only admins, trainers, learning managers, and Senior Operations Managers can edit Cross-Training definitions.")
    ids = request.form.getlist("row_id")
    labels = request.form.getlist("label")
    source_depts = request.form.getlist("source_dept")
    target_processes = request.form.getlist("target_process")
    es_list = request.form.getlist("target_early")
    ls_list = request.form.getlist("target_late")
    ns_list = request.form.getlist("target_night")
    rows = []
    try:
        for i, row_id in enumerate(ids):
            rows.append({
                "id": int(row_id), "label": labels[i].strip(),
                "source_dept": source_depts[i].strip(), "target_process": target_processes[i].strip(),
                "target_early": int(es_list[i] or 0), "target_late": int(ls_list[i] or 0), "target_night": int(ns_list[i] or 0),
            })
    except (ValueError, IndexError):
        flash("Something in the table didn't parse right — check the target columns are whole numbers.", "warning")
        return redirect(url_for("cross_training", tab="definitions"))
    db.bulk_update_internal_xt_targets(rows)
    flash(f"Saved {len(rows)} row(s).", "success")
    return redirect(url_for("cross_training", tab="definitions"))


@application.route("/ld-management/cross-training/definitions/<int:row_id>/delete", methods=["POST"])
def xt_definitions_delete(row_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_definitions(login_id, _effective_role()):
        return _deny("Only admins, trainers, learning managers, and Senior Operations Managers can edit Cross-Training definitions.")
    scenario = request.form.get("scenario", "general")
    db.delete_xt_standard(row_id)
    flash("Standard removed.", "info")
    return redirect(url_for("cross_training", tab="definitions", scenario=scenario))


@application.route("/ld-management/exclusions")
def xt_exclusions_page():
    return redirect(url_for("cross_training", tab="exclusions"))


@application.route("/ld-management/exclusions/add", methods=["POST"])
def xt_exclusions_add():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_exclusions(login_id, _effective_role()):
        return _deny("Only trainers and admins can manage Cross-Training exclusions.")
    employee_login = (request.form.get("employee_login") or "").strip()
    merged_function = (request.form.get("merged_function") or "").strip()
    reason = (request.form.get("reason") or "").strip()
    if not employee_login or not merged_function or not reason:
        flash("Associate, process, and a reason are all required.", "warning")
        return redirect(url_for("cross_training", tab="exclusions"))
    db.add_xt_exclusion(employee_login, merged_function, reason, excluded_by=login_id)
    flash(f"{employee_login} excluded from being counted as Trained on {merged_function}.", "success")
    return redirect(url_for("cross_training", tab="exclusions"))


@application.route("/ld-management/exclusions/<int:exclusion_id>/remove", methods=["POST"])
def xt_exclusions_remove(exclusion_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not _can_manage_xt_exclusions(login_id, _effective_role()):
        return _deny("Only trainers and admins can manage Cross-Training exclusions.")
    db.remove_xt_exclusion(exclusion_id)
    flash("Exclusion removed — the associate's real status is restored.", "info")
    return redirect(url_for("cross_training", tab="exclusions"))


@application.route("/section/<section>")
def section_detail(section):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if section not in db.SECTIONS:
        flash("Unknown section.", "danger")
        return redirect(url_for("overview"))

    role = _effective_role()
    if role is None:
        return _deny("Request a role to access this page.")
    am_scope = login_id if role in OPS_SELF_ROLES else request.args.get("am")
    fc_scope = request.args.get("fc")

    items = db.get_items(section=section, am_login=am_scope, fc=fc_scope)
    items.sort(key=lambda r: (r.get("due_date") or "9999-99-99"))

    de_tech_ambassadors = []
    if section == "planning_de_tech":
        for dept_amb in db.get_ambassadors("early", ambassador_type="process").values():
            de_tech_ambassadors += dept_amb
        for shift_key in ("late", "night"):
            for dept_amb in db.get_ambassadors(shift_key, ambassador_type="process").values():
                de_tech_ambassadors += dept_amb

    return render_template(
        "section_detail.html",
        **_login_context(),
        section=section,
        section_label=db.SECTIONS[section],
        items=items,
        card=db.scorecard([section], am_login=am_scope, fc=fc_scope),
        am_scope=am_scope,
        fc_scope=fc_scope,
        ams=db.all_ams(),
        fcs=db.all_fcs(),
        de_tech_ambassadors=de_tech_ambassadors,
        de_tech_centrally_scheduled=db.DE_TECH_CENTRALLY_SCHEDULED,
        current_week=db.week_start_for(),
        shift_options=db.SHIFTS,
    )


def _de_tech_is_ambassador_trained(subcategory):
    """FSRI and the Robotic Arm Palletizer briefing are centrally
    scheduled by regular Trainers, not ambassadors — this is the same
    exclusion list db.DE_TECH_CENTRALLY_SCHEDULED already uses
    elsewhere for these two topics, so both stay consistent."""
    return (subcategory or "").strip() not in db.DE_TECH_CENTRALLY_SCHEDULED


def _ir_role_is_ambassador_trainable(role_name):
    """The Indirect Roles equivalent of the same exclusion — FSRI and
    any palletizer-named role are centrally trained, not ambassador-
    trained, so they're left out of the picker when assigning an
    Indirect Roles ambassador."""
    r = (role_name or "").lower()
    return "fsri" not in r and "palletiz" not in r


def _ambassador_trainable_ir_sections():
    """The full Indirect Role catalog, grouped by section, filtered to
    the roles ambassadors can actually be assigned to train — used by
    both the inline picker on Add Ambassador and the standalone
    'manage roles' picker for an existing ambassador."""
    sections = {}
    for row in db.get_ir_role_config_rows():
        if _ir_role_is_ambassador_trainable(row["role"]):
            sections.setdefault(row["section"], []).append(row)
    return sections


@application.route("/ld-management/de-tech/create-internal-training", methods=["POST"])
def create_de_tech_internal_training():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = _effective_role()
    if role is None:
        return _deny("Request a role to access this page.")

    item_ids = [int(i) for i in request.form.getlist("tracked_item_id") if i.isdigit()]
    ambassador_login = (request.form.get("ambassador_login") or "").strip()
    shift = request.form.get("shift") or "early"
    week_start = request.form.get("week_start") or db.week_start_for()
    day_index = request.form.get("day_index")
    start_time = (request.form.get("start_time") or "09:00").strip()

    if not item_ids or not ambassador_login or day_index is None or day_index == "":
        flash("Pick at least one record, an ambassador, and a day to schedule this.", "warning")
        return redirect(request.referrer or url_for("overview"))

    items = [i for i in db.get_items(section="planning_de_tech") if i["id"] in item_ids]
    items = [i for i in items if _de_tech_is_ambassador_trained(i.get("subcategory"))]
    if not items:
        flash("FSRI and Palletizer briefings are trained by Trainers, not ambassadors — pick a different topic.", "warning")
        return redirect(request.referrer or url_for("overview"))

    topics = sorted({i["subcategory"] for i in items if i.get("subcategory")})
    ambassador = db.get_xt_employee_name(ambassador_login)
    slot_id = db.add_training_slot(
        shift=shift, week_start=week_start, day_index=int(day_index), training_id=None,
        start_time=start_time, capacity=None, instructor=ambassador_login, room=None,
        notes=f"Internal ambassador-led DE Technical Briefing: {', '.join(topics)}",
        created_by=login_id,
    )
    n = 0
    for i in items:
        db.add_slot_attendee(slot_id, i["employee_login"], i.get("full_name"), i.get("fc"), added_by=login_id)
        n += 1
    flash(f"Created an internal training with {ambassador_login} for {n} associate(s).", "success")
    return redirect(request.referrer or url_for("overview"))


# ------------------------------------------------------------ scorecards ----

@application.route("/scorecards")
def scorecards():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))

    role = _effective_role()
    can_see_som_om = role in OPS_MANAGER_ROLES or role in ADMIN_TIER_ROLES
    can_admin = role in ADMIN_TIER_ROLES
    tab = request.args.get("tab", "reporting")
    if tab == "som_om" and not can_see_som_om:
        tab = "reporting"
    if tab in ("ambassador_attendance", "ambassador_hours", "escalations") and not can_admin:
        tab = "reporting"
    if tab == "manager_rankings" and not can_see_som_om:
        tab = "reporting"

    manager_rankings = None
    safety_ranking = get_safety_training_compliance_ranking() if tab == "manager_rankings" else None
    manager_ranking_labels = {k: db.SCORECARD_CATEGORY_MAP[k][0] for k in MANAGER_RANKING_METRICS}
    ld_all_rankings = get_all_ld_metric_rankings() if tab == "manager_rankings" else None

    ld_overall = db.scorecard(
        db.PLANNING_SECTIONS + db.TRAINING_SECTIONS + db.STAFFING_SECTIONS
    )
    leadership = {
        "xt": db.scorecard(["staffing_xt"]),
        "indirect_roles": db.scorecard(["staffing_indirect_coverage", "planning_indirect_roles"]),
        "instructor_hours": db.scorecard(["staffing_instructor"]),
    }

    am_breakdown = []
    if tab == "som_om":
        for am_login in db.all_ams():
            scores = []
            for _key, _label, sections in db.SCORECARD_CATEGORIES:
                card = db.scorecard(sections, am_login=am_login)
                if card["pct_ok"] is not None:
                    scores.append(card["pct_ok"])
            am_breakdown.append({
                "am_login": am_login,
                "score": round(sum(scores) / len(scores)) if scores else None,
            })
        am_breakdown.sort(key=lambda r: (r["score"] if r["score"] is not None else 999))

    ambassador_attendance_report = None
    ambassador_attendance_shift = None
    ambassador_attendance_cards = None
    ambassador_meeting_shift_grid = None
    if tab == "ambassador_attendance":
        ambassador_attendance_shift = request.args.get("shift") or None
        ambassador_attendance_report = db.get_ambassador_attendance_report(shift=ambassador_attendance_shift)
        ambassador_meeting_shift_grid = db.get_ambassador_meeting_shift_grid()

    ambassador_hours_overview = None
    if tab == "ambassador_hours":
        ambassador_hours_overview = db.get_ambassador_hours_overview()

    escalation_org_summary = None
    phase_3_escalations = None
    if tab == "escalations":
        escalation_org_summary = db.get_escalation_summary_by_senior()
        escalation_org_summary.sort(key=lambda b: -b["total"])
        phase_3_escalations = db.get_phase_3_escalations()

    return render_template(
        "scorecards.html",
        **_login_context(),
        ld_overall=ld_overall,
        leadership=leadership,
        safety_health=db.get_safety_compliance_health() if tab == "reporting" else None,
        xt_site_compliance=db.get_xt_site_compliance() if tab == "reporting" else None,
        ir_summary_site=db.summarize_ir_overview(db.compute_ir_overview()) if tab == "reporting" else None,
        tab=tab,
        can_see_som_om=can_see_som_om,
        am_breakdown=am_breakdown,
        ambassador_attendance_report=ambassador_attendance_report,
        ambassador_attendance_shift=ambassador_attendance_shift,
        ambassador_attendance_cards=ambassador_attendance_cards,
        ambassador_meeting_shift_grid=ambassador_meeting_shift_grid,
        escalation_org_summary=escalation_org_summary,
        phase_3_escalations=phase_3_escalations,
        ambassador_meeting_statuses=db.AMBASSADOR_MEETING_STATUSES,
        attendance_statuses=db.AMBASSADOR_ATTENDANCE_STATUSES,
        ambassador_hours_overview=ambassador_hours_overview,
        manager_rankings=manager_rankings,
        manager_ranking_labels=manager_ranking_labels,
        safety_ranking=safety_ranking,
        ld_all_rankings=ld_all_rankings,
        ld_metrics=LD_METRICS,
        shifts=db.SHIFTS,
    )


@application.route("/scorecards/ambassador-hours.csv")
def ambassador_hours_export():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return Response("Admin access required", status=403, mimetype="text/plain")

    overview = db.get_ambassador_hours_overview()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Department", "Shift", "Ambassador", "Login", "Process",
        "Hours (last 60 days)", "Target hours", "Gap to target", "Status",
    ])
    for department in overview["departments"]:
        for shift in department["shifts"].values():
            for ambassador in shift["ambassadors"]:
                for process in ambassador["processes"]:
                    writer.writerow([
                        department["department"],
                        shift["shift_label"],
                        ambassador["full_name"],
                        ambassador["login"],
                        process["process"],
                        process["hours"],
                        process["required_hours"],
                        process["gap"],
                        process["severity_label"],
                    ])

    csv_bytes = "\ufeff" + output.getvalue()
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=ambassador-practice-report.csv"},
    )


# ----------------------------------------------------------- weekly plan ----
# The "Weekly training plan" nav item now shows the shift-based training
# board (Early/Late/Night sub-tabs) — superseding the old flat plan table.

@application.route("/weekly-plan")
def weekly_plan():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    return redirect(url_for("ld_shift", shift="early"))


# ----------------------------------------------------------- escalations ----

@application.route("/escalations")
def escalations():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = _effective_role()
    if role is None:
        return _deny("Request a role to access Escalations.")

    scope, include_som_broadcast = _escalation_visibility_scope(login_id, role)

    return render_template(
        "escalations.html",
        **_login_context(),
        record_escalations=db.get_visible_record_escalations(scope, include_som_broadcast, status="open"),
        escalation_tickets=db.get_escalation_tickets(scope, status="open"),
        can_log_escalation=role in ADMIN_TIER_ROLES,
    )


@application.route("/escalations/tickets/<int:ticket_id>")
def escalation_ticket_detail(ticket_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = _effective_role()
    if role is None:
        return "", 403
    ticket = db.get_escalation_ticket(ticket_id)
    if not ticket:
        return "", 404
    scope, include_som_broadcast = _escalation_visibility_scope(login_id, role)
    can_view = scope is None or ticket["concerning_manager"] in scope
    if not can_view:
        return "", 403
    can_manage = role in ADMIN_TIER_ROLES  # only trainers/L&D staff resolve or escalate to the next phase
    return render_template(
        "escalation_ticket_fragment.html", ticket=ticket, category_map=db.ESCALATION_TICKET_CATEGORY_MAP,
        can_manage=can_manage, assignable_managers=db.get_escalation_assignable_managers() if can_manage else [],
        max_phase=db.ESCALATION_MAX_PHASE,
    )


@application.route("/escalations/tickets/<int:ticket_id>/comment", methods=["POST"])
def escalation_ticket_comment(ticket_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    role = _effective_role()
    if role is None:
        return "", 403
    ticket = db.get_escalation_ticket(ticket_id)
    if not ticket:
        return "", 404
    scope, _include_som_broadcast = _escalation_visibility_scope(login_id, role)
    if not (scope is None or ticket["concerning_manager"] in scope):
        return "", 403
    comment = (request.form.get("comment") or "").strip()
    comment_type = "verification_request" if request.form.get("verification_request") else "comment"
    if not comment:
        flash("Write something before posting.", "warning")
    else:
        db.add_escalation_comment(ticket_id, comment, comment_type, posted_by=login_id)
        flash("Posted — it'll appear on the trainers' Updates page.", "success")
    return redirect(request.referrer or url_for("escalations"))


@application.route("/escalations/tickets/<int:ticket_id>/due-date", methods=["POST"])
def escalation_ticket_set_due_date(ticket_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only trainers can set a due date on an escalation.")
    due_date = (request.form.get("due_date") or "").strip() or None
    db.set_escalation_ticket_due_date(ticket_id, due_date, set_by=login_id)
    flash("Due date updated." if due_date else "Due date cleared.", "success")
    return redirect(request.referrer or url_for("escalations"))


@application.route("/escalations/tickets/<int:ticket_id>/resolve", methods=["POST"])
def escalation_ticket_resolve(ticket_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only trainers can resolve escalation tickets.")
    db.resolve_escalation_ticket(ticket_id, resolved_by=login_id)
    flash(f"Escalation ticket resolved.", "success")
    return redirect(url_for("escalations"))


@application.route("/escalations/tickets/<int:ticket_id>/escalate", methods=["POST"])
def escalation_ticket_escalate(ticket_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only trainers can escalate a ticket to the next phase.")
    new_target = (request.form.get("new_target_login") or "").strip()
    if not new_target:
        flash("Pick a manager to reassign the escalation to.", "warning")
        return redirect(url_for("escalations"))
    ok = db.escalate_ticket_phase(ticket_id, new_target, escalated_by=login_id)
    if ok:
        flash(f"Escalation reassigned to '{new_target}' and bumped to the next phase.", "success")
    else:
        flash(f"Already at Phase {db.ESCALATION_MAX_PHASE} (max) — can't escalate further.", "warning")
    return redirect(url_for("escalations"))


@application.route("/escalations/records/<int:escalation_id>/resolve", methods=["POST"])
def record_escalation_resolve(escalation_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can resolve record escalations.")
    db.resolve_record_escalation(escalation_id, resolved_by=login_id)
    flash(f"Escalation #{escalation_id} marked resolved.", "success")
    return redirect(url_for("escalations"))


@application.route("/api/escalations/senior/<login>")
def api_escalations_senior(login):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    summary = db.get_escalation_summary_by_senior()
    bucket = next((b for b in summary if b["senior_login"] == login), None)
    if not bucket:
        return render_template("escalation_senior_fragment.html", senior_login=login, groups=[])
    groups = {}
    for e in bucket["record_escalations"]:
        key = ("record", e["target_login"] or (e["target_role"] or "unassigned"), e["phase"])
        g = groups.setdefault(key, {"target": key[1], "phase": key[2], "kind": "record", "records": [], "tickets": []})
        g["records"].append(e)
    for t in bucket["tickets"]:
        key = ("ticket", t["target_login"] or "unassigned", t["phase"])
        g = groups.setdefault(key, {"target": key[1], "phase": key[2], "kind": "ticket", "records": [], "tickets": []})
        g["tickets"].append(t)
    group_list = sorted(groups.values(), key=lambda g: (-g["phase"], g["target"] or ""))
    return render_template("escalation_senior_fragment.html", senior_login=login, groups=group_list)


@application.route("/escalations/<int:escalation_id>/resolve", methods=["POST"])
def escalation_resolve(escalation_id):
    db.resolve_escalation(escalation_id)
    flash(f"Escalation #{escalation_id} marked resolved.", "success")
    return redirect(url_for("escalations"))


# --------------------------------------------------------- trainer metrics -

@application.route("/trainer-metrics", methods=["GET", "POST"])
def trainer_metrics():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Trainer metrics is an L&D tool — you don't have access to it.")

    if request.method == "POST":
        db.add_trainer_metric(
            trainer_login=request.form.get("trainer_login"),
            full_name=request.form.get("full_name"),
            fc=request.form.get("fc"),
            metric_name=request.form.get("metric_name"),
            metric_value=float(request.form.get("metric_value") or 0),
            period=request.form.get("period"),
        )
        flash("Metric recorded.", "success")
        return redirect(url_for("trainer_metrics"))

    rows = db.get_trainer_metrics()
    periods = sorted({r["period"] for r in rows}, reverse=True)
    return render_template(
        "trainer_metrics.html",
        **_login_context(),
        rows=rows,
        periods=periods,
    )


# --------------------------------------------------- org structure (config) -
# Folded into L&D Settings — these are POST targets only now, no standalone
# page; the form and list render inline on the settings page.

@application.route("/ops-structure", methods=["POST"])
def ops_structure():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("You don't have access to change settings.")

    db.add_ops_role(
        role_title=request.form.get("role_title"),
        person_name=request.form.get("person_name"),
        reports_to=request.form.get("reports_to"),
        scope_note=request.form.get("scope_note"),
        sort_order=int(request.form.get("sort_order") or 0),
    )
    flash("Role added.", "success")
    return redirect(url_for("ld_settings"))


@application.route("/ops-structure/<int:role_id>/delete", methods=["POST"])
def ops_structure_delete(role_id):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("You don't have access to change settings.")
    db.delete_ops_role(role_id)
    return redirect(url_for("ld_settings"))


# ------------------------------------------------------- L&D Management ----

@application.route("/ld-management")
def ld_management():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    return redirect(url_for("ld_shift", shift="early", **request.args))


@application.route("/site/switch", methods=["POST"])
def site_switch():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    site_code = (request.form.get("site_code") or "").strip().upper()
    if site_code != db.DEFAULT_SITE_CODE and not db.get_site(site_code):
        flash("That site doesn't exist.", "warning")
        return redirect(request.referrer or url_for("overview"))
    session["site"] = site_code
    flash(f"Switched to {site_code}.", "info")
    return redirect(url_for("overview"))


@application.route("/site/create", methods=["GET", "POST"])
def site_create():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not (db.is_global_admin(login_id) or login_id in BOOTSTRAP_ADMIN_LOGINS):
        return _deny("Only regional/global admins can create new sites.")

    if request.method == "POST":
        site_code = request.form.get("site_code")
        site_name = request.form.get("site_name")
        region = request.form.get("region")
        try:
            db.create_site(site_code, site_name, region, created_by=login_id)
            flash(f"{site_code} created — empty and ready to configure. Switch to it, then set up Indirect Roles, Cross-Training, and Ambassador Definitions from scratch, same as any other site.", "success")
            return redirect(url_for("site_create"))
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("site_create"))

    return render_template(
        "site_create.html",
        **_login_context(),
        sites=db.get_sites(),
        global_admins=db.get_global_admins(),
    )


@application.route("/site/global-admins/add", methods=["POST"])
def site_global_admin_add():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not (db.is_global_admin(login_id) or login_id in BOOTSTRAP_ADMIN_LOGINS):
        return _deny("Only regional/global admins can manage regional access.")
    new_login = request.form.get("login")
    try:
        db.add_global_admin(new_login, added_by=login_id)
        flash(f"{new_login} now has regional (all-site) access.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("site_create"))


@application.route("/site/global-admins/<login>/remove", methods=["POST"])
def site_global_admin_remove(login):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not (db.is_global_admin(login_id) or login_id in BOOTSTRAP_ADMIN_LOGINS):
        return _deny("Only regional/global admins can manage regional access.")
    db.remove_global_admin(login)
    flash("Regional access removed.", "info")
    return redirect(url_for("site_create"))


@application.route("/regional-overview")
def regional_overview():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if not (db.is_global_admin(login_id) or login_id in BOOTSTRAP_ADMIN_LOGINS):
        return _deny("Only regional/global admins can see the Regional Overview.")
    region = (request.args.get("region") or "").strip() or None
    status = (request.args.get("status") or "").strip() or None
    if status not in ("good", "watch", "serious"):
        status = None
    return render_template(
        "regional_overview.html",
        **_login_context(),
        overview=get_regional_overview(region=region, status=status),
        ld_metrics=LD_METRICS,
        today=date.today(),
    )


@application.route("/ld-management/settings", methods=["GET", "POST"])
def ld_settings():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("L&D Settings is an L&D tool — you don't have access to it.")

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Give the training a name.", "warning")
        else:
            new_id = db.add_training(
                name=name,
                category=request.form.get("category") or None,
                validity_note=request.form.get("validity_note") or None,
            )
            topic_links = _parse_topic_links(request.form.getlist("topics"))
            if topic_links:
                db.set_training_topics(new_id, topic_links)
            flash(f"'{name}' added — it's now selectable when creating training slots.", "success")
        return redirect(url_for("ld_settings", tab="trainings"))

    tab = request.args.get("tab", "users")
    selected_trainer = request.args.get("trainer")
    trainers = db.get_trainers()
    if selected_trainer not in {t["login"] for t in trainers}:
        selected_trainer = None

    ir_upload_counts = None
    if tab == "indirect_roles":
        conn = db.get_db()
        ir_upload_counts = {
            "ir_roster": conn.execute("SELECT COUNT(*) as n FROM ir_roster").fetchone()["n"],
            "ir_dashboard": conn.execute("SELECT COUNT(*) as n FROM ir_dashboard").fetchone()["n"],
            "ir_umbrella": conn.execute("SELECT COUNT(*) as n FROM ir_umbrella").fetchone()["n"],
            "ir_learn": conn.execute("SELECT COUNT(*) as n FROM ir_learn").fetchone()["n"],
        }
        conn.close()

    trainings = db.get_trainings()
    training_topics_map = {t["id"]: db.get_training_topics(t["id"]) for t in trainings}

    return render_template(
        "ld_settings.html",
        **_login_context(),
        tab=tab,
        selected_trainer=selected_trainer,
        shifts=db.SHIFTS,
        trainings=trainings,
        training_topics_map=training_topics_map,
        available_topics=db.get_available_topics(),
        trainers=trainers,
        all_ams=db.get_am_pool(),
        unassigned_ams=db.get_unassigned_ams(),
        ambassador_departments=db.AMBASSADOR_DEPARTMENTS,
        am_trainer_map=db.get_am_to_trainers_map(),
        ops_roles=db.get_ops_structure(),
        admins_and_trainers=db.get_roster_by_roles(list(ADMIN_TIER_ROLES)),
        som_om_roster=db.get_roster_by_roles(["som", "om"]),
        som_department_map={r["login"]: db.get_assigned_departments(r["login"]) for r in db.get_roster_by_roles(["som"])},
        xt_home_departments=db.get_xt_home_departments(),
        am_teamlead_roster=db.get_roster_by_roles(["am", "team_lead"]),
        pending_requests=db.get_pending_role_requests(),
        bootstrap_logins=BOOTSTRAP_ADMIN_LOGINS,
        storage_diagnostics=db.get_storage_diagnostics(),
        ir_upload_counts=ir_upload_counts,
        upload_sections={k: db.SECTIONS[k] for k in db.UPLOADABLE_SECTIONS},
        csv_import_sections={k: db.SECTIONS[k] for k in db.CSV_IMPORT_SECTIONS if k != "planning_indirect_roles"},
        upload_status=db.get_upload_status_by_section([k for k in db.CSV_IMPORT_SECTIONS if k != "planning_indirect_roles"]),
        data_stale_after_days=db.DATA_STALE_AFTER_DAYS,
        freshness=db.data_freshness(),
        xt_freshness=db.xt_hours_freshness(),
    )


def _parse_topic_links(raw_values):
    """Checkbox values come in as 'section|topic' strings — split them back
    into the (section, topic) tuples set_training_topics expects."""
    links = []
    for v in raw_values:
        if "|" in v:
            section, topic = v.split("|", 1)
            links.append((section, topic))
    return links


def _require_admin_tier():
    if not _require_login():
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("You don't have access to change settings.")
    return None


@application.route("/ld-management/settings/trainers/assign", methods=["POST"])
def ld_trainer_assign():
    denied = _require_admin_tier()
    if denied:
        return denied
    trainer_login = request.form.get("trainer_login")
    am_logins = [a for a in request.form.getlist("am_logins") if a]
    if not am_logins and request.form.get("am_login"):  # single-AM fallback
        am_logins = [request.form["am_login"]]
    if trainer_login and am_logins:
        db.assign_ams_to_trainer(trainer_login, am_logins)
        flash(f"Assigned {len(am_logins)} AM{'s' if len(am_logins) != 1 else ''} to {trainer_login}.", "success")
    return redirect(url_for("ld_settings", tab="users", trainer=trainer_login))


@application.route("/ld-management/settings/trainers/bulk-assign", methods=["POST"])
def ld_trainer_bulk_assign():
    denied = _require_admin_tier()
    if denied:
        return denied
    am_logins = request.form.getlist("am_login")
    trainer_logins = request.form.getlist("trainer_login")
    by_trainer = {}
    for am_login, trainer_login in zip(am_logins, trainer_logins):
        if am_login and trainer_login:
            by_trainer.setdefault(trainer_login, []).append(am_login)
    total = 0
    for trainer_login, ams in by_trainer.items():
        db.assign_ams_to_trainer(trainer_login, ams)
        total += len(ams)
    if total:
        flash(f"Assigned {total} AM{'s' if total != 1 else ''} across {len(by_trainer)} trainer{'s' if len(by_trainer) != 1 else ''}.", "success")
    else:
        flash("Pick a trainer for at least one AM before saving.", "warning")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/trainers/reassign", methods=["POST"])
def ld_trainer_reassign():
    denied = _require_admin_tier()
    if denied:
        return denied
    am_login = request.form.get("am_login")
    from_trainer = request.form.get("from_trainer")
    to_trainer = request.form.get("to_trainer")
    if am_login and from_trainer and to_trainer:
        db.reassign_am(am_login, from_trainer, to_trainer)
        flash(f"Moved {am_login} from {from_trainer} to {to_trainer}.", "success")
    return redirect(url_for("ld_settings", tab="users", trainer=from_trainer))


@application.route("/ld-management/settings/trainers/unassign", methods=["POST"])
def ld_trainer_unassign():
    denied = _require_admin_tier()
    if denied:
        return denied
    trainer_login = request.form.get("trainer_login")
    am_login = request.form.get("am_login")
    if trainer_login and am_login:
        db.unassign_am_from_trainer(trainer_login, am_login)
    return redirect(url_for("ld_settings", tab="users", trainer=trainer_login))


@application.route("/ld-management/settings/trainers/bulk-unassign", methods=["POST"])
def ld_trainer_bulk_unassign():
    denied = _require_admin_tier()
    if denied:
        return denied
    trainer_login = request.form.get("trainer_login")
    am_logins = request.form.getlist("am_logins")
    if trainer_login and am_logins:
        for am_login in am_logins:
            db.unassign_am_from_trainer(trainer_login, am_login)
        flash(f"Unassigned {len(am_logins)} AM{'s' if len(am_logins) != 1 else ''} from {trainer_login}.", "info")
    return redirect(url_for("ld_settings", tab="users", trainer=trainer_login))


@application.route("/ld-management/settings/trainers/bulk-reassign", methods=["POST"])
def ld_trainer_bulk_reassign():
    denied = _require_admin_tier()
    if denied:
        return denied
    from_trainer = request.form.get("from_trainer")
    to_trainer = request.form.get("to_trainer")
    am_logins = request.form.getlist("am_logins")
    if from_trainer and to_trainer and am_logins:
        for am_login in am_logins:
            db.reassign_am(am_login, from_trainer, to_trainer)
        flash(f"Moved {len(am_logins)} AM{'s' if len(am_logins) != 1 else ''} from {from_trainer} to {to_trainer}.", "success")
    elif not to_trainer:
        flash("Choose a trainer to move them to.", "warning")
    return redirect(url_for("ld_settings", tab="users", trainer=from_trainer))


@application.route("/ld-management/settings/<int:training_id>/deactivate", methods=["POST"])
def ld_training_deactivate(training_id):
    denied = _require_admin_tier()
    if denied:
        return denied
    db.set_training_active(training_id, False)
    flash("Training deactivated — existing slots keep it, but it won't appear for new ones.", "info")
    return redirect(url_for("ld_settings", tab="trainings"))


@application.route("/ld-management/settings/<int:training_id>/activate", methods=["POST"])
def ld_training_activate(training_id):
    denied = _require_admin_tier()
    if denied:
        return denied
    db.set_training_active(training_id, True)
    return redirect(url_for("ld_settings", tab="trainings"))


@application.route("/ld-management/settings/<int:training_id>/edit", methods=["POST"])
def ld_training_edit(training_id):
    denied = _require_admin_tier()
    if denied:
        return denied
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the training a name.", "warning")
        return redirect(url_for("ld_settings", tab="trainings"))
    db.update_training(
        training_id,
        name=name,
        category=request.form.get("category") or None,
        validity_note=request.form.get("validity_note") or None,
    )
    db.set_training_topics(training_id, _parse_topic_links(request.form.getlist("topics")))
    flash(f"'{name}' updated.", "success")
    return redirect(url_for("ld_settings", tab="trainings"))


@application.route("/ld-management/settings/roles/assign", methods=["POST"])
def ld_role_assign():
    denied = _require_admin_tier()
    if denied:
        return denied
    login = (request.form.get("login") or "").strip()
    role = request.form.get("role")
    shift = request.form.get("shift") or None
    department = (request.form.get("department") or "").strip() or None
    full_name = (request.form.get("full_name") or "").strip() or None
    title = (request.form.get("title") or "").strip() or None
    if not login or role not in ROLES:
        flash("Give a login and pick a valid role.", "warning")
        return redirect(url_for("ld_settings", tab="users"))
    if shift and shift not in db.SHIFTS:
        shift = None
    if login.lower() in BOOTSTRAP_ADMIN_LOGINS:
        flash(f"'{login}' is a bootstrap admin and always has full Admin access — nothing to change.", "info")
        return redirect(url_for("ld_settings", tab="users"))
    db.set_user_role(login, role, assigned_by=session.get("login"), shift=shift, department=department, full_name=full_name, title=title)
    flash(f"'{login}' assigned {ROLES[role]}.", "success")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/roles/auto-detect", methods=["POST"])
def ld_role_auto_detect():
    denied = _require_admin_tier()
    if denied:
        return denied
    counts = db.apply_detected_roles(assigned_by=session.get("login"))
    total_new = sum(counts[k] for k in ("am", "om", "som", "trainer", "team_lead", "learning_manager"))
    if total_new == 0 and counts["reports_to"] == 0 and counts["corrected"] == 0 and counts["revoked"] == 0:
        flash("No new Area Manager / Operations Manager / Senior Operations Manager / Trainer / Team Lead / Learning Manager titles found in the compliance data to assign.", "info")
    else:
        msg = (
            f"Auto-assigned {counts['am']} Area Manager, {counts['om']} Operations Manager, "
            f"{counts['som']} Senior Operations Manager, {counts['trainer']} Trainer, "
            f"{counts['team_lead']} Team Lead, {counts['learning_manager']} Learning Manager role(s), "
            f"and wired up {counts['reports_to']} reporting line(s)."
        )
        if counts["corrected"]:
            msg += f" Corrected {counts['corrected']} previously auto-assigned role(s) based on updated data."
        if counts["revoked"]:
            msg += f" Reverted {counts['revoked']} previously auto-assigned role(s) that no longer match anything in the data."
        if counts["unconfirmed_om"]:
            msg += f" ({counts['unconfirmed_om']} Operations Manager assignment(s) had no title of their own on record — assumed from being listed as an Area Manager's supervisor; worth double-checking.)"
        flash(msg, "success")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/api/am-profile-lookup")
def api_am_profile_lookup():
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    login = request.args.get("login", "")
    return jsonify(db.suggest_am_profile(login))


@application.route("/ld-management/settings/roles/reports-to", methods=["POST"])
def ld_reports_to():
    denied = _require_admin_tier()
    if denied:
        return denied
    login = (request.form.get("login") or "").strip()
    manager_login = (request.form.get("manager_login") or "").strip() or None
    if not login:
        return redirect(url_for("ld_settings", tab="users"))
    if manager_login and manager_login.lower() == login.lower():
        flash("Someone can't report to themselves.", "warning")
        return redirect(url_for("ld_settings", tab="users"))
    db.set_reports_to(login, manager_login)
    if manager_login:
        flash(f"'{login}' now reports to '{manager_login}'.", "success")
    else:
        flash(f"Cleared '{login}''s manager.", "info")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/roles/departments/assign", methods=["POST"])
def ld_assign_department():
    denied = _require_admin_tier()
    if denied:
        return denied
    login = (request.form.get("login") or "").strip()
    department = (request.form.get("department") or "").strip()
    if not login or not department:
        return redirect(url_for("ld_settings", tab="users"))
    db.add_assigned_department(login, department, assigned_by=session.get("login_id"))
    flash(f"Assigned {db.normalize_department(department)} to '{login}'.", "success")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/roles/departments/remove", methods=["POST"])
def ld_remove_department():
    denied = _require_admin_tier()
    if denied:
        return denied
    login = (request.form.get("login") or "").strip()
    department = (request.form.get("department") or "").strip()
    if login and department:
        db.remove_assigned_department(login, department)
        flash(f"Removed {db.normalize_department(department)} from '{login}'.", "info")
    return redirect(url_for("ld_settings", tab="users"))



@application.route("/ld-management/settings/roles/title", methods=["POST"])
def ld_set_title():
    denied = _require_admin_tier()
    if denied:
        return denied
    login = (request.form.get("login") or "").strip()
    title = (request.form.get("title") or "").strip() or None
    if not login:
        return redirect(url_for("ld_settings", tab="users"))
    db.set_title(login, title)
    flash(f"'{login}''s title updated." if title else f"Cleared '{login}''s title.", "success")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/roles/revoke", methods=["POST"])
def ld_role_revoke():
    denied = _require_admin_tier()
    if denied:
        return denied
    login = (request.form.get("login") or "").strip()
    if not login:
        return redirect(url_for("ld_settings", tab="users"))
    if login.lower() in BOOTSTRAP_ADMIN_LOGINS:
        flash(f"'{login}' is a bootstrap admin and can't be revoked from here.", "warning")
        return redirect(url_for("ld_settings", tab="users"))
    db.revoke_user_role(login)
    flash(f"'{login}' no longer has an assigned role — back to pending.", "info")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/roles/requests/<int:request_id>/decide", methods=["POST"])
def ld_role_request_decide(request_id):
    denied = _require_admin_tier()
    if denied:
        return denied
    approve = request.form.get("decision") == "approve"
    role_if_approved = request.form.get("role") or None
    db.decide_role_request(request_id, approve, decided_by=session.get("login"), role_if_approved=role_if_approved)
    flash("Role request approved." if approve else "Role request denied.", "success" if approve else "info")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/settings/roles/bulk-template")
def ld_role_bulk_template():
    denied = _require_admin_tier()
    if denied:
        return denied
    import openpyxl
    from openpyxl.styles import Font
    from io import BytesIO

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Role Assignments"
    ws.append(["login", "role", "shift"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.append(["jdoe", "am", "early"])
    ws.append(["asmith", "trainer", "late"])
    ws.append(["kwong", "learning_manager", ""])
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 14

    legend = wb.create_sheet("Valid role values")
    legend.append(["role key", "label"])
    for cell in legend[1]:
        cell.font = Font(bold=True)
    for key, label in ROLES.items():
        legend.append([key, label])
    legend.column_dimensions["A"].width = 22
    legend.column_dimensions["B"].width = 26

    shift_legend = wb.create_sheet("Valid shift values")
    shift_legend.append(["shift key", "label"])
    for cell in shift_legend[1]:
        cell.font = Font(bold=True)
    for key, label in db.SHIFTS.items():
        shift_legend.append([key, label])
    shift_legend.append(["(blank)", "for Admin / Learning Manager / Senior Operations Manager — leave shift empty"])
    shift_legend.column_dimensions["A"].width = 14
    shift_legend.column_dimensions["B"].width = 60

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return application.response_class(
        buf.read(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=role_assignments_template.xlsx"},
    )


@application.route("/ld-management/settings/roles/bulk-upload", methods=["POST"])
def ld_role_bulk_upload():
    denied = _require_admin_tier()
    if denied:
        return denied
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a .xlsx file to upload.", "warning")
        return redirect(url_for("ld_settings", tab="users"))

    import openpyxl
    from io import BytesIO
    try:
        wb = openpyxl.load_workbook(BytesIO(f.read()), data_only=True)
        ws = wb.worksheets[0]
    except Exception as e:
        flash(f"Couldn't read that file: {e}", "danger")
        return redirect(url_for("ld_settings", tab="users"))

    assigned, skipped = 0, []
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    for row in rows:
        if not row or not row[0]:
            continue
        login = str(row[0]).strip()
        role = str(row[1]).strip() if len(row) > 1 and row[1] else ""
        shift = str(row[2]).strip() if len(row) > 2 and row[2] else None
        if shift and shift not in db.SHIFTS:
            skipped.append(f"{login}: invalid shift '{shift}', assigned role without a shift")
            shift = None
        if not login or role not in ROLES:
            skipped.append(f"{login or '(blank)'}: invalid role '{role}'")
            continue
        if login.lower() in BOOTSTRAP_ADMIN_LOGINS:
            skipped.append(f"{login}: bootstrap admin, can't be changed")
            continue
        db.set_user_role(login, role, assigned_by=session.get("login"), shift=shift)
        assigned += 1

    msg = f"Bulk-assigned {assigned} role{'s' if assigned != 1 else ''}."
    if skipped:
        msg += f" Skipped {len(skipped)}: " + "; ".join(skipped[:8]) + (" …" if len(skipped) > 8 else "")
    flash(msg, "success" if assigned else "warning")
    return redirect(url_for("ld_settings", tab="users"))


@application.route("/ld-management/ambassadors/<shift>", methods=["GET", "POST"])
def ambassador_management(shift):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if shift not in db.SHIFTS:
        flash("Unknown shift.", "danger")
        return redirect(url_for("ambassador_management", shift="early"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can access Ambassador Management.")

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            department = request.form.get("department")
            login = (request.form.get("login") or "").strip()
            full_name = request.form.get("full_name")
            is_process = bool(request.form.get("is_process_ambassador"))
            is_indirect_roles = bool(request.form.get("is_indirect_roles_ambassador"))
            ir_role_ids = [int(r) for r in request.form.getlist("ir_role_config_id") if r.isdigit()]
            if department not in db.AMBASSADOR_DEPARTMENTS or not login:
                flash("Pick a department and enter a login to add an ambassador.", "warning")
            elif not is_process and not is_indirect_roles:
                flash("Pick at least one ambassador type (Process and/or Indirect Roles).", "warning")
            else:
                new_id = db.add_ambassador(department, shift, login, full_name, added_by=login_id,
                                            is_process=is_process, is_indirect_roles=is_indirect_roles)
                if is_indirect_roles and ir_role_ids:
                    db.set_ambassador_indirect_roles(new_id, ir_role_ids, assigned_by=login_id)
                flash(f"'{login}' added as an ambassador for {department}.", "success")
        elif action == "delist":
            ambassador_id = request.form.get("ambassador_id")
            if ambassador_id:
                db.delist_ambassador(int(ambassador_id), delisted_by=login_id)
                flash("Ambassador delisted.", "info")
        elif action == "set_types":
            ambassador_id = request.form.get("ambassador_id")
            is_process = bool(request.form.get("is_process_ambassador"))
            is_indirect_roles = bool(request.form.get("is_indirect_roles_ambassador"))
            if ambassador_id and (is_process or is_indirect_roles):
                db.set_ambassador_types(int(ambassador_id), is_process, is_indirect_roles)
                flash("Ambassador type updated.", "success")
            elif ambassador_id:
                flash("An ambassador needs at least one type.", "warning")
        return redirect(url_for("ambassador_management", shift=shift, type=request.args.get("type", "")))

    type_filter = request.args.get("type") or None
    if type_filter not in (None, "process", "indirect_roles"):
        type_filter = None
    roster = db.get_ambassadors(shift, ambassador_type=type_filter)
    for dept_ambassadors in roster.values():
        for a in dept_ambassadors:
            if a.get("is_indirect_roles_ambassador"):
                a["ir_roles"] = db.get_ambassador_indirect_roles(a["id"])
    return render_template(
        "ambassador_management.html",
        **_login_context(),
        gap_summary=db.get_ambassador_gap_summary(shift),
        shift=shift,
        roster=roster,
        type_filter=type_filter,
        departments=db.AMBASSADOR_DEPARTMENTS,
        shifts=db.SHIFTS,
        ir_role_config=db.get_ir_role_config_rows(),
        ir_role_sections=_ambassador_trainable_ir_sections(),
    )


@application.route("/api/ambassador-ir-roles-form/<int:ambassador_id>")
def api_ambassador_ir_roles_form(ambassador_id):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    conn = db.get_db()
    amb = conn.execute("SELECT * FROM ambassadors WHERE id=?", (ambassador_id,)).fetchone()
    conn.close()
    if not amb:
        return '<div class="empty-state">Ambassador not found.</div>'
    assigned_ids = {r["role_id"] for r in db.get_ambassador_indirect_roles(ambassador_id)}
    sections = _ambassador_trainable_ir_sections()
    return render_template(
        "ambassador_ir_roles_fragment.html",
        ambassador=dict(amb), sections=sections, assigned_ids=assigned_ids,
    )


@application.route("/ld-management/ambassadors/<int:ambassador_id>/indirect-roles", methods=["POST"])
def ambassador_set_indirect_roles(ambassador_id):
    login_id = _require_login()
    if not login_id:
        return jsonify({"ok": False, "error": "Please log in again."}), 401
    if _effective_role() not in ADMIN_TIER_ROLES:
        return jsonify({"ok": False, "error": "Only L&D can access Ambassador Management."}), 403
    role_ids = [int(r) for r in request.form.getlist("ir_role_config_id") if r.isdigit()]
    db.set_ambassador_indirect_roles(ambassador_id, role_ids, assigned_by=login_id)
    roles = db.get_ambassador_indirect_roles(ambassador_id)
    return jsonify({"ok": True, "count": len(roles), "roles": [r["role"] for r in roles]})


@application.route("/ld-management/ambassadors/definitions", methods=["GET", "POST"])
def ambassador_definitions():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can access Ambassador Management.")

    if request.method == "POST":
        departments = request.form.getlist("department")
        shift_keys = request.form.getlist("shift")
        targets = request.form.getlist("target")
        rows = []
        try:
            for i in range(len(departments)):
                rows.append((departments[i], shift_keys[i], int(targets[i] or 0)))
        except (ValueError, IndexError):
            flash("Targets must be whole numbers.", "warning")
            return redirect(url_for("ambassador_definitions"))
        db.bulk_set_ambassador_targets(rows)
        flash("Ambassador targets saved.", "success")
        return redirect(url_for("ambassador_definitions"))

    return render_template(
        "ambassador_definitions.html",
        **_login_context(),
        targets=db.get_ambassador_targets(),
        departments=db.AMBASSADOR_DEPARTMENTS,
        shifts=db.SHIFTS,
        process_definitions=db.get_ambassador_process_definitions(),
    )


@application.route("/ld-management/ambassadors/definitions/processes/add", methods=["POST"])
def ambassador_process_definition_add():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can access Ambassador Management.")
    department = request.form.get("department")
    process = (request.form.get("process") or "").strip()
    if not process:
        flash("Enter a process name.", "warning")
        return redirect(url_for("ambassador_definitions"))
    try:
        db.add_ambassador_process_definition(department, process, added_by=login_id)
        flash(f"{process} is now tracked for {department}.", "success")
    except ValueError as e:
        flash(str(e), "danger")
    return redirect(url_for("ambassador_definitions"))


@application.route("/ld-management/ambassadors/definitions/processes/<int:definition_id>/remove", methods=["POST"])
def ambassador_process_definition_remove(definition_id):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can access Ambassador Management.")
    db.remove_ambassador_process_definition(definition_id)
    flash("Process removed from tracking.", "info")
    return redirect(url_for("ambassador_definitions"))


@application.route("/ld-management/ambassador-meetings", methods=["GET", "POST"])
def ambassador_meetings():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Only L&D can access Ambassador Meetings.")

    shift = request.args.get("shift") if request.method == "GET" else request.form.get("shift")
    if shift not in db.SHIFTS:
        shift = "early"
    week_start = db.week_start_for(request.args.get("week") if request.method == "GET" else request.form.get("week"))

    if request.method == "POST":
        action = request.form.get("action", "attendance")
        group_key = request.form.get("group_key")
        if group_key not in db.AMBASSADOR_MEETING_GROUP_MAP:
            flash("Unknown meeting group.", "warning")
            return redirect(url_for("ambassador_meetings", shift=shift, week=week_start))

        if action == "attendance":
            ambassador_id = request.form.get("ambassador_id")
            status = request.form.get("attendance_status")
            if ambassador_id and status in db.AMBASSADOR_ATTENDANCE_LABELS:
                db.set_ambassador_attendance(
                    week_start, shift, group_key, int(ambassador_id),
                    attendance_status=status,
                    notes=request.form.get("notes") or None,
                    recorded_by=login_id,
                )
                flash("Attendance saved.", "success")
        elif action == "notes":
            db.set_ambassador_meeting_notes(week_start, shift, group_key, request.form.get("notes") or None, login_id)
            flash("Meeting notes saved.", "success")
        elif action == "cancel":
            reason = (request.form.get("cancel_reason") or "").strip()
            if not reason:
                flash("A reason is required to cancel a meeting.", "warning")
            else:
                db.set_ambassador_meeting_status(week_start, shift, group_key, "cancelled", reason, login_id)
                flash("Meeting marked cancelled.", "info")
        elif action == "biweekly":
            db.set_ambassador_meeting_status(week_start, shift, group_key, "biweekly_skip", request.form.get("cancel_reason") or None, login_id)
            flash("Meeting marked as this week's biweekly skip.", "info")
        elif action == "reopen":
            db.set_ambassador_meeting_status(week_start, shift, group_key, "held", None, login_id)
            flash("Meeting reopened.", "info")
        elif action == "upload":
            files = [f for f in request.files.getlist("documents") if f and f.filename]
            if not files:
                flash("Choose at least one file to upload.", "warning")
            else:
                uploaded, rejected = 0, []
                for f in files:
                    ext = os.path.splitext(f.filename)[1].lower()
                    if ext not in db.AMBASSADOR_DOC_ALLOWED_EXT:
                        rejected.append(f.filename)
                        continue
                    stored_name = f"{uuid.uuid4().hex}{ext}"
                    db.storage_save(stored_name, f)
                    db.add_ambassador_meeting_document(
                        week_start, shift, group_key, stored_name, f.filename, f.mimetype, login_id
                    )
                    uploaded += 1
                if uploaded:
                    flash(f"Uploaded {uploaded} file{'s' if uploaded != 1 else ''}.", "success")
                if rejected:
                    flash("Skipped (unsupported type): " + ", ".join(rejected), "warning")
        return redirect(url_for("ambassador_meetings", shift=shift, week=week_start))

    groups = [
        {"key": key, **db.get_ambassador_meeting_roster(week_start, shift, key)}
        for key, _, _ in db.AMBASSADOR_MEETING_GROUPS
    ]

    return render_template(
        "ambassador_meetings.html",
        **_login_context(),
        shift=shift,
        shifts=db.SHIFTS,
        week_start=week_start,
        week_dates=db.week_dates(week_start),
        prev_week=db.adjacent_week(week_start, -1),
        next_week=db.adjacent_week(week_start, 1),
        groups=groups,
        attendance_statuses=db.AMBASSADOR_ATTENDANCE_STATUSES,
        week_label=db.calendar_week_label(week_start),
        image_ext=db.AMBASSADOR_IMAGE_EXT,
    )


@application.route("/ld-management/ambassador-meetings/host/<group_key>")
def ambassador_meeting_host(group_key):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    shift = request.args.get("shift")
    if shift not in db.SHIFTS or group_key not in db.AMBASSADOR_MEETING_GROUP_MAP:
        return "", 404
    week_start = db.week_start_for(request.args.get("week"))
    g = db.get_ambassador_meeting_roster(week_start, shift, group_key)
    return render_template(
        "ambassador_meeting_host_fragment.html",
        g=g, group_key=group_key, shift=shift, shift_label=db.SHIFTS[shift],
        week_start=week_start, week_label=db.calendar_week_label(week_start),
        image_ext=db.AMBASSADOR_IMAGE_EXT,
    )


@application.route("/ambassador-meetings/documents/<int:doc_id>")
def ambassador_meeting_document(doc_id):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    doc = db.get_ambassador_meeting_document(doc_id)
    if not doc:
        return "", 404
    if not db.storage_exists(doc["stored_filename"]):
        return "", 404
    # PDFs and images render inline in the browser; Word/PowerPoint have no
    # native in-browser renderer available offline, so those download
    # instead — inline is still requested so a browser that CAN handle it
    # will try to.
    if db.USE_S3:
        url = db.storage_presigned_url(
            doc["stored_filename"], doc["original_name"], doc["content_type"], inline=True
        )
        return redirect(url)
    return send_file(
        db.storage_local_path(doc["stored_filename"]), mimetype=doc["content_type"] or "application/octet-stream",
        as_attachment=False, download_name=doc["original_name"],
    )


@application.route("/ambassador-meetings/documents/<int:doc_id>/delete", methods=["POST"])
def ambassador_meeting_document_delete(doc_id):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return "", 403
    doc = db.delete_ambassador_meeting_document(doc_id)
    if doc:
        db.storage_delete(doc["stored_filename"])
        flash("Document removed.", "info")
    return redirect(request.referrer or url_for("ambassador_meetings"))


@application.route("/ld-management/<shift>", methods=["GET", "POST"])
def ld_shift(shift):
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if shift not in db.SHIFTS:
        flash("Unknown shift.", "danger")
        return redirect(url_for("ld_shift", shift="early"))

    role = _effective_role()
    week_start = db.week_start_for(request.args.get("week"))
    fc_scope = request.args.get("fc")

    if request.method == "POST":
        if role not in ADMIN_TIER_ROLES:
            return _deny("Only L&D can create training slots.")
        db.add_training_slot(
            shift=shift,
            week_start=week_start,
            day_index=int(request.form.get("day_index", 0)),
            training_id=int(request.form["training_id"]) if request.form.get("training_id") else None,
            start_time=request.form.get("start_time") or None,
            capacity=int(request.form["capacity"]) if request.form.get("capacity") else None,
            instructor=request.form.get("instructor") or None,
            room=request.form.get("room") or None,
            notes=request.form.get("notes") or None,
            created_by=login_id,
        )
        flash("Training slot added to the plan.", "success")
        return redirect(url_for("ld_shift", shift=shift, week=week_start, fc=fc_scope))

    week_dates = db.week_dates(week_start)
    slots = db.get_training_slots(week_start, shift=shift)
    slots_by_day = {i: [] for i in range(7)}
    for s in slots:
        slots_by_day.setdefault(s["day_index"], []).append(s)

    # Pending (no role) users get a read-only board — no create-slot form,
    # no click-to-see-who-needs-it panel (that needs a scope they don't
    # have yet).
    can_edit_plan = role is not None

    return render_template(
        "ld_shift.html",
        **_login_context(),
        shift=shift,
        shifts=db.SHIFTS,
        week_start=week_start,
        prev_week=db.adjacent_week(week_start, -1),
        next_week=db.adjacent_week(week_start, 1),
        this_week=db.week_start_for(),
        week_dates=week_dates,
        day_names=db.DAY_NAMES,
        slots_by_day=slots_by_day,
        trainings=db.get_trainings(active_only=True),
        fcs=db.all_fcs(),
        fc_scope=fc_scope,
        can_edit_plan=can_edit_plan,
        can_create_slots=role in ADMIN_TIER_ROLES,
        de_tech_draft_slots=db.get_draft_training_slots(week_start, shift=shift, metric_key="de_tech") if role in ADMIN_TIER_ROLES else None,
        safety_draft_slots=db.get_draft_training_slots(week_start, shift=shift, metric_key="safety_compliance") if role in ADMIN_TIER_ROLES else None,
        iso_week=db.iso_calendar_week(week_start),
        min_capacity=db.get_trainer_draft_min_capacity() if role in ADMIN_TIER_ROLES else None,
    )


@application.route("/ld-management/slot/<int:slot_id>/delete", methods=["POST"])
def ld_slot_delete(slot_id):
    denied = _require_admin_tier()
    if denied:
        return denied
    slot = db.get_training_slot(slot_id)
    db.delete_training_slot(slot_id)
    flash("Training slot removed.", "info")
    if slot:
        return redirect(url_for("ld_shift", shift=slot["shift"], week=slot["week_start"]))
    return redirect(url_for("ld_management"))


@application.route("/ld-management/de-tech/generate-draft", methods=["POST"])
def de_tech_generate_draft():
    denied = _require_admin_tier()
    if denied:
        return denied
    shift = request.form.get("shift")
    week_start = request.form.get("week_start") or db.week_start_for()
    if shift not in db.SHIFTS:
        flash("Unknown shift.", "danger")
        return redirect(url_for("ld_shift", shift="early", week=week_start))
    login_id = _require_login()
    total_created, total_held_back = 0, 0
    for metric_key in db.TRAINER_PLANNED_METRICS:
        created, held_back = db.generate_trainer_draft(metric_key, shift, week_start, created_by=login_id)
        total_created += len(created)
        total_held_back += held_back
    if total_created:
        msg = f"Draft plan generated — {total_created} slot(s) for you to review, set a time on, and approve."
        if total_held_back:
            msg += f" {total_held_back} more associate(s) are overdue but below the minimum group size — held back until there are enough."
        flash(msg, "success")
    elif total_held_back:
        flash(f"{total_held_back} associate(s) are overdue but below the minimum group size to draft a session yet.", "info")
    else:
        flash("Nobody currently overdue for this shift across Safety Compliance or DE Tech — nothing to draft.", "info")
    return redirect(url_for("ld_shift", shift=shift, week=week_start))


@application.route("/ld-management/de-tech/draft/<int:slot_id>/approve", methods=["POST"])
def de_tech_draft_approve(slot_id):
    denied = _require_admin_tier()
    if denied:
        return jsonify({"ok": False, "error": "not authorized"}), 403
    slot = db.get_training_slot(slot_id)
    if not slot:
        return jsonify({"ok": False, "error": "That draft slot no longer exists."}), 404
    start_time = request.form.get("start_time") or ""
    try:
        db.approve_draft_slot(
            slot_id, start_time, approved_by=_require_login(),
            instructor=request.form.get("instructor") or None,
        )
        return jsonify({"ok": True, "message": f"Approved for {start_time}."})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@application.route("/ld-management/de-tech/draft/<int:slot_id>/discard", methods=["POST"])
def de_tech_draft_discard(slot_id):
    denied = _require_admin_tier()
    if denied:
        return jsonify({"ok": False, "error": "not authorized"}), 403
    db.discard_draft_slot(slot_id)
    return jsonify({"ok": True})


@application.route("/ld-management/de-tech/draft/<int:slot_id>/remove-attendee", methods=["POST"])
def de_tech_draft_remove_attendee(slot_id):
    denied = _require_admin_tier()
    if denied:
        return jsonify({"ok": False, "error": "not authorized"}), 403
    employee_login = request.form.get("employee_login")
    db.remove_slot_attendee_by_login(slot_id, employee_login)
    return jsonify({"ok": True})


@application.route("/ld-management/slot/<int:slot_id>/edit", methods=["POST"])
def ld_slot_edit(slot_id):
    denied = _require_admin_tier()
    if denied:
        return jsonify({"ok": False, "error": "not authorized"}), 403
    slot = db.get_training_slot(slot_id)
    if not slot:
        return jsonify({"ok": False, "error": "That slot no longer exists."}), 404
    training_id = request.form.get("training_id")
    capacity = request.form.get("capacity")
    db.edit_training_slot(
        slot_id,
        training_id=(int(training_id) if training_id else None) if "training_id" in request.form else None,
        start_time=request.form.get("start_time") if "start_time" in request.form else None,
        capacity=(int(capacity) if capacity else None) if "capacity" in request.form else None,
        instructor=request.form.get("instructor") if "instructor" in request.form else None,
        room=request.form.get("room") if "room" in request.form else None,
    )
    return jsonify({"ok": True})


@application.route("/ld-management/de-tech/min-capacity", methods=["POST"])
def de_tech_set_min_capacity():
    denied = _require_admin_tier()
    if denied:
        return denied
    shift = request.form.get("shift", "early")
    week_start = request.form.get("week_start") or db.week_start_for()
    value = request.form.get("min_capacity")
    try:
        new_value = db.set_trainer_draft_min_capacity(value, updated_by=_require_login())
        flash(f"Minimum group size for a draft session set to {new_value}.", "success")
    except (TypeError, ValueError):
        flash("Enter a whole number of 1 or more.", "warning")
    return redirect(url_for("ld_shift", shift=shift, week=week_start))



@application.route("/api/slots/<int:slot_id>/attendees", methods=["POST"])
def api_add_attendee(slot_id):
    login_id = _require_login()
    if not login_id:
        return jsonify({"error": "not signed in"}), 401
    if _effective_role() is None:
        return jsonify({"error": "no role assigned"}), 403
    data = request.get_json(silent=True) or request.form
    employee_login = (data.get("employee_login") or "").strip()
    if not employee_login:
        return jsonify({"error": "employee_login required"}), 400

    slot = db.get_training_slot(slot_id)
    fit = db.check_attendee_fit(slot["training_id"], employee_login) if slot and slot.get("training_id") else {"warning": None, "mismatch": False, "reason": None}
    possible_issue = fit["mismatch"] or bool(fit["warning"])
    confirmed = str(data.get("confirmed") or "").lower() in ("1", "true", "yes")

    # Anyone flagged as a possible mismatch — no record of needing this
    # topic at all, OR already compliant / not due for a while — gets a
    # blocking confirmation before they're actually enrolled, rather than
    # being silently added and only surfaced afterward. Skip straight
    # through when there's nothing to flag, or once the caller has
    # already confirmed.
    if possible_issue and not confirmed:
        return jsonify({
            "ok": True,
            "needs_confirmation": True,
            "warning": fit["warning"],
            "mismatch": fit["mismatch"],
            "mismatch_reason": fit["reason"],
            "reason": fit["reason"] or fit["warning"],
        })

    flagged = possible_issue
    flag_reason = fit["reason"] or fit["warning"]

    attendee_id, was_new = db.add_slot_attendee(
        slot_id=slot_id,
        employee_login=employee_login,
        full_name=data.get("full_name"),
        fc=data.get("fc"),
        added_by=login_id,
        flagged=flagged,
        flag_reason=flag_reason,
    )
    attendees = db.get_training_slots(slot["week_start"], shift=slot["shift"])
    count = next((len(s["attendees"]) for s in attendees if s["id"] == slot_id), None)
    return jsonify({
        "ok": True,
        "slot_id": slot_id,
        "attendee_id": attendee_id,
        "already_enrolled": not was_new,
        "attendee_count": count,
        "day_index": slot["day_index"],
        "start_time": slot["start_time"],
        "warning": fit["warning"],
        "mismatch": flagged,
        "mismatch_reason": flag_reason,
    })


@application.route("/api/slots/<int:slot_id>/needs-training")
def api_needs_training(slot_id):
    login_id = _require_login()
    if not login_id:
        return "", 401
    role = _effective_role()
    if role is None:
        return "", 403

    slot = db.get_training_slot(slot_id)
    if not slot:
        return "", 404

    if role in OPS_SELF_ROLES:
        scope_am, scope_shift = login_id, None
    else:
        scope_am, scope_shift = None, slot["shift"]

    needs = db.get_needs_training_for_slot(slot_id, scope_am=scope_am, scope_shift=scope_shift)
    return render_template(
        "needs_training_fragment.html",
        slot=slot,
        needs=needs,
        day_names=db.DAY_NAMES,
        scope_am=scope_am,
        scope_shift=scope_shift,
    )


@application.route("/api/slots/<int:slot_id>/modal")
def api_slot_modal(slot_id):
    login_id = _require_login()
    if not login_id:
        return "", 401
    role = _effective_role()
    if role is None:
        return "", 403

    slot = db.get_training_slot(slot_id)
    if not slot:
        return "", 404

    if role in OPS_SELF_ROLES:
        scope_am, scope_shift = login_id, None
    else:
        scope_am, scope_shift = None, slot["shift"]

    week_slots = db.get_training_slots(slot["week_start"], shift=slot["shift"])
    full_slot = next((s for s in week_slots if s["id"] == slot_id), slot)
    needs = db.get_needs_training_for_slot(slot_id, scope_am=scope_am, scope_shift=scope_shift)

    return render_template(
        "slot_modal_fragment.html",
        slot=full_slot,
        needs=needs,
        day_names=db.DAY_NAMES,
        scope_am=scope_am,
        scope_shift=scope_shift,
        can_edit_plan=role is not None,
        can_create_slots=role in ADMIN_TIER_ROLES,
        trainings=db.get_trainings(active_only=True) if role in ADMIN_TIER_ROLES else None,
    )


@application.route("/api/slot-attendees/<int:attendee_id>/delete", methods=["POST"])
def api_remove_attendee(attendee_id):
    login_id = _require_login()
    if not login_id:
        return jsonify({"error": "not signed in"}), 401
    if _effective_role() is None:
        return jsonify({"error": "no role assigned"}), 403
    db.remove_slot_attendee(attendee_id)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- upload ----
# The upload UI itself now lives as a tab on L&D Settings (ld_settings.html,
# tab='data') — this route stays as the POST handler + a GET redirect for
# anyone with the old /data link bookmarked.

@application.route("/data", methods=["GET", "POST"])
def data_upload():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Uploading data is an L&D/Trainer function — you don't have access to it.")

    if request.method == "GET":
        return redirect(url_for("ld_settings", tab="data"))

    section = request.form.get("section")
    mode = request.form.get("mode")
    valid_sections = db.UPLOADABLE_SECTIONS if mode != "csv" else db.CSV_IMPORT_SECTIONS
    if section not in valid_sections:
        flash("Choose a valid section.", "danger")
        return redirect(url_for("ld_settings", tab="data"))

    if mode == "csv":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Choose a CSV file first.", "warning")
            return redirect(url_for("ld_settings", tab="data"))
        try:
            if section == "compliance_safety":
                n = db.ingest_safety_compliance_csv(f.read(), f.filename)
            elif section == "planning_de_tech":
                n = db.ingest_de_tech_csv(f.read(), f.filename)
            elif section == "planning_bts":
                n = db.ingest_bts_csv(f.read(), f.filename)
            elif section == "xt_hours":
                n = db.ingest_xt_hours_csv(f.read(), f.filename)
            else:
                n = db.ingest_items_csv(section, f.read(), f.filename)
            flash(f"Imported {n} rows into {db.SECTIONS[section]}.", "success")
            snapshot_olr_metrics_for_all_reviewees()
        except Exception as e:
            flash(f"Import failed: {e}", "danger")
    else:
        db.add_tracked_item(
            section=section,
            employee_login=request.form.get("employee_login"),
            full_name=request.form.get("full_name"),
            fc=request.form.get("fc"),
            am_login=request.form.get("am_login"),
            subcategory=request.form.get("subcategory"),
            status=request.form.get("status") or "Not Started",
            due_date=request.form.get("due_date") or None,
            value=float(request.form["value"]) if request.form.get("value") else None,
            notes=request.form.get("notes"),
        )
        flash(f"Added to {db.SECTIONS[section]}.", "success")
    return redirect(url_for("ld_settings", tab="data"))


@application.route("/ld-management/settings/indirect-roles/upload", methods=["POST"])
def ir_data_upload():
    login_id = _require_login()
    if not login_id:
        return redirect(url_for("login"))
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Uploading data is an L&D/Trainer function — you don't have access to it.")

    source = request.form.get("source")
    ingesters = {
        "ir_roster": db.ingest_ir_roster_csv,
        "ir_dashboard": db.ingest_ir_dashboard_csv,
        "ir_umbrella": db.ingest_ir_umbrella_csv,
        "ir_learn": db.ingest_ir_learn_csv,
    }
    if source not in ingesters:
        flash("Choose a valid data source.", "danger")
        return redirect(url_for("ld_settings", tab="indirect_roles"))
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose a CSV file first.", "warning")
        return redirect(url_for("ld_settings", tab="indirect_roles"))
    try:
        n = ingesters[source](f.read(), f.filename)
        flash(f"Imported {n} rows.", "success")
        snapshot_olr_metrics_for_all_reviewees()
    except Exception as e:
        flash(f"Import failed: {e}", "danger")
    return redirect(url_for("ld_settings", tab="indirect_roles"))


@application.route("/data/<section>/reset", methods=["POST"])
def data_reset(section):
    if _effective_role() not in ADMIN_TIER_ROLES:
        return _deny("Uploading data is an L&D/Trainer function — you don't have access to it.")
    if section == "xt_hours":
        db.reset_xt_hours()
        flash("Cleared all Cross-Training hours-on-function data.", "info")
    elif section in db.UPLOADABLE_SECTIONS:
        db.reset_section(section)
        flash(f"Cleared all records in {db.SECTIONS[section]}.", "info")
    return redirect(url_for("ld_settings", tab="data"))


# ----------------------------------------------------------------- misc ----

@application.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@application.route("/api/tickety-diagnostic")
def tickety_diagnostic():
    """Admin-only connectivity check — creates nothing, just confirms
    whether the SigV4A client initializes and whether the CRT
    dependency is actually present on this instance. Meant for
    verifying the deployment once it's live; this sandbox can't reach
    Tickety's internal endpoint to test the real call itself."""
    if _effective_role() not in ADMIN_TIER_ROLES:
        return jsonify({"ok": False, "error": "Admin access required"}), 403
    client = tickety_client.get_tickety_client()
    if client is None:
        return jsonify({
            "ok": False,
            "error": tickety_client._client_error,
            "hint": "Usually means the awscrt dependency isn't installed, or TicketyPythonSdk isn't loaded — check the EB instance's installed packages.",
        }), 500
    return jsonify({"ok": True, "message": "Tickety SigV4A client initialized successfully. This confirms the client builds correctly — it does not confirm a real ticket can be created (account onboarding, IAM policy, and network reachability to Tickety are separate concerns)."})


if __name__ == "__main__":
    db.set_current_site(db.DEFAULT_SITE_CODE)
    db.init_global_db()
    db.init_db()
    _seed_if_empty()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
