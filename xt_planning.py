"""Cross-training standards: replay past shifts, measure coverage, and
say for every standard whether to keep it, raise it, lower it, train
from a different department, or hire veterans instead.

Pure arithmetic over plain rows — no Flask, no database — for two
reasons. The same code has to run over a site's real tables
(db.load_planning_data) and over the in-memory sample the page can show
before a site has sent its files; and every verdict on the page has to
be reproducible from its inputs in a test, because a recommendation to
change headcount is one a senior will argue with.

The method, in one paragraph, because the page prints it:

    For a standard (process P, shift s, source departments S, target T),
    take every past shift in the window that had P volume. Veterans who
    were in that day produce at the veteran rate for the day's shift
    length. Cross-trainees add what the standard would have given: T of
    them, times the share of their department that was in that day, but
    never more than their department could spare after its own volume.
    The shift meets plan if that covers P's planned units. Coverage is
    the share of replayed shifts that meet plan, and the recommended
    target is the smallest that reaches the bar (95% by default).

What is deliberately not modelled: which individuals were moved (the
replay asks whether enough could have been, not who), learning curves,
and anything that stopped a process for reasons headcount cannot fix.
A shift lost to a conveyor fault reads as a miss here; the bar sitting
at 95% rather than 100% is what absorbs those.
"""
import math
import random
from collections import Counter, defaultdict
from datetime import date, timedelta

SHIFTS = ("early", "late", "night")
DEFAULTS = {
    "coverage_bar": 0.95,    # share of replayed shifts that must meet plan
    "band": 1,               # a target within this of the best is "keep"...
    "band_pct": 10.0,        # ...or within this share of the target, if larger
    "absence_pct": 8.0,      # used where absence cannot be measured
    "window_weeks": 8,
    "shift_hours": 9.5,      # where the calendar has no length for a day
    "similar_band": 0.15,    # "comparable" volume for the day plan's history
}


def _median(values):
    values = sorted(v for v in values if v)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _key(text):
    return (text or "").strip().lower()


