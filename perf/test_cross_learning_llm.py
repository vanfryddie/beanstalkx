"""The Cross Learning Model's language-model layer.

The model call itself is stubbed throughout: what is tested is that the
page is right with a reply, right without one, right when the reply is
slow, wrong or hostile, and never shows a number the deterministic layer
disagrees with. The arithmetic the model is handed — target review and
forecast — is tested on its own, conclusions first: the same evidence
gives the same verdict, and the verdict moves when the evidence does.
"""
import json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LDIS_BEDROCK_MODEL_ID", None)
import db, application, bedrock_client

fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def client(login="admin", role="admin"):
    application.application.config["TESTING"] = True
    c = application.application.test_client()
    with c.session_transaction() as s:
        s["login"], s["role"], s["auth_source"], s["site"] = login, role, "manual", "SCN2"
    return c


def reply_with(obj, stop="end_turn"):
    """A stub transport answering with `obj` as the model's text, and
    counting how often it was called."""
    calls = []

    def transport(body):
        calls.append(body)
        text = obj if isinstance(obj, str) else json.dumps(obj)
        return {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
                "stop_reason": stop, "usage": {"input_tokens": 1, "output_tokens": 1}}
    transport.calls = calls
    return transport


def raising(exc):
    def transport(body):
        raise exc
    return transport


def client_error(code, message):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": message}}, "InvokeModel")


db.set_current_site("SCN2")
db.init_db()
conn = db.get_db()
has_volume = conn.execute("SELECT COUNT(*) AS n FROM process_volume").fetchone()["n"]
conn.close()
if not has_volume:
    # Build the conditions rather than trust whichever database this
    # was pointed at: the review needs volume to have anything to judge.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "seed_xt_insights", os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_xt_insights.py"))
    seeder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seeder)
    seeder.seed()

ctx = application.application.test_request_context()
ctx.push()
db.begin_request_scope()
MODEL = application.build_cross_learning_model("month")
PAYLOAD = application._clm_review_payload(MODEL)

print("1. The target review, by arithmetic alone")
check("there are paths to review", len(MODEL["reviews"]) > 5, len(MODEL["reviews"]))
for row in MODEL["reviews"]:
    move = row["recommendation"]["recommended"] - row["target"]
    want = "too_low" if move > 1 else "too_high" if move < -1 else "about_right"
    if row["review"]["judgement"] != want:
        check(f"{row['path_key']} judged by its own figures", False, row["review"])
        break
else:
    check("every judgement follows from recommended against target", True)
review = application._clm_target_review
rec = {"recommended": 20}
check("20 needed against 30 is too high", review({"target": 30}, rec)["judgement"] == "too_high")
check("20 against 10 is too low", review({"target": 10}, rec)["judgement"] == "too_low")
check("20 against 21 is about right (same band as the decision table)",
      review({"target": 21}, rec)["judgement"] == "about_right")
check("no volume means it cannot be judged", review({"target": 21}, None)["judgement"] == "cannot_judge")
check("no target means it cannot be judged", review({"target": 0}, rec)["judgement"] == "cannot_judge")

# The model tab and the Insights tab must quote one target per path.
insights = application.build_xt_insights("month")
by_key = {application._clm_path_key(p): (p.get("recommendation") or {}).get("recommended")
          for p in insights["paths"]} if "paths" in insights else {}
if by_key:
    mismatched = [r["path_key"] for r in MODEL["rows"]
                  if r["path_key"] in by_key and (r["recommendation"] or {}).get("recommended") != by_key[r["path_key"]]]
    check("the model tab and Insights recommend the same target for every path", not mismatched, mismatched[:3])
pool = application._xt_size_pool
check("pool sizing rounds up", pool(4.1, 1.5, None) == (7, False, False))
check("and is capped by the source pool", pool(40, 1.5, 30) == (30, True, False))
check("and says when the pool is empty", pool(4, 1.5, 0)[2] is True)

print("\n2. The forecast, by arithmetic alone")
row = MODEL["reviews"][0]
cost = MODEL["cost_model"]
f = application._clm_forecast(MODEL, row["path_key"], 4000, 100)
on_shift = 4000 / 100 / cost["shift_hours"]
expected, limited, _ = pool(on_shift, cost["cover_factor"], row.get("headcount"))
check("hours of work is volume over rate", f["hours_needed"] == 40.0, f["hours_needed"])
check("people on shift is hours over the shift length", f["on_shift"] == round(on_shift, 1), f["on_shift"])
check("the target is that, times cover, capped by the pool", f["target"] == expected, (f["target"], expected))
check("hand-worked: 4000 at 100/h on a 10h shift at cover 1.5 is 6",
      pool(4000 / 100 / 10, 1.5, None)[0] == 6)
