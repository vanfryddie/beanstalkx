# Day plan — the data it needs, and how to structure it

The question the Day plan answers: *Pack Singles has X units on Monday.
How many veterans and how many cross-trainees will actually be there, at
what rates, can the cross-trainees' own departments spare them, and has a
mix like that met plan before? So what should the cross-training target
be, trained from which departments, or should Pack Singles hire veterans
instead?*

Five inputs in total. **Three you already have, two are new.** The two new
files are about one grain each: what each **process** did on a day, and
what each **associate** did on a day.

| # | File | Grain (one row per…) | Status | What it answers |
|---|---|---|---|---|
| 1 | Employee roster (`employeeList`) | associate | ✅ have | who is employed today, and on which shift pattern |
| 1b | Shift calendar (`SSPOT_Information`) | shift pattern cohort × working day | ✅ have | who is scheduled on a date, and how long their shift is that day; a day off is a day with no row |
| 2 | Trained status (once the LDMT `Hours_on_Function` export; now built from FCLM, no upload) | associate × function | ✅ from FCLM | home department, trained or not on each function, proficiency → who counts as a veteran or a cross-trainee for a process |
| 3 | **Process day** | date × shift × process | ➕ new (extends today's volume upload) | volume, hours and plan for every process — the target process *and* every department that might lend people |
| 4 | **Associate day** | date × shift × associate × process | ➕ new | who actually worked where and how fast → veteran vs cross-trainee rates, labour moves, and the mix on every past day |

Targets are already configured in the tool. Unplanned absence starts as
a percentage you set. Once file 4 exists it can be measured instead:
someone scheduled in the shift calendar who booked no hours was absent.

---

## What the files you have already give

Profiled from the SCN2 exports you shared (counts only, nothing personal
copied anywhere):

- **They join cleanly.** Login and employee ID agree on every one of the
  5,552 matching rows. Shift pattern agrees on 99.7%.
- **Home department comes from Hours on Function, not the roster.** The
  roster's `Department ID` is a cost centre: one code (1299) covers RSP,
  Pack Multis, Ship Dock and Pack Singles. The hours file's `fclm mapped`
  is the real home department, and the roster's `Management Area ID`
  matches it one-for-one (Pack Singles = 18, RSP = 3, Receive = 2, …).
- **The Pack Singles split already exists:** 198 trained veterans (home
  department Pack Singles) and 81 trained cross-trainees, from RSP (33),
  Receive (22), OBPS (15), Chutings (4), Ship Dock (3), Facilities (2),
  Pack Multis (1) and TOM (1). That is the "Y veterans, Z cross-trainees"
  of the question.
- **The schedule comes from the SSPOT shift calendar** (shared 27 Nov):
  one row per cohort per working day, with start and end times, from
  2026-09-01 to 2026-11-28. A day off is simply a day with no row.
  This replaces hand-configured rotations: it is the real dated calendar,
  including changes and odd weeks, and gives each day's actual shift
  length (8.75h for most, 6.5h, 7h, 10.5h and others). That length is
  what turns hours into heads.
  - **How a person finds their row.** A cohort is the roster's
    `Shift Pattern` plus a side: `L58F12XE-OB`, `L58F12XE-IB`, and also
    `-IC` (ICQA), `-SP` (Support) and `-TR` (Transport). On 98% of
    code-days every side has the same times, so the side rarely matters.
    For the other 2%, the tool maps each home department to its side
    once (Pack Singles → OB, Receive → IB, IC/QA/CS → IC, …). RSP works
    both sides and needs choosing.
  - **Mapping to the tool's shifts:** `D…` codes are early (06:00, 05:00,
    plus a 10:45 "Central"), `L…` are late (14:45), `N…` are night (23:30,
    23:15, and an 18:30 long night). A night belongs to the date it
    starts, as in the calendar itself.
  - **Coverage:** the calendar has 54 of the roster's 83 codes, which
    covers **2,503 of 2,637 people (95%)**. The other 134 are on 29 codes
    it does not contain. The biggest are `D06A5CJG` (27 people),
    `N58F12L0` (14) and `N58FC29V` (11), plus a cluster of `N58FC…`
    codes of 3–7 people each, which looks like a newer night pattern.
    Include those codes in the next export. Until then, those people are
    shown as "schedule unknown", not counted as present or absent.
  - **For planning ahead,** the export needs future dates. This one ends
    tomorrow (28 Nov). Exporting 4–6 weeks forward lets you plan a
    Monday three weeks out.
