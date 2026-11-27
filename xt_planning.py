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
WEEKDAY_NAMES = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")
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
        # SSPOT marks some day codes "Central" (a 10:45 start at SCN2).
        # The volume files only know early/late/night, so Central is a
        # re-cut of people, not of processes: a person on a Central code
        # that day is reported under Central in the labour mix.
        self.central = set()
        lengths = defaultdict(list)
        for row in data.get("calendar") or []:
            self.scheduled.add((row["shift_code"], row["work_date"]))
            self.calendar_codes.add(row["shift_code"])
            if (row.get("pattern") or "").strip().lower() == "central":
                self.central.add((row["shift_code"], row["work_date"]))
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
        # Lending rules a manager has set ("RSP cannot lend on Mondays"):
        # (department key, shift or "*", weekday). Set once, before any
        # figure is computed; the caches below assume they do not change.
        self.lend_blocks = set()
        for rule in data.get("lend_rules") or []:
            for wd in rule.get("weekdays") or range(7):
                self.lend_blocks.add((_key(rule.get("department")), rule.get("shift") or "*", int(wd)))

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
        plan, else the plan the process usually carries, else what
        veterans actually do, else anyone."""
        if row and row.get("plan_rate"):
            return row["plan_rate"]
        cache = self.__dict__.setdefault("_usual_plan", {})
        if process not in cache:
            cache[process] = _median([r.get("plan_rate") for (d, s, p), r in self.pday.items()
                                      if p == process and r.get("plan_rate")])
        return cache[process] or self.rate(process, "vet")[0] or self.rate(process, "all")[0]

    @staticmethod
    def demand(row):
        return (row.get("plan_units") or row.get("units") or 0.0) if row else 0.0

    def blocked(self, dept, shift, day):
        """Whether a rule says this department does not lend on this
        shift and weekday."""
        if not self.lend_blocks:
            return False
        wd = (date.fromisoformat(day) if isinstance(day, str) else day).weekday()
        return (dept, shift, wd) in self.lend_blocks or (dept, "*", wd) in self.lend_blocks

    def dept_spare(self, dept, day, shift):
        """People a department had in, less the people its own processes
        needed that day. Negative means it was short itself; a rule that
        it does not lend that day caps it at nothing to spare."""
        key = ("spare", dept, day, shift)
        if key not in self._rates:
            spare = self._dept_spare(dept, day, shift)
            self._rates[key] = min(spare, 0.0) if self.blocked(dept, shift, day) else spare
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

def _replay_days(prep, process, shift, sources, vet_rate, xt_rates, keep_day=None):
    """Everything about each past shift that does not depend on the
    target, computed once so the coverage curve is cheap."""
    owner = prep.owner.get(process)
    days = []
    for d, s in prep.days:
        if s != shift or (keep_day and not keep_day(d)):
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
            "owner_share": prep.presence_share(owner, d, s),
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


def _days(prep, process, shift, sources, vet_rate, rates, scenario, keep_day=None):
    if scenario is None:
        return _replay_days(prep, process, shift, sources, vet_rate, rates, keep_day)
    return scenario_days(prep, process, shift, sources, vet_rate, rates, scenario)


# -- context the arithmetic must respect: seasons and staffing policies --------

# SCN2's own rules, as a manager wrote them: Smart Paper is staffed from
# Pack Singles alone, Rebin and Induct by their home team only, and Ship
# Dock takes no cross-trainees. Defaults a site edits, not facts.
DEFAULT_POLICIES = "SPP = Pack Singles\nSmart Paper = Pack Singles\nRebin = home\nInduct = home\ndept:Ship Dock = home"
# Peak runs from 20 November into the new year. "protect" means no
# cross-training pool is cut while it is on or in the weeks before it.
DEFAULT_SEASONS = "Peak = 11-20 to 01-03 protect"


def parse_policies(text):
    """'SPP = Pack Singles' / 'Rebin = home' / 'dept:Ship Dock = home'
    into {'process:spp': ['pack singles'], ...}. 'home' (or nothing
    after the =) leaves an empty list, which means the home department
    only. Returns the policies and the lines that could not be read."""
    policies, bad = {}, []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, rest = line.partition("=")
        name = name.strip()
        if not sep or not name:
            bad.append(line)
            continue
        kind = "dept" if name.lower().startswith("dept:") else "process"
        name = name.split(":", 1)[1].strip() if kind == "dept" else name
        allowed = [_key(x) for x in rest.split(",") if _key(x) and _key(x) != "home"]
        policies[f"{kind}:{_key(name)}"] = allowed
    return policies, bad


def parse_seasons(text):
    """'Peak = 11-20 to 01-03 protect' into [{'name', 'start', 'end',
    'protect'}]. Returns the seasons and the lines that could not be read."""
    seasons, bad = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("=")
        words = rest.replace(" to ", " ").split()
        try:
            start, end = words[0], words[1]
            for md in (start, end):
                date(2000, int(md[:2]), int(md[3:5]))      # 2000 is a leap year: 02-29 is allowed
            if len(start) != 5 or len(end) != 5:
                raise ValueError
        except (IndexError, ValueError):
            bad.append(line)
            continue
        seasons.append({"name": name.strip() or "Season", "start": start, "end": end,
                        "protect": "protect" in [w.lower() for w in words[2:]]})
    return seasons, bad


def unmatched_policies(prep, policies):
    """Policy names that match no process or department in the data, so
    the page can say a rule is doing nothing rather than let it pass."""
    out = []
    for key, allowed in (policies or {}).items():
        kind, name = key.split(":", 1)
        known = prep.owner if kind == "process" else prep.departments
        if name not in known:
            out.append(key)
        out += [f"dept:{d}" for d in allowed if d not in prep.departments]
    return sorted(set(out))


def season_on(day, seasons):
    """The season a date falls in, if any. Seasons are month-day ranges
    that may cross the new year (Peak: 11-20 to 01-03)."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    md = d.strftime("%m-%d")
    for season in seasons or []:
        start, end = season.get("start"), season.get("end")
        if not start or not end:
            continue
        inside = start <= md <= end if start <= end else (md >= start or md <= end)
        if inside:
            return season
    return None


