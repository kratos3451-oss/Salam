import statistics
import threading
import time

import ccxt
import telebot

# ================= AYARLAR (KATI FILTRE) =================
TELEGRAM_TOKEN = "7043903963:AAF4Y5wgayT_PwRYVX4yM91TXETlFSYoffo"
TELEGRAM_CHAT_ID = "5448895488"

# --- STRATEJI AYARLARI ---
VOLUME_THRESHOLD = 5.0        # Hacim en az 5 KAT artmis olmali
BODY_STABILITY_PERCENT = 0.05  # Degisim kesinlikle %0.05 ve alti olmali
MAX_ALLOWED_TICKS = 5         # Makas 5 tick'ten fazlaysa ASLA bildirme
DEPTH_MULTIPLIER_TARGET = 3.0  # Tahta 3 kat dolmussa "DUVAR" ibaresi ekle

# --- OZEL TAKIP LISTESI ---
watched_coins = {
    "FIGHT/USDT",
    "ELSA/USDT",
    "SENT/USDT",
    "IMU/USDT",
    "BIRB/USDT",
}

depth_memory = {}
market_info_cache = {}
alert_streaks = {}

exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# ================= ANALIZ MOTORU =================


def get_tick_size(symbol):
    if symbol in market_info_cache:
        return market_info_cache[symbol]
    try:
        markets = exchange.load_markets()
        precision = markets[symbol]["precision"]["price"]
        if precision is None:
            return None
        # CCXT precision is typically decimals; convert to tick size.
        precision_value = float(precision)
        if precision_value.is_integer():
            tick_size = 10 ** (-int(precision_value))
        else:
            tick_size = precision_value
        market_info_cache[symbol] = tick_size
        return tick_size
    except Exception:
        return None


def analyze_bybit(symbol):
    try:
        tick_size = get_tick_size(symbol)
        if not tick_size:
            return False, None

        # 1. Tahta Analizi (Makas Kontrolu)
        orderbook = exchange.fetch_order_book(symbol, limit=10)
        bid = orderbook["bids"][0][0] if orderbook["bids"] else 0
        ask = orderbook["asks"][0][0] if orderbook["asks"] else 0
        if not bid or not ask:
            return False, None

        spread_ticks = round((ask - bid) / tick_size)

        # KRITER: 5 tick'ten fazla makas varsa iptal
        if spread_ticks > MAX_ALLOWED_TICKS:
            return False, None

        current_depth = sum([b[0] * b[1] for b in orderbook["bids"][:5]]) + sum(
            [a[0] * a[1] for a in orderbook["asks"][:5]]
        )

        # 2. Mum Verileri (Stabilite Kontrolu)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1m", limit=40)
        current = ohlcv[-1]
        open_p, close_p, vol = current[1], current[4], current[5]

        # Fiyat Degisimi (Body Change)
        body_change = abs(close_p - open_p) / (open_p if open_p > 0 else 1) * 100

        # 3. Hacim Analizi (5 Kat Sarti)
        volumes = [c[5] for c in ohlcv[-30:-10]]
        normal_vol_m = statistics.median(volumes)
        vol_ratio = vol / (normal_vol_m if normal_vol_m > 0 else 1)

        # 4. Derinlik Takibi
        if symbol not in depth_memory:
            depth_memory[symbol] = []
        if len(depth_memory[symbol]) > 5:
            depth_ratio = current_depth / statistics.median(depth_memory[symbol])
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
            }
        return False, None
    except Exception:
        return False, None


# ================= ANA DONGU =================


def scanner_loop():
    print("📡 5x Hacim & 5-Tick Radari Aktif...")
    while True:
        try:
            for symbol in list(watched_coins):
                try:
                    detected, data = analyze_bybit(symbol)
                    if detected:
                        now = time.time()
                        # Seri (Streak) Takibi
                        if symbol not in alert_streaks:
                            alert_streaks[symbol] = {"count": 1, "last_time": now}
                        else:
                            if now - alert_streaks[symbol]["last_time"] < 130:
                                alert_streaks[symbol]["count"] += 1
                            else:
                                alert_streaks[symbol]["count"] = 1
                            alert_streaks[symbol]["last_time"] = now

                        streak = alert_streaks[symbol]["count"]
                        exc = "!" * (streak - 1)
                        header = (
                            f"{exc} GÜÇLÜ BOT TESPİTİ {exc}"
                            if streak > 1
                            else "🚨 HACİM PATLAMASI"
                        )

                        msg = (
                            f"*{header}* ({symbol})\n\n"
                            f"📈 *Hacim Artışı:* {data['vol_ratio']:.1f} KAT ✅\n"
                            f"📊 *Değişim:* %{data['body_ch']:.4f}\n"
                            f"📏 *Makas:* {data['ticks']} Tick\n"
                            f"🌊 *Derinlik:* {data['depth_ratio']:.1f} Kat\n"
                            f"💰 *Fiyat:* {data['price']}"
                        )

                        try:
                            bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
                        except Exception:
                            pass
                        time.sleep(50)
                    time.sleep(1.2)
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
            bot.reply_to(
                message,
                "🔍 Takipteki Coinler:\n" + "\n".join(watched_coins),
            )
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
        coin = message.text.split()[1].upper()
        if "/" not in coin:
            coin += "/USDT"
        watched_coins.add(coin)
        bot.reply_to(message, f"✅ {coin} eklendi.")
    except Exception:
        pass


@bot.message_handler(commands=["sil"])
def remove_coin(message):
    try:
        coin = message.text.split()[1].upper()
        if "/" not in coin:
            coin += "/USDT"
        watched_coins.discard(coin)
        bot.reply_to(message, f"🗑️ {coin} çıkarıldı.")
    except Exception:
        pass


if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()
    print("🤖 Bot çalışıyor...")
    bot.infinity_polling()
