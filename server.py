#!/usr/bin/env python3
"""Local X retail mood app. Token stays in .env on this machine."""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from classify import SKIP_WHY, ScoreError, classify_posts, score_from_counts, scoring_ready, scorer_info, write_thesis  # noqa: E402

STATIC = ROOT / "static"
RESULTS = ROOT / "results"
HISTORY = ROOT / "history.jsonl"
TZ = ZoneInfo("America/Edmonton")
TICKER_RE = re.compile(r"^[A-Za-z0-9.]{1,12}$")
RESULTS.mkdir(exist_ok=True)

app = FastAPI(title="Xmood")


class PullIn(BaseModel):
    ticker: str
    symbol: str | None = None
    name: str | None = None
    confirmed: bool = False


def _now() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M America/Edmonton")


def _when(iso: str) -> str:
    raw = (iso or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return raw
    return dt.strftime("%Y-%m-%d %H:%M")


def _token() -> str:
    return (os.environ.get("X_BEARER_TOKEN") or "").strip()


_CA_LISTING_SUFFIXES = (".CN", ".V", ".TO", ".NE")


def _quote_row(row: dict) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or "").strip()
    if not sym:
        return None
    longname = str(row.get("longname") or "").strip()
    shortname = str(row.get("shortname") or "").strip()
    return {
        "symbol": sym,
        "name": longname or shortname,
        "exchange": str(row.get("exchDisp") or row.get("exchange") or "").strip(),
        "type": str(row.get("quoteType") or "").strip(),
    }


def _yahoo_quotes(client, q: str) -> list[dict[str, str]]:
    r = client.get(
        "https://query1.finance.yahoo.com/v1/finance/search",
        params={"q": q, "quotesCount": 12, "newsCount": 0},
    )
    if r.status_code >= 400:
        return []
    out: list[dict[str, str]] = []
    for row in (r.json() or {}).get("quotes") or []:
        parsed = _quote_row(row)
        if parsed:
            out.append(parsed)
    return out


def _yahoo_lookup(q: str) -> list[dict[str, str]]:
    import httpx

    q = (q or "").strip()
    if not q:
        return []
    undotted = "." not in q
    queries = [q]
    if undotted:
        root = q.upper()
        queries.extend(root + suf for suf in _CA_LISTING_SUFFIXES)
    headers = {"User-Agent": "Mozilla/5.0"}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    with httpx.Client(timeout=20.0, headers=headers) as client:
        for query in queries:
            for row in _yahoo_quotes(client, query):
                key = row["symbol"].upper()
                if key in seen:
                    continue
                seen.add(key)
                out.append(row)
    if undotted:
        root = q.upper()
        out = [
            row
            for row in out
            if (sym := row["symbol"].upper()) == root or sym.startswith(root + ".")
        ]

        def _ca_first(row: dict[str, str]) -> int:
            sym = row["symbol"].upper()
            if any(sym == root + suf for suf in _CA_LISTING_SUFFIXES):
                return 0
            return 1

        out.sort(key=_ca_first)
    return out


def _yahoo_news(q: str) -> list[str]:
    import httpx

    q = (q or "").strip()
    if not q:
        return []
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=20.0, headers=headers) as client:
        r = client.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": 4, "newsCount": 12},
        )
    if r.status_code >= 400:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in (r.json() or {}).get("news") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        key = title.casefold()
        if key in seen:
            continue
        seen.add(key)
        publisher = str(row.get("publisher") or "").strip()
        headline = f"{title} ({publisher})" if publisher else title
        out.append(headline)
        if len(out) >= 10:
            break
    return out


def _query(symbol: str, name: str) -> str:
    # Letter-only tags use the X cashtag. A dotted tag is quoted so $CH.V is not read as $CH.
    tag = symbol.strip()
    if re.fullmatch(r"[A-Za-z0-9]+", tag):
        cashtag = "$" + tag
    else:
        cashtag = '"$' + tag + '"'
    return cashtag + " -is:retweet"


def _post_hits_symbol(text: str, symbol: str, name: str) -> bool:
    blob = text or ""
    pat = re.compile(r"\$" + re.escape(symbol) + r"(?![A-Za-z0-9])", re.I)
    if pat.search(blob):
        return True
    if name and name.lower() in blob.lower():
        return True
    return False


