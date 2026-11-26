"""Validation shared by every portal PDF download.

The eCourts portals are written in PHP files saved with a UTF-8 byte-order
mark, and some of them emit that mark (``EF BB BF``) before the PDF itself:
High Court order and cause-list PDFs arrive as ``EF BB BF %PDF-1.4 ...``.
The PDF's own byte offsets do not count the mark, so stripping it gives back
exactly the file the court uploaded. Checking ``content[:4] == b"%PDF"`` on the
raw bytes rejected every such file.

When the portal has no PDF to give it answers with a short text or HTML
message instead, also behind the byte-order mark. Two of those are worth
telling apart from "something unexpected came back":

- "Orders is not uploaded for case number ..." : the order exists on the case
  page but the court has not uploaded its PDF (:class:`OrderNotUploadedError`).
- "Unable to connect to server" : the portal's own back end is down, usually
  at night (:class:`PortalUnavailableError`). Try again later.

All three errors subclass ``RuntimeError``, which is what these downloads
raised before, so existing ``except RuntimeError`` callers keep working.
"""

from __future__ import annotations

import re

_PDF_MAGIC = b"%PDF"
_PREAMBLE = b"\xef\xbb\xbf \t\r\n"

_NOT_UPLOADED_RE = re.compile(rb"not\s+(?:been\s+)?uploaded", re.I)
_UNAVAILABLE_RE = re.compile(
    rb"unable\s+to\s+connect|connection\s+to\s+server\s+failed|try\s+after\s+some\s+time", re.I
)


class PdfUnavailableError(RuntimeError):
    """The portal answered a PDF request with something that is not a PDF."""


class OrderNotUploadedError(PdfUnavailableError):
    """The court lists the order but has not uploaded its PDF."""


class PortalUnavailableError(PdfUnavailableError):
    """The portal's back end could not serve the file right now."""


def ensure_pdf(content: bytes) -> bytes:
    """Return the PDF bytes, without any leading byte-order mark or whitespace.

    Raises:
        OrderNotUploadedError: the portal says the order is not uploaded.
        PortalUnavailableError: the portal says its server is unreachable.
        PdfUnavailableError: anything else that is not a PDF.
    """
    body = content.lstrip(_PREAMBLE)
    if body[:4] == _PDF_MAGIC:
        return body
    head = body[:400]
    detail = f"(got {len(content)} bytes; head={content[:64]!r})"
    if _NOT_UPLOADED_RE.search(head):
        raise OrderNotUploadedError(f"The court has not uploaded this order's PDF {detail}")
    if _UNAVAILABLE_RE.search(head):
        raise PortalUnavailableError(f"The court's server is not responding; try later {detail}")
    raise PdfUnavailableError(f"PDF download did not return a valid PDF {detail}")
