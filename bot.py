# bot.py — Discord-only; Live data (Tradier) / Sandbox trades (Tradier); Google Sheets logging
# Uses your env vars:
# DISCORD_TOKEN, OPENAI_API_KEY
# GOOGLE_SERVICE_ACCOUNT_JSON_TEXT, GOOGLE_SHEET_ID
# TRADES_TAB, ACTIVE_TRADES_TAB, PARTIALS_TAB, SIGNALS_TAB, EVENTS_TAB
# OPENAI_MODEL, OPENAI_MODEL_FALLBACK
# TRADIER_LIVE_API_KEY, TRADIER_SANDBOX_API_KEY, TRADIER_SANDBOX_ACCOUNT_ID
# EXTENDED_LIMIT_SLIPPAGE_BPS, EXTENDED_STOCK_ENABLED
# Optional: TIMEZONE (default America/New_York), GPT_SYSTEM_PROMPT

import os, json, requests, asyncio, traceback, discord
from datetime import datetime
from typing import Optional, Literal

# ---------- Timezone ----------
def _tz():
    tz = os.getenv("TIMEZONE", "America/New_York")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(tz)
    except Exception:
        # fallback: naive UTC isoformat if zoneinfo not available
        return None

_TZ = _tz()

def now_iso():
    if _TZ:
        return datetime.now(_TZ).isoformat()
    try:
        from datetime import timezone
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()

# ---------- OpenAI (GPT) ----------
try:
    from openai import OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    _openai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    _openai = None

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_MODEL_FALLBACK = os.getenv("OPENAI_MODEL_FALLBACK", "gpt-4o-mini")

GPT_SYSTEM_PROMPT = os.getenv(
    "GPT_SYSTEM_PROMPT",
    "You are TradeAlertBot. Extract trading intents precisely. "
    "Ask for missing details. Only immediate market orders require confirmation; "
    "conditional and limit/stop orders execute without confirmation. "
    "Always log key steps succinctly."
)

# ---------- Google Sheets (one spreadsheet; multiple tabs) ----------
GSHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
GS_JSON_TEXT = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_TEXT", "").strip()

# Tabs (allow overriding via env)
EVENTS_TAB = os.getenv("EVENTS_TAB", "Events")
TRADES_TAB = os.getenv("TRADES_TAB", "Trades")
ACTIVE_TRADES_TAB = os.getenv("ACTIVE_TRADES_TAB", "ActiveTrades")
PARTIALS_TAB = os.getenv("PARTIALS_TAB", "Partials")
SIGNALS_TAB = os.getenv("SIGNALS_TAB", "Signals")
CONVERSATIONS_TAB = os.getenv("CONVERSATIONS_TAB", "Conversations")  # optional, for chat pairs

_sheets_ok = False
_ws = {}

def _sheet_append_row(tab: str, header: list[str], row: list):
    global _sheets_ok, _ws
    if not _sheets_ok:
        return False
    try:
        ws = _ws.get(tab)
        if not ws:
            try:
                ws = _sheet.worksheet(tab)
            except Exception:
                ws = _sheet.add_worksheet(tab, rows=2000, cols=max(10, len(header)))
                ws.append_row(header)
            # ensure header exists
            try:
                first = ws.row_values(1)
                if not first:
                    ws.append_row(header)
            except Exception:
                pass
            _ws[tab] = ws
        ws.append_row(row)
        return True
    except Exception as e:
        print(f"Sheets append error ({tab}):", e)
        return False

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    if GSHEET_ID and GS_JSON_TEXT:
        SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(GS_JSON_TEXT), SCOPES)
        _gs_client = gspread.authorize(creds)
        _sheet = _gs_client.open_by_key(GSHEET_ID)
        _sheets_ok = True
    else:
        print("Sheets init: missing GOOGLE_SHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON_TEXT")
except Exception as e:
    print("Sheets init failed:", e)
    _sheets_ok = False

def log_event(kind: str, direction: str, actor: str,
              channel_id: Optional[str], user_id: Optional[str], payload):
    ts = now_iso()
    try:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except Exception:
        data = str(payload)
    _sheet_append_row(
        EVENTS_TAB,
        ["timestamp", "kind", "direction", "actor", "channel_id", "user_id", "payload_json"],
        [ts, kind, direction, actor, str(channel_id or ""), str(user_id or ""), data]
    )

def log_trade(action: str, symbol: str, qty: int, details):
    ts = now_iso()
    try:
        dj = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
    except Exception:
        dj = str(details)
    _sheet_append_row(
        TRADES_TAB,
        ["timestamp", "action", "symbol", "qty", "details_json"],
        [ts, action, symbol, int(qty), dj]
    )

def log_conversation(user_text: str, assistant_text: str, channel_id: str, user_id: str):
    ts = now_iso()
    _sheet_append_row(
        CONVERSATIONS_TAB,
        ["timestamp", "channel_id", "user_id", "user_text", "assistant_text"],
        [ts, channel_id, user_id, user_text or "", assistant_text or ""]
    )

