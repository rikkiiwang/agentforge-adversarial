from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    database_url: str
    openai_api_key: str
    anthropic_api_key: str
    target_url: str
    target_version: str
    copilot_patient_id: str
    copilot_physician_user_id: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            target_url=os.environ.get(
                "TARGET_URL", "https://copilot-production-b532.up.railway.app"
            ),
            target_version=os.environ.get("TARGET_VERSION", "manual-mvp"),
            copilot_patient_id=os.environ.get("COPILOT_PATIENT_ID", "").strip(),
            copilot_physician_user_id=os.environ.get(
                "COPILOT_PHYSICIAN_USER_ID", "admin"
            ).strip(),
        )
