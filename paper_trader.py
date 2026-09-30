# ===== کتابخانه‌ها =====
import os
import json
import time
import requests
import pandas as pd
from datetime import datetime

# ===== خواندن کلیدها =====
PUBLIC_KEY  = os.getenv("NOBITEX_PUBLIC_KEY")
PRIVATE_KEY = os.getenv("NOBITEX_PRIVATE_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ==================================================
# پارامترهای استراتژی (بهینه DOGE)
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

STATE_FILE  = "paper_state.json"
TRADES_FILE = "paper_trades.csv"


# ==================================================
# ارسال پیام به تلگرام
# ==================================================
def send_telegram(message: str, parse_mode: str = "HTML"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured, skipping.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram error: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Telegram exception: {e}")


# ==================================================
# فرمت قیمت
# ==================================================
def fmt_price(p):
    p = float(p)
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1:    return f"{p:,.4f}"
    return f"{p:.6f}"


# ==================================================
# نوار پیشرفت (برای نمایش نزدیکی به شرط)
# ==================================================
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
# توابع وضعیت
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
# دریافت داده
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


# ==================================================
# کندل بسته‌نشده
# ==================================================
def drop_unclosed_candle(df):
    if len(df) == 0:
        return df
    now = pd.Timestamp.now()
    last_time = df["time"].iloc[-1]
    if (now - last_time).total_seconds() < 3600:
        return df.iloc[:-1].reset_index(drop=True)
    return df


# ==================================================
# اندیکاتورها
# ==================================================
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
# منطق معاملات
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


def calc_pnl(state, exit_price):
    entry_value = state["entry_price"] * state["position_size"]
    exit_value  = exit_price * state["position_size"]
    gross_pnl   = exit_value - entry_value
    fee_amount  = (entry_value + exit_value) * (FEE / 2)
    net_pnl     = gross_pnl - fee_amount
    profit_pct  = (net_pnl / entry_value) * 100
    return net_pnl, profit_pct


# ==================================================
# ساخت پیام وضعیت (با درصد نزدیکی)
# ==================================================
def build_status_message(latest, now_str, state):
    price       = latest["close"]
    donchian    = latest["donchian_high"]
    adx         = latest["adx"]
    volume      = latest["volume"]
    vol_avg     = latest["vol_avg"]
    vol_ratio   = volume / vol_avg if vol_avg > 0 else 0

    # ===== درصد نزدیکی به هر شرط =====
    price_pct   = min(100, (price / donchian * 100)) if donchian > 0 else 0
    adx_pct     = min(100, (adx / ADX_MIN * 100))
    vol_pct     = min(100, (vol_ratio / VOL_MULT * 100))

    # ===== امتیاز کل =====
    total_pct   = (price_pct + adx_pct + vol_pct) / 3

    msg = (
        f"📊 <b>بررسی {COIN}</b>\n"
        f"🕐 {now_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 قیمت: <b>{fmt_price(price)}</b> ریال\n"
        f"\n"
        f"<b>شرایط ورود:</b>\n"
        f"{status_emoji(price_pct)} قیمت vs سقف: <b>{price_pct:.1f}%</b>\n"
        f"   <code>{progress_bar(price_pct)}</code>\n"
        f"   هدف: {fmt_price(donchian)}\n"
        f"\n"
        f"{status_emoji(adx_pct)} ADX: <b>{adx:.1f}</b> / {ADX_MIN} (<b>{adx_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(adx_pct)}</code>\n"
        f"\n"
        f"{status_emoji(vol_pct)} حجم: <b>{vol_ratio:.2f}x</b> / {VOL_MULT}x (<b>{vol_pct:.1f}%</b>)\n"
        f"   <code>{progress_bar(vol_pct)}</code>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🎯 <b>آمادگی کل: {total_pct:.1f}%</b>\n"
    )

    if total_pct >= 90:
        msg += "\n⚡ <b>نزدیک سیگنال!</b>"
    elif total_pct >= 70:
        msg += "\n🔥 در حال نزدیک شدن..."
    elif total_pct >= 40:
        msg += "\n⏳ در حال شکل‌گیری..."
    else:
        msg += "\n😴 بازار آرام"

    return msg


# ==================================================
# اجرای یک‌باره
# ==================================================
def run_once():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str}] Checking {COIN}...")

    state = load_state()
    df_raw = fetch_candles(SYMBOL, limit=500)
    if df_raw is None or len(df_raw) < 100:
        send_telegram(f"⚠️ <b>خطا در دریافت داده</b>\nزمان: {now_str}")
        print("No data")
        return

    df = calculate_indicators(drop_unclosed_candle(df_raw))
    latest = df.iloc[-1]

    # ===== جلوگیری از پردازش تکراری =====
    candle_key = str(latest["time"])
    if state.get("last_candle_time") == candle_key:
        print(f"Already processed: {candle_key}")
        return

    # ===== به‌روزرسانی Trailing =====
    update_trailing_stop(state, latest)
    save_state(state)

    # ===== بررسی سیگنال ورود =====
    if not state["in_position"] and check_entry(latest):
        entry_price = latest["close"] * (1 + SLIPPAGE)
        trade_value = state["capital"] * POSITION_PCT
        position_size = (trade_value * (1 - FEE / 2)) / entry_price

        state.update({
            "in_position": True,
            "entry_price": entry_price,
            "entry_time": str(latest["time"]),
            "entry_atr": latest["atr"],
            "highest_price": entry_price,
            "stop_loss": entry_price - (TRAILING_ATR * latest["atr"]),
            "position_size": position_size,
            "last_candle_time": candle_key,
        })
        save_state(state)

        msg = (
            f"🟢 <b>سیگنال خرید (BUY)</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💰 قیمت ورود: <b>{fmt_price(entry_price)}</b> ریال\n"
            f"📦 حجم: <b>{position_size:.4f}</b> {COIN}\n"
            f"💵 ارزش: <b>{trade_value:,.0f}</b> ریال\n"
            f"🛡️ حد ضرر: {fmt_price(state['stop_loss'])}\n"
            f"⏰ زمان: {now_str}\n"
            f"━━━━━━━━━━━━━━━"
        )
        send_telegram(msg)
        print(f">>> BUY @ {fmt_price(entry_price)}")
        return

    # ===== بررسی سیگنال خروج =====
    if state["in_position"]:
        reason = check_exit(state, latest)
        if reason:
            exit_price = latest["close"] * (1 - SLIPPAGE)
            net_pnl, profit_pct = calc_pnl(state, exit_price)
            state["capital"] += net_pnl

            trade = {
                "entry_time":  state["entry_time"],
                "exit_time":   str(latest["time"]),
                "entry":       round(state["entry_price"], 6),
                "exit":        round(exit_price, 6),
                "size":        round(state["position_size"], 6),
                "profit_rial": round(net_pnl, 2),
                "profit_%":    round(profit_pct, 3),
                "capital":     round(state["capital"], 2),
                "reason":      reason,
            }
            log_trade(trade)

            color = "🟢" if net_pnl > 0 else "🔴"
            msg = (
                f"{color} <b>سیگنال فروش (SELL)</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💰 قیمت خروج: <b>{fmt_price(exit_price)}</b> ریال\n"
                f"📊 سود/ضرر: <b>{net_pnl:+,.0f}</b> ریال ({profit_pct:+.2f}%)\n"
                f"📝 دلیل: {reason}\n"
                f"💼 سرمایه جدید: {state['capital']:,.0f} ریال\n"
                f"⏰ زمان: {now_str}\n"
                f"━━━━━━━━━━━━━━━"
            )
            send_telegram(msg)

            state.update({
                "in_position": False, "entry_price": 0, "entry_time": None,
                "stop_loss": 0, "highest_price": 0, "entry_atr": 0,
                "position_size": 0,
            })
            save_state(state)
            print(f"<<< SELL [{reason}] @ {fmt_price(exit_price)}")
            return

    # ===== اگر سیگنالی نبود =====
    state["last_candle_time"] = candle_key
    save_state(state)

    # ===== پیام وضعیت با درصد نزدیکی =====
    status_msg = build_status_message(latest, now_str, state)
    send_telegram(status_msg)
    print("No signal — status sent")


# ==================================================
# اجرا
# ==================================================
if __name__ == "__main__":
    run_once()