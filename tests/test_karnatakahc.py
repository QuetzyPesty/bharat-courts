"""High Court of Karnataka website client: parser and client, offline.

Fixtures in tests/fixtures/karnatakahc/ copy the live markup (verified 2026-11-28)
with made-up parties, advocates and handles.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from bharat_courts.captcha.base import CaptchaSolver
from bharat_courts.config import BharatCourtsConfig
from bharat_courts.karnatakahc import KarnatakaHCClient, endpoints
from bharat_courts.karnatakahc import parser as p
from bharat_courts.karnatakahc.client import (
    KarnatakaCaptchaError,
    KarnatakaSearchError,
    digits_only,
    order_pdf_url,
)
from bharat_courts.pdf import OrderNotUploadedError

FIX = Path(__file__).parent / "fixtures" / "karnatakahc"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- endpoints


@pytest.mark.parametrize(
    "given,letter",
    [
        ("Bengaluru", "B"),
        ("bengaluru bench", "B"),
        ("Bangalore", "B"),
        ("D", "D"),
        ("dharwad", "D"),
        ("Kalaburagi", "K"),
        ("Gulbarga", "K"),
        ("k", "K"),
    ],
)
def test_bench_letter(given, letter):
    assert endpoints.bench_letter(given) == letter


def test_bench_letter_rejects_other_places():
    with pytest.raises(ValueError):
        endpoints.bench_letter("Mysuru")


@pytest.mark.parametrize(
    "cnr,letter",
    [
        ("KAHC010226192026", "B"),
        ("kahc020195112026", "D"),
        ("KAHC030108662026", "K"),
        ("DLHC010001232026", None),
    ],
)
def test_bench_from_cnr(cnr, letter):
    assert endpoints.bench_from_cnr(cnr) == letter


def test_case_status_form_joins_with_stars():
    form = endpoints.case_status_form(
        bench="Dharwad", case_type="CRL.P", number="0583", year=2025, captcha="123456"
    )
    assert form == {"benchval": "D*CRL.P*583*2025*123456"}


def test_name_search_form():
    form = endpoints.name_search_form(
        bench="B",
        case_type="",
        name="SAMPLE",
        side="respondent",
        captcha="111111",
        from_date="01-01-2026",
        to_date="27-11-2026",
    )
    assert form == {"benchval": "B*0*SAMPLE*2*111111*01-01-2026*27-11-2026"}


@pytest.mark.parametrize("raw,clean", [("14214o", "142140"), (" 9l2 4S6 ", "912456"), ("abc", "6")])
def test_digits_only(raw, clean):
    assert digits_only(raw) == clean


# ---------------------------------------------------------------- parser


def test_case_page():
    c = p.parse_case_page(fx("case_page.html"))
    assert c["case_number"] == "WA 1234/2026"
    assert (c["case_type"], c["number"], c["year"], c["cnr"]) == (
        "WA",
        "1234",
        "2026",
        "KAHC010999992026",
    )
    assert c["status"] == "Pending"
    # The site leaves the petitioner's <span> unclosed: only its own text, not the next fields.
    assert c["petitioner"] == "ASHA EXAMPLE TRUST"
    assert c["petitioner_advocate"] == "R.K.SAMPLE"
    assert c["respondent"] == "STATE OF KARNATAKA"
    assert c["judge"] == "CHIEF JUSTICE AND A.B. EXAMPLE"
    assert c["next_hearing_date"] == "08/02/2027"
    assert c["last_action_date"] == "26/11/2026"
    assert c["case_handle"] == "Q0FTRUhBTkRMRQ=="
    assert c["filing_handle"] == "RklMSU5HSEFORExF"


def test_case_page_not_found():
    assert p.parse_case_page(fx("not_found.html")) is None
    assert p.is_not_found(fx("not_found.html"))


def test_parties_keep_name_and_advocate_not_address():
    pet, res = p.parse_parties(fx("parties.html"))
    assert [(x.name, x.advocate) for x in pet] == [
        ("ASHA EXAMPLE TRUST", "R.K.SAMPLE"),
        ("SRI RAVI SAMPLE", ""),
    ]
    assert [(x.name, x.advocate) for x in res] == [("STATE OF KARNATAKA", "AGA EXAMPLE")]
    assert "BENGALURU" not in " ".join(x.name for x in pet + res)


def test_daily_orders():
    rows = p.parse_daily_orders(fx("daily_orders.html"))
    assert [r["date"] for r in rows] == [date(2026, 11, 26), date(2026, 9, 15), date(2026, 4, 9)]
    assert rows[0]["action"] == "ADJOURNED"
    assert rows[0]["judge"] == "CHIEF JUSTICE AND A.B. EXAMPLE"
    assert "list on 08.02.2027" in rows[0]["text"]
    assert "Last Updated" not in rows[0]["text"] and "20:23" not in rows[0]["text"]
    assert rows[0]["token"] == ""
    assert rows[1]["token"] == "T1JERVJUT0tFTg=="
    assert rows[2]["judge"].startswith("REGISTRAR")


def test_sections():
    assert p.parse_linked_cases(fx("linked_cases.html")) == ["WA 2000/2025 (Main)", "WA 1234/2026"]
    ias = p.parse_ias(fx("ias.html"))
    assert [(i["number"], i["classification"]) for i in ias] == [
        ("1/2026", "Condonation Delay in filing"),
        ("2/2026", "Stay"),
    ]
    assert p.parse_lower_court(fx("lower_court.html")) == [
        {"case_number": "WP 16000/2025", "court": "High Court", "date": date(2025, 11, 28)}
    ]
    assert p.parse_judgments(fx("judgments_none.html")) == []


def test_search_results():
    rows = p.parse_search_results(fx("search_results.html"), "Bengaluru")
    assert [r["case_number"] for r in rows] == ["WA 1234/2026", "CRL.P 10460/2026"]
    assert rows[1]["case_type"] == "CRL.P"
    assert (
        rows[0]["petitioner"] == "ASHA EXAMPLE TRUST"
        and rows[0]["respondent"] == "STATE OF KARNATAKA"
    )
    assert rows[1]["respondent_advocate"] == "SPP EXAMPLE"
    assert rows[0]["open_key"] == "B*WA*1234*2026*123456"
    assert rows[0]["filing_date"] == date(2026, 3, 25)


def test_case_types_and_canonical():
    types = p.parse_case_types(fx("case_types.html"))
    assert types[0] == ("CRL.P", "CRL.P - Criminal Petition") and len(types) == 4
    assert p.canonical_type("Crl.P.") == p.canonical_type("CRL.P") == "CRLP"


@pytest.mark.parametrize(
    "text,ok",
    [
        ("26/11/2026", date(2026, 11, 26)),
        ("2026-03-25", date(2026, 3, 25)),
        ("25/03/2026 16:41:50", date(2026, 3, 25)),
        ("", None),
        ("01/01/5000", None),
    ],
)
def test_parse_date(text, ok):
    assert p.parse_date(text) == ok


# ---------------------------------------------------------------- client


class Answers(CaptchaSolver):
    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls = 0

    async def solve(self, image_bytes: bytes) -> str:
        self.calls += 1
        return self.answers.pop(0) if self.answers else "000000"


def cfg() -> BharatCourtsConfig:
    return BharatCourtsConfig(request_delay=0, timeout=5, max_retries=1)


def _mock_site(router: respx.Router, *, case_answers: list[str]) -> dict:
    seen: dict = {"benchvals": [], "sections": []}
    router.get(endpoints.CASE_MENU_URL).respond(200, text="<html></html>")
    router.get(url__regex=r".*/captcha\.php.*").respond(200, content=b"\xff\xd8jpeg")
    router.post(endpoints.CASE_TYPES_URL).respond(200, text=fx("case_types.html"))
    answers = iter(case_answers)

    def case_status(request: httpx.Request):
        seen["benchvals"].append(parse_qs(request.content.decode())["benchval"][0])
        return httpx.Response(200, text=next(answers))

    router.post(endpoints.CASE_STATUS_URL).mock(side_effect=case_status)
    pages = {
        "H1": "parties.html",
        "H3": "daily_orders.html",
        "H4": "linked_cases.html",
        "H5": "judgments_none.html",
        "H9": "ias.html",
        "H2": "lower_court.html",
    }

    def section(request: httpx.Request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        seen["sections"].append(form)
        return httpx.Response(200, text=fx(pages[form["div"]]))

    router.post(endpoints.SECTION_URL).mock(side_effect=section)
    return seen


@respx.mock
async def test_case_status_retries_refused_captcha_and_reads_every_section():
    seen = _mock_site(respx.mock, case_answers=[fx("captcha_refused.txt"), fx("case_page.html")])
    solver = Answers("12345", "111111", "222222")  # five digits is never sent
    async with KarnatakaHCClient(config=cfg(), captcha_solver=solver) as khc:
        code = await khc.resolve_case_type("w.a.")
        case = await khc.case_status(bench="Bengaluru", case_type=code, number="1234", year="2026")
    assert seen["benchvals"] == ["B*WA*1234*2026*111111", "B*WA*1234*2026*222222"]
    assert solver.calls == 3
    assert case.case_number == "WA 1234/2026" and case.cnr_number == "KAHC010999992026"
    assert case.bench == "Bengaluru" and case.court_name == "High Court of Karnataka"
    assert case.next_hearing_date == date(2027, 2, 8)
    assert case.last_action_date == date(2026, 11, 26)
    assert case.coram == "CHIEF JUSTICE AND A.B. EXAMPLE"
    assert case.case_stage == "WRIT APPEAL MATTER/S"
    assert [x.name for x in case.petitioners] == ["ASHA EXAMPLE TRUST", "SRI RAVI SAMPLE"]
    assert len(case.history) == 3 and case.first_hearing_date == date(2026, 4, 9)
    assert [o.order_date for o in case.orders] == [
        date(2026, 11, 26),
        date(2026, 9, 15),
        date(2026, 4, 9),
    ]
    assert case.orders[1].pdf_url == order_pdf_url("T1JERVJUT0tFTg==")
    assert case.orders[0].pdf_url == "" and "08.02.2027" in case.orders[0].order_text
    assert case.linked_cases[0] == "WA 2000/2025 (Main)"
    assert case.interlocutory_applications[1]["classification"] == "Stay"
    assert case.lower_court[0]["case_number"] == "WP 16000/2025"
    assert all(
        s["casenum"] == "Q0FTRUhBTkRMRQ==" and s["lda"] == "26/11/2026" for s in seen["sections"]
    )
    assert case.to_dict()["source"] == "karnatakahc"


@respx.mock
async def test_case_status_not_found_is_none():
    _mock_site(respx.mock, case_answers=[fx("not_found.html")])
    async with KarnatakaHCClient(config=cfg(), captcha_solver=Answers("123456")) as khc:
        assert (
            await khc.case_status(bench="D", case_type="WP", number="999999", year="2026") is None
        )


@respx.mock
async def test_captcha_gives_up():
    _mock_site(respx.mock, case_answers=[fx("captcha_refused.txt")] * 3)
    async with KarnatakaHCClient(
        config=cfg(), captcha_solver=Answers("111111", "222222", "333333")
    ) as khc:
        with pytest.raises(KarnatakaCaptchaError):
            await khc._post_with_captcha(
                endpoints.CASE_STATUS_URL, lambda c: {"benchval": c}, max_attempts=3
            )


@respx.mock
async def test_unknown_case_type():
    _mock_site(respx.mock, case_answers=[])
    async with KarnatakaHCClient(config=cfg(), captcha_solver=Answers()) as khc:
        with pytest.raises(ValueError):
            await khc.resolve_case_type("XYZ")


@respx.mock
async def test_search_by_advocate_and_open_result_without_new_captcha():
    respx.get(endpoints.CASE_MENU_URL).respond(200, text="")
    respx.get(url__regex=r".*/captcha\.php.*").respond(200, content=b"img")
    adv = respx.post(endpoints.ADVOCATE_URL).respond(200, text=fx("search_results.html"))
    status = respx.post(endpoints.CASE_STATUS_URL).respond(200, text=fx("case_page.html"))
    respx.post(endpoints.SECTION_URL).respond(200, text=fx("judgments_none.html"))
    solver = Answers("123456")
    async with KarnatakaHCClient(config=cfg(), captcha_solver=solver) as khc:
        rows = await khc.search_by_advocate(
            bench="Bengaluru",
            name="SAMPLE",
            filed_from=date(2026, 1, 1),
            filed_to=date(2026, 11, 27),
        )
        case = await khc.open_search_result(rows[0]["open_key"])
    assert parse_qs(adv.calls[0].request.content.decode())["benchval"] == [
        "B*0*SAMPLE*0*123456*01-01-2026*27-11-2026"
    ]
    assert parse_qs(status.calls[0].request.content.decode())["benchval"] == [
        "B*WA*1234*2026*123456"
    ]
    assert solver.calls == 1
    assert case.case_number == "WA 1234/2026"


async def test_name_search_limits_are_checked_before_asking_the_site():
    async with KarnatakaHCClient(config=cfg(), captcha_solver=Answers()) as khc:
        with pytest.raises(KarnatakaSearchError):
            await khc.search_by_party(bench="B", name="ABC")
        with pytest.raises(KarnatakaSearchError):
            await khc.search_by_party(
                bench="B", name="EXAMPLE", filed_from=date(2024, 1, 1), filed_to=date(2026, 1, 1)
            )


@respx.mock
async def test_download_order_pdf_from_json():
    pdf = b"%PDF-1.6 example"
    respx.get(endpoints.CASE_MENU_URL).respond(200, text="")
    route = respx.post(endpoints.ORDER_PDF_URL).respond(
        200,
        text=json.dumps({"status": 2, "data": base64.b64encode(pdf).decode(), "filename": "x.pdf"}),
    )
    async with KarnatakaHCClient(config=cfg(), captcha_solver=Answers()) as khc:
        assert await khc.download_order_pdf(order_pdf_url("T1JE+RVJ/UT0==")) == pdf
    assert parse_qs(route.calls[0].request.content.decode())["data"] == ["T1JE+RVJ/UT0=="]


@respx.mock
async def test_download_order_pdf_falls_back_then_reports_not_uploaded():
    respx.get(endpoints.CASE_MENU_URL).respond(200, text="")
    respx.post(endpoints.ORDER_PDF_URL).respond(
        200, text=json.dumps({"status": 1, "message": {"status": "x"}})
    )
    fb = respx.post(endpoints.ORDER_PDF_FALLBACK_URL).respond(200, text="Order not uploaded yet")
    async with KarnatakaHCClient(config=cfg(), captcha_solver=Answers()) as khc:
        khc._last_handle = "HANDLE"
        with pytest.raises(OrderNotUploadedError):
            await khc.download_order_pdf("TOKEN")
    assert parse_qs(fb.calls[0].request.content.decode()) == {
        "rdata": ["TOKEN"],
        "casenum": ["HANDLE"],
    }
