"""MongoDB async connection og collection-helpers (Motor driver)."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

load_dotenv()  # læser .env hvis filen findes

MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "iot_solutions")

_client: AsyncIOMotorClient | None = None


def client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client


def database() -> AsyncIOMotorDatabase:
    return client()[MONGO_DB]


def parks():
    return database()["parks"]


def devices():
    return database()["devices"]


def metrics():
    return database()["metrics"]


def alerts():
    """Domain event log: 'Anomaly Detected' events."""
    return database()["alerts"]


METRIC_RETENTION_DAYS = int(os.getenv("METRIC_RETENTION_DAYS", "30"))


async def init_indexes() -> None:
    await devices().create_index("park_id")
    await metrics().create_index([("device_id", 1), ("timestamp", -1)])
    await metrics().create_index([("park_id", 1), ("timestamp", -1)])
    await alerts().create_index([("device_id", 1), ("timestamp", -1)])
    await alerts().create_index([("park_id", 1), ("timestamp", -1)])
    # TTL-indekser: ryd op automatisk efter METRIC_RETENTION_DAYS
    ttl_seconds = METRIC_RETENTION_DAYS * 24 * 60 * 60
    await metrics().create_index("timestamp", expireAfterSeconds=ttl_seconds)
    await alerts().create_index("timestamp", expireAfterSeconds=ttl_seconds)


async def ping() -> bool:
    try:
        await client().admin.command("ping")
        return True
    except Exception:
        return False


def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
