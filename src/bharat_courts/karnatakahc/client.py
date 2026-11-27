"""High Court of Karnataka website client (judiciary.karnataka.gov.in).

The High Court runs its own case-status pages beside eCourts HC Services, with
more on them: the daily-order register (each sitting's order text, typed in),
interlocutory applications, linked cases, the order under appeal, documents.
This client reads them the way the page's own scripts do.

Flow (see ``endpoints`` for the wire format):

1. ``GET casemenu.php``: PHP session cookie.
2. ``GET captcha.php``: a six-digit CAPTCHA bound to that session.
3. ``POST casestatus.php`` (``benchval=B*WA*1035*2026*<captcha>``): the case page,
   ``2`` for a wrong CAPTCHA, ``RECORD_NOT_FOUND`` for no such case.
4. ``POST case_details_div.php`` per section with the page's encrypted handle.
5. ``POST napix/highcourtorderdetails.php`` with an order's token: base64 PDF in JSON.

Usage::

    async with KarnatakaHCClient() as khc:
        case = await khc.case_status(bench="Bengaluru", case_type="WA", number="1035", year="2026")
        if case:
            print(case.case_number, case.cnr_number, case.next_hearing_date)
            for order in case.orders:
                if order.pdf_url:
                    pdf = await khc.download_order_pdf(order.pdf_url)
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import parse_qs, quote, urlparse

from bharat_courts.captcha import default_solver
from bharat_courts.captcha.base import CaptchaSolver
from bharat_courts.config import BharatCourtsConfig
from bharat_courts.config import config as default_config
from bharat_courts.http import RateLimitedClient
from bharat_courts.karnatakahc import endpoints
from bharat_courts.karnatakahc import parser as p
from bharat_courts.models import CaseDetail, CaseOrder, HearingEntry, PartyEntry
from bharat_courts.pdf import PdfUnavailableError, ensure_pdf

logger = logging.getLogger(__name__)

COURT_NAME = "High Court of Karnataka"

# What OCR confuses with digits on this CAPTCHA (it is digits only).
_DIGIT_LOOKALIKES = str.maketrans(
    {
        "o": "0",
        "O": "0",
        "D": "0",
        "Q": "0",
        "l": "1",
        "I": "1",
        "i": "1",
        "|": "1",
        "z": "2",
        "Z": "2",
        "s": "5",
        "S": "5",
        "b": "6",
        "G": "6",
        "T": "7",
        "B": "8",
        "g": "9",
        "q": "9",
    }
)


class KarnatakaCaptchaError(RuntimeError):
    """The site kept refusing the CAPTCHA answers."""


class KarnatakaSearchError(ValueError):
    """The site refused the search itself (date range over a year, name too short)."""


def digits_only(answer: str) -> str:
    """An OCR (or typed) CAPTCHA answer reduced to the six digits the site expects."""
    return re.sub(r"\D", "", (answer or "").strip().translate(_DIGIT_LOOKALIKES))


@dataclass
class KarnatakaCase(CaseDetail):
    """A case as the High Court's own site shows it: :class:`CaseDetail` plus what only it has."""

    bench: str = ""  # "Bengaluru" | "Dharwad" | "Kalaburagi"
    case_number: str = ""  # "WA 1035/2026"
    number: str = ""
    year: str = ""
    classification: str = ""
    last_posted_for: str = ""
    last_action: str = ""
    last_action_date: date | None = None
    petitioner_advocate: str = ""
    respondent_advocate: str = ""
    linked_cases: list[str] = field(default_factory=list)
    interlocutory_applications: list[dict] = field(default_factory=list)
    lower_court: list[dict] = field(default_factory=list)
    source: str = "karnatakahc"
    source_url: str = endpoints.CASE_MENU_URL


def order_pdf_url(token: str) -> str:
    """The URL a :class:`CaseOrder` carries for an order: the site's endpoint plus the token."""
    return f"{endpoints.ORDER_PDF_URL}?data={quote(token, safe='')}"


def _token_of(pdf_url_or_token: str) -> str:
    if pdf_url_or_token.startswith("http"):
        q = parse_qs(urlparse(pdf_url_or_token).query)
        return (q.get("data") or [""])[0]
    return pdf_url_or_token


