from __future__ import annotations

import email.policy
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.storage import build_workbook, counts, import_message, initialize, query_tickets


def purchase_message() -> bytes:
    message = EmailMessage()
    message["From"] = "12306 <12306@rails.com.cn>"
    message["To"] = "owner@example.com"
    message["Subject"] = "用户支付通知"
    message["Message-ID"] = "<purchase@example.test>"
    message["Date"] = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    message.set_content("HTML version")
    message.add_alternative(
        """
        <p>您于2026年07月01日成功购买了1张车票，票款共计45.00元，
        订单号码 E123ABC</p>
        <p>1. 张三，2026年08月01日08:30开，北京站-天津站，
        C1001次列车，05车01A号，二等座，成人票，票价45.00元，检票口3</p>
        """,
        subtype="html",
    )
    return message.as_bytes(policy=email.policy.SMTP)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "tickets.sqlite3"
        self.raw = self.root / "purchase.eml"
        self.raw.write_bytes(purchase_message())
        initialize(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_purchase_is_parsed_and_idempotent(self):
        self.assertTrue(import_message(self.database, self.raw))
        self.assertFalse(import_message(self.database, self.raw))
        self.assertEqual(counts(self.database), {"emails": 1, "tickets": 1, "ticket_events": 1})
        ticket = query_tickets(self.database, {"from_station": "北京"})[0]
        self.assertEqual(ticket["passenger"], "张三")
        self.assertEqual(ticket["train_no"], "C1001")
        self.assertEqual(ticket["seat_class"], "二等座")
        self.assertEqual(ticket["fare"], 45.0)

    def test_excel_export_is_a_valid_zip_container(self):
        import_message(self.database, self.raw)
        workbook = build_workbook(query_tickets(self.database, {}), {})
        self.assertTrue(workbook.startswith(b"PK"))
        self.assertGreater(len(workbook), 4000)


if __name__ == "__main__":
    unittest.main()
