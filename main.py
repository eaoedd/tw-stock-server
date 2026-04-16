from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import urllib.request
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional

import yfinance as yf
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

_yf_lock = threading.Lock()

app = FastAPI(title="TW Stock History Server")
app.mount("/static", StaticFiles(directory="static"), name="static")

CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)

# Cache is considered stale after this many hours
CACHE_TTL_HOURS = 24

# ── TWSE/TPEX Chinese name table (refreshed daily) ──────────────────────────
_TW_NAMES_FILE = CACHE_DIR / "tw_names.json"   # {code: zh_name}
_tw_names: dict[str, str] = {}
_tw_names_fetched_at: Optional[datetime] = None


def _fetch_tw_names() -> dict[str, str]:
    """Download Chinese name tables from TWSE and TPEX."""
    names: dict[str, str] = {}
    sources = [
        # TWSE listed (main board + ETFs)
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
        # TPEX listed (OTC)
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis",
    ]
    for url in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                rows = json.loads(r.read())
            for row in rows:
                code = row.get("Code") or row.get("SecuritiesCompanyCode", "")
                name = row.get("Name") or row.get("CompanyName", "")
                if code and name:
                    names[code.strip()] = name.strip()
        except Exception:
            pass
    return names


def _ensure_tw_names() -> None:
    global _tw_names, _tw_names_fetched_at
    now = datetime.now()
    # Use in-memory table if fresh
    if _tw_names_fetched_at and now - _tw_names_fetched_at < timedelta(hours=24):
        return
    # Try loading from file cache
    if _TW_NAMES_FILE.exists():
        try:
            payload = json.loads(_TW_NAMES_FILE.read_text())
            cached_at = datetime.fromisoformat(payload["cached_at"])
            if now - cached_at < timedelta(hours=24):
                _tw_names = payload["names"]
                _tw_names_fetched_at = cached_at
                return
        except Exception:
            pass
    # Fetch fresh
    names = _fetch_tw_names()
    if names:
        _tw_names = names
        _tw_names_fetched_at = now
        _TW_NAMES_FILE.write_text(
            json.dumps({"cached_at": now.isoformat(), "names": names}, ensure_ascii=False)
        )


# ── Per-ticker name cache (en from yfinance; 30-day TTL) ────────────────────
_NAMES_FILE = CACHE_DIR / "names.json"
_NAMES_TTL_DAYS = 30
_names_cache: dict = {}


def _load_names_file() -> None:
    if _NAMES_FILE.exists():
        try:
            _names_cache.update(json.loads(_NAMES_FILE.read_text()))
        except Exception:
            pass


def _save_names_file() -> None:
    _NAMES_FILE.write_text(json.dumps(_names_cache, ensure_ascii=False))


def _fetch_en_name(symbol: str) -> str:
    """Fetch English longName from yfinance; returns symbol string on failure."""
    try:
        with _yf_lock:
            info = yf.Ticker(symbol).info
        return info.get("longName") or info.get("shortName") or ""
    except Exception:
        return ""


def get_ticker_names(raw: str) -> dict:
    """Return {zh, en} display names for a bare ticker (no suffix)."""
    _ensure_tw_names()
    key = raw.upper()

    zh = _tw_names.get(key, "")

    # English name: serve from cache or fetch
    entry = _names_cache.get(key, {})
    cached_at_str = entry.get("cached_at")
    en = entry.get("en", "")
    if not en or (
        cached_at_str
        and datetime.now() - datetime.fromisoformat(cached_at_str) > timedelta(days=_NAMES_TTL_DAYS)
    ):
        en = _fetch_en_name(key + ".TW") or _fetch_en_name(key + ".TWO") or ""
        _names_cache[key] = {"en": en, "cached_at": datetime.now().isoformat()}
        _save_names_file()

    return {"zh": zh, "en": en}


_load_names_file()
# Pre-load TWSE names in background so first request is fast
threading.Thread(target=_ensure_tw_names, daemon=True).start()


# ── Real-time quote cache (TTL = 5 s, in-memory) ────────────────────────────
_rt_cache: dict[str, dict] = {}
RT_CACHE_TTL = 5  # seconds

def _is_tw_market_open() -> bool:
    """True if current Asia/Taipei time is a weekday between 09:00 and 13:30.
    Asia/Taipei is UTC+8 with no DST."""
    now_utc = datetime.utcnow()
    now_tw  = now_utc + timedelta(hours=8)
    if now_tw.weekday() >= 5:
        return False
    t = now_tw.time()
    return dtime(9, 0) <= t <= dtime(13, 30)