- **What these files cannot give:** anything per day. Hours on
  Function only has 60/90/180-day totals, and neither file has units. So
  no rates, and no record of who worked which process on which day.
  Files 3 and 4 fill exactly that gap.
- **Two things to know about the population:**
  - 233 people are in the hours file but not on the roster (most likely
    leavers or agency staff the roster no longer lists). The roster
    decides who is available.
  - 356 are on the roster but not in the hours file (new starters,
    managers, technicians): employed but trained on nothing yet.

---

## File 3 — Process day

**One row per date, shift and process.** This is the existing volume
upload with two optional columns added (`department`, `plan_units`), so a
file in today's format still loads.

| Column | Required | Meaning |
|---|---|---|
| `date` | yes | The working day, `YYYY-MM-DD`. A night shift belongs to the date it *started*. |
| `shift` | yes | `early` / `late` / `night`, or `D` / `L` / `N`. |
| `process` | yes | Spelled as **`Merged Functions`** in Hours on Function: `Pack Singles`, `Pick`, `Stow Each Nike`, … This is how volume meets trained people. |
| `department` | recommended | The home department that owns the process, spelled as `fclm mapped`: `Pack Singles`, `RSP`, `Receive`, … Needed to add up a lending department's own workload. If absent, the tool guesses from who is trained, and the guess is shown for correcting. |
| `units` | yes | Units actually processed. |
| `hours` | yes | Direct paid hours actually worked on the process. |
| `plan_units` | recommended | The volume the plan expected that day. Needed for "when did we exceed planned volume". |
| `plan_rate` | recommended | The planned rate, in units per hour. |
| `plan_hours` | optional | Planned hours. Derived from plan_units ÷ plan_rate when absent. |
| `fc` | optional | For a multi-site file. |

Example (made-up figures):

```csv
date,shift,process,department,units,hours,plan_units,plan_rate
2026-11-23,early,Pack Singles,Pack Singles,41250,312.5,40000,130
2026-11-23,early,Pick,RSP,96800,701.0,99000,140
2026-11-23,early,Each Receive,Receive,30400,190.0,28000,155
```

**Include every process, not only the target one.** A cross-trainee from
RSP can only move to Pack Singles if RSP's own volume leaves someone
spare. That is answered from RSP's rows in this file.

## File 4 — Associate day

**One row per date, shift, associate and process they booked direct
hours on.** Someone who did 5h of Pick and 3h of Pack Singles has two
rows. This one file gives three things:

- **rates by population:** the units and hours of veterans and of
  cross-trainees, separately, on each process each day;
- **the constellation of every past day:** who worked the process, from
  which home department;
- **labour moves:** anyone booking hours on a process outside their home
  department, which also shows what their own department lost.

| Column | Required | Meaning |
|---|---|---|
| `date` | yes | As in file 3. |
| `shift` | yes | As in file 3. |
| `employee_login` | yes (or `employee_id`) | Joins to the roster and to Hours on Function. Either key works; they agree everywhere. |
| `process` | yes | As in file 3 (`Merged Functions` spelling). If the export only has FCLM's own function names (e.g. *MultiLowDensityPick*, *RF Pick*), put that in `fclm_function` instead. The names are mapped to processes once, in the tool. |
| `fclm_function` | optional | The raw FCLM function name, kept for the mapping and for checking it. |
| `units` | yes | Units this associate processed on this process that day. |
| `hours` | yes | Direct hours this associate booked on this process that day. |

Example (made-up logins and figures):

```csv
date,shift,employee_login,process,fclm_function,units,hours
2026-11-23,early,exampla,Pack Singles,Pack Singles,1120,8.0
2026-11-23,early,examplb,Pick,RF Pick,610,5.0
2026-11-23,early,examplb,Pack Singles,Pack Singles,290,3.0
```

On that example day, `examplb` (home department RSP, say) was a labour
move. They are counted as a cross-trainee on Pack Singles for 3h at about
97 an hour, and RSP lost those 3h from Pick.

