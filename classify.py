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
SKIP_WHY = "Grok did not label this post"
GROK_LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "label": {"type": "string", "enum": ["bull", "bear", "neutral", "spam"]},
                    "why": {"type": "string"},
                },
                "required": ["i", "label", "why"],
            },
        }
    },
    "required": ["labels"],
}
GROK_THESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "bull": {"type": "string"},
        "bear": {"type": "string"},
    },
    "required": ["summary", "bull", "bear"],
}

SYSTEM = """You score social posts for a retail mood gauge on one stock.

Return only JSON: {"labels":[{"i":0,"label":"bull","why":"short reason"}]}

label must be one of: bull, bear, neutral, spam.

Read the whole post. Grade what the writer is doing with this stock in front of other readers, including jokes, sarcasm, and quoting other people.

bull: the writer is talking this stock up. That includes saying they are buying, expecting a rise, cheering, or posting strong results, shipments, or other good operating news as the point of the post. A hold-nudge question (holding, adding, buying more, or whether it is too late) is bull.
bear: the writer is talking this stock down. That includes selling, expecting a fall, or posting a miss, a warning, or other bad news as the point of the post.
neutral: they are actually asking for information with no lean, or a true even split, or nothing about whether things are going well or badly. A question that teases someone for not holding, or implies they should already own it, is bull, not a genuine question.
spam: ads or a pile of unrelated tickers.

Score the writer's meaning in any language. Do not treat a question mark as no view by itself.

If the post is a list of results, grade the results. Income up, a beat, a first shipment, or a unit delivered as the headline is bull. A miss, a cut, or a decline as the headline is bear. Naming which part of the business grew does not make it neutral.

why: one short ordinary-English clause. No extra keys."""

SYSTEM_SECTOR = """You score social posts for a retail mood gauge on one industry.

Return only JSON: {"labels":[{"i":0,"label":"bull","why":"short reason"}]}

label must be one of: bull, bear, neutral, spam.

Read the whole post. Grade what the writer is doing with this industry in front of other readers, including jokes, sarcasm, and quoting other people.

bull: the writer is talking this industry up. That includes saying they are buying names in it, expecting a rise, cheering, or posting strong results or other good operating news as the point of the post. A hold-nudge question (holding, adding, buying more, or whether it is too late) is bull.
bear: the writer is talking this industry down. That includes selling, expecting a fall, or posting a miss, a warning, or other bad news as the point of the post.
neutral: they are actually asking for information with no lean, or a true even split, or nothing about whether things are going well or badly. A question that teases someone for not holding, or implies they should already own it, is bull, not a genuine question.
spam: not about this industry, ads, or a pile of unrelated tickers. A post about another industry or the broad market is spam.

Score the writer's meaning in any language. Do not treat a question mark as no view by itself.

If the post is a list of results, grade the results. Income up, a beat, a first shipment, or a unit delivered as the headline is bull. A miss, a cut, or a decline as the headline is bear.

why: one short ordinary-English clause. No extra keys."""

THESIS_SYSTEM = """You write a short two-sided case for one listed name.

Return only JSON: {"summary":"...","bull":"...","bear":"..."}

summary: one short ordinary-English mood line that states the mood score.
bull: the well thought-out case for the name doing well. Use the given news and operating points.
bear: the well thought-out case for the name doing poorly. Use the given news and operating points.

If the facts mention a shipment, delivery, offtake, sold-out output, or customers taking product, the company is already operating. Do not call it unproven, a hopeful, a project company with only plans, or a slide-deck name.

If facts are empty: say the case is limited because no company news was fetched. Do not write a generic sector story.

Ordinary English. Do not recap social posts. Do not invent filings, prices, or quotes. No trader slang. No print. No book. No extra keys."""

