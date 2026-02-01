import statistics
import threading
import time

import ccxt
import telebot

# ================= AYARLAR (KATI FILTRE) =================
TELEGRAM_TOKEN = "7043903963:AAF4Y5wgayT_PwRYVX4yM91TXETlFSYoffo"
TELEGRAM_CHAT_ID = "5448895488"

# --- STRATEJI AYARLARI ---
VOLUME_THRESHOLD = 5.0         # Hacim en az 5 KAT artmis olmali
BODY_STABILITY_PERCENT = 0.05  # Degisim kesinlikle %0.05 ve alti olmali
MAX_ALLOWED_TICKS = 5          # Makas 5 tick'ten fazlaysa ASLA bildirme
DEPTH_MULTIPLIER_TARGET = 3.0  # Tahta 3 kat dolmussa "DUVAR" ibaresi ekle
COOLDOWN_SECONDS = 60
STREAK_WINDOW_SECONDS = 130
MARKET_CACHE_TTL_SECONDS = 1800
SCAN_SLEEP_SECONDS = 1.2
RETRY_ATTEMPTS = 3
RETRY_BASE_SLEEP = 1.0

# --- OZEL TAKIP LISTESI ---
watched_coins = {
    "FIGHT/USDT",
    "ELSA/USDT",
    "SENT/USDT",
    "IMU/USDT",
    "BIRB/USDT",
}

watched_coins_lock = threading.Lock()
depth_memory = {}
market_info_cache = {}
alert_streaks = {}
alert_last_sent = {}
pending_alerts = {}
last_candle_ts = {}
market_cache = None
market_cache_loaded_at = 0.0

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ================= ANALIZ MOTORU =================


RETRYABLE_EXCEPTIONS = (
    ccxt.NetworkError,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
    ccxt.ExchangeNotAvailable,
    ccxt.RateLimitExceeded,
)


def safe_fetch(callable_fn, *args, **kwargs):
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return callable_fn(*args, **kwargs)
        except RETRYABLE_EXCEPTIONS:
            time.sleep(RETRY_BASE_SLEEP * attempt)
        except Exception:
            break
    return None


def load_markets_cached():
    global market_cache, market_cache_loaded_at
    now = time.time()
    if market_cache and now - market_cache_loaded_at < MARKET_CACHE_TTL_SECONDS:
        return market_cache

    markets = safe_fetch(exchange.load_markets)
    if markets:
        market_cache = markets
        market_cache_loaded_at = now
    return market_cache


def _tick_size_from_market(market):
    precision = market.get("precision", {}).get("price")
    if precision is not None:
        try:
            precision_value = float(precision)
            if precision_value.is_integer():
                return 10 ** (-int(precision_value))
            if precision_value > 0:
                return precision_value
        except (TypeError, ValueError):
            pass

    info = market.get("info") or {}
    price_filter = info.get("priceFilter") or info.get("price_filter") or {}
    tick = price_filter.get("tickSize") or price_filter.get("tick_size")
    if tick is not None:
        try:
            return float(tick)
        except (TypeError, ValueError):
            pass
    return None


def get_tick_size(symbol):
    if symbol in market_info_cache:
        return market_info_cache[symbol]
    try:
        markets = load_markets_cached()
        if not markets or symbol not in markets:
            return None
        tick_size = _tick_size_from_market(markets[symbol])
        if not tick_size:
            return None
        market_info_cache[symbol] = tick_size
        return tick_size
    except Exception:
        return None


def _volumes_window(ohlcv, candle_index):
    length = len(ohlcv)
    if length == 0:
        return []
    idx = candle_index if candle_index >= 0 else length + candle_index
    if idx < 0 or idx >= length:
        return []
    start = idx - 29
    end = idx - 9
    if start < 0:
        return []
    return [c[5] for c in ohlcv[start : end + 1]]


