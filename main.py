import asyncio
import logging

import storage
import bot as bot_module
from bot import bot, dp, margin_poll_loop, order_poll_loop, on_ws_event
from ws_manager import HLWebSocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    await storage.init_db()
    logger.info("DB initialized")

    # Ініціалізуємо WebSocket клієнт
    ws = HLWebSocket(on_event=on_ws_event)
    bot_module.ws_client = ws
    await ws.start()

    # Підписуємось на всі збережені гаманці
    wallets = await storage.get_all_wallets()
    for address, label, _, __ in wallets:
        await ws.subscribe(address)
        logger.info(f"Subscribed to {address[:8]}... ({label or 'no label'})")

    asyncio.create_task(margin_poll_loop())
    asyncio.create_task(order_poll_loop())

    logger.info("Starting Telegram bot...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