def _fetch_twse_quote(ticker: str) -> Optional[dict]:
    """Fetch near-real-time quote from TWSE MIS API. Tries TSE then OTC."""
    bare = ticker.upper().strip()
    for prefix in ("tse", "otc"):
        url = (
            f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
            f"?ex_ch={prefix}_{bare}.tw&json=1&delay=0"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                payload = json.loads(r.read())
            items = payload.get("msgArray", [])
            if not items:
                continue
            item = items[0]
            z = item.get("z", "-")
            if not z or z == "-":
                continue
            return {
                "ticker":      bare,
                "price":       float(z),
                "prev_close":  float(item.get("y") or 0),
                "open":        float(item.get("o") or 0),
                "high":        float(item.get("h") or 0),
                "low":         float(item.get("l") or 0),
                "volume":      int(float(item.get("v") or 0)),
                "name_zh":     item.get("n", ""),
                "update_time": item.get("t", ""),
                "exchange":    prefix,
                "market_open": True,
            }
        except Exception:
            continue
    return None


def ticker_to_tw(ticker: str) -> str:
    """Append .TW suffix if not already present."""
    ticker = ticker.upper().strip()
    if not ticker.endswith(".TW"):
        ticker += ".TW"
    return ticker


def cache_path(ticker: str) -> Path:
    safe = ticker.replace(".", "_")
    return CACHE_DIR / f"{safe}.json"


def load_cache(ticker: str, ignore_ttl: bool = False) -> Optional[dict]:
    path = cache_path(ticker)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if not ignore_ttl:
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at > timedelta(hours=CACHE_TTL_HOURS):
            return None  # stale
    return data


def save_cache(ticker: str, records: list[dict]) -> None:
    path = cache_path(ticker)
    payload = {
        "ticker": ticker,
        "cached_at": datetime.now().isoformat(),
        "records": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False))


def _yf_download(symbol: str, period: str, timeout: int = 12):
    """Wrapper around yf.download — serialized with a lock because yfinance is not thread-safe.
    Runs in a separate thread so we can enforce a hard timeout."""
    import concurrent.futures
    import pandas as pd

    def _do():
        try:
            with _yf_lock:
                return yf.download(symbol, period=period, auto_adjust=True, progress=False)
        except Exception:
            return pd.DataFrame()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_do)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return pd.DataFrame()


def fetch_history(ticker: str, period: str) -> list[dict]:
    """Download history from Yahoo Finance and return as list of dicts."""
    raw = ticker.upper().strip()
    # Strip any existing exchange suffix to get the bare ticker
    for suffix in (".TWO", ".TW"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break

    tw_ticker = raw + ".TW"
    df = _yf_download(tw_ticker, period)
    if df.empty:
        two_ticker = raw + ".TWO"
        df = _yf_download(two_ticker, period)
        if df.empty:
            raise HTTPException(status_code=404, detail=f"No data found for {tw_ticker} or {two_ticker}")
        tw_ticker = two_ticker
    # Flatten multi-level columns produced by newer yfinance versions
    if isinstance(df.columns, __import__("pandas").MultiIndex):
        df.columns = [col[0] for col in df.columns]
    df = df.reset_index()
    import math

    def safe_float(v):
        f = float(v)
        return None if math.isnan(f) else round(f, 2)

    records = []
    for _, row in df.iterrows():
        close = safe_float(row["Close"])
        if close is None:
            continue  # skip incomplete rows (e.g. today's partial data)
        records.append({
            "date": str(row["Date"])[:10],
            "open": safe_float(row["Open"]),
            "high": safe_float(row["High"]),
            "low": safe_float(row["Low"]),
            "close": close,
            "volume": int(row["Volume"]),
        })
    return records


# ── Auth ─────────────────────────────────────────────────────────────────────
_sessions: dict[str, dict] = {}   # token → {username, expires_at}


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + ":" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 100_000)
        return secrets.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "username": username,
        "expires_at": (datetime.now() + timedelta(days=30)).isoformat(),
    }
    return token


def _require_auth(x_auth_token: str = Header(default="")) -> str:
    session = _sessions.get(x_auth_token)
    if not session or datetime.now() > datetime.fromisoformat(session["expires_at"]):
        _sessions.pop(x_auth_token, None)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return session["username"]


# ── User / Watchlist / Portfolio endpoints ───────────────────────────────────
USERS_DIR = CACHE_DIR / "users"
USERS_DIR.mkdir(exist_ok=True)


def _user_path(username: str) -> Path:
    safe = "".join(c for c in username if c.isalnum() or c in "-_.")
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid username")
    return USERS_DIR / f"{safe}.json"


def _read_user(path: Path) -> dict:
    data = json.loads(path.read_text())
    changed = False
    if "password_hash" not in data:          # migrate old files
        data["password_hash"] = _hash_password("")
        changed = True
    if "portfolios" not in data:
        data["portfolios"] = []
        changed = True
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False))
    return data


def _write_user(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False))


@app.get("/me")
def get_me(current_user: str = Depends(_require_auth)):
    return {"username": current_user}