check("the gap is the target less those trained", f["short_by"] == max(0, expected - (row["trained"] or 0)))
train_hours = (cost["per_process"].get(row.get("process") or "") or {}).get("train_hours") or cost["default_train_hours"]
check("and is costed at the path's own training hours",
      f["cost_to_close"] == round(f["short_by"] * train_hours * cost["hourly_cost"]))
check("a zero rate is refused rather than divided by", application._clm_forecast(MODEL, row["path_key"], 4000, 0) is None)
check("an unknown path is refused", application._clm_forecast(MODEL, "nope:1:early", 4000, 100) is None)

# The source decision, on a model built so the evidence can be moved.
real_home = db.xt_process_home_departments


def toy(stretch_b=False, stretch_a=False, headcount=50, trained_b=4):
    """A path drawn from department A, also worked by B and C. A, B and
    C each own a process whose state is set by the arguments."""
    db.xt_process_home_departments = lambda: {"Pick": "Pick Dept", "Stow A": "Dept A",
                                              "Stow B": "Dept B", "Stow C": "Dept C"}

    def path(key, label, process, title, outside=0.1):
        return {"kind": "standard", "id": key, "shift": "early", "label": label, "process": process,
                "path_key": f"standard:{key}:early", "target": 10, "trained": 6,
                "headcount": headcount, "source_departments": ["Dept A"],
                "plan": {"rate": 100, "plan_rate": 100, "pct_to_plan": 90},
                "situation": {"title": title},
                "constellation": {"outside_share": outside, "home_share": 1 - outside}}
    target = path(1, "Pick from A", "Pick", "Settled")
    target["constellation"].update({"home_share": 0.5, "outside_share": 0.5, "by_department": [
        {"department": "Dept A", "hours": 100.0, "people": 5, "trained": 2},
        {"department": "Dept B", "hours": 80.0, "people": 5, "trained": trained_b},
        {"department": "Dept C", "hours": 20.0, "people": 2, "trained": 1}]})
    rows = [target,
            path(2, "Stow A", "Stow A", "Genuinely short" if stretch_a else "Settled"),
            path(3, "Stow B", "Stow B", "Genuinely short" if stretch_b else "Settled"),
            path(4, "Stow C", "Stow C", "Settled")]
    return {"rows": rows, "cost_model": cost}


try:
    f = application._clm_forecast(toy(), "standard:1:early", 4000, 100)
    check("candidates are the named source, then everyone already working it",
          [c["department"] for c in f["candidates"]][:1] in (["Dept A"], ["Dept B"]) and
          {c["department"] for c in f["candidates"]} == {"Dept A", "Dept B", "Dept C"})
    check("first pick is the department with the most people already trained on it",
          f["suggested_source"] == "Dept B", f["suggested_source"])
    check("and that is not a case for raising home headcount", f["raise_home_headcount"] is False)
    f = application._clm_forecast(toy(stretch_b=True), "standard:1:early", 4000, 100)
    check("when B is short on its own work, the pick moves off B",
          f["suggested_source"] != "Dept B" and f["suggested_source"] is not None, f["suggested_source"])
    f = application._clm_forecast(toy(stretch_b=True, stretch_a=True), "standard:1:early", 4000, 100)
    check("a department leaning on borrowed help counts as short too", f["suggested_source"] == "Dept C"
          or f["suggested_source"] is None, f["suggested_source"])
    f = application._clm_forecast(toy(headcount=5), "standard:1:early", 40000, 100)
    check("a forecast bigger than the source pool says staff the department",
          f["raise_home_headcount"] and "pool_limited" in f["raise_home_reasons"], f["raise_home_reasons"])
    check("and names the process's home department", f["home_department"] == "Pick Dept")
finally:
    db.xt_process_home_departments = real_home

print("\n3. What leaves the server")
conn = db.get_db()
people = conn.execute("SELECT DISTINCT employee_login, full_name FROM xt_hours").fetchall()
conn.close()
review_text = json.dumps(PAYLOAD)
forecast_text = json.dumps(application._clm_forecast_payload(
    application._clm_forecast(MODEL, MODEL["reviews"][0]["path_key"], 4000, 100)))
leaked = [p["employee_login"] for p in people
          if p["employee_login"] and len(p["employee_login"]) > 3 and
          (f'"{p["employee_login"]}"' in review_text or f'"{p["employee_login"]}"' in forecast_text)]
