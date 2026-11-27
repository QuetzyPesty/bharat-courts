"""Parsers for the High Court of Karnataka website's case-status fragments.

Every answer is an HTML fragment that the site's jQuery drops into the page.
Field values on the case page sit in ``<span id="...">`` elements with stable
ids (``case_no``, ``next_hearing_date`` ...); the sections loaded afterwards
are plain tables. ``RECORD_NOT_FOUND`` marks an empty answer everywhere.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from bharat_courts.models import PartyEntry

NOT_FOUND = "RECORD_NOT_FOUND"

# "WA 1035/2026 (KAHC010226192026)", "CRL.P 10460/2026", "WP 204165/2026"
_CASE_NO_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9.()\- ]*?)\s+(\d+)\s*/\s*(\d{4})\s*(?:\(([A-Z0-9]{16})\))?"
)
_CNR_RE = re.compile(r"\b(KAHC\d{12})\b")
_VIEWJUD_RE = re.compile(r"viewjud\(\s*[\"']([^\"']+)[\"']\s*\)")
_PART_RE = re.compile(r"getcasedetailspart\(\s*[\"']([^\"']+)[\"']\s*\)")


def _text(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def parse_date(text: str) -> date | None:
    """dd/mm/yyyy (optionally with a time), dd-mm-yyyy or yyyy-mm-dd; None otherwise."""
    t = (text or "").strip().split(" ")[0]
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        return d if 1900 < d.year < 3000 else None
    return None


def is_not_found(fragment: str) -> bool:
    return NOT_FOUND in (fragment or "") and "case_no_val" not in (fragment or "")


def split_case_number(text: str) -> tuple[str, str, str, str]:
    """'WA 1035/2026 (KAHC010226192026)' -> ('WA', '1035', '2026', 'KAHC010226192026').

    Returns empty strings for what is not there.
    """
    m = _CASE_NO_RE.match(text or "")
    if not m:
        cnr = _CNR_RE.search(text or "")
        return "", "", "", cnr.group(1) if cnr else ""
    return m.group(1).strip(), m.group(2), m.group(3), m.group(4) or ""


def parse_case_page(fragment: str) -> dict | None:
    """The ``casestatus.php`` answer -> a flat dict, or None for RECORD_NOT_FOUND.

    Keys: status, case_number, case_type, number, year, cnr, classification,
    filing_date, petitioner, petitioner_advocate, respondent, respondent_advocate,
    filing_number, judge, last_posted_for, last_action_date, last_action,
    next_hearing_date, case_handle, filing_handle.
    """
    if is_not_found(fragment) or 'id="case_no"' not in (fragment or ""):
        return None
    soup = BeautifulSoup(fragment, "lxml")

    def span(i: str) -> str:
        return _text(soup.find(id=i))

    def hidden(i: str) -> str:
        el = soup.find("input", id=i)
        return (el.get("value") or "").strip() if el else ""

    status = ""
    label = soup.find(string=re.compile(r"^\s*Status:\s*$"))
    if label:
        nxt = (
            label.find_parent("span").find_next_sibling("span")
            if label.find_parent("span")
            else None
        )
        status = _text(nxt)
    case_number = span("case_no")
    ctype, number, year, cnr = split_case_number(case_number)
    return {
        "status": status,
        "case_number": f"{ctype} {number}/{year}" if number else case_number,
        "case_type": ctype,
        "number": number,
        "year": year,
        "cnr": cnr,
        "classification": span("classification"),
        "filing_date": span("date_of_filing"),
        # The site leaves the petitioner's span unclosed: take only the text before the next field.
        "petitioner": _first_text(soup.find(id="petitioner")),
        "petitioner_advocate": span("pet_advocate"),
        "respondent": _first_text(soup.find(id="respondent")),
        "respondent_advocate": span("resp_advocate"),
        "filing_number": span("filing_no"),
        "judge": span("judge"),
        "last_posted_for": span("last_posted_for"),
        "last_action_date": span("last_date_of_action"),
        "last_action": span("last_action_taken"),
        "next_hearing_date": span("next_hearing_date"),
        "tentative_date": span("tentative_date"),
        "case_handle": hidden("case_no_val"),
        "filing_handle": hidden("fr_no_val"),
    }


def _first_text(el) -> str:
    """The element's own leading text, stopping at the first nested block (for unclosed spans)."""
    if el is None:
        return ""
    parts = []
    for child in el.children:
        if getattr(child, "name", None) in ("div", "span", "table"):
            break
        parts.append(child.get_text(" ") if hasattr(child, "get_text") else str(child))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_parties(fragment: str) -> tuple[list[PartyEntry], list[PartyEntry]]:
    """Section H1 -> (petitioners, respondents), each with the advocate named for them.

    Addresses are not kept: they are on the site for service, not needed to follow a case.
    """
    soup = BeautifulSoup(fragment or "", "lxml")
    out: dict[str, list[PartyEntry]] = {"petitioner": [], "respondent": []}
    for table in soup.find_all("table"):
        side = (table.get("id") or "").lower()
        if side not in out:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            name = _text(tds[1]).rstrip(",. ").strip()
            advocate = ""
            if len(tds) > 2:
                b = tds[2].find("b", string=re.compile(r"Advocate", re.I))
                if b:
                    sib = b.next_sibling
                    advocate = (
                        str(sib).strip() if sib and not getattr(sib, "name", None) else ""
                    ).strip()
            if name:
                out[side].append(PartyEntry(name=name, advocate=advocate))
    return out["petitioner"], out["respondent"]


