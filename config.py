"""Configuration loader for Chinese Tech Wire."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

# Project root. When packaged with PyInstaller, __file__ resolves inside the
# temporary extraction directory (sys._MEIPASS), not the real project
# checkout next to the .exe — using that would silently read a missing/
# bundled config/settings.yaml and .env instead of the operator's real ones.
# sys.executable is the actual .exe location and is what must be used here.
#
# The exe may sit directly in the project root, or in dist/ (the build
# output location, one level below root) — search upward a few levels for
# config/settings.yaml rather than assuming the exe's immediate directory,
# mirroring web.launcher.project_root() (kept independent here rather than
# importing it, since config.py is the most foundational module and must
# not gain a dependency on the web/ package).
def _find_root_upward(start: Path, max_levels: int = 4) -> Path:
    candidate = start
    for _ in range(max_levels + 1):
        if (candidate / "config" / "settings.yaml").exists():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return start


if getattr(sys, "frozen", False):
    ROOT = _find_root_upward(Path(sys.executable).resolve().parent)
else:
    ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("CTW_CONFIG_DIR", ROOT / "config")).expanduser().resolve()
DATA_DIR = Path(os.environ.get("CTW_DATA_DIR", ROOT / "data")).expanduser().resolve()


class Settings(BaseSettings):
    model_config = {"env_file": None, "extra": "ignore"}

    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")
    translation_provider: str = Field(default="none", alias="TRANSLATION_PROVIDER")
    translation_api_key: str = Field(default="", alias="TRANSLATION_API_KEY")
    translation_base_url: str = Field(
        default="https://api.openai.com/v1", alias="TRANSLATION_BASE_URL"
    )
    translation_model: str = Field(default="gpt-4o-mini", alias="TRANSLATION_MODEL")
    # Gemini
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.0-flash", alias="GEMINI_MODEL")
    # OpenRouter (OpenAI-compatible endpoint). The key comes from the
    # environment only — never committed, never logged.
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_model: str = Field(
        default="google/gemini-2.5-flash", alias="OPENROUTER_MODEL"
    )
    database_url: str = Field(default="sqlite:///data/ctw.db", alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Cloud migration / runtime bridge. Chinese Tech Wire is a Tier B
    # (staging/soak only) clank — this must never default to a
    # production-sounding value. "soaking" is the least-trusted channel;
    # an operator has to explicitly opt in to anything else via env var.
    release_channel: str = Field(default="soaking", alias="CTW_RELEASE_CHANNEL")


def load_yaml_config() -> Dict[str, Any]:
    path = CONFIG_DIR / "settings.yaml"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_settings() -> Settings:
    env_file = Path(os.environ.get("CTW_ENV_FILE", ROOT / ".env")).expanduser().resolve()
    load_dotenv(env_file)
    return Settings(_env_file=env_file)


# Global convenience
settings = get_settings()
yaml_config = load_yaml_config()