class Prepared:
    """The five inputs, indexed once for the replay. Built per request
    (or per sample), never mutated afterwards."""

    def __init__(self, data, today=None, window_weeks=8, shift_hours=9.5):
        self.today = today or date.today()
        self.window_start = (self.today - timedelta(weeks=window_weeks)).isoformat()
        self.window_end = self.today.isoformat()
        self.shift_hours = shift_hours

        # Who: the roster decides who is employed; Hours on Function
        # decides home department and what each person is trained on.
        roster = {_key(r["employee_login"]): r for r in data.get("roster") or []}
        self.has_roster = bool(roster)
        self.home, self.code, self.person_shift = {}, {}, {}
        self.trained = defaultdict(set)          # process -> logins
        self.process_names = {}
        for row in data.get("hours") or []:
            login = _key(row["employee_login"])
            if self.has_roster and login not in roster:
                continue                         # left, or never on the roster
            if row.get("fclm_mapped") and login not in self.home:
                self.home[login] = row["fclm_mapped"]
            if row.get("shift_pattern") and login not in self.code:
                self.code[login] = row["shift_pattern"]
                self.person_shift[login] = row.get("shift")
            process = row.get("merged_function")
            if process:
                self.process_names.setdefault(_key(process), process)
                if row.get("trained_status") == "Trained":
                    self.trained[_key(process)].add(login)
        for login, r in roster.items():
            if r.get("shift_pattern"):
                self.code[login] = r["shift_pattern"]
                self.person_shift[login] = r.get("shift") or self.person_shift.get(login)

        # People per (department, shift): the population a presence
        # share is measured against.
        self.people = defaultdict(set)
        for login, dept in self.home.items():
            shift = self.person_shift.get(login)
            if shift:
                self.people[(_key(dept), shift)].add(login)
        self.departments = {}
        for dept in self.home.values():
            self.departments.setdefault(_key(dept), dept)

        # When: the shift calendar. A code is scheduled on a day if any
        # side of it has a row; the sides agree on 98% of code-days and
        # a person is on their code whichever side the row is filed under.
        self.scheduled = set()
        self.calendar_codes = set()
        lengths = defaultdict(list)
        for row in data.get("calendar") or []:
            self.scheduled.add((row["shift_code"], row["work_date"]))
            self.calendar_codes.add(row["shift_code"])
            if row.get("hours") and row.get("shift"):
                lengths[(row["work_date"], row["shift"])].append(row["hours"])
        self.length_by_day = {k: _median(v) for k, v in lengths.items()}

        # What happened: process totals and plan, and who worked where.
        self.pday = {}
        owner_votes = defaultdict(Counter)
        for row in data.get("process_day") or []:
            p = _key(row["process"])
            self.process_names.setdefault(p, row["process"])
            self.pday[(row["work_date"], row["shift"], p)] = row
            if row.get("department"):
                owner_votes[p][row["department"]] += 1
        self.present = defaultdict(set)
        self.work = defaultdict(list)
        for row in data.get("associate_day") or []:
            if not row.get("hours"):
                continue
            login = _key(row["employee_login"])
            d, s, p = row["work_date"], row["shift"], _key(row["process"])
            self.present[(d, s)].add(login)
            self.work[(d, s, p)].append((login, row.get("units") or 0.0, row["hours"]))

        # Which department owns a process: the file says so where it can;
        # otherwise whichever department most of its trained people
        # belong to — the same derivation the Retention tab uses.
        self.owner = {}
        for p in self.process_names:
            if owner_votes.get(p):
                self.owner[p] = _key(owner_votes[p].most_common(1)[0][0])
            else:
                homes = Counter(_key(self.home[l]) for l in self.trained.get(p, ()) if l in self.home)
                self.owner[p] = homes.most_common(1)[0][0] if homes else None
                if self.owner[p]:
                    self.departments.setdefault(self.owner[p], self.owner[p])
        self.owned = defaultdict(list)
        for p, dept in self.owner.items():
            if dept:
                self.owned[dept].append(p)

        self.days = sorted({(d, s) for (d, s, _p) in self.pday
                            if self.window_start <= d < self.window_end})
        self._rates = {}

    # -- small lookups ----------------------------------------------------

    def length(self, day, shift):
        return self.length_by_day.get((day, shift)) or self.shift_hours

    def is_scheduled(self, login, day):
        """True, False, or None when the person's code is not in the
        calendar at all — unknown is reported, never guessed."""
        code = self.code.get(login)
        if not code or code not in self.calendar_codes:
            return None
        return (code, day) in self.scheduled

    def rate(self, process, group):
        """Units an hour on a process over the window, for a group:
        'vet' (home department owns it), 'xt' (everyone else), or one
        department's key. Returns (rate, hours it rests on)."""
        cache_key = (process, group)
        if cache_key in self._rates:
            return self._rates[cache_key]
        owner = self.owner.get(process)
        units = hours = 0.0
        for (d, s, p), rows in self.work.items():
            if p != process or not (self.window_start <= d < self.window_end):
                continue
            for login, u, h in rows:
                home = _key(self.home.get(login))
                if group == "vet" and home != owner:
                    continue
                if group == "xt" and (home == owner or not home):
                    continue
                if group not in ("vet", "xt", "all") and home != group:
                    continue
                units += u
                hours += h
        result = (round(units / hours, 1) if hours else None, round(hours, 1))
        self._rates[cache_key] = result
        return result

    def plan_rate(self, process, row=None):
        """The rate a day's demand is converted to hours at: the day's own
        plan, else what veterans actually do, else anyone."""
        if row and row.get("plan_rate"):
            return row["plan_rate"]
        return self.rate(process, "vet")[0] or self.rate(process, "all")[0]

    @staticmethod
    def demand(row):
        return (row.get("plan_units") or row.get("units") or 0.0) if row else 0.0

    def dept_spare(self, dept, day, shift):
        """People a department had in, less the people its own processes
        needed that day. Negative means it was short itself."""
        key = ("spare", dept, day, shift)
        if key not in self._rates:
            self._rates[key] = self._dept_spare(dept, day, shift)
        return self._rates[key]

    def _dept_spare(self, dept, day, shift):
        present = sum(1 for l in self.present.get((day, shift), ())
                      if _key(self.home.get(l)) == dept)
        need_hours = 0.0
        for p in self.owned.get(dept, ()):
            row = self.pday.get((day, shift, p))
            rate = self.plan_rate(p, row)
            if row and rate:
                need_hours += self.demand(row) / rate
        return present - need_hours / self.length(day, shift)

    def presence_share(self, dept, day, shift):
        key = ("share", dept, day, shift)
        if key not in self._rates:
            self._rates[key] = self._presence_share(dept, day, shift)
        return self._rates[key]

    def _presence_share(self, dept, day, shift):
        population = self.people.get((dept, shift)) or set()
        if not population:
            return 0.0
        return len(population & self.present.get((day, shift), set())) / len(population)


# -- the replay -------------------------------------------------------------