def analyze_bybit(symbol, candle_index=-1):
    try:
        tick_size = get_tick_size(symbol)
        if not tick_size:
            return False, None, None

        # 1. Tahta Analizi (Makas Kontrolu)
        orderbook = safe_fetch(exchange.fetch_order_book, symbol, limit=10)
        if not orderbook:
            return False, None, None
        bid = orderbook["bids"][0][0] if orderbook["bids"] else 0
        ask = orderbook["asks"][0][0] if orderbook["asks"] else 0
        if not bid or not ask or ask < bid:
            return False, None, None

        spread_ticks = round((ask - bid) / tick_size)

        # KRITER: 5 tick'ten fazla makas varsa iptal
        if spread_ticks > MAX_ALLOWED_TICKS:
            return False, None, None

        current_depth = sum(b[0] * b[1] for b in orderbook["bids"][:5]) + sum(
            a[0] * a[1] for a in orderbook["asks"][:5]
        )

        # 2. Mum Verileri (Stabilite Kontrolu)
        ohlcv = safe_fetch(exchange.fetch_ohlcv, symbol, timeframe="1m", limit=40)
        if not ohlcv or len(ohlcv) < 31:
            return False, None, None
        current = ohlcv[candle_index]
        candle_ts, open_p, close_p, vol = current[0], current[1], current[4], current[5]

        # Fiyat Degisimi (Body Change)
        body_change = abs(close_p - open_p) / (open_p if open_p > 0 else 1) * 100

        # 3. Hacim Analizi (5 Kat Sarti)
        volumes = _volumes_window(ohlcv, candle_index)
        if not volumes:
            return False, None, candle_ts
        normal_vol_m = statistics.median(volumes)
        vol_ratio = vol / (normal_vol_m if normal_vol_m > 0 else 1)

        # 4. Derinlik Takibi
        if symbol not in depth_memory:
            depth_memory[symbol] = []
        if len(depth_memory[symbol]) > 5:
            depth_median = statistics.median(depth_memory[symbol])
            if depth_median > 0:
                depth_ratio = current_depth / depth_median
            else:
                depth_ratio = 1.0
        else:
            depth_ratio = 1.0

        depth_memory[symbol].append(current_depth)
        if len(depth_memory[symbol]) > 20:
            depth_memory[symbol].pop(0)

        # --- KESIN KARAR ---
        # Hacim >= 5x VE Degisim <= %0.05 VE Makas <= 5 Tick
        if vol_ratio >= VOLUME_THRESHOLD and body_change <= BODY_STABILITY_PERCENT:
            return True, {
                "ticks": spread_ticks,
                "vol_ratio": vol_ratio,
                "price": close_p,
                "body_ch": body_change,
                "depth_ratio": depth_ratio,
                "is_wall": depth_ratio >= DEPTH_MULTIPLIER_TARGET,
                "candle_ts": candle_ts,
            }, candle_ts
        return False, None, candle_ts
    except Exception:
        return False, None, None


# ================= ANA DONGU =================


def should_send_alert(symbol, now):
    last_sent = alert_last_sent.get(symbol)
    if last_sent and now - last_sent < COOLDOWN_SECONDS:
        return False
    return True


def update_streak(symbol, now):
    if symbol not in alert_streaks:
        alert_streaks[symbol] = {"count": 1, "last_time": now}
    else:
        if now - alert_streaks[symbol]["last_time"] < STREAK_WINDOW_SECONDS:
            alert_streaks[symbol]["count"] += 1
        else:
            alert_streaks[symbol]["count"] = 1
        alert_streaks[symbol]["last_time"] = now
    return alert_streaks[symbol]["count"]


def get_watched_symbols():
    with watched_coins_lock:
        return list(watched_coins)


def update_pending_alert(symbol, candle_ts, data):
    pending = pending_alerts.get(symbol)
    if not pending or pending["candle_ts"] != candle_ts:
        pending_alerts[symbol] = {
            "candle_ts": candle_ts,
            "data": data,
        }
        return None

    pending["data"] = data
    return pending["data"]


def clear_pending_alert(symbol):
    pending_alerts.pop(symbol, None)


