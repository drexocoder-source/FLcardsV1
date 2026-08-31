from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from database.mongo import MongoDatabase

logger = logging.getLogger("fl_cards.health")


async def _response(writer: asyncio.StreamWriter, status: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("utf-8")
    writer.write(headers + body)
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def health_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    database: MongoDatabase,
) -> None:
    try:
        request_line = (await reader.readline()).decode("latin-1").strip()
        path = request_line.split(" ")[1] if " " in request_line else "/"
        while True:
            header = await reader.readline()
            if header in (b"", b"\r\n"):
                break

        if path == "/health":
            healthy = await database.is_healthy()
            await _response(
                writer,
                "200 OK" if healthy else "503 Service Unavailable",
                {
                    "service": "Fʟ | Cᴀʀᴅs",
                    "status": "ok" if healthy else "degraded",
                    "database": "connected" if healthy else "unavailable",
                    "time": datetime.now(UTC).isoformat(),
                },
            )
        else:
            await _response(
                writer,
                "200 OK",
                {"service": "Fʟ | Cᴀʀᴅs", "message": "Telegram bot is running"},
            )
    except (ConnectionError, asyncio.IncompleteReadError):
        logger.debug("Health client disconnected before response")
    finally:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()


async def start_health_server(database: MongoDatabase, port: int) -> asyncio.AbstractServer:
    server = await asyncio.start_server(
        lambda reader, writer: health_handler(reader, writer, database),
        host="0.0.0.0",
        port=port,
    )
    logger.info("Health app listening on port %s", port)
    return server


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    database = MongoDatabase(
        os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        os.getenv("MONGO_DB_NAME", "fl_cards"),
    )
    await database.connect()
    server = await start_health_server(database, int(os.getenv("PORT", "8080")))
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        await server.wait_closed()
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
