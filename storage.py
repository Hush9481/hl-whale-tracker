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
        # Міграція: додаємо thread_id якщо таблиця вже існує без нього
        try:
            await db.execute("ALTER TABLE watched_wallets ADD COLUMN thread_id INTEGER DEFAULT NULL")
        except Exception:
            pass  # колонка вже є
        await db.execute("""
            CREATE TABLE IF NOT EXISTS position_snapshots (
                address     TEXT NOT NULL,
                coin        TEXT NOT NULL,
                snapshot    TEXT NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (address, coin)
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
