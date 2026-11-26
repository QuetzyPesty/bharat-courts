"""Regression tests for order/cause-list PDF downloads and the links behind them.

Each case mirrors markup captured from the live portals on 2026-11-26
(names replaced):

- High Court PDFs arrive behind a UTF-8 byte-order mark (``EF BB BF %PDF``);
  every Karnataka HC order and cause-list PDF was rejected as "not a PDF".
- District order links are ``href='#'`` + ``onclick=displayPdf(...)``, so
  every district order came back with ``pdf_url == "#"``.
- ``HCServicesClient.court_orders`` returned one "Order" dated today built
  from the search's ``orderurlpath``; the portal refused that link.
- District CNRs with digits in the prefix (``MP04...``) were not recognised.
"""

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from bharat_courts.captcha.base import CaptchaSolver
from bharat_courts.casedetail import parse_case_detail
from bharat_courts.config import BharatCourtsConfig
from bharat_courts.courts import get_court
from bharat_courts.districtcourts import endpoints as dc_endpoints
from bharat_courts.districtcourts.client import DistrictCourtClient
from bharat_courts.districtcourts.parser import parse_case_status_html, parse_court_orders_html
from bharat_courts.hcservices import endpoints as hc_endpoints
from bharat_courts.hcservices.client import HCServicesClient
from bharat_courts.hcservices.parser import _parse_date
from bharat_courts.pdf import (
    OrderNotUploadedError,
    PdfUnavailableError,
    PortalUnavailableError,
    ensure_pdf,
)

BOM = b"\xef\xbb\xbf"
PDF = b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n5 0 obj\n<</Length 6 0 R>>\nendobj\n%%EOF\n"

# The four displayPdf arguments, exactly as the portal writes them: base64 with
# "+", "/" and "=" that must survive the round trip through the URL.
DC_ARGS = (
    "cl+sGJoSJ58oyVespa8VHg==",
    "XJwMD/wl/BfV3hZgrTeJ7BSdP+PGothM2IuTiqMH6Z0=",
    "1Wa2LjH5w7zcPVzbT+mRgw==",
    "Me6QLwh18hGm+CC2Gq7/LkwvKvSY21XRpmb2MLcRcAcf",
)
DC_ONCLICK = "displayPdf('{}','{}','{}','{}','')".format(*DC_ARGS)

DC_ORDERS_PAGE = f"""
<h3 class='h2class' id='orderheading'>Interim Orders </h3>
<table width="100%" class="order_table table " border="1"><thead><tr>
<th scope='col'>Order Number</th><th scope='col'>&nbsp;&nbsp;Order Date </th>
<th scope='col'>&nbsp;&nbsp;Order Details </th></tr></thead>
<tr><td>&nbsp;&nbsp;1</td><td style='border-top:none;'>&nbsp;&nbsp;14-07-2025</font></td>
<td colspan='3'><a href='#' aria-label='Interim Orders | Order on Exhibit on 14-07-2025 '
 onclick={DC_ONCLICK} ><font> &nbsp;&nbsp;Order on Exhibit </font><span></span></a></td></tr>
</table>
"""


class AutoCaptchaSolver(CaptchaSolver):
    async def solve(self, image_bytes: bytes) -> str:
        return "test12"


@pytest.fixture
def fast_config():
    return BharatCourtsConfig(request_delay=0, timeout=5, max_retries=1)


# ---------------------------------------------------------------- ensure_pdf


def test_bom_before_pdf_is_stripped():
    assert ensure_pdf(BOM + PDF) == PDF


def test_plain_pdf_is_unchanged():
    assert ensure_pdf(PDF) == PDF


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            BOM + b"<h2><table border='0' align='center'><td align='center'> Orders is not "
            b"uploaded for case number AR/288/2025</td></h2>",
            OrderNotUploadedError,
        ),
        (BOM + b"Unable to connect to server", PortalUnavailableError),
        (b"<html>something else</html>", PdfUnavailableError),
    ],
)
def test_non_pdf_answers_are_told_apart(body, error):
    with pytest.raises(error) as exc:
        ensure_pdf(body)
    # Callers that caught RuntimeError before still do.
    assert isinstance(exc.value, RuntimeError)


