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
from queries import canonical_industry, load_taxonomy, parent_sector, reddit_sector_query, reddit_symbol_query, resolve_sector_subject, sector_query, symbol_query  # noqa: E402
from reddit import RedditSearchError, search_reddit  # noqa: E402

STATIC = ROOT / "static"
RESULTS = ROOT / "results"
HISTORY = ROOT / "history.jsonl"
TZ = ZoneInfo("America/Edmonton")
TICKER_RE = re.compile(r"^[A-Za-z0-9.]{1,12}$")
RESULTS.mkdir(exist_ok=True)

app = FastAPI(title="Xmood")


class PullIn(BaseModel):
    ticker: str = ""
    symbol: str | None = None
    name: str | None = None
    confirmed: bool = False
    mode: str = "symbol"
    industry: str | None = None
    sector: str | None = None


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


def _socialcrawl_key() -> str:
    return (os.environ.get("SOCIALCRAWL_API_KEY") or "").strip()


def _quote_row(row: dict) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    sym = str(row.get("symbol") or "").strip()
    if not sym or "=" in sym:
        return None
    longname = str(row.get("longname") or row.get("longName") or "").strip()
    shortname = str(row.get("shortname") or row.get("shortName") or row.get("name") or "").strip()
    industry = canonical_industry(str(row.get("industryDisp") or row.get("industry") or ""))
    sector = str(row.get("sectorDisp") or row.get("sector") or "").strip()
    if industry and not sector:
        sector = parent_sector(industry)
    return {
        "symbol": sym,
        "name": longname or shortname,
        "exchange": str(row.get("exchDisp") or row.get("exchange") or "").strip(),
        "type": str(row.get("quoteType") or row.get("typeDisp") or "").strip(),
        "industry": industry,
        "sector": sector,
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


def _yahoo_lookup_docs(client, q: str) -> list[dict[str, str]]:
    r = client.get(
        "https://query1.finance.yahoo.com/v1/finance/lookup",
        params={"query": q, "type": "equity", "count": 100},
    )
    if r.status_code >= 400:
        return []
    out: list[dict[str, str]] = []
    for result in ((r.json() or {}).get("finance") or {}).get("result") or []:
        if not isinstance(result, dict):
            continue
        for row in result.get("documents") or []:
            parsed = _quote_row(row)
            if parsed:
                out.append(parsed)
    return out


def _keeps_typed(sym: str, typed: str) -> bool:
    typed_u = (typed or "").strip().upper()
    s = (sym or "").strip().upper()
    if not typed_u or not s or "=" in s:
        return False
    if s == typed_u:
        return True
    root = s.split(".", 1)[0]
    return root == typed_u


_NON_EQUITY_MARKERS = ("ETF", "FUTURE", "INDEX", "MUTUALFUND", "OPTION", "CRYPTOCURRENCY")


def _prefer_equity(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    kept: list[dict[str, str]] = []
    for row in rows:
        t = (row.get("type") or "").strip().upper()
        if any(marker in t for marker in _NON_EQUITY_MARKERS):
            continue
        kept.append(row)
    return kept


def _name_needs_fill(name: str) -> bool:
    n = (name or "").strip()
    return not n or " " not in n


def _fill_empty_names(client, rows: list[dict[str, str]]) -> None:
    for row in rows:
        if not _name_needs_fill(row.get("name") or ""):
            continue
        sym = row["symbol"]
        for hit in _yahoo_quotes(client, sym):
            if hit["symbol"].upper() != sym.upper():
                continue
            if hit.get("name"):
                row["name"] = hit["name"]
            if hit.get("exchange") and not row.get("exchange"):
                row["exchange"] = hit["exchange"]
            if hit.get("type") and not row.get("type"):
                row["type"] = hit["type"]
            if hit.get("industry") and not row.get("industry"):
                row["industry"] = hit["industry"]
            if hit.get("sector") and not row.get("sector"):
                row["sector"] = hit["sector"]
            break
        if (row.get("name") or "").strip():
            continue
        r = client.get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": sym},
        )
        if r.status_code >= 400:
            continue
        for qrow in ((r.json() or {}).get("quoteResponse") or {}).get("result") or []:
            if not isinstance(qrow, dict):
                continue
            if str(qrow.get("symbol") or "").upper() != sym.upper():
                continue
            name = str(qrow.get("longName") or qrow.get("shortName") or "").strip()
            if name:
                row["name"] = name
            break


def _yahoo_lookup(q: str) -> list[dict[str, str]]:
    import httpx

    q = (q or "").strip()
    if not q:
        return []
    headers = {"User-Agent": "Mozilla/5.0"}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    with httpx.Client(timeout=20.0, headers=headers) as client:
        rows = list(_yahoo_quotes(client, q))
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            for extra in pool.map(lambda qq: _yahoo_lookup_docs(client, qq), (q, q + ".")):
                rows.extend(extra)
        for row in rows:
            key = row["symbol"].upper()
            if key in seen:
                continue
            if not _keeps_typed(row["symbol"], q):
                continue
            seen.add(key)
            out.append(row)
        _fill_empty_names(client, out)
    return _prefer_equity(out)


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


def _x_stem(symbol: str) -> str:
    tag = (symbol or "").strip().lstrip("$")
    return tag.split(".", 1)[0] if tag else ""


def _yahoo_profile(symbol: str) -> dict[str, str]:
    import httpx

    symbol = (symbol or "").strip()
    empty = {"industry": "", "sector": ""}
    if not symbol:
        return empty
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with httpx.Client(timeout=20.0, headers=headers, follow_redirects=True) as client:
            rows = _yahoo_quotes(client, symbol)
    except Exception:
        return empty
    hit = next((row for row in rows if (row.get("symbol") or "").upper() == symbol.upper()), None)
    if not hit:
        return empty
    industry = canonical_industry(hit.get("industry") or "")
    sector = (hit.get("sector") or "").strip() or parent_sector(industry)
    if not industry:
        return empty
    return {"industry": industry, "sector": sector}


def _query(symbol: str, name: str) -> str:
    return symbol_query(symbol, name)


def _post_hits_symbol(text: str, symbol: str, name: str) -> bool:
    blob = text or ""
    tag = (symbol or "").strip().lstrip("$")
    stem = _x_stem(tag)
    for piece in dict.fromkeys([stem, tag]):
        if not piece:
            continue
        if re.search(r"\$" + re.escape(piece) + r"(?![A-Za-z0-9])", blob, re.I):
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
        pid = str(tw.get("id") or "")
        row = dict(tw)
        row["id"] = pid
        row["username"] = users.get(str(tw.get("author_id") or ""), "")
        row["url"] = f"https://x.com/i/web/status/{pid}" if pid else ""
        row["source"] = "x"
        out.append(row)
    return out


def _credits_note(reddit: dict[str, int | None] | None) -> str:
    note = "100 posts is $0.50"
    reddit = reddit or {}
    used = reddit.get("used")
    remaining = reddit.get("remaining")
    if used is None and remaining is None:
        return note
    bits = [note]
    if used is not None and remaining is not None:
        bits.append(f"Reddit used {used} credit" + ("s" if used != 1 else "") + f", {remaining} remaining.")
    elif used is not None:
        bits.append(f"Reddit used {used} credit" + ("s" if used != 1 else "") + ".")
    else:
        bits.append(f"Reddit credits remaining: {remaining}.")
    return " ".join(bits)


def _gather_posts(
    x_query: str,
    reddit_query: str,
    token: str,
    reddit_key: str,
    note=None,
) -> tuple[list[dict[str, Any]], int, dict[str, int | None]]:
    x_posts: list[dict[str, Any]] = []
    reddit_posts: list[dict[str, Any]] = []
    reddit_creds: dict[str, int | None] = {"used": None, "remaining": None}
    x_err: Exception | None = None
    reddit_err: Exception | None = None

    def do_x() -> None:
        nonlocal x_posts, x_err
        if not token:
            return
        try:
            if note:
                note("Searching X…")
            x_posts = _search_x(x_query, token)
        except Exception as e:
            x_err = e

    def do_reddit() -> None:
        nonlocal reddit_posts, reddit_creds, reddit_err
        if not reddit_key:
            return
        try:
            if note:
                note("Searching Reddit…")
            reddit_posts, reddit_creds = search_reddit(reddit_query, reddit_key)
        except Exception as e:
            reddit_err = e

    threads: list[threading.Thread] = []
    if token:
        t = threading.Thread(target=do_x, daemon=True)
        t.start()
        threads.append(t)
    if reddit_key:
        t = threading.Thread(target=do_reddit, daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    if x_err:
        raise x_err
    if reddit_err and token and note:
        note("Reddit search failed; scoring X only.")
    if reddit_err and not token:
        if isinstance(reddit_err, RedditSearchError):
            raise HTTPException(status_code=reddit_err.status_code, detail=reddit_err.detail) from reddit_err
        raise reddit_err
    return x_posts + reddit_posts, len(x_posts), reddit_creds


def _payload(
    symbol: str,
    name: str,
    query: str,
    raw: list[dict[str, Any]],
    note=None,
    *,
    kind: str = "stock",
    industry: str = "",
    sector: str = "",
    theme: bool = False,
    n_x: int | None = None,
    reddit_creds: dict[str, int | None] | None = None,
    reddit_query: str = "",
) -> dict[str, Any]:
    posts: list[dict[str, Any]] = []
    dropped_wrong = 0
    if kind == "sector":
        hits = list(raw)
        dropped_wrong = 0
        score_symbol = industry or symbol
        score_name = sector
    else:
        hits = [tw for tw in raw if _post_hits_symbol(tw.get("text") or "", symbol, name)]
        dropped_wrong = len(raw) - len(hits)
        score_symbol = symbol
        score_name = name
    x_count = len(raw) if n_x is None else n_x
    for tw in hits:
        text = tw.get("text") or ""
        lang = tw.get("lang")
        text_en, translated = _translate(text, lang)
        pid = str(tw.get("id") or "")
        source = str(tw.get("source") or "x").strip().lower() or "x"
        url = str(tw.get("url") or "").strip()
        if not url and source == "x" and pid:
            url = f"https://x.com/i/web/status/{pid}"
        created = str(tw.get("created_at") or "")
        if "T" in created or created.endswith("Z"):
            created = _when(created)
        row = {
            "id": pid,
            "url": url,
            "created_at": created,
            "text": text,
            "text_original": text,
            "text_en": text_en,
            "lang": lang or "",
            "translated": translated,
            "username": str(tw.get("username") or "").lstrip("@"),
            "source": source,
        }
        sub = str(tw.get("subreddit") or "").strip()
        if sub:
            row["subreddit"] = sub
        if "likes" in tw:
            row["likes"] = tw.get("likes")
        if "comments" in tw:
            row["comments"] = tw.get("comments")
        posts.append(row)

    def _score_progress(done: int, total: int) -> None:
        if note:
            if done <= 0:
                note("Scoring…")
            else:
                note(f"Scoring {done}/{total}")

    try:
        if note:
            note("Scoring…")
        classified = classify_posts(
            posts,
            symbol=score_symbol,
            name=score_name,
            kind="sector" if kind == "sector" else "stock",
            on_progress=_score_progress,
        )
    except ScoreError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    kept = [p for p in classified if p.get("classification") != "spam"]
    spam_n = len(classified) - len(kept)
    bull = sum(1 for p in kept if p.get("classification") == "bull")
    bear = sum(1 for p in kept if p.get("classification") == "bear")
    neut = sum(1 for p in kept if p.get("classification") == "neutral")
    score, label = score_from_counts(bull, bear, neut)
    if not classified:
        label = "No posts matched that industry" if kind == "sector" else "No posts matched that exact tag"
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

    if kind == "sector":
        news_q = [industry + " industry"]
        if sector and sector.casefold() not in industry.casefold():
            news_q.append(sector + " sector")
    else:
        news_q = [symbol] + ([name] if name else [])
    for qn in news_q:
        for headline in _yahoo_news(qn):
            if len(facts) >= 16:
                break
            _add_fact(headline)
        if len(facts) >= 16:
            break
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
            score_symbol,
            score_name,
            score=score,
            label=label,
            bull=bull,
            bear=bear,
            neutral=neut,
            facts=facts,
            kind="sector" if kind == "sector" else "stock",
        )
    except Exception:
        thesis = {"summary": "", "bull": "", "bear": ""}
    as_of = _now()
    if kind == "sector":
        profile_industry = industry
        profile_sector = sector
        company = industry
        display = ""
        hist_key = "SEC:" + industry
        ticker_out = ""
    else:
        if not (industry and sector):
            profile = _yahoo_profile(symbol)
            industry = industry or profile.get("industry") or ""
            sector = sector or profile.get("sector") or ""
        profile_industry = industry
        profile_sector = sector
        company = name
        display = symbol
        hist_key = symbol
        ticker_out = symbol
    result = {
        "mode": "sector" if kind == "sector" else "symbol",
        "ticker": ticker_out,
        "display_ticker": display,
        "company": company,
        "industry": profile_industry,
        "sector": profile_sector,
        "theme": bool(theme) if kind == "sector" else False,
        "as_of": as_of,
        "window": "last 7 days",
        "query": query,
        "reddit_query": reddit_query,
        "credits_estimate_usd": round(0.005 * x_count, 3),
        "credits_note": _credits_note(reddit_creds),
        "reddit_credits_used": (reddit_creds or {}).get("used"),
        "reddit_credits_remaining": (reddit_creds or {}).get("remaining"),
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
            "ticker": hist_key,
            "score": score,
            "bull": bull,
            "bear": bear,
            "neutral": neut,
            "n_kept": len(kept),
        }
    )
    result["history"] = _load_history(hist_key)
    (RESULTS / "current.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", hist_key)
    (RESULTS / f"{safe}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


@app.get("/api/scorer")
def api_scorer() -> JSONResponse:
    return JSONResponse(scorer_info())


@app.get("/api/lookup")
def api_lookup(q: str = "") -> JSONResponse:
    return JSONResponse({"matches": _yahoo_lookup(q)})


@app.get("/api/sectors")
def api_sectors() -> JSONResponse:
    return JSONResponse({"sectors": load_taxonomy()})


def _stream_pull(work_fn):
    q = queue.Queue()

    def note(msg: str) -> None:
        q.put(("status", msg))

    def work() -> None:
        try:
            work_fn(note, q)
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


def _ready_to_search() -> tuple[str, str, None] | tuple[None, None, str]:
    token = _token()
    reddit_key = _socialcrawl_key()
    if not token and not reddit_key:
        return None, None, "Add X_BEARER_TOKEN or SOCIALCRAWL_API_KEY to the .env file next to this app."
    if not scoring_ready():
        return None, None, (
            "Scoring uses the grok command on this computer. It was not found on PATH. "
            "Set GROK_BIN in .env if needed."
        )
    return token, reddit_key, None


@app.post("/api/pull")
def api_pull(body: PullIn):
    mode = (body.mode or "symbol").strip().lower()
    if mode == "sector":
        industry, sector, is_theme = resolve_sector_subject(body.industry or "")
        if not industry:
            raise HTTPException(status_code=400, detail="Type a theme or pick an industry.")
        token, reddit_key, err = _ready_to_search()
        if err:
            raise HTTPException(status_code=400, detail=err)
        query = sector_query(industry, sector)
        r_query = reddit_sector_query(industry, sector)

        def work(note, q) -> None:
            raw, n_x, reddit_creds = _gather_posts(query, r_query, token or "", reddit_key or "", note)
            result = _payload(
                "",
                "",
                query,
                raw,
                note=note,
                kind="sector",
                industry=industry,
                sector=sector,
                theme=is_theme,
                n_x=n_x,
                reddit_creds=reddit_creds,
                reddit_query=r_query,
            )
            q.put(("done", result))

        return _stream_pull(work)

    ticker = (body.ticker or "").strip().upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="Ticker must be letters, digits, or dots.")
    if not (body.confirmed and (body.symbol or ticker)):
        matches = _yahoo_lookup(ticker)
        if not matches:
            return JSONResponse(
                {
                    "status": "none",
                    "detail": "None found.",
                    "matches": [],
                }
            )
        return JSONResponse(
            {
                "status": "pick",
                "detail": "Pick a listing, then Pull runs that exact tag.",
                "matches": matches,
            }
        )
    token, reddit_key, err = _ready_to_search()
    if err:
        raise HTTPException(status_code=400, detail=err)
    matches = _yahoo_lookup(ticker)
    chosen = {
        "symbol": (body.symbol or ticker).strip(),
        "name": (body.name or "").strip(),
    }
    if not chosen["name"]:
        hit = [m for m in matches if m["symbol"].upper() == chosen["symbol"].upper()]
        if hit:
            chosen["name"] = hit[0]["name"]

    symbol = chosen["symbol"]
    name = chosen.get("name") or ""
    query = _query(symbol, name)
    r_query = reddit_symbol_query(symbol, name)

    def work(note, q) -> None:
        profile = {"industry": "", "sector": ""}
        hit = next((m for m in matches if m["symbol"].upper() == symbol.upper()), None)
        if hit:
            if hit.get("industry"):
                profile["industry"] = canonical_industry(hit.get("industry") or "")
            if hit.get("sector"):
                profile["sector"] = hit.get("sector") or ""
            if profile["industry"] and not profile["sector"]:
                profile["sector"] = parent_sector(profile["industry"])

        def fetch_profile() -> None:
            if profile.get("industry"):
                return
            try:
                profile.update(_yahoo_profile(symbol))
            except Exception:
                pass

        t_prof = threading.Thread(target=fetch_profile, daemon=True)
        t_prof.start()
        raw, n_x, reddit_creds = _gather_posts(query, r_query, token or "", reddit_key or "", note)
        t_prof.join(timeout=12)
        result = _payload(
            symbol,
            name,
            query,
            raw,
            note=note,
            kind="stock",
            industry=profile.get("industry") or "",
            sector=profile.get("sector") or "",
            n_x=n_x,
            reddit_creds=reddit_creds,
            reddit_query=r_query,
        )
        q.put(("done", result))

    return _stream_pull(work)


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
