# Setting up Amazon Bedrock for LDIS — a click-by-click guide

This switches on the **generated explanations** in LDIS: the "Why change
these standards?" panel on **Cross Training → Cross Learning Model**,
and the generated readings further down that tab. Without it, everything
still works. The panel then shows the computed evidence instead of
written explanations.

You do not need to know AWS. Follow the steps in order, and stop at the
checkpoints (✅) to confirm each step worked before moving on.

**Time:** about 30–45 minutes, mostly waiting for AWS.
**You need:** a login to the AWS console for the account that runs LDIS,
with permission to change IAM roles and Elastic Beanstalk settings (an
administrator usually has this). You also need the new LDIS zip file.

> **A note on button names.** AWS renames and moves console buttons from
> time to time. If a button is not exactly where this guide says, look
> for the same words nearby, or use the search box at the top of the
> console. Step 7 runs a check script on the server that reports what is
> wrong in plain words, so a mislabelled button cannot leave you guessing.

---

## Which model, and why

**Use Claude Opus 5.5.** If your account does not offer it, use
**Claude Sonnet 5.5** instead.

- The job is to weigh dozens of figures (targets, rates, hours,
  department shares) and argue for a change in a way a manager can check.
  That is reasoning, and Opus is Anthropic's strongest general model for
  it.
- LDIS calls it rarely: once when the data changes, then it reuses the
  answer for 12 hours. Cost per call barely matters at that volume. For
  prices, search "Amazon Bedrock pricing" and look at your region.
- Sonnet 5.5 is faster and cheaper, and good enough if Opus is not
  available. Switching later is one setting (step 5); no code changes.

---

## Step 1 — Find your LDIS environment's region and its role

1. Sign in to the AWS console.
2. Click the **search box at the top** of the page, type **Elastic
   Beanstalk**, and click **Elastic Beanstalk** in the results.
3. In the left menu click **Environments**. Click the **name** of your
   LDIS environment in the list.
4. **Write down the region.** It is in the **top-right corner** of the
   console, next to your account name, for example *Europe (Ireland)*.
   Click it to see the code, for example `eu-west-1`. Write down the code.
   **Every step below happens in this same region.** If the region ever
   changes in the top-right corner, change it back.
5. In the left menu, under your environment's name, click
   **Configuration**.
6. Find the box called **Service access** (on some consoles: *Security*).
   **Write down the "EC2 instance profile".** It is usually
   `aws-elasticbeanstalk-ec2-role`. This is the identity LDIS uses to
   call AWS.
7. **Write down your account ID.** Click your account name in the
   **top-right corner**. The 12-digit number in the menu is your account
   ID.

✅ You have: a region code, an instance profile name, and a 12-digit
account ID.

## Step 2 — Get access to the Claude model

1. Click the **search box at the top**, type **Bedrock**, and click
   **Amazon Bedrock**.
2. Check the **top-right corner** still shows the region from step 1.
3. In the **left menu**, scroll to the bottom and click **Model access**.
   It may sit under a heading such as *Bedrock configurations* or
   *Configure and learn*. If the menu is collapsed, click the **☰** icon
   at the top left first.

   **If you see a Model access page:**

   4. Click **Modify model access** (or **Enable specific models**, or
      **Manage model access**).
   5. Scroll to **Anthropic**. Tick **Claude Opus 5.5**, and also
      **Claude Sonnet 5.5** as a fallback.
   6. Click **Next** at the bottom.
   7. If a form asks for **use case details** (Anthropic asks once per
      account), fill it in:
      - Company name: your company
      - Company website: your company's site
      - Intended users: *Internal employees*
      - Use case: *Internal learning & development analytics: explaining
        cross-training headcount targets to site managers.*

      Click **Submit**.
   8. Click **Submit** (or **Save changes**) on the review page.
   9. Wait. Refresh the page every few minutes until **Access status**
      for the models says **Access granted**. This usually takes minutes,
      sometimes longer.

   **If there is no Model access page:** your account turns on models
   automatically the first time they are used. Skip to step 3. If step 7
   later reports `marketplace`, step 4 explains the one extra permission
   needed.

✅ Claude Opus 5.5 shows **Access granted**, or there was no Model access
page at all.

## Step 3 — Copy the inference profile ID

Current Claude models are called through an **inference profile**: an
ID that starts with a geography, such as `eu.`, `us.`, `apac.` or
`global.`.

1. Still in Amazon Bedrock, in the **left menu** click
   **Cross-region inference** (it may sit under *Infer* or *Inference
   and assessment*).
2. In the list, find the row for **Claude Opus 5.5**.
   - **For an EU region, pick the profile whose ID starts with `eu.`**.
     It keeps requests inside EU regions. One starting `global.` may send
     them to any AWS region in the world. LDIS sends no names or logins
     to the model, only figures and department names, but staying in the
     EU is the cleaner choice for an EU site.
