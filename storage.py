import aiosqlite
import json
from config import DB_PATH


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS watched_wallets (
                address     TEXT PRIMARY KEY,
                label       TEXT DEFAULT '',
                chat_id     INTEGER NOT NULL,
                thread_id   INTEGER DEFAULT NULL,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            await db.execute("ALTER TABLE watched_wallets ADD COLUMN thread_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE watched_wallets ADD COLUMN pushover_enabled INTEGER DEFAULT 0")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS position_snapshots (
                address     TEXT NOT NULL,
                coin        TEXT NOT NULL,
                snapshot    TEXT NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (address, coin)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pushover_users (
                user_id     INTEGER PRIMARY KEY,
                user_key    TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_snapshots (
                address     TEXT NOT NULL,
                oid         INTEGER NOT NULL,
                snapshot    TEXT NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (address, oid)
            )
        """)
        await db.commit()


async def add_wallet(address: str, label: str, chat_id: int, thread_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO watched_wallets (address, label, chat_id, thread_id) VALUES (?, ?, ?, ?)",
            (address.lower(), label, chat_id, thread_id)
        )
        await db.commit()


async def remove_wallet(address: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM watched_wallets WHERE address = ?", (address.lower(),))
        await db.execute("DELETE FROM position_snapshots WHERE address = ?", (address.lower(),))
        await db.execute("DELETE FROM order_snapshots WHERE address = ?", (address.lower(),))
        await db.commit()


async def get_wallet(address: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT address, label, chat_id, thread_id FROM watched_wallets WHERE address = ?",
            (address.lower(),)
        ) as cursor:
            return await cursor.fetchone()


async def get_all_wallets() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT address, label, chat_id, thread_id FROM watched_wallets"
        ) as cursor:
            return await cursor.fetchall()


async def save_snapshot(address: str, coin: str, snapshot: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO position_snapshots (address, coin, snapshot, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
            (address.lower(), coin, json.dumps(snapshot))
        )
        await db.commit()


async def delete_snapshot(address: str, coin: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM position_snapshots WHERE address = ? AND coin = ?",
            (address.lower(), coin)
        )
        await db.commit()


async def get_snapshots(address: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT coin, snapshot FROM position_snapshots WHERE address = ?",
            (address.lower(),)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}


async def set_pushover_user(user_id: int, user_key: str):
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"set_pushover_user: user_id={user_id} db={DB_PATH}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO pushover_users (user_id, user_key) VALUES (?, ?)",
            (user_id, user_key)
        )
        await db.commit()
    logger.info(f"set_pushover_user: committed ok")


async def delete_pushover_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pushover_users WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_all_pushover_keys() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_key FROM pushover_users") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def get_wallet_pushover(address: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT pushover_enabled FROM watched_wallets WHERE address = ?",
            (address.lower(),)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False


async def set_wallet_pushover(address: str, enabled: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE watched_wallets SET pushover_enabled = ? WHERE address = ?",
            (1 if enabled else 0, address.lower())
        )
        await db.commit()


async def get_order_snapshots(address: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT oid, snapshot FROM order_snapshots WHERE address = ?",
            (address.lower(),)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0]: json.loads(row[1]) for row in rows}


async def save_order_snapshot(address: str, oid: int, snapshot: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO order_snapshots (address, oid, snapshot, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
            (address.lower(), oid, json.dumps(snapshot))
        )
        await db.commit()


async def delete_order_snapshot(address: str, oid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM order_snapshots WHERE address = ? AND oid = ?",
            (address.lower(), oid)
        )
        await db.commit()
