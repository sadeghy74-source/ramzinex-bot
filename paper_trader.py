# ===== کتابخانه‌ها =====
import os
import json
import time
import base64
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# ===== خواندن کلیدها =====
PUBLIC_KEY  = os.getenv("NOBITEX_PUBLIC_KEY")
PRIVATE_KEY = os.getenv("NOBITEX_PRIVATE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==================================================
# ارزهای تحت پوشش
# ==================================================
COINS = {
    "DOGE": {
        "symbol":       "DOGEIRT",
        "donchian_in":  30,
        "donchian_out": 15,
        "trailing_atr": 4.0,
        "adx_min":      35,
        "vol_mult":     1.5,
    },
    "ADA": {
        "symbol":       "ADAIRT",
        "donchian_in":  20,
        "donchian_out": 10,
        "trailing_atr": 3.0,
        "adx_min":      35,
        "vol_mult":     1.5,
    },
    "XRP": {
        "symbol":       "XRPIRT",
        "donchian_in":  30,
        "donchian_out": 15,
        "trailing_atr": 4.0,
        "adx_min":      30,
        "vol_mult":     1.0,
    },
}

# ==================================================
# تنظیمات عمومی
# ==================================================
FEE             = 0.005
SLIPPAGE        = 0.001
INITIAL_CAPITAL = 20_000_000
POSITION_PCT    = 1.0

MAX_ORDER_RLS       = 100_000
PENDING_TIMEOUT_MIN = 30
MESSAGE_MAX_AGE_SEC = 900   # فقط پیام‌های ۱۵ دقیقه اخیر پردازش بشن

STATE_FILE  = "paper_state.json"
TRADES_FILE = "paper_trades.csv"


# ==================================================
# زمان ایران
# ==================================================
def now_iran():
    iran_tz = timezone(timedelta(hours=3, minutes=30))
    return datetime.now(iran_tz)


# ==================================================
# تلگرام
# ==================================================
def send_telegram(message: str, parse_mode: str = "HTML"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return data["result"]["message_id"]
        else:
            print(f"Telegram error: {r.status_code} {data}")
    except Exception as e:
        print(f"Telegram exception: {e}")
    return None


def get_updates(offset=0):
    if not TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 0, "offset": offset}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception:
        pass
    return []


# ==================================================
# نوبیتکس
# ==================================================
def pad_b64(s):
    return s + "=" * (-len(s) % 4)


def _sign_and_send(method, path, body):
    from nacl.signing import SigningKey
    private_bytes = base64.urlsafe_b64decode(pad_b64(PRIVATE_KEY))
    signing_key   = SigningKey(private_bytes)
    body_json = json.dumps(body, separators=(',', ':'))
    timestamp = str(int(time.time()))
    message   = f"{timestamp}{method}{path}{body_json}"
    signature = signing_key.sign(message.encode('utf-8'))
    sig_b64   = base64.urlsafe_b64encode(signature.signature).decode('utf-8')
    headers = {
        "Nobitex-Key": pad_b64(PUBLIC_KEY),
        "Nobitex-Signature": sig_b64,
        "Nobitex-Timestamp": timestamp,
        "Content-Type": "application/json",
        "User-Agent": "TraderBot/1.0",
    }
    url = "https://apiv2.nobitex.ir" + path
    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, data=body_json, headers=headers, timeout=15)
    return r.status_code, r.json()


def get_wallets():
    try:
        code, data = _sign_and_send("POST", "/users/wallets/list", {"type": "spot"})
        if code == 200 and data.get("status") == "ok":
            wallets = {}
            for w in data.get("wallets", []):
                wallets[w["currency"]] = float(w["balance"])
            return wallets
    except Exception as e:
        print(f"get_wallets error: {e}")
    return {}


def place_market_buy(symbol, base_currency, amount_rls):
    body = {
        "type": "buy",
        "execution": "market",
        "srcCurrency": "rls",
        "dstCurrency": base_currency.lower(),
        "amount": str(int(amount_rls)),
        "price": "0",
    }
    return _sign_and_send("POST", "/market/orders/add", body)


def place_market_sell(symbol, base_currency, amount_base):
    body = {
        "type": "sell",
        "execution": "market",
        "srcCurrency": base_currency.lower(),
        "dstCurrency": "rls",
        "amount": str(float(amount_base)),
        "price": "0",
    }
    return _sign_and_send("POST", "/market/orders/add", body)


# ==================================================
# کمکی
# ==================================================
def fmt_price(p):
    p = float(p)
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1:    return f"{p:,.4f}"
    return f"{p:.6f}"


