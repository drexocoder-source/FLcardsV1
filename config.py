from __future__ import annotations

import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _mongo_uri() -> str:
    value = _required("MONGO_URI")
    # Copying a connection string from a project page can append the page URL
    # in parentheses. It is not part of the MongoDB URI.
    if " (http" in value:
        value = value.split(" (http", 1)[0].strip()
    if not value.startswith(("mongodb://", "mongodb+srv://")):
        raise RuntimeError("MONGO_URI must start with mongodb:// or mongodb+srv://")
    return value


def _owner_ids() -> frozenset[int]:
    raw = os.getenv("OWNER_IDS", "8186068163").strip()
    if not raw:
        return frozenset()
    try:
        return frozenset(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise RuntimeError("OWNER_IDS must be a comma-separated list of Telegram user IDs") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    mongo_uri: str
    mongo_db_name: str
    owner_ids: frozenset[int]
    log_group_id: int
    openrouter_api_key: str
    openrouter_model: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            api_id = int(_required("API_ID"))
            log_group_id = int(os.getenv("LOG_GROUP_ID", "-1003692127639"))
            port = int(os.getenv("PORT", "8080"))
        except ValueError as exc:
            raise RuntimeError("API_ID, LOG_GROUP_ID, and PORT must be valid integers") from exc

        return cls(
            bot_token=_required("BOT_TOKEN"),
            api_id=api_id,
            api_hash=_required("API_HASH"),
            mongo_uri=_mongo_uri(),
            mongo_db_name=os.getenv("MONGO_DB_NAME", "fl_cards").strip() or "fl_cards",
            owner_ids=_owner_ids(),
            log_group_id=log_group_id,
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip(),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip(),
            port=port,
        )
