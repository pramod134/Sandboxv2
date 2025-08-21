# bot.py — Discord-only; Live data / Sandbox trades; Google Sheets logging

import os, json, asyncio, traceback, requests, discord
from datetime import datetime
from typing import Optional, Literal

# ---------- OpenAI (GPT) ----------
try:
    from openai import OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    _openai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception:
    _openai = None

GPT_INSTRUCTIONS = os.getenv("GPT_INSTRUCTIONS",
    "You are TradeAlertBot. Extract trading intents precisely. "
    "Ask for missing details. Only immediate market orders require confirmation; "
    "conditional and limit/stop orders execute without confirm."
)

# ---------- Timezone (America/New_York) ----------
try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:
    _NY = None

def now_ny_iso() -> str:
    if _NY:
        return datetime.now(_NY).isoformat()
    try:
        from datetime import timezone
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return datetime.utcnow().isoformat()

# ---------- Google Sheets (single spreadsheet, multiple tabs) ----------
GS_SPREADSHEET_ID = os.getenv("GS_SPREADSHEET_ID", "")
GS_EVENTS_TAB = os.getenv("GS_EVENTS_TAB", "Events")
GS_TRADES_TAB = os.getenv("GS_TRADES_TAB", "Trades")
GS_CONVERSATIONS_TAB = os.getenv("GS_CONVERSATIONS_TAB", "Conversations")

_sheets_ok = False
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    svc_json = os.getenv("GS_SERVICE_ACCOUNT_JSON", "")
    creds_dict = json.loads(svc_json) if svc_json else {}
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES)
    _gs_client = gspread.authorize(creds)
    _sheet = _gs_client.open_by_key(GS_SPREADSHEET_ID)

    def _get_or_create_tab(name: str, header: list[str]):
        try:
            ws = _sheet.worksheet(name)
        except Exception:
            ws = _sheet.add_worksheet(name, rows=2000, cols=max(10, len(header)))
            ws.append_row(header)
        # Ensure header exists (only if sheet was empty)
        try:
            first = ws.row_values(1)
            if not first:
                ws.append_row(header)
        except Exception:
            pass
        return ws

    _ws_events = _get_or_create_tab(GS_EVENTS_TAB,
        ["timestamp_NY","kind","direction","actor","channel_id","user_id","payload_json"])
    _ws_trades = _get_or_create_tab(GS_TRADES_TAB,
        ["timestamp_NY","action","symbol","qty","details_json"])
    _ws_convos = _get_or_create_tab(GS_CONVERSATIONS_TAB,
        ["timestamp_NY","channel_id","user_id","user_text","assistant_text"])
    _sheets_ok = True
except Exception as e:
    print("Sheets init failed:", e)
    _ws_events = _ws_trades = _ws_convos = None

def _append_row(ws, row: list):
    if not _sheets_ok or ws is None:
        return False
    try:
        ws.append_row(row)
        return True
    except Exception as e:
        print("Sheets append error:", e)
        return False

def log_event(kind: str, direction: str, actor: str,
              channel_id: Optional[str], user_id: Optional[str], payload) -> None:
    ts = now_ny_iso()
    try:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except Exception:
        data = str(payload)
    _append_row(_ws_events, [ts, kind, direction, actor, str(channel_id or ""), str(user_id or ""), data])

def log_trade(action: str, symbol: str, qty: int, details) -> None:
    ts = now_ny_iso()
    try:
        dj = details if isinstance(details, str) else json.dumps(details, ensure_ascii=False)
    except Exception:
        dj = str(details)
    _append_row(_ws_trades, [ts, action, symbol, int(qty), dj])

def log_conversation(user_text: str, assistant_text: str, channel_id: str, user_id: str) -> None:
    ts = now_ny_iso()
    _append_row(_ws_convos, [ts, channel_id, user_id, user_text or "", assistant_text or ""])

