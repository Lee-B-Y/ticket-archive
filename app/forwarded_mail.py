from __future__ import annotations

import email
import email.header
import email.policy
import hashlib
from dataclasses import dataclass, field
from email.message import Message
from email.utils import getaddresses
from typing import Mapping, Sequence


OFFICIAL_SENDERS = frozenset({"12306@rails.com.cn"})


@dataclass(frozen=True)
class RoutedMail:
    owner_username: str
    canonical_raw: bytes
    message_id: str
    subject: str
    mode: str


@dataclass(frozen=True)
class RejectedMail:
    reason: str
    canonical_sha256: str
    message_id: str
    subject: str
    mode: str


@dataclass
class RouteBatch:
    routed: list[RoutedMail] = field(default_factory=list)
    rejected: list[RejectedMail] = field(default_factory=list)


def decode_header(value: str | None) -> str:
    if not value:
        return ""
    decoded: list[str] = []
    for part, charset in email.header.decode_header(value):
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def header_addresses(message: Message, *names: str) -> tuple[str, ...]:
    values: list[str] = []
    for name in names:
        values.extend(decode_header(value) for value in message.get_all(name, []))
    return tuple(
        dict.fromkeys(
            address.strip().casefold()
            for _, address in getaddresses(values)
            if address and "@" in address
        )
    )


def _message_bytes(message: Message) -> bytes:
    return message.as_bytes(policy=email.policy.default)


def attached_messages(message: Message) -> list[bytes]:
    attached: list[bytes] = []
    seen: set[str] = set()
    for part in message.walk():
        filename = decode_header(part.get_filename()).casefold()
        candidates: list[bytes] = []
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                candidates.extend(
                    _message_bytes(item) for item in payload if isinstance(item, Message)
                )
        elif filename.endswith(".eml"):
            raw = part.get_payload(decode=True) or b""
            if raw:
                candidates.append(raw)
        for raw in candidates:
            digest = hashlib.sha256(raw).hexdigest()
            if digest not in seen:
                attached.append(raw)
                seen.add(digest)
    return attached


def _normalise_owner_lookup(
    owner_lookup: Mapping[str, str | Sequence[str]],
) -> dict[str, set[str]]:
    normalised: dict[str, set[str]] = {}
    for address, owners in owner_lookup.items():
        if isinstance(owners, str):
            values = {owners}
        else:
            values = {str(owner) for owner in owners}
        normalised.setdefault(address.strip().casefold(), set()).update(values)
    return normalised


def _owners_for(addresses: Sequence[str], owner_lookup: Mapping[str, set[str]]) -> set[str]:
    owners: set[str] = set()
    for address in addresses:
        owners.update(owner_lookup.get(address.casefold(), ()))
    return owners


def _reject(raw: bytes, message: Message, mode: str, reason: str) -> RejectedMail:
    return RejectedMail(
        reason=reason,
        canonical_sha256=hashlib.sha256(raw).hexdigest(),
        message_id=decode_header(message.get("Message-ID")).strip(),
        subject=decode_header(message.get("Subject")).strip(),
        mode=mode,
    )


def route_forwarded_message(
    raw: bytes,
    owner_lookup: Mapping[str, str | Sequence[str]],
) -> RouteBatch:
    """Split and attribute one message received by the shared mailbox.

    Automatic forwarding preserves the original message as the outer message.
    Manual bulk forwarding is represented by one or more attached source messages.
    """

    result = RouteBatch()
    lookup = _normalise_owner_lookup(owner_lookup)
    outer = email.message_from_bytes(raw, policy=email.policy.default)
    embedded = attached_messages(outer)
    mode = "manual" if embedded else "automatic"
    source_messages = embedded or [raw]

    outer_owners: set[str] | None = None
    if mode == "manual":
        outer_owners = _owners_for(header_addresses(outer, "From"), lookup)

    for canonical_raw in source_messages:
        source = email.message_from_bytes(canonical_raw, policy=email.policy.default)
        senders = set(header_addresses(source, "From", "Sender"))
        if not senders or not senders.issubset(OFFICIAL_SENDERS):
            result.rejected.append(
                _reject(canonical_raw, source, mode, "unofficial_sender")
            )
            continue

        recipient_owners = _owners_for(header_addresses(source, "To"), lookup)
        if not recipient_owners:
            result.rejected.append(
                _reject(canonical_raw, source, mode, "unknown_recipient")
            )
            continue
        if len(recipient_owners) != 1:
            result.rejected.append(
                _reject(canonical_raw, source, mode, "ambiguous_recipient")
            )
            continue

        owner = next(iter(recipient_owners))
        if mode == "manual":
            if not outer_owners:
                result.rejected.append(
                    _reject(canonical_raw, source, mode, "unknown_outer_sender")
                )
                continue
            if len(outer_owners) != 1 or owner not in outer_owners:
                result.rejected.append(
                    _reject(canonical_raw, source, mode, "owner_conflict")
                )
                continue

        result.routed.append(
            RoutedMail(
                owner_username=owner,
                canonical_raw=canonical_raw,
                message_id=decode_header(source.get("Message-ID")).strip(),
                subject=decode_header(source.get("Subject")).strip(),
                mode=mode,
            )
        )
    return result
