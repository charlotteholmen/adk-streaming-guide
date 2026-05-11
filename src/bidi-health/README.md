# bidi-health

Generic uptime probe for ADK-based bidi WebSocket apps. One service monitors
many apps via a YAML config; each app gets a text probe and an audio probe.
Designed to be wired up to Cloud Monitoring uptime checks.

## What it probes

For each configured app, two end-to-end checks:

| Path | What it exercises |
|---|---|
| `GET /check/{app}/live` | WebSocket connect → text frame → ADK → Live API → model → response (works against both half-cascade and native-audio models) |
| `GET /check/{app}/live/audio` | Above + Cloud Text-to-Speech → binary PCM upload → automatic VAD → input transcription → output transcription |

Plus:

- `GET /health` — service liveness (no upstream check)
- `GET /apps` — list configured app names

Probes return 200 with transcript JSON on success, 503 on any failure. The
text route returns 200 `{"status":"skipped"}` when an app has
`text_probe_enabled: false` (audio-only apps).

## Configuration

Configure with `apps.yaml`. Path defaults to `./apps.yaml` (override with
`APPS_CONFIG=/path/to/apps.yaml`). See `apps.yaml.example` for the full schema.

Minimal per-app entry (e.g. bidi-demo):

```yaml
- name: bidi-demo-prod
  ws_url: wss://bidi-demo-xxx.us-east1.run.app
  query: "What time is it in Tokyo?"
```

The `name` becomes the URL slug (`/check/bidi-demo-prod/live`). The query
phrase is sent as text and synthesized to PCM for the audio probe.

Optional fields:

| Field | Purpose |
|---|---|
| `audio_query` | Different phrase for audio probe (default: same as `query`) |
| `text_timeout_seconds` / `audio_timeout_seconds` | Per-app timeout override |
| `ws_query_params` | Mapping appended to the WebSocket URL as `?k=v&...` (e.g. `{source: en, target: ja}` for translator language selection) |
| `setup_message` | JSON text frame sent **before** any other payload, for apps that require a per-session handshake (e.g. `'{"glossary":[]}'` for the translator) |
| `text_probe_enabled` | Set `false` for audio-only apps where text input is silently dropped server-side; the text route then short-circuits with `{"status":"skipped"}` |

All target apps must follow the ADK bidi-demo protocol shape (WebSocket path
`/ws/{user_id}/{session_id}`, JSON text frames, raw PCM binary frames, ADK
Event JSON responses); the optional fields above accommodate apps that vary
slightly within that shape.

Audio-only example:

```yaml
- name: adk-live-translator-prod
  ws_url: wss://live-translation-xxx.us-central1.run.app
  ws_query_params:
    source: en
    target: ja
  setup_message: '{"glossary":[]}'
  text_probe_enabled: false
  query: "What time is it in Tokyo?"
```

## Local Development

```bash
cd src/bidi-health
uv sync
cp apps.yaml.example apps.yaml
# Edit apps.yaml — point ws_url at a running bidi app

cd app
uv run --project .. uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

In another terminal:

```bash
curl http://localhost:8001/health
curl http://localhost:8001/apps
curl http://localhost:8001/check/bidi-demo-prod/live
curl http://localhost:8001/check/bidi-demo-prod/live/audio
```

## Deploy to Cloud Run

```bash
cd src/bidi-health
cp apps.yaml.example apps.yaml
# Edit apps.yaml with your real WebSocket URLs

gcloud run deploy bidi-health \
  --source . \
  --project "${GOOGLE_CLOUD_PROJECT}" \
  --region "${GOOGLE_CLOUD_LOCATION}" \
  --allow-unauthenticated \
  --timeout 60 \
  --min-instances 0 \
  --max-instances 1
```

The `apps.yaml` is baked into the container image at build time (see
`Dockerfile`). Re-deploy whenever the config changes. For larger fleets,
consider mounting via Secret Manager instead.

## Cloud Monitoring uptime checks

One uptime check per `(app, probe-type)` you want monitored. The matcher
word (e.g. `Tokyo`, `東京`) lives in the uptime check, not in `apps.yaml`
— it's a Cloud Monitoring concern. Non-ASCII matchers (Japanese, etc.) are
supported by gcloud and round-trip cleanly through the API.

Audio probe (covers both audio I/O *and* the text path implicitly):

```bash
gcloud monitoring uptime create "bidi-demo-prod /check/.../live/audio" \
  --resource-type=uptime-url \
  --resource-labels=host=bidi-health-xxx.us-east1.run.app,project_id=PROJECT \
  --protocol=https \
  --path=/check/bidi-demo-prod/live/audio \
  --port=443 \
  --request-method=get \
  --matcher-content=Tokyo \
  --period=5 \
  --timeout=60 \
  --regions=europe,asia-pacific,usa-iowa \
  --validate-ssl=true \
  --project=PROJECT
```

For the matching alert policy (uses Monitoring REST API since gcloud doesn't
have first-class support for uptime-based alert policies):

```bash
TOKEN=$(gcloud auth print-access-token)
CHECK_ID=...      # from gcloud monitoring uptime list-configs
CHANNEL=projects/PROJECT/notificationChannels/...

cat > policy.json <<EOF
{
  "displayName": "bidi-demo-prod /check/.../live/audio uptime failure",
  "combiner": "OR",
  "conditions": [{
    "conditionThreshold": {
      "filter": "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND metric.label.check_id=\"${CHECK_ID}\" AND resource.type=\"uptime_url\"",
      "comparison": "COMPARISON_GT",
      "thresholdValue": 1,
      "duration": "300s",
      "trigger": {"count": 1},
      "aggregations": [{
        "alignmentPeriod": "1200s",
        "perSeriesAligner": "ALIGN_NEXT_OLDER",
        "crossSeriesReducer": "REDUCE_COUNT_FALSE",
        "groupByFields": ["resource.label.*"]
      }]
    },
    "displayName": "Failure of uptime check_id ${CHECK_ID}"
  }],
  "notificationChannels": ["${CHANNEL}"],
  "enabled": true
}
EOF

curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @policy.json \
  "https://monitoring.googleapis.com/v3/projects/PROJECT/alertPolicies"
```

**Order of operations matters when removing**: an uptime check cannot be
deleted while an alert policy references it. Delete the policy first, then
the check. (gcloud surfaces this only as a generic `INVALID_ARGUMENT`.)

For audio-only apps where you've set `text_probe_enabled: false`, only
create the audio uptime check — the text route would always return
`{"status":"skipped"}`, which would either always pass or always fail any
matcher you set, depending on your matcher word.

## Architecture

```
src/bidi-health/
├── pyproject.toml
├── uv.lock
├── Dockerfile
├── README.md
├── apps.yaml.example
└── app/
    ├── main.py     # FastAPI app, routes, lifespan TTS preload
    ├── config.py   # Pydantic models, YAML loader
    ├── probes.py   # text_probe(), audio_probe()
    └── tts.py      # Cloud TTS synthesis with PCM cache
```

TTS is synthesized once per unique `(query, voice)` tuple at startup and
cached in-process. Multiple apps sharing the same probe phrase share the
synthesized PCM payload.