THESIS_SYSTEM_SECTOR = """You write a short two-sided case for one industry.

Return only JSON: {"summary":"...","bull":"...","bear":"..."}

summary: one short ordinary-English mood line that states the mood score.
bull: the well thought-out case for the industry doing well. Use the given news and operating points.
bear: the well thought-out case for the industry doing poorly. Use the given news and operating points.

Write about the industry as a whole: demand, costs, regulation, and operators in that line of work. Never treat the industry title as a listed company.

If facts are empty: say the case is limited because no industry news was fetched.

Ordinary English. Do not recap social posts. Do not invent filings, prices, or quotes. No trader slang. No print. No book. No extra keys."""


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


def score_from_counts(bull: int, bear: int, neutral: int = 0) -> tuple[int | None, str]:
    n = bull + bear + neutral
    if n < 1:
        return None, label_for_score(None)
    score = round(50 + 50 * (bull - bear) / n)
    return score, label_for_score(score)


def _unlabeled(n: int) -> list[dict[str, str]]:
    return [{"label": "neutral", "why": SKIP_WHY} for _ in range(n)]


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```.*$", "", text)
    return text.strip()


def _json_blobs(text: str) -> list[Any]:
    """Pull JSON values out of grok stdout, including wrappers, fences, and logs."""
    text = (text or "").strip()
    if not text:
        return []
    found: list[Any] = []
    stripped = _strip_fences(text)
    for candidate in (text, stripped):
        try:
            found.append(json.loads(candidate))
        except json.JSONDecodeError:
            pass
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I):
        body = m.group(1).strip()
        try:
            found.append(json.loads(body))
        except json.JSONDecodeError:
            pass
    decoder = json.JSONDecoder()
    i = 0
    src = stripped or text
    while i < len(src):
        while i < len(src) and src[i] not in "{[":
            i += 1
        if i >= len(src):
            break
        try:
            obj, end = decoder.raw_decode(src[i:])
        except json.JSONDecodeError:
            i += 1
            continue
        found.append(obj)
        i += max(end, 1)
    return found


def _has_labels(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("labels"), list)


def _unwrap_labels(data: Any, depth: int = 0) -> Any:
    if depth > 8 or data is None:
        return None
    if isinstance(data, str):
        for blob in _json_blobs(data):
            hit = _unwrap_labels(blob, depth + 1)
            if hit is not None:
                return hit
        return None
    if _has_labels(data):
        return data
    if isinstance(data, dict):
        for key in ("result", "text", "content"):
            if key not in data:
                continue
            hit = _unwrap_labels(data[key], depth + 1)
            if hit is not None:
                return hit
        for key, val in data.items():
            if key in ("result", "text", "content"):
                continue
            if isinstance(val, (str, dict, list)):
                hit = _unwrap_labels(val, depth + 1)
                if hit is not None:
                    return hit
        return None
    if isinstance(data, list):
        if data and all(isinstance(x, dict) and ("label" in x or "i" in x) for x in data):
            return {"labels": data}
        for item in data:
            hit = _unwrap_labels(item, depth + 1)
            if hit is not None:
                return hit
        return None
    return None


def _parse_labels(raw: str, n: int) -> list[dict[str, str]]:
    data = _unwrap_labels(raw)
    rows = data.get("labels") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return _unlabeled(n)
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
        out.append(hit if hit else {"label": "neutral", "why": SKIP_WHY})
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


def _grok_cmd(binary: Path | str, prompt: str, schema: dict[str, Any] | None = None) -> list[str]:
    return [
        str(binary),
        "-p",
        prompt,
        "--verbatim",
        "--json-schema",
        json.dumps(schema or GROK_LABEL_SCHEMA, separators=(",", ":")),
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--no-subagents",
        "--disable-web-search",
        "--reasoning-effort",
        "low",
        "--cwd",
        "/tmp",
        "--rules",
        "Reply with JSON only. Do not use tools. Do not edit files.",
    ]


def _chat_grok(messages: list[dict[str, str]], schema: dict[str, Any] | None = None) -> str:
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
        _grok_cmd(binary, prompt, schema=schema),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "grok failed").strip()[:240]
        raise ScoreError("grok failed: " + err)
    out = r.stdout or ""
    if r.stderr:
        out = out + "\n" + r.stderr
    return out


def _score_chunk(
    items: list[dict[str, Any]],
    symbol: str,
    name: str,
    chat: Callable[[list[dict[str, str]]], str],
    kind: str = "stock",
) -> list[dict[str, str]]:
    lines = [f'[{it["i"]}] {it["text"]}' for it in items]
    if kind == "sector":
        subject = f"{symbol} ({name})" if name else symbol
        user = (
            f"Industry: {subject}\nScore the writer's view of this industry in each post.\n\n"
            + "\n\n".join(lines)
        )
        system = SYSTEM_SECTOR
    else:
        stock = f"{symbol} ({name})" if name else symbol
        user = f"Stock: {stock}\nScore the writer's view of this stock in each post.\n\n" + "\n\n".join(lines)
        system = SYSTEM
    raw = chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    return _parse_labels(raw, len(items))


def classify_posts(
    posts: list[dict[str, Any]],
    symbol: str = "",
    name: str = "",
    chat: Callable[[list[dict[str, str]]], str] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    kind: str = "stock",
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
    total = len(pending)
    if on_progress:
        on_progress(0, total)

    chunk = 10
    for start in range(0, total, chunk):
        batch = pending[start : start + chunk]
        payload = [{"i": j, "text": scoring_text(item)} for j, (_idx, item) in enumerate(batch)]
        try:
            labels = _score_chunk(payload, symbol, name, chat_fn, kind=kind)
        except ScoreError:
            if chat is None:
                raise
            labels = _unlabeled(len(batch))
        if not labels or len(labels) != len(batch):
            labels = _unlabeled(len(batch))
        if chat is None and labels and all(lab.get("why") == SKIP_WHY for lab in labels):
            raise ScoreError("Grok did not return labels.")
        for (_idx, item), lab in zip(batch, labels):
            item["classification"] = lab["label"]
            item["reason"] = lab["why"]
            out[_idx] = item
        if on_progress:
            on_progress(min(start + chunk, total), total)
    return out


def _empty_thesis() -> dict[str, str]:
    return {"summary": "", "bull": "", "bear": ""}


def _has_thesis(obj: Any) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in ("summary", "bull", "bear"))


def _unwrap_thesis(data: Any, depth: int = 0) -> Any:
    if depth > 8 or data is None:
        return None
    if isinstance(data, str):
        for blob in _json_blobs(data):
            hit = _unwrap_thesis(blob, depth + 1)
            if hit is not None:
                return hit
        return None
    if _has_thesis(data):
        return data
    if isinstance(data, dict):
        for key in ("result", "text", "content"):
            if key not in data:
                continue
            hit = _unwrap_thesis(data[key], depth + 1)
            if hit is not None:
                return hit
        for key, val in data.items():
            if key in ("result", "text", "content"):
                continue
            if isinstance(val, (str, dict, list)):
                hit = _unwrap_thesis(val, depth + 1)
                if hit is not None:
                    return hit
        return None
    if isinstance(data, list):
        for item in data:
            hit = _unwrap_thesis(item, depth + 1)
            if hit is not None:
                return hit
        return None
    return None


def _parse_thesis(raw: str) -> dict[str, str]:
    data = _unwrap_thesis(raw)
    if not isinstance(data, dict):
        return _empty_thesis()
    return {
        "summary": str(data.get("summary") or "").strip(),
        "bull": str(data.get("bull") or "").strip(),
        "bear": str(data.get("bear") or "").strip(),
    }


def write_thesis(
    symbol: str,
    name: str = "",
    chat: Callable[[list[dict[str, str]]], str] | None = None,
    score: int | None = None,
    label: str = "",
    bull: int = 0,
    bear: int = 0,
    neutral: int = 0,
    facts: list[str] | None = None,
    kind: str = "stock",
) -> dict[str, str]:
    stock = f"{symbol} ({name})" if name else symbol
    score_s = "none" if score is None else str(score)
    fact_items = [str(x).strip() for x in (facts or []) if str(x).strip()]
    if fact_items:
        fact_block = "\n".join(f"- {x}" for x in fact_items)
    else:
        fact_block = "(none)"
    if kind == "sector":
        user = (
            f"Industry: {stock}\n"
            f"Mood score: {score_s}\n"
            f"Mood label: {label or 'unknown'}\n"
            f"Counts: bull={bull} bear={bear} neutral={neutral}\n"
            f"Industry news and operating points:\n{fact_block}\n"
            "Write a short mood line that states the mood score, plus a well thought-out two-sided case for the industry.\n"
            "Do not treat the industry name as one company. Do not write a CEO, dividend, or one-stock case.\n"
            "Use the given news and operating points. Ordinary English. Do not recap social posts. Do not invent filings, prices, or quotes."
        )
        system = THESIS_SYSTEM_SECTOR
    else:
        user = (
            f"Listed name: {stock}\n"
            f"Mood score: {score_s}\n"
            f"Mood label: {label or 'unknown'}\n"
            f"Counts: bull={bull} bear={bear} neutral={neutral}\n"
            f"Company news and operating points:\n{fact_block}\n"
            "Write a short mood line that states the mood score, plus a well thought-out two-sided case.\n"
            "Use the given news and operating points. Ordinary English. Do not recap social posts. Do not invent filings, prices, or quotes."
        )
        system = THESIS_SYSTEM
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        if chat is not None:
            raw = chat(messages)
        else:
            raw = _chat_grok(messages, schema=GROK_THESIS_SCHEMA)
    except Exception:
        return _empty_thesis()
    return _parse_thesis(raw)


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

    wrapped = [
        json.dumps({"text": '{"labels":[{"i":0,"label":"bull","why":"from text"}]}'}),
        json.dumps({"result": '{"labels":[{"i":0,"label":"bear","why":"from result"}]}'}),
        json.dumps({"content": '{"labels":[{"i":0,"label":"spam","why":"from content"}]}'}),
        json.dumps(
            {
                "text": '{"labels":[{"i":0,"label":"bull","why":"envelope"}]}',
                "stopReason": "end_turn",
                "sessionId": "abc",
            }
        ),
        json.dumps({"content": [{"type": "text", "text": '{"labels":[{"i":0,"label":"bear","why":"parts"}]}'}]}),
        "noise before\n```json\n{\"labels\":[{\"i\":0,\"label\":\"neutral\",\"why\":\"fenced\"}]}\n```\nafter",
        "log line\n{\"labels\":[{\"i\":0,\"label\":\"bull\",\"why\":\"logs\"}]}\nmore log",
    ]
    want = ["bull", "bear", "spam", "bull", "bear", "neutral", "bull"]
    for blob, lab in zip(wrapped, want):
        got = _parse_labels(blob, 1)
        if got[0]["label"] != lab:
            print(f"FAIL unwrap want={lab} got={got[0]['label']}")
            failed += 1

    def one_bad(messages: list[dict[str, str]]) -> str:
        blob = messages[-1]["content"]
        if "[0]" in blob and "first post" in blob:
            return "not json at all"
        return json.dumps({"labels": [{"i": 0, "label": "bull", "why": "ok"}]})

    mixed = classify_posts(
        [
            {"id": "a", "text": "first post about the name"},
            {"id": "b", "text": "second post about the name"},
        ],
        symbol="ZZZ",
        chat=one_bad,
    )
    if mixed[0]["classification"] != "neutral" or mixed[0]["reason"] != SKIP_WHY:
        print("FAIL skip-unparsed got=" + mixed[0]["classification"] + "/" + mixed[0]["reason"])
        failed += 1
    if mixed[1]["classification"] != "neutral" or mixed[1]["reason"] != SKIP_WHY:
        print("FAIL skip-rest got=" + mixed[1]["classification"])
        failed += 1

    cmd = _grok_cmd("/tmp/grok", "prompt")
    need = ["--verbatim", "--json-schema", "--output-format", "json", "--max-turns", "1", "--no-subagents", "--disable-web-search", "--reasoning-effort", "low", "--cwd", "--rules"]
    if any(flag not in cmd for flag in need) or "plain" in cmd:
        print("FAIL grok flags " + " ".join(cmd))
        failed += 1
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    enum = (((schema.get("properties") or {}).get("labels") or {}).get("items") or {}).get("properties", {}).get("label", {}).get("enum")
    if enum != ["bull", "bear", "neutral", "spam"]:
        print("FAIL json-schema enum")
        failed += 1
    s, lab = score_from_counts(8, 0)
    if s != 100 or lab != "Extreme bullish":
        print("FAIL score 100")
        failed += 1
    s, lab = score_from_counts(1, 0, 1)
    if s == 100:
        print("FAIL score 1,0,1 is 100")
        failed += 1

    parsed_th = _parse_thesis('{"summary":"Mixed mood.","bull":"Sales can grow.","bear":"Costs may stay high."}')
    if parsed_th != {"summary": "Mixed mood.", "bull": "Sales can grow.", "bear": "Costs may stay high."}:
        print("FAIL thesis parse")
        failed += 1
    wrapped_th = _parse_thesis('noise before\n{"summary":"Quiet.","bull":"Demand.","bear":"Debt."}\nmore log')
    if wrapped_th["summary"] != "Quiet." or wrapped_th["bull"] != "Demand." or wrapped_th["bear"] != "Debt.":
        print("FAIL thesis unwrap")
        failed += 1

    def fake_thesis(messages: list[dict[str, str]]) -> str:
        return json.dumps({"summary": "Steady.", "bull": "Customers stay.", "bear": "Rivals catch up."})

    written = write_thesis("ZZZ", "Zed Co", chat=fake_thesis)
    if written != {"summary": "Steady.", "bull": "Customers stay.", "bear": "Rivals catch up."}:
        print("FAIL write_thesis")
        failed += 1

    def capture_kind(messages: list[dict[str, str]]) -> str:
        blob = messages[0]["content"] + "\n" + messages[-1]["content"]
        if "Industry:" not in messages[-1]["content"]:
            raise AssertionError("sector prompt missing Industry:")
        if "Caterpillar" in blob:
            raise AssertionError("sector prompt mixed in a stock legal name")
        n = len(re.findall(r"\[(\d+)\] ", messages[-1]["content"]))
        return json.dumps({"labels": [{"i": i, "label": "bull", "why": "hold nudge"} for i in range(n)]})

    sector_scored = classify_posts(
        [{"id": "s", "text": "Should I hold software infrastructure here?"}],
        symbol="Software - Infrastructure",
        name="Technology",
        kind="sector",
        chat=capture_kind,
    )
    if sector_scored[0]["classification"] != "bull":
        print("FAIL sector-score got=" + sector_scored[0]["classification"])
        failed += 1
    if "hold-nudge" not in SYSTEM or "hold-nudge" not in SYSTEM_SECTOR:
        print("FAIL hold-nudge missing from scoring prompt")
        failed += 1

    def boom(_messages: list[dict[str, str]]) -> str:
        raise ScoreError("grok failed")

    if write_thesis("ZZZ", chat=boom) != _empty_thesis():
        print("FAIL thesis empty on error")
        failed += 1

    tcmd = _grok_cmd("/tmp/grok", "prompt", schema=GROK_THESIS_SCHEMA)
    if tcmd[tcmd.index("--reasoning-effort") + 1] != "low" or "none" in tcmd or "--always-approve" in tcmd:
        print("FAIL thesis grok flags " + " ".join(tcmd))
        failed += 1
    tschema = json.loads(tcmd[tcmd.index("--json-schema") + 1])
    props = tschema.get("properties") or {}
    if not all(k in props for k in ("summary", "bull", "bear")):
        print("FAIL thesis json-schema")
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