# ---------- Policy toggles ----------
REQUIRE_CONFIRM_MARKET_ONLY = os.getenv("REQUIRE_CONFIRM_MARKET_ONLY","true").strip().lower() in ("1","true","t","yes","y","on")

def needs_confirmation(order_type: str, is_conditional: bool) -> bool:
    if not REQUIRE_CONFIRM_MARKET_ONLY:
        return True
    if is_conditional:
        return False
    return (order_type.lower() == "market")

# ---------- Tradier clients (Live DATA vs Sandbox TRADES) ----------
DATA_BASE = os.getenv("TRADIER_DATA_BASE_URL", "https://api.tradier.com/v1")
DATA_TOKEN = os.getenv("TRADIER_DATA_TOKEN_LIVE", "")
TRADE_BASE = os.getenv("TRADIER_TRADE_BASE_URL", "https://sandbox.tradier.com/v1")
TRADE_TOKEN = os.getenv("TRADIER_TRADE_TOKEN_SANDBOX", "")
TRADE_ACCT  = os.getenv("TRADIER_TRADE_ACCOUNT_ID_SANDBOX", "")

def tradier_data_request(endpoint: str, method: str = "GET", params=None, data=None):
    url = f"{DATA_BASE}{endpoint}"
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
    url = f"{TRADE_BASE}{endpoint}"
    headers = {"Authorization": f"Bearer {TRADE_TOKEN}", "Accept": "application/json"}
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
    return tradier_data_request("/markets/options/chains", params={"symbol": symbol, "expiration": expiry, "greeks":"true"})

def get_history(symbol: str, interval="hour", start: Optional[str]=None, end: Optional[str]=None) -> dict:
    params = {"symbol": symbol, "interval": interval}
    if start: params["start"] = start
    if end: params["end"] = end
    return tradier_data_request("/markets/history", params=params)

# ---------- Trading (SANDBOX) ----------
def place_option_order_by_occ(occ: str, side: str, qty: int, type: str = "market",
                              limit: Optional[float] = None, stop: Optional[float] = None,
                              duration: str = "day") -> dict:
    payload = {
        "class": "option",
        "symbol": occ,           # OCC string accepted by Tradier
        "side": side,            # buy_to_open, sell_to_close, etc.
        "quantity": int(qty),
        "type": type.lower(),
        "duration": duration,
    }
    if limit is not None and type.lower() in ("limit","stop_limit"):
        payload["price"] = float(limit)
    if stop is not None and type.lower() in ("stop","stop_limit"):
        payload["stop"] = float(stop)
    res = tradier_trade_request(f"/accounts/{TRADE_ACCT}/orders", method="POST", data=payload)
    log_trade(side, occ, qty, res)
    return res

def place_equity_order(symbol: str, side: Literal["buy","sell"], quantity: int,
                       type: str = "market", limit: Optional[float] = None,
                       session: str = "REG", duration: str = "day") -> dict:
    payload = {
        "class": "equity",
        "symbol": symbol.upper(),
        "side": side,
        "quantity": int(quantity),
        "type": type.lower(),
        "duration": duration,
        "session": session.upper(),   # REG, EXT
    }
    if limit is not None and type.lower() in ("limit","stop_limit"):
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

# ---------- GPT Orchestrator (simple) ----------
async def gpt_orchestrate(user_text: str, channel_id: str, user_id: str) -> str:
    if not _openai:
        return "GPT not configured. Set OPENAI_API_KEY."
    try:
        log_event("gpt","out","bot", channel_id, user_id, {"prompt": user_text})
        resp = _openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": GPT_INSTRUCTIONS},
                {"role": "user", "content": user_text},
            ]
        )
        reply = resp.choices[0].message.content
        log_event("gpt","in","gpt", channel_id, user_id, {"response": reply})
        return reply
    except Exception as e:
        return f"GPT error: {e}"

# ---------- Discord ----------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
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

    # Simple command example: quote SYMBOL
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
