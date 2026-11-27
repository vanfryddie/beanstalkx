# Planning data templates

Filled-in examples of the two files the Cross Learning Model's
standards evaluation needs and no existing export provides. Generated
from the same in-memory sample the page shows with **Sample data**
switched on, so they always load. The people are invented (logins
`smp00001`…); the process and department names are SCN2's.

| File | One row per | Covers |
|---|---|---|
| `process_day_template.csv` | date × shift × process | one week |
| `associate_day_template.csv` | date × shift × associate × process | one day |

Columns, rules and why each exists: `LDIS_Day_Plan_Data_Spec.md`.
The same templates, built around your own standards, download from
**L&D Settings → Data Upload → Cross-training planning data**.

Two things the templates show that are easy to miss:

- `associate_day` carries **indirect hours too** (process `Indirect`,
  units 0). Without them, someone who came in but was not needed on a
  direct process looks absent, and their department looks shorter than
  it was.
- `fclm_function` is optional. Fill it, and leave `process` empty, if
  your export only has FCLM function names; map those names to processes
  once, under Configuration on the Cross Learning Model tab.
