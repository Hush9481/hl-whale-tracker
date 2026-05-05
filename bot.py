import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import storage
import hyperliquid
import tracker
import pushover
from config import TELEGRAM_BOT_TOKEN, POLL_INTERVAL, ALLOWED_CHATS, PUSHOVER_APP_TOKEN
from ws_manager import HLWebSocket

logger = logging.getLogger(__name__)

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

_locks: dict[str, asyncio.Lock] = {}
_live_tasks: dict[int, asyncio.Task] = {}
_last_checked: dict[str, float] = {}

LIVE_UPDATE_INTERVAL = 5
LIVE_TIMEOUT = 300
MARGIN_POLL_INTERVAL = 2
ORDER_POLL_INTERVAL = 3


# ─── Перевірка доступу ───────────────────────────────────────────────────────

def is_allowed(message: types.Message) -> bool:
    """Повертає True якщо повідомлення з дозволеного чату/треду."""
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    logger.info(f"MSG from chat_id={chat_id} thread_id={thread_id} | allowed={ALLOWED_CHATS}")

    if not ALLOWED_CHATS:
        return True

    for allowed_chat, allowed_thread in ALLOWED_CHATS:
        if chat_id == allowed_chat:
            if allowed_thread is None or allowed_thread == thread_id:
                return True
    return False


def _get_lock(address: str) -> asyncio.Lock:
    if address not in _locks:
        _locks[address] = asyncio.Lock()
    return _locks[address]


# ─── Live-повідомлення ───────────────────────────────────────────────────────

async def build_live_text(address: str) -> str:
    state = await hyperliquid.get_user_state(address)
    if state is None:
        return "❌ Не вдалось отримати дані"

    positions = hyperliquid.parse_positions(state)
    prices = await hyperliquid.get_all_mids()
    account_value = float(state.get("marginSummary", {}).get("accountValue", 0))
    updated = datetime.now().strftime("%H:%M:%S")

    if not positions:
        return (
            f"📊 <code>{address[:6]}...{address[-4:]}</code>\n"
            f"Акаунт: <b>${account_value:,.2f}</b>\n\n"
            "Немає відкритих позицій\n\n"
            f"🕐 {updated}"
        )

    lines = [
        f"📊 <code>{address[:6]}...{address[-4:]}</code>  "
        f"Акаунт: <b>${account_value:,.2f}</b>\n",
    ]

    for coin, p in sorted(positions.items(), key=lambda x: -x[1]["position_value"]):
        side_emoji = "🟢" if p["side"] == "LONG" else "🔴"
        cur_px = prices.get(coin, 0)
        upnl = p["unrealized_pnl"]
        upnl_str = f"+${upnl:,.2f}" if upnl >= 0 else f"-${abs(upnl):,.2f}"
        upnl_emoji = "📈" if upnl >= 0 else "📉"

        liq_str = ""
        if p["liquidation_price"] > 0:
            liq_px = p["liquidation_price"]
            if cur_px > 0:
                dist = tracker.liq_distance_pct(p, cur_px)
                danger = " ‼️" if dist is not None and dist < tracker.LIQ_DANGER_PCT else ""
                liq_str = f"  Liq: ${liq_px:,.4f}{danger}"
            else:
                liq_str = f"  Liq: ${liq_px:,.4f}"

        cur_str = f"${cur_px:,.4f}" if cur_px else "—"

        lines.append(
            f"{side_emoji} <b>{coin}</b>  {p['side']}  {p['leverage']}x  (${p['position_value']:,.0f})\n"
            f"   Ціна: <b>{cur_str}</b>  |  Entry: ${p['entry_price']:,.4f}{liq_str}\n"
            f"   uPnL: <b>{upnl_emoji} {upnl_str}</b>\n"
        )

    lines.append(f"🕐 <i>Оновлено: {updated}</i>")
    return "\n".join(lines)


