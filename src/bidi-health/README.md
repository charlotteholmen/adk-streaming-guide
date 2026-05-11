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

Probes return 200 with transcript JSON on success, 503 on any failure.

## Configuration

Configure with `apps.yaml`. Path defaults to `./apps.yaml` (override with
`APPS_CONFIG=/path/to/apps.yaml`). See `apps.yaml.example` for the full schema.

Per-app entry:

```yaml
- name: bidi-demo-prod
  ws_url: wss://bidi-demo-xxx.us-east1.run.app
  query: "What time is it in Tokyo?"
```

The `name` becomes the URL slug (`/check/bidi-demo-prod/live`). Optional
overrides: `audio_query`, `text_timeout_seconds`, `audio_timeout_seconds`.

All target apps must follow the ADK bidi-demo protocol (WebSocket path
`/ws/{user_id}/{session_id}`, JSON text frames, raw PCM binary frames, ADK
Event JSON responses).

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

One uptime check per `(app, probe-type)`:

```bash
gcloud monitoring uptime create "bidi-demo-prod /check/.../live" \
  --resource-type=uptime-url \
  --resource-labels=host=bidi-health-xxx.us-east1.run.app,project_id=PROJECT \
  --protocol=https \
  --path=/check/bidi-demo-prod/live \
  --port=443 \
  --request-method=get \
  --matcher-content=Tokyo \
  --period=5 \
  --timeout=30 \
  --regions=europe,asia-pacific,usa-iowa \
  --validate-ssl=true \
  --project=PROJECT
```

Repeat for `/check/bidi-demo-prod/live/audio` if you want audio coverage.
The matcher word (e.g. `Tokyo`) lives in the uptime check, not in
`apps.yaml` — it's a Cloud Monitoring concern.

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