def progress_bar(pct, length=10):
    filled = int(round(pct / 100 * length))
    filled = max(0, min(length, filled))
    return "▰" * filled + "▱" * (length - filled)


def status_emoji(pct):
    if pct >= 100: return "✅"
    if pct >= 75:  return "🟢"
    if pct >= 50:  return "🟡"
    if pct >= 25:  return "🟠"
    return "🔴"


# ==================================================
# State
# ==================================================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            # ===== اطمینان از وجود ساختار coins =====
            if "coins" not in state:
                raise ValueError("Old state format")
            # ===== اطمینان از وجود همه ارزها =====
            for coin in COINS:
                if coin not in state["coins"]:
                    state["coins"][coin] = _new_coin_state()
            return state
        except Exception:
            pass

    # ===== ساخت state جدید =====
    state = {"last_update_id": 0, "coins": {}}
    for coin in COINS:
        state["coins"][coin] = _new_coin_state()
    return state


def _new_coin_state():
    return {
        "in_position": False, "entry_price": 0, "entry_time": None,
        "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
        "capital": INITIAL_CAPITAL, "initial_capital": INITIAL_CAPITAL,
        "position_size": 0,
        "last_candle_time": None,
        "pending": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def log_trade(trade):
    df = pd.DataFrame([trade])
    if os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE, mode="a", header=False, index=False)
    else:
        df.to_csv(TRADES_FILE, index=False)


# ==================================================
# داده
# ==================================================
_cache = {}


def fetch_candles(symbol, limit=500, cache_seconds=60):
    now_ts = time.time()
    if symbol in _cache:
        if (now_ts - _cache[symbol]["time"]) < cache_seconds:
            return _cache[symbol]["df"]

    url = "https://apiv2.nobitex.ir/market/udf/history"
    now = int(time.time())
    params = {"symbol": symbol, "resolution": "60", "to": now, "countback": limit}

    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
            if data.get("s") == "ok":
                df = pd.DataFrame({
                    "time":   pd.to_datetime(data["t"], unit="s"),
                    "open":   data["o"], "high": data["h"],
                    "low":    data["l"], "close": data["c"],
                    "volume": data["v"],
                })
                _cache[symbol] = {"time": now_ts, "df": df}
                return df
        except Exception:
            time.sleep(1)
    return _cache.get(symbol, {}).get("df")


def drop_unclosed_candle(df):
    if len(df) == 0:
        return df
    now = pd.Timestamp.now()
    last_time = df["time"].iloc[-1]
    if (now - last_time).total_seconds() < 3600:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def calculate_indicators(df, donchian_in, donchian_out):
    df = df.copy()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    up  = df["high"] - df["high"].shift()
    dn  = df["low"].shift() - df["low"]
    pdm = up.where((up > dn) & (up > 0), 0)
    mdm = dn.where((dn > up) & (dn > 0), 0)
    atr_ = tr.ewm(span=14, adjust=False).mean()
    pdi  = 100 * (pdm.ewm(span=14, adjust=False).mean() / atr_)
    mdi  = 100 * (mdm.ewm(span=14, adjust=False).mean() / atr_)
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()

    df["vol_avg"]       = df["volume"].rolling(20).mean()
    df["donchian_high"] = df["high"].rolling(donchian_in).max().shift(1)
    df["donchian_low"]  = df["low"].rolling(donchian_out).min().shift(1)

    return df.dropna().reset_index(drop=True)


# ==================================================
# منطق
# ==================================================
def update_trailing_stop(state_coin, latest, trailing_atr):
    if not state_coin["in_position"]:
        return state_coin
    if latest["high"] > state_coin["highest_price"]:
        state_coin["highest_price"] = latest["high"]
    new_sl = state_coin["highest_price"] - (trailing_atr * state_coin["entry_atr"])
    if new_sl > state_coin["stop_loss"]:
        state_coin["stop_loss"] = new_sl
    return state_coin


def check_entry(latest, adx_min, vol_mult):
    return (
        latest["close"] > latest["donchian_high"] and
        latest["adx"] > adx_min and
        latest["volume"] > (vol_mult * latest["vol_avg"])
    )


def check_exit(state_coin, latest):
    if not state_coin["in_position"]:
        return None
    if latest["low"] <= state_coin["stop_loss"]:
        return "STOP_LOSS"
    if latest["close"] < latest["donchian_low"]:
        return "BREAKDOWN"
    return None


