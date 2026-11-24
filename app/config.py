"""Runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str
    app_title: str = "Street-light retrofit savings accounting"
    app_version: str = "1.0.0"


def load_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("LIGHTS_DB_PATH", "./data/lights.db"),
    )
