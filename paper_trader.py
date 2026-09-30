# ===== کتابخانه‌ها =====
import os
import json
import time
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ==================================================
# پارامترها (دقیقاً یکسان با بهترین بک‌تست DOGE)
# ==================================================
COIN         = "DOGE"
SYMBOL       = "DOGEIRT"
DONCHIAN_IN  = 30
DONCHIAN_OUT = 15
TRAILING_ATR = 4.0
ADX_MIN      = 35
VOL_MULT     = 1.5

# ===== ریسک =====
FEE           = 0.005
SLIPPAGE      = 0.001
MAX_HOLD_HOURS = None      # ✅ غیرفعال (یکسان با بک‌تست)

# ===== سرمایه =====
INITIAL_CAPITAL = 20_000_000
POSITION_PCT    = 1.0

# ===== گزینه‌ها =====
USE_DYNAMIC_ATR     = False
ONLY_CLOSED_CANDLES = True

STATE_FILE  = "paper_state.json"
TRADES_FILE = "paper_trades.csv"


# ==================================================
# کمکی
# ==================================================
def fmt_price(p):
    p = float(p)
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1:    return f"{p:,.4f}"
    return f"{p:.6f}"


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
# کندل بسته‌نشده
# ==================================================
def drop_unclosed_candle(df):
    if not ONLY_CLOSED_CANDLES or len(df) == 0:
        return df
    now = pd.Timestamp.now()
    last_time = df["time"].iloc[-1]
    if (now - last_time).total_seconds() < 3600:
        return df.iloc[:-1].reset_index(drop=True)
    return df


# ==================================================
# قیمت لحظه‌ای
# ==================================================
def get_live_price(symbol):
    """
    قیمت لحظه‌ای بازار.
    این قیمت معادل open کندل بعدی در بک‌تست است،
    چون لحظه بسته شدن کندل i دقیقاً لحظه باز شدن کندل i+1 است.
    """
    try:
        url = f"https://apiv2.nobitex.ir/v2/orderbook/{symbol}"
        r = requests.get(url, timeout=5)
        data = r.json()
        if data.get("status") == "ok":
            asks = data.get("asks", [])
            bids = data.get("bids", [])
            return (
                float(bids[0][0]) if bids else None,
                float(asks[0][0]) if asks else None,
            )
    except Exception:
        return None, None
    return None, None


# ==================================================
# دریافت کندل‌ها
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
# اندیکاتورها
# ==================================================
def calculate_indicators(df):
    df = df.copy()
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(window=14).mean()

    up  = df["high"] - df["high"].shift()
    dn  = df["low"].shift() - df["low"]
    pdm = up.where((up > dn) & (up > 0), 0)
    mdm = dn.where((dn > up) & (dn > 0), 0)
    atr_ = tr.ewm(span=14, adjust=False).mean()
    pdi  = 100 * (pdm.ewm(span=14, adjust=False).mean() / atr_)
    mdi  = 100 * (mdm.ewm(span=14, adjust=False).mean() / atr_)
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()

    df["vol_avg"]       = df["volume"].rolling(window=20).mean()
    df["donchian_high"] = df["high"].rolling(window=DONCHIAN_IN).max().shift(1)
    df["donchian_low"]  = df["low"].rolling(window=DONCHIAN_OUT).min().shift(1)

    return df.dropna().reset_index(drop=True)