def _replay_days(prep, process, shift, sources, vet_rate, xt_rates):
    """Everything about each past shift that does not depend on the
    target, computed once so the coverage curve is cheap."""
    owner = prep.owner.get(process)
    days = []
    for d, s in prep.days:
        if s != shift:
            continue
        row = prep.pday.get((d, s, process))
        demand = prep.demand(row)
        if not demand:
            continue
        length = prep.length(d, s)
        vets = sum(1 for l in prep.present.get((d, s), ())
                   if _key(prep.home.get(l)) == owner
                   and (l in prep.trained.get(process, ()) or not prep.trained.get(process)))
        # A department that owns several processes (RSP: Pick and Stow)
        # splits its people between them; counting every RSP veteran on
        # Pick and again on Stow would cover both twice over. The split
        # is by each process's share of the department's hours that day.
        own_hours = {}
        for q in prep.owned.get(owner, ()):
            q_row = prep.pday.get((d, s, q))
            q_rate = prep.plan_rate(q, q_row)
            if q_row and q_rate:
                own_hours[q] = prep.demand(q_row) / q_rate
        if own_hours.get(process) and sum(own_hours.values()):
            vets = vets * own_hours[process] / sum(own_hours.values())
        actual_rate = (row["units"] / row["hours"]) if row.get("units") and row.get("hours") else None
        missed = bool((row.get("plan_units") and (row.get("units") or 0) < row["plan_units"] * 0.999)
                      or (row.get("plan_rate") and actual_rate and actual_rate < row["plan_rate"]))
        xt_working = Counter(_key(prep.home.get(l)) for l, _u, h in prep.work.get((d, s, process), ())
                             if h and _key(prep.home.get(l)) != owner)
        days.append({
            "day": d, "demand": demand, "length": length,
            "vet_output": vets * length * (vet_rate or 0.0), "vets": vets,
            "share": {S: prep.presence_share(S, d, s) for S in sources},
            "spare": {S: prep.dept_spare(S, d, s) for S in sources},
            "rate": xt_rates, "missed": missed,
            "xt_working": sum(xt_working.values()), "xt_by_dept": xt_working,
        })
    return days


def _meets(day, target, weights, extra_vets=0, owner_share=0.0, vet_rate=0.0):
    output = day["vet_output"] + extra_vets * owner_share * day["length"] * vet_rate
    for S, w in weights.items():
        lend = min(target * w * day["share"][S], max(0.0, day["spare"][S]))
        output += lend * day["length"] * (day["rate"].get(S) or 0.0)
    return output >= day["demand"]


def _coverage(days, target, weights, **extra):
    if not days:
        return None
    return sum(1 for d in days if _meets(d, target, weights, **extra)) / len(days)


def _min_target(days, weights, cap, bar):
    """Smallest target reaching the bar, or None if even `cap` does not.
    Coverage never falls as the target rises, so the first hit is it."""
    top = _coverage(days, cap, weights)
    if top is None or top < bar:
        return None, top
    low, high = 0, cap
    while low < high:
        mid = (low + high) // 2
        if _coverage(days, mid, weights) >= bar:
            high = mid
        else:
            low = mid + 1
    return low, _coverage(days, low, weights)