def _translate(text: str, lang: str | None) -> tuple[str, bool]:
    if not text:
        return "", False
    if not lang or lang.lower().startswith("en"):
        return text, False
    try:
        from deep_translator import GoogleTranslator

        en = GoogleTranslator(source="auto", target="en").translate(text)
        if en and en.strip() and en.strip() != text.strip():
            return en, True
    except Exception:
        pass
    return text, False


def _load_history(ticker: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not HISTORY.is_file():
        return out
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("ticker", "")).upper() == ticker.upper():
            out.append(row)
    return out[-40:]


def _append_history(row: dict[str, Any]) -> None:
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _search_x(query: str, token: str) -> list[dict[str, Any]]:
    import httpx

    params = {
        "query": query,
        "max_results": 100,
        "tweet.fields": "id,text,created_at,lang,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=45.0) as client:
        r = client.get("https://api.x.com/2/tweets/search/recent", params=params, headers=headers)
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="X token was rejected. Check .env on this machine.")
    if r.status_code == 403:
        raise HTTPException(status_code=403, detail="X refused the search. The token may lack search access.")
    if r.status_code == 429:
        raise HTTPException(status_code=429, detail="X rate limit. Wait a minute and pull again.")
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"X search failed ({r.status_code}).")
    body = r.json() or {}
    users = {}
    for u in ((body.get("includes") or {}).get("users") or []):
        if isinstance(u, dict) and u.get("id"):
            users[str(u["id"])] = str(u.get("username") or "").lstrip("@")
    out = []
    for tw in body.get("data") or []:
        if not isinstance(tw, dict):
            continue
        row = dict(tw)
        row["username"] = users.get(str(tw.get("author_id") or ""), "")
        out.append(row)
    return out