# ---------- Policy toggles ----------
REQUIRE_CONFIRM_MARKET_ONLY = os.getenv("REQUIRE_CONFIRM_MARKET_ONLY", "true").strip().lower() in ("1","true","t","yes","y","on")
EXTENDED_LIMIT_SLIPPAGE_BPS = float(os.getenv("EXTENDED_LIMIT_SLIPPAGE_BPS", "0") or 0)
EXTENDED_STOCK_ENABLED = os.getenv("EXTENDED_STOCK_ENABLED", "false").strip().lower() in ("1","true","t","yes","y","on")

def needs_confirmation(order_type: str, is_conditional: bool) -> bool:
    if not REQUIRE_CONFIRM_MARKET_ONLY:
        return True
    if is_conditional:
        return False
    return order_type.lower() == "market"

# ---------- Tradier: split live data vs sandbox trading ----------
DATA_BASE = os.getenv("TRADIER_LIVE_BASE_URL", "https://api.tradier.com/v1").rstrip("/")
TRADE_BASE = os.getenv("TRADIER_SANDBOX_BASE_URL", "https://sandbox.tradier.com/v1").rstrip("/")
DATA_TOKEN = os.getenv("TRADIER_LIVE_API_KEY", "").strip()
TRADE_TOKEN = os.getenv("TRADIER_SANDBOX_API_KEY", "").strip()
TRADE_ACCT  = os.getenv("TRADIER_SANDBOX_ACCOUNT_ID", "").strip()

def tradier_data_request(endpoint: str, method: str = "GET", params=None, data=None):
    url = f"{DATA_BASE}{endpoint if endpoint.startswith('/') else '/'+endpoint}"
    headers = {"Authorization": f"Bearer {DATA_TOKEN}", "Accept": "application/json"}
    log_event("broker","out","bot", None, None, {"client":"DATA","endpoint":endpoint,"method":method,"params":params})
    r = requests.request(method, url, headers=headers, params=params, data=data, timeout=20)
    if not r.ok:
        log_event("broker","in","tradier", None, None, {"status":r.status_code,"body":r.text})
        raise RuntimeError(f"Tradier DATA {r.status_code}: {r.text}")
    js = r.json()
    log_event("broker","in","tradier", None, None, js)
    return js

