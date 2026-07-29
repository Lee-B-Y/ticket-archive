from __future__ import annotations

import email
import email.policy
import unittest
from email.message import EmailMessage

from app.forwarded_mail import route_forwarded_message


OFFICIAL_SENDER = "12306@rails.com.cn"
SYSTEM_MAILBOX = "ticket-helper@example.test"
ALICE_LOGIN = "alice-login@example.test"
ALICE_TICKET_MAIL = "alice-rail@example.test"
BOB_LOGIN = "bob-login@example.test"
BOB_TICKET_MAIL = "bob-rail@example.test"


def make_ticket_mail(
    recipient: str,
    message_id: str,
    *,
    sender: str = OFFICIAL_SENDER,
) -> tuple[EmailMessage, bytes]:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "Rail ticket notice"
    message["Message-ID"] = message_id
    message.set_content("Synthetic ticket mail used only by unit tests.")
    return message, message.as_bytes(policy=email.policy.SMTP)


def make_forwarder(sender: str, message_id: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = SYSTEM_MAILBOX
    message["Subject"] = "Forwarded rail ticket messages"
    message["Message-ID"] = message_id
    message.set_content("Synthetic forwarding wrapper.")
    return message


def field(value, name: str):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


class ForwardedMailRoutingTests(unittest.TestCase):
    def setUp(self):
        self.owners = {
            ALICE_LOGIN: "alice",
            ALICE_TICKET_MAIL: "alice",
            BOB_LOGIN: "bob",
            BOB_TICKET_MAIL: "bob",
        }

    def route(self, raw: bytes, owner_lookup=None):
        result = route_forwarded_message(raw, owner_lookup or self.owners)
        return list(field(result, "routed")), list(field(result, "rejected"))

    def assert_reason(self, rejected, expected: str):
        self.assertEqual([field(item, "reason") for item in rejected], [expected])

    def test_automatic_forward_from_official_sender_routes_by_original_to(self):
        _, raw = make_ticket_mail(ALICE_TICKET_MAIL, "<auto-1@example.test>")
        routed, rejected = self.route(raw)

        self.assertEqual(rejected, [])
        self.assertEqual(len(routed), 1)
        self.assertEqual(field(routed[0], "owner_username"), "alice")
        self.assertEqual(field(routed[0], "message_id"), "<auto-1@example.test>")
        self.assertEqual(field(routed[0], "canonical_raw"), raw)

    def test_octet_stream_eml_attachments_are_routed_individually(self):
        outer = make_forwarder(ALICE_LOGIN, "<wrapper-octets@example.test>")
        _, first = make_ticket_mail(ALICE_TICKET_MAIL, "<attached-1@example.test>")
        _, second = make_ticket_mail(ALICE_TICKET_MAIL, "<attached-2@example.test>")
        outer.add_attachment(
            first,
            maintype="application",
            subtype="octet-stream",
            filename="notice-1.eml",
        )
        outer.add_attachment(
            second,
            maintype="application",
            subtype="octet-stream",
            filename="notice-2.eml",
        )

        routed, rejected = self.route(outer.as_bytes(policy=email.policy.SMTP))

        self.assertEqual(rejected, [])
        self.assertEqual([field(item, "message_id") for item in routed], [
            "<attached-1@example.test>",
            "<attached-2@example.test>",
        ])
        self.assertEqual({field(item, "owner_username") for item in routed}, {"alice"})
        parsed = [
            email.message_from_bytes(field(item, "canonical_raw"), policy=email.policy.default)
            for item in routed
        ]
        self.assertEqual([item["To"] for item in parsed], [ALICE_TICKET_MAIL, ALICE_TICKET_MAIL])

    def test_message_rfc822_attachment_is_supported(self):
        outer = make_forwarder(ALICE_LOGIN, "<wrapper-rfc822@example.test>")
        nested, _ = make_ticket_mail(ALICE_TICKET_MAIL, "<attached-rfc822@example.test>")
        outer.add_attachment(nested)

        routed, rejected = self.route(outer.as_bytes(policy=email.policy.SMTP))

        self.assertEqual(rejected, [])
        self.assertEqual(len(routed), 1)
        self.assertEqual(field(routed[0], "owner_username"), "alice")
        self.assertEqual(field(routed[0], "message_id"), "<attached-rfc822@example.test>")

    def test_manual_forward_requires_outer_sender_and_inner_to_same_owner(self):
        outer = make_forwarder(ALICE_LOGIN, "<wrapper-conflict@example.test>")
        _, nested = make_ticket_mail(BOB_TICKET_MAIL, "<conflict@example.test>")
        outer.add_attachment(
            nested,
            maintype="application",
            subtype="octet-stream",
            filename="conflicting-owner.eml",
        )

        routed, rejected = self.route(outer.as_bytes(policy=email.policy.SMTP))

        self.assertEqual(routed, [])
        self.assert_reason(rejected, "owner_conflict")

    def test_unknown_original_recipient_is_rejected(self):
        _, raw = make_ticket_mail("unknown@example.test", "<unknown-recipient@example.test>")

        routed, rejected = self.route(raw)

        self.assertEqual(routed, [])
        self.assert_reason(rejected, "unknown_recipient")

    def test_recipient_bound_to_multiple_owners_is_rejected(self):
        _, raw = make_ticket_mail(ALICE_TICKET_MAIL, "<ambiguous@example.test>")
        owners = dict(self.owners)
        owners[ALICE_TICKET_MAIL] = {"alice", "bob"}

        routed, rejected = self.route(raw, owners)

        self.assertEqual(routed, [])
        self.assert_reason(rejected, "ambiguous_recipient")

    def test_manual_forward_from_unbound_outer_sender_is_rejected(self):
        outer = make_forwarder("unbound@example.test", "<wrapper-unbound@example.test>")
        _, nested = make_ticket_mail(ALICE_TICKET_MAIL, "<nested-unbound@example.test>")
        outer.add_attachment(
            nested,
            maintype="application",
            subtype="octet-stream",
            filename="unbound.eml",
        )

        routed, rejected = self.route(outer.as_bytes(policy=email.policy.SMTP))

        self.assertEqual(routed, [])
        self.assert_reason(rejected, "unknown_outer_sender")

    def test_non_official_direct_sender_is_rejected(self):
        _, raw = make_ticket_mail(
            ALICE_TICKET_MAIL,
            "<untrusted@example.test>",
            sender="not-the-railway@example.test",
        )

        routed, rejected = self.route(raw)

        self.assertEqual(routed, [])
        self.assert_reason(rejected, "unofficial_sender")

    def test_nested_message_id_is_preserved_in_canonical_raw(self):
        outer = make_forwarder(ALICE_LOGIN, "<wrapper-preserve@example.test>")
        _, nested = make_ticket_mail(ALICE_TICKET_MAIL, "<preserved-id@example.test>")
        outer.add_attachment(
            nested,
            maintype="application",
            subtype="octet-stream",
            filename="preserve.eml",
        )

        routed, rejected = self.route(outer.as_bytes(policy=email.policy.SMTP))

        self.assertEqual(rejected, [])
        self.assertEqual(field(routed[0], "message_id"), "<preserved-id@example.test>")
        canonical = email.message_from_bytes(
            field(routed[0], "canonical_raw"), policy=email.policy.default
        )
        self.assertEqual(canonical["Message-ID"], "<preserved-id@example.test>")


if __name__ == "__main__":
    unittest.main()
