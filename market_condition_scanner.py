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
DEPTH_MULTIPLIER_TARGET = 3.0  # Tahta 3 kat dolmussa "DUVAR" ibaresi ekle
ORDERBOOK_LIMIT = 10
COOLDOWN_SECONDS = 60
STREAK_WINDOW_SECONDS = 130
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
bid_depth_memory = {}
ask_depth_memory = {}
candle_stats = {}
alert_streaks = {}
alert_last_sent = {}

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


def _ratio_to_median(value, memory_list):
    if len(memory_list) > 5:
        median_value = statistics.median(memory_list)
        if median_value > 0:
            return value / median_value
    return 1.0


def analyze_bybit(symbol, candle_index=-1):
    try:
        # 1. Tahta Analizi
        orderbook = safe_fetch(exchange.fetch_order_book, symbol, limit=ORDERBOOK_LIMIT)
        if not orderbook:
            return None
        bid = orderbook["bids"][0][0] if orderbook["bids"] else 0
        ask = orderbook["asks"][0][0] if orderbook["asks"] else 0
        if not bid or not ask or ask < bid:
            return None

        bid_depth = sum(b[0] * b[1] for b in orderbook["bids"][:5])
        ask_depth = sum(a[0] * a[1] for a in orderbook["asks"][:5])
        current_depth = bid_depth + ask_depth

        if symbol not in depth_memory:
            depth_memory[symbol] = []
        if symbol not in bid_depth_memory:
            bid_depth_memory[symbol] = []
        if symbol not in ask_depth_memory:
            ask_depth_memory[symbol] = []

        depth_ratio = _ratio_to_median(current_depth, depth_memory[symbol])
        bid_depth_ratio = _ratio_to_median(bid_depth, bid_depth_memory[symbol])
        ask_depth_ratio = _ratio_to_median(ask_depth, ask_depth_memory[symbol])

        depth_memory[symbol].append(current_depth)
        if len(depth_memory[symbol]) > 20:
            depth_memory[symbol].pop(0)

        bid_depth_memory[symbol].append(bid_depth)
        if len(bid_depth_memory[symbol]) > 20:
            bid_depth_memory[symbol].pop(0)

        ask_depth_memory[symbol].append(ask_depth)
        if len(ask_depth_memory[symbol]) > 20:
            ask_depth_memory[symbol].pop(0)

        # 2. Mum Verileri (Stabilite Kontrolu)
        ohlcv = safe_fetch(exchange.fetch_ohlcv, symbol, timeframe="1m", limit=40)
        if not ohlcv or len(ohlcv) < 31:
            return None
        current = ohlcv[candle_index]
        candle_ts, open_p, close_p, vol = current[0], current[1], current[4], current[5]

        # Fiyat Degisimi (Body Change)
        body_change = abs(close_p - open_p) / (open_p if open_p > 0 else 1) * 100

        # 3. Hacim Analizi (5 Kat Sarti)
        volumes = _volumes_window(ohlcv, candle_index)
        if not volumes:
            return None
        normal_vol_m = statistics.median(volumes)
        vol_ratio = vol / (normal_vol_m if normal_vol_m > 0 else 1)

        return {
            "candle_ts": candle_ts,
            "vol_ratio": vol_ratio,
            "price": close_p,
            "body_ch": body_change,
            "depth_ratio": depth_ratio,
            "bid_depth_ratio": bid_depth_ratio,
            "ask_depth_ratio": ask_depth_ratio,
        }
    except Exception:
        return None


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


def _init_candle_stats(data):
    return {
        "candle_ts": data["candle_ts"],
        "count": 1,
        "sum_vol_ratio": data["vol_ratio"],
        "sum_body_ch": data["body_ch"],
        "sum_depth_ratio": data["depth_ratio"],
        "sum_bid_depth_ratio": data["bid_depth_ratio"],
        "sum_ask_depth_ratio": data["ask_depth_ratio"],
        "sum_price": data["price"],
    }


