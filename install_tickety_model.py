#!/usr/bin/env python3
"""
Run this once you have the real service-2.json from Option 1, 2, or 3.

Usage:
    python3 install_tickety_model.py /path/to/service-2.json

It reads the file's own "apiVersion" field and copies it into the exact
vendor/tickety_service_model/tickety/<api-version>/service-2.json path
tickety_client.py expects — so you don't have to figure out or type
that version string by hand.

If you used Option 3 (printed the JSON to stdout instead of finding a
file on disk), paste that output into a file first, e.g.:
    python3 -c "..." > /tmp/service-2.json
    python3 install_tickety_model.py /tmp/service-2.json
"""
import json
import os
import shutil
import sys

def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    src_path = sys.argv[1]
    if not os.path.isfile(src_path):
        print(f"Not a file: {src_path}")
        sys.exit(1)

    with open(src_path) as f:
        try:
            model = json.load(f)
        except json.JSONDecodeError as e:
            print(f"That file isn't valid JSON ({e}) — did Option 3's print get cut off, "
                  f"or wrapped in extra text? It needs to be just the JSON object.")
            sys.exit(1)

    api_version = model.get("metadata", {}).get("apiVersion")
    endpoint_prefix = model.get("metadata", {}).get("endpointPrefix", "")
    if not api_version:
        print("Couldn't find metadata.apiVersion in that file — this might not be "
              "the right service-2.json, or its structure is different than expected. "
              "Open it and check for a top-level \"metadata\": {\"apiVersion\": \"...\"} field.")
        sys.exit(1)

    if endpoint_prefix and endpoint_prefix != "tickety":
        print(f"Warning: this file's endpointPrefix is '{endpoint_prefix}', not 'tickety'. "
              f"Double-check this is actually the Tickety service model before continuing.")

    here = os.path.dirname(os.path.abspath(__file__))
    dest_dir = os.path.join(here, "vendor", "tickety_service_model", "tickety", api_version)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, "service-2.json")

    shutil.copy(src_path, dest_path)
    print(f"Installed at: {dest_path}")
    print(f"API version found in the file: {api_version}")
    print("\nNext: redeploy, then hit /api/tickety-diagnostic to confirm the client now initializes.")

if __name__ == "__main__":
    main()