@pytest.mark.asyncio
async def test_hc_download_accepts_bom_prefixed_pdf(fast_config):
    url = f"{hc_endpoints.BASE_URL}/cases/display_pdf.php?filename=abc"
    with respx.mock:
        respx.get(url).mock(return_value=Response(200, content=BOM + PDF))
        async with HCServicesClient(config=fast_config, captcha_solver=AutoCaptchaSolver()) as c:
            assert await c.download_order_pdf(url) == PDF


# ---------------------------------------------------------- district links


def test_district_case_page_order_link_carries_display_pdf_fields():
    detail = parse_case_detail(
        DC_ORDERS_PAGE, cnr="MP04050004072025", base_url=dc_endpoints.BASE_URL
    )
    assert len(detail.orders) == 1
    order = detail.orders[0]
    assert order.order_date == date(2025, 7, 14)
    assert order.order_type == "Order on Exhibit"
    url = urlparse(order.pdf_url)
    assert order.pdf_url.startswith(dc_endpoints.BASE_URL + "/home/display_pdf?")
    fields = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
    assert fields == {
        "normal_v": DC_ARGS[0],
        "case_val": DC_ARGS[1],
        "court_code": DC_ARGS[2],
        "filename": DC_ARGS[3],
        "appFlag": "",
    }


def test_district_court_orders_table_uses_display_pdf_too():
    html = f"""<table><tr><th>Sr</th><th>Date</th><th>Type</th><th>Judge</th><th>Order</th></tr>
    <tr><td>1</td><td>14-07-2025</td><td>Interim Order</td><td>JMFC</td>
    <td><a href='#' onclick={DC_ONCLICK}>View</a></td></tr></table>"""
    orders = parse_court_orders_html(html, base_url=dc_endpoints.BASE_URL)
    assert orders[0].pdf_url.startswith(dc_endpoints.BASE_URL + "/home/display_pdf?")


def test_root_relative_district_link_is_not_doubled():
    html = """<table><tr><th>Sr</th><th>Date</th><th>Type</th><th>Judge</th><th>Order</th></tr>
    <tr><td>1</td><td>14-07-2025</td><td>Order</td><td>JMFC</td>
    <td><a href="/ecourtindia_v6/cases/display_pdf.php?filename=x">View</a></td></tr></table>"""
    orders = parse_court_orders_html(html, base_url=dc_endpoints.BASE_URL)
    assert orders[0].pdf_url == (
        "https://services.ecourts.gov.in/ecourtindia_v6/cases/display_pdf.php?filename=x"
    )


def _ajax(**kw):
    return Response(200, text=json.dumps({"status": 1, "app_token": "tok", **kw}))


@pytest.mark.asyncio
async def test_district_download_posts_the_fields_and_fetches_the_report(fast_config):
    detail = parse_case_detail(
        DC_ORDERS_PAGE, cnr="MP04050004072025", base_url=dc_endpoints.BASE_URL
    )
    seen = {}

    def display_pdf(request):
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return _ajax(order="reports/6ce9536b.pdf")

    with respx.mock:
        respx.post(url__regex=r".*\?p=home/display_pdf$").mock(side_effect=display_pdf)
        respx.get(f"{dc_endpoints.BASE_URL}/reports/6ce9536b.pdf").mock(
            return_value=Response(200, content=PDF)
        )
        async with DistrictCourtClient(config=fast_config, captcha_solver=AutoCaptchaSolver()) as c:
            assert await c.download_order_pdf(detail.orders[0].pdf_url) == PDF

    assert seen["normal_v"] == DC_ARGS[0]
    assert seen["case_val"] == DC_ARGS[1]
    assert seen["filename"] == DC_ARGS[3]


