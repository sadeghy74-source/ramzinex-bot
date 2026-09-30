# ===== کتابخانه‌ها =====
import os
import json
import time
import base64
import requests
import pandas as pd
from datetime import datetime

# ===== خواندن کلیدها =====
PUBLIC_KEY  = os.getenv("NOBITEX_PUBLIC_KEY")
PRIVATE_KEY = os.getenv("NOBITEX_PRIVATE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==================================================
# تنظیمات
# ==================================================
COIN         = "DOGE"
SYMBOL       = "DOGEIRT"
DONCHIAN_IN  = 30
DONCHIAN_OUT = 15
TRAILING_ATR = 4.0
ADX_MIN      = 35
VOL_MULT     = 1.5

FEE             = 0.005
SLIPPAGE        = 0.001
INITIAL_CAPITAL = 20_000_000
POSITION_PCT    = 1.0

REAL_TRADING_ENABLED = True
MAX_ORDER_RLS        = 100_000
PENDING_TIMEOUT_MIN  = 30

TEST_MODE_FORCE_BUY = True   # ← برای تست True، بعداً False کن

STATE_FILE  = "paper_state.json"
TRADES_FILE = "paper_trades.csv"


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


def place_market_buy(amount_rls):
    body = {
        "type": "buy",
        "execution": "market",
        "market": SYMBOL,
        "amount": int(amount_rls),
        "price": 0,
    }
    return _sign_and_send("POST", "/market/orders/add", body)


def place_market_sell(amount_doge):
    body = {
        "type": "sell",
        "execution": "market",
        "market": SYMBOL,
        "amount": float(amount_doge),
        "price": 0,
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
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "in_position": False, "entry_price": 0, "entry_time": None,
        "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
        "capital": INITIAL_CAPITAL, "initial_capital": INITIAL_CAPITAL,
        "position_pct": POSITION_PCT, "position_size": 0,
        "last_candle_time": None,
        "last_update_id": 0,
        "pending": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log_trade(trade):
    df = pd.DataFrame([trade])
    if os.path.exists(TRADES_FILE):
        df.to_csv(TRADES_FILE, mode="a", header=False, index=False)
    else:
        df.to_csv(TRADES_FILE, index=False)


# ==================================================
# داده
# ==================================================
_cache = {"time": 0, "df": None}


def fetch_candles(symbol, limit=500, cache_seconds=60):
    now_ts = time.time()
    if _cache["df"] is not None and (now_ts - _cache["time"]) < cache_seconds:
        return _cache["df"]

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
                _cache["time"] = now_ts
                _cache["df"]   = df
                return df
        except Exception:
            time.sleep(1)
    return _cache["df"]


def drop_unclosed_candle(df):
    if len(df) == 0:
        return df
    now = pd.Timestamp.now()
    last_time = df["time"].iloc[-1]
    if (now - last_time).total_seconds() < 3600:
        return df.iloc[:-1].reset_index(drop=True)
    return df


def calculate_indicators(df):
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
    df["donchian_high"] = df["high"].rolling(DONCHIAN_IN).max().shift(1)
    df["donchian_low"]  = df["low"].rolling(DONCHIAN_OUT).min().shift(1)

    return df.dropna().reset_index(drop=True)


# ==================================================
# منطق
# ==================================================
def update_trailing_stop(state, latest):
    if not state["in_position"]:
        return state
    if latest["high"] > state["highest_price"]:
        state["highest_price"] = latest["high"]
    new_sl = state["highest_price"] - (TRAILING_ATR * state["entry_atr"])
    if new_sl > state["stop_loss"]:
        state["stop_loss"] = new_sl
    return state


def check_entry(latest):
    return (
        latest["close"] > latest["donchian_high"] and
        latest["adx"] > ADX_MIN and
        latest["volume"] > (VOL_MULT * latest["vol_avg"])
    )


def check_exit(state, latest):
    if not state["in_position"]:
        return None
    if latest["low"] <= state["stop_loss"]:
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

        print(f"Received command: {text}")

        if text == "/buy":
            pending = state.get("pending")

            if not pending or pending.get("action") != "BUY":
                send_telegram("❌ <b>سیگنال فعالی برای خرید نیست</b>")
                continue

            pending_time = datetime.fromisoformat(pending["time"])
            age_min = (datetime.now() - pending_time).total_seconds() / 60
            if age_min > PENDING_TIMEOUT_MIN:
                send_telegram("❌ <b>سیگنال منقضی شده</b>")
                state["pending"] = None
                continue

            amount_rls = pending["amount_rls"]
            send_telegram(f"⏳ در حال ارسال سفارش {amount_rls:,} ریال...")
            print(f"Sending BUY: {amount_rls} RLS")

            try:
                code, res = place_market_buy(amount_rls)
            except Exception as e:
                code, res = 0, {"error": str(e)}

            print(f"Order response: {code} | {res}")

            if code == 200 and res.get("status") == "ok":
                order = res.get("order", {})
                order_id = order.get("id", "?")
                filled_price = order.get("price", pending["price"])

                state["in_position"]   = True
                state["entry_price"]   = float(filled_price) if filled_price else pending["price"]
                state["entry_time"]    = str(pending["time"])
                state["entry_atr"]     = pending["atr"]
                state["highest_price"] = pending["price"]
                state["stop_loss"]     = pending["price"] - (TRAILING_ATR * pending["atr"])
                state["position_size"] = pending["size"]
                state["pending"]       = None
                save_state(state)

                send_telegram(
                    f"✅ <b>خرید انجام شد</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🆔 {order_id}\n"
                    f"💰 {fmt_price(filled_price)}\n"
                    f"📦 {pending['size']:.4f} {COIN}\n"
                    f"🛡️ SL: {fmt_price(state['stop_loss'])}"
                )
            else:
                try:
                    err_json = json.dumps(res, ensure_ascii=False, indent=2)
                except Exception:
                    err_json = str(res)

                send_telegram(
                    f"❌ <b>سفارش رد شد</b>\n"
                    f"Status: <code>{code}</code>\n"
                    f"<code>{err_json[:900]}</code>"
                )
                state["pending"] = None
                save_state(state)

        elif text == "/sell":
            pending = state.get("pending")

            if not pending or pending.get("action") != "SELL":
                send_telegram("❌ <b>سیگنال فعالی برای فروش نیست</b>")
                continue

            send_telegram("⏳ در حال فروش...")

            wallets = get_wallets()
            doge_balance = wallets.get("doge", 0)
            if doge_balance <= 0:
                send_telegram("❌ <b>موجودی DOGE صفره</b>")
                state["pending"] = None
                continue

            try:
                code, res = place_market_sell(doge_balance)
            except Exception as e:
                code, res = 0, {"error": str(e)}

            if code == 200 and res.get("status") == "ok":
                order = res.get("order", {})
                order_id = order.get("id", "?")
                filled_price = float(order.get("price", 0))

                entry_value = state["entry_price"] * state["position_size"]
                exit_value  = filled_price * doge_balance
                gross_pnl   = exit_value - entry_value
                fee_amount  = (entry_value + exit_value) * (FEE / 2)
                net_pnl     = gross_pnl - fee_amount
                profit_pct  = (net_pnl / entry_value * 100) if entry_value > 0 else 0

                state["capital"] = state.get("capital", INITIAL_CAPITAL) + net_pnl
                log_trade({
                    "entry_time":  state.get("entry_time"),
                    "exit_time":   str(datetime.now()),
                    "entry":       round(state["entry_price"], 6),
                    "exit":        round(filled_price, 6),
                    "size":        round(doge_balance, 6),
                    "profit_rial": round(net_pnl, 2),
                    "profit_%":    round(profit_pct, 3),
                    "capital":     round(state["capital"], 2),
                    "reason":      "MANUAL_SELL",
                })

                state.update({
                    "in_position": False, "entry_price": 0, "entry_time": None,
                    "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
                    "position_size": 0, "pending": None,
                })
                save_state(state)

                color = "🟢" if net_pnl > 0 else "🔴"
                send_telegram(
                    f"{color} <b>فروش انجام شد</b>\n"
                    f"💰 {fmt_price(filled_price)}\n"
                    f"📊 {net_pnl:+,.0f} ({profit_pct:+.2f}%)"
                )
            else:
                try:
                    err_json = json.dumps(res, ensure_ascii=False, indent=2)
                except Exception:
                    err_json = str(res)

                send_telegram(
                    f"❌ <b>سفارش فروش رد شد</b>\n"
                    f"Status: <code>{code}</code>\n"
                    f"<code>{err_json[:900]}</code>"
                )
                state["pending"] = None
                save_state(state)

        elif text == "/cancel":
            state["pending"] = None
            save_state(state)
            send_telegram("❌ <b>لغو شد</b>")

        elif text == "/status":
            wallets = get_wallets()
            rls = wallets.get("rls", 0)
            doge = wallets.get("doge", 0)
            if state["in_position"]:
                send_telegram(
                    f"🟢 در معامله\n"
                    f"💰 {fmt_price(state['entry_price'])}\n"
                    f"📦 {state['position_size']:.4f}\n"
                    f"🛡️ {fmt_price(state['stop_loss'])}\n"
                    f"💼 RLS: {rls:,.0f}\n🪙 DOGE: {doge:.4f}"
                )
            else:
                send_telegram(
                    f"⚪ خارج از معامله\n"
                    f"💼 RLS: {rls:,.0f}\n🪙 DOGE: {doge:.4f}"
                )

        elif text in ("/start", "/help"):
            send_telegram(
                f"🤖 <b>دستورات</b>\n"
                f"/buy — خرید\n"
                f"/sell — فروش\n"
                f"/cancel — لغو\n"
                f"/status — وضعیت\n"
                f"/help — راهنما"
            )

        else:
            send_telegram(f"❓ دستور ناشناخته: <code>{text[:50]}</code>")

    save_state(state)
    return state


# ==================================================
# پیام وضعیت
# ==================================================
def build_status_message(latest, now_str, state):
    price     = latest["close"]
    donchian  = latest["donchian_high"]
    adx       = latest["adx"]
    volume    = latest["volume"]
    vol_avg   = latest["vol_avg"]
    vol_ratio = volume / vol_avg if vol_avg > 0 else 0

    price_pct = min(100, (price / donchian * 100)) if donchian > 0 else 0
    adx_pct   = min(100, (adx / ADX_MIN * 100))
    vol_pct   = min(100, (vol_ratio / VOL_MULT * 100))
    total_pct = (price_pct + adx_pct + vol_pct) / 3

    real_buy_ok = (
        latest["close"] > latest["donchian_high"] and
        latest["adx"] > ADX_MIN and
        latest["volume"] > (VOL_MULT * latest["vol_avg"])
    )
    buy_ok = True if (TEST_MODE_FORCE_BUY and not state["in_position"]) else real_buy_ok

    msg = (
        f"📊 <b>بررسی {COIN}</b>\n"
        f"🕐 {now_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 قیمت: <b>{fmt_price(price)}</b> ریال\n\n"
        f"<b>شرایط ورود:</b>\n"
        f"{status_emoji(price_pct)} قیمت vs سقف: <b>{price_pct:.1f}%</b>\n"
        f"   <code>{progress_bar(price_pct)}</code>\n"
        f"\n"
        f"{status_emoji(adx_pct)} ADX: <b>{adx:.1f}</b> / {ADX_MIN} (<b>{adx_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(adx_pct)}</code>\n"
        f"\n"
        f"{status_emoji(vol_pct)} حجم: <b>{vol_ratio:.2f}x</b> / {VOL_MULT}x (<b>{vol_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(vol_pct)}</code>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 <b>آمادگی: {total_pct:.1f}%</b>\n"
    )

    if TEST_MODE_FORCE_BUY and not state["in_position"]:
        msg += "\n🧪 <b>حالت تست فعال</b>"

    if state["in_position"]:
        msg += (
            f"\n🟢 <b>در معامله</b>\n"
            f"💰 {fmt_price(state['entry_price'])}\n"
            f"🛡️ SL: {fmt_price(state['stop_loss'])}\n"
            f"\n🔴 برای فروش: /sell"
        )
    elif buy_ok:
        msg += "\n\n🟢 <b>سیگنال خرید آماده!</b>"
        msg += "\n📩 بفرست: <code>/buy</code>"
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
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str}] Checking {COIN}...")

    state = load_state()

    # ===== ۱. اول داده بگیر =====
    df_raw = fetch_candles(SYMBOL, limit=500)
    if df_raw is None or len(df_raw) < 100:
        print("No data")
        return

    df = calculate_indicators(drop_unclosed_candle(df_raw))
    latest = df.iloc[-1]

    # ===== ۲. Trailing =====
    update_trailing_stop(state, latest)
    save_state(state)

    # ===== ۳. ساخت پیام و ثبت pending =====
    msg, buy_ok = build_status_message(latest, now_str, state)

    # ===== ۳a. pending خرید =====
    if buy_ok and not state["in_position"]:
        entry_price = latest["close"] * (1 + SLIPPAGE)
        trade_value = state["capital"] * POSITION_PCT
        position_size = (trade_value * (1 - FEE / 2)) / entry_price
        amount_rls = min(trade_value, MAX_ORDER_RLS)

        state["pending"] = {
            "action":      "BUY",
            "price":       entry_price,
            "size":        position_size,
            "amount_rls":  amount_rls,
            "atr":         latest["atr"],
            "time":        datetime.now().isoformat(),
        }
        save_state(state)
        print(f">>> BUY PENDING @ {fmt_price(entry_price)}")

    # ===== ۳b. pending فروش =====
    elif state["in_position"]:
        exit_price = latest["close"] * (1 - SLIPPAGE)
        state["pending"] = {
            "action":      "SELL",
            "price":       exit_price,
            "time":        datetime.now().isoformat(),
        }
        save_state(state)
        print(f"<<< SELL PENDING @ {fmt_price(exit_price)}")

    # ===== ۴. حالا دستورات /buy و /sell رو پردازش کن =====
    try:
        state = process_commands(state)
    except Exception as e:
        print(f"Command error: {e}")

    # ===== ۵. ارسال پیام وضعیت =====
    send_telegram(msg)
    print("Status sent")


# ==================================================
# اجرا
# ==================================================
if __name__ == "__main__":
    run_once()