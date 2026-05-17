"""Pure async probe functions: text and audio.

Each opens a WebSocket to the target ADK bidi app, exchanges one turn, and
returns a `ProbeResult`. Routes in main.py wrap these into HTTP responses.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import websockets
from config import AppConfig, Defaults

logger = logging.getLogger(__name__)

# Audio streaming constants — Live API input format
AUDIO_SAMPLE_RATE = 16000
AUDIO_BYTES_PER_SAMPLE = 2  # 16-bit
AUDIO_CHUNK_MS = 100
AUDIO_CHUNK_BYTES = AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_SAMPLE * AUDIO_CHUNK_MS // 1000
AUDIO_TRAILING_SILENCE_MS = 1500  # let automatic VAD detect end-of-speech


@dataclass
class ProbeResult:
    ok: bool
    transcript: str | None = None
    input_transcription: str | None = None
    output_transcription: str | None = None
    error: str | None = None


# Max time to wait for the upstream `{"ready": true}` warmup signal. Apps that
# warm up by sending a short clip to the model and waiting for the first real
# response typically settle in 3-10s; 20s leaves headroom.
READY_WAIT_SECONDS = 20.0

# Some upstream apps (e.g. adk-live-translator) emit the ready signal as soon
# as the warmup turn produces its first audio part, but only finish wiring the
# Live session to the upstream's audio forwarder a moment later. Audio sent in
# that window is silently dropped. Sleep briefly after ready to let the
# upstream finish wiring up.
READY_SETTLE_SECONDS = 2.0


class _ReadyWaitTimeout(Exception):
    """Raised when the upstream's warmup-ready signal never arrives."""


async def _wait_for_ready(ws) -> None:
    """Read text frames until one decodes to a dict with `ready == True`.

    Non-JSON and binary frames are ignored. Raises `_ReadyWaitTimeout` if no
    such frame arrives within `READY_WAIT_SECONDS`. After detecting ready,
    sleeps `READY_SETTLE_SECONDS` to avoid the race window described above.
    """
    try:
        async with asyncio.timeout(READY_WAIT_SECONDS):
            async for message in ws:
                try:
                    event = json.loads(message)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict) and event.get("ready") is True:
                    await asyncio.sleep(READY_SETTLE_SECONDS)
                    return
    except asyncio.TimeoutError as e:
        raise _ReadyWaitTimeout(
            f"upstream did not emit ready signal within {READY_WAIT_SECONDS}s"
        ) from e


def _ws_url_for(app: AppConfig, prefix: str) -> str:
    user_id = "uptime-check"
    session_id = f"{prefix}-{uuid.uuid4().hex[:12]}"
    url = f"{app.ws_url}/ws/{user_id}/{session_id}"
    if app.ws_query_params:
        url = f"{url}?{urlencode(app.ws_query_params)}"
    return url


async def text_probe(app: AppConfig, defaults: Defaults) -> ProbeResult:
    """Send a text query, drain events until turn_complete, return transcript.

    Collects from both `content.parts[].text` (half-cascade models) and
    `outputTranscription.text` (native-audio models) so the same probe works
    against either modality. Retries once on abrupt WebSocket close — the
    upstream commonly drops the connection without a close frame when it
    can't open a Live API session (e.g. transient RESOURCE_EXHAUSTED).
    """
    timeout = app.effective_text_timeout(defaults)
    transcript_parts: list[str] = []

    for attempt in range(2):
        transcript_parts.clear()
        ws_url = _ws_url_for(app, "health")

        async def _check():
            async with websockets.connect(ws_url) as ws:
                if app.setup_message:
                    await ws.send(app.setup_message)
                if app.wait_for_ready:
                    await _wait_for_ready(ws)
                await ws.send(json.dumps({"type": "text", "text": app.query}))
                async for message in ws:
                    event = json.loads(message)

                    content = event.get("content")
                    if content and content.get("parts"):
                        for part in content["parts"]:
                            if part.get("text"):
                                transcript_parts.append(part["text"])

                    ot = event.get("outputTranscription")
                    if ot and ot.get("text"):
                        transcript_parts.append(ot["text"])

                    # turnComplete is the canonical end-of-turn signal but can
                    # be late or missed. outputTranscription.finished is the
                    # per-sentence boundary and is reliably present on the
                    # final aggregated event.
                    if event.get("turnComplete") or (ot and ot.get("finished")):
                        break

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
            break
        except asyncio.TimeoutError:
            return ProbeResult(ok=False, error="Model response timed out")
        except _ReadyWaitTimeout as e:
            return ProbeResult(ok=False, error=str(e))
        except websockets.exceptions.ConnectionClosed as e:
            if attempt == 0:
                logger.warning(
                    "text_probe %s closed early (%s); retrying once",
                    app.name,
                    e,
                )
                await asyncio.sleep(2)
                continue
            return ProbeResult(ok=False, error=str(e))
        except Exception as e:
            return ProbeResult(ok=False, error=str(e))

    transcript = "".join(transcript_parts)
    if not transcript:
        return ProbeResult(ok=False, error="No transcript received")
    return ProbeResult(ok=True, transcript=transcript)


