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
# ارزها
# ==================================================
COINS = {
    "DOGE": {"symbol": "DOGEIRT", "donchian_in": 30, "donchian_out": 15, "trailing_atr": 4.0, "adx_min": 35, "vol_mult": 1.5},
    "ADA":  {"symbol": "ADAIRT",  "donchian_in": 20, "donchian_out": 10, "trailing_atr": 3.0, "adx_min": 35, "vol_mult": 1.5},
    "XRP":  {"symbol": "XRPIRT",  "donchian_in": 30, "donchian_out": 15, "trailing_atr": 4.0, "adx_min": 30, "vol_mult": 1.0},
}

# ==================================================
# تنظیمات
# ==================================================
FEE             = 0.005
SLIPPAGE        = 0.001
INITIAL_CAPITAL = 20_000_000
POSITION_PCT    = 1.0

MAX_ORDER_RLS       = 100_000
PENDING_TIMEOUT_MIN = 30

STATE_FILE  = "paper_state.json"
TRADES_FILE = "paper_trades.csv"


def now_iran():
    return datetime.now(timezone(timedelta(hours=3, minutes=30)))


# ==================================================
# تلگرام
# ==================================================
def send_telegram(message, parse_mode="HTML", reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message,
               "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return data["result"]["message_id"]
    except Exception:
        pass
    return None


def edit_telegram(message_id, new_text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id,
               "text": new_text, "parse_mode": "HTML"}
    payload["reply_markup"] = reply_markup if reply_markup else {"inline_keyboard": []}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def answer_callback(callback_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_id, "text": text}, timeout=10)
    except Exception:
        pass


def get_updates(offset=0):
    if not TELEGRAM_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"timeout": 0, "offset": offset}, timeout=15)
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
    signing_key = SigningKey(private_bytes)
    body_json = json.dumps(body, separators=(',', ':'))
    timestamp = str(int(time.time()))
    message = f"{timestamp}{method}{path}{body_json}"
    signature = signing_key.sign(message.encode('utf-8'))
    sig_b64 = base64.urlsafe_b64encode(signature.signature).decode('utf-8')
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
            return {w["currency"]: float(w["balance"]) for w in data.get("wallets", [])}
    except Exception:
        pass
    return {}


def place_market_buy(base_currency, amount_rls):
    body = {"type": "buy", "execution": "market",
            "srcCurrency": "rls", "dstCurrency": base_currency.lower(),
            "amount": str(int(amount_rls)), "price": "0"}
    return _sign_and_send("POST", "/market/orders/add", body)


def place_market_sell(base_currency, amount_base):
    body = {"type": "sell", "execution": "market",
            "srcCurrency": base_currency.lower(), "dstCurrency": "rls",
            "amount": str(float(amount_base)), "price": "0"}
    return _sign_and_send("POST", "/market/orders/add", body)


# ==================================================
# کمکی
# ==================================================
def fmt_price(p):
    p = float(p)
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1: return f"{p:,.4f}"
    return f"{p:.6f}"


def progress_bar(pct, length=10):
    filled = max(0, min(length, int(round(pct / 100 * length))))
    return "▰" * filled + "▱" * (length - filled)


def status_emoji(pct):
    if pct >= 100: return "✅"
    if pct >= 75: return "🟢"
    if pct >= 50: return "🟡"
    if pct >= 25: return "🟠"
    return "🔴"


# ==================================================
# State
# ==================================================
def _new_coin_state():
    return {"in_position": False, "entry_price": 0, "entry_time": None,
            "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
            "capital": INITIAL_CAPITAL, "initial_capital": INITIAL_CAPITAL,
            "position_size": 0, "last_candle_time": None, "pending": None}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            if "coins" not in state:
                raise ValueError("old")
            for coin in COINS:
                if coin not in state["coins"]:
                    state["coins"][coin] = _new_coin_state()
            return state
        except Exception:
            pass
    state = {"last_update_id": 0, "coins": {}}
    for coin in COINS:
        state["coins"][coin] = _new_coin_state()
    return state


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
    if symbol in _cache and (now_ts - _cache[symbol]["time"]) < cache_seconds:
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
                    "time": pd.to_datetime(data["t"], unit="s"),
                    "open": data["o"], "high": data["h"],
                    "low": data["l"], "close": data["c"], "volume": data["v"],
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
    if (now - df["time"].iloc[-1]).total_seconds() < 3600:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def calculate_indicators(df, din, dout):
    df = df.copy()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    up = df["high"] - df["high"].shift()
    dn = df["low"].shift() - df["low"]
    pdm = up.where((up > dn) & (up > 0), 0)
    mdm = dn.where((dn > up) & (dn > 0), 0)
    atr_ = tr.ewm(span=14, adjust=False).mean()
    pdi = 100 * (pdm.ewm(span=14, adjust=False).mean() / atr_)
    mdi = 100 * (mdm.ewm(span=14, adjust=False).mean() / atr_)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()

    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["donchian_high"] = df["high"].rolling(din).max().shift(1)
    df["donchian_low"] = df["low"].rolling(dout).min().shift(1)
    return df.dropna().reset_index(drop=True)