def evaluate_cell(prep, process, shift, current_target, sources, settings):
    """One process on one shift: the verdict and the evidence for it."""
    # The tolerance grows with the target: one person either way is noise
    # on a standard of 5 and on one of 50 alike, but 10% of 50 is not.
    bar = settings["coverage_bar"]
    band = max(settings["band"], round(current_target * settings.get("band_pct", 10.0) / 100.0))
    owner = prep.owner.get(process)
    vet_rate = prep.rate(process, "vet")[0] or prep.rate(process, "all")[0]
    xt_all = prep.rate(process, "xt")[0]
    assumed = []

    def xt_rate(S):
        r = prep.rate(process, S)[0]
        if r is None:
            assumed.append(prep.departments.get(S, S))
        return r or xt_all or vet_rate

    source_keys = [_key(S) for S in sources if _key(S) and _key(S) != owner] or []
    raw = {S: w for S, w in _weights(sources).items() if S in source_keys}
    weights = {S: w / sum(raw.values()) for S, w in raw.items()} if raw and sum(raw.values()) else raw
    rates = {S: xt_rate(S) for S in source_keys}
    days = _replay_days(prep, process, shift, source_keys, vet_rate, rates)
    cell = {
        "process": prep.process_names.get(process, process), "shift": shift,
        "owner": prep.departments.get(owner, owner), "target": current_target,
        "sources": [prep.departments.get(S, S) for S in source_keys],
        "days": len(days), "vet_rate": vet_rate, "xt_rate": xt_all,
    }
    if not days or not vet_rate:
        cell.update(verdict="no_data", coverage=None,
                    why_missing="no volume for this process and shift in the window"
                    if not days else "no hours worked on it, so no rate")
        return cell

    cap = sum(len(prep.people.get((S, shift), ())) for S in source_keys)
    coverage = _coverage(days, current_target, weights) if source_keys else _coverage(days, 0, {})
    best, best_cov = _min_target(days, weights, cap, bar) if source_keys else (
        (0, coverage) if coverage >= bar else (None, coverage))
    lend_days = {prep.departments.get(S, S): sum(1 for d in days if d["spare"][S] >= 1)
                 for S in source_keys}
    cell.update(
        coverage=coverage, best=best, best_coverage=best_cov, cap=cap,
        missed=sum(1 for d in days if d["missed"]),
        peak_xt=max(d["xt_working"] for d in days),
        lend_days=lend_days, assumed_rates=sorted(set(assumed)),
        curve=[(t, round(_coverage(days, t, weights) or 0, 3))
               for t in range(0, min(cap, max(current_target, best or 0) + 6) + 1)] if source_keys else [],
    )

    # Could another department do it with fewer people trained? Only a
    # department with people on this shift, that is not the owner and
    # not already a source.
    alternative = None
    for S, population in prep.people.items():
        dept, s = S
        # A current source alone is an alternative too when there are
        # several: "train only from Receive" is a switch worth proposing
        # when RSP, the other source, can never spare anyone.
        if s != shift or dept == owner or not population or (dept in source_keys and len(source_keys) == 1):
            continue
        alt_rates = {dept: xt_rate(dept)}
        alt_days = _replay_days(prep, process, shift, [dept], vet_rate, alt_rates)
        t, cov = _min_target(alt_days, {dept: 1.0}, len(population), bar)
        if t is None:
            continue
        lends = sum(1 for d in alt_days if d["spare"][dept] >= 1)
        if alternative is None or (t, -lends) < (alternative["target"], -alternative["lend_days"]):
            alternative = {"department": prep.departments.get(dept, dept), "target": t,
                           "coverage": cov, "lend_days": lends}

    if best is None:
        if alternative:
            verdict = "switch"
        else:
            verdict = "hire"
            owner_share = sum(prep.presence_share(owner, d["day"], shift) for d in days) / len(days)
            for extra in range(1, 400):
                if _coverage(days, cap, weights, extra_vets=extra, owner_share=owner_share,
                             vet_rate=vet_rate) >= bar:
                    cell["hire"] = extra
                    break
    elif coverage < bar:
        verdict = "switch" if alternative and alternative["target"] < best - band else "increase"
    elif max(best, cell["peak_xt"]) < current_target - band:
        # Never below the most cross-trainees the floor actually used on
        # one shift: the replay says fewer would have done, but a target
        # under what was really deployed is one the next peak breaks.
        verdict = "decrease"
        best = max(best, cell["peak_xt"])
        cell["best"], cell["best_coverage"] = best, _coverage(days, best, weights)
    else:
        verdict = "keep"
    cell["verdict"] = verdict
    cell["alternative"] = alternative if verdict == "switch" else None
    cell["new_target"] = {"increase": best, "decrease": best,
                          "switch": alternative["target"] if alternative else None}.get(verdict)
    return cell


def _weights(sources):
    """How a combined standard's target is split across its sources:
    in proportion to what each was given, equally where unstated."""
    if isinstance(sources, dict):
        total = sum(sources.values()) or len(sources)
        return {_key(k): (v / total if total else 1 / len(sources)) for k, v in sources.items()}
    keys = [_key(s) for s in sources if _key(s)]
    return {k: 1 / len(keys) for k in keys} if keys else {}


def evidence(cell):
    """The sentences behind a change, as computed facts. These are what
    the page shows without a language model, and the only figures one is
    allowed to quote."""
    lines = []
    n = cell["days"]
    if cell.get("missed") is not None:
        lines.append(f"{cell['missed']} of {n} replayed shifts missed plan.")
    for dept, k in (cell.get("lend_days") or {}).items():
        lines.append(f"After covering its own volume, {dept} could lend on {k} of {n} shifts.")
    alt = cell.get("alternative")
    if alt:
        lines.append(f"{alt['department']} could lend on {alt['lend_days']} of {n} shifts, and a target "
                     f"of {alt['target']} from there covers {round(alt['coverage'] * 100)}% of them.")
    if cell["verdict"] in ("increase", "decrease") and cell.get("best") is not None:
        lines.append(f"A target of {cell['best']} covers {round(cell['best_coverage'] * 100)}% of "
                     f"replayed shifts; {cell['target']} covers {round(cell['coverage'] * 100)}%.")
    if cell["verdict"] == "decrease":
        lines.append(f"Peak use was {cell['peak_xt']} cross-trainees on one shift.")
    if cell["verdict"] == "hire":
        lines.append(f"Even with all {cell['cap']} people in the source departments trained, coverage "
                     f"reaches {round((cell.get('best_coverage') or 0) * 100)}%. About "
                     f"{cell.get('hire', '?')} more veterans in {cell['owner']} would reach the bar.")
    if cell.get("assumed_rates"):
        lines.append("No history of " + ", ".join(cell["assumed_rates"]) +
                     " on this process, so their rate is taken as the cross-trainee average.")
    return lines