async def audio_probe(app: AppConfig, defaults: Defaults, pcm: bytes) -> ProbeResult:
    """Stream pre-synthesized PCM as binary frames + trailing silence.

    Validates BOTH `inputTranscription` (Vertex transcribed what we sent) and
    `outputTranscription` (model produced an audio response). Retries once on
    abrupt WebSocket close — see text_probe for rationale.
    """
    timeout = app.effective_audio_timeout(defaults)
    input_parts: list[str] = []
    output_parts: list[str] = []

    silence = b"\x00" * (
        AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_SAMPLE * AUDIO_TRAILING_SILENCE_MS // 1000
    )
    payload = pcm + silence

    for attempt in range(2):
        input_parts.clear()
        output_parts.clear()
        ws_url = _ws_url_for(app, "health-audio")

        async def _check():
            async with websockets.connect(ws_url) as ws:
                if app.setup_message:
                    await ws.send(app.setup_message)
                if app.wait_for_ready:
                    await _wait_for_ready(ws)
                for offset in range(0, len(payload), AUDIO_CHUNK_BYTES):
                    await ws.send(payload[offset : offset + AUDIO_CHUNK_BYTES])
                    # Pace at real-time so VAD sees a normal stream, not a burst
                    await asyncio.sleep(AUDIO_CHUNK_MS / 1000)

                async for message in ws:
                    event = json.loads(message)

                    it = event.get("inputTranscription")
                    if it and it.get("text"):
                        input_parts.append(it["text"])
                    ot = event.get("outputTranscription")
                    if ot and ot.get("text"):
                        output_parts.append(ot["text"])

                    if event.get("turnComplete") or (ot and ot.get("finished")):
                        break

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
            break
        except asyncio.TimeoutError:
            return ProbeResult(
                ok=False,
                error="Audio probe timed out",
                input_transcription="".join(input_parts) or None,
                output_transcription="".join(output_parts) or None,
            )
        except _ReadyWaitTimeout as e:
            return ProbeResult(ok=False, error=str(e))
        except websockets.exceptions.ConnectionClosed as e:
            if attempt == 0:
                logger.warning(
                    "audio_probe %s closed early (%s); retrying once",
                    app.name,
                    e,
                )
                await asyncio.sleep(2)
                continue
            return ProbeResult(ok=False, error=str(e))
        except Exception as e:
            return ProbeResult(ok=False, error=str(e))

    input_transcription = "".join(input_parts)
    output_transcription = "".join(output_parts)

    if not input_transcription:
        return ProbeResult(
            ok=False,
            error="No input transcription (audio not recognized)",
        )
    if not output_transcription:
        return ProbeResult(
            ok=False,
            error="No output transcription (model did not respond)",
            input_transcription=input_transcription,
        )

    return ProbeResult(
        ok=True,
        input_transcription=input_transcription,
        output_transcription=output_transcription,
    )
