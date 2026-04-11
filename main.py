from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yfinance as yf
from fastapi import FastAPI, HTTPException, Query
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


def ticker_to_tw(ticker: str) -> str:
    """Append .TW suffix if not already present."""
    ticker = ticker.upper().strip()
    if not ticker.endswith(".TW"):
        ticker += ".TW"
    return ticker


def cache_path(ticker: str) -> Path:
    safe = ticker.replace(".", "_")
    return CACHE_DIR / f"{safe}.json"


def load_cache(ticker: str) -> Optional[dict]:
    path = cache_path(ticker)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
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


def _yf_download(symbol: str, period: str):
    """Wrapper around yf.download — serialized with a lock because yfinance is not thread-safe."""
    try:
        with _yf_lock:
            return yf.download(symbol, period=period, auto_adjust=True, progress=False)
    except Exception:
        import pandas as pd
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


# ── User / Watchlist endpoints ───────────────────────────────────────────────
USERS_DIR = CACHE_DIR / "users"
USERS_DIR.mkdir(exist_ok=True)

DEFAULT_WATCHLIST: list = []


def _user_path(username: str) -> Path:
    safe = "".join(c for c in username if c.isalnum() or c in "-_.")
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid username")
    return USERS_DIR / f"{safe}.json"


@app.get("/users")
def list_users():
    users = sorted(f.stem for f in USERS_DIR.glob("*.json"))
    return {"users": users}


@app.post("/users/{username}")
def create_user(username: str):
    path = _user_path(username)
    if path.exists():
        raise HTTPException(status_code=409, detail="User already exists")
    path.write_text(json.dumps({"tickers": DEFAULT_WATCHLIST}, ensure_ascii=False))
    return {"created": username}


@app.get("/watchlist/{username}")
def get_watchlist(username: str):
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    return json.loads(path.read_text())


@app.put("/watchlist/{username}")
def save_watchlist(username: str, body: dict):
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    tickers = [str(t).upper() for t in body.get("tickers", []) if t]
    path.write_text(json.dumps({"tickers": tickers}, ensure_ascii=False))
    return {"ok": True}


@app.delete("/users/{username}")
def delete_user(username: str):
    path = _user_path(username)
    if not path.exists():
        raise HTTPException(status_code=404, detail="User not found")
    path.unlink()
    return {"deleted": username}


@app.get("/history/{ticker}")
def get_history(
    ticker: str,
    period: str = Query(default="1y", description="yfinance period: 1mo 3mo 6mo 1y 2y 5y max"),
    refresh: bool = Query(default=False, description="Force re-fetch ignoring cache"),
):
    """Return OHLCV history for a Taiwan stock/ETF ticker."""
    cache_key = f"{ticker.upper()}_{period}"

    if not refresh:
        cached = load_cache(cache_key)
        if cached:
            return {**cached, "source": "cache"}

    records = fetch_history(ticker, period)
    save_cache(cache_key, records)
    tw_ticker = ticker_to_tw(ticker)
    return {
        "ticker": tw_ticker,
        "period": period,
        "cached_at": datetime.now().isoformat(),
        "records": records,
        "source": "fetched",
    }


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
