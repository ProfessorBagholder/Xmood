#!/usr/bin/env python3
"""Label each post by reading the whole post with Grok on this computer."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$[A-Za-z]{1,6}(?:\.[A-Za-z]{1,4})?")
LABELS = {"bull", "bear", "neutral", "spam"}

SYSTEM = """You score social posts for a retail mood gauge on one stock.

Return only JSON: {"labels":[{"i":0,"label":"bull","why":"short reason"}]}

label must be one of: bull, bear, neutral, spam.

Read the whole post. Grade what the writer is doing with this stock in front of other readers, including jokes, sarcasm, and quoting other people.

bull: the writer is talking this stock up. That includes saying they are buying, expecting a rise, cheering, or posting strong results, shipments, or other good operating news as the point of the post.
bear: the writer is talking this stock down. That includes selling, expecting a fall, or posting a miss, a warning, or other bad news as the point of the post.
neutral: a genuine question, or a true even split, or nothing about whether things are going well or badly.
spam: ads or a pile of unrelated tickers.

If the post is a list of results, grade the results. Income up, a beat, a first shipment, or a unit delivered as the headline is bull. A miss, a cut, or a decline as the headline is bear. Naming which part of the business grew does not make it neutral.

why: one short ordinary-English clause. No extra keys."""


class ScoreError(RuntimeError):
    pass


def is_spam(text: str) -> bool:
    return len(CASHTAG_RE.findall(text or "")) >= 4


def scoring_text(post: dict[str, Any]) -> str:
    original = post.get("text") or post.get("text_original") or ""
    en = post.get("text_en")
    if isinstance(en, str) and en.strip() and en.strip() != str(original).strip():
        return original + "\nEnglish: " + en.strip()
    return original


def label_for_score(score: int | None) -> str:
    if score is None:
        return "Not enough directional posts"
    if score <= 20:
        return "Extreme bearish"
    if score <= 40:
        return "Bearish"
    if score <= 60:
        return "Mixed"
    if score <= 80:
        return "Bullish"
    return "Extreme bullish"


def score_from_counts(bull: int, bear: int) -> tuple[int | None, str]:
    if bull + bear < 8:
        return None, "Not enough directional posts"
    score = round(100 * bull / (bull + bear))
    return score, label_for_score(score)


def _parse_labels(raw: str, n: int) -> list[dict[str, str]]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ScoreError("Grok did not return JSON.")
        data = json.loads(m.group(0))
    rows = data.get("labels") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ScoreError("Grok JSON was missing labels.")
    by_i: dict[int, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        lab = str(row.get("label") or "").strip().lower()
        if lab not in LABELS:
            continue
        why = str(row.get("why") or "").strip() or "whole-post read"
        by_i[i] = {"label": lab, "why": why}
    out = []
    for i in range(n):
        hit = by_i.get(i)
        out.append(hit if hit else {"label": "neutral", "why": "no label returned"})
    return out


def grok_bin() -> Path | None:
    env = (os.environ.get("GROK_BIN") or "").strip()
    candidates = [
        env,
        shutil.which("grok"),
        str(Path.home() / ".grok" / "bin" / "grok"),
        "/home/box/.grok/bin/grok",
    ]
    for c in candidates:
        if not c:
            continue
        path = Path(c)
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def scorer_info() -> dict[str, str]:
    path = grok_bin()
    if path is None:
        return {"scorer": "none", "path": "", "detail": "grok command not found"}
    return {
        "scorer": "grok",
        "path": str(path),
        "detail": "Scored with grok at " + str(path),
    }


def scoring_ready() -> bool:
    return grok_bin() is not None


def _chat_grok(messages: list[dict[str, str]]) -> str:
    binary = grok_bin()
    if binary is None:
        raise ScoreError(
            "Scoring uses the grok command on this computer. It was not found. "
            "Install it, sign in, then restart. Do not add an xAI console key."
        )
    parts = []
    for m in messages:
        parts.append(f"{m.get('role', 'user').upper()}:\n{m.get('content') or ''}")
    prompt = "\n\n".join(parts) + "\n\nReply with JSON only."
    r = subprocess.run(
        [
            str(binary),
            "-p",
            prompt,
            "--output-format",
            "plain",
            "--no-subagents",
            "--disable-web-search",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "grok failed").strip()[:240]
        raise ScoreError("grok failed: " + err)
    return r.stdout or ""


def _score_chunk(
    items: list[dict[str, Any]],
    symbol: str,
    name: str,
    chat: Callable[[list[dict[str, str]]], str],
) -> list[dict[str, str]]:
    lines = [f'[{it["i"]}] {it["text"]}' for it in items]
    stock = f"{symbol} ({name})" if name else symbol
    user = f"Stock: {stock}\nScore the writer's view of this stock in each post.\n\n" + "\n\n".join(lines)
    raw = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
    return _parse_labels(raw, len(items))


def classify_posts(
    posts: list[dict[str, Any]],
    symbol: str = "",
    name: str = "",
    chat: Callable[[list[dict[str, str]]], str] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    pending: list[tuple[int, dict[str, Any]]] = []
    for p in posts:
        item = dict(p)
        original = item.get("text") or item.get("text_original") or ""
        text = scoring_text(item)
        if is_spam(original) or is_spam(text):
            item["classification"] = "spam"
            item["reason"] = "four or more cashtags"
            out.append(item)
        else:
            pending.append((len(out), item))
            out.append(item)
    if not pending:
        return out
    chat_fn = chat or _chat_grok
    for start in range(0, len(pending), 1):
        batch = pending[start : start + 1]
        payload = [{"i": j, "text": scoring_text(item)} for j, (_idx, item) in enumerate(batch)]
        labels = _score_chunk(payload, symbol, name, chat_fn)
        for (_idx, item), lab in zip(batch, labels):
            item["classification"] = lab["label"]
            item["reason"] = lab["why"]
            out[_idx] = item
    return out


def _selftest() -> int:
    failed = 0
    if is_spam("$AAA $BBB $CCC $DDD check this") is not True:
        print("FAIL spam cashtags")
        failed += 1
    if is_spam("I'm very bullish on this stock") is not False:
        print("FAIL spam false positive")
        failed += 1

    def always_mixed(messages: list[dict[str, str]]) -> str:
        blob = messages[-1]["content"]
        labels = [{"i": int(m.group(1)), "label": "neutral", "why": "forced"} for m in re.finditer(r"\[(\d+)\] ", blob)]
        return json.dumps({"labels": labels})

    stated = classify_posts(
        [{"id": "me", "text": "I'm very bullish on $CH.V"}],
        symbol="CH.V",
        chat=always_mixed,
    )
    if stated[0]["classification"] != "neutral":
        print("FAIL no-override want=neutral got=" + stated[0]["classification"])
        failed += 1

    parsed = _parse_labels(
        '{"labels":[{"i":0,"label":"bull","why":"joke"},{"i":1,"label":"bear","why":"selling"}]}',
        2,
    )
    if parsed[0]["label"] != "bull" or parsed[1]["label"] != "bear":
        print("FAIL parse")
        failed += 1
    s, lab = score_from_counts(8, 0)
    if s != 100 or lab != "Extreme bullish":
        print("FAIL score 100")
        failed += 1
    if failed:
        print(f"selftest FAIL ({failed})")
        return 1
    print("selftest PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Classify X posts with Grok.")
    p.add_argument("--json", metavar="FILE")
    p.add_argument("-o", "--out")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.json:
        p.error("need --json FILE or --selftest")
    with open(args.json, encoding="utf-8") as f:
        data = json.load(f)
    posts = data.get("posts")
    if not isinstance(posts, list):
        print("error: JSON must have a posts array", file=sys.stderr)
        return 2
    data["posts"] = classify_posts(posts, symbol=str(data.get("ticker") or ""))
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