# ==================================================
# منطق
# ==================================================
def update_trailing_stop(sc, latest, tatr):
    if not sc["in_position"]:
        return
    if latest["high"] > sc["highest_price"]:
        sc["highest_price"] = latest["high"]
    new_sl = sc["highest_price"] - (tatr * sc["entry_atr"])
    if new_sl > sc["stop_loss"]:
        sc["stop_loss"] = new_sl


def check_entry(latest, adx_min, vol_mult):
    return (latest["close"] > latest["donchian_high"] and
            latest["adx"] > adx_min and
            latest["volume"] > (vol_mult * latest["vol_avg"]))


# ==================================================
# پردازش Callbacks (دکمه‌ها)
# ==================================================
def process_callbacks(state):
    offset = state.get("last_update_id", 0) + 1
    updates = get_updates(offset=offset)
    if not updates:
        return state

    for upd in updates:
        state["last_update_id"] = upd["update_id"]

        if "callback_query" not in upd:
            continue

        cb = upd["callback_query"]
        cb_id = cb["id"]
        data = cb.get("data", "")
        msg_id = cb["message"]["message_id"]

        parts = data.split("_")  # مثلاً "buy_DOGE" یا "cancel_ADA"

        # ===== دکمه غیرفعال =====
        if parts[0] == "disabled":
            answer_callback(cb_id, "❌ شرایط فراهم نیست")
            continue

        # ===== دکمه تأیید خرید =====
        if parts[0] == "buy":
            coin = parts[1]
            if coin not in COINS:
                answer_callback(cb_id, "❌ ارز ناشناخته")
                continue

            sc = state["coins"][coin]
            pending = sc.get("pending")

            if not pending or pending.get("action") != "BUY":
                answer_callback(cb_id, "❌ سیگنال فعالی نیست")
                continue

            ptime = datetime.fromisoformat(pending["time"])
            if (now_iran().replace(tzinfo=None) - ptime.replace(tzinfo=None)).total_seconds() / 60 > PENDING_TIMEOUT_MIN:
                answer_callback(cb_id, "❌ منقضی شده")
                sc["pending"] = None
                continue

            answer_callback(cb_id, "⏳ در حال ارسال...")
            amount = pending["amount_rls"]
            print(f"Sending BUY {coin}: {amount} RLS")

            try:
                code, res = place_market_buy(coin, amount)
            except Exception as e:
                code, res = 0, {"error": str(e)}

            if code == 200 and res.get("status") == "ok":
                order = res.get("order", {})
                filled = order.get("price", pending["price"])
                sc.update({
                    "in_position": True, "entry_price": float(filled) if filled else pending["price"],
                    "entry_time": str(pending["time"]), "entry_atr": pending["atr"],
                    "highest_price": pending["price"],
                    "stop_loss": pending["price"] - (COINS[coin]["trailing_atr"] * pending["atr"]),
                    "position_size": pending["size"], "pending": None,
                })
                save_state(state)
                edit_telegram(msg_id, f"✅ <b>خرید {coin} انجام شد</b>\n💰 {fmt_price(filled)}\n📦 {pending['size']:.4f}")
            else:
                err = json.dumps(res, ensure_ascii=False)[:900]
                edit_telegram(msg_id, f"❌ <b>خطا</b>\n<code>{err}</code>")
                sc["pending"] = None
                save_state(state)

        # ===== دکمه تأیید فروش =====
        elif parts[0] == "sell":
            coin = parts[1]
            if coin not in COINS:
                answer_callback(cb_id, "❌ ارز ناشناخته")
                continue

            sc = state["coins"][coin]
            if not sc["in_position"]:
                answer_callback(cb_id, "❌ در معامله نیستی")
                continue

            answer_callback(cb_id, "⏳ در حال فروش...")
            wallets = get_wallets()
            balance = wallets.get(coin.lower(), 0)
            if balance <= 0:
                edit_telegram(msg_id, f"❌ <b>موجودی {coin} صفره</b>")
                continue

            try:
                code, res = place_market_sell(coin, balance)
            except Exception as e:
                code, res = 0, {"error": str(e)}

            if code == 200 and res.get("status") == "ok":
                order = res.get("order", {})
                filled = float(order.get("price", 0))
                ev = sc["entry_price"] * sc["position_size"]
                xv = filled * balance
                fee = (ev + xv) * (FEE / 2)
                net = (xv - ev) - fee
                pp = (net / ev * 100) if ev > 0 else 0
                sc["capital"] += net
                log_trade({"coin": coin, "entry_time": sc.get("entry_time"),
                           "exit_time": str(now_iran()), "entry": round(sc["entry_price"], 6),
                           "exit": round(filled, 6), "size": round(balance, 6),
                           "profit_rial": round(net, 2), "profit_%": round(pp, 3),
                           "capital": round(sc["capital"], 2), "reason": "BUTTON_SELL"})
                sc.update({"in_position": False, "entry_price": 0, "entry_time": None,
                           "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
                           "position_size": 0, "pending": None})
                save_state(state)
                color = "🟢" if net > 0 else "🔴"
                edit_telegram(msg_id, f"{color} <b>فروش {coin} انجام شد</b>\n💰 {fmt_price(filled)}\n📊 {net:+,.0f} ({pp:+.2f}%)")
            else:
                err = json.dumps(res, ensure_ascii=False)[:900]
                edit_telegram(msg_id, f"❌ <b>خطای فروش</b>\n<code>{err}</code>")

        # ===== دکمه لغو =====
        elif parts[0] == "cancel":
            coin = parts[1]
            if coin in COINS:
                state["coins"][coin]["pending"] = None
                save_state(state)
                answer_callback(cb_id, "❌ لغو شد")
                edit_telegram(msg_id, f"❌ <b>لغو {coin}</b>")

        # ===== /status =====
        elif data == "status":
            answer_callback(cb_id, "📊")
            process_status(state)

        # ===== /help =====
        elif data == "help":
            answer_callback(cb_id, "🤖")

    save_state(state)
    return state


