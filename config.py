import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "12"))
MIN_POSITION_VALUE = float(os.getenv("MIN_POSITION_VALUE", "100"))
# На Railway volume монтується в /data — DB_PATH встановлюємо туди
# Локально за замовчуванням tracker.db в поточній папці
_data_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
DB_PATH = os.path.join(_data_dir, "tracker.db") if _data_dir else os.getenv("DB_PATH", "tracker.db")

HL_API_URL = "https://api.hyperliquid.xyz/info"

SIZE_CHANGE_THRESHOLD = 0.02  # 2%

def _parse_allowed_chats() -> set:
    """
    Парсить ALLOWED_CHATS з .env
    Формат: chat_id або chat_id:thread_id через кому
    Приклад: 123456789,-1001234567890:5
    """
    raw = os.getenv("ALLOWED_CHATS", "")
    result = set()
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            chat_id_str, thread_id_str = entry.split(":", 1)
            result.add((int(chat_id_str), int(thread_id_str)))
        else:
            result.add((int(entry), None))
    return result

ALLOWED_CHATS: set = _parse_allowed_chats()
