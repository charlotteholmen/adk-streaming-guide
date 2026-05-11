"""Cloud Text-to-Speech synthesis with per-(text, voice) PCM cache.

Returns raw 16kHz 16-bit PCM mono — the format the Live API expects on input.
"""

import logging
import struct
from dataclasses import dataclass

from config import TtsVoiceConfig
from google.cloud import texttospeech

logger = logging.getLogger(__name__)

AUDIO_SAMPLE_RATE = 16000  # Hz, Live API input format


@dataclass(frozen=True)
class _CacheKey:
    text: str
    language_code: str
    ssml_gender: str


_pcm_cache: dict[_CacheKey, bytes] = {}


def synthesize_query(text: str, voice: TtsVoiceConfig) -> bytes:
    """Return raw 16kHz 16-bit PCM mono for `text`. Cached by (text, voice)."""
    key = _CacheKey(text, voice.language_code, voice.ssml_gender)
    if key in _pcm_cache:
        return _pcm_cache[key]

    client = texttospeech.TextToSpeechClient()
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(
            language_code=voice.language_code,
            ssml_gender=texttospeech.SsmlVoiceGender[voice.ssml_gender],
        ),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=AUDIO_SAMPLE_RATE,
        ),
    )
    pcm = _strip_wav_header(response.audio_content)
    _pcm_cache[key] = pcm
    logger.info(
        "TTS synthesized: %d PCM bytes for text=%r voice=%s/%s",
        len(pcm),
        text,
        voice.language_code,
        voice.ssml_gender,
    )
    return pcm


def _strip_wav_header(wav_bytes: bytes) -> bytes:
    """Strip RIFF/WAVE container, return raw PCM samples.

    TTS LINEAR16 returns a WAV file; Live API expects raw PCM. Walks chunks
    instead of assuming a 44-byte header — TTS may emit a LIST/INFO chunk
    before "data".
    """
    if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
        raise ValueError("TTS response is not a RIFF/WAVE container")
    pos = 12
    while pos + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[pos : pos + 4]
        chunk_size = struct.unpack("<I", wav_bytes[pos + 4 : pos + 8])[0]
        if chunk_id == b"data":
            return wav_bytes[pos + 8 : pos + 8 + chunk_size]
        pos += 8 + chunk_size
    raise ValueError("No 'data' chunk found in WAV")