def evaluate(prep, standards, settings, department=None, shift=None):
    """Every cell. `standards` is the tool's defined paths: dicts with
    process, shift, target, source_departments."""
    settings = dict(DEFAULTS, **(settings or {}))
    cells = {}
    for path in standards:
        p = _key(path.get("process"))
        if not p or path.get("shift") not in SHIFTS:
            continue
        prep.process_names.setdefault(p, path["process"])
        cell = cells.setdefault((p, path["shift"]), {"target": 0, "sources": defaultdict(float),
                                                     "labels": []})
        cell["target"] += path.get("target") or 0
        cell["labels"].append(path.get("label"))
        srcs = [s for s in (path.get("source_departments") or []) if s]
        for s in srcs:
            cell["sources"][s] += (path.get("target") or 0) / len(srcs) or 1
    results = []
    for (p, s), info in cells.items():
        owner = prep.owner.get(p)
        if department and _key(department) != owner:
            continue
        if shift and shift != s:
            continue
        result = evaluate_cell(prep, p, s, int(round(info["target"])), dict(info["sources"]), settings)
        result["labels"] = info["labels"]
        result["evidence"] = evidence(result)
        result["id"] = f"{p}|{s}"
        results.append(result)
    processes = sorted({r["process"] for r in results}, key=lambda x: (prep.departments.get(prep.owner.get(_key(x)), "") or "", x))
    changes = [r for r in results if r["verdict"] in ("increase", "decrease", "switch", "hire")]
    order = {"hire": 0, "increase": 1, "switch": 2, "decrease": 3}
    changes.sort(key=lambda r: (order[r["verdict"]], r["process"], SHIFTS.index(r["shift"])))
    return {
        "cells": {(r["process"], r["shift"]): r for r in results},
        "processes": processes,
        "shifts": [x for x in SHIFTS if not shift or x == shift],
        "standards": len(results),
        "changes": changes,
        "kept": sum(1 for r in results if r["verdict"] == "keep"),
        "no_data": sum(1 for r in results if r["verdict"] == "no_data"),
        "settings": settings,
        "window": (prep.window_start, prep.window_end),
        "replayed_days": len(prep.days),
    }


# -- one day, planned ahead ---------------------------------------------------

def measured_absence(prep, dept, shift):
    """Unplanned absence for a department on a shift, measured: of the
    person-days its people were scheduled in the window, the share with
    no hours booked. None when the calendar or the hours cannot say."""
    scheduled = absent = 0
    for login in prep.people.get((dept, shift), ()):
        code = prep.code.get(login)
        if code not in prep.calendar_codes:
            continue
        for d, s in prep.days:
            if s != shift or (code, d) not in prep.scheduled:
                continue
            scheduled += 1
            if login not in prep.present.get((d, s), ()):
                absent += 1
    return (100.0 * absent / scheduled, scheduled) if scheduled >= 20 else (None, scheduled)