def protected_season_near(day, seasons, weeks):
    """A protected season that is on at `day` or starts within `weeks`.
    Cutting a cross-training pool then is cutting it just before it is
    needed most, and rebuilding it costs every head its training hours."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    for offset in range(0, 7 * weeks + 1, 1):
        season = season_on(d + timedelta(days=offset), seasons)
        if season and season.get("protect"):
            return season
    return None


def day_filter(prep, settings, scenario):
    """Which past days a judgement may draw on. Judged during a season,
    only that season's days (a peak week is not a normal week); judged
    outside one, no season's days. With too few such days in the window
    the filter is dropped, and the evidence says the judgement mixes."""
    seasons = settings.get("seasons") or []
    if scenario is not None or not seasons:
        return None, None
    current = season_on(prep.today, seasons)
    if current:
        keep = lambda d: season_on(d, seasons) is current
        label = f"judged on {current['name']} days only"
    else:
        keep = lambda d: season_on(d, seasons) is None
        label = "season days left out"
    # A week of distinct dates at least: every weekday once.
    if len({d for d, _s in prep.days if keep(d)}) < 7:
        return None, "too few days to separate the season, so it mixes"
    return keep, label


def policy_for(prep, process, settings):
    """The departments allowed to staff a process, if a policy says so:
    by process name, else by its owning department. An empty list means
    its home department only. None means no policy."""
    policies = settings.get("policies") or {}
    owner = prep.owner.get(process)
    process = _key(process)
    allowed = policies.get("process:" + process)
    if allowed is None and owner:
        allowed = policies.get("dept:" + owner)
    if allowed is None:
        return None
    return set(allowed) or ({owner} if owner else set())


def evaluate_cell(prep, process, shift, current_target, sources, settings, scenario=None, explore=True):
    """One process on one shift: the verdict and the evidence for it.
    With a scenario, the days judged are its future days rather than the
    past ones; every rule after that is the same."""
    # The tolerance grows with the target: one person either way is noise
    # on a standard of 5 and on one of 50 alike, but 10% of 50 is not.
    # Learned from outcomes: where recommendations for this process have
    # covered less than the replay predicted, the bar is raised by the
    # shortfall, so the next recommendation leaves that much more room.
    margin = (settings.get("calibration") or {}).get(process, 0.0)
    bar = min(0.995, settings["coverage_bar"] + margin)
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

    allowed = policy_for(prep, process, settings)
    named = [_key(S) for S in sources if _key(S) and _key(S) != owner]
    # A source the policy does not allow contributes nobody: the standard
    # can say what it likes, the floor will not staff it that way.
    source_keys = [S for S in named if allowed is None or S in allowed]
    disallowed = [S for S in named if S not in source_keys]
    raw = {S: w for S, w in _weights(sources).items() if S in source_keys}
    weights = {S: w / sum(raw.values()) for S, w in raw.items()} if raw and sum(raw.values()) else raw
    rates = {S: xt_rate(S) for S in source_keys}
    keep_day, season_note = day_filter(prep, settings, scenario)
    days = _days(prep, process, shift, source_keys, vet_rate, rates, scenario, keep_day)
    train_hours = (settings.get("train_hours") or {}).get(process) or settings.get("default_train_hours") or 40.0
    hourly = settings.get("hourly_cost") or 0.0
    trained_pool = prep.trained.get(process, set())

    def trained_now(depts):
        return sum(len(trained_pool & prep.people.get((S, shift), set())) for S in depts)

    def new_trainees(target, depts, split=None):
        # Per source, on its share of the target: people trained in a
        # department that gets none of the target train nobody else.
        split = split or {S: 1 / len(depts) for S in depts}
        return sum(max(0, math.ceil(target * split.get(S, 0) - 1e-9) - trained_now([S])) for S in depts)
    cell = {
        "process": prep.process_names.get(process, process), "shift": shift,
        "owner": prep.departments.get(owner, owner), "target": current_target,
        "sources": [prep.departments.get(S, S) for S in source_keys],
        "days": len(days), "vet_rate": vet_rate, "xt_rate": xt_all,
        "basis": "scenario" if scenario is not None else "history",
        "calibration": margin, "bar": bar,
        "policy": sorted(prep.departments.get(d, d) for d in allowed) if allowed is not None else None,
        "home_only": allowed is not None and not (allowed - {owner}),
        "disallowed": [prep.departments.get(S, S) for S in disallowed],
        "season_note": season_note, "train_hours": train_hours, "hourly_cost": hourly,
        "trained_now": trained_now(source_keys),
        "trained_by_source": {prep.departments.get(S, S): trained_now([S]) for S in source_keys},
        "weights": dict(weights),
        "rules": sorted({(prep.departments.get(d, d), wd) for (d, sh, wd) in prep.lend_blocks
                         if d in [_key(x) for x in sources] and sh in (shift, "*")}),
    }
    if not days or not vet_rate:
        cell.update(verdict="no_data", coverage=None,
                    why_missing=("no volume for this process and shift" +
                                 (" in the scenario" if scenario is not None else " in the window"))
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
        missed=sum(1 for d in days if d["missed"]) if scenario is None else None,
        # In a scenario the floor is the historical peak, not the
        # scenario's own (it has no people yet), and only on decreases.
        peak_xt=max(d["xt_working"] for d in days) if scenario is None else _historical_peak(prep, process, shift),
        lend_days=lend_days, assumed_rates=sorted(set(assumed)),
        curve=[(t, round(_coverage(days, t, weights) or 0, 3))
               for t in range(0, min(cap, max(current_target, best or 0) + 12) + 1)] if source_keys else [],
    )

    # Could another department do it with fewer people trained? Only a
    # department with people on this shift, that is not the owner and
    # not already a source.
    cell["new_at_target"] = new_trainees(current_target, source_keys, weights)
    if not explore:
        # A re-check of one proposed target and source: coverage and
        # training are all it needs, not the search for alternatives.
        return cell
    alternative, options = None, []
    for S, population in prep.people.items():
        dept, s = S
        # A current source alone is an alternative too when there are
        # several: "train only from Receive" is a switch worth proposing
        # when RSP, the other source, can never spare anyone.
        if s != shift or dept == owner or not population or (dept in source_keys and len(source_keys) == 1):
            continue
        if allowed is not None and dept not in allowed:
            continue
        alt_rates = {dept: xt_rate(dept)}
        alt_days = _days(prep, process, shift, [dept], vet_rate, alt_rates, scenario, keep_day)
        t, cov = _min_target(alt_days, {dept: 1.0}, len(population), bar)
        lends = sum(1 for d in alt_days if d["spare"][dept] >= 1)
        option = {"department": prep.departments.get(dept, dept), "target": t, "coverage": cov,
                  "lend_days": lends, "trained_now": trained_now([dept]), "people": len(population),
                  "new_trainees": new_trainees(t, [dept]) if t is not None else None,
                  "curve": [(x, round(_coverage(alt_days, x, {dept: 1.0}) or 0, 3))
                            for x in range(max(0, (t or current_target) - 6),
                                           min(len(population), (t or current_target) + 8) + 1)]}
        options.append(option)
        if t is None:
            continue
        # Fewest people to train first: a department that needs more
        # target but already has the people trained is the cheaper switch.
        if alternative is None or (option["new_trainees"], t, -lends) < (
                alternative["new_trainees"], alternative["target"], -alternative["lend_days"]):
            alternative = option
    cell["options"] = sorted(options, key=lambda o: (o["new_trainees"] is None, o["new_trainees"] or 0))

    cur_new = new_trainees(best, source_keys, weights) if best is not None else None
    if cell["home_only"]:
        # By policy only the home team staffs this process: a
        # cross-training standard here should not exist, and a shortfall
        # is a headcount question.
        if coverage < bar:
            owner_share = sum(d["owner_share"] for d in days) / len(days)
            for extra in range(1, 400):
                if _coverage(days, 0, {}, extra_vets=extra, owner_share=owner_share, vet_rate=vet_rate) >= bar:
                    cell["hire"] = extra
                    break
        verdict = "home_only" if (current_target or cell.get("hire")) else "keep"
        cell.update(verdict=verdict, alternative=None, new_target=0 if verdict == "home_only" else None,
                    new_trainees=0, training_hours=0)
        return cell
    if best is None:
        if alternative:
            verdict = "switch"
        else:
            verdict = "hire"
            owner_share = sum(d["owner_share"] for d in days) / len(days)
            for extra in range(1, 400):
                if _coverage(days, cap, weights, extra_vets=extra, owner_share=owner_share,
                             vet_rate=vet_rate) >= bar:
                    cell["hire"] = extra
                    break
    elif coverage < bar:
        # Switch when it means training fewer people, or as many people
        # for a clearly smaller target: each head costs its training hours.
        verdict = ("switch" if alternative and (
            alternative["new_trainees"] < (cur_new or 0)
            or (alternative["new_trainees"] == cur_new and alternative["target"] < best - band))
            else "increase")
    elif max(best, cell["peak_xt"]) < current_target - band:
        # Never below the most cross-trainees the floor actually used on
        # one shift: the replay says fewer would have done, but a target
        # under what was really deployed is one the next peak breaks.
        verdict = "decrease"
        best = max(best, cell["peak_xt"])
        cell["best"], cell["best_coverage"] = best, _coverage(days, best, weights)
    else:
        verdict = "keep"
    season = protected_season_near(scenario["start"] if scenario else prep.today, settings.get("seasons"),
                                   settings.get("protect_weeks", 8)) if settings.get("seasons") else None
    if verdict == "decrease" and season:
        # The replay would cut the pool; the season says it is about to be
        # needed, and rebuilding it after costs every head its training.
        cell["held_from"] = best
        cell["season"] = season["name"]
        verdict = "hold"
    cell["verdict"] = verdict
    cell["alternative"] = alternative if verdict == "switch" else None
    cell["new_target"] = {"increase": best, "decrease": best,
                          "switch": alternative["target"] if alternative else None}.get(verdict)
    if verdict == "switch" and alternative:
        cell["new_trainees"] = alternative["new_trainees"]
    elif verdict == "increase":
        cell["new_trainees"] = cur_new
    else:
        cell["new_trainees"] = 0
    cell["training_hours"] = (cell["new_trainees"] or 0) * train_hours
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
    shifts_word = "scenario shifts" if cell.get("basis") == "scenario" else "replayed shifts"
    if cell.get("missed") is not None:
        lines.append(f"{cell['missed']} of {n} replayed shifts missed plan.")
    if cell.get("basis") == "scenario" and cell.get("coverage") is not None:
        met = round(cell["coverage"] * n)
        lines.append(f"At the current target of {cell['target']}, {met} of {n} scenario shifts meet plan, "
                     f"each future day played out against recent same weekdays.")
    for dept, k in (cell.get("lend_days") or {}).items():
        lines.append(f"After covering its own volume, {dept} could lend on {k} of {n} shifts.")
    alt = cell.get("alternative")
    if alt:
        lines.append(f"{alt['department']} could lend on {alt['lend_days']} of {n} shifts, and a target "
                     f"of {alt['target']} from there covers {round(alt['coverage'] * 100)}% of them.")
    if cell["verdict"] in ("increase", "decrease") and cell.get("best") is not None:
        lines.append(f"A target of {cell['best']} covers {round(cell['best_coverage'] * 100)}% of "
                     f"{shifts_word}; {cell['target']} covers {round(cell['coverage'] * 100)}%.")
    if cell["verdict"] == "decrease":
        lines.append(f"Peak use was {cell['peak_xt']} cross-trainees on one shift"
                     f"{' in the history' if cell.get('basis') == 'scenario' else ''}.")
    if cell["verdict"] == "hire":
        lines.append(f"Even with all {cell['cap']} people in the source departments trained, coverage "
                     f"reaches {round((cell.get('best_coverage') or 0) * 100)}%. About "
                     f"{cell.get('hire', '?')} more veterans in {cell['owner']} would reach the bar.")
    if cell.get("home_only"):
        who = " or ".join(cell["policy"]) or cell["owner"]
        lines.append(f"By policy only {who} staffs {cell['process']}: no cross-trainees here"
                     + (f"; about {cell['hire']} more people would be needed in {who}." if cell.get("hire") else "."))
    elif cell.get("disallowed"):
        lines.append(f"{', '.join(cell['disallowed'])} may not staff {cell['process']} by policy, "
                     f"so the standard's people from there count for nothing.")
    if cell["verdict"] == "hold":
        lines.append(f"The replay alone would cut this to {cell['held_from']}, but {cell['season']} is on or "
                     f"close: the pool is held, because rebuilding it costs {cell['train_hours']:g} hours a head.")
    if cell.get("new_trainees"):
        lines.append(f"{cell['new_trainees']} people to train, {cell['training_hours']:g} training hours "
                     f"at {cell['train_hours']:g} a head.")
    if cell.get("season_note"):
        lines.append(f"Past shifts {cell['season_note']}.")
    if cell.get("rules"):
        days_by = defaultdict(list)
        for dept, wd in cell["rules"]:
            days_by[dept].append(WEEKDAY_NAMES[wd])
        for dept, names in days_by.items():
            lines.append(f"{dept} is set not to lend on {', '.join(names)} (a rule from feedback), "
                         f"so it counts no spare people those days.")
    if cell.get("calibration"):
        lines.append(f"The bar for this process is {round(cell['bar'] * 100)}%, not "
                     f"{round((cell['bar'] - cell['calibration']) * 100)}%: past recommendations here covered "
                     f"less than predicted, so it asks for that much more.")
    if cell.get("assumed_rates"):
        lines.append("No history of " + ", ".join(cell["assumed_rates"]) +
                     " on this process, so their rate is taken as the cross-trainee average.")
    return lines


def evaluate(prep, standards, settings, department=None, shift=None, scenario=None):
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
        result = evaluate_cell(prep, p, s, int(round(info["target"])), dict(info["sources"]), settings, scenario)
        result["labels"] = info["labels"]
        result["evidence"] = evidence(result)
        result["id"] = f"{p}|{s}"
        results.append(result)
    processes = sorted({r["process"] for r in results}, key=lambda x: (prep.departments.get(prep.owner.get(_key(x)), "") or "", x))
    changes = [r for r in results if r["verdict"] in ("increase", "decrease", "switch", "hire", "home_only", "hold")]
    order = {"home_only": 0, "hire": 1, "increase": 2, "switch": 3, "decrease": 4, "hold": 5}
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
        "window": (prep.window_start, prep.window_end) if scenario is None else scenario_window(scenario),
        "replayed_days": len(prep.days) if scenario is None else len(scenario_dates(scenario)) * len(SHIFTS),
        "basis": "history" if scenario is None else "scenario",
        "scenario": scenario,
    }


# -- the model's prediction, checked --------------------------------------------

AI_ACTIONS = ("keep", "increase", "decrease", "switch", "hire", "hold", "remove")


def check_prediction(prep, cell, pred, settings, scenario=None):
    """What the language model proposed for one standard, held to the
    same arithmetic as everything else. The model may weigh what the
    replay cannot (a peak coming, the cost of retraining, a feedback
    reason), so it is allowed to disagree; it is not allowed to break a
    staffing policy, to name a department that does not exist, or to
    have its pick's coverage taken on its word. The coverage and the
    training hours shown beside a prediction are always recomputed here.
    Returns None for an answer that cannot be read at all."""
    if not isinstance(pred, dict) or pred.get("action") not in AI_ACTIONS:
        return None
    settings = dict(DEFAULTS, **(settings or {}))
    process, shift = cell["id"].split("|", 1)
    action = pred["action"]
    flags, refused = [], None
    try:
        target = int(round(float(pred.get("target")))) if pred.get("target") is not None else None
    except (TypeError, ValueError):
        target = None
    try:
        hire = max(0, int(round(float(pred.get("hire") or 0))))
    except (TypeError, ValueError):
        hire = 0
    try:
        confidence = min(1.0, max(0.0, float(pred.get("confidence"))))
    except (TypeError, ValueError):
        confidence = None
    if action in ("keep", "hold", "hire"):
        target = cell["target"] if target is None else target
    if action == "remove":
        target = 0
    names = {_key(n): n for n in (cell.get("sources") or [])}
    names.update({_key(o["department"]): o["department"] for o in cell.get("options") or []})
    source = pred.get("source")
    if isinstance(source, list):
        source = source[0] if len(source) == 1 else None
    source_key = _key(source) if source else None
    if source_key and source_key not in names:
        refused = (f"{source} may not staff {cell['process']}" if cell.get("policy") is not None
                   and source_key in prep.departments else f"{source} is not a department that can lend here")
    if cell.get("home_only") and (target or 0) > 0:
        refused = f"by policy only {' or '.join(cell['policy'] or [cell['owner']])} staffs {cell['process']}"
    if target is not None and target < 0:
        target = 0
    # Every key, every time: the page renders these without guessing.
    out = {"action": action, "target": target, "source": None, "hire": hire or None, "confidence": confidence,
           "flags": flags, "refused": refused, "coverage": None, "new_trainees": None, "training_hours": None,
           "agrees": False}
    if refused:
        return out
    current = [_key(n) for n in cell.get("sources") or []]
    use = [source_key] if source_key and (action == "switch" or source_key not in current) else current
    out["source"] = " + ".join(names.get(k, k) for k in use) or None
    if target is not None and use:
        cap = sum(len(prep.people.get((k, shift), ())) for k in use)
        if target > cap:
            flags.append(f"{target} is more than the {cap} people on this shift in {out['source']}; judged at {cap}")
            target = out["target"] = cap
        split = {k: v for k, v in (cell.get("weights") or {}).items() if k in use} if use == current else None
        recheck = evaluate_cell(prep, process, shift, target, split or use, settings, scenario, explore=False)
        out["coverage"] = recheck.get("coverage")
        out["new_trainees"] = recheck.get("new_at_target") or 0
        out["training_hours"] = out["new_trainees"] * (cell.get("train_hours") or 0)
        if out["coverage"] is not None and out["coverage"] < cell["bar"] - 1e-9 and not hire:
            flags.append(f"below the {round(cell['bar'] * 100)}% bar on the replay")
    computed = cell.get("new_target") if cell.get("new_target") is not None else cell["target"]
    same = {"keep": ("keep", "hold"), "hold": ("hold", "keep"), "increase": ("increase",), "decrease": ("decrease",),
            "switch": ("switch",), "hire": ("hire",), "remove": ("home_only",)}[action]
    out["agrees"] = cell["verdict"] in same and (target is None or abs(target - computed) <= max(1, round(computed * .1)))
    return out


def season_volume(prep, process, shift, seasons):
    """Average demand a day for a process and shift inside and outside
    the seasons, from the days the data holds: the model is told how
    different peak has been here, not asked to imagine it."""
    inside, outside = [], []
    for d, s in prep.days:
        if s != shift:
            continue
        row = prep.pday.get((d, s, process))
        if not row:
            continue
        (inside if season_on(d, seasons) else outside).append(prep.demand(row))
    mean = lambda xs: round(sum(xs) / len(xs)) if xs else None
    return {"in_season_units_a_day": mean(inside), "in_season_days": len(inside),
            "out_of_season_units_a_day": mean(outside), "out_of_season_days": len(outside)}


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
    allowed = policy_for(prep, p, settings)
    for dept in sorted({_key(prep.home[l]) for l in trained if l in prep.home} - {owner}):
        people = trained & on_shift(dept)
        # A department the staffing policy keeps off this process is not
        # a lender, however many of its people are trained on it.
        if not people or (allowed is not None and dept not in allowed):
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
            elif _model(prep, q, shift):
                vol, how = round(forecast_value(_model(prep, q, shift), day_s)), "forecast"
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
            "deployable": 0 if prep.blocked(dept, shift, day_s) else max(0, math.floor(min(hc["available"], max(0.0, spare)))),
            "blocked": prep.blocked(dept, shift, day_s),
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
    codes["early"].append("DSMPC")            # a Central code, as SSPOT has
    pattern = {"early": "Day", "late": "Mid", "night": "Night"}
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
                                     "pattern": "Central" if code == "DSMPC" else pattern[s],
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


# -- labour mix: who worked each process, and how fast, against plan ---------

PERIOD_KINDS = ("day", "week", "month", "quarter")
SHIFT_GROUPS = ("early", "late", "night", "central")


def period_key(day, kind):
    d = date.fromisoformat(day) if isinstance(day, str) else day
    if kind == "day":
        return d.isoformat()
    if kind == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if kind == "month":
        return f"{d.year}-{d.month:02d}"
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def period_bounds(key, kind):
    """First and last day of a period, inclusive, as ISO dates."""
    if kind == "day":
        return key, key
    if kind == "week":
        start = date.fromisoformat(key)
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if kind == "month":
        year, month = int(key[:4]), int(key[5:7])
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        return date(year, month, 1).isoformat(), (nxt - timedelta(days=1)).isoformat()
    year, quarter = int(key[:4]), int(key[-1])
    first = date(year, 3 * quarter - 2, 1)
    nxt = date(year + (quarter == 4), (3 * quarter) % 12 + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def period_label(key, kind):
    start, end = (date.fromisoformat(x) for x in period_bounds(key, kind))
    if kind == "day":
        return start.strftime("%a %-d %b %Y")
    if kind == "week":
        if start.month == end.month:
            return f"{start.day}–{end.day} {end.strftime('%b %Y')}"
        return f"{start.strftime('%-d %b')}–{end.strftime('%-d %b %Y')}"
    if kind == "month":
        return start.strftime("%B %Y")
    return f"Q{key[-1]} {key[:4]}"


def available_periods(prep, kind, limit=12):
    """The periods the associate-day data covers, newest first, and the
    one to show by default: the newest that has finished, so a week read
    on its Tuesday does not compare two days against a whole plan."""
    days = sorted({d for (d, _s, _p) in prep.work})
    keys = sorted({period_key(d, kind) for d in days}, reverse=True)[:limit]
    today = prep.today.isoformat()
    finished = [k for k in keys if period_bounds(k, kind)[1] < today]
    return keys, (finished[0] if finished else (keys[0] if keys else None))


def labour_mix(prep, start, end, department=None, shift=None):
    """Hours and rate against plan, veterans against cross-trainees, per
    process and shift, and summed per shift.

    Veterans are the process's home department; cross-trainees are
    everyone else with a known home department. Rate against plan is
    units over (hours times each process's own planned rate), so a shift
    total weighs every process by its own plan rather than averaging
    rates that mean different things. Indirect time and processes with no
    owner are left out; hours from people with no home department are
    counted and reported, not guessed into either group."""
    plan_any = defaultdict(list)
    for (_d, _s, p), row in prep.pday.items():
        if row.get("plan_rate"):
            plan_any[p].append(row["plan_rate"])
    plan_any = {p: _median(v) for p, v in plan_any.items()}
    dept_key = _key(department) if department else None

    cells = {}
    unknown_hours = 0.0
    for (d, s, p), rows in prep.work.items():
        if not (start <= d <= end):
            continue
        owner = prep.owner.get(p)
        if not owner or p not in plan_any and not prep.pday.get((d, s, p)):
            continue
        if dept_key and owner != dept_key:
            continue
        day_row = prep.pday.get((d, s, p))
        plan = (day_row or {}).get("plan_rate") or plan_any.get(p)
        for login, units, hours in rows:
            if not hours:
                continue
            group = "central" if (prep.code.get(login), d) in prep.central else s
            if shift and group != shift:
                continue
            home = _key(prep.home.get(login))
            if not home:
                unknown_hours += hours
                continue
            side = "vet" if home == owner else "xt"
            c = cells.setdefault((p, group), {
                "vet": [0.0, 0.0, 0.0, set()], "xt": [0.0, 0.0, 0.0, set()],
                "plan_hours": 0.0, "plan_weighted": 0.0, "by_dept": defaultdict(lambda: [0.0, 0.0, 0.0, set()])})
            for bucket in (c[side],) + ((c["by_dept"][home],) if side == "xt" else ()):
                bucket[0] += hours
                bucket[1] += units
                bucket[2] += hours * plan if plan else 0.0
                bucket[3].add(login)
            if plan:
                c["plan_hours"] += hours
                c["plan_weighted"] += hours * plan

    def figures(bucket):
        hours, units, planned, people = bucket
        return {"hours": round(hours, 1), "people": len(people),
                "rate": round(units / hours, 1) if hours else None,
                "pct": round(100 * units / planned, 1) if planned else None}

    detail = []
    totals = {}
    for (p, group), c in cells.items():
        vet, xt = figures(c["vet"]), figures(c["xt"])
        total = vet["hours"] + xt["hours"]
        detail.append({
            "process": prep.process_names.get(p, p), "department": prep.departments.get(prep.owner[p], prep.owner[p]),
            "shift": group, "vet": vet, "xt": xt,
            "vet_share": round(vet["hours"] / total, 3) if total else None,
            "xt_share": round(xt["hours"] / total, 3) if total else None,
            "plan": round(c["plan_weighted"] / c["plan_hours"], 1) if c["plan_hours"] else None,
            "by_dept": sorted(({"department": prep.departments.get(k, k), **figures(v)}
                               for k, v in c["by_dept"].items()), key=lambda x: -x["hours"]),
        })
        t = totals.setdefault(group, {"vet": [0.0, 0.0, 0.0, set()], "xt": [0.0, 0.0, 0.0, set()]})
        for side in ("vet", "xt"):
            for i in range(3):
                t[side][i] += c[side][i]
            t[side][3] |= c[side][3]
    overview = []
    for group in SHIFT_GROUPS:
        if group not in totals:
            continue
        vet, xt = figures(totals[group]["vet"]), figures(totals[group]["xt"])
        total = vet["hours"] + xt["hours"]
        overview.append({"shift": group, "vet": vet, "xt": xt,
                         "vet_share": round(vet["hours"] / total, 3) if total else None,
                         "xt_share": round(xt["hours"] / total, 3) if total else None})
    # Processes that lean on cross-trainees first, most cross-trained
    # hours at the top, so the rows shown before "View all" are the ones
    # this page is about; a process's shifts stay together.
    xt_by_process = defaultdict(float)
    for r in detail:
        xt_by_process[r["process"]] += r["xt"]["hours"]
    detail.sort(key=lambda r: (-xt_by_process[r["process"]], r["process"].lower(), SHIFT_GROUPS.index(r["shift"])))
    return {"overview": overview, "detail": detail, "unknown_hours": round(unknown_hours, 1),
            "start": start, "end": end}


# -- learning from the past: forecasts, and the future days they make ---------
#
# "Learn from the past" here means a model fitted to the site's own
# history and graded on weeks it did not see, so every forecast comes
# with how wrong it has been. It is deliberately small enough to explain
# in one sentence on the page:
#
#     the average of the last two weeks, with each weekday's usual share
#     of the week, and the trend of the last eight weeks at half
#     strength (capped at ±5% a week).
#
# A language model is not asked for volumes: it cannot be graded on held-
# out weeks the way this can, and a number nobody can check is the thing
# this page exists to avoid. It explains the result instead.

FORECAST = {"level_days": 14, "trend_weeks": 8, "trend_damping": 0.5, "trend_cap": 0.05,
            "profile_shrink": 3, "backtest_weeks": 4, "min_days": 14}


def history_series(prep, process, shift, before=None):
    """(date, units) for every past day the process ran on that shift.

    Planned units where the data has them, actual units otherwise. On a
    day the floor was short, units done is less than the work there was,
    so a forecast learned from it reads every shortfall as the normal
    volume and recommends fewer cross-trainees than the work needs. With
    FCLM alone there is no planned volume, so that bias remains; the
    page says which a forecast was learned from."""
    before = before or prep.today.isoformat()
    out = []
    for (d, s, p), row in prep.pday.items():
        if p == process and s == shift and d < before:
            value = row.get("plan_units") or row.get("units") or 0
            if value > 0:
                out.append((d, float(value)))
    return sorted(out)


def fit_forecast(series):
    """The model above, from (date, units) pairs. None with too little
    history to say anything."""
    if len(series) < FORECAST["min_days"]:
        return None
    by_wd = defaultdict(list)
    for d, v in series:
        by_wd[date.fromisoformat(d).weekday()].append(v)
    overall = sum(v for _d, v in series) / len(series)
    k = FORECAST["profile_shrink"]
    profile = [((sum(by_wd[w]) / len(by_wd[w]) / overall) * len(by_wd[w]) + k) / (len(by_wd[w]) + k)
               if by_wd[w] else 1.0 for w in range(7)]
    deseason = [(d, v / profile[date.fromisoformat(d).weekday()]) for d, v in series]
    last = date.fromisoformat(series[-1][0])
    recent = [x for d, x in deseason if (last - date.fromisoformat(d)).days < FORECAST["level_days"]]
    level = sum(recent) / len(recent)
    weekly = defaultdict(list)
    for d, x in deseason:
        weeks_back = (last - date.fromisoformat(d)).days // 7
        if weeks_back < FORECAST["trend_weeks"]:
            weekly[weeks_back].append(x)
    points = [(-wb, sum(xs) / len(xs)) for wb, xs in weekly.items() if len(xs) >= 3]
    trend = 0.0
    if len(points) >= 3:
        mx = sum(p[0] for p in points) / len(points)
        my = sum(p[1] for p in points) / len(points)
        den = sum((p[0] - mx) ** 2 for p in points)
        if den and my:
            trend = (sum((p[0] - mx) * (p[1] - my) for p in points) / den) / my * FORECAST["trend_damping"]
            trend = max(-FORECAST["trend_cap"], min(FORECAST["trend_cap"], trend))
    return {"profile": profile, "level": level, "trend": trend, "last": last.isoformat(), "days": len(series)}


def forecast_value(model, day):
    d = date.fromisoformat(day) if isinstance(day, str) else day
    weeks = (d - date.fromisoformat(model["last"])).days / 7.0
    return model["level"] * (1 + model["trend"]) ** weeks * model["profile"][d.weekday()]


def backtest(prep, process, shift):
    """How wrong the forecast has been: refit as of each of the last few
    weeks and predict the week after, on days it did not see. Returns
    accuracy (1 - weighted absolute error), and the 10th/90th percentile
    of the relative error, which is the band the page prints."""
    series = history_series(prep, process, shift)
    if len(series) < FORECAST["min_days"] + 7:
        return None
    last = date.fromisoformat(series[-1][0])
    abs_err = total = 0.0
    rel = []
    for k in range(1, FORECAST["backtest_weeks"] + 1):
        cutoff = (last - timedelta(days=7 * k)).isoformat()
        model = fit_forecast([x for x in series if x[0] <= cutoff])
        if not model:
            continue
        horizon_end = (date.fromisoformat(cutoff) + timedelta(days=7)).isoformat()
        for d, v in series:
            if cutoff < d <= horizon_end:
                pred = forecast_value(model, d)
                abs_err += abs(v - pred)
                total += v
                if pred:
                    rel.append((v - pred) / pred)
    if not total or len(rel) < 5:
        return None
    rel.sort()
    q = lambda f: rel[min(len(rel) - 1, max(0, int(round(f * (len(rel) - 1)))))]
    return {"accuracy": round(1 - abs_err / total, 3), "low": round(q(0.1), 3), "high": round(q(0.9), 3),
            "days": len(rel)}


def _model(prep, process, shift):
    cache = prep.__dict__.setdefault("_models", {})
    if (process, shift) not in cache:
        cache[(process, shift)] = fit_forecast(history_series(prep, process, shift))
    return cache[(process, shift)]


def forecast_table(prep, processes=None, start=None, weeks=4):
    """Per process and shift: the forecast average units a day over the
    horizon, and how accurate the model has been. What the scenario
    editor shows beside each input."""
    start = start or prep.today
    dates = [start + timedelta(days=i) for i in range(7 * weeks)]
    rows = []
    for p in sorted(processes or prep.process_names, key=lambda x: prep.process_names.get(x, x).lower()):
        for s in SHIFTS:
            model = _model(prep, p, s)
            if not model:
                continue
            avg = sum(forecast_value(model, d) for d in dates) / len(dates)
            planned = sum(1 for (d, sh, q), r in prep.pday.items() if q == p and sh == s and r.get("plan_units"))
            rows.append({"process": prep.process_names.get(p, p), "key": p, "shift": s,
                         "learned_from": "planned units" if planned >= model["days"] / 2 else "units done",
                         "forecast": round(avg), "trend_pct": round(model["trend"] * 100, 1),
                         "history_days": model["days"], "quality": backtest(prep, p, s),
                         "plan_rate": prep.plan_rate(p)})
    return rows


def scenario_dates(scenario):
    start = date.fromisoformat(scenario["start"])
    return [start + timedelta(days=i) for i in range(7 * int(scenario.get("weeks") or 4))]


def scenario_window(scenario):
    dates = scenario_dates(scenario)
    return (dates[0].isoformat(), dates[-1].isoformat())


def scenario_demand(prep, scenario, process, shift, day):
    """Units a process is expected to need on a future day: the planned
    average a day entered for the scenario, spread over the week by the
    process's own weekday pattern, else the forecast; times the
    scenario's multiplier."""
    line = (scenario.get("lines") or {}).get((process, shift)) or {}
    model = _model(prep, process, shift)
    mult = float(scenario.get("multiplier") or 1.0)
    if line.get("planned_units"):
        profile = model["profile"][day.weekday()] if model else 1.0
        return float(line["planned_units"]) * profile * mult
    return forecast_value(model, day) * mult if model else 0.0


def scenario_plan_rate(prep, scenario, process, shift):
    line = (scenario.get("lines") or {}).get((process, shift)) or {}
    return line.get("plan_rate") or prep.plan_rate(process)


def _profile(prep, kind, key, weekday):
    """Presence share and shift length by weekday, learned from the
    history window; the future has no attendance yet, so a Tuesday is
    expected to look like the Tuesdays before it."""
    cache = prep.__dict__.setdefault("_profiles", {})
    ck = (kind, key, weekday)
    if ck not in cache:
        values = []
        for d, s in prep.days:
            if date.fromisoformat(d).weekday() != weekday:
                continue
            if kind == "presence" and s == key[1]:
                values.append(prep.presence_share(key[0], d, s))
            elif kind == "length" and s == key:
                values.append(prep.length(d, s))
        if kind == "presence":
            cache[ck] = sum(values) / len(values) if values else None
        else:
            cache[ck] = _median(values) or prep.shift_hours
    return cache[ck]


def presence_profile(prep, dept, shift, weekday):
    value = _profile(prep, "presence", (dept, shift), weekday)
    if value is None:
        overall = [v for v in (_profile(prep, "presence", (dept, shift), w) for w in range(7) if w != weekday)
                   if v is not None]
        return sum(overall) / len(overall) if overall else 0.0
    return value


def _historical_peak(prep, process, shift):
    owner = prep.owner.get(process)
    peak = 0
    for (d, s, p), rows in prep.work.items():
        if p == process and s == shift:
            peak = max(peak, sum(1 for l, _u, h in rows if h and _key(prep.home.get(l)) != owner))
    return peak


SCENARIO_VARIANTS = 8


def _weekday_mean(prep, process, shift, weekday):
    cache = prep.__dict__.setdefault("_wd_means", {})
    key = (process, shift, weekday)
    if key not in cache:
        values = [v for d, v in history_series(prep, process, shift) if date.fromisoformat(d).weekday() == weekday]
        cache[key] = sum(values) / len(values) if values else None
    return cache[key]


def _swing(prep, process, shift, past_day):
    """How far a past day's volume sat from its weekday's usual: 1.12 for
    a day 12% busier than the average Tuesday."""
    row = prep.pday.get((past_day, shift, process))
    mean = _weekday_mean(prep, process, shift, date.fromisoformat(past_day).weekday())
    # The same measure the weekday mean is taken of (history_series):
    # planned units where there are any. Dividing units done by a mean of
    # planned units read every short day as a quiet one and took a fifth
    # off every scenario's volume (found 27 Nov, comparing a forecast
    # scenario cell by cell with the replay of the same weeks).
    value = (row or {}).get("plan_units") or (row or {}).get("units")
    return value / mean if value and mean else 1.0


def scenario_days(prep, process, shift, sources, vet_rate, xt_rates, scenario):
    """The scenario's future shifts, in the shape the replay uses, so the
    same verdict rules judge them.

    Each future day is played out once for each of the last eight past
    days on the same weekday: with that day's swing in volume (for this
    process and for every lender's own work) and that day's attendance
    in every department. An average Tuesday never happens, and a plan
    judged against one is judged against the only day cross-trainees are
    not needed for; this way a scenario inherits the variability the
    site has actually shown, and a target that covers 95% of these
    covers the bad Tuesdays too."""
    owner = prep.owner.get(process)
    trained = prep.trained.get(process, set())
    owner_people = prep.people.get((owner, shift), set())
    vets_on_record = len(owner_people & trained) if trained else len(owner_people)
    past_by_wd = defaultdict(list)
    for d, s in sorted(prep.days, reverse=True):
        if s == shift:
            past_by_wd[date.fromisoformat(d).weekday()].append(d)
    days = []
    for day in scenario_dates(scenario):
        base = scenario_demand(prep, scenario, process, shift, day)
        if base <= 0:
            continue
        wd = day.weekday()
        length = prep.length_by_day.get((day.isoformat(), shift)) or _profile(prep, "length", shift, wd)
        pasts = past_by_wd.get(wd, [])[:SCENARIO_VARIANTS] or [None]
        for past in pasts:
            def demand_of(q):
                value = scenario_demand(prep, scenario, q, shift, day)
                return value * (_swing(prep, q, shift, past) if past else 1.0)

            def share(dept):
                return prep.presence_share(dept, past, shift) if past else presence_profile(prep, dept, shift, wd)
            own_hours = {}
            for q in prep.owned.get(owner, ()):
                rate = scenario_plan_rate(prep, scenario, q, shift)
                if rate:
                    own_hours[q] = demand_of(q) / rate
            split = own_hours.get(process, 0) / sum(own_hours.values()) if sum(own_hours.values()) else 1.0
            vets = vets_on_record * share(owner) * split
            spare = {}
            for S in sources:
                need = 0.0
                for q in prep.owned.get(S, ()):
                    rate = scenario_plan_rate(prep, scenario, q, shift)
                    if rate:
                        need += demand_of(q) / rate
                spare[S] = len(prep.people.get((S, shift), ())) * share(S) - need / length
                if prep.blocked(S, shift, day):
                    spare[S] = min(spare[S], 0.0)
            days.append({
                "day": day.isoformat(), "as_on": past, "demand": demand_of(process), "length": length,
                "owner_share": share(owner), "vet_output": vets * length * (vet_rate or 0.0), "vets": vets,
                "share": {S: share(S) for S in sources}, "spare": spare, "rate": xt_rates,
                "missed": False, "xt_working": 0, "xt_by_dept": {},
            })
    return days


def forecast_scenario(prep, weeks=4, start=None):
    """The scenario nobody has to write: the forecast itself, from
    tomorrow, for the next few weeks."""
    start = start or (prep.today + timedelta(days=1))
    return {"id": "forecast", "name": f"Forecast, next {weeks} weeks", "start": start.isoformat(),
            "weeks": weeks, "multiplier": 1.0, "lines": {}}



# -- outcomes: did a recommendation that was applied do what it said? --------

def coverage_since(prep, process, shift, sources, target, since, settings=None):
    """The coverage a standard actually achieved on the past shifts since
    `since`, at `target`: the replay, run only over the days after the
    change. Returns (coverage, shifts) or (None, 0) with too few days."""
    process = _key(process)
    source_keys = [_key(S) for S in sources if _key(S) and _key(S) != prep.owner.get(process)]
    vet_rate = prep.rate(process, "vet")[0] or prep.rate(process, "all")[0]
    xt_all = prep.rate(process, "xt")[0]
    rates = {S: prep.rate(process, S)[0] or xt_all or vet_rate for S in source_keys}
    days = [d for d in _replay_days(prep, process, shift, source_keys, vet_rate, rates) if d["day"] >= since]
    if len(days) < 5 or not vet_rate:
        return None, len(days)
    weights = {S: 1 / len(source_keys) for S in source_keys} if source_keys else {}
    return _coverage(days, target, weights), len(days)
