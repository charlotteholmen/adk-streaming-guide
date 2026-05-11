"""Pydantic models + YAML loader for the bidi-health apps config."""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_NAME_PATTERN = re.compile(r"^[a-z0-9-]+$")


class TtsVoiceConfig(BaseModel):
    language_code: str = "en-US"
    ssml_gender: Literal["NEUTRAL", "MALE", "FEMALE"] = "NEUTRAL"


class Defaults(BaseModel):
    text_timeout_seconds: int = 20
    audio_timeout_seconds: int = 30
    tts_voice: TtsVoiceConfig = Field(default_factory=TtsVoiceConfig)


class AppConfig(BaseModel):
    name: str
    ws_url: str
    query: str
    audio_query: str | None = None
    text_timeout_seconds: int | None = None
    audio_timeout_seconds: int | None = None

    @field_validator("name")
    @classmethod
    def _name_url_safe(cls, v: str) -> str:
        if not _NAME_PATTERN.fullmatch(v):
            raise ValueError(
                f"name must match {_NAME_PATTERN.pattern!r}, got {v!r}"
            )
        return v

    @field_validator("ws_url")
    @classmethod
    def _ws_url_scheme(cls, v: str) -> str:
        if not (v.startswith("ws://") or v.startswith("wss://")):
            raise ValueError(
                f"ws_url must start with ws:// or wss://, got {v!r}"
            )
        return v.rstrip("/")

    def effective_text_timeout(self, defaults: Defaults) -> int:
        return self.text_timeout_seconds or defaults.text_timeout_seconds

    def effective_audio_timeout(self, defaults: Defaults) -> int:
        return self.audio_timeout_seconds or defaults.audio_timeout_seconds

    def effective_audio_query(self) -> str:
        return self.audio_query or self.query


class AppsConfig(BaseModel):
    defaults: Defaults = Field(default_factory=Defaults)
    apps: list[AppConfig]

    @model_validator(mode="after")
    def _names_unique(self) -> "AppsConfig":
        names = [a.name for a in self.apps]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(f"duplicate app names: {sorted(dupes)}")
        return self

    def get(self, name: str) -> AppConfig | None:
        return next((a for a in self.apps if a.name == name), None)


def load_apps_config(path: str | Path) -> AppsConfig:
    """Parse and validate apps.yaml at `path`."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return AppsConfig.model_validate(raw)
