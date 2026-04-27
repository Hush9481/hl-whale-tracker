from dataclasses import dataclass
from enum import Enum
from typing import Optional
from config import SIZE_CHANGE_THRESHOLD

# Мінімальний % зменшення маржі щоб вважати це "виводом ліквідності"
MARGIN_REMOVE_THRESHOLD = 0.03   # 3%
# Скільки % до ліквідації вважати "небезпечним" (для попередження)
LIQ_DANGER_PCT = 15.0


class ChangeType(Enum):
    OPENED          = "ВІДКРИТО"
    CLOSED          = "ЗАКРИТО"
    LIQUIDATED      = "ЛІКВІДОВАНО"
    INCREASED       = "ЗБІЛЬШЕНО"
    DECREASED       = "ЗМЕНШЕНО"
    SIDE_FLIP       = "ФЛІП СТОРОНИ"
    MARGIN_REMOVED  = "ВИВІД МАРЖІ ⚠️"
    MARGIN_ADDED    = "ДОДАНО МАРЖУ"
    ORDER_PLACED    = "ВИСТАВЛЕНО ОРДЕР"
    ORDER_CANCELLED = "СКАСОВАНО ОРДЕР"


@dataclass
class OrderChange:
    change_type: ChangeType
    coin: str
    order: dict


@dataclass
class PositionChange:
    change_type: ChangeType
    coin: str
    old_pos: Optional[dict]
    new_pos: Optional[dict]


def liq_distance_pct(pos: dict, current_price: float) -> Optional[float]:
    """
    Відстань від поточної ціни до ліквідаційної ціни у %.
    LONG: (current - liq) / current * 100
    SHORT: (liq - current) / current * 100
    Менше = небезпечніше.
    """
    liq = pos.get("liquidation_price", 0)
    if liq == 0 or current_price == 0:
        return None
    if pos["side"] == "LONG":
        return (current_price - liq) / current_price * 100
    else:
        return (liq - current_price) / current_price * 100


def _is_likely_liquidation(old_pos: dict) -> bool:
    upnl = old_pos.get("unrealized_pnl", 0)
    margin = old_pos.get("margin_used", 1)
    if margin == 0:
        return False
    loss_ratio = abs(upnl) / margin if upnl < 0 else 0
    return loss_ratio > 0.8


def diff_positions(old: dict, new: dict) -> list[PositionChange]:
    changes = []
    all_coins = set(old.keys()) | set(new.keys())

    for coin in all_coins:
        old_pos = old.get(coin)
        new_pos = new.get(coin)

        if old_pos is None and new_pos is not None:
            changes.append(PositionChange(ChangeType.OPENED, coin, None, new_pos))
            continue

        if old_pos is not None and new_pos is None:
            change_type = (
                ChangeType.LIQUIDATED
                if _is_likely_liquidation(old_pos)
                else ChangeType.CLOSED
            )
            changes.append(PositionChange(change_type, coin, old_pos, None))
            continue

        if old_pos and new_pos:
            old_size = abs(old_pos["size"])
            new_size = abs(new_pos["size"])

            # Фліп сторони — найважливіше, перевіряємо першим
            if old_pos["side"] != new_pos["side"]:
                changes.append(PositionChange(ChangeType.SIDE_FLIP, coin, old_pos, new_pos))
                continue

            # Зміна розміру
            if new_size > old_size * (1 + SIZE_CHANGE_THRESHOLD):
                changes.append(PositionChange(ChangeType.INCREASED, coin, old_pos, new_pos))
                continue
            if new_size < old_size * (1 - SIZE_CHANGE_THRESHOLD):
                changes.append(PositionChange(ChangeType.DECREASED, coin, old_pos, new_pos))
                continue

            # Вивід маржі: розмір стабільний, але margin_used суттєво зменшився
            # і ліквідаційна ціна наблизилась
            old_margin = old_pos.get("margin_used", 0)
            new_margin = new_pos.get("margin_used", 0)
            old_liq = old_pos.get("liquidation_price", 0)
            new_liq = new_pos.get("liquidation_price", 0)

            if old_margin > 0 and new_margin < old_margin * (1 - MARGIN_REMOVE_THRESHOLD):
                liq_moved = _liq_moved_closer(old_pos["side"], old_liq, new_liq)
                if liq_moved:
                    changes.append(
                        PositionChange(ChangeType.MARGIN_REMOVED, coin, old_pos, new_pos)
                    )
            elif old_margin > 0 and new_margin > old_margin * (1 + MARGIN_REMOVE_THRESHOLD):
                # Додавання маржі: розмір стабільний, маржа зросла, лік відсунулась
                liq_moved_away = _liq_moved_away(old_pos["side"], old_liq, new_liq)
                if liq_moved_away or (old_liq == 0 and new_liq == 0):
                    changes.append(
                        PositionChange(ChangeType.MARGIN_ADDED, coin, old_pos, new_pos)
                    )

    return changes