@pytest.mark.asyncio
async def test_district_download_explains_an_expired_session(fast_config):
    detail = parse_case_detail(DC_ORDERS_PAGE, base_url=dc_endpoints.BASE_URL)
    timeout = {"errormsg": "<br/> <strong>Oops!</strong> Session timeout..!!!<br/>"}
    with respx.mock:
        respx.post(url__regex=r".*\?p=home/display_pdf$").mock(
            return_value=Response(200, text=json.dumps(timeout))
        )
        async with DistrictCourtClient(config=fast_config, captcha_solver=AutoCaptchaSolver()) as c:
            with pytest.raises(PdfUnavailableError, match="look the case up again"):
                await c.download_order_pdf(detail.orders[0].pdf_url)


# ------------------------------------------------------------- district CNRs


@pytest.mark.parametrize("cnr", ["MP04050004072025", "BRPA010216322024", "WBCHCO0001032025"])
def test_district_party_rows_keep_cnrs_with_any_prefix(cnr):
    html = f"""<table><tr><th>Sr No</th><th>Case</th><th>Parties</th><th>View</th></tr>
    <tr><th colspan='3'>Civil Court</th></tr>
    <tr><td>1</td><td>RCT/985/2025</td><td>A Bank Through Manager<br>Vs</br>A Shop</td>
    <td><a class='someclass' href='#' onClick="viewHistory(220600009852025,'{cnr}',2,'',
    'CScaseNumber',23,50,1230217,'CSpartyName')">View</a></td></tr></table>"""
    rows = parse_case_status_html(html)
    assert [r.cnr_number for r in rows] == [cnr]


# ------------------------------------------------------ HC orders by number


@pytest.mark.asyncio
async def test_hc_court_orders_reads_the_case_page(fast_config, fixtures_dir):
    search = (fixtures_dir / "hcservices_case_status.json").read_text()
    detail_html = (fixtures_dir / "hcservices_case_detail.html").read_text()
    with respx.mock:
        respx.get(hc_endpoints.MAIN_PAGE_URL).mock(return_value=Response(200, text="<html/>"))
        respx.get(hc_endpoints.CAPTCHA_IMAGE_URL).mock(return_value=Response(200, content=b"img"))
        respx.post(hc_endpoints.SHOW_RECORDS_URL).mock(return_value=Response(200, text=search))
        cnr_page = respx.post(hc_endpoints.INDEX_QRY_URL).mock(
            return_value=Response(200, text=detail_html)
        )
        async with HCServicesClient(config=fast_config, captcha_solver=AutoCaptchaSolver()) as c:
            orders = await c.court_orders(
                get_court("delhi"), case_type="134", case_number="3", year="2024"
            )

    assert cnr_page.called
    assert b"DLHC010582482024" in cnr_page.calls.last.request.content
    # The case page's real order, not a placeholder dated today.
    assert [o.order_date for o in orders] == [date(2025, 7, 25)]
    assert orders[0].judge == "HONOURABLE MR. JUSTICE C D SAMPLE"


# ---------------------------------------------------- small parser fixes


def test_madras_counsel_on_an_indented_line_is_kept():
    html = """<span class='Petitioner_Advocate_table'>1) A PETITIONER<br />
    &nbsp;&nbsp;&nbsp;&nbsp;M/S.SAMPLE LAW ASSOCIATES <br />2)  SECOND PETITIONER<br></span>
    <span class='Respondent_Advocate_table'>1) THE BRANCH MANAGER<br> &nbsp;&nbsp; <br />
    2)  THE AUTHORISED OFFICER<br></span>"""
    detail = parse_case_detail(html)
    assert [(p.name, p.advocate) for p in detail.petitioners] == [
        ("A PETITIONER", "M/S.SAMPLE LAW ASSOCIATES"),
        ("SECOND PETITIONER", ""),
    ]
    assert [p.advocate for p in detail.respondents] == ["", ""]


def test_advocate_cause_list_placeholder_date_is_none():
    assert _parse_date("5000-01-01") is None
    assert _parse_date("2026-11-27") == date(2026, 11, 27)
