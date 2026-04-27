import re
import aiohttp
import logging

logger = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def send(app_token: str, user_keys: list, title: str, message: str):
    if not app_token or not user_keys:
        return
    clean = _strip_html(message)
    async with aiohttp.ClientSession() as session:
        for key in user_keys:
            try:
                async with session.post(PUSHOVER_URL, data={
                    "token": app_token,
                    "user": key,
                    "title": title,
                    "message": clean,
                    "sound": "pushover",
                    "priority": 1,
                }) as resp:
                    if resp.status != 200:
                        logger.warning(f"Pushover {resp.status} for user {key[:6]}...")
            except Exception as e:
                logger.error(f"Pushover error: {e}")