def _liq_moved_closer(side: str, old_liq: float, new_liq: float) -> bool:
    """Перевіряє, що ліквідаційна ціна наблизилась до ринку."""
    if old_liq == 0 or new_liq == 0:
        return False
    if side == "LONG":
        return new_liq > old_liq
    else:
        return new_liq < old_liq


def _liq_moved_away(side: str, old_liq: float, new_liq: float) -> bool:
    """Перевіряє, що ліквідаційна ціна відсунулась від ринку (маржу додали)."""
    if old_liq == 0 or new_liq == 0:
        return False
    if side == "LONG":
        return new_liq < old_liq
    else:
        return new_liq > old_liq


def format_change(
    change: PositionChange,
    wallet_label: str,
    address: str,
    current_price: Optional[float] = None,
) -> str:
    emoji_map = {
        ChangeType.OPENED:         "🟢",
        ChangeType.CLOSED:         "🔴",
        ChangeType.LIQUIDATED:     "💀",
        ChangeType.INCREASED:      "📈",
        ChangeType.DECREASED:      "📉",
        ChangeType.SIDE_FLIP:      "🔄",
        ChangeType.MARGIN_REMOVED: "🚨",
        ChangeType.MARGIN_ADDED:   "🛡",
    }
    emoji = emoji_map[change.change_type]
    short_addr = f"{address[:6]}...{address[-4:]}"
    display = (
        f"{wallet_label}  <code>{short_addr}</code>"
        if wallet_label else f"<code>{short_addr}</code>"
    )

    lines = [
        f"{emoji} <b>{change.change_type.value}</b>  ·  {display}",
        f"Токен: <b>{change.coin}</b>",
    ]

    # ── ВИВІД / ДОДАВАННЯ МАРЖІ ─────────────────────────────────────────────
    if change.change_type in (ChangeType.MARGIN_REMOVED, ChangeType.MARGIN_ADDED):
        is_removed = change.change_type == ChangeType.MARGIN_REMOVED
        old_p = change.old_pos
        new_p = change.new_pos
        side_str = "🟢 LONG" if new_p["side"] == "LONG" else "🔴 SHORT"

        old_margin = old_p["margin_used"]
        new_margin = new_p["margin_used"]
        delta = abs(new_margin - old_margin)
        delta_pct = delta / old_margin * 100
        action_str = f"💸 Виведено: <b>${delta:,.0f}</b>  ({delta_pct:.1f}%)" if is_removed \
                else f"💰 Додано: <b>${delta:,.0f}</b>  ({delta_pct:.1f}%)"

        old_liq = old_p["liquidation_price"]
        new_liq = new_p["liquidation_price"]
        liq_shift = abs(new_liq - old_liq)
        liq_shift_pct = liq_shift / old_liq * 100 if old_liq else 0

        lines += [
            f"Сторона: {side_str}  ·  {new_p['leverage']}x",
            f"Розмір: <b>${new_p['position_value']:,.0f}</b>",
            "",
            action_str,
            f"   Було: ${old_margin:,.0f}  →  Стало: ${new_margin:,.0f}",
        ]

        if old_liq > 0 and new_liq > 0:
            direction = "наблизилась ↑" if is_removed else "відсунулась ↓"
            lines += [
                "",
                f"📍 Ліквідаційна ціна ({direction}):",
                f"   Була:  ${old_liq:,.4f}",
                f"   Стала: <b>${new_liq:,.4f}</b>  (зсув {liq_shift_pct:.2f}%)",
            ]

        if current_price and current_price > 0:
            dist_old = liq_distance_pct(old_p, current_price)
            dist_new = liq_distance_pct(new_p, current_price)
            if dist_old is not None and dist_new is not None:
                danger = " ‼️" if (is_removed and dist_new < LIQ_DANGER_PCT) else ""
                lines += [
                    "",
                    f"📏 Дистанція до ліквідації:",
                    f"   Була:  {dist_old:.2f}%",
                    f"   Стала: <b>{dist_new:.2f}%{danger}</b>",
                    f"   Поточна ціна: ${current_price:,.4f}",
                ]

        footer = "⚠️ <i>Кит зменшив маржу — ліквідаційна ціна наблизилась</i>" if is_removed \
            else "✅ <i>Кит збільшив маржу — позиція захищена краще</i>"
        lines.append(f"\n{footer}")

    # ── Всі інші типи ───────────────────────────────────────────────────────
    elif change.new_pos:
        p = change.new_pos
        side_str = "🟢 LONG" if p["side"] == "LONG" else "🔴 SHORT"
        margin_type = "cross" if p.get("is_cross") else "isolated"
        lines += [
            f"Сторона: {side_str}  ·  {p['leverage']}x  ({margin_type})",
            f"Розмір: <b>${p['position_value']:,.0f}</b>",
            f"Entry: ${p['entry_price']:,.4f}",
        ]
        if p["liquidation_price"] > 0:
            liq_line = f"Ліквідація: ${p['liquidation_price']:,.4f}"
            if current_price:
                dist = liq_distance_pct(p, current_price)
                if dist is not None:
                    danger = " ‼️" if dist < LIQ_DANGER_PCT else ""
                    liq_line += f"  ({dist:.2f}%{danger})"
            lines.append(liq_line)
        else:
            lines.append("Ліквідація: <i>cross margin — рахується по акаунту</i>")
        lines.append(f"uPnL: <b>${p['unrealized_pnl']:+,.2f}</b>")
        if change.old_pos:
            size_diff = abs(p["size"]) - abs(change.old_pos["size"])
            val_diff = p["position_value"] - change.old_pos["position_value"]
            sign = "+" if size_diff > 0 else ""
            lines.append(f"Зміна: {sign}{size_diff:.4f}  (${val_diff:+,.0f})")

    elif change.old_pos:
        p = change.old_pos
        side_str = "🟢 LONG" if p["side"] == "LONG" else "🔴 SHORT"
        lines += [
            f"Була: {side_str}  {abs(p['size']):.4f}  @ ${p['entry_price']:,.4f}",
            f"uPnL на момент закриття: <b>${p['unrealized_pnl']:+,.2f}</b>",
            f"Розмір позиції: ${p['position_value']:,.0f}",
        ]
        if change.change_type == ChangeType.LIQUIDATED:
            lines.append("⚠️ <i>Ймовірна примусова ліквідація</i>")

    lines.append(
        f"\n🔗 <a href='https://app.hyperliquid.xyz/trade/{change.coin}'>Hyperliquid chart</a>"
        f"  ·  <a href='https://hypurrscan.io/address/{address}'>Hypurrscan</a>"
    )
    return "\n".join(lines)


