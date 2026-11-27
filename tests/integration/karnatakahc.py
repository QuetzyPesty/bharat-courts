#!/usr/bin/env python3
"""Live checks for the High Court of Karnataka website client.

Covers ``KarnatakaHCClient`` (case types, case_status with every section,
download_order_pdf, search_by_party, search_by_advocate, open_search_result)
against a known case, cross-checks the same case through HC Services, then
looks up a *different* case each day per bench, picked from a live party
search, so all three benches (Bengaluru, Dharwad, Kalaburagi) get exercised.

The site is reachable from Indian IPs; from elsewhere it usually times out
(reported as SKIP via the portal-outage rule).

Usage::

    python tests/integration/karnatakahc.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _common import Check, Suite, first_working, require, rotate  # noqa: E402

require("ddddocr", "ocr")

suite = Suite(
    "Karnataka HC website live checks",
    results_dir="/tmp/bharat_courts_karnatakahc_test",
    backend="karnatakahc",
)

# WA 1035/2026, Bengaluru: a pending writ appeal with a daily-order PDF (verified 2026-11-28).
KNOWN = {
    "bench": "Bengaluru",
    "case_type": "WA",
    "number": "1035",
    "year": "2026",
    "cnr": "KAHC010226192026",
}
PARTIES = ["STATE OF KARNATAKA", "UNION OF INDIA", "BANK", "CORPORATION", "COMMISSIONER"]
BENCHES = {"Bengaluru": "KAHC01", "Dharwad": "KAHC02", "Kalaburagi": "KAHC03"}
shared: dict = {}


@suite.check("Case types from the site", "KarnatakaHCClient.case_types", "resolve_case_type")
async def check_case_types(c: Check) -> None:
    from bharat_courts import KarnatakaHCClient

    async with KarnatakaHCClient() as khc:
        types = await khc.case_types()
        code = await khc.resolve_case_type("Crl.P.")
    c.details["types"] = len(types)
    c.details["Crl.P."] = code
    assert len(types) > 30, f"only {len(types)} case types"
    assert code == "CRL.P"
    c.passed = True


@suite.check("Regression: WA 1035/2026 with every section", "KarnatakaHCClient.case_status")
async def check_known_case(c: Check) -> None:
    from bharat_courts import KarnatakaHCClient

    async with KarnatakaHCClient() as khc:
        case = await khc.case_status(
            bench=KNOWN["bench"],
            case_type=KNOWN["case_type"],
            number=KNOWN["number"],
            year=KNOWN["year"],
        )
        assert case is not None, "the site has no such case"
        c.details["cnr"] = case.cnr_number
        c.details["next"] = str(case.next_hearing_date)
        c.details["orders"] = len(case.orders)
        c.details["history"] = len(case.history)
        assert case.cnr_number == KNOWN["cnr"], f"CNR mismatch: {case.cnr_number}"
        assert case.petitioners and case.respondents, "no parties"
        assert case.history, "no daily-order history"
        assert any(o.order_text for o in case.orders), "no daily-order text"
        with_pdf = [o for o in case.orders if o.pdf_url]
        assert with_pdf, "no order carries a PDF token"
        data = await khc.download_order_pdf(with_pdf[0].pdf_url)
        c.details["pdf"] = f"{len(data):,} bytes"
        assert data[:4] == b"%PDF"
    shared["case"] = case
    suite.save_json("wa_1035_2026", case.to_dict(exclude_none=True))
    c.passed = True


@suite.check("A case the site does not have is None", "KarnatakaHCClient.case_status(not found)")
async def check_not_found(c: Check) -> None:
    from bharat_courts import KarnatakaHCClient

    async with KarnatakaHCClient() as khc:
        case = await khc.case_status(bench="Dharwad", case_type="WP", number="999999", year="2026")
    assert case is None, f"got {case!r}"
    c.passed = True


@suite.check("Cross-check via HC Services (same CNR)", "HCServicesClient.case_status_by_cnr")
async def check_hcservices(c: Check) -> None:
    from bharat_courts import HCServicesClient

    async with HCServicesClient() as hc:
        detail = await hc.case_status_by_cnr(KNOWN["cnr"])
    c.details["hcservices"] = (
        f"{detail.case_type} {detail.registration_number} next={detail.next_hearing_date}"
    )
    assert detail.cnr_number == KNOWN["cnr"]
    case = shared.get("case")
    if case:
        c.details["website"] = f"{case.case_number} next={case.next_hearing_date}"
    c.passed = True


@suite.check(
    "Rotating case per bench from a live party search",
    "KarnatakaHCClient.search_by_party",
    "open_search_result",
)
async def check_rotating(c: Check) -> None:
    from bharat_courts import KarnatakaHCClient

    to = date.today()
    async with KarnatakaHCClient() as khc:
        for bench, prefix in BENCHES.items():

            async def search(party: str, bench=bench):
                return await khc.search_by_party(
                    bench=bench, name=party, filed_from=to - timedelta(days=60), filed_to=to
                )

            party, rows = await first_working(
                c, rotate(PARTIES), search, label=f"{bench} party", accept=lambda r: bool(r)
            )
            assert rows, f"{bench}: no party search found anything"
            row = rotate(rows, 1)[0]
            case = await khc.open_search_result(row["open_key"])
            assert case is not None, f"{bench}: {row['case_number']} did not open"
            assert case.cnr_number.startswith(prefix), (
                f"{bench}: CNR {case.cnr_number} lacks {prefix}"
            )
            c.details[bench] = f"{case.case_number} {case.cnr_number} next={case.next_hearing_date}"
    c.passed = True


@suite.check("Advocate search", "KarnatakaHCClient.search_by_advocate")
async def check_advocate(c: Check) -> None:
    from bharat_courts import KarnatakaHCClient

    case = shared.get("case")
    name = (case.petitioner_advocate if case else "") or "KUMAR"
    name = name.split(".")[-1].strip() or name
    async with KarnatakaHCClient() as khc:
        rows = await khc.search_by_advocate(bench="Bengaluru", name=name)
    c.details["advocate"] = name
    c.details["cases"] = len(rows)
    assert rows, f"no cases for advocate {name!r}"
    c.passed = True


if __name__ == "__main__":
    suite.main()