# ==================================================
# پردازش دستورات
# ==================================================
def process_commands(state):
    offset = state.get("last_update_id", 0) + 1
    updates = get_updates(offset=offset)
    if not updates:
        return state

    for upd in updates:
        state["last_update_id"] = upd["update_id"]

        if "message" not in upd:
            continue

        msg = upd["message"]
        text = msg.get("text", "").strip()

        if not text:
            continue

        # ===== فیلتر پیام‌های قدیمی =====
        msg_date = msg.get("date", 0)
        age_sec = time.time() - msg_date
        if age_sec > MESSAGE_MAX_AGE_SEC:
            print(f"Skipping old message: {text} (age: {int(age_sec/60)} min)")
            continue

        print(f"Received command: {text}")
        parts = text.split()

        # ==================================================
        # /buy SYMBOL
        # ==================================================
        if parts[0] == "/buy":
            if len(parts) < 2:
                send_telegram(
                    "❓ <b>ارز رو مشخص کن</b>\n\n"
                    "مثال: <code>/buy DOGE</code>\n"
                    "ارزهای موجود: " + ", ".join(COINS.keys())
                )
                continue

            coin = parts[1].upper()
            if coin not in COINS:
                send_telegram(f"❌ ارز <code>{coin}</code> پشتیبانی نمی‌شه")
                continue

            state_coin = state["coins"][coin]
            pending = state_coin.get("pending")

            if not pending or pending.get("action") != "BUY":
                send_telegram(f"❌ <b>سیگنال خرید فعالی برای {coin} نیست</b>")
                continue

            pending_time = datetime.fromisoformat(pending["time"])
            age_min = (now_iran().replace(tzinfo=None) - pending_time.replace(tzinfo=None)).total_seconds() / 60
            if age_min > PENDING_TIMEOUT_MIN:
                send_telegram(f"❌ <b>سیگنال {coin} منقضی شده</b>")
                state_coin["pending"] = None
                continue

            amount_rls = pending["amount_rls"]
            send_telegram(f"⏳ در حال ارسال سفارش خرید {coin} — {amount_rls:,} ریال...")
            print(f"Sending BUY {coin}: {amount_rls} RLS")

            try:
                code, res = place_market_buy(COINS[coin]["symbol"], coin, amount_rls)
            except Exception as e:
                code, res = 0, {"error": str(e)}

            print(f"Order response: {code} | {res}")

            if code == 200 and res.get("status") == "ok":
                order = res.get("order", {})
                order_id = order.get("id", "?")
                filled_price = order.get("price", pending["price"])

                state_coin["in_position"]   = True
                state_coin["entry_price"]   = float(filled_price) if filled_price else pending["price"]
                state_coin["entry_time"]    = str(pending["time"])
                state_coin["entry_atr"]     = pending["atr"]
                state_coin["highest_price"] = pending["price"]
                state_coin["stop_loss"]     = pending["price"] - (COINS[coin]["trailing_atr"] * pending["atr"])
                state_coin["position_size"] = pending["size"]
                state_coin["pending"]       = None
                save_state(state)

                send_telegram(
                    f"✅ <b>خرید {coin} انجام شد</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🆔 {order_id}\n"
                    f"💰 {fmt_price(filled_price)}\n"
                    f"📦 {pending['size']:.4f} {coin}\n"
                    f"🛡️ SL: {fmt_price(state_coin['stop_loss'])}"
                )
            else:
                try:
                    err_json = json.dumps(res, ensure_ascii=False, indent=2)
                except Exception:
                    err_json = str(res)

                send_telegram(
                    f"❌ <b>سفارش {coin} رد شد</b>\n"
                    f"Status: <code>{code}</code>\n"
                    f"<code>{err_json[:900]}</code>"
                )
                state_coin["pending"] = None
                save_state(state)

        # ==================================================
        # /sell SYMBOL
        # ==================================================
        elif parts[0] == "/sell":
            if len(parts) < 2:
                send_telegram(
                    "❓ <b>ارز رو مشخص کن</b>\n\n"
                    "مثال: <code>/sell DOGE</code>"
                )
                continue

            coin = parts[1].upper()
            if coin not in COINS:
                send_telegram(f"❌ ارز <code>{coin}</code> پشتیبانی نمی‌شه")
                continue

            state_coin = state["coins"][coin]

            if not state_coin["in_position"]:
                send_telegram(f"❌ <b>در {coin} معامله‌ای نداری</b>")
                continue

            send_telegram(f"⏳ در حال فروش {coin}...")

            wallets = get_wallets()
            balance = wallets.get(coin.lower(), 0)
            if balance <= 0:
                send_telegram(f"❌ <b>موجودی {coin} صفره</b>")
                continue

            try:
                code, res = place_market_sell(COINS[coin]["symbol"], coin, balance)
            except Exception as e:
                code, res = 0, {"error": str(e)}

            if code == 200 and res.get("status") == "ok":
                order = res.get("order", {})
                order_id = order.get("id", "?")
                filled_price = float(order.get("price", 0))

                entry_value = state_coin["entry_price"] * state_coin["position_size"]
                exit_value  = filled_price * balance
                gross_pnl   = exit_value - entry_value
                fee_amount  = (entry_value + exit_value) * (FEE / 2)
                net_pnl     = gross_pnl - fee_amount
                profit_pct  = (net_pnl / entry_value * 100) if entry_value > 0 else 0

                state_coin["capital"] = state_coin.get("capital", INITIAL_CAPITAL) + net_pnl
                log_trade({
                    "coin":        coin,
                    "entry_time":  state_coin.get("entry_time"),
                    "exit_time":   str(now_iran()),
                    "entry":       round(state_coin["entry_price"], 6),
                    "exit":        round(filled_price, 6),
                    "size":        round(balance, 6),
                    "profit_rial": round(net_pnl, 2),
                    "profit_%":    round(profit_pct, 3),
                    "capital":     round(state_coin["capital"], 2),
                    "reason":      "MANUAL_SELL",
                })

                state_coin.update({
                    "in_position": False, "entry_price": 0, "entry_time": None,
                    "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
                    "position_size": 0, "pending": None,
                })
                save_state(state)

                color = "🟢" if net_pnl > 0 else "🔴"
                send_telegram(
                    f"{color} <b>فروش {coin} انجام شد</b>\n"
                    f"💰 {fmt_price(filled_price)}\n"
                    f"📊 {net_pnl:+,.0f} ({profit_pct:+.2f}%)"
                )
            else:
                try:
                    err_json = json.dumps(res, ensure_ascii=False, indent=2)
                except Exception:
                    err_json = str(res)

                send_telegram(
                    f"❌ <b>سفارش فروش {coin} رد شد</b>\n"
                    f"Status: <code>{code}</code>\n"
                    f"<code>{err_json[:900]}</code>"
                )
                state_coin["pending"] = None
                save_state(state)

        # ==================================================
        # /cancel
        # ==================================================
        elif parts[0] == "/cancel":
            if len(parts) >= 2:
                coin = parts[1].upper()
                if coin in COINS:
                    state["coins"][coin]["pending"] = None
                    save_state(state)
                    send_telegram(f"❌ <b>لغو {coin} انجام شد</b>")
                else:
                    send_telegram(f"❌ ارز <code>{coin}</code> پشتیبانی نمی‌شه")
            else:
                for coin in COINS:
                    state["coins"][coin]["pending"] = None
                save_state(state)
                send_telegram("❌ <b>همه لغو شدند</b>")

        # ==================================================
        # /status
        # ==================================================
        elif text == "/status":
            wallets = get_wallets()
            rls = wallets.get("rls", 0)

            msg = f"📊 <b>وضعیت</b>\n"
            msg += f"━━━━━━━━━━━━━━━\n"
            msg += f"💼 RLS: {rls:,.0f}\n\n"

            for coin in COINS:
                sc = state["coins"][coin]
                balance = wallets.get(coin.lower(), 0)
                if sc["in_position"]:
                    msg += f"🟢 <b>{coin}</b>: در معامله\n"
                    msg += f"   💰 {fmt_price(sc['entry_price'])} | 📦 {sc['position_size']:.4f}\n"
                    msg += f"   🛡️ SL: {fmt_price(sc['stop_loss'])}\n"
                else:
                    msg += f"⚪ <b>{coin}</b>: خارج (موجودی: {balance:.4f})\n"

            send_telegram(msg)

        # ==================================================
        # /help
        # ==================================================
        elif text in ("/start", "/help"):
            coins_list = ", ".join(COINS.keys())
            send_telegram(
                f"🤖 <b>دستورات</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"<b>ارزهای موجود:</b> {coins_list}\n\n"
                f"<code>/buy DOGE</code> — خرید DOGE\n"
                f"<code>/sell ADA</code> — فروش ADA\n"
                f"<code>/cancel XRP</code> — لغو XRP\n"
                f"<code>/status</code> — وضعیت همه\n"
                f"<code>/help</code> — راهنما"
            )

        else:
            send_telegram(f"❓ دستور ناشناخته: <code>{text[:50]}</code>")

    save_state(state)
    return state