def diff_orders(old: dict, new: list) -> list[OrderChange]:
    """
    old: {oid: order_dict} з БД
    new: список ордерів з API
    """
    changes = []
    new_by_oid = {o["oid"]: o for o in new}

    for oid, order in new_by_oid.items():
        if oid not in old:
            changes.append(OrderChange(ChangeType.ORDER_PLACED, order["coin"], order))

    for oid, order in old.items():
        if oid not in new_by_oid:
            changes.append(OrderChange(ChangeType.ORDER_CANCELLED, order["coin"], order))

    return changes


def format_order_change(
    change: OrderChange,
    wallet_label: str,
    address: str,
    current_price: Optional[float] = None,
) -> str:
    is_placed = change.change_type == ChangeType.ORDER_PLACED
    emoji = "📋" if is_placed else "❌"
    short_addr = f"{address[:6]}...{address[-4:]}"
    display = (
        f"{wallet_label}  <code>{short_addr}</code>"
        if wallet_label else f"<code>{short_addr}</code>"
    )

    o = change.order
    side_emoji = "🟢" if o["side"] == "BUY" else "🔴"
    side_str = "BUY LIMIT" if o["side"] == "BUY" else "SELL LIMIT"
    lim_px = o["limit_price"]
    size = o["size"]
    usd_val = size * lim_px

    lines = [
        f"{emoji} <b>{change.change_type.value}</b>  ·  {display}",
        f"{side_emoji} <b>{side_str}</b>  {change.coin}  <b>${lim_px:,.2f}</b>",
        f"~<b>${usd_val:,.0f}</b>  ({size:g} {change.coin})",
    ]

    if not is_placed:
        lines.append("<i>Ордер зник — скасований або виконаний</i>")

    lines.append(
        f"\n🔗 <a href='https://app.hyperliquid.xyz/trade/{change.coin}'>Hyperliquid chart</a>"
        f"  ·  <a href='https://hypurrscan.io/address/{address}'>Hypurrscan</a>"
    )
    return "\n".join(lines)
