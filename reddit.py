#!/usr/bin/env python3
"""SocialCrawl Reddit search. Maps items into the same post shape Grok scores."""
from __future__ import annotations

import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "tests" / "fixtures" / "socialcrawl_reddit_search.json"
TZ = ZoneInfo("America/Edmonton")
SEARCH_URL = "https://www.socialcrawl.dev/v1/reddit/search"


class RedditSearchError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def format_when(iso: str) -> str:
    raw = (iso or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(TZ)
    except ValueError:
        return raw
    return dt.strftime("%Y-%m-%d %H:%M")


def _as_dict(row: Any) -> dict[str, Any]:
    return row if isinstance(row, dict) else {}


def _reddit_text(post: dict[str, Any]) -> str:
    ext = _as_dict(post.get("ext"))
    content = _as_dict(post.get("content"))
    title = str(ext.get("title") or "").strip()
    selftext = str(ext.get("selftext") or "").strip()
    body = selftext or str(content.get("text") or "").strip()
    if title and body:
        if body == title or body.startswith(title):
            return body
        return title + "\n\n" + body
    return title or body


def reddit_items_to_posts(items: Any) -> list[dict[str, Any]]:
    """Map SocialCrawl reddit/search items into classify_posts-compatible dicts."""
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        wrap = _as_dict(item)
        post = _as_dict(wrap.get("post") if "post" in wrap else wrap)
        if not post:
            continue
        pid = str(post.get("id") or "").strip()
        text = _reddit_text(post)
        if not pid and not text:
            continue
        author = _as_dict(post.get("author"))
        eng = _as_dict(post.get("engagement"))
        ext = _as_dict(post.get("ext"))
        sub = str(ext.get("subreddit") or "").strip().lstrip("/")
        if sub.lower().startswith("r/"):
            sub = sub[2:]
        row: dict[str, Any] = {
            "id": pid,
            "url": str(post.get("url") or "").strip(),
            "created_at": format_when(str(post.get("published_at") or "")),
            "text": text,
            "text_original": text,
            "text_en": text,
            "username": str(author.get("username") or "").lstrip("@"),
            "source": "reddit",
            "subreddit": sub,
        }
        if "likes" in eng:
            row["likes"] = eng.get("likes")
        if "comments" in eng:
            row["comments"] = eng.get("comments")
        out.append(row)
    return out


def reddit_posts_from_response(body: Any) -> list[dict[str, Any]]:
    data = _as_dict(body).get("data")
    items = _as_dict(data).get("items") if isinstance(data, dict) else None
    return reddit_items_to_posts(items)


def _intish(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def reddit_credits(body: Any, headers: Any = None) -> dict[str, int | None]:
    blob = _as_dict(body)
    used = blob.get("credits_used")
    remaining = blob.get("credits_remaining")
    if headers is not None:
        get = headers.get if hasattr(headers, "get") else lambda _k: None
        if used is None:
            used = get("x-credits-used")
        if remaining is None:
            remaining = get("x-credits-remaining")
    return {"used": _intish(used), "remaining": _intish(remaining)}


def search_reddit(query: str, api_key: str) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
    """One page of SocialCrawl Reddit search. No sentiment endpoint."""
    import certifi
    import httpx

    q = (query or "").strip()
    if not q:
        return [], {"used": None, "remaining": None}
    headers = {"x-api-key": api_key, "Accept": "application/json"}
    params = {"query": q, "sort": "relevance", "timeframe": "week"}
    # macOS CPython can fail default SSL verify against socialcrawl.dev;
    # pin the certifi CA bundle instead of disabling verification.
    with httpx.Client(timeout=45.0, verify=certifi.where()) as client:
        r = client.get(SEARCH_URL, params=params, headers=headers)
    if r.status_code == 401:
        raise RedditSearchError(401, "SocialCrawl key was rejected. Check SOCIALCRAWL_API_KEY in .env.")
    if r.status_code == 403:
        raise RedditSearchError(403, "SocialCrawl refused Reddit search. Check the API key.")
    if r.status_code == 429:
        raise RedditSearchError(429, "SocialCrawl rate limit. Wait a minute and pull again.")
    if r.status_code >= 400:
        raise RedditSearchError(502, f"Reddit search failed ({r.status_code}).")
    body = r.json() or {}
    return reddit_posts_from_response(body), reddit_credits(body, r.headers)


def _selftest() -> int:
    failed = 0

    def check(ok: bool, msg: str) -> None:
        nonlocal failed
        if not ok:
            print("FAIL " + msg)
            failed += 1

    check("reddit/search" in SEARCH_URL, "must call GET /v1/reddit/search")
    check("content_analysis" not in SEARCH_URL, "must not use SocialCrawl sentiment")
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    posts = reddit_posts_from_response(body)
    check(len(posts) == 2, f"fixture want=2 posts got={len(posts)}")
    first = posts[0] if posts else {}
    check(first.get("id") == "1abc234", "id want=1abc234 got=" + str(first.get("id")))
    check(
        first.get("url") == "https://www.reddit.com/r/stocks/comments/1abc234/pltr_thoughts/",
        "url missing or wrong",
    )
    check(first.get("source") == "reddit", "source want=reddit")
    check(first.get("username") == "retail_dan", "username want=retail_dan got=" + str(first.get("username")))
    check(first.get("subreddit") == "stocks", "subreddit want=stocks got=" + str(first.get("subreddit")))
    check(first.get("likes") == 42, "likes want=42")
    check(first.get("comments") == 11, "comments want=11")
    check("$PLTR thoughts" in str(first.get("text") or ""), "title must be in text")
    check("$PLTR looks strong into earnings" in str(first.get("text") or ""), "body must be in text")
    check(first.get("text") == first.get("text_original") == first.get("text_en"), "text fields must match")
    check(first.get("created_at") == "2026-09-01 09:04", "created_at must use _when on published_at got=" + str(first.get("created_at")))
    second = posts[1] if len(posts) > 1 else {}
    check(second.get("id") == "2def456", "second id")
    check(second.get("subreddit") == "wallstreetbets", "r/ prefix must be stripped")
    check(second.get("text") == "Palantir\n\nI'm long Palantir", "title then selftext got=" + repr(second.get("text")))
    creds = reddit_credits(body)
    check(creds == {"used": 1, "remaining": 99}, "credits from body got=" + str(creds))
    header_only = reddit_credits({}, {"x-credits-used": "1", "x-credits-remaining": "40"})
    check(header_only == {"used": 1, "remaining": 40}, "credits from headers got=" + str(header_only))
    check(reddit_items_to_posts(None) == [], "None items must be empty")
    check(reddit_items_to_posts([{"nope": 1}]) == [], "item without post must be skipped")

    from classify import classify_posts

    def fake_chat(_messages: list[dict[str, str]]) -> str:
        return json.dumps(
            {
                "labels": [
                    {"i": 0, "label": "bull", "why": "mapped reddit text"},
                    {"i": 1, "label": "bull", "why": "mapped reddit text"},
                ]
            }
        )

    scored = classify_posts(posts, symbol="PLTR", name="Palantir", chat=fake_chat)
    check(len(scored) == 2 and all(p.get("classification") == "bull" for p in scored), "classify_posts must score mapped reddit posts")
    check(all(p.get("source") == "reddit" for p in scored), "classify_posts must keep source=reddit")

    import server

    check(server._post_hits_symbol(str(first.get("text") or ""), "PLTR", "Palantir"), "mapped title+text must hit the symbol filter")
    check(server._post_hits_symbol(str(second.get("text") or ""), "PLTR", "Palantir"), "company name in mapped text must hit the symbol filter")
    old_x = os.environ.get("X_BEARER_TOKEN")
    old_r = os.environ.get("SOCIALCRAWL_API_KEY")
    old_ready = server.scoring_ready
    try:
        server.scoring_ready = lambda: True
        os.environ.pop("X_BEARER_TOKEN", None)
        os.environ.pop("SOCIALCRAWL_API_KEY", None)
        _token, _key, err = server._ready_to_search()
        check(bool(err) and "X_BEARER_TOKEN or SOCIALCRAWL_API_KEY" in str(err), "neither source key must error")
        from fastapi.testclient import TestClient

        resp = TestClient(server.app).post("/api/pull", json={"mode": "sector", "industry": "Software - Infrastructure"})
        check(resp.status_code == 400, "pull without source keys want=400 got=" + str(resp.status_code))
        check("SOCIALCRAWL_API_KEY" in str((resp.json() or {}).get("detail") or ""), "pull error must mention Reddit key")
        os.environ["SOCIALCRAWL_API_KEY"] = "test-reddit-key"
        token, key, err = server._ready_to_search()
        check(err is None and token == "" and key == "test-reddit-key", "reddit-only pull must be allowed")
        os.environ.pop("SOCIALCRAWL_API_KEY", None)
        os.environ["X_BEARER_TOKEN"] = "test-x-token"
        token, key, err = server._ready_to_search()
        check(err is None and token == "test-x-token" and key == "", "x-only pull must be allowed")

        old_search_x = server._search_x
        old_search_reddit = server.search_reddit
        try:
            server._search_x = lambda _q, _t: [
                {
                    "id": "x1",
                    "text": "$PLTR from x",
                    "source": "x",
                    "username": "xuser",
                    "created_at": "2026-09-01T15:04:00Z",
                    "url": "https://x.com/i/web/status/x1",
                }
            ]
            server.search_reddit = lambda _q, _k: (posts, {"used": 1, "remaining": 99})
            notes: list[str] = []
            merged, n_x, creds = server._gather_posts("($PLTR) -is:retweet", "PLTR OR $PLTR", "xtok", "rtok", notes.append)
            check(n_x == 1 and len(merged) == 3, f"merge want 1 x + 2 reddit got n_x={n_x} n={len(merged)}")
            check(merged[0].get("source") == "x" and merged[1].get("source") == "reddit", "concat order is X then Reddit")
            check(creds == {"used": 1, "remaining": 99}, "gather must pass reddit credits")
            check("Searching X…" in notes and "Searching Reddit…" in notes, "status notes must mention both sources")
            merged, n_x, _creds = server._gather_posts("q", "q", "xtok", "", notes.append)
            check(n_x == 1 and len(merged) == 1 and merged[0]["source"] == "x", "missing reddit key must stay X-only")
            merged, n_x, _creds = server._gather_posts("q", "q", "", "rtok", notes.append)
            check(n_x == 0 and len(merged) == 2 and all(p.get("source") == "reddit" for p in merged), "missing X key must stay Reddit-only")
        finally:
            server._search_x = old_search_x
            server.search_reddit = old_search_reddit
    finally:
        server.scoring_ready = old_ready
        if old_x is None:
            os.environ.pop("X_BEARER_TOKEN", None)
        else:
            os.environ["X_BEARER_TOKEN"] = old_x
        if old_r is None:
            os.environ.pop("SOCIALCRAWL_API_KEY", None)
        else:
            os.environ["SOCIALCRAWL_API_KEY"] = old_r
    if failed:
        print(f"selftest FAIL ({failed})")
        return 1
    print("selftest PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        return _selftest()
    print("usage: python3 reddit.py --selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
