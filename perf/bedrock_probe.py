"""Can this server reach a Claude model on Amazon Bedrock?

Run it ON the Elastic Beanstalk instance, after deploying, with the app's
own Python — so what it proves is the instance profile, the region, the
pinned boto3 and bedrock_client.py exactly as the Cross Learning Model
tab will use them, not somebody's laptop credentials:

    sudo -i
    cd /var/app/current && source /var/app/venv/*/bin/activate
    python3 perf/bedrock_probe.py            # list, then call the configured model
    python3 perf/bedrock_probe.py <id>       # call a specific id instead

The environment properties set in the EB console are not in an SSH
shell's environment, so this reads them the way the platform does
(get-config) when they are missing. Exit status 0 means the tab will get
generated explanations; anything else prints which of the three set-up
steps is missing.
"""
import json
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GET_CONFIG = "/opt/elasticbeanstalk/bin/get-config"

FIXES = {
    "iam": "The instance profile may not call this model. Add the inline policy from "
           "LDIS_Bedrock_Setup.md (step 4) to the EB instance role, with this exact "
           "inference profile ARN and the model ARNs it routes to.",
    "model_access": "Model access is not granted for this account. Bedrock console -> Model "
                    "access (or the model's catalog page) -> request access for the Anthropic "
                    "model, and submit Anthropic's use-case form if asked (step 2).",
    "marketplace": "First use of this model subscribes the account through AWS Marketplace, "
                   "and the caller lacks those permissions. Add the optional Marketplace "
                   "statement from LDIS_Bedrock_Setup.md (step 4), run this once, then remove it.",
    "needs_profile": "This model is only served through an inference profile. Use the id from "
                     "Bedrock console -> Cross-region inference (it starts eu., us., apac. or "
                     "global.), not the bare model id (step 3).",
    "timeout": "No answer within LDIS_BEDROCK_TIMEOUT seconds. Try again; if it persists, "
               "check the instance can reach bedrock-runtime (VPC endpoint or NAT).",
    "throttled": "Throttled — the account's Bedrock quota for this model is very low or used up.",
}


def load_eb_environment():
    if os.environ.get("LDIS_BEDROCK_MODEL_ID") or not os.path.exists(GET_CONFIG):
        return
    try:
        props = json.loads(subprocess.check_output([GET_CONFIG, "environment"], text=True))
    except Exception as exc:
        print(f"(could not read EB environment properties: {exc})")
        return
    for key in ("LDIS_BEDROCK_MODEL_ID", "AWS_REGION", "LDIS_BEDROCK_TIMEOUT",
                "LDIS_BEDROCK_ANTHROPIC_VERSION"):
        if props.get(key) and not os.environ.get(key):
            os.environ[key] = props[key]


def main():
    load_eb_environment()
    if len(sys.argv) > 1:
        os.environ["LDIS_BEDROCK_MODEL_ID"] = sys.argv[1]
    logging.basicConfig(level=logging.INFO, format="  log: %(message)s")

    import boto3
    region = os.environ.get("AWS_REGION", "us-east-1")
    print(f"region       {region}{'' if os.environ.get('AWS_REGION') else '  (AWS_REGION unset - the default)'}")
    print(f"boto3        {boto3.__version__}")
    try:
        print(f"caller       {boto3.client('sts', region_name=region).get_caller_identity()['Arn']}")
    except Exception as exc:
        print(f"caller       unknown: {exc}")
        print("-> No usable AWS credentials. On EB the instance profile provides them.")
        return 2

    bedrock = boto3.client("bedrock", region_name=region)
    try:
        models = bedrock.list_foundation_models(byProvider="Anthropic")["modelSummaries"]
        print("\nAnthropic models offered in this region:")
        for m in sorted(models, key=lambda m: m["modelId"]):
            print(f"  {m['modelId']:<55} {','.join(m.get('inferenceTypesSupported') or [])}")
    except Exception as exc:
        print(f"\n(could not list models: {type(exc).__name__} - the policy's List actions are optional)")
    if hasattr(bedrock, "list_inference_profiles"):
        try:
            profiles = bedrock.list_inference_profiles()["inferenceProfileSummaries"]
            print("\nInference profiles for Anthropic models:")
            for p in profiles:
                if "anthropic" in p["inferenceProfileId"]:
                    print(f"  {p['inferenceProfileId']}")
        except Exception as exc:
            print(f"\n(could not list inference profiles: {type(exc).__name__})")
    else:
        print("\n(this boto3 cannot list inference profiles; use the console, or:\n"
              "   aws bedrock list-inference-profiles --region " + region + ")")

    import bedrock_client
    model = bedrock_client.model_id()
    if not model:
        print("\nLDIS_BEDROCK_MODEL_ID is not set - pick an id above and set it (step 5),\n"
              "or pass one: python3 perf/bedrock_probe.py <id>")
        return 1
    print(f"\ncalling      {model}  (InvokeModel, as the app does)")
    answer, status = bedrock_client.ask_json(
        "Reply with JSON only.", {"question": 'Reply with {"reachable": true}.'}, max_tokens=2000)
    print(f"status       {status}")
    if answer is not None:
        print(f"reply        {json.dumps(answer)}")
        print("\nOK - the Cross Learning Model tab will show generated explanations.")
        return 0
    print("\n-> " + FIXES.get(status, "See the log line above for the error AWS returned."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