3. Click the profile's name. **Copy and save two things:**
   - the **Inference profile ID**, for example something starting with
     `eu.anthropic.`;
   - the **Inference profile ARN**, a long line starting with
     `arn:aws:bedrock:`.
4. On the same page, find the list of **models** or **destination
   regions** the profile sends requests to, and **copy every model ARN**
   it shows. Each one looks like
   `arn:aws:bedrock:eu-west-3::foundation-model/…`.

**If you cannot find those ARNs on the page,** use CloudShell instead:

1. Click the **CloudShell icon** (a small `>_` square) in the **top bar**
   of the console. A terminal opens at the bottom of the screen; the first
   time takes about a minute.
2. Paste this and press Enter (it lists the Claude profiles):

```bash
aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'anthropic')].[inferenceProfileId,inferenceProfileArn]" --output table
```

3. Then paste this, replacing `PASTE_PROFILE_ID` with the ID you chose,
   and press Enter (it lists the model ARNs for step 4):

```bash
aws bedrock get-inference-profile --inference-profile-identifier PASTE_PROFILE_ID --query "models[].modelArn" --output text
```

✅ You have saved: one profile ID, one profile ARN, and one or more model
ARNs.

## Step 4 — Allow LDIS to call the model

1. Click the **search box at the top**, type **IAM**, and click **IAM**.
2. In the left menu click **Roles**.
3. In the role list's search box, type the instance profile name from
   step 1 (usually `aws-elasticbeanstalk-ec2-role`) and click it.
4. Find the **Permissions** tab (it is open by default). Click the
   **Add permissions** button, then **Create inline policy**.
5. At the top of the policy editor, click **JSON**.
6. **Delete everything** in the editor box and paste this:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LdisInvokeClaude",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "PASTE_PROFILE_ARN",
        "PASTE_MODEL_ARN_1",
        "PASTE_MODEL_ARN_2"
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

7. Replace the placeholders, keeping the quotation marks:
   - `PASTE_PROFILE_ARN`: the profile ARN from step 3.
   - `PASTE_MODEL_ARN_1`, `PASTE_MODEL_ARN_2`: the model ARNs from step 3,
     one per line. Add a line for each extra ARN (with a comma after every
     line except the last), or delete the spare line if there is only one.
     **The role needs every model ARN the profile lists.** A cross-region
     profile forwards each request to one of several regions, so a missing
     region makes calls fail some of the time.
8. Click **Next** at the bottom.
9. In **Policy name**, type `ldis-bedrock-invoke`.
10. Click **Create policy**.

The second block only lets the step 7 check list which models your
account offers. It reads, it changes nothing, and you can delete it once
everything works.

✅ The role's Permissions tab now lists `ldis-bedrock-invoke`.

**Only if step 7 later says `marketplace`:** on accounts where models
switch on at first use, that first call subscribes the account through
AWS Marketplace, which needs two more permissions once:

1. Go back to this role → **Permissions** → click `ldis-bedrock-invoke` →
   **Edit** → **JSON**.
2. Inside the `"Statement": [ … ]` list, after the last `}` of the second
   block, type a comma and paste:

```json
{
  "Sid": "LdisFirstUseSubscribe",
  "Effect": "Allow",
  "Action": ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"],
  "Resource": "*"
}
```

3. **Next** → **Save changes**. Run step 7 again. Once it says OK, remove
   this block the same way.

## Step 5 — Tell LDIS which model to use

1. Go back to **Elastic Beanstalk** (search box at the top) →
   **Environments** → click your LDIS environment.
2. In the left menu click **Configuration**.
3. Find the box **Updates, monitoring, and logging** and click its
   **Edit** button.
4. Scroll down to **Environment properties**.
5. Click **Add environment property** and fill in:

| Name | Value |
|---|---|
| `LDIS_BEDROCK_MODEL_ID` | the **profile ID** from step 3 (not the ARN) |

6. **If there is no `AWS_REGION` row already**, click **Add environment
   property** again and add:

| Name | Value |
|---|---|
| `AWS_REGION` | the region code from step 1, for example `eu-west-1` |

7. Scroll to the bottom and click **Apply**.
8. Wait. The environment shows **Updating** with a spinner, then turns
   back to **Ok** (green). That takes a few minutes.

> **About `AWS_REGION`:** LDIS's file storage (S3) reads the same
> setting, and without it falls back to `us-east-1`. If your S3 bucket is
> in the environment's region (the usual case), this is correct for both.
> After step 6, upload and download any one file in LDIS to confirm file
> storage still works.

✅ The environment is green again, and the two properties appear under
Environment properties.

## Step 6 — Deploy the new LDIS version

