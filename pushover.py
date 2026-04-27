import re
import aiohttp
import logging
from config import PUSHOVER_APP_TOKEN, PUSHOVER_USER_KEY

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def send(title: str, message: str):
    if not PUSHOVER_APP_TOKEN or not PUSHOVER_USER_KEY:
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(PUSHOVER_URL, data={
                "token": PUSHOVER_APP_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title,
                "message": _strip_html(message),
            }) as resp:
                if resp.status != 200:
                    logger.warning(f"Pushover response {resp.status}")
    except Exception as e:
        logger.error(f"Pushover error: {e}")
