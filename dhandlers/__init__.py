from __future__ import annotations

from pyrogram import Client

from config import Settings
from database.mongo import MongoDatabase

from .admin import register_admin_handlers
from .audit import register_audit_handlers
from .collection import register_collection_handlers
from .modes import register_mode_handlers
from .start import register_start_handlers


def register_handlers(bot: Client, database: MongoDatabase, settings: Settings) -> None:
    register_start_handlers(bot, database, settings)
    register_collection_handlers(bot, database, settings)
    register_mode_handlers(bot, database, settings)
    register_admin_handlers(bot, database, settings)
    register_audit_handlers(bot, database, settings)
