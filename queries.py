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


def canonical_industry(industry: str, taxonomy: list[dict[str, object]] | None = None) -> str:
    # Yahoo search uses an em dash; the picker list uses a hyphen.
    want = _norm_name(industry)
    if not want:
        return ""
    tax = taxonomy or load_taxonomy()
    for row in tax:
        for name in row.get("industries") or []:
            n = str(name).strip()
            if _norm_name(n) == want:
                return n
    return ""


def resolve_sector_subject(
    industry: str,
    taxonomy: list[dict[str, object]] | None = None,
) -> tuple[str, str, bool]:
    """Picker spelling and real Yahoo parent, or a free theme with no parent.

    is_theme is True when the string is not a Yahoo industry. Themes never get
    a fabricated parent sector.
    """
    raw = (industry or "").strip()
    if not raw:
        return "", "", False
    tax = taxonomy if taxonomy is not None else load_taxonomy()
    canon = canonical_industry(raw, tax)
    if canon:
        return canon, parent_sector(canon, tax), False
    return raw, "", True


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
    """Quoted industry phrase only. Parent sector is too broad (Industrials is not Waste Management)."""
    industry = (industry or "").strip()
    if not industry:
        return "-is:retweet"
    return f'"{industry}" -is:retweet'


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
    wm = sector_query("Waste Management", "Industrials")
    check("Waste Management" in wm, "Waste Management missing from industry query")
    check("Industrials" not in wm, "parent sector must not be in the X search got=" + wm)


    tax = load_taxonomy()
    n_sec = len(tax)
    n_ind = sum(len(row["industries"]) for row in tax)
    check(n_sec == 11, f"Yahoo sector count want=11 got={n_sec}")
    check(n_ind == 145, f"Yahoo industry count want=145 got={n_ind}")
    check(parent_sector(industry, tax) == "Technology", "parent sector for Software - Infrastructure")
    names = {n for row in tax for n in row["industries"]}
    check("Software - Infrastructure" in names, "taxonomy missing Software - Infrastructure")
    check("Bogus Sector" not in names, "taxonomy grew a made-up industry")
    check(canonical_industry("Software—Infrastructure", tax) == "Software - Infrastructure", "em dash must map to picker spelling")
    check(canonical_industry("not a yahoo industry", tax) == "", "unknown industry must stay empty")
    check(canonical_industry("quantum", tax) == "", "canonical_industry(quantum) must be empty")
    check("Quantum" not in names and "quantum" not in {n.casefold() for n in names}, "taxonomy must not grow a Quantum industry")

    qtm = sector_query("quantum")
    check('"quantum"' in qtm, "sector_query(quantum) must contain quoted quantum got=" + qtm)
    check(qtm == '"quantum" -is:retweet', "sector_query(quantum) want=\"quantum\" -is:retweet got=" + qtm)
    check("Technology" not in qtm and "Industrials" not in qtm, "theme query must not add a parent sector got=" + qtm)

    theme_name, theme_parent, is_theme = resolve_sector_subject("quantum", tax)
    check(is_theme is True, "quantum must resolve as a theme")
    check(theme_name == "quantum", "theme keeps the typed phrase got=" + theme_name)
    check(theme_parent == "", "theme must not invent a parent sector got=" + theme_parent)
    check(resolve_sector_subject("", tax) == ("", "", False), "empty industry must stay empty")

    wm_name, wm_parent, wm_theme = resolve_sector_subject("waste management", tax)
    check(wm_theme is False, "Waste Management must stay a Yahoo industry")
    check(wm_name == "Waste Management", "Waste Management must use picker spelling got=" + wm_name)
    check(wm_parent == "Industrials", "Waste Management parent want=Industrials got=" + wm_parent)

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
