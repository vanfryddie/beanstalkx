# Day plan — the data it needs, and how to structure it

The question the Day plan answers: *Pack Singles has X units on Monday.
How many veterans and how many cross-trainees will actually be there, at
what rates, can the cross-trainees' own departments spare them, and has a
mix like that met plan before? So what should the cross-training target
be, trained from which departments, or should Pack Singles hire veterans
instead?*

Four inputs in total. **Two you already have, two are new.** The two new
files are about one grain each: what each **process** did on a day, and
what each **associate** did on a day.

| # | File | Grain (one row per…) | Status | What it answers |
|---|---|---|---|---|
| 1 | Employee roster (`employeeList`) | associate | ✅ have | who is employed today, and on which shift pattern → who is scheduled on a date (with the rotations set in Settings → Shifts) |
| 2 | Hours on Function (`LDMT_Hours_on_Function`) | associate × function | ✅ have | home department, trained or not on each function, proficiency → who counts as a veteran or a cross-trainee for a process |
| 3 | **Process day** | date × shift × process | ➕ new (extends today's volume upload) | volume, hours and plan for every process — the target process *and* every department that might lend people |
| 4 | **Associate day** | date × shift × associate × process | ➕ new | who actually worked where and how fast → veteran vs cross-trainee rates, labour moves, and the mix on every past day |

Targets are already configured in the tool, and absence is a percentage
you set (it can later be measured from files 1 + 4, see the end).

---

## What the two files you have already give

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
- **Schedules work from the shift pattern** (`L58F8R80`, `D58F83YA`, …),
  as long as each code is configured in Settings → Shifts with its days
  on and off.
- **What these two files cannot give:** anything per day. Hours on
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

A daily attendance file (who was scheduled, who came in, who was off
sick) would let the tool *measure* unplanned absence instead of you
typing a percentage. It can mostly be derived instead: the roster plus
the rotation says who was scheduled, and file 4 says who booked any
hours. Scheduled with no hours means absent. Only send an attendance
export if you have one cheaply, with `date`, `employee_login`,
`scheduled` (Y/N) and `status` (worked / planned off / unplanned absent).

---

## How much history

- **At least 13 weeks of files 3 and 4,** and 26 if you can. 13 weeks
  gives each weekday about a dozen comparable days. 26 weeks shows
  seasonal peaks, and those peaks are what a cross-training pool is for.
- **Keep sending them weekly** (or daily), each covering the days since
  the last one. Re-sending a day replaces that day, so overlaps are safe.
- **Files 1 and 2 go up as today:** a fresh copy whenever they change.
  Each upload is treated as the current state.

## Checks the tool will run on upload

- Process names that do not match a `Merged Functions` value are listed,
  not silently dropped.
- Logins in file 4 that are not on the roster or in Hours on Function are
  counted and shown.
- File 4's hours, summed per process per day, are compared with file 3's.
  A day where they differ by more than 10% is flagged.
- Shift patterns on the roster with no configured rotation are listed:
  those people's availability on a date is unknown, and is shown as
  unknown rather than assumed.

## How the four combine for one day (Pack Singles, Monday, X units)

1. **Trained.** From file 2: veterans (home department Pack Singles,
   trained) and cross-trainees (any other home department, trained),
   kept to people still on the roster (file 1).
2. **Scheduled.** From file 1's shift pattern and the rotation: who is
   working that Monday on that shift. Then minus B% unplanned absence,
   giving available veterans and available cross-trainees by department.
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
   rotations and absence, which departments to train from (spare capacity
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
   the shift length.
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
| **Keep · NN% coverage** | The current target reaches 95%, and no target lower by more than the band also reaches it. NN is the current target's coverage. |
| **Increase to N** | The current target is under 95%. N is the smallest target that reaches 95%. |
| **Decrease to N** | A target lower by more than the band still reaches 95%. N is the smallest such. |
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

So the "Why" panel needs no more data than the grid does: files 1–4.

### One difference from the mockup to settle

The mockup names sources by **process** ("Pick → Pack Singles"). The
tool's standards, and Hours on Function, name them by **home department**
(RSP, Receive, …), because a person belongs to a department, not to a
process. RSP covers Pick, Stow and Rebin, among others. The grid will
show department names (e.g. **RSP → Pack Singles**) unless a process
name is preferred, in which case each department needs one process to
represent it.