**Include indirect hours too**, as rows with process `Indirect` (or the
indirect function's own name) and units 0. Someone who came in but was
not needed on a direct process otherwise looks absent, so their
department looks shorter than it was and lends less in the replay.

**Do not add the home department, trained status or name to this file.**
Those come from files 1 and 2 as they stood on the day. Repeating them per
row only creates copies that drift out of date. The login is enough.

### Why two new files and not one

- **The plan lives at process level, not associate level.** Putting
  `plan_units` on every associate row repeats it hundreds of times, and
  the copies end up disagreeing.
- **File 3 is complete where file 4 might not be.** File 3 has every
  process total. File 4 can be missing people, for example agency staff
  without a login match, so its sums are checked against file 3 rather
  than replacing it. The tool reports how much of each day's hours file 4
  accounts for, so you can see how far to trust the rates.

### Why not three

No attendance file is needed. The shift calendar says who was scheduled,
and file 4 says who booked any hours. Scheduled with no hours means
absent, which measures unplanned absence per department, shift and
weekday instead of relying on a typed-in percentage.

---

## How much history

- **At least 13 weeks of files 3 and 4,** and 26 if you can. 13 weeks
  gives each weekday about a dozen comparable days. 26 weeks shows
  seasonal peaks, and those peaks are what a cross-training pool is for.
- **Keep sending them weekly** (or daily), each covering the days since
  the last one. Re-sending a day replaces that day, so overlaps are safe.
- **Files 1 and 2 go up as today:** a fresh copy whenever they change.
  Each upload is treated as the current state.
- **The shift calendar (1b)** should cover the same history as files 3
  and 4 (to replay past shifts) plus 4–6 weeks ahead (to plan). Re-sent
  days replace earlier ones.

## Where the files go

**L&D Settings → Data Upload → Cross-training planning data** lists all
five, shows what each holds, and has an Upload button for each. Process
day and associate day also have a **Template** button: a filled-in
example built around your own standards, with invented people. Static
copies are in `data_templates/` in the repository.

## Checks the tool will run on upload

- Process names that do not match a `Merged Functions` value are listed,
  not silently dropped.
- Logins in file 4 that are not on the roster or in Hours on Function are
  counted and shown.
- File 4's hours, summed per process per day, are compared with file 3's.
  A day where they differ by more than 10% is flagged.
- Shift patterns on the roster that the shift calendar does not contain
  are listed (today: 29 codes, 134 people). Those people's availability
  on a date is shown as unknown, not assumed.

## How they combine for one day (Pack Singles, Monday, X units)

1. **Trained.** From file 2: veterans (home department Pack Singles,
   trained) and cross-trainees (any other home department, trained),
   kept to people still on the roster (file 1).
2. **Scheduled.** From file 1's shift pattern and the shift calendar
   (1b): who has a row on that Monday, on that shift, and for how long.
   Then minus unplanned absence (typed in, or measured from files 1b + 4
   for that department and weekday). This gives available veterans and
   available cross-trainees by department, in heads and in hours.
3. **Rates.** From file 4 history: the veteran rate and the cross-trainee
   rate on Pack Singles, overall and per lending department, with how
   many days each rests on.
4. **What lenders can spare.** From file 3: each lending department's own
   volume that day (defaulting to its recent same-weekday average, which
   you can edit). Divided by its own rate, that gives the people it needs;
   take that from its available people, and the rest is what it can lend.
5. **History.** From files 3 + 4: past days with similar Pack Singles
   volume, which met plan_units and plan_rate, and the mix of veterans and
   cross-trainees from each department that worked them.
6. **Recommendation, computed.** The mix that covers X at the expected
   rates, the cross-trainee target that keeps enough available after
   days off and absence, which departments to train from (spare capacity
   first, then their people's Pack Singles rate), or, where no department
   can spare enough, how many veterans Pack Singles should hire instead.
   The language model explains this result. It does not produce it.

---

## The view this feeds: Cross-training standards

The target screen (from the mockup, 27 Nov): a grid of **process ×
shift**. Each cell shows the standard as it stands (its source and its
target headcount) and a verdict: **Keep · 97% coverage**, **Increase to
38**, **Decrease to 18** or **Switch to Pick · 18**. Beside it, a panel
answers *"Why change these standards?"* in two or three sentences per
change. A header counts processes, shifts and standards, and "3 changes
proposed · 15 kept". Filters choose department, shift and the window
("Last 8 weeks"). **Run evaluation** recomputes everything, and **Sample
data** shows the page before your own files exist.

Every number on that screen is computed. The language model writes only
the sentences in the "Why" panel, from figures it is handed, with the same
checking as the Cross Learning Model: a sentence quoting a number the
evaluation did not produce is removed.

### Coverage, the number every cell turns on

> **Coverage = the share of replayed shifts on which this standard would
> have met plan.** The bar is 95%, and it can be changed in Configuration.

**Replaying a shift** means taking a real past shift from the window and
asking: *with this many trained cross-trainees from this source, would
plan have been met?* For process P on one past shift:

1. **Demand.** P's `plan_units` that day (file 3). The hours it needs are
   demand ÷ plan_rate.
2. **Veterans.** P's veterans who were in that day (file 4: booked any
   hours), times the veteran rate on P (file 4, trailing history), times
   that day's shift length (shift calendar).
3. **The gap** is what the veterans cannot cover.
4. **Cross-trainees present.** The standard's target T, times the share
   of the source department's trained people who were in that day.
5. **What the source can spare.** Its people in that day, minus the
   people its own work needed: its processes' volume that day (file 3)
   divided by their rates. A source that was flat out lends nobody,
   however many of its people are trained.
6. **Lendable** = the smaller of 4 and 5. The shift **meets plan** if
   veteran output plus lendable × the cross-trainee rate (file 4) × shift
   length covers demand.

Running this for every past shift in the window, at each candidate
target, gives coverage as a function of the target.

### How each verdict is chosen

| Verdict | Rule |
|---|---|
| **Keep · NN% coverage** | The current target reaches 95%, and no target lower by more than the band also reaches it. The band is 1 person or 10% of the target, whichever is larger. NN is the current target's coverage. |
| **Increase to N** | The current target is under 95%. N is the smallest target that reaches 95%. |
| **Decrease to N** | A target lower by more than the band still reaches 95%. N is the smallest such, but never below the most cross-trainees actually used on one shift in the window. |
| **Switch to *source* · N** | Another department reaches 95% with the same or a smaller target, because it could spare people on more shifts. Shown with both departments' "could lend on k of n shifts". |
| **Hire, don't train** | No target reaches 95%, even with every trained person in the source department, because the sources cannot spare people on enough shifts. The cell shows how many veterans P would need instead. |

### The evidence lines in the "Why" panel, and where each comes from

| Sentence on the mockup | Computed as |
|---|---|
| "4 of 8 comparable shifts missed plan" | Past shifts in the window on the same shift and weekday type, with P's volume within ±15% of the case. Count of those where actual units < `plan_units` or actual rate < `plan_rate` (file 3). |
| "Pick had spare capacity in 7 of 8" | Step 5 above, per shift, for that source. |
| "Peak use was 16 cross-trainees" | The most cross-trainees who booked hours on P in any one shift in the window (file 4). |
| "A target of 18 covers 97% of replayed shifts" | The coverage curve at that target. |
| "After covering home demand, Receive could lend on 2 of 8 shifts" | Step 5 for the current source, compared with the alternative's. |

So the "Why" panel needs no more data than the grid does: files 1–4 and
the shift calendar.

### One difference from the mockup to settle

The mockup names sources by **process** ("Pick → Pack Singles"). The
tool's standards, and Hours on Function, name them by **home department**
(RSP, Receive, …), because a person belongs to a department, not to a
process. RSP covers Pick, Stow and Rebin, among others. The grid will
show department names (e.g. **RSP → Pack Singles**) unless a process
name is preferred, in which case each department needs one process to
represent it.

## The second view: Labour mix & rate performance

Below the standards grid (mockup of 27 Nov). For one day, week, month or
quarter, filterable by department and shift:

- **Shift overview:** veteran and cross-trainee hours and share, and
  each group's rate against plan, per shift, Central included.
- **Department & process detail:** the same for every process and shift,
  with the hours-weighted planned rate. Each row opens to show its
  cross-trainees by home department (people, hours, rate, % of plan).

How the figures are made, from file 4 with file 3's plan:

- A **veteran** is someone whose home department (Hours on Function)
  owns the process; anyone else with a known home department is a
  **cross-trainee**. Hours from people with no home department are
  counted aside and the page says how many.
- **% of plan** = units ÷ (hours × that day's planned rate for the
  process). A shift total therefore weighs every process by its own plan.
- **Central** is a re-cut of people: someone on a code that SSPOT marks
  Central that day is reported under Central, whatever shift the volume
  file filed the work under.
- Indirect time, and processes with no owning department, are left out.


## Read daily from FCLM instead of uploaded (27 Nov)

Three of the five inputs now come from FCLM by themselves. A manager's
browser reads them with the **LDIS Floor Sync** userscript (version 1.3
or later), the same one the Floor/Staffing page already uses, and posts
them to the console. The console's server never holds an FCLM (Midway)
session, so it cannot read FCLM on its own.

| Input | From | How often |
|---|---|---|
| Roster | FCLM **Employee Roster** | once a day |
| Associate day | FCLM **Function Rollup**, one request per process path the floor sweep covers | each past day, once |
| Process day | the associate day summed, with the planned rate from FCLM **Process Path Rollup** | each past day, once |
| Trained status (Hours on Function) | derived from the FCLM history (below) — its only source | after every sync |
| Shift calendar | still uploaded: the schedule is in SSPOT, not FCLM | weekly, with future weeks |

**How a day is read.** One operating day runs from the usual start of
the early shift (from the shift calendar, 06:00 by default) to the same
time next morning, so a night shift falls wholly inside the day it
began. Each person's hours go to their own shift, taken from their
roster shift pattern. FCLM function names are the Hours on Function
process names. A function with no match is kept under its own name and
listed on the Data Upload tab. Employee ids are resolved to logins
through the roster; an id nobody can name is counted and left out.

**Planned rate.** For each process, the Process Path Rollup line item
that most specifically covers the process paths it was worked under: a
"- Total" row where there is one, and the one naming the fewest paths.
The rate is that line's volume over its hours at plan. Overrides go in
the sync settings, one "line item = process" per line.

**What FCLM does not have:** planned *volume*. The PPR's plan is a rate.
Planned volume is entered on the Cross Learning Model tab instead, as
scenarios (below).

**When it runs.** On any console page, at most every 30 minutes: it reads
yesterday first, then fills the history behind it, a few days per run
(28 days of history and 3 per run by default; both settings). A day is
stored whole; one with a process that failed to load is marked
incomplete and read again on the next run. A roster under half the size
of the one on record is refused, not treated as most of the site leaving.
**Sync now** on the Data Upload tab runs it at once.

**Load on FCLM:** about 30 requests per day read (one per process path
plus one PPR) and one roster a day, three at a time.


## Hours on Function from FCLM (27 Nov)

The LDMT export's statuses are formulas over the hours a person booked on
a function, which the sync stores day by day, so the file is now rebuilt
from FCLM:

- **Proficiency:** 20 hours or more in the last 60 days is Proficient;
  else 20 in 90 is Practice; else 20 in 180 is Refresh; else Lapsed.
- **Trained:** Proficient, Practice or Refresh is Trained; Lapsed is not.
- **Home department:** the roster's Management Area ID, mapped to a
  department. The map is learned from the last uploaded Hours on Function
  where it can be, and is editable in the sync settings.
- **Manager:** the roster's manager name, matched to a login on the
  roster. An ambiguous name matches nobody.

The rebuilt file goes through the old upload's own importer, so
exclusions, the Insights history and every page behave exactly as they
did for an upload.

**FCLM is the only source (27 Nov, later).** The LDMT upload, its Clear
button and the *Trained status from* setting are gone. Trained status is
rebuilt whenever new FCLM data arrives (a day, a week or a roster), not
once a day and not after waiting for 180 days. That wait was why the
standards, retention and ambassador readiness looked frozen. On the
default setting they kept reading the last LDMT upload for the whole
backfill, about thirty runs of a browser left open. Separately, the sync
settings form capped "days of history" at 120 against a default of 180,
so a browser refused to save the form at all.

With under 180 days stored, the 90- and 180-day totals are short, so
some people trained two to six months ago read as Lapsed until the
history fills. The sync card shows how many days it is working from.
Insights keeps one census a day, the day's latest, however often the
table is rebuilt. A site whose table still holds an LDMT file keeps it
only until the first sync that brings FCLM history; the Data Upload tab
says which it is. The sync's default history is 180 days, six days per
run. The first fill is about 5,000 requests to FCLM, spread over about
thirty runs.

Only the process paths the sync reads count. A function worked under a
process path outside that list has no hours, so it derives as Lapsed.

## Planned volume and scenarios (27 Nov)

The **Judge against** chip at the top of the Cross Learning Model tab
picks what the standards are judged on: the past (the replay), the
**forecast** for the next four weeks, or a saved **scenario**.

- **Forecast:** learned from this site's history, per process and shift.
  It is the average of the last two weeks, each weekday at its usual
  share of the week, and the trend of the last eight weeks at half
  strength, capped at ±5% a week. Each forecast is graded by refitting
  it as of each of the last four weeks and scoring the week after, which
  it had not seen. The editor shows that accuracy and the usual miss
  beside every input. It learns from planned units where the data has
  them, else from units done. Units done reads short days as quiet ones,
  and the page says which.
- **Scenario:** a name, start, weeks, a volume % for the whole scenario,
  and per process and shift the planned units a day and planned rate.
  Blank lines use the forecast. Planned units are spread over the week by
  the process's own weekday pattern.
- **How a scenario is judged:** each future day is played out once for
  each of the last eight past days on the same weekday, with that day's
  volume swing (for this process and for every lender's own work) and
  that day's attendance in every department. The same verdict rules
  then apply as for the replay. A target that covers 95% of these covers
  the bad days too.
- **Consistency:** a forecast that says the weeks ahead look like the
  past reaches the past's verdict on 85% or more of standards (39 of 42
  on the sample). That is tested, so scenario differences come from the
  volume entered, not from the model.

The language model does not produce volumes. It explains the verdicts,
told whether the shifts were past or planned, and its sentences go
through the same number check.


## How long the sync takes, and why (27 Nov)

Each report is a few megabytes of HTML that FCLM builds on request, so
time is FCLM's, not the browser's. Three things keep it down:

- **The Process Path Rollup is read first.** Process paths it shows no
  hours on are not read: no rollup request for a path nobody worked.
  A path it does not mention is read anyway.
- **Five reports at a time** (setting: 1–8).
- **Older history is read a week at a time.** Days are read one by one
  for the last 98 days (setting), back to the start of FCLM's week;
  before that, one week-span report per process path replaces seven
  daily ones. That history only feeds the 180-day trained status. A
  week is stored only if FCLM's report header says it covered a week;
  if FCLM answers with anything else, weeks are switched off and every
  day is read.

The Data Upload card shows each read's cost: reports, seconds, megabytes,
and paths skipped.


## Feedback: how the recommendations learn (27 Nov)

The model on Bedrock is not retrained by use; every call starts fresh.
What improves is what it, and the arithmetic, are given. Under each
recommendation, **Your call** records agree, disagree (with a reason) or
"I've applied it":

1. **Rules.** Disagreeing because a department cannot lend on certain
   days creates a lending rule. From then on, the replay, the scenarios
   and the day plan count no spare people for it on those days, and the
   evidence says so.
2. **Outcomes.** An applied recommendation is scored once 14 days have
   passed: the coverage the new target actually achieved on the shifts
   since, against the coverage predicted when it was made. Where a
   process's recommendations fall short three times or more, its bar is
   raised by the average shortfall (at most 5 points). The next
   recommendation there leaves that much more room, and says why.
3. **Memory.** Reasons, site notes ("peak starts 1 Dec", "RSP layout
   change in week 49"), the rules and the track record are given to the
   explanation model with every change. It is told to say when an earlier
   disagreement still applies, and when the evidence now contradicts
   one. Its figures are still checked.

Everything learned is listed under "What it has learned from feedback",
and each item can be removed.
