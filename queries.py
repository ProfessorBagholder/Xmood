#!/usr/bin/env python3
"""X search query builders. No network. Used by the local app and by --selftest."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TAXONOMY_PATH = ROOT / "yahoo_sectors.json"


def _norm_name(s: str) -> str:
    text = (s or "").strip().casefold()
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text


def load_taxonomy(path: Path | None = None) -> list[dict[str, object]]:
    raw = json.loads((path or TAXONOMY_PATH).read_text(encoding="utf-8"))
    sectors = raw.get("sectors") if isinstance(raw, dict) else raw
    out: list[dict[str, object]] = []
    if not isinstance(sectors, list):
        return out
    for row in sectors:
        if not isinstance(row, dict):
            continue
        sector = str(row.get("sector") or "").strip()
        inds = row.get("industries") or []
        if not sector or not isinstance(inds, list):
            continue
        names = [str(x).strip() for x in inds if str(x).strip()]
        if names:
            out.append({"sector": sector, "industries": names})
    return out


def parent_sector(industry: str, taxonomy: list[dict[str, object]] | None = None) -> str:
    want = _norm_name(industry)
    if not want:
        return ""
    for row in taxonomy or load_taxonomy():
        for name in row.get("industries") or []:
            if _norm_name(str(name)) == want:
                return str(row.get("sector") or "")
    return ""


def x_stem(symbol: str) -> str:
    tag = (symbol or "").strip().lstrip("$")
    return tag.split(".", 1)[0] if tag else ""


def symbol_query(symbol: str, name: str = "") -> str:
    """Keep the listing tag (QNC.V). Also search the undotted cashtag and the company name.

    Undotted names such as CAT use the cashtag only, with no company name.
    """
    tag = (symbol or "").strip().lstrip("$")
    stem = x_stem(tag)
    bits: list[str] = []
    if "." in tag:
        bits.append('"$' + tag + '"')
        if stem:
            bits.append("$" + stem)
        nm = (name or "").strip()
        if nm:
            bits.append('"' + nm + '"')
    elif stem:
        bits.append("$" + stem)
    if not bits:
        return "-is:retweet"
    return "(" + " OR ".join(bits) + ") -is:retweet"


def sector_query(industry: str, sector: str = "") -> str:
    """Quoted industry phrase, plus parent sector when it is not already in the name. No legal name."""
    industry = (industry or "").strip()
    sector = (sector or "").strip()
    bits: list[str] = []
    if industry:
        bits.append(f'"{industry}"')
    if sector and _norm_name(sector) not in _norm_name(industry):
        bits.append(f'"{sector}"')
    if not bits:
        return "-is:retweet"
    if len(bits) == 1:
        return bits[0] + " -is:retweet"
    return "(" + " OR ".join(bits) + ") -is:retweet"


def _selftest() -> int:
    failed = 0

    def check(ok: bool, msg: str) -> None:
        nonlocal failed
        if not ok:
            print("FAIL " + msg)
            failed += 1

    cat = symbol_query("CAT", "Caterpillar Inc.")
    check(cat == "($CAT) -is:retweet", "CAT want=($CAT) -is:retweet got=" + cat)
    check("$CAT" in cat, "CAT cashtag missing")
    check("Caterpillar" not in cat, "CAT must not include company name")
    check("Inc." not in cat, "CAT must not include legal name")

    qnc = symbol_query("QNC.V", "Quantum eMotion Corp.")
    check("QNC.V" in qnc, "QNC.V must appear in the query got=" + qnc)
    check("$QNC" in qnc, "QNC.V stem cashtag missing got=" + qnc)
    check("Quantum eMotion Corp." in qnc, "QNC.V company name missing got=" + qnc)
    check("Caterpillar" not in qnc, "QNC.V picked up CAT name")

    industry = "Software - Infrastructure"
    sec = sector_query(industry, "Technology")
    check(industry in sec, "sector query must contain the industry name got=" + sec)
    check(sec != cat, "Symbol vs Sector query builders must differ")
    check(sec != qnc, "Sector query must differ from dotted symbol query")
    check("Caterpillar" not in sec and "Quantum" not in sec, "sector query mixed in a stock legal name")
    check("$CAT" not in sec, "sector query must not use a cashtag")
    check("-is:retweet" in sec, "sector query needs -is:retweet")

    tax = load_taxonomy()
    n_sec = len(tax)
    n_ind = sum(len(row["industries"]) for row in tax)
    check(n_sec == 11, f"Yahoo sector count want=11 got={n_sec}")
    check(n_ind == 145, f"Yahoo industry count want=145 got={n_ind}")
    check(parent_sector(industry, tax) == "Technology", "parent sector for Software - Infrastructure")
    names = {n for row in tax for n in row["industries"]}
    check("Software - Infrastructure" in names, "taxonomy missing Software - Infrastructure")
    check("Bogus Sector" not in names, "taxonomy grew a made-up industry")

    if failed:
        print(f"selftest FAIL ({failed})")
        return 1
    print("selftest PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        return _selftest()
    print("usage: python3 queries.py --selftest", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