def _payload(symbol: str, name: str, query: str, raw: list[dict[str, Any]], note=None) -> dict[str, Any]:
    posts: list[dict[str, Any]] = []
    dropped_wrong = 0
    hits = [tw for tw in raw if _post_hits_symbol(tw.get("text") or "", symbol, name)]
    dropped_wrong = len(raw) - len(hits)
    n_hits = len(hits)
    for i, tw in enumerate(hits, 1):
        if note and i == 1:
            note("Searching X…")
        text = tw.get("text") or ""
        lang = tw.get("lang")
        text_en, translated = _translate(text, lang)
        pid = str(tw.get("id") or "")
        posts.append(
            {
                "id": pid,
                "url": f"https://x.com/i/web/status/{pid}" if pid else "",
                "created_at": _when(str(tw.get("created_at") or "")),
                "text": text,
                "text_original": text,
                "text_en": text_en,
                "lang": lang or "",
                "translated": translated,
                "username": str(tw.get("username") or "").lstrip("@"),
            }
        )
    def _score_progress(done: int, total: int) -> None:
        if note:
            if done <= 0:
                note("Scoring…")
            else:
                note(f"Scoring {done}/{total}")

    try:
        if note:
            note("Scoring…")
        classified = classify_posts(posts, symbol=symbol, name=name, on_progress=_score_progress)
    except ScoreError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    kept = [p for p in classified if p.get("classification") != "spam"]
    spam_n = len(classified) - len(kept)
    bull = sum(1 for p in kept if p.get("classification") == "bull")
    bear = sum(1 for p in kept if p.get("classification") == "bear")
    neut = sum(1 for p in kept if p.get("classification") == "neutral")
    score, label = score_from_counts(bull, bear)
    if not classified:
        label = "No posts matched that exact tag"
    facts: list[str] = []
    seen_facts: set[str] = set()

    def _add_fact(item: str) -> None:
        t = (item or "").strip()
        if not t:
            return
        key = t.casefold()
        if key in seen_facts:
            return
        seen_facts.add(key)
        facts.append(t)

    for headline in _yahoo_news(symbol) + (_yahoo_news(name) if name else []):
        if len(facts) >= 16:
            break
        _add_fact(headline)
    for p in kept:
        if len(facts) >= 16:
            break
        why = str(p.get("reason") or "").strip()
        if not why or why == SKIP_WHY:
            continue
        _add_fact(why)
    facts = facts[:16]
    if note:
        note("Writing thesis…")
    try:
        thesis = write_thesis(
            symbol,
            name,
            score=score,
            label=label,
            bull=bull,
            bear=bear,
            neutral=neut,
            facts=facts,
        )
    except Exception:
        thesis = {"summary": "", "bull": "", "bear": ""}
    as_of = _now()
    result = {
        "ticker": symbol,
        "display_ticker": symbol,
        "company": name,
        "as_of": as_of,
        "window": "last 7 days",
        "query": query,
        "credits_estimate_usd": round(0.005 * len(raw), 3),
        "credits_note": "100 posts is $0.50",
        "n_fetched": len(raw),
        "n_dropped_wrong_symbol": dropped_wrong,
        "n_kept": len(kept),
        "n_spam": spam_n,
        "bull": bull,
        "bear": bear,
        "neutral": neut,
        "score": score,
        "label": label,
        "thesis": {
            "summary": str((thesis or {}).get("summary") or ""),
            "bull": str((thesis or {}).get("bull") or ""),
            "bear": str((thesis or {}).get("bear") or ""),
        },
        "status": "complete",
        "scorer": scorer_info().get("scorer"),
        "scorer_path": scorer_info().get("path"),
        "scorer_detail": scorer_info().get("detail"),
        "posts": kept[:12] if kept else classified[:12],
        "history": [],
    }
    _append_history(
        {
            "as_of": as_of,
            "ticker": symbol,
            "score": score,
            "bull": bull,
            "bear": bear,
            "neutral": neut,
            "n_kept": len(kept),
        }
    )
    result["history"] = _load_history(symbol)
    (RESULTS / "current.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
    (RESULTS / f"{safe}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


@app.get("/api/scorer")
def api_scorer() -> JSONResponse:
    return JSONResponse(scorer_info())


@app.get("/api/lookup")
def api_lookup(q: str = "") -> JSONResponse:
    return JSONResponse({"matches": _yahoo_lookup(q)})


@app.post("/api/pull")
def api_pull(body: PullIn):
    ticker = (body.ticker or "").strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="Ticker must be letters, digits, or dots.")
    token = _token()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Add X_BEARER_TOKEN to the .env file next to this app.",
        )
    if not scoring_ready():
        raise HTTPException(
            status_code=400,
            detail="Scoring uses the grok command on this computer. It was not found on PATH. Set GROK_BIN in .env if needed.",
        )
    matches = _yahoo_lookup(body.symbol or ticker)
    exact = [m for m in matches if m["symbol"].upper() == ticker.upper()]
    chosen = None
    if body.confirmed and (body.symbol or ticker):
        chosen = {
            "symbol": (body.symbol or ticker).strip(),
            "name": (body.name or "").strip(),
        }
        if not chosen["name"]:
            hit = [m for m in matches if m["symbol"].upper() == chosen["symbol"].upper()]
            if hit:
                chosen["name"] = hit[0]["name"]
    elif exact and ("." in ticker or len(matches) == 1):
        chosen = exact[0]
    elif len(matches) == 1:
        chosen = matches[0]
    elif len(matches) > 1:
        return JSONResponse(
            {
                "status": "pick",
                "detail": "Several listings match. Pick one, then Pull runs that exact tag.",
                "matches": matches,
            }
        )
    else:
        chosen = {"symbol": ticker, "name": ""}

    symbol = chosen["symbol"]
    name = chosen.get("name") or ""
    query = _query(symbol, name)
    q = queue.Queue()

    def note(msg: str) -> None:
        q.put(("status", msg))

    def work() -> None:
        try:
            note("Searching X…")
            raw = _search_x(query, token)
            result = _payload(symbol, name, query, raw, note=note)
            q.put(("done", result))
        except HTTPException as e:
            q.put(("error", e.detail))
        except Exception as e:
            q.put(("error", str(e)))

    def events():
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while True:
            kind, payload = q.get()
            if kind == "status":
                yield json.dumps({"t": "status", "text": payload}) + "\n"
            elif kind == "error":
                yield json.dumps({"t": "error", "detail": payload}) + "\n"
                break
            else:
                yield json.dumps({"t": "done", "result": payload}) + "\n"
                break
        t.join(timeout=2)

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/status")
def api_status() -> JSONResponse:
    path = RESULTS / "current.json"
    if not path.is_file():
        return JSONResponse({"status": "idle", "label": "No pull yet", "posts": []})
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=8787, reload=False)