# ==================================================
# پیام وضعیت
# ==================================================
def build_status_message(coin, latest, now_str, state_coin):
    params = COINS[coin]
    price     = latest["close"]
    donchian  = latest["donchian_high"]
    adx       = latest["adx"]
    volume    = latest["volume"]
    vol_avg   = latest["vol_avg"]
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    price_pct = min(100, (price / donchian * 100)) if donchian > 0 else 0
    adx_pct   = min(100, (adx / params["adx_min"] * 100))
    vol_pct   = min(100, (vol_ratio / params["vol_mult"] * 100))
    total_pct = (price_pct + adx_pct + vol_pct) / 3

    buy_ok = check_entry(latest, params["adx_min"], params["vol_mult"]) and not state_coin["in_position"]

    msg = (
        f"📊 <b>بررسی {coin}</b>\n"
        f"🕐 {now_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 قیمت: <b>{fmt_price(price)}</b> ریال\n\n"
        f"<b>شرایط ورود:</b>\n"
        f"{status_emoji(price_pct)} قیمت vs سقف: <b>{price_pct:.1f}%</b>\n"
        f"   <code>{progress_bar(price_pct)}</code>\n"
        f"   هدف: {fmt_price(donchian)}\n\n"
        f"{status_emoji(adx_pct)} ADX: <b>{adx:.1f}</b> / {params['adx_min']} (<b>{adx_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(adx_pct)}</code>\n\n"
        f"{status_emoji(vol_pct)} حجم: <b>{vol_ratio:.2f}x</b> / {params['vol_mult']}x (<b>{vol_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(vol_pct)}</code>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 <b>آمادگی: {total_pct:.1f}%</b>\n"
    )

    if state_coin["in_position"]:
        msg += (
            f"\n🟢 <b>در معامله</b>\n"
            f"💰 {fmt_price(state_coin['entry_price'])}\n"
            f"🛡️ SL: {fmt_price(state_coin['stop_loss'])}\n"
            f"\n🔴 برای فروش: <code>/sell {coin}</code>"
        )
    elif buy_ok:
        msg += "\n\n🟢 <b>سیگنال خرید آماده!</b>"
        msg += f"\n📩 بفرست: <code>/buy {coin}</code>"
    elif total_pct >= 90:
        msg += "\n⚡ <b>نزدیک سیگنال!</b>"
    elif total_pct >= 70:
        msg += "\n🔥 در حال نزدیک شدن..."
    elif total_pct >= 40:
        msg += "\n⏳ در حال شکل‌گیری..."
    else:
        msg += "\n😴 بازار آرام"

    return msg, buy_ok