@app.post("/login")
def login(body: dict):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    if not username:
        raise HTTPException(status_code=400, detail="Username required")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=401, detail="Invalid username or password")
    data = _read_user(path)
    if not _verify_password(password, data["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"token": _create_session(username), "username": username}


@app.post("/users/{username}")
def create_user(username: str, body: dict):
    path = _user_path(username)
    if path.exists():
        raise HTTPException(status_code=409, detail="User already exists")
    password = body.get("password", "")
    if not password:
        raise HTTPException(status_code=400, detail="Password required")
    _write_user(path, {
        "password_hash": _hash_password(password),
        "tickers": [],
        "portfolios": [],
    })
    return {"token": _create_session(username), "username": username}


@app.get("/watchlist/{username}")
def get_watchlist(username: str, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    return {"tickers": _read_user(path).get("tickers", [])}


@app.put("/watchlist/{username}")
def save_watchlist(username: str, body: dict, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    data["tickers"] = [str(t).upper() for t in body.get("tickers", []) if t]
    _write_user(path, data)
    return {"ok": True}


@app.get("/portfolios/{username}")
def get_portfolios(username: str, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    return {"portfolios": _read_user(path).get("portfolios", [])}


@app.put("/portfolios/{username}")
def save_portfolios(username: str, body: dict, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    data["portfolios"] = body.get("portfolios", [])
    _write_user(path, data)
    return {"ok": True}


@app.get("/trades/{username}")
def get_trades(username: str, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    return {"trades": _read_user(path).get("trades", [])}


@app.post("/trades/{username}")
def add_trade(username: str, body: dict, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    trade = {
        "id":       secrets.token_hex(8),
        "date":     str(body.get("date", "")),
        "ticker":   str(body.get("ticker", "")).upper(),
        "action":   str(body.get("action", "buy")).lower(),
        "shares":   float(body.get("shares", 0)),
        "price":    float(body.get("price", 0)),
        "tax":      float(body.get("tax", 0)),
        "fee":      float(body.get("fee", 0)),
        "note":     str(body.get("note", "")),
        "linkedId": str(body.get("linkedId", "")),
    }
    data.setdefault("trades", []).append(trade)
    _write_user(path, data)
    return {"trade": trade}


@app.put("/trades/{username}/{trade_id}")
def update_trade(username: str, trade_id: str, body: dict, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    trades = data.get("trades", [])
    for t in trades:
        if t["id"] == trade_id:
            t["date"]   = str(body.get("date",   t["date"]))
            t["ticker"] = str(body.get("ticker", t["ticker"])).upper()
            t["action"] = str(body.get("action", t["action"])).lower()
            t["shares"] = float(body.get("shares", t["shares"]))
            t["price"]  = float(body.get("price",  t["price"]))
            t["tax"]      = float(body.get("tax",      t["tax"]))
            t["fee"]      = float(body.get("fee",      t["fee"]))
            t["note"]     = str(body.get("note",      t["note"]))
            t["linkedId"] = str(body.get("linkedId",  t.get("linkedId", "")))
            _write_user(path, data)
            return {"trade": t}
    raise HTTPException(status_code=404, detail="Trade not found")


@app.delete("/trades/{username}/{trade_id}")
def delete_trade(username: str, trade_id: str, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    trades = data.get("trades", [])
    deleted = {trade_id}
    # cascade: also delete any trade linked to this one
    for t in trades:
        if t.get("linkedId") == trade_id:
            deleted.add(t["id"])
    data["trades"] = [t for t in trades if t["id"] not in deleted]
    _write_user(path, data)
    return {"ok": True, "deleted": list(deleted)}


@app.get("/users/{username}/export")
def export_user(username: str, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    export = {
        "username": username,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "trades":     data.get("trades", []),
        "watchlist":  data.get("tickers", []),
        "portfolios": data.get("portfolios", []),
    }
    from fastapi.responses import Response
    return Response(
        content=json.dumps(export, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{username}_backup.json"'},
    )


@app.post("/users/{username}/import")
def import_user(username: str, body: dict, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    data = _read_user(path)
    if "trades" in body:
        data["trades"] = body["trades"]
    if "watchlist" in body:
        data["tickers"] = body["watchlist"]
    if "portfolios" in body:
        data["portfolios"] = body["portfolios"]
    _write_user(path, data)
    return {"ok": True, "trades": len(data.get("trades", [])),
            "watchlist": len(data.get("tickers", [])),
            "portfolios": len(data.get("portfolios", []))}


@app.delete("/users/{username}")
def delete_user(username: str, current_user: str = Depends(_require_auth)):
    if current_user != username:
        raise HTTPException(status_code=403, detail="Forbidden")
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    path.unlink()
    for tok, s in list(_sessions.items()):
        if s["username"] == username:
            del _sessions[tok]
    return {"deleted": username}


@app.get("/history/{ticker}")
def get_history(
    ticker: str,
    period: str = Query(default="1y", description="yfinance period: 1mo 3mo 6mo 1y 2y 5y max"),
    refresh: bool = Query(default=False, description="Force re-fetch ignoring cache"),
):
    """Return OHLCV history for a Taiwan stock/ETF ticker."""
    cache_key = f"{ticker.upper()}_{period}"

    # Always serve fresh cache (within TTL) — no need to hit yfinance, even if refresh=true
    cached = load_cache(cache_key)
    if cached:
        return {**cached, "source": "cache"}

    # No fresh cache — serve stale if refresh not requested, otherwise try yfinance
    if not refresh:
        stale = load_cache(cache_key, ignore_ttl=True)
        if stale:
            return {**stale, "source": "cache_stale"}

    # refresh=true or no cache at all — try yfinance
    try:
        records = fetch_history(ticker, period)
    except HTTPException:
        records = []

    if records:
        save_cache(cache_key, records)
        tw_ticker = ticker_to_tw(ticker)
        return {
            "ticker": tw_ticker,
            "period": period,
            "cached_at": datetime.now().isoformat(),
            "records": records,
            "source": "fetched",
        }

    # yfinance failed — fall back to stale cache as last resort
    stale = load_cache(cache_key, ignore_ttl=True)
    if stale:
        return {**stale, "source": "cache_stale"}

    raise HTTPException(status_code=404, detail=f"No data found for {ticker}")


@app.get("/names")
def get_names(tickers: str = Query(..., description="Comma-separated bare tickers, e.g. 0050,00675L")):
    """Return {zh, en} display names for a list of tickers."""
    result = {}
    for raw in tickers.split(","):
        raw = raw.strip().upper()
        if raw:
            result[raw] = get_ticker_names(raw)
    return {"names": result}


@app.get("/quotes")
def get_quotes(tickers: str = Query(..., description="Comma-separated tickers, e.g. 0050,00675L")):
    """Return latest price + daily change for a list of tickers."""
    import math

    results = []
    for raw in tickers.split(","):
        raw = raw.strip()
        if not raw:
            continue
        tw_ticker = ticker_to_tw(raw)
        try:
            df = _yf_download(tw_ticker, "5d")
            if isinstance(df.columns, __import__("pandas").MultiIndex):
                df.columns = [col[0] for col in df.columns]
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                results.append({"ticker": raw.upper(), "error": "insufficient data"})
                continue
            last  = df.iloc[-1]
            prev  = df.iloc[-2]
            close = round(float(last["Close"]), 2)
            chg   = round(close - float(prev["Close"]), 2)
            pct   = round(chg / float(prev["Close"]) * 100, 2)
            results.append({
                "ticker": raw.upper(),
                "close": close,
                "change": chg,
                "change_pct": pct,
                "date": str(df.index[-1])[:10],
            })
        except Exception as e:
            results.append({"ticker": raw.upper(), "error": str(e)})
    return {"quotes": results}


@app.get("/quote/{ticker}")
def get_realtime_quote(ticker: str):
    """Return near-real-time TWSE/TPEX quote. Cached in-memory for 5 seconds."""
    bare = ticker.upper().strip()
    market_open = _is_tw_market_open()

    if not market_open:
        cached = _rt_cache.get(bare)
        if cached:
            return {**cached["data"], "market_open": False, "stale": True}
        return {"ticker": bare, "market_open": False, "price": None}

    cached = _rt_cache.get(bare)
    if cached and (datetime.now() - cached["fetched_at"]).total_seconds() < RT_CACHE_TTL:
        return cached["data"]

    data = _fetch_twse_quote(bare)
    if data:
        _rt_cache[bare] = {"data": data, "fetched_at": datetime.now()}
        return data

    if cached:
        return {**cached["data"], "market_open": True, "stale": True}

    raise HTTPException(status_code=404, detail=f"No real-time data for {bare}")


@app.get("/cache")
def list_cache():
    """List all cached tickers."""
    items = []
    for f in CACHE_DIR.glob("*.json"):
        data = json.loads(f.read_text())
        items.append({
            "ticker": data["ticker"],
            "cached_at": data["cached_at"],
            "records": len(data["records"]),
        })
    return {"cached": items}


@app.delete("/cache/{ticker}")
def delete_cache(ticker: str, period: str = Query(default="1y")):
    """Delete cached data for a ticker."""
    cache_key = f"{ticker.upper()}_{period}"
    path = cache_path(cache_key)
    if path.exists():
        path.unlink()
        return {"deleted": str(path.name)}
    raise HTTPException(status_code=404, detail="Cache not found")


@app.get("/", response_class=FileResponse)
def index():
    return FileResponse("static/index.html")
