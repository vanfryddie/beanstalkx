# Cross Learning Model — LLM layer

Requested 2026-11-26. Not built. This is the brief for the work, written
down so it survives into whichever session builds it.

## What exists today

The Cross Learning Model tab (`templates/_xt_model.html`,
`build_cross_learning_model()` in `application.py`) already computes, per
defined path and shift:

- trained headcount against target, and the gap
- units, hours and the rate they make, against the planned rate
- the constellation: whose hours carried the path, split into the path's
  own source departments and people cross-trained in from elsewhere,
  ranked by department and by individual
- a verdict from a 4×3 decision table (standing × plan attainment) and a
  short list of actions, each costed where a cost is knowable
- routes the floor is running that no standard describes

`build_xt_insights()` already produces per-period history (week, month,
quarter) of volume, hours, trained records and lapse rate, plus a
headcount recommendation derived from the busiest day's volume, cover
factor and shift length.

The numbers are there. What is missing is the reasoning a senior can read,
and the ability to ask a forward-looking question.

## What was asked for

1. **Analyse history** — volume, rate and hours per process per shift,
   against the current targets and the trained headcount, over the
   periods the model already builds.

2. **Judge the targets** — is each target too high or too low, and say so
   with the evidence that led there.

3. **Recommend targets**, with the reasoning attached.

4. **Show the home-department share** — hours worked in a process by its
   own departments versus hours cross-trained in. (The constellation
   already computes this; it needs to be part of the explanation, not just
   a bar.)

5. **A forecasting input** — the user enters a volume and rate forecast,
   and gets back:
   - the optimal cross-training target for that forecast
   - which department the people should come from, and why that department
   - whether a department instead needs to raise its own home headcount
     rather than lend people out

6. **Every prediction and recommendation explains why.** This is the
   requirement the whole feature turns on.

## Decisions to make before building

**Where the model runs.** LDIS holds named Amazon associates, their
compliance status and their hours. Sending that to a model endpoint is a
data-handling decision before it is a technical one. Bedrock inside the
site's own AWS account keeps it in the existing boundary and fits the
`boto3` dependency already in `requirements.txt`; a public API endpoint
needs an egress rule through the Elastic Beanstalk environment and a
conversation with whoever owns that policy. **Confirm this first — it
decides the whole design.** Do not assume.

**What the model is given.** The strong default is to send computed
aggregates — the figures already on the page — and never raw rows with
names in them. The reasoning the feature wants is over rates, targets,
hours and department shares; none of it needs an associate's name. Keep
the per-person detail on the server and out of the prompt.

**What the model is allowed to decide.** The arithmetic — heads needed for
a volume at a rate, cost to close a gap, cover factor — is already
deterministic and tested. It should stay that way. The model's job is to
weigh and explain, not to compute: it should be handed the numbers and
asked which reading is right and why, so that a wrong answer is a wrong
argument rather than a wrong figure on a page. Anything it returns that is
a number the app can check should be checked.

**What happens when it is unavailable.** The tab must still work. The
decision table and the costed actions are the floor; the LLM layer adds
reasoning on top of them. A failed or slow call degrades to what the page
shows today, not to an error.

## Constraints that apply

Everything in `CLAUDE.md` applies, and three things bear directly:

- **No external runtime dependencies.** An HTTPS call to a model endpoint
  is fine from the standard library or from `boto3`, which is already a
  dependency. Do not add an SDK for it.
- **Use the latest Claude models.** Do not pin something older out of
  habit.
- **Test it through `app.test_client()` and show it passing.** The model
  call itself gets stubbed; what gets tested is that the page is right
  with a response, right without one, and never shows a number the
  deterministic layer disagrees with.

## Suggested shape of the work

1. Confirm the hosting decision above.
2. A thin client module — one function, a timeout, a stubbed path for
   tests, no SDK.
3. A prompt built from the aggregates `build_cross_learning_model()`
   already returns, so the model argues about figures the page is showing.
4. The explanation rendered alongside the existing verdict, clearly marked
   as generated, with the deterministic figures still visible beside it.
5. The forecast form: volume and rate in, the deterministic headcount
   maths run first, the model asked to explain the result and to say which
   source department and why — including the case where the answer is "do
   not cross-train for this, staff the home department".
