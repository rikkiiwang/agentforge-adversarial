from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    database_url: str
    openai_api_key: str
    target_url: str
    target_version: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            target_url=os.environ.get(
                "TARGET_URL", "https://copilot-production-b532.up.railway.app"
            ),
            target_version=os.environ.get("TARGET_VERSION", "manual-mvp"),
        )
