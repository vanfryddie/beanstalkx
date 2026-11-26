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

## Where the model runs — decided

**Amazon Bedrock, in the same AWS account as the Elastic Beanstalk
environment.** Decided 2026-11-26. The data stays inside the account's own
boundary, and it is the same credential path the app already uses for S3
and Tickety rather than a new one.

Verified against the pinned dependency, not assumed:

- `boto3==1.34.162` already in `requirements.txt` exposes the
  `bedrock-runtime` client, with `converse`, `converse_stream`,
  `invoke_model` and `invoke_model_with_response_stream`. **No new
  dependency is needed**, which is what keeps this inside the project's
  no-new-dependency rule. Prefer `converse` — it is the model-agnostic
  shape and avoids hand-rolling a request body per model.
- The app reads its region from `AWS_REGION`, defaulting to `us-east-1`
  (see `db.py` for the S3 client). Bedrock should follow the same
  variable rather than inventing a second one, and the default should be
  the environment's real region rather than inherited by accident.

Three things that will block it, none of them code, all outside this repo:

- **The EB instance profile needs an IAM policy** allowing the Bedrock
  invoke action on the model it will call. The role is the one the
  environment already runs under; this is an addition to it.
- **Model access has to be enabled** for that account and region in the
  Bedrock console. A brand-new account has none enabled and the call
  fails with an access error that reads like a permissions bug.
- **Not every model is in every region.** Check what the account can
  actually reach — `bedrock:ListFoundationModels`, or the console — and
  use a cross-region inference profile if the region alone does not have
  it.

**Do not hard-code a model id from memory.** Bedrock ids are
region-prefixed and they change; read the account's own list and pick the
latest Claude model it offers. Load the `claude-api` skill before writing
the call.

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

- **No external runtime dependencies.** The call goes through the `boto3`
  already in `requirements.txt`. Do not add an SDK for it, and do not
  bump boto3 without a reason — the version pinned today already has
  what this needs.
- **Use the latest Claude model the account offers.** Do not pin
  something older out of habit, and do not write an id from memory.
- **Test it through `app.test_client()` and show it passing.** The model
  call itself gets stubbed; what gets tested is that the page is right
  with a response, right without one, and never shows a number the
  deterministic layer disagrees with.

## Suggested shape of the work

1. Confirm the account can reach a model at all — region, enabled model
   access, and an instance-profile policy — before building anything on
   top of it. A five-line script that calls `converse` once and prints
   the reply settles it, and settles it now rather than at the end.
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
