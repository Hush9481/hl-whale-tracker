import aiohttp
import asyncio
import logging
from typing import Optional
from config import HL_API_URL, MIN_POSITION_VALUE

logger = logging.getLogger(__name__)

_mids_cache: dict = {}  # coin -> price, оновлюється щопуллу


async def get_all_mids() -> dict:
    """Повертає поточні mid-ціни для всіх активів {coin: price}"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HL_API_URL,
                json={"type": "allMids"},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    # raw — dict {coin: "price_string"}
                    result = {coin: float(px) for coin, px in raw.items()}
                    _mids_cache.update(result)
                    return result
    except Exception as e:
        logger.error(f"Error fetching allMids: {e}")
    return _mids_cache  # повертаємо кеш при помилці


def get_cached_price(coin: str) -> Optional[float]:
    return _mids_cache.get(coin)


async def get_user_state(address: str, retries: int = 3) -> Optional[dict]:
    payload = {"type": "clearinghouseState", "user": address}
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_API_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"HL API status {resp.status} for {address}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout fetching {address}, attempt {attempt + 1}")
        except Exception as e:
            logger.error(f"Error fetching {address}: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)
    return None


async def get_open_orders(address: str) -> list:
    """Повертає список відкритих лімітних ордерів"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                HL_API_URL,
                json={"type": "openOrders", "user": address},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    return [
                        {
                            "oid": item["oid"],
                            "coin": item["coin"],
                            "side": "BUY" if item["side"] == "B" else "SELL",
                            "limit_price": float(item["limitPx"]),
                            "size": float(item["sz"]),
                            "orig_size": float(item["origSz"]),
                            "timestamp": item.get("timestamp", 0),
                        }
                        for item in raw
                        if float(item.get("sz", 0)) > 0
                    ]
    except Exception as e:
        logger.error(f"Error fetching openOrders for {address}: {e}")
    return []


async def get_account_value(address: str) -> Optional[float]:
    """Повертає загальну вартість акаунту в $"""
    state = await get_user_state(address)
    if not state:
        return None
    margin = state.get("marginSummary", {})
    return float(margin.get("accountValue", 0))


def parse_positions(state: dict) -> dict:
    """
    Повертає dict {coin: position_data} для всіх відкритих позицій
    з вартістю >= MIN_POSITION_VALUE
    """
    positions = {}
    for item in state.get("assetPositions", []):
        pos = item.get("position", {})
        size = float(pos.get("szi", 0))
        if size == 0:
            continue

        coin = pos.get("coin", "")
        entry_px = float(pos.get("entryPx") or 0)
        pos_value = float(pos.get("positionValue") or 0)

        if pos_value < MIN_POSITION_VALUE:
            continue

        lev = pos.get("leverage", {})
        lev_value = lev.get("value", 1) if isinstance(lev, dict) else 1
        lev_type = lev.get("type", "isolated") if isinstance(lev, dict) else "isolated"
        is_cross = lev_type == "cross"

        liq_px_raw = pos.get("liquidationPx")
        liq_px = float(liq_px_raw) if liq_px_raw else 0.0

        positions[coin] = {
            "coin": coin,
            "size": size,
            "side": "LONG" if size > 0 else "SHORT",
            "entry_price": entry_px,
            "position_value": pos_value,
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            "liquidation_price": liq_px,
            "margin_used": float(pos.get("marginUsed") or 0),
            "leverage": lev_value,
            "is_cross": is_cross,
            "cum_funding": float(
                (pos.get("cumFunding") or {}).get("allTime", 0)
            ),
        }
    return positions
