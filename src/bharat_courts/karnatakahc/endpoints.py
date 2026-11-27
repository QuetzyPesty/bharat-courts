"""URLs and form builders for the High Court of Karnataka's website.

Portal: https://judiciary.karnataka.gov.in/casemenu.php ("Case Status")

The page is plain PHP + jQuery. Every search is one POST whose single field,
``benchval``, joins the form's values with ``*``; the last value is the CAPTCHA
(``captcha.php``: six digits, bound to the PHP session cookie):

- case number:  ``casestatus.php``     ``B*WA*1035*2026*<captcha>``
- advocate:     ``advocatestatus.php``
  ``B*<type or 0>*<name>*<1 pet | 2 resp | 0 either>*<captcha>*<from>*<to>``
- party:        ``partystatus.php``    same shape as advocate

Answers: ``2`` = wrong CAPTCHA; ``3`` = the date range is longer than a year;
``4`` = the name is shorter than four letters; otherwise an HTML fragment.

The case page carries two encrypted handles (``case_no_val``, ``fr_no_val``);
each section of the case (parties, daily orders, IAs, ...) is then
``case_details_div.php`` with ``casenum=<handle>&lda=<last date of action>&div=H<n>``.
An order PDF is ``napix/highcourtorderdetails.php`` with ``data=<token from the
order's viewjud("...")>``, answering JSON ``{"status": 2, "data": <base64>, "filename"}``;
when that fails the site itself retries ``fetch_cis_judgment.php`` (``rdata``, ``casenum``).
"""

from __future__ import annotations

BASE_URL = "https://judiciary.karnataka.gov.in"

CASE_MENU_URL = f"{BASE_URL}/casemenu.php"
CAPTCHA_URL = f"{BASE_URL}/captcha.php"
CASE_TYPES_URL = f"{BASE_URL}/loadcasetype.php"
CASE_STATUS_URL = f"{BASE_URL}/casestatus.php"
ADVOCATE_URL = f"{BASE_URL}/advocatestatus.php"
PARTY_URL = f"{BASE_URL}/partystatus.php"
SECTION_URL = f"{BASE_URL}/case_details_div.php"
ORDER_PDF_URL = f"{BASE_URL}/napix/highcourtorderdetails.php"
ORDER_PDF_FALLBACK_URL = f"{BASE_URL}/fetch_cis_judgment.php"

# bench name -> the site's bench letter. The CNR's first six characters give the bench too.
BENCHES: dict[str, str] = {"bengaluru": "B", "dharwad": "D", "kalaburagi": "K"}
BENCH_NAMES: dict[str, str] = {"B": "Bengaluru", "D": "Dharwad", "K": "Kalaburagi"}
CNR_BENCH: dict[str, str] = {"KAHC01": "B", "KAHC02": "D", "KAHC03": "K"}
_BENCH_ALIASES = {
    "bangalore": "B",
    "bengaluru": "B",
    "blr": "B",
    "dharwad": "D",
    "dwd": "D",
    "kalaburagi": "K",
    "kalburagi": "K",
    "gulbarga": "K",
    "klb": "K",
}

# Party side for advocate/party searches.
SIDES: dict[str, str] = {"petitioner": "1", "respondent": "2", "either": "0"}

# Case-page sections (case_details_div.php ``div``). H8 (scrutiny) takes the filing handle.
SECTIONS: dict[str, str] = {
    "parties": "H1",
    "lower_court": "H2",
    "daily_orders": "H3",
    "linked_cases": "H4",
    "judgments": "H5",
    "scrutiny": "H8",
    "ias": "H9",
    "documents": "H10",
    "caveats": "H12",
    "prayer": "H15",
}

CAPTCHA_REJECTED = "2"
RANGE_TOO_LONG = "3"
NAME_TOO_SHORT = "4"


def bench_letter(bench: str) -> str:
    """'Bengaluru' / 'bengaluru bench' / 'B' / 'Gulbarga' -> the site's letter.

    Raises:
        ValueError: not one of the three benches.
    """
    b = (bench or "").strip()
    if b.upper() in BENCH_NAMES:
        return b.upper()
    letter = _BENCH_ALIASES.get(b.lower().replace("bench", "").strip())
    if not letter:
        raise ValueError(
            f"Unknown Karnataka HC bench {bench!r}: use Bengaluru, Dharwad or Kalaburagi"
        )
    return letter


def bench_from_cnr(cnr: str) -> str | None:
    """'KAHC020195112026' -> 'D'; None if it is not a Karnataka HC CNR."""
    return CNR_BENCH.get((cnr or "").strip().upper()[:6])


def case_status_form(
    *, bench: str, case_type: str, number: str | int, year: str | int, captcha: str
) -> dict[str, str]:
    return {
        "benchval": "*".join(
            [bench_letter(bench), case_type, str(int(number)), str(int(year)), captcha]
        )
    }


def name_search_form(
    *, bench: str, case_type: str, name: str, side: str, captcha: str, from_date: str, to_date: str
) -> dict[str, str]:
    """Advocate/party search. Dates are dd-mm-yyyy filing dates, at most a year apart."""
    return {
        "benchval": "*".join(
            [
                bench_letter(bench),
                case_type or "0",
                name,
                SIDES.get(side, side),
                captcha,
                from_date,
                to_date,
            ]
        )
    }


def section_form(*, handle: str, last_action_date: str, div: str) -> dict[str, str]:
    return {"casenum": handle, "lda": last_action_date, "div": div}
