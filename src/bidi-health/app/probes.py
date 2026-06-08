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
                        transcript_parts[:] = [ot["text"]]

                    if event.get("turnComplete") or (ot and ot.get("finished")):
                        break

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
            break
        except asyncio.TimeoutError:
            return ProbeResult(ok=False, error="Model response timed out")
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

    Native audio models with grounding tools (e.g. Google Search) deliver
    outputTranscription events AFTER turnComplete — the model streams audio
    first, then the transcription service catches up. The loop therefore waits
    for ``outputTranscription.finished == True`` with non-empty text as the
    exit condition, not ``turnComplete``.
    """
    timeout = app.effective_audio_timeout(defaults)
    input_text: list[str] = []
    output_text: list[str] = []

    silence = b"\x00" * (
        AUDIO_SAMPLE_RATE * AUDIO_BYTES_PER_SAMPLE * AUDIO_TRAILING_SILENCE_MS // 1000
    )
    payload = pcm + silence

    for attempt in range(2):
        input_text.clear()
        output_text.clear()
        ws_url = _ws_url_for(app, "health-audio")

        async def _check():
            async with websockets.connect(ws_url) as ws:
                if app.setup_message:
                    await ws.send(app.setup_message)
                for offset in range(0, len(payload), AUDIO_CHUNK_BYTES):
                    await ws.send(payload[offset : offset + AUDIO_CHUNK_BYTES])
                    await asyncio.sleep(AUDIO_CHUNK_MS / 1000)

                async for message in ws:
                    event = json.loads(message)

                    it = event.get("inputTranscription")
                    if it and it.get("text"):
                        input_text[:] = [it["text"]]

                    ot = event.get("outputTranscription")
                    if ot and ot.get("text"):
                        output_text[:] = [ot["text"]]

                    # Best: finished transcription with text.
                    if ot and ot.get("finished") and output_text:
                        break
                    # Fallback: turnComplete after output was already
                    # collected (apps that never send finished=true).
                    if event.get("turnComplete") and output_text:
                        break

                    # turnComplete with no output yet → grounding-tool
                    # apps deliver transcription late. Keep draining.
                    if event.get("turnComplete") and not output_text:
                        async def _drain():
                            async for msg in ws:
                                ev = json.loads(msg)
                                o = ev.get("outputTranscription")
                                if o and o.get("text"):
                                    output_text[:] = [o["text"]]
                                if o and o.get("finished") and output_text:
                                    break
                        try:
                            await asyncio.wait_for(_drain(), timeout=15)
                        except asyncio.TimeoutError:
                            pass
                        break

        try:
            await asyncio.wait_for(_check(), timeout=timeout)
            break
        except asyncio.TimeoutError:
            return ProbeResult(
                ok=False,
                error="Audio probe timed out",
                input_transcription=input_text[0] if input_text else None,
                output_transcription=output_text[0] if output_text else None,
            )
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

    input_transcription = input_text[0] if input_text else ""
    output_transcription = output_text[0] if output_text else ""

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