class KarnatakaHCClient:
    """Async client for the High Court of Karnataka's case-status pages."""

    def __init__(
        self,
        config: BharatCourtsConfig | None = None,
        captcha_solver: CaptchaSolver | None = None,
        http_client: RateLimitedClient | None = None,
    ):
        self._config = config or default_config
        self._captcha_solver = captcha_solver or default_solver()
        if http_client:
            self._http = http_client
            self._owns_http = False
        else:
            self._http = RateLimitedClient(self._config)
            self._owns_http = True
        self._session_ready = False
        self._last_handle = ""  # the case page's handle, for the PDF fallback endpoint

    async def __aenter__(self):
        await self._http.__aenter__()
        return self

    async def __aexit__(self, *args):
        if self._owns_http:
            await self._http.__aexit__(*args)

    # ------------------------------------------------------------------ session + CAPTCHA

    async def _init_session(self) -> None:
        if not self._session_ready:
            await self._http.get(endpoints.CASE_MENU_URL)
            self._session_ready = True

    async def _post(self, url: str, data: dict) -> str:
        resp = await self._http.post(url, data=data, headers={"Referer": endpoints.CASE_MENU_URL})
        return resp.text.lstrip("﻿")

    async def _post_with_captcha(self, url: str, form_builder, *, max_attempts: int = 6) -> str:
        """POST ``form_builder(captcha)`` until the site accepts the CAPTCHA.

        An OCR answer that is not six digits after cleaning is not sent (a new
        CAPTCHA is fetched instead), so a bad read costs one image, not a refusal.
        """
        await self._init_session()
        for attempt in range(1, max_attempts + 1):
            img = await self._http.get(
                endpoints.CAPTCHA_URL, headers={"Referer": endpoints.CASE_MENU_URL}
            )
            answer = digits_only(await self._captcha_solver.solve(img.content))
            if len(answer) != 6:
                logger.info(
                    "CAPTCHA read %d/%d not six digits (%r); new image",
                    attempt,
                    max_attempts,
                    answer,
                )
                continue
            body = await self._post(url, form_builder(answer))
            if body.strip() == endpoints.CAPTCHA_REJECTED:
                logger.info("CAPTCHA %d/%d refused", attempt, max_attempts)
                continue
            return body
        raise KarnatakaCaptchaError(
            f"The High Court website refused the CAPTCHA {max_attempts} times"
        )

    # ------------------------------------------------------------------ lookups

    async def case_types(self) -> list[tuple[str, str]]:
        """The site's High Court case types: [("WA", "WA - Writ Appeal"), ...]."""
        await self._init_session()
        return p.parse_case_types(await self._post(endpoints.CASE_TYPES_URL, {"type": "H"}))

    async def resolve_case_type(self, typed: str) -> str:
        """'W.A.' / 'wa' / 'Crl.P.' -> the site's own code ('WA', 'CRL.P'). Raises ValueError."""
        want = p.canonical_type(typed)
        for code, _label in await self.case_types():
            if p.canonical_type(code) == want:
                return code
        raise ValueError(f"The High Court of Karnataka has no case type {typed!r}")

    async def case_status(
        self,
        *,
        bench: str,
        case_type: str,
        number: str | int,
        year: str | int,
        sections: tuple[str, ...] = (
            "parties",
            "daily_orders",
            "judgments",
            "linked_cases",
            "ias",
            "lower_court",
        ),
    ) -> KarnatakaCase | None:
        """The case's page and its sections; None if the site has no such case.

        ``case_type`` is the site's code ("WA", "CRL.P"); pass anything else through
        :meth:`resolve_case_type` first. ``sections`` picks which parts of the case page
        to load (each is one more request).
        """
        letter = endpoints.bench_letter(bench)
        body = await self._post_with_captcha(
            endpoints.CASE_STATUS_URL,
            lambda c: endpoints.case_status_form(
                bench=letter, case_type=case_type, number=number, year=year, captcha=c
            ),
        )
        return await self._case_from_page(body, letter, sections)

    async def open_search_result(
        self,
        open_key: str,
        *,
        sections: tuple[str, ...] = (
            "parties",
            "daily_orders",
            "judgments",
            "linked_cases",
            "ias",
            "lower_court",
        ),
    ) -> KarnatakaCase | None:
        """Open a case from an advocate/party search result (``open_key``) in the same session.

        The site accepts the search's CAPTCHA answer again here, as its own page does.
        """
        body = await self._post(endpoints.CASE_STATUS_URL, {"benchval": open_key})
        if body.strip() == endpoints.CAPTCHA_REJECTED:
            bench, ctype, number, year = open_key.split("*")[:4]
            return await self.case_status(
                bench=bench, case_type=ctype, number=number, year=year, sections=sections
            )
        return await self._case_from_page(body, open_key.split("*")[0], sections)

    async def _case_from_page(
        self, body: str, letter: str, sections: tuple[str, ...]
    ) -> KarnatakaCase | None:
        page = p.parse_case_page(body)
        if page is None:
            if p.is_not_found(body):
                return None
            raise PdfUnavailableError(
                f"The High Court website sent an unexpected answer: {body[:200]!r}"
            )
        self._last_handle = page["case_handle"]
        case = KarnatakaCase(
            cnr_number=page["cnr"],
            case_type=page["case_type"],
            case_number=page["case_number"],
            number=page["number"],
            year=page["year"],
            registration_number=f"{page['number']}/{page['year']}" if page["number"] else "",
            filing_number=page["filing_number"],
            filing_date=p.parse_date(page["filing_date"]),
            next_hearing_date=p.parse_date(page["next_hearing_date"]),
            case_stage=page["last_posted_for"],
            status=page["status"],
            coram=page["judge"],
            court_name=COURT_NAME,
            state="Karnataka",
            bench=endpoints.BENCH_NAMES[letter],
            classification=page["classification"],
            last_posted_for=page["last_posted_for"],
            last_action=page["last_action"],
            last_action_date=p.parse_date(page["last_action_date"]),
            petitioner_advocate=page["petitioner_advocate"],
            respondent_advocate=page["respondent_advocate"],
        )
        if page["status"].lower().startswith("disposed"):
            case.decision_date = case.last_action_date
        lda = page["last_action_date"]

        async def section(name: str) -> str:
            div = endpoints.SECTIONS[name]
            handle = page["filing_handle"] if name == "scrutiny" else page["case_handle"]
            try:
                return await self._post(
                    endpoints.SECTION_URL,
                    endpoints.section_form(handle=handle, last_action_date=lda, div=div),
                )
            except Exception as e:  # one section failing must not lose the case page
                logger.warning("Karnataka HC section %s failed: %s", name, e)
                return ""

        if "parties" in sections:
            case.petitioners, case.respondents = p.parse_parties(await section("parties"))
        if not case.petitioners and page["petitioner"]:
            case.petitioners = [PartyEntry(page["petitioner"], page["petitioner_advocate"])]
            case.respondents = [PartyEntry(page["respondent"], page["respondent_advocate"])]
        if "daily_orders" in sections:
            for row in p.parse_daily_orders(await section("daily_orders")):
                case.history.append(
                    HearingEntry(
                        hearing_date=row["date"],
                        business_date=row["date"],
                        purpose=row["action"],
                        judge=row["judge"],
                    )
                )
                if row["date"] and (row["token"] or row["text"]):
                    case.orders.append(
                        CaseOrder(
                            order_date=row["date"],
                            order_type="Daily Order",
                            judge=row["judge"],
                            pdf_url=order_pdf_url(row["token"]) if row["token"] else "",
                            order_text=row["text"],
                        )
                    )
        if "judgments" in sections:
            for row in p.parse_judgments(await section("judgments")):
                case.orders.append(
                    CaseOrder(
                        order_date=row["date"] or case.last_action_date or date.today(),
                        order_type="Judgment",
                        judge=row["judge"],
                        pdf_url=order_pdf_url(row["token"]),
                        order_text=row["text"],
                    )
                )
        if "linked_cases" in sections:
            case.linked_cases = p.parse_linked_cases(await section("linked_cases"))
        if "ias" in sections:
            case.interlocutory_applications = p.parse_ias(await section("ias"))
        if "lower_court" in sections:
            case.lower_court = p.parse_lower_court(await section("lower_court"))
        first = min((h.hearing_date for h in case.history if h.hearing_date), default=None)
        case.first_hearing_date = first
        return case

    async def case_status_by_cnr_hint(
        self, cnr: str, *, case_type: str, number: str | int, year: str | int
    ) -> KarnatakaCase | None:
        """Case status where the bench comes from a Karnataka CNR (KAHC01/02/03...)."""
        letter = endpoints.bench_from_cnr(cnr)
        if not letter:
            raise ValueError(f"{cnr!r} is not a High Court of Karnataka CNR")
        return await self.case_status(bench=letter, case_type=case_type, number=number, year=year)

    async def _name_search(
        self,
        url: str,
        *,
        bench: str,
        name: str,
        side: str,
        case_type: str,
        filed_from: date | None,
        filed_to: date | None,
    ) -> list[dict]:
        if len(name.strip()) < 4:
            raise KarnatakaSearchError(
                "The High Court website needs at least four letters of the name"
            )
        filed_to = filed_to or date.today()
        filed_from = filed_from or (filed_to - timedelta(days=364))
        if (filed_to - filed_from).days > 365:
            raise KarnatakaSearchError(
                "The High Court website searches at most a year of filings at a time"
            )
        letter = endpoints.bench_letter(bench)
        body = await self._post_with_captcha(
            url,
            lambda c: endpoints.name_search_form(
                bench=letter,
                case_type=case_type or "0",
                name=name.strip(),
                side=side,
                captcha=c,
                from_date=filed_from.strftime("%d-%m-%Y"),
                to_date=filed_to.strftime("%d-%m-%Y"),
            ),
        )
        if body.strip() == endpoints.RANGE_TOO_LONG:
            raise KarnatakaSearchError(
                "The High Court website searches at most a year of filings at a time"
            )
        if body.strip() == endpoints.NAME_TOO_SHORT:
            raise KarnatakaSearchError(
                "The High Court website needs at least four letters of the name"
            )
        return p.parse_search_results(body, endpoints.BENCH_NAMES[letter])

    async def search_by_advocate(
        self,
        *,
        bench: str,
        name: str,
        side: str = "either",
        case_type: str = "0",
        filed_from: date | None = None,
        filed_to: date | None = None,
    ) -> list[dict]:
        """Cases filed in a date range (default: the last year) by an advocate's name.

        ``side``: "petitioner", "respondent" or "either".
        """
        return await self._name_search(
            endpoints.ADVOCATE_URL,
            bench=bench,
            name=name,
            side=side,
            case_type=case_type,
            filed_from=filed_from,
            filed_to=filed_to,
        )

    async def search_by_party(
        self,
        *,
        bench: str,
        name: str,
        side: str = "either",
        case_type: str = "0",
        filed_from: date | None = None,
        filed_to: date | None = None,
    ) -> list[dict]:
        """Cases filed in a date range (default: the last year) by a party's name."""
        return await self._name_search(
            endpoints.PARTY_URL,
            bench=bench,
            name=name,
            side=side,
            case_type=case_type,
            filed_from=filed_from,
            filed_to=filed_to,
        )

    # ------------------------------------------------------------------ PDFs

    async def download_order_pdf(self, pdf_url: str) -> bytes:
        """An order or judgment PDF from :attr:`CaseOrder.pdf_url` (or its bare token).

        Raises the :mod:`bharat_courts.pdf` errors when the site has no PDF to give.
        """
        token = _token_of(pdf_url)
        if not token:
            raise PdfUnavailableError("No order token in this link")
        await self._init_session()
        body = await self._post(endpoints.ORDER_PDF_URL, {"data": token})
        data = _pdf_from_json(body)
        if data is None and self._last_handle:
            body = await self._post(
                endpoints.ORDER_PDF_FALLBACK_URL, {"rdata": token, "casenum": self._last_handle}
            )
            data = _pdf_from_json(body)
        if data is None:
            return ensure_pdf(body.encode("utf-8", "replace"))
        return ensure_pdf(data)


def _pdf_from_json(body: str) -> bytes | None:
    """``{"status": 2, "data": "<base64>"}`` or ``{"cnt": 1, ...}`` -> bytes; else None."""
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not obj.get("data"):
        return None
    if str(obj.get("status", "")) != "2" and str(obj.get("cnt", "")) != "1":
        return None
    try:
        return base64.b64decode(obj["data"])
    except (binascii.Error, ValueError):
        return None