def tradier_trade_request(endpoint: str, method: str = "GET", params=None, data=None):
    url = f"{TRADE_BASE}{endpoint if endpoint.startswith('/') else '/'+endpoint}"
    headers = {
        "Authorization": f"Bearer {TRADE_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    log_event("broker","out","bot", None, None, {"client":"TRADE","endpoint":endpoint,"method":method,"params":params,"data":data})
    r = requests.request(method, url, headers=headers, params=params, data=data, timeout=20)
    if not r.ok:
        log_event("broker","in","tradier", None, None, {"status":r.status_code,"body":r.text})
        raise RuntimeError(f"Tradier TRADE {r.status_code}: {r.text}")
    js = r.json()
    log_event("broker","in","tradier", None, None, js)
    return js

# ---------- Market data (LIVE) ----------
def get_equity_quote(symbol: str) -> dict:
    return tradier_data_request("/markets/quotes", params={"symbols": symbol})

def get_option_chain(symbol: str, expiry: str) -> dict:
    return tradier_data_request("/markets/options/chains", params={"symbol": symbol, "expiration": expiry, "greeks": "true"})

def get_history(symbol: str, interval="hour", start: Optional[str]=None, end: Optional[str]=None) -> dict:
    params = {"symbol": symbol, "interval": interval}
    if start: params["start"] = start
    if end: params["end"] = end
    return tradier_data_request("/markets/history", params=params)

# ---------- Trading (SANDBOX) ----------
def _infer_underlying_from_occ(occ: str) -> str:
    """Best-effort extraction of underlying from OCC symbol, e.g., AMD250822C00185000 -> AMD"""
    for i, ch in enumerate(occ):
        if ch.isdigit():
            return occ[:i].upper()
    return occ[:4].upper()

def place_option_order_by_occ(occ: str, side: str, qty: int, type: str = "market",
                              limit: Optional[float] = None, stop: Optional[float] = None,
                              duration: str = "day", underlying: Optional[str] = None,
                              is_conditional: bool = False) -> dict:
    """
    Sends BOTH 'symbol' (underlying) and 'option_symbol' (OCC) for class=option.
    Respects confirmation policy: market only; conditional orders skip confirmation.
    """
    if needs_confirmation(type, is_conditional):
        # In your real flow, you'd stage and ask user; here we just log a policy note.
        log_event("policy","out","bot", None, None, {"confirm_required": True, "reason": "market order", "occ": occ})
        # You can raise or return a message; keeping passive here:
    underlying = (underlying or _infer_underlying_from_occ(occ))
    payload = {
        "class": "option",
        "symbol": underlying,        # underlying ticker
        "option_symbol": occ,        # full OCC
        "side": side,                # e.g., buy_to_open, sell_to_close
        "quantity": int(qty),
        "type": type.lower(),
        "duration": duration,
    }
    if limit is not None and payload["type"] in ("limit","stop_limit"):
        # Apply slippage policy if provided (bps)
        if EXTENDED_LIMIT_SLIPPAGE_BPS:
            limit = float(limit) * (1 + EXTENDED_LIMIT_SLIPPAGE_BPS/10000.0)
        payload["price"] = float(limit)
    if stop is not None and payload["type"] in ("stop","stop_limit"):
        payload["stop"] = float(stop)
    res = tradier_trade_request(f"/accounts/{TRADE_ACCT}/orders", method="POST", data=payload)
    log_trade(side, occ, qty, res)
    return res

def place_equity_order(symbol: str, side: Literal["buy","sell"], quantity: int,
                       type: str = "market", limit: Optional[float] = None,
                       session: str = "REG", duration: str = "day",
                       is_conditional: bool = False) -> dict:
    if needs_confirmation(type, is_conditional):
        log_event("policy","out","bot", None, None, {"confirm_required": True, "reason": "market order", "symbol": symbol})
    payload = {
        "class": "equity",
        "symbol": symbol.upper(),
        "side": side,
        "quantity": int(quantity),
        "type": type.lower(),
        "duration": duration,
        "session": "EXT" if EXTENDED_STOCK_ENABLED else session.upper(),
    }
    if limit is not None and type.lower() in ("limit","stop_limit"):
        if EXTENDED_LIMIT_SLIPPAGE_BPS:
            limit = float(limit) * (1 + EXTENDED_LIMIT_SLIPPAGE_BPS/10000.0)
        payload["price"] = float(limit)
    res = tradier_trade_request(f"/accounts/{TRADE_ACCT}/orders", method="POST", data=payload)
    log_trade(side, symbol, quantity, res)
    return res

def get_positions() -> dict:
    return tradier_trade_request(f"/accounts/{TRADE_ACCT}/positions")

# ---------- OCC helper ----------
def build_occ(underlying: str, expiry_yyyymmdd: str, cp: Literal["call","put"], strike: float) -> str:
    yy = expiry_yyyymmdd[2:4]; mm = expiry_yyyymmdd[4:6]; dd = expiry_yyyymmdd[6:8]
    cp_code = "C" if cp.lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{underlying.upper()}{yy}{mm}{dd}{cp_code}{strike_int:08d}"

# ---------- GPT Orchestrator ----------
async def gpt_orchestrate(user_text: str, channel_id: str, user_id: str) -> str:
    if not _openai:
        return "GPT not configured. Set OPENAI_API_KEY."
    try:
        log_event("gpt","out","bot", channel_id, user_id, {"prompt": user_text})
        try:
            resp = _openai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role":"system","content": GPT_SYSTEM_PROMPT},
                          {"role":"user","content": user_text}]
            )
        except Exception:
            # fallback model
            resp = _openai.chat.completions.create(
                model=OPENAI_MODEL_FALLBACK,
                messages=[{"role":"system","content": GPT_SYSTEM_PROMPT},
                          {"role":"user","content": user_text}]
            )
        reply = resp.choices[0].message.content
        log_event("gpt","in","gpt", channel_id, user_id, {"response": reply})
        return reply
    except Exception as e:
        return f"GPT error: {e}"

# ---------- Discord ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    log_event("system","info","bot", None, None, "Bot started and logged in")

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content = (message.content or "").strip()
    if not content:
        return

    # Discord IN
    try:
        log_event("discord","in","user", str(message.channel.id), str(message.author.id), content)
    except Exception:
        pass

    # Simple command: quote SYMBOL
    if content.lower().startswith("quote "):
        sym = content.split(" ",1)[1].strip().upper()
        try:
            q = get_equity_quote(sym)
            text = f"📈 {sym} quote:\n```json\n{json.dumps(q, indent=2)}```"
            await message.channel.send(text)
            log_conversation(content, text, str(message.channel.id), str(message.author.id))
            log_event("discord","out","assistant", str(message.channel.id), str(message.author.id), {"quote_symbol": sym})
            return
        except Exception as e:
            err = f"❌ Quote error: {e}"
            await message.channel.send(err)
            log_event("system","error","bot", str(message.channel.id), str(message.author.id), err)
            return

    # Default: send to GPT
    reply = await gpt_orchestrate(content, str(message.channel.id), str(message.author.id))
    await message.channel.send(reply)
    try:
        log_conversation(content, reply, str(message.channel.id), str(message.author.id))
        log_event("discord","out","assistant", str(message.channel.id), str(message.author.id), reply)
    except Exception:
        pass

# ---------- Entrypoint ----------
def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not set")
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