named = [p["full_name"] for p in people
         if p["full_name"] and len(p["full_name"]) > 5 and
         (p["full_name"] in review_text or p["full_name"] in forecast_text)]
check("no login is sent to the model", not leaked, leaked[:3])
check("no name is sent to the model", not named, named[:3])
check("the review payload is bounded", len(PAYLOAD["paths"]) <= application.CLM_LLM_MAX_PATHS)
check("and carries the own-department share it is asked to reason about",
      any(p["own_department_share"] is not None for p in PAYLOAD["paths"]))
db.end_request_scope()
ctx.pop()

print("\n4. Vetting what comes back")
first = PAYLOAD["paths"][0]
vet = application._clm_vet_review


def one(item):
    return vet({"paths": [dict(item, key=first["key"])]}, PAYLOAD)["paths"].get(first["key"])


good = f"The target is {first['current_target']} with {first['trained']} trained."
made_up = "The rate fell to 9876.5 units an hour."
got = one({"judgement": "about_right", "reasons": [good, made_up]})
check("a sentence quoting the page's own figures is kept", got and good in got["reasons"])
check("a sentence quoting a figure the page does not show is removed", got and made_up not in got["reasons"])
if first.get("own_department_share") is not None:
    pct = round(first["own_department_share"] * 100)
    got = one({"judgement": "about_right", "reasons": [f"{pct}% of hours came from its own departments."]})
    check("a share may be quoted as a percentage", got and got["reasons"])
cur = first["current_target"] or 10
got = one({"judgement": "too_high", "recommended_target": cur + 5, "reasons": [good]})
check("calling a target too high while recommending more is dropped",
      got is not None and got["judgement"] is None and got["recommended_target"] is None, got)
if first.get("recommended") is not None:
    other = first["recommended"] + 7
    got = one({"judgement": None, "recommended_target": other, "reasons": [made_up]})
    check("a departure from the computed target with no surviving reason is dropped",
          got is None or got["recommended_target"] is None, got)
    got = one({"recommended_target": other, "reasons": [good]})
    check("one with a reason is kept, flagged, and the computed target travels with it",
          got and got["disagrees"] and got["computed_target"] == first["recommended"], got)
check("a path key the payload never sent is ignored",
      not vet({"paths": [{"key": "invented:1:early", "judgement": "too_high", "reasons": [good]}]}, PAYLOAD)["paths"])
check("a judgement outside the four is ignored", (one({"judgement": "fire everyone", "reasons": [good]}) or {}).get("judgement") is None)

fpay = {"candidates": [{"department": "Dept A"}, {"department": "Dept B"}], "home_department": "Pick Dept",
        "target": 7, "suggested_source": "Dept B", "raise_home_headcount": False}
fv = application._clm_vet_forecast
got = fv({"source_department": "Dept Z", "reasons": ["Target of 7 holds."]}, fpay)
check("a department the payload did not offer is refused", got and got["source_department"] is None and got["dropped"])
got = fv({"source_department": "dept b", "raise_home_headcount": False, "reasons": ["Target of 7 holds."]}, fpay)
check("a candidate is matched however it is cased, and agreement is noticed",
      got and got["source_department"] == "Dept B" and got["agrees_on_source"])
got = fv({"source_department": "Dept A", "raise_home_headcount": True, "department_to_grow": "Pick Dept",
          "reasons": ["Target of 7 holds."]}, fpay)
check("disagreeing with the arithmetic is flagged, not hidden", got and not got["agrees_on_source"]
      and not got["agrees_on_headcount"] and got["department_to_grow"] == "Pick Dept")
