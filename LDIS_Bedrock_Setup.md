# Setting up Amazon Bedrock for the Cross Learning Model

This switches on the generated explanations on
**Cross Training → Cross Learning Model**. Until it is set up, the tab works
exactly as before, plus two new sections that need no model at all: the
target review ("Are the targets right?") and the forecast form ("Plan for a
forecast").

Everything below happens in the AWS console of **the account and region the
Elastic Beanstalk environment runs in**. Allow about 30 minutes, most of it
waiting for model access.

> **About the button names.** This guide was written without access to the
> AWS documentation or your console, so menu labels may differ slightly from
> what you see. Every step that matters is checked by a script you run on the
> server (step 6), which reports what your account actually allows. The AWS
> CLI commands below report the same facts and do not depend on the console
> layout.

---

## Which model, and why

**Use Claude Opus 5.5 (Anthropic)**, through an inference profile in your
geography (`eu.` if your environment is in an EU region).

- **The job is judgement, not volume.** The model weighs 60 paths of
  targets, rates, hours and department shares, then argues for a reading. How
  well it reasons is what the feature is for. Opus is Anthropic's strongest
  general model below the far more expensive Fable tier.
- **The volume is tiny.** A review runs only when the data behind the tab
  changes. Answers are cached for 12 hours, and a forecast is one short call.
  Expect a few calls a day, so the price difference between models hardly
  matters. Bedrock prices its own per-token rates; check the Bedrock pricing
  page for your region.
- **The fallback is Claude Sonnet 5.5.** If Opus 5.5 is not offered in your
  region, or access is slow to come through, Sonnet 5.5 is faster and
  cheaper. Switching is one environment property (step 5); no code changes.
- **Not Haiku.** It is good at short classification, but weaker at the
  multi-factor argument this tab exists to show.
- **Not Fable.** It is several times the price, for a task that does not
  need it.

Do not type a model id from this page or from memory. Bedrock ids are
region-prefixed and they change. Copy the id your account shows you (step 3).

---

## Step 1 — Find the environment's region and instance role

1. Open **Elastic Beanstalk** → **Environments** → your LDIS environment.
2. The **region** is in the top-right corner of the console (for example
   *Europe (Ireland) eu-west-1*). Use this same region for every step below.
3. Open **Configuration** → **Service access**. Note the **EC2 instance
   profile**. On most environments it is `aws-elasticbeanstalk-ec2-role`.
   This is the identity the app calls Bedrock as.

## Step 2 — Get access to the model

1. Open **Amazon Bedrock** in that region.
2. Find **Model access** in the left menu (on newer consoles it may sit under
   *Configure* or *Bedrock configurations*).
   - **If there is a Model access page:** choose *Modify model access* or
     *Request access*, tick **Anthropic → Claude Opus 5.5** (and **Claude
     Sonnet 5.5** as the fallback), and submit.
   - **If there is no such page,** your console grants access to serverless
     models on first use. Nothing to tick. Step 4's optional Marketplace
     statement may be needed once instead.
3. **Anthropic asks once per account for a short use-case form.** If the
   console prompts for it, fill it in: a company name, a website, and a
   sentence such as *"Internal learning & development analytics: explaining
   cross-training headcount targets to site managers."*
4. Wait until the model shows **Access granted** (usually minutes, sometimes
   longer).

## Step 3 — Copy the inference profile id

Current Claude models are served through **inference profiles**, ids that
start with a geography prefix such as `eu.`, `us.`, `apac.` or `global.`.
A bare model id gets refused with an "on-demand throughput isn't supported"
error.

1. In Bedrock, open **Cross-region inference** (sometimes under *Infer* or
   *Inference profiles*).
2. Find **Claude Opus 5.5**. Prefer the profile for your own geography, for
   example `eu.` for an EU region. A `global.` profile may route requests to
   any AWS region worldwide. The app sends no names or logins (only rates,
   hours, targets and department names), but staying in-geography is the
   cleaner choice for an EU site.
3. Copy two things: the **inference profile ID** and its **ARN**.

Or from CloudShell (the `>_` icon in the console header), in the same region:

```bash
aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'anthropic')].[inferenceProfileId,inferenceProfileArn]" --output table
```

Then list the model ARNs that profile routes to. You need them in step 4:

```bash
aws bedrock get-inference-profile --inference-profile-identifier PASTE_PROFILE_ID --query "models[].modelArn" --output text
```

## Step 4 — Let the instance role call it

1. Open **IAM** → **Roles** → the role from step 1
   (`aws-elasticbeanstalk-ec2-role`).
2. **Add permissions** → **Create inline policy** → **JSON**, and paste the
   policy below. Replace the placeholders:
   - `PROFILE_ARN`: the inference profile ARN from step 3.
   - The `foundation-model` lines: every ARN the `get-inference-profile`
     command printed. A cross-region profile forwards each request to the
     model in one of several regions, so the role needs the model in each of
     them, not only your own.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LdisInvokeClaude",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "PROFILE_ARN",
        "arn:aws:bedrock:REGION_1::foundation-model/MODEL_ID",
        "arn:aws:bedrock:REGION_2::foundation-model/MODEL_ID"
      ]
    },
    {
      "Sid": "LdisProbeListsModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels",
        "bedrock:ListInferenceProfiles",
        "bedrock:GetInferenceProfile"
      ],
      "Resource": "*"
    }
  ]
}
```

3. Name it `ldis-bedrock-invoke` and save.

The second statement only lets the step 6 probe list what the account offers.
It is read-only, and you can remove it once everything works.

**Optional, only if the probe reports `marketplace`:** on accounts where
access is granted on first use, the first call subscribes the account to the
model through AWS Marketplace. To allow that once, add this statement, run
the probe again, then delete the statement:

```json
{
  "Sid": "LdisFirstUseSubscribe",
  "Effect": "Allow",
  "Action": ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"],
  "Resource": "*"
}
```

## Step 5 — Tell the app which model to use

1. **Elastic Beanstalk** → your environment → **Configuration** →
   **Updates, monitoring, and logging** → **Edit** → scroll to
   **Environment properties**.
2. Add:

| Name | Value |
|---|---|
| `LDIS_BEDROCK_MODEL_ID` | the inference profile **ID** from step 3, e.g. what starts `eu.anthropic.` |
| `AWS_REGION` | the region from step 1, e.g. `eu-west-1`, if not already set |

3. **Apply.** The environment restarts with the new values.

Optional properties (leave unset unless needed):

| Name | Default | When to set it |
|---|---|---|
| `LDIS_BEDROCK_TIMEOUT` | `60` | Seconds to wait for an answer. Keep it under 100: gunicorn stops a worker at 120. |
| `LDIS_BEDROCK_ANTHROPIC_VERSION` | `bedrock-2023-05-31` | Only if the probe's log shows Bedrock rejecting the `anthropic_version` field. |

> **About `AWS_REGION`:** the app's S3 client reads the same variable, and
> falls back to `us-east-1` when it is unset. If the S3 bucket is in the
> environment's region (the usual case), setting it is correct for both. After
> applying, upload and download one file to confirm S3 still works.

To switch off the generated explanations, delete `LDIS_BEDROCK_MODEL_ID`.
To switch model, change it.

## Step 6 — Deploy, then prove it from the server

1. Deploy the new zip: **Elastic Beanstalk** → environment → **Upload and
   deploy**.
2. Open a shell on the instance, with `eb ssh` or with **EC2 → the instance →
   Connect → Session Manager**. Then:

```bash
sudo -i
```

```bash
cd /var/app/current && source /var/app/venv/*/bin/activate && python3 perf/bedrock_probe.py
```

The probe reads the environment properties itself, prints the region, the
role it runs as and the models and profiles the account offers, then makes
one real call through the app's own code. It ends with one of:

| Result | What to do |
|---|---|
| `OK - the Cross Learning Model tab will show generated explanations.` | Done. |
| `iam` | Step 4: the policy is missing, or names a different profile or model ARN. |
| `model_access` | Step 2: access not granted yet, or the use-case form is outstanding. |
| `marketplace` | Step 4's optional Marketplace statement, once. |
| `needs_profile` | Step 3: `LDIS_BEDROCK_MODEL_ID` holds a bare model id. Use the profile id. |
| `timeout` | Try again. If it repeats, the instance may have no route to `bedrock-runtime` (a private subnet needs a NAT gateway or a Bedrock VPC endpoint). |

To try another id without changing the environment:

```bash
python3 perf/bedrock_probe.py PASTE_ANOTHER_PROFILE_ID
```

## Step 7 — Check the tab

Open **Cross Training → Cross Learning Model**. The page loads as before.
Within a minute:

- an overview appears under **What it concluded**, marked *Generated
  explanation*;
- each card gets a dashed box with a judgement (*Target too high / too low /
  About right*), the target the model suggests next to the computed one, and
  its reasons;
- the **Are the targets right?** table fills its *Generated reading* column;
- a forecast you enter gets a generated recommendation: which department to
  draw people from and why, or whether to raise home headcount instead.

If something goes wrong, the page shows one quiet line ("No generated
explanations: …") and everything else stays as it was. Server-side
details are in **Logs → Request logs → Last 100 lines**: look for
`Bedrock call failed (<reason>)`.

---

## How it behaves, for reference

- **What is sent.** Per path: targets, trained counts, volume, hours, rate
  against plan, the computed recommendation and how it was worked out, and
  the hours by own department and by people cross-trained in, with
  department names. **No associate names or logins.** A test fails if any are
  ever added.
- **What the model may change.** Nothing that is computed. Its suggested
  target is shown *beside* the computed one, flagged when they differ. A
  departure with no valid reason is discarded.
- **What is checked.** Any sentence quoting a number the page does not
  show is removed before display, and the page says how many were removed.
  A department the forecast did not offer as a candidate is refused. A
  judgement that contradicts its own suggested target is dropped.
- **Speed.** The tab never waits for the model. The review is asked in
  batches of 15 paths at once, so the wait is the slowest batch. Each answer
  is cached for 12 hours per server process, until the data changes.
- **When it fails.** Missing access, timeouts, throttling, refusals and
  unreadable answers all leave the page as it was without the model.