def day_plan(prep, process, shift, day, volume, rate=None, absence_pct=None,
             other_volumes=None, settings=None):
    """How many veterans and cross-trainees there will be on one day, at
    what rates, what the lenders can spare after their own volume, what
    past days like it looked like, and the cheapest mix that covers it."""
    settings = dict(DEFAULTS, **(settings or {}))
    p = _key(process)
    owner = prep.owner.get(p)
    day_s = day.isoformat() if isinstance(day, date) else day
    length = prep.length(day_s, shift)
    weekday = date.fromisoformat(day_s).weekday()
    other_volumes = {(_key(k)): v for k, v in (other_volumes or {}).items()}

    def absence(dept):
        if absence_pct is not None:
            return absence_pct, "entered"
        measured, basis = measured_absence(prep, dept, shift)
        return (measured, "measured") if measured is not None else (settings["absence_pct"], "default")

    def headcount(logins, dept):
        sched = unknown = 0
        for l in logins:
            state = prep.is_scheduled(l, day_s)
            if state is None:
                unknown += 1
            elif state:
                sched += 1
        pct, how = absence(dept)
        return {"trained": len(logins), "scheduled": sched, "unknown": unknown,
                "absence_pct": round(pct, 1), "absence_basis": how,
                "available": round(sched * (1 - pct / 100.0), 1)}

    on_shift = lambda dept: {l for l in prep.people.get((dept, shift), ())}
    trained = prep.trained.get(p, set())
    vets = headcount(sorted(trained & on_shift(owner)), owner) if owner else None
    vet_rate = rate or prep.rate(p, "vet")[0] or prep.rate(p, "all")[0]
    xt_all = prep.rate(p, "xt")[0]

    lenders = []
    for dept in sorted({_key(prep.home[l]) for l in trained if l in prep.home} - {owner}):
        people = trained & on_shift(dept)
        if not people:
            continue
        hc = headcount(sorted(people), dept)
        dept_rate, basis = prep.rate(p, dept)
        # Its own work that day: the volume entered, else what its
        # processes averaged on the same weekday in the window.
        own = []
        need_hours = 0.0
        for q in prep.owned.get(dept, ()):
            if dept in other_volumes and len(prep.owned[dept]) == 1:
                vol, how = other_volumes[dept], "entered"
            elif (dept + "|" + q) in other_volumes:
                vol, how = other_volumes[dept + "|" + q], "entered"
            else:
                samples = [prep.demand(prep.pday.get((d, shift, q))) for d, s in prep.days
                           if s == shift and date.fromisoformat(d).weekday() == weekday
                           and prep.pday.get((d, shift, q))]
                vol, how = (round(sum(samples) / len(samples)) if samples else 0), "same-weekday average"
            q_rate = prep.plan_rate(q)
            own.append({"process": prep.process_names.get(q, q), "volume": vol, "basis": how,
                        "rate": q_rate})
            if q_rate:
                need_hours += vol / q_rate
        everyone = headcount(sorted(on_shift(dept)), dept)
        need_heads = need_hours / length
        spare = everyone["available"] - need_heads
        lenders.append({
            "department": prep.departments.get(dept, dept), "key": dept, **hc,
            "rate": dept_rate or xt_all or vet_rate, "rate_hours": basis,
            "rate_assumed": dept_rate is None,
            "own_work": own, "own_need_heads": round(need_heads, 1),
            "people_available": everyone["available"], "spare": round(spare, 1),
            "deployable": max(0, math.floor(min(hc["available"], max(0.0, spare)))),
        })

    # The cheapest mix: veterans first (no move, no training), then the
    # lenders with the most to spare at the best rate.
    covered = (vets["available"] if vets else 0) * length * (vet_rate or 0)
    remaining = max(0.0, volume - covered)
    plan = []
    for lender in sorted(lenders, key=lambda x: (-(x["rate"] or 0) * (x["deployable"] > 0), -x["deployable"])):
        if remaining <= 0 or not lender["deployable"] or not lender["rate"]:
            continue
        heads = min(lender["deployable"], math.ceil(remaining / (length * lender["rate"])))
        plan.append({"department": lender["department"], "heads": heads})
        remaining -= heads * length * lender["rate"]
    shortfall_units = max(0.0, remaining)
    hire = math.ceil(shortfall_units / (length * vet_rate * (1 - absence(owner)[0] / 100.0))) \
        if shortfall_units and vet_rate and owner else 0

    # Past days like this one: same shift, volume within the band.
    history = []
    for d, s in prep.days:
        row = prep.pday.get((d, s, p))
        if s != shift or not row:
            continue
        demand = prep.demand(row)
        if not demand or abs(demand - volume) > volume * settings["similar_band"]:
            continue
        rows = prep.work.get((d, s, p), ())
        by_dept = Counter(_key(prep.home.get(l)) for l, _u, h in rows if h)
        met = not ((row.get("plan_units") and (row.get("units") or 0) < row["plan_units"] * 0.999)
                   or (row.get("plan_rate") and row.get("hours") and
                       (row.get("units") or 0) / row["hours"] < row["plan_rate"]))
        history.append({"day": d, "volume": round(demand), "met": met,
                        "vets": by_dept.get(owner, 0),
                        "xt": {prep.departments.get(k, k): v for k, v in by_dept.items() if k != owner}})
    met_days = [h for h in history if h["met"]]
    typical = None
    if met_days:
        xt_mean = Counter()
        for h in met_days:
            xt_mean.update(h["xt"])
        typical = {"vets": round(sum(h["vets"] for h in met_days) / len(met_days), 1),
                   "xt": {k: round(v / len(met_days), 1) for k, v in xt_mean.most_common()}}
    return {
        "process": prep.process_names.get(p, process), "owner": prep.departments.get(owner, owner),
        "shift": shift, "day": day_s, "weekday": date.fromisoformat(day_s).strftime("%A"),
        "volume": round(volume), "length": round(length, 2),
        "length_from_calendar": (day_s, shift) in prep.length_by_day,
        "vets": vets, "vet_rate": vet_rate, "xt_rate": xt_all,
        "hours_needed": round(volume / vet_rate, 1) if vet_rate else None,
        "vet_units": round(covered), "lenders": lenders, "plan": plan,
        "xt_heads": sum(x["heads"] for x in plan),
        "shortfall_units": round(shortfall_units), "hire": hire,
        "history": history, "history_met": len(met_days), "typical": typical,
    }


