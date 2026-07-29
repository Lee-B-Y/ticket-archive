from __future__ import annotations

import email
import email.policy
import html
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import getaddresses
from html.parser import HTMLParser


FORWARD_TERMS = ("转发", "forward", "forwarding")
VERIFY_TERMS = ("验证", "确认", "接受", "verify", "verification", "confirm", "confirmation", "accept", "activate")
POSITIVE_TERMS = ("接受转发", "确认转发", "验证转发", "接受", "确认", "验证", "启用", "accept", "confirm", "verify", "activate")
NEGATIVE_TERMS = ("取消", "拒绝", "不接受", "cancel", "reject", "decline", "deny", "unsubscribe")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


@dataclass(frozen=True)
class VerificationDetection:
    recognized: bool
    action_url: str | None
    reason: str
    subject: str
    message_id: str
    sender_domain: str


@dataclass(frozen=True)
class ConfirmationResult:
    status: str
    http_status: int | None
    detail: str


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value for key, value in attrs}
        self._href = values.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((html.unescape(self._href.strip()), " ".join(self._text).strip()))
            self._href = None
            self._text = []


def _message_text(message: email.message.EmailMessage) -> tuple[str, list[tuple[str, str]]]:
    chunks: list[str] = []
    links: list[tuple[str, str]] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_maintype() != "text":
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        if not isinstance(content, str):
            continue
        chunks.append(content)
        if part.get_content_subtype().casefold() == "html":
            parser = _AnchorParser()
            parser.feed(content)
            links.extend(parser.links)
        for url in URL_PATTERN.findall(content):
            links.append((html.unescape(url.rstrip(".,);]}>")), ""))
    return "\n".join(chunks), links


def _sender_domain(message: email.message.EmailMessage) -> str:
    addresses = getaddresses(message.get_all("From", []))
    if not addresses or "@" not in addresses[0][1]:
        return ""
    return addresses[0][1].rsplit("@", 1)[1].casefold()


def _has_any(value: str, terms: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(term in folded for term in terms)


def _url_is_basic_https(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme.casefold() == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def detect_forward_verification(raw: bytes) -> VerificationDetection:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(message.get("Subject", "")).strip()
    message_id = str(message.get("Message-ID", "")).strip()
    sender_domain = _sender_domain(message)
    _, raw_links = _message_text(message)
    subject_is_explicit = _has_any(subject, FORWARD_TERMS) and _has_any(subject, VERIFY_TERMS)
    if not subject_is_explicit:
        return VerificationDetection(False, None, "not_forward_verification", subject, message_id, sender_domain)

    unique: dict[str, str] = {}
    for url, label in raw_links:
        if _url_is_basic_https(url):
            unique.setdefault(url, label)
    if not unique:
        return VerificationDetection(True, None, "no_safe_https_link", subject, message_id, sender_domain)

    positive: list[str] = []
    for url, label in unique.items():
        parsed = urllib.parse.urlsplit(url)
        decoded_url = urllib.parse.unquote_plus(f"{parsed.path}?{parsed.query}").casefold()
        evidence = f"{label}\n{decoded_url}".casefold()
        query = urllib.parse.parse_qs(parsed.query)
        handlers = {item.casefold() for item in query.get("handler", [])}
        if _has_any(evidence, NEGATIVE_TERMS) or "cancelfw_result" in handlers:
            continue
        if "verifyfw_result" in handlers or _has_any(evidence, POSITIVE_TERMS):
            positive.append(url)

    candidates = list(dict.fromkeys(positive))
    if len(candidates) == 1:
        return VerificationDetection(True, candidates[0], "action_selected", subject, message_id, sender_domain)
    reason = "no_positive_action" if not candidates else "ambiguous_positive_actions"
    return VerificationDetection(True, None, reason, subject, message_id, sender_domain)


def _validate_public_https_url(url: str) -> None:
    if not _url_is_basic_https(url):
        raise ValueError("verification URL must use HTTPS on the default port")
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ""
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("verification URL cannot use a non-public IP address")
    addresses = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("verification hostname did not resolve")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if address.is_global:
            continue
        # Mihomo fake-IP DNS uses this reserved range for public names. Literal
        # addresses remain forbidden, so allowing the resolved range does not
        # expose local services.
        if literal is None and isinstance(address, ipaddress.IPv4Address) and address in FAKE_IP_NETWORK:
            continue
        raise ValueError("verification hostname resolved to a non-public address")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def confirm_forward_verification(url: str, *, attempts: int = 3, timeout: int = 20) -> ConfirmationResult:
    _validate_public_https_url(url)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; TicketArchive/1.0; forwarding-verification)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(request, timeout=timeout) as response:
                status = int(response.status)
                response.read(65536)
                return ConfirmationResult(
                    "confirmed" if 200 <= status < 300 else "failed",
                    status,
                    "verification endpoint accepted the request" if 200 <= status < 300 else "verification endpoint returned an unexpected status",
                )
        except urllib.error.HTTPError as exc:
            return ConfirmationResult("failed", int(exc.code), "verification endpoint returned an HTTP error")
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            if attempt == attempts:
                return ConfirmationResult("failed", None, type(exc).__name__)
            time.sleep(attempt)
    return ConfirmationResult("failed", None, "verification retry loop ended unexpectedly")