# ==================================================
# اجرا
# ==================================================
def run_once():
    now_str = now_iran().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str}] Checking all coins...")

    state = load_state()

    # ===== ۱. پردازش هر ارز =====
    for coin in COINS:
        params = COINS[coin]
        state_coin = state["coins"][coin]

        print(f"\n  [{coin}]")
        df_raw = fetch_candles(params["symbol"], limit=500)
        if df_raw is None or len(df_raw) < 100:
            print(f"    No data")
            continue

        df = calculate_indicators(drop_unclosed_candle(df_raw),
                                  params["donchian_in"], params["donchian_out"])
        if len(df) == 0:
            continue
        latest = df.iloc[-1]

        # Trailing
        update_trailing_stop(state_coin, latest, params["trailing_atr"])
        save_state(state)

        # پیام
        msg, buy_ok = build_status_message(coin, latest, now_str, state_coin)

        # pending خرید
        if buy_ok and not state_coin["in_position"]:
            entry_price = latest["close"] * (1 + SLIPPAGE)
            trade_value = state_coin["capital"] * POSITION_PCT
            position_size = (trade_value * (1 - FEE / 2)) / entry_price
            amount_rls = min(trade_value, MAX_ORDER_RLS)

            state_coin["pending"] = {
                "action":      "BUY",
                "price":       entry_price,
                "size":        position_size,
                "amount_rls":  amount_rls,
                "atr":         latest["atr"],
                "time":        now_iran().isoformat(),
            }
            save_state(state)
            print(f"    >>> BUY PENDING @ {fmt_price(entry_price)}")

        # pending فروش
        elif state_coin["in_position"]:
            exit_price = latest["close"] * (1 - SLIPPAGE)
            state_coin["pending"] = {
                "action":      "SELL",
                "price":       exit_price,
                "time":        now_iran().isoformat(),
            }
            save_state(state)
            print(f"    <<< SELL PENDING @ {fmt_price(exit_price)}")

        # ارسال پیام
        send_telegram(msg)
        print(f"    Status sent")

    # ===== ۲. پردازش دستورات =====
    try:
        state = process_commands(state)
    except Exception as e:
        print(f"Command error: {e}")


# ==================================================
# اجرا
# ==================================================
if __name__ == "__main__":
    run_once()