check("a reply with nothing checkable left is no reply", fv({"reasons": ["It needs 4242 more."]}, fpay) is None)
check("bare JSON is read", bedrock_client.extract_json('{"a": 1}') == {"a": 1})
check("so is JSON in a code fence", bedrock_client.extract_json('```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}})
check("and prose is not", bedrock_client.extract_json("I think the target is fine.") is None)

print("\n5. The page without a model")
bedrock_client.set_transport(None)
c = client()
r = c.get("/ld-management/cross-training?tab=model", follow_redirects=True)
html = r.get_data(as_text=True)
check("the tab renders", r.status_code == 200)
check("with the target review", "Are the targets right?" in html)
check("and the forecast form", 'name="fc_volume"' in html)
check("and no generated slots or loader", "data-llm-slot" not in html and "xt_model_explain" not in html
      and "/model/explain" not in html)
r = c.get("/ld-management/cross-training/model/explain")
check("the explain endpoint answers 'off' without an error", r.status_code == 200 and r.get_json()["status"] == "off"
      and not r.get_json()["slots"])
key = MODEL["reviews"][0]["path_key"]
r = c.get(f"/ld-management/cross-training?tab=model&fc_path={key}&fc_volume=4000&fc_rate=100", follow_redirects=True)
html = r.get_data(as_text=True)
check("a forecast is worked out with no model at all", r.status_code == 200 and "Cross-training target" in html)
r = c.get(f"/ld-management/cross-training?tab=model&fc_path={key}&fc_volume=abc&fc_rate=100", follow_redirects=True)
check("nonsense in the form is answered, not raised", r.status_code == 200 and "cannot be worked out" in r.get_data(as_text=True))

print("\n6. The page with a model")
reasons_for = {p["key"]: [f"Trained stands at {p['trained']} against {p['current_target']}."] for p in PAYLOAD["paths"]}
stub = reply_with({"overview": "Most targets sit away from the work.",
                   "paths": [{"key": k, "judgement": "about_right", "reasons": v + ["Down to 9876.5 an hour."]}
                             for k, v in reasons_for.items()]})
bedrock_client.set_transport(stub)
html = c.get("/ld-management/cross-training?tab=model", follow_redirects=True).get_data(as_text=True)
check("slots and the loader appear once a model is configured",
      "data-llm-slot" in html and "/model/explain" in html)
r = c.get("/ld-management/cross-training/model/explain?period=month")
data = r.get_json()
check("the explain endpoint fills slots", r.status_code == 200 and data["status"] == "ok" and data["slots"], data.get("status"))
card_keys = [k for k in data["slots"] if k.startswith("card:")]
check("one card slot and one review slot per path answered",
      len(card_keys) == len([k for k in data["slots"] if k.startswith("review:")]) == len(reasons_for))
sample = data["slots"][card_keys[0]]
check("every fragment says it was generated", all("Generated explanation" in v for k, v in data["slots"].items()
                                                   if k.startswith("card:") or k == "overview"))
check("the fabricated figure never reaches the page", not any("9876" in v for v in data["slots"].values()))
check("and the page says something was removed", data["dropped"] >= len(reasons_for) and "removed" in data["slots"]["overview"])
check("the payload sent was the aggregates", json.loads(stub.calls[0]["messages"][0]["content"])["paths"][0]["key"]
      == PAYLOAD["paths"][0]["key"])
check("thinking blocks in the reply are skipped, not read as text", data["slots"])
batches = math.ceil(len(PAYLOAD["paths"]) / application.CLM_LLM_BATCH)
check("the paths are asked about in batches", len(stub.calls) == batches and
      all(len(json.loads(b["messages"][0]["content"])["paths"]) <= application.CLM_LLM_BATCH for b in stub.calls),
      (len(stub.calls), batches))
c.get("/ld-management/cross-training/model/explain?period=month")
check("the same data does not pay for a second call", len(stub.calls) == batches, len(stub.calls))

if batches > 1:
    def one_batch_fails(body):
        sent = json.loads(body["messages"][0]["content"])
        if sent["paths"][0]["key"] == PAYLOAD["paths"][0]["key"]:
            raise RuntimeError("first batch lost")
        return stub(body)
    bedrock_client.set_transport(one_batch_fails)
    data = c.get("/ld-management/cross-training/model/explain?period=month").get_json()
    answered = {k.split(":", 1)[1] for k in data["slots"] if k.startswith("card:")}
    check("a batch that fails costs only its own paths", data["status"] == "ok" and answered and
          PAYLOAD["paths"][0]["key"] not in answered and len(answered) < len(PAYLOAD["paths"]))
    check("and the page says some readings are missing", "did not come back" in data["slots"].get("overview", ""))
hostile = reply_with({"paths": [{"key": PAYLOAD["paths"][0]["key"], "judgement": "about_right",
                                 "reasons": [f"<script>alert(1)</script> trained {PAYLOAD['paths'][0]['trained']}."]}]})
bedrock_client.set_transport(hostile)
data = c.get("/ld-management/cross-training/model/explain?period=month").get_json()
check("what the model writes is escaped like any other text",
      not any("<script>" in v for v in data["slots"].values()) and any("&lt;script&gt;" in v for v in data["slots"].values()))

fkey = MODEL["reviews"][0]["path_key"]
bedrock_client.set_transport(None)
ctx = application.application.test_request_context(); ctx.push(); db.begin_request_scope()
fc = application._clm_forecast(application.build_cross_learning_model("month"), fkey, 4000, 100)
db.end_request_scope(); ctx.pop()
pick = fc["candidates"][0]["department"] if fc["candidates"] else None
stub = reply_with({"target_verdict": f"A target of {fc['target']} covers it.",
                   "source_department": pick or "Nobody", "raise_home_headcount": False,
                   "reasons": [f"{fc['trained']} are trained today.", "It will take 31337 hours."],
                   "summary": f"Train {fc['short_by']} more."})
bedrock_client.set_transport(stub)
r = c.get(f"/ld-management/cross-training/model/forecast-explain?period=month&fc_path={fkey}&fc_volume=4000&fc_rate=100")
data = r.get_json()
check("the forecast endpoint fills its slot", data["status"] == "ok" and "forecast" in data["slots"], data)
frag = data["slots"].get("forecast", "")
check("with the source department named", (pick or "no department") in frag)
check("and the invented figure removed", "31337" not in frag and data["dropped"] >= 1)
bad_fc = c.get("/ld-management/cross-training/model/forecast-explain?fc_path=x&fc_volume=1&fc_rate=1").get_json()
check("a forecast it cannot work out is no call at all", bad_fc["status"] == "nothing" and len(stub.calls) == 1)

print("\n7. Every way the call can fail leaves the page as it was")
from botocore.exceptions import ReadTimeoutError
failures = [
    ("timeout", raising(ReadTimeoutError(endpoint_url="https://bedrock-runtime"))),
    ("iam", raising(client_error("AccessDeniedException",
                                 "User: arn:aws:sts::1:assumed-role/x is not authorized to perform: bedrock:InvokeModel"))),
    ("model_access", raising(client_error("AccessDeniedException",
                                          "You don't have access to the model with the specified model ID."))),
    ("marketplace", raising(client_error("AccessDeniedException",
                                         "Model access is denied due to IAM user or service role is not authorized "
                                         "to perform the required AWS Marketplace actions (aws-marketplace:Subscribe)"))),
    ("needs_profile", raising(client_error("ValidationException",
                                           "Invocation of model ID x with on-demand throughput isn't supported."))),
    ("throttled", raising(client_error("ThrottlingException", "Too many requests"))),
    ("error", raising(RuntimeError("anything else"))),
    ("bad_json", reply_with("The targets look broadly fine to me.")),
    ("max_tokens", reply_with('{"paths": [', stop="max_tokens")),
    ("refused", reply_with("", stop="refusal")),
]
for expected_status, transport in failures:
    bedrock_client.set_transport(transport)
    r = c.get("/ld-management/cross-training/model/explain?period=month")
    data = r.get_json()
    check(f"{expected_status}: 200, no slots, a plain reason",
          r.status_code == 200 and data["status"] == expected_status and not data["slots"] and data.get("note"),
          data)
    r = c.get(f"/ld-management/cross-training?tab=model&fc_path={fkey}&fc_volume=4000&fc_rate=100", follow_redirects=True)
    if r.status_code != 200 or "Cross-training target" not in r.get_data(as_text=True):
        check(f"{expected_status}: the tab and its forecast still render", False)
bedrock_client.set_transport(raising(RuntimeError("boom")))
real = application._clm_vet_review
application._clm_vet_review = lambda *a: 1 / 0
try:
    bedrock_client.set_transport(reply_with({"paths": []}))
    r = c.get("/ld-management/cross-training/model/explain?period=month")
    check("a bug in the vetting is caught, logged, and answered quietly",
          r.status_code == 200 and r.get_json()["status"] == "error")
finally:
    application._clm_vet_review = real

print("\n8. Who may ask")
r = client("am_test", "area_manager").get("/ld-management/cross-training/model/explain")
check("an area manager cannot call the model", r.status_code in (302, 403))
r = application.application.test_client().get("/ld-management/cross-training/model/explain")
check("nor can someone not logged in", r.status_code in (302, 401))

print("\n9. Configuration")
bedrock_client.set_transport(None)
os.environ.pop("LDIS_BEDROCK_MODEL_ID", None)
check("unset model id means off", not bedrock_client.enabled())
os.environ["LDIS_BEDROCK_MODEL_ID"] = "  "
check("a blank one too", not bedrock_client.enabled())
os.environ["LDIS_BEDROCK_MODEL_ID"] = "eu.anthropic.example"
check("a model id turns it on", bedrock_client.enabled())
os.environ.pop("LDIS_BEDROCK_MODEL_ID", None)

print("\n" + "=" * 62)
if fails:
    print(f"{len(fails)} CROSS LEARNING LLM CHECK(S) FAILED")
    for name in fails:
        print("  -", name)
    sys.exit(1)
print("ALL CROSS LEARNING LLM CHECKS PASSED")