def parse_daily_orders(fragment: str) -> list[dict]:
    """Section H3 -> one dict per sitting, newest first as the site lists them.

    Keys: seq, date (date | None), judge, action, text (the order as typed into the
    daily-order register, often the whole short order), token (for the PDF, or "").
    """
    soup = BeautifulSoup(fragment or "", "lxml")
    rows = []
    for table in soup.find_all("table"):
        trs = table.find_all("tr", recursive=False) or table.find_all("tr")
        if not trs:
            continue
        head = trs[0].find_all("td")
        if len(head) < 3 or parse_date(_text(head[1])) is None:
            continue
        body = trs[1] if len(trs) > 1 else None
        token, text = "", ""
        if body is not None:
            m = _VIEWJUD_RE.search(str(body))
            token = m.group(1) if m else ""
            cells = body.find_all("td")
            note = cells[-1] if cells else body
            for b in note.find_all("b"):
                if "Last Updated" in b.get_text():
                    for sib in list(b.next_siblings):
                        sib.extract()
                    b.extract()
            text = "\n".join(
                line.strip() for line in note.get_text("\n").splitlines() if line.strip()
            )
        rows.append(
            {
                "seq": _text(head[0]),
                "date": parse_date(_text(head[1])),
                "judge": _text(head[2]),
                "action": _text(head[3]) if len(head) > 3 else "",
                "text": text,
                "token": token,
            }
        )
    return rows


def parse_judgments(fragment: str) -> list[dict]:
    """Section H5 -> [{"date", "judge", "token", "text"}] for every PDF the section links."""
    if is_not_found(fragment):
        return []
    soup = BeautifulSoup(fragment or "", "lxml")
    out = []
    for tr in soup.find_all("tr"):
        m = _VIEWJUD_RE.search(str(tr))
        if not m:
            continue
        cells = [_text(td) for td in tr.find_all("td")]
        when = next((parse_date(c) for c in cells if parse_date(c)), None)
        out.append(
            {
                "date": when,
                "judge": next((c for c in cells if "JUSTICE" in c.upper()), ""),
                "token": m.group(1),
                "text": " ".join(c for c in cells if c),
            }
        )
    return out


def parse_linked_cases(fragment: str) -> list[str]:
    """Section H4 -> ['WA 2059/2025 (Main)', 'WA 2053/2025', ...]."""
    if is_not_found(fragment):
        return []
    soup = BeautifulSoup(fragment or "", "lxml")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            out.append(re.sub(r"\(\s*(\w+)\s*\)", r"(\1)", _text(tds[1])))
    return out


def parse_ias(fragment: str) -> list[dict]:
    """Section H9 -> [{"number": "1/2026", "filed": date, "classification", "party"}]."""
    if is_not_found(fragment):
        return []
    soup = BeautifulSoup(fragment or "", "lxml")
    out = []
    for tr in soup.find_all("tr"):
        tds = [_text(td) for td in tr.find_all("td")]
        if len(tds) >= 3 and re.match(r"\d+/\d{4}$", tds[0]):
            out.append(
                {
                    "number": tds[0],
                    "filed": parse_date(tds[1]),
                    "classification": tds[2],
                    "party": tds[3] if len(tds) > 3 else "",
                }
            )
    return out


def parse_lower_court(fragment: str) -> list[dict]:
    """Section H2 -> [{"case_number", "court", "date"}] (the order under appeal, etc.)."""
    if is_not_found(fragment):
        return []
    soup = BeautifulSoup(fragment or "", "lxml")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 3 and _text(tds[0]):
            out.append(
                {
                    "case_number": _text(tds[0]),
                    "court": _text(tds[1]).replace("()", "").strip(),
                    "date": parse_date(_text(tds[2])),
                }
            )
    return out


def parse_search_results(fragment: str, bench: str) -> list[dict]:
    """Advocate/party search table -> one dict per case.

    Keys: bench, case_number, case_type, number, year, filing_number, filing_date,
    petitioner, respondent, petitioner_advocate, respondent_advocate, last_action_date,
    last_action, open_key (the site's own ``benchval`` for opening the case in the same
    session without another CAPTCHA).
    """
    soup = BeautifulSoup(fragment or "", "lxml")
    out = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        ctype, number, year, _ = split_case_number(_text(tds[2]))
        if not number:
            continue
        m = _PART_RE.search(str(tds[2]))
        pet, _, res = _text(tds[4]).partition(" V/S ")
        pa, _, ra = _text(tds[5]).partition(" V/S")
        out.append(
            {
                "bench": bench,
                "case_number": f"{ctype} {number}/{year}",
                "case_type": ctype,
                "number": number,
                "year": year,
                "filing_number": _text(tds[1]),
                "filing_date": parse_date(_text(tds[3])),
                "petitioner": pet.strip(),
                "respondent": res.strip(),
                "petitioner_advocate": pa.strip(),
                "respondent_advocate": ra.strip(),
                "last_action_date": parse_date(_text(tds[6])),
                "last_action": _text(tds[7]),
                "open_key": m.group(1) if m else "",
            }
        )
    return out


def parse_case_types(fragment: str) -> list[tuple[str, str]]:
    """``loadcasetype.php`` options -> [("WA", "WA - Writ Appeal"), ...], placeholder dropped."""
    soup = BeautifulSoup(fragment or "", "lxml")
    return [
        (o.get("value", "").strip(), _text(o))
        for o in soup.find_all("option")
        if o.get("value", "").strip() not in ("", "0")
    ]


def canonical_type(raw: str) -> str:
    """'Crl.P.' / 'CRL.P' / 'crlp' -> 'CRLP': for matching a typed case type to the site's list."""
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