# -- the sample ----------------------------------------------------------------

DEFAULT_SITE = {
    # process: owning department. SCN2's own vocabulary, used only when a
    # site has not loaded Hours on Function yet.
    "processes": {"Pack Singles": "Pack Singles", "Pack Multis": "Pack Multis", "Pick": "RSP",
                  "Stow Each Nike": "RSP", "Each Receive": "Receive", "Chutings": "Chutings",
                  "SLAM KO": "Ship Dock"},
    "departments": ["Pack Singles", "Pack Multis", "RSP", "Receive", "Chutings", "Ship Dock", "OBPS"],
}


def sample_data(standards, owners=None, today=None, weeks=12, seed=7):
    """A complete, made-up site — roster, Hours on Function, shift
    calendar, process day and associate day — shaped exactly like the
    real files, so the page can be seen working before a site has sent
    its own. People are invented (logins 'smp00001'…): a performance
    figure must never be attached to a real person's login, even as a
    demonstration. Department and process names, and the standards, are
    the site's own where it has them, so the grid reads like home."""
    rnd = random.Random(seed)
    today = today or date.today()
    owners = dict(owners or {})
    for path in standards:
        if path.get("process") and path["process"] not in owners:
            owners[path["process"]] = DEFAULT_SITE["processes"].get(path["process"])
    if not owners:
        owners = dict(DEFAULT_SITE["processes"])
    for process, dept in list(owners.items()):
        if not dept:
            owners[process] = process        # a process nobody owns becomes its own department
    departments = sorted(set(owners.values()) | {s for p in standards for s in (p.get("source_departments") or [])}
                         | set(DEFAULT_SITE["departments"]))
    # Every department has work of its own. One that owned nothing would
    # look idle every day and be the answer to every "who can lend?",
    # which says something about the sample, not about cross-training.
    by_owner = {d: p for p, d in DEFAULT_SITE["processes"].items()}
    for dept in departments:
        if dept not in owners.values():
            name = by_owner.get(dept)
            owners[name if name and name not in owners else f"{dept} work"] = dept

    plan_rate = {p: rnd.choice([95, 110, 125, 140]) for p in owners}
    weekday_factor = [1.1, 1.05, 1.0, 1.0, 1.05, 0.92, 0.88]
    hours_per_shift = {"early": 8.75, "late": 8.75, "night": 8.5}
    presence = 5 / 7 * 0.93                    # five days in seven, 7% absent

    # Codes: three per shift, each working five days and resting on two
    # consecutive ones, staggered so the site runs seven days.
    codes = {s: [f"{s[0].upper()}SMP{k}" for k in range(3)] for s in SHIFTS}
    rest = {code: {(k * 2 + i) % 7 for i in range(2)} for s in SHIFTS for k, code in enumerate(codes[s])}

    # People: every department staffs each process it owns on every shift,
    # plus enough over that to lend what its standards ask of it — a
    # department named as the source of 50 cross-trainees is, on a real
    # site, one big enough to have them.
    lends = defaultdict(float)
    for path in standards:
        sources = [x for x in path.get("source_departments") or [] if x and x != owners.get(path.get("process"))]
        for src in sources:
            lends[(src, path.get("shift"))] += (path.get("target") or 0) / len(sources)
    roster, hours_rows, people = [], [], defaultdict(list)
    per_process = {}
    counter = 0
    for dept in departments:
        processes = [p for p, d in owners.items() if d == dept]
        for s in SHIFTS:
            for p in processes:
                per_process[(p, s)] = rnd.randint(22, 40)
            size = sum(per_process[(p, s)] for p in processes) + int(lends[(dept, s)] * rnd.uniform(0.9, 1.5))
            for _ in range(size):
                counter += 1
                login = f"smp{counter:05d}"
                code = rnd.choice(codes[s])
                people[(dept, s)].append(login)
                roster.append({"employee_login": login, "shift_pattern": code, "shift": s})
                for p in processes:
                    hours_rows.append({"employee_login": login, "fclm_mapped": dept, "merged_function": p,
                                       "trained_status": "Trained", "shift_pattern": code, "shift": s})
    code_of = {r["employee_login"]: r["shift_pattern"] for r in roster}

    # Cross-training, as the standards describe it, and each standard's
    # target per process and shift.
    targets = defaultdict(float)
    for path in standards:
        p, s = path.get("process"), path.get("shift")
        if not p or s not in SHIFTS:
            continue
        targets[(p, s)] += path.get("target") or 0
        sources = [x for x in path.get("source_departments") or [] if x and x != owners.get(p)]
        for src in sources:
            pool = people.get((src, s), [])
            n = min(len(pool), max(2, int((path.get("target") or 6) / max(1, len(sources)) * rnd.uniform(0.7, 1.1))))
            for login in rnd.sample(pool, n):
                hours_rows.append({"employee_login": login, "fclm_mapped": src, "merged_function": p,
                                   "trained_status": "Trained", "shift_pattern": code_of[login], "shift": s})
    trained = defaultdict(set)
    home = {}
    for r in hours_rows:
        home[r["employee_login"]] = r["fclm_mapped"]
        if r.get("merged_function"):
            trained[r["merged_function"]].add(r["employee_login"])

    # Demand. A process with a standard needs its veterans plus roughly
    # its target's worth of cross-trainees — times a factor that leaves
    # most standards about right and some clearly too high or too low,
    # because that mix is what a real site shows. A process nobody
    # cross-trains into keeps its own people at 55–90% busy, which is
    # what decides whether its department can lend.
    demand_mean = {}
    for (p, s), n in per_process.items():
        rate, length = plan_rate[p], hours_per_shift[s]
        own = n * presence * length * rate
        if targets.get((p, s)):
            factor = rnd.choice([0.4, 1.0, 1.0, 1.0, 1.0, 1.7])
            xt = targets[(p, s)] * presence * 0.82 * length * rate * factor * 0.92
            demand_mean[(p, s)] = own * 0.85 + xt
        else:
            demand_mean[(p, s)] = own * rnd.uniform(0.55, 0.9)

    start = today - timedelta(weeks=weeks)
    calendar, process_day, associate_day = [], [], []
    personal = {l: rnd.uniform(0.92, 1.1) for l in home}
    for offset in range((today - start).days + 28):
        d = start + timedelta(days=offset)
        ds = d.isoformat()
        for s in SHIFTS:
            for code in codes[s]:
                if d.weekday() not in rest[code]:
                    calendar.append({"shift_code": code, "side": "OB", "shift": s, "work_date": ds,
                                     "hours": hours_per_shift[s]})
        if d >= today:
            continue                          # the calendar runs ahead; history does not
        for s in SHIFTS:
            length = hours_per_shift[s]
            present = {l for (dept, sh), logins in people.items() if sh == s for l in logins
                       if d.weekday() not in rest[code_of[l]] and rnd.random() > 0.07}
            busy = set()
            demand = {p: round(demand_mean[(p, s)] * weekday_factor[d.weekday()] * rnd.uniform(0.88, 1.12))
                      for p in owners if (p, s) in demand_mean}
            done = defaultdict(float)
            worked = defaultdict(float)

            def book(login, p, factor):
                u = length * plan_rate[p] * personal[login] * factor
                associate_day.append({"work_date": ds, "shift": s, "employee_login": login, "process": p,
                                      "units": round(u), "hours": length})
                busy.add(login)
                done[p] += u
                worked[p] += length
            # First every department covers its own work with its own people...
            for p in demand:
                crew = [l for l in present if home[l] == owners[p] and l in trained[p] and l not in busy]
                rnd.shuffle(crew)
                for l in crew:
                    if done[p] >= demand[p]:
                        break
                    book(l, p, 1.0)
            # ...then what is left is covered by cross-trainees whose own
            # department did not need them, as far as there are any.
            for p in demand:
                movers = [l for l in present if home[l] != owners[p] and l in trained[p] and l not in busy]
                rnd.shuffle(movers)
                for l in movers:
                    if done[p] >= demand[p]:
                        break
                    book(l, p, 0.82)
                process_day.append({"work_date": ds, "shift": s, "process": p, "department": owners[p],
                                    "units": round(done[p]), "hours": round(worked[p], 1),
                                    "plan_units": demand[p], "plan_rate": plan_rate[p]})
            # Everyone else who came in booked indirect time. A real
            # associate-day file should carry these hours too: without
            # them a person who came in and was not needed on a process
            # looks absent, and their department looks shorter than it was.
            for l in present - busy:
                associate_day.append({"work_date": ds, "shift": s, "employee_login": l,
                                      "process": "Indirect", "units": 0, "hours": length})
    return {"roster": roster, "hours": hours_rows, "calendar": calendar,
            "process_day": process_day, "associate_day": associate_day}