1. In Elastic Beanstalk, click your environment's name to open its
   overview page.
2. Click **Upload and deploy**.
3. Click **Choose file** and pick the new LDIS zip file.
4. In **Version label**, type something you will recognise, for example
   `ldis-xt-standards-2026-11-27`.
5. Click **Deploy**.
6. Wait for the environment to go back to **Ok** (green): a few minutes.

✅ Green, and LDIS opens in your browser as normal.

## Step 7 — Check it from the server

This runs a small check on the LDIS server itself. It uses the role,
region and model the app really uses, and it tells you in plain words
which step, if any, is missing.

1. Click the **search box at the top**, type **EC2**, and click **EC2**.
2. In the left menu click **Instances**.
3. Tick the box next to the instance whose **Name** is your LDIS
   environment's name.
4. Click the **Connect** button at the top.
5. Click the **Session Manager** tab, then **Connect**. A black terminal
   opens in a new browser tab.
   - If Session Manager says it is not available, use the **EC2 Instance
     Connect** tab and **Connect** instead. If neither works, ask whoever
     manages the AWS account to run this step with `eb ssh`.
6. In the terminal, paste each line and press Enter after each:

```bash
sudo -i
```

```bash
cd /var/app/current && source /var/app/venv/*/bin/activate
```

```bash
python3 perf/bedrock_probe.py
```

7. Read the **last line**:

| The check says | What it means | What to do |
|---|---|---|
| `OK - the Cross Learning Model tab will show generated explanations.` | Everything works. | Go to step 8. |
| `iam` | The role may not call the model. | Step 4: check the policy is on the right role, and that every ARN is pasted exactly. |
| `model_access` | The model is not switched on for the account. | Step 2: wait for "Access granted", or finish the use-case form. |
| `marketplace` | First use needs the Marketplace permission. | The "Only if step 7 says marketplace" part of step 4, once. |
| `needs_profile` | The setting holds a bare model ID. | Step 5: use the **profile ID** (starts `eu.`, `us.` and so on). |
| `timeout` | No answer in time. | Run it again. If it repeats, the server may have no internet route to Bedrock. Ask whoever manages the network for a NAT gateway or a Bedrock VPC endpoint. |
| `LDIS_BEDROCK_MODEL_ID is not set` | Step 5 did not apply. | Redo step 5 and wait for green. |

The check also lists which Claude models and profiles your account
offers. To try a different one without changing settings:

```bash
python3 perf/bedrock_probe.py PASTE_ANOTHER_PROFILE_ID
```

8. Type `exit` twice and close the tab.

## Step 8 — See it in LDIS

1. Open LDIS → **Cross Training** → **Cross Learning Model**.
2. Until your own process-day and associate-day files are loaded, tick
   **Sample data** to see the page working.
3. Within a minute, entries in **Why change these standards?** change
   from the computed evidence to short written explanations, each marked
   **Generated**, and the footer reads **Amazon Bedrock explanations**.
4. Under every standard in the grid an **AI:** line appears. It is the
   model's own prediction, with the coverage LDIS recomputed for it. The
   **Why change these standards?** panel opens with a summary: how many
   predictions agree with the replay, how many differ, and how many were
   refused for breaking a staffing policy. Each change also shows the AI
   prediction in full, with its confidence and the checks.

If something goes wrong later, the page keeps the computed evidence and
carries on. To see why:

1. Elastic Beanstalk → your environment → **Logs** in the left menu.
2. **Request logs** → **Last 100 lines** → **Download** (or the link that
   appears).
3. Look for a line containing `Bedrock call failed (`. The word in
   brackets is one of the reasons in the table in step 7.

---

## Switching it off, or changing model

- **Off:** Step 5, delete the `LDIS_BEDROCK_MODEL_ID` row, **Apply**.
  LDIS goes back to computed evidence only.
- **Different model:** Step 5, change the value to another profile ID,
  **Apply**. If the new model's profile lists different model ARNs, add
  them to the policy in step 4.

## What LDIS sends, and what it does with the answer

- **Sent:** per proposed change, the figures the page already shows:
  targets, coverage, replayed shifts, how often each department could
  lend, rates, and department and process names. **No associate names or
  logins.** A test fails if one is ever added.
- **Checked:** any sentence from the model that quotes a number the page
  did not compute is thrown away before display.
- **Predictions:** the model proposes its own target per standard,
  weighing the season ahead, the 40 training hours a head and your
  feedback. LDIS checks every proposal on the same replay before showing
  it:
  - a pick that breaks a staffing policy is refused;
  - the coverage and training hours shown are LDIS's own, not the
    model's;
  - a pick below the bar is labelled so.

  The computed verdict stays in the grid beside it.
- **When it fails:** missing access, timeouts and odd answers all leave
  the page showing the computed evidence. The tab never waits on the model.