def process_status(state):
    wallets = get_wallets()
    rls = wallets.get("rls", 0)
    msg = f"📊 <b>وضعیت</b>\n━━━━━━━━━━━━━━━\n💼 RLS: {rls:,.0f}\n\n"
    for coin in COINS:
        sc = state["coins"][coin]
        bal = wallets.get(coin.lower(), 0)
        if sc["in_position"]:
            msg += f"🟢 <b>{coin}</b>: در معامله\n   💰 {fmt_price(sc['entry_price'])} | SL: {fmt_price(sc['stop_loss'])}\n"
        else:
            msg += f"⚪ <b>{coin}</b>: خارج ({bal:.4f})\n"
    send_telegram(msg)


# ==================================================
# پیام + دکمه
# ==================================================
def build_status_message(coin, latest, now_str, sc):
    params = COINS[coin]
    price = latest["close"]
    donchian = latest["donchian_high"]
    adx = latest["adx"]
    vol_ratio = latest["volume"] / latest["vol_avg"] if latest["vol_avg"] > 0 else 0

    price_pct = min(100, (price / donchian * 100)) if donchian > 0 else 0
    adx_pct = min(100, (adx / params["adx_min"] * 100))
    vol_pct = min(100, (vol_ratio / params["vol_mult"] * 100))
    total_pct = (price_pct + adx_pct + vol_pct) / 3

    buy_ok = check_entry(latest, params["adx_min"], params["vol_mult"]) and not sc["in_position"]
    sell_ok = sc["in_position"]

    msg = (
        f"📊 <b>بررسی {coin}</b>\n🕐 {now_str}\n━━━━━━━━━━━━━━━\n"
        f"💰 قیمت: <b>{fmt_price(price)}</b> ریال\n\n"
        f"<b>شرایط ورود:</b>\n"
        f"{status_emoji(price_pct)} قیمت vs سقف: <b>{price_pct:.1f}%</b>\n"
        f"   <code>{progress_bar(price_pct)}</code>\n   هدف: {fmt_price(donchian)}\n\n"
        f"{status_emoji(adx_pct)} ADX: <b>{adx:.1f}</b> / {params['adx_min']} (<b>{adx_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(adx_pct)}</code>\n\n"
        f"{status_emoji(vol_pct)} حجم: <b>{vol_ratio:.2f}x</b> / {params['vol_mult']}x (<b>{vol_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(vol_pct)}</code>\n"
        f"━━━━━━━━━━━━━━━\n🎯 <b>آمادگی: {total_pct:.1f}%</b>\n"
    )

    if sc["in_position"]:
        msg += f"\n🟢 <b>در معامله</b> | 💰 {fmt_price(sc['entry_price'])} | 🛡️ {fmt_price(sc['stop_loss'])}"
    elif buy_ok:
        msg += "\n🟢 <b>سیگنال خرید آماده!</b>"
    elif total_pct >= 90:
        msg += "\n⚡ <b>نزدیک سیگنال!</b>"
    elif total_pct >= 70:
        msg += "\n🔥 در حال نزدیک شدن..."
    else:
        msg += "\n😴 بازار آرام"

    # ===== دکمه‌ها =====
    if sc["in_position"]:
        keyboard = {"inline_keyboard": [[
            {"text": "🔴 فروش", "callback_data": f"sell_{coin}"},
            {"text": "❌ لغو", "callback_data": f"cancel_{coin}"},
        ], [
            {"text": "📊 وضعیت", "callback_data": "status"},
            {"text": "❓ راهنما", "callback_data": "help"},
        ]]}
    elif buy_ok:
        keyboard = {"inline_keyboard": [[
            {"text": "🟢 خرید", "callback_data": f"buy_{coin}"},
            {"text": "❌ لغو", "callback_data": f"cancel_{coin}"},
        ], [
            {"text": "📊 وضعیت", "callback_data": "status"},
            {"text": "❓ راهنما", "callback_data": "help"},
        ]]}
    else:
        keyboard = {"inline_keyboard": [[
            {"text": "📊 وضعیت", "callback_data": "status"},
            {"text": "❓ راهنما", "callback_data": "help"},
        ]]}

    return msg, keyboard