# ==================================================
# منطق معاملات
# ==================================================
def update_trailing_stop(state, latest):
    if not state["in_position"]:
        return state
    if latest["high"] > state["highest_price"]:
        state["highest_price"] = latest["high"]
    atr_ref = latest["atr"] if USE_DYNAMIC_ATR else state["entry_atr"]
    new_sl  = state["highest_price"] - (TRAILING_ATR * atr_ref)
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

    # ۱. Stop Loss (تشخیص با Low — مثل بک‌تست)
    if latest["low"] <= state["stop_loss"]:
        return "STOP_LOSS"

    # ۲. شکست Donchian
    if latest["close"] < latest["donchian_low"]:
        return "BREAKDOWN"

    # ۳. Timeout (فقط اگه فعال باشه)
    if MAX_HOLD_HOURS and state.get("entry_time"):
        try:
            entry_dt = pd.to_datetime(state["entry_time"])
            hours = (pd.to_datetime(latest["time"]) - entry_dt).total_seconds() / 3600
            if hours >= MAX_HOLD_HOURS:
                return "TIMEOUT"
        except Exception:
            pass

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
# اجرای یک‌باره
# ==================================================
def run_once(auto_trade=False):
    """
    auto_trade=True → معامله خودکار انجام می‌شود
    auto_trade=False → فقط سیگنال را نشان می‌دهد
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str}] Checking {COIN}...")

    state = load_state()
    df_raw = fetch_candles(SYMBOL, limit=500)
    if df_raw is None or len(df_raw) < 100:
        print("  No data")
        return

    df = calculate_indicators(drop_unclosed_candle(df_raw))
    latest = df.iloc[-1]

    # ===== جلوگیری از پردازش تکراری =====
    candle_key = str(latest["time"])
    if state.get("last_candle_time") == candle_key:
        print(f"  Already processed: {candle_key}")
        return

    print(f"  Candle: {latest['time']} | Close: {fmt_price(latest['close'])} | "
          f"ADX: {latest['adx']:.1f} | Donchian: {fmt_price(latest['donchian_high'])}")

    # ===== به‌روزرسانی Trailing =====
    update_trailing_stop(state, latest)
    save_state(state)

    # ===== خروج =====
    if state["in_position"]:
        reason = check_exit(state, latest)
        if reason:
            bid, _ = get_live_price(SYMBOL)
            exit_price = (bid if bid else latest["close"]) * (1 - SLIPPAGE)
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
            print(f"  <<< SELL [{reason}] @ {fmt_price(exit_price)} | "
                  f"P/L: {net_pnl:+,.0f} ({profit_pct:+.2f}%)")

            state.update({
                "in_position": False, "entry_price": 0, "entry_time": None,
                "stop_loss": 0, "highest_price": 0, "entry_atr": 0, "position_size": 0,
            })
            state["last_candle_time"] = candle_key
            save_state(state)
            return

    # ===== ورود =====
    else:
        if check_entry(latest):
            _, ask = get_live_price(SYMBOL)
            entry_price = (ask if ask else latest["close"]) * (1 + SLIPPAGE)

            pct = state.get("position_pct", POSITION_PCT)
            trade_value = state["capital"] * pct
            position_size = (trade_value * (1 - FEE / 2)) / entry_price

            if auto_trade:
                state.update({
                    "in_position": True,
                    "entry_price": entry_price,
                    "entry_time":  str(latest["time"]),
                    "entry_atr":   latest["atr"],
                    "highest_price": entry_price,
                    "stop_loss":   entry_price - (TRAILING_ATR * latest["atr"]),
                    "position_size": position_size,
                })
                print(f"  >>> BUY @ {fmt_price(entry_price)} | "
                      f"Size: {position_size:.6f} | Value: {trade_value:,.0f}")
            else:
                print(f"  ⚠️ SIGNAL BUY (auto_trade off) @ {fmt_price(entry_price)}")

    state["last_candle_time"] = candle_key
    save_state(state)


# ==================================================
# اجرا
# ==================================================
if __name__ == "__main__":
    import sys
    auto = "--auto" in sys.argv

    if "once" in sys.argv:
        run_once(auto_trade=auto)
    else:
        print(f"Paper Trader started (auto={auto}). Press Ctrl+C to stop.\n")
        while True:
            try:
                run_once(auto_trade=auto)
                now = datetime.now()
                next_hour = (now.replace(minute=1, second=0, microsecond=0)
                             + pd.Timedelta(hours=1))
                wait = (next_hour - now).total_seconds()
                if wait < 0:
                    wait += 3600
                print(f"  Sleeping {int(wait)}s...")
                time.sleep(wait)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(60)