def _update_candle_stats(stats, data):
    stats["count"] += 1
    stats["sum_vol_ratio"] += data["vol_ratio"]
    stats["sum_body_ch"] += data["body_ch"]
    stats["sum_depth_ratio"] += data["depth_ratio"]
    stats["sum_bid_depth_ratio"] += data["bid_depth_ratio"]
    stats["sum_ask_depth_ratio"] += data["ask_depth_ratio"]
    stats["sum_price"] += data["price"]


def _average_candle_stats(stats):
    count = max(stats["count"], 1)
    return {
        "candle_ts": stats["candle_ts"],
        "avg_vol_ratio": stats["sum_vol_ratio"] / count,
        "avg_body_ch": stats["sum_body_ch"] / count,
        "avg_depth_ratio": stats["sum_depth_ratio"] / count,
        "avg_bid_depth_ratio": stats["sum_bid_depth_ratio"] / count,
        "avg_ask_depth_ratio": stats["sum_ask_depth_ratio"] / count,
        "avg_price": stats["sum_price"] / count,
    }


def _finalize_candle(symbol, stats):
    averages = _average_candle_stats(stats)
    base_ok = (
        averages["avg_vol_ratio"] >= VOLUME_THRESHOLD
        and averages["avg_body_ch"] <= BODY_STABILITY_PERCENT
    )
    if not base_ok:
        return

    orderbook_ok = (
        averages["avg_bid_depth_ratio"] >= DEPTH_MULTIPLIER_TARGET
        and averages["avg_ask_depth_ratio"] >= DEPTH_MULTIPLIER_TARGET
    )
    if not should_send_alert(symbol, time.time()):
        return

    now = time.time()
    streak = update_streak(symbol, now)
    base_header = "✅ KESIN BOT" if orderbook_ok else "⚠️ BOT OLABILIR"
    if streak > 1:
        exc = "!" * (streak - 1)
        header = f"{exc} {base_header} {exc}"
    else:
        header = base_header

    wall_line = (
        "🧱 *Duvar:* Evet"
        if averages["avg_depth_ratio"] >= DEPTH_MULTIPLIER_TARGET
        else "🧱 *Duvar:* Hayır"
    )
    depth_side_line = (
        "📚 *Emir Miktarı B/A (Ort):* "
        f"{averages['avg_bid_depth_ratio']:.1f}x/"
        f"{averages['avg_ask_depth_ratio']:.1f}x"
    )

    msg = (
        f"*{header}* ({symbol})\n\n"
        f"📈 *Hacim Artışı (Ort):* {averages['avg_vol_ratio']:.1f} KAT ✅\n"
        f"📊 *Değişim (Ort):* %{averages['avg_body_ch']:.4f}\n"
        f"{depth_side_line}\n"
        f"🌊 *Toplam Derinlik (Ort):* {averages['avg_depth_ratio']:.1f} Kat\n"
        f"{wall_line}\n"
        f"💰 *Fiyat (Ort):* {averages['avg_price']}"
    )

    try:
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
        alert_last_sent[symbol] = now
    except Exception:
        pass


def scanner_loop():
    print("📡 5x Hacim Radari Aktif...")
    while True:
        try:
            for symbol in get_watched_symbols():
                try:
                    data = analyze_bybit(symbol)
                    if not data:
                        time.sleep(SCAN_SLEEP_SECONDS)
                        continue

                    stats = candle_stats.get(symbol)
                    if not stats or stats["candle_ts"] != data["candle_ts"]:
                        if stats:
                            _finalize_candle(symbol, stats)
                        candle_stats[symbol] = _init_candle_stats(data)
                    else:
                        _update_candle_stats(stats, data)

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
                "🤖 Bot Aktif!\nKriter: 5x Hacim, %0.05 Değişim, B/A emir 3x.",
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
