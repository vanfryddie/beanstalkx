# Vendoring the Tickety service model

This directory is deliberately empty of any actual API definition. Here's
exactly what needs to go here, and why it isn't already.

## Why this directory exists

botocore has no built-in knowledge of "tickety" as a service — it's an
Amazon-internal API, not a public AWS service. `session.create_client
(service_name="tickety", ...)` only works once botocore can find a file
describing that service's operations, inputs, and outputs. Normally
`TicketyPythonSdk` provides this, but it isn't published to public PyPI,
so it can't be installed via `requirements.txt` on Elastic Beanstalk the
way every other dependency in this app is.

`tickety_client.py` points botocore at this folder via the `AWS_DATA_PATH`
environment variable — the same mechanism botocore itself documents for
loading any customer-vendored service model, not something specific to
this app. Once the real file is here, no code changes are needed anywhere
else — `get_tickety_client()` will just start succeeding.

## What file, exactly

A file named `service-2.json`, in this exact layout:

```
vendor/tickety_service_model/
  tickety/
    <api-version>/
      service-2.json
```

`<api-version>` is whatever Tickety's actual API version string is (a
date, like `2023-01-01`) — check the real SDK for it, don't guess.

## Where to get the real file

- Look inside the actual `TicketyPythonSdk` package once you can access
  it (via CodeArtifact, an internal build, or checking out
  `code.amazon.com/packages/TicketyServicePythonExamples` referenced in
  Tickety's own setup guide) — botocore-style SDKs keep this file at
  `<package>/data/tickety/<version>/service-2.json` internally.
- If the package structure is different from a standard botocore data
  layout, search the package contents for a file literally named
  `service-2.json` — that's the one to copy here.

## What NOT to do

Don't write this file by hand or ask an AI to generate one — it has to
exactly match Tickety's real API shapes (operation names, required
fields, error types). A hand-written or generated stand-in could let
`get_tickety_client()` return a client that *looks* like it initialized
successfully while being completely wrong about what `create_ticket`
actually expects, which would fail in confusing ways against the real
API — much harder to debug than the current, honest "Tickety client
unavailable" error.