def scanner_loop():
    print("📡 5x Hacim & 5-Tick Radari Aktif...")
    while True:
        try:
            for symbol in get_watched_symbols():
                try:
                    detected, data, candle_ts = analyze_bybit(symbol)
                    if candle_ts is None:
                        time.sleep(SCAN_SLEEP_SECONDS)
                        continue

                    previous_ts = last_candle_ts.get(symbol)
                    if previous_ts and candle_ts != previous_ts:
                        pending = pending_alerts.get(symbol)
                        if pending and pending["candle_ts"] == previous_ts:
                            confirmed, confirmed_data, _ = analyze_bybit(
                                symbol, candle_index=-2
                            )
                            if confirmed and should_send_alert(symbol, time.time()):
                                now = time.time()
                                streak = update_streak(symbol, now)
                                exc = "!" * (streak - 1)
                                header = (
                                    f"{exc} GÜÇLÜ BOT TESPİTİ {exc}"
                                    if streak > 1
                                    else "🚨 HACİM PATLAMASI"
                                )
                                wall_line = (
                                    "🧱 *Duvar:* Evet"
                                    if confirmed_data["is_wall"]
                                    else "🧱 *Duvar:* Hayır"
                                )

                                msg = (
                                    f"*{header}* ({symbol})\n\n"
                                    f"📈 *Hacim Artışı:* {confirmed_data['vol_ratio']:.1f} KAT ✅\n"
                                    f"📊 *Değişim:* %{confirmed_data['body_ch']:.4f}\n"
                                    f"📏 *Makas:* {confirmed_data['ticks']} Tick\n"
                                    f"🌊 *Derinlik:* {confirmed_data['depth_ratio']:.1f} Kat\n"
                                    f"{wall_line}\n"
                                    f"💰 *Fiyat:* {confirmed_data['price']}"
                                )

                                try:
                                    bot.send_message(
                                        TELEGRAM_CHAT_ID, msg, parse_mode="Markdown"
                                    )
                                    alert_last_sent[symbol] = now
                                except Exception:
                                    pass
                        clear_pending_alert(symbol)

                    if detected:
                        update_pending_alert(symbol, data["candle_ts"], data)
                    else:
                        pending = pending_alerts.get(symbol)
                        if pending and pending["candle_ts"] == candle_ts:
                            clear_pending_alert(symbol)

                    last_candle_ts[symbol] = candle_ts
                    time.sleep(SCAN_SLEEP_SECONDS)
                except Exception:
                    time.sleep(2)
        except Exception:
            time.sleep(10)


# ================= TELEGRAM YONETIMI =================


@bot.message_handler(commands=["start", "yardim", "liste"])
def telegram_commands(message):
    try:
        cmd = message.text.split()[0]
        if "liste" in cmd:
            with watched_coins_lock:
                symbols = sorted(watched_coins)
            bot.reply_to(message, "🔍 Takipteki Coinler:\n" + "\n".join(symbols))
        else:
            bot.reply_to(
                message,
                "🤖 Bot Aktif!\nKriter: 5x Hacim, 5-Tick Makas, %0.05 Değişim.",
            )
    except Exception:
        pass


@bot.message_handler(commands=["ekle"])
def add_coin(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Kullanim: /ekle COIN (ornek: /ekle BTC)")
            return
        coin = parts[1].upper()
        if "/" not in coin:
            coin += "/USDT"
        with watched_coins_lock:
            watched_coins.add(coin)
        bot.reply_to(message, f"✅ {coin} eklendi.")
    except Exception:
        pass


@bot.message_handler(commands=["sil"])
def remove_coin(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Kullanim: /sil COIN (ornek: /sil BTC)")
            return
        coin = parts[1].upper()
        if "/" not in coin:
            coin += "/USDT"
        with watched_coins_lock:
            watched_coins.discard(coin)
        bot.reply_to(message, f"🗑️ {coin} çıkarıldı.")
    except Exception:
        pass


if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    print("🤖 Bot çalışıyor...")
    bot.infinity_polling()
