"""
Barclaycard balance fetcher.

Reads the latest Barclaycard statement/notification email from a Gmail inbox
(via IMAP) and parses the outstanding balance. Barclaycard has no public API,
so this is the only available source.

Expects Outlook → Gmail auto-forward rule to be in place. See README for setup.
"""

import email
import email.policy
import html.parser
import imaplib
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

BALANCE_REGEX = re.compile(
    r"(?:outstanding\s+balance|current\s+balance|balance(?:\s+owed|\s+due)?)"
    r"[^£\d]*£\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)


class _HTMLStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts = []

    def handle_data(self, data):
        self._parts.append(data)

    def get_text(self):
        return " ".join(self._parts)


def _strip_html(html_bytes: bytes, charset: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html_bytes.decode(charset, errors="replace"))
    return stripper.get_text()


def _connect() -> imaplib.IMAP4_SSL:
    host = os.environ.get("IMAP_HOST", "imap.gmail.com")
    port = int(os.environ.get("IMAP_PORT", "993"))
    user = os.environ["IMAP_USER"]
    password = os.environ["IMAP_APP_PASSWORD"]
    folder = os.environ.get("IMAP_FOLDER", "Credit Card")
    imap = imaplib.IMAP4_SSL(host, port)
    imap.login(user, password)
    imap.select(f'"{folder}"')
    return imap


def _find_latest_message(imap: imaplib.IMAP4_SSL) -> email.message.EmailMessage:
    sender = os.environ.get("BARCLAYCARD_SENDER", "")
    keyword = os.environ.get("BARCLAYCARD_SUBJECT_KEYWORD", "balance")
    if sender:
        _, data = imap.search(None, "FROM", f'"{sender}"', "SUBJECT", f'"{keyword}"')
    else:
        _, data = imap.search(None, "SUBJECT", f'"{keyword}"')
    uids = data[0].split()
    if not uids:
        raise LookupError(
            f"No Barclaycard email found (FROM={sender!r}, SUBJECT={keyword!r}). "
            "Check the forward rule is active and an email has arrived."
        )
    latest = uids[-1]
    _, msg_data = imap.fetch(latest, "(RFC822)")
    raw = msg_data[0][1]
    return email.message_from_bytes(raw, policy=email.policy.default)


def _extract_text(msg: email.message.EmailMessage) -> str:
    plain_part = None
    html_part = None
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain" and plain_part is None:
            plain_part = part
        elif ct == "text/html" and html_part is None:
            html_part = part
    if plain_part is not None:
        charset = plain_part.get_content_charset() or "utf-8"
        payload = plain_part.get_payload(decode=True)
        return payload.decode(charset, errors="replace")
    if html_part is not None:
        charset = html_part.get_content_charset() or "utf-8"
        payload = html_part.get_payload(decode=True)
        return _strip_html(payload, charset)
    return ""


def _parse_balance(body: str) -> float:
    if os.environ.get("BARCLAYCARD_DEBUG"):
        print("[barclaycard] email body:\n", body, flush=True)
    match = BALANCE_REGEX.search(body)
    if not match:
        print(
            "[barclaycard] regex did not match. Full email body:\n",
            body,
            flush=True,
        )
        raise ValueError(
            "Barclaycard balance regex did not match. "
            "Check stderr for the full email body and update BALANCE_REGEX in "
            "src/barclaycard.py to suit the actual phrasing."
        )
    amount_str = match.group(1).replace(",", "")
    return round(float(amount_str), 2)


def get_balance() -> float:
    """Return the latest Barclaycard outstanding balance in GBP."""
    imap = _connect()
    try:
        msg = _find_latest_message(imap)
        body = _extract_text(msg)
        return _parse_balance(body)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