# ==================================================
# اجرا
# ==================================================
def run_once():
    now_str = now_iran().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str}] Checking all coins...")

    state = load_state()

    for coin in COINS:
        params = COINS[coin]
        sc = state["coins"][coin]

        print(f"  [{coin}]")
        df_raw = fetch_candles(params["symbol"], limit=500)
        if df_raw is None or len(df_raw) < 100:
            print(f"    No data")
            continue

        df = calculate_indicators(drop_unclosed_candle(df_raw), params["donchian_in"], params["donchian_out"])
        if len(df) == 0:
            continue
        latest = df.iloc[-1]

        update_trailing_stop(sc, latest, params["trailing_atr"])
        save_state(state)

        msg, keyboard = build_status_message(coin, latest, now_str, sc)

        # pending خرید
        if check_entry(latest, params["adx_min"], params["vol_mult"]) and not sc["in_position"]:
            ep = latest["close"] * (1 + SLIPPAGE)
            tv = sc["capital"] * POSITION_PCT
            ps = (tv * (1 - FEE / 2)) / ep
            sc["pending"] = {"action": "BUY", "price": ep, "size": ps,
                             "amount_rls": min(tv, MAX_ORDER_RLS),
                             "atr": latest["atr"], "time": now_iran().isoformat()}

        # pending فروش
        elif sc["in_position"]:
            sc["pending"] = {"action": "SELL", "price": latest["close"] * (1 - SLIPPAGE),
                             "time": now_iran().isoformat()}
        save_state(state)

        send_telegram(msg, reply_markup=keyboard)
        print(f"    Status sent")

    # ===== پردازش دکمه‌ها =====
    try:
        state = process_callbacks(state)
    except Exception as e:
        print(f"Callback error: {e}")


if __name__ == "__main__":
    run_once()
# updated