async def live_update_loop(chat_id: int, message_id: int, address: str):
    elapsed = 0
    while elapsed < LIVE_TIMEOUT:
        await asyncio.sleep(LIVE_UPDATE_INTERVAL)
        elapsed += LIVE_UPDATE_INTERVAL
        try:
            text = await build_live_text(address)
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                continue
            logger.warning(f"Live edit error: {e}")
            break
        except Exception as e:
            logger.error(f"Live update error: {e}")
            break

    try:
        text = await build_live_text(address)
        stopped_at = datetime.now().strftime("%H:%M:%S")
        text += f"\n\n⏹ <i>Зупинено о {stopped_at}  ·  /check щоб запустити знову</i>"
        await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception:
        pass

    _live_tasks.pop(chat_id, None)


def _cancel_live(chat_id: int):
    task = _live_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


# ─── Ядро: перевірити позиції і надіслати зміни ─────────────────────────────

async def check_and_notify(address: str, label: str, chat_id: int, thread_id: int = None):
    import time
    async with _get_lock(address):
        _last_checked[address] = time.monotonic()

        state = await hyperliquid.get_user_state(address)
        if state is None:
            return

        new_positions = hyperliquid.parse_positions(state)
        old_positions = await storage.get_snapshots(address)
        current_prices = await hyperliquid.get_all_mids()

        changes = tracker.diff_positions(old_positions, new_positions)

        enabled_groups = await storage.get_wallet_notify_groups(address)
        push_enabled = await storage.get_wallet_pushover(address)
        push_keys = await storage.get_all_pushover_keys() if push_enabled else []
        push_min = await storage.get_wallet_pushover_min(address) if push_keys else 0.0

        for change in changes:
            if enabled_groups is not None:
                group = tracker.get_change_type_group(change.change_type)
                if group not in enabled_groups:
                    continue
            price = current_prices.get(change.coin)
            text = tracker.format_change(change, label, address, current_price=price)
            await bot.send_message(
                chat_id, text,
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if push_keys and tracker.change_usd_amount(change) >= push_min:
                title = f"🐋 {change.coin} {change.change_type.value}"
                await pushover.send(PUSHOVER_APP_TOKEN, push_keys, title, text)

        for coin in list(old_positions):
            if coin not in new_positions:
                await storage.delete_snapshot(address, coin)
        for coin, pos in new_positions.items():
            await storage.save_snapshot(address, coin, pos)


# ─── WebSocket callback ──────────────────────────────────────────────────────

TWAP_POLL_INTERVAL = 15

_TWAP_STATUS_MAP = {
    "activated":  tracker.ChangeType.TWAP_STARTED,
    "finished":   tracker.ChangeType.TWAP_FINISHED,
    "terminated": tracker.ChangeType.TWAP_CANCELLED,
    "error":      tracker.ChangeType.TWAP_ERROR,
}


async def check_twaps_and_notify(address: str, label: str, chat_id: int, thread_id):
    enabled = await storage.get_wallet_notify_groups(address)
    if enabled is not None and "twaps" not in enabled:
        return

    history = await hyperliquid.get_twap_history(address)
    if not history:
        return

    prices = await hyperliquid.get_all_mids()

    for item in history:
        twap_id    = item.get("twapId")
        state      = item.get("state", {})
        status_obj = item.get("status", {})
        status     = status_obj.get("status", "") if isinstance(status_obj, dict) else str(status_obj)
        ct = _TWAP_STATUS_MAP.get(status)

        if ct is None or twap_id is None:
            continue
        if await storage.is_twap_event_seen(address, twap_id, status):
            continue

        await storage.mark_twap_event_seen(address, twap_id, status)

        coin         = state.get("coin", "?")
        side_raw     = state.get("side", "A")
        side         = "BUY" if side_raw == "B" else "SELL"
        size         = float(state.get("sz", 0))
        executed_sz  = float(state.get("executedSz", 0))
        duration_min = int(state.get("minutes", 0))

        twap_change = tracker.TwapChange(
            change_type   = ct,
            coin          = coin,
            side          = side,
            size          = size,
            executed_size = executed_sz,
            twap_id       = twap_id,
            duration_min  = duration_min,
        )
        price = prices.get(coin)
        text  = tracker.format_twap_change(twap_change, label, address, current_price=price)
        await bot.send_message(
            chat_id, text,
            message_thread_id=thread_id,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def twap_poll_loop():
    await asyncio.sleep(15)
    logger.info(f"TWAP poll loop started (interval={TWAP_POLL_INTERVAL}s)")
    while True:
        await asyncio.sleep(TWAP_POLL_INTERVAL)
        try:
            wallets = await storage.get_all_wallets()
            for address, label, chat_id, thread_id in wallets:
                try:
                    await check_twaps_and_notify(address, label, chat_id, thread_id)
                except Exception as e:
                    logger.error(f"TWAP poll error {address[:8]}: {e}")
                await asyncio.sleep(0.3)
        except Exception as e:
            logger.error(f"TWAP poll loop error: {e}")


async def on_ws_event(address: str, event_type: str, data):
    wallet = await storage.get_wallet(address)
    if not wallet:
        return
    _, label, chat_id, thread_id = wallet
    logger.info(f"WS event [{event_type}] for {address[:8]}...")

    if event_type == "twap":
        # WS triggered — immediate poll (polling handles dedup)
        await check_twaps_and_notify(address, label, chat_id, thread_id)
        return

    await asyncio.sleep(0.6)
    await check_and_notify(address, label, chat_id, thread_id)


# ─── Команди ────────────────────────────────────────────────────────────────

ws_client: HLWebSocket = None


@dp.message(Command("myid"))
async def cmd_myid(message: types.Message):
    chat_id = message.chat.id
    thread_id = message.message_thread_id
    chat_type = message.chat.type

    lines = [
        "🔑 <b>Ідентифікатори чату</b>\n",
        f"Chat ID: <code>{chat_id}</code>",
        f"Тип: {chat_type}",
    ]
    if thread_id:
        lines.append(f"Thread ID: <code>{thread_id}</code>")
        lines.append(f"\nДодай в .env:")
        lines.append(f"<code>ALLOWED_CHATS={chat_id}:{thread_id}</code>")
    else:
        lines.append(f"\nДодай в .env:")
        lines.append(f"<code>ALLOWED_CHATS={chat_id}</code>")

    await message.answer("\n".join(lines))


@dp.message(Command("start", "help"))
async def cmd_start(message: types.Message):
    if not is_allowed(message):
        return
    await message.answer(
        "🐋 <b>Hyperliquid Whale Tracker</b>\n\n"
        "<b>Команди:</b>\n"
        "/guide — гайд по користуванню\n"
        "/add <code>0xАДРЕСА</code> [мітка] — додати гаманець\n"
        "/remove <code>0xАДРЕСА</code> — видалити гаманець\n"
        "/lists — список з кнопками видалення\n"
        "/check <code>0xАДРЕСА</code> — live ціна + PnL\n"
        "/filter <code>0xАДРЕСА</code> — фільтр типів сповіщень\n"
        "/pushover <code>0xАДРЕСА</code> — увімк/вимк Pushover для гаманця\n"
        "/pushfilter <code>0xАДРЕСА</code> <code>СУМА</code> — мінімум $ для пушу\n"
        "/setpushover <code>USER_KEY</code> — підключити свій Pushover\n"
        "/mypushover — перевірити статус підключення\n"
        "/delpushover — відключити свій Pushover\n"
        "/setthread — надсилати всі алерти в цю гілку\n"
        "/myid — показати ID цього чату\n\n"
        "Зміни позицій приходять окремим повідомленням."
    )


@dp.message(Command("guide"))
async def cmd_guide(message: types.Message):
    if not is_allowed(message):
        return
    await message.answer(
        "📖 <b>Гайд по користуванню HyperLiquid Whale Tracker</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>Функції бота</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "• Трекає всі відкриті позиції та баланс гаманця\n"
        "• Відстежує відкриття / закриття позицій\n"
        "• Відстежує виставлення / скасування лімітних ордерів\n"
        "• Відстежує запуск / завершення / скасування TWAP ордерів\n"
        "• Сповіщає про додавання / виведення маржі з позиції\n"
        "• Pushover push-сповіщення з фільтром по сумі\n"
        "• Гнучкий фільтр типів сповіщень per-wallet\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "➕ <b>Підключити гаманець</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Додати гаманець:\n"
        "<code>/add 0x...123</code>\n\n"
        "2️⃣ Переглянути поточні позиції (live):\n"
        "<code>/check 0x...123</code>\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 <b>Підключити Pushover</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "1️⃣ Реєструємо свій Pushover ключ:\n"
        "<code>/setpushover YOUR_USER_KEY</code>\n\n"
        "2️⃣ Вмикаємо Pushover для гаманця:\n"
        "<code>/pushover 0x...123</code>\n\n"
        "3️⃣ (Опційно) Встановлюємо мінімальну суму:\n"
        "<code>/pushfilter 0x...123 20000</code>\n"
        "<i>→ пуш прийде лише при зміні позиції ≥ $20,000</i>\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Важливо — типи сповіщень</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 <b>ЗБІЛЬШЕНО</b> — кит добрав частину позиції\n"
        "📉 <b>ЗМЕНШЕНО</b> — кит зменшив частину позиції\n"
        "🟢 <b>ВІДКРИТО</b> — нова позиція\n"
        "🔴 <b>ЗАКРИТО</b> — позиція повністю закрита\n"
        "🔄 <b>ФЛІП СТОРОНИ</b> — позиція перевернута\n"
        "🚨 <b>ВИВІД МАРЖІ</b> — кит зменшив заставу (ризик)\n"
        "🛡 <b>ДОДАНО МАРЖУ</b> — кит укріпив позицію\n\n"
        "📋 <b>ВИСТАВЛЕНО ОРДЕР</b> — лімітка виставлена\n"
        "❌ <b>СКАСОВАНО ОРДЕР</b> — лімітка знята або виконана\n\n"
        "⏱ <b>ТВАП ЗАПУЩЕНО</b> — TWAP ордер активований\n"
        "✅ <b>ТВАП ЗАВЕРШЕНО</b> — TWAP виконано повністю\n"
        "🛑 <b>ТВАП СКАСОВАНО</b> — TWAP зупинено вручну\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>Фільтр сповіщень</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Щоб не отримувати спам від лімітних ордерів:\n"
        "<code>/filter 0x...123</code>\n"
        "Відкриється меню з кнопками ✅/❌ для кожного типу:\n"
        "<i>Позиції · Зміни розміру · Маржа · Ліміт ордери · TWAP</i>",
        disable_web_page_preview=True,
    )


@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    if not is_allowed(message):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Використання: /add <code>0xАДРЕСА</code> [мітка]")
        return

    address = parts[1].strip().lower()
    label = parts[2].strip() if len(parts) > 2 else ""

    if not (address.startswith("0x") and len(address) == 42):
        await message.answer("❌ Невалідна адреса. Формат: <code>0x...</code> (42 символи)")
        return

    msg = await message.answer("🔍 Перевіряю адресу...")

    state = await hyperliquid.get_user_state(address)
    if state is None:
        await msg.edit_text("❌ Не вдалось підключитись до Hyperliquid API")
        return

    positions = hyperliquid.parse_positions(state)
    thread_id = message.message_thread_id
    await storage.add_wallet(address, label, message.chat.id, thread_id)
    for coin, pos in positions.items():
        await storage.save_snapshot(address, coin, pos)
    if ws_client:
        await ws_client.subscribe(address)

    label_str = f" <b>{label}</b>" if label else ""
    pos_lines = []
    for coin, p in positions.items():
        side = "🟢 L" if p["side"] == "LONG" else "🔴 S"
        margin = "cross" if p.get("is_cross") else "isolated"
        pos_lines.append(f"  {side} {coin}  ${p['position_value']:,.0f}  {p['leverage']}x ({margin})")

    pos_text = "\n".join(pos_lines) if pos_lines else "  (немає відкритих позицій)"
    await msg.edit_text(
        f"✅ Додано{label_str}\n"
        f"<code>{address}</code>\n\n"
        f"Поточні позиції:\n{pos_text}\n\n"
        "⚡ WebSocket підписка активна"
    )


@dp.message(Command("remove"))
async def cmd_remove(message: types.Message):
    if not is_allowed(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Використання: /remove <code>0xАДРЕСА</code>")
        return

    address = parts[1].strip().lower()
    await _do_remove(message.chat.id, address)


async def _do_remove(chat_id: int, address: str):
    wallet = await storage.get_wallet(address)
    if not wallet:
        await bot.send_message(chat_id, "❌ Гаманець не знайдений в списку")
        return

    await storage.remove_wallet(address)
    if ws_client:
        await ws_client.unsubscribe(address)

    label = wallet[1]
    label_str = f" ({label})" if label else ""
    await bot.send_message(chat_id, f"🗑 Видалено{label_str}\n<code>{address}</code>")


@dp.message(Command("lists"))
async def cmd_list(message: types.Message):
    if not is_allowed(message):
        return
    await send_wallet_list(message.chat.id, message.message_thread_id)


async def send_wallet_list(chat_id: int, thread_id: int = None):
    wallets = await storage.get_all_wallets()
    if not wallets:
        await bot.send_message(
            chat_id,
            "Список порожній.\nДодайте: /add <code>0xАДРЕСА</code> [мітка]",
            message_thread_id=thread_id,
        )
        return

    lines = [f"📋 <b>Відстежувані гаманці</b> ({len(wallets)}):"]
    buttons = []

    for addr, label, _, __ in wallets:
        label_str = f"  <b>{label}</b>" if label else ""
        lines.append(f"\n• <code>{addr}</code>{label_str}")
        btn_label = f"🗑 {label}" if label else f"🗑 {addr[:8]}..."
        buttons.append([InlineKeyboardButton(
            text=btn_label,
            callback_data=f"remove:{addr}"
        )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await bot.send_message(
        chat_id, "\n".join(lines),
        reply_markup=keyboard,
        message_thread_id=thread_id,
    )


@dp.callback_query(F.data.startswith("remove:"))
async def cb_remove(callback: types.CallbackQuery):
    if not is_allowed(callback.message):
        await callback.answer("⛔ Доступ заборонено", show_alert=True)
        return

    address = callback.data.split(":", 1)[1]
    wallet = await storage.get_wallet(address)

    if not wallet:
        await callback.answer("Вже видалено", show_alert=False)
        await callback.message.delete()
        return

    await storage.remove_wallet(address)
    if ws_client:
        await ws_client.unsubscribe(address)

    label = wallet[1]
    label_str = f" ({label})" if label else ""
    await callback.answer(f"Видалено{label_str}", show_alert=False)

    # Оновлюємо список або видаляємо повідомлення якщо список порожній
    wallets = await storage.get_all_wallets()
    if not wallets:
        await callback.message.edit_text("📋 Список порожній.")
    else:
        lines = [f"📋 <b>Відстежувані гаманці</b> ({len(wallets)}):"]
        buttons = []
        for addr, lbl, _, __ in wallets:
            lbl_str = f"  <b>{lbl}</b>" if lbl else ""
            lines.append(f"\n• <code>{addr}</code>{lbl_str}")
            btn_label = f"🗑 {lbl}" if lbl else f"🗑 {addr[:8]}..."
            buttons.append([InlineKeyboardButton(
                text=btn_label,
                callback_data=f"remove:{addr}"
            )])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text("\n".join(lines), reply_markup=keyboard)


@dp.message(Command("setthread"))
async def cmd_setthread(message: types.Message):
    if not is_allowed(message):
        return

    chat_id = message.chat.id
    thread_id = message.message_thread_id

    wallets = await storage.get_all_wallets()
    if not wallets:
        await message.answer("Список гаманців порожній.")
        return

    for address, label, _, __ in wallets:
        await storage.add_wallet(address, label, chat_id, thread_id)

    thread_str = f" (thread {thread_id})" if thread_id else ""
    await message.answer(
        f"✅ Готово — {len(wallets)} гаманців тепер надсилають алерти в цю гілку{thread_str}"
    )


@dp.message(Command("setpushover"))
async def cmd_setpushover(message: types.Message):

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Використання: /setpushover <code>USER_KEY</code>\n\n"
            "User Key знаходиться на головній сторінці pushover.net після логіну."
        )
        return

    user_key = parts[1].strip()
    user_id = message.from_user.id
    await storage.set_pushover_user(user_id, user_key)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(
        f"✅ Pushover підключено\n"
        f"Тепер ти будеш отримувати пуш-сповіщення для всіх гаманців з увімкненим Pushover."
    )


@dp.message(Command("delpushover"))
async def cmd_delpushover(message: types.Message):
    await storage.delete_pushover_user(message.from_user.id)
    await message.answer("🔕 Pushover відключено")


@dp.message(Command("mypushover"))
async def cmd_mypushover(message: types.Message):
    import aiosqlite
    from config import DB_PATH
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_key FROM pushover_users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    all_keys = await storage.get_all_pushover_keys()
    if row:
        await message.answer(
            f"✅ Pushover підключено\n"
            f"Всього зареєстровано: {len(all_keys)} користувач(ів)"
        )
    else:
        await message.answer(
            "❌ Ти не підключений до Pushover\n"
            "Введи: /setpushover <code>USER_KEY</code>"
        )


@dp.message(Command("pushfilter"))
async def cmd_pushfilter(message: types.Message):
    if not is_allowed(message):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Використання: /pushfilter <code>0xАДРЕСА</code> <code>СУМА</code>\n"
            "Приклад: /pushfilter 0x1234...abcd 20000\n"
            "Встановити 0 — без фільтру"
        )
        return

    address = parts[1].strip().lower()
    wallet = await storage.get_wallet(address)
    if not wallet:
        await message.answer("❌ Гаманець не знайдений в списку відстеження")
        return

    try:
        min_usd = float(parts[2].strip().replace(",", ""))
    except ValueError:
        await message.answer("❌ Невалідна сума")
        return

    await storage.set_wallet_pushover_min(address, min_usd)
    label = wallet[1]
    label_str = f" <b>{label}</b>" if label else ""
    if min_usd > 0:
        await message.answer(
            f"✅ Фільтр Pushover{label_str}\n"
            f"<code>{address}</code>\n"
            f"Пуш тільки при зміні ≥ <b>${min_usd:,.0f}</b>"
        )
    else:
        await message.answer(f"✅ Фільтр знятий{label_str} — пуш при будь-якій зміні")


@dp.message(Command("testpushover"))
async def cmd_testpushover(message: types.Message):
    from config import DB_PATH
    keys = await storage.get_all_pushover_keys()
    if not keys:
        await message.answer(
            f"❌ Немає зареєстрованих Pushover користувачів\n"
            f"<code>DB: {DB_PATH}</code>\n"
            f"<code>your user_id: {message.from_user.id}</code>"
        )
        return

    await pushover.send(
        PUSHOVER_APP_TOKEN, keys,
        "🐋 HL Whale Tracker",
        "Тестове сповіщення — підключення працює!"
    )
    await message.answer(f"✅ Тест відправлено на {len(keys)} пристрій(и)")


@dp.message(Command("pushover"))
async def cmd_pushover(message: types.Message):
    if not is_allowed(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Використання: /pushover <code>0xАДРЕСА</code>")
        return

    address = parts[1].strip().lower()
    wallet = await storage.get_wallet(address)
    if not wallet:
        await message.answer("❌ Гаманець не знайдений в списку відстеження")
        return

    current = await storage.get_wallet_pushover(address)
    new_state = not current
    await storage.set_wallet_pushover(address, new_state)

    label = wallet[1]
    label_str = f" <b>{label}</b>" if label else ""
    status = "✅ Pushover увімкнено" if new_state else "🔕 Pushover вимкнено"
    await message.answer(
        f"{status}{label_str}\n<code>{address}</code>"
    )


@dp.message(Command("filter"))
async def cmd_filter(message: types.Message):
    if not is_allowed(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Використання: /filter <code>0xАДРЕСА</code>")
        return
    address = parts[1].strip().lower()
    wallet = await storage.get_wallet(address)
    if not wallet:
        await message.answer("❌ Гаманець не знайдений в списку відстеження")
        return
    await _send_filter_menu(message.chat.id, message.message_thread_id, address, wallet[1])


async def _send_filter_menu(chat_id: int, thread_id, address: str, label: str):
    enabled = await storage.get_wallet_notify_groups(address)
    if enabled is None:
        enabled = tracker.ALL_GROUPS.copy()

    label_str = f" <b>{label}</b>" if label else ""
    short = f"{address[:6]}...{address[-4:]}"

    buttons, row = [], []
    for group, gname in tracker.GROUP_LABELS.items():
        icon = "✅" if group in enabled else "❌"
        row.append(InlineKeyboardButton(
            text=f"{icon} {gname}",
            callback_data=f"flt:{address}:{group}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await bot.send_message(
        chat_id,
        f"⚙️ <b>Фільтр сповіщень</b>{label_str}\n"
        f"<code>{short}</code>\n\n"
        "Натисніть щоб увімк/вимк тип сповіщень:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        message_thread_id=thread_id,
    )


@dp.callback_query(F.data.startswith("flt:"))
async def cb_filter(callback: types.CallbackQuery):
    _, address, group = callback.data.split(":", 2)

    enabled = await storage.get_wallet_notify_groups(address)
    if enabled is None:
        enabled = tracker.ALL_GROUPS.copy()

    if group in enabled:
        enabled.discard(group)
    else:
        enabled.add(group)

    await storage.set_wallet_notify_groups(address, enabled)

    wallet = await storage.get_wallet(address)
    label = wallet[1] if wallet else ""
    label_str = f" <b>{label}</b>" if label else ""
    short = f"{address[:6]}...{address[-4:]}"

    buttons, row = [], []
    for g, gname in tracker.GROUP_LABELS.items():
        icon = "✅" if g in enabled else "❌"
        row.append(InlineKeyboardButton(
            text=f"{icon} {gname}",
            callback_data=f"flt:{address}:{g}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await callback.message.edit_text(
        f"⚙️ <b>Фільтр сповіщень</b>{label_str}\n"
        f"<code>{short}</code>\n\n"
        "Натисніть щоб увімк/вимк тип сповіщень:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    if not is_allowed(message):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Використання: /check <code>0xАДРЕСА</code>")
        return

    address = parts[1].strip().lower()
    _cancel_live(message.chat.id)

    msg = await message.answer("🔍 Завантажую...")
    text = await build_live_text(address)
    await msg.edit_text(text, disable_web_page_preview=True)

    task = asyncio.create_task(
        live_update_loop(message.chat.id, msg.message_id, address)
    )
    _live_tasks[message.chat.id] = task


# ─── Order tracking ─────────────────────────────────────────────────────────

async def check_orders_and_notify(address: str, label: str, chat_id: int, thread_id: int = None):
    new_orders = await hyperliquid.get_open_orders(address)
    old_orders = await storage.get_order_snapshots(address)
    current_prices = await hyperliquid.get_all_mids()

    changes = tracker.diff_orders(old_orders, new_orders)
    enabled_groups = await storage.get_wallet_notify_groups(address) if changes else None

    for change in changes:
        if enabled_groups is not None and "orders" not in enabled_groups:
            pass  # skip send, but still update snapshots below
        else:
            price = current_prices.get(change.coin)
            text = tracker.format_order_change(change, label, address, current_price=price)
            await bot.send_message(
                chat_id, text,
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    new_oids = {o["oid"] for o in new_orders}
    old_oids = set(old_orders.keys())
    for o in new_orders:
        await storage.save_order_snapshot(address, o["oid"], o)
    for oid in old_oids - new_oids:
        await storage.delete_order_snapshot(address, oid)


async def order_poll_loop():
    logger.info(f"Order poll loop started (interval={ORDER_POLL_INTERVAL}s)")
    while True:
        await asyncio.sleep(ORDER_POLL_INTERVAL)
        try:
            wallets = await storage.get_all_wallets()
            for address, label, chat_id, thread_id in wallets:
                try:
                    await check_orders_and_notify(address, label, chat_id, thread_id)
                except Exception as e:
                    logger.error(f"Order poll error {address[:8]}: {e}")
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Order poll loop error: {e}")


# ─── Fast margin poll ────────────────────────────────────────────────────────

async def margin_poll_loop():
    import time
    logger.info(f"Margin poll loop started (interval={MARGIN_POLL_INTERVAL}s)")
    while True:
        await asyncio.sleep(MARGIN_POLL_INTERVAL)
        try:
            wallets = await storage.get_all_wallets()
            for address, label, chat_id, thread_id in wallets:
                last = _last_checked.get(address, 0)
                if time.monotonic() - last < 1.5:
                    continue
                try:
                    await check_and_notify(address, label, chat_id, thread_id)
                except Exception as e:
                    logger.error(f"Margin poll error {address[:8]}: {e}")
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Margin poll loop error: {e}")
