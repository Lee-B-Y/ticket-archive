from __future__ import annotations

import email
import email.policy
import hashlib
import html
import io
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import xlsxwriter


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS emails(
  id INTEGER PRIMARY KEY,message_id TEXT,subject TEXT,received_at TEXT,
  sha256 TEXT NOT NULL,raw_path TEXT NOT NULL,parse_status TEXT NOT NULL,
  UNIQUE(message_id),UNIQUE(sha256)
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY,order_no TEXT UNIQUE,event_at TEXT,total REAL
);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY,order_id INTEGER,passenger TEXT,departure_at TEXT,
  from_station TEXT,to_station TEXT,train_no TEXT,seat_raw TEXT,seat_class TEXT,
  ticket_type TEXT,fare REAL,gate TEXT,status TEXT,refund_fee REAL,
  refund_amount REAL,FOREIGN KEY(order_id) REFERENCES orders(id)
);
CREATE TABLE IF NOT EXISTS ticket_events(
  id INTEGER PRIMARY KEY,email_id INTEGER,order_no TEXT,event_type TEXT,
  event_at TEXT,passenger TEXT,payload TEXT,
  UNIQUE(email_id,event_type,passenger)
);
CREATE TABLE IF NOT EXISTS forward_quarantine(
  id INTEGER PRIMARY KEY,uidvalidity INTEGER NOT NULL,imap_uid INTEGER NOT NULL,
  digest TEXT NOT NULL,reason TEXT NOT NULL,subject TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(uidvalidity,imap_uid,digest,reason)
);
CREATE TABLE IF NOT EXISTS forward_verification_events(
  id INTEGER PRIMARY KEY,uidvalidity INTEGER NOT NULL,imap_uid INTEGER NOT NULL,
  subject TEXT,sender_domain TEXT,action_host TEXT,action_sha256 TEXT,status TEXT NOT NULL,
  http_status INTEGER,detail TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(uidvalidity,imap_uid)
);
CREATE TABLE IF NOT EXISTS sync_runs(
  id INTEGER PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,
  new_messages INTEGER NOT NULL DEFAULT 0,error TEXT
);
"""

TICKET_RE = re.compile(
    r"(?P<name>[^，。]+)，\s*(?P<dt>\d{4}年\d{2}月\d{2}日\d{2}:\d{2})开，\s*"
    r"(?P<from>[^，]+)站-(?P<to>[^，]+)站，\s*(?P<train>[^，]+)次列车，\s*"
    r"(?P<seat>[^，]+)，\s*(?P<class>[^，]+)，\s*(?:(?P<type>[^，]+)，\s*)?"
    r"票价(?P<fare>[\d.]+)元(?:，\s*检票口(?P<gate>[^，]+))?"
)
STATUSES = {"PURCHASED", "CHANGED", "REFUNDED"}
STATUS_NAMES = {"PURCHASED": "已购票", "CHANGED": "已改签", "REFUNDED": "已退票"}


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _message_text(raw: bytes) -> tuple[email.message.EmailMessage, str]:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_type() not in {"text/html", "text/plain"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            content = re.sub(r"<[^>]+>", " ", str(content))
        parts.append(html.unescape(str(content)))
    return message, re.sub(r"\s+", " ", " ".join(parts)).strip()


def _departure(value: str) -> str:
    return datetime.strptime(value, "%Y年%m月%d日%H:%M").isoformat(timespec="minutes")


def import_message(database: Path, raw_path: Path) -> bool:
    raw = raw_path.read_bytes()
    message, body = _message_text(raw)
    message_id = str(message.get("Message-ID", "")).strip() or None
    subject = str(message.get("Subject", ""))
    date_header = message.get("Date")
    received_at = (
        date_header.datetime.isoformat()
        if date_header is not None and getattr(date_header, "datetime", None)
        else datetime.now().astimezone().isoformat()
    )
    digest = hashlib.sha256(raw).hexdigest()
    conn = connect(database)
    try:
        inserted = conn.execute(
            """INSERT OR IGNORE INTO emails(message_id,subject,received_at,sha256,raw_path,parse_status)
            VALUES(?,?,?,?,?,'PENDING')""",
            (message_id, subject, received_at, digest, str(raw_path)),
        )
        if inserted.rowcount == 0:
            return False
        email_id = inserted.lastrowid
        if "用户支付通知" in subject:
            kind = "PURCHASE"
            match = re.search(
                r"于(\d{4}年\d{2}月\d{2}日).*成功购买了\d+张车票，\s*票款共计([\d.]+)元，\s*订单号码\s*([A-Z0-9]+)",
                body,
            )
        elif "用户改签通知" in subject:
            kind = "CHANGE"
            match = re.search(
                r"于(\d{4}年\d{2}月\d{2}日).*成功改签车票\d+张，\s*新车票票款共计([\d.]+)元。\s*订单号码\s*([A-Z0-9]+)",
                body,
            )
        elif "用户退票通知" in subject:
            kind = "REFUND"
            match = re.search(
                r"于(\d{4}年\d{2}月\d{2}日).*订单号码\s*([A-Z0-9]+)\s*，\s*应退票款([\d.]+)元",
                body,
            )
        else:
            conn.execute("UPDATE emails SET parse_status='IGNORED' WHERE id=?", (email_id,))
            conn.commit()
            return True
        if not match:
            conn.execute("UPDATE emails SET parse_status='ERROR' WHERE id=?", (email_id,))
            conn.commit()
            return True
        if kind == "REFUND":
            event_date, order_no, total = match.group(1), match.group(2), float(match.group(3))
        else:
            event_date, total, order_no = match.group(1), float(match.group(2)), match.group(3)
        conn.execute(
            "INSERT INTO orders(order_no,event_at,total) VALUES(?,?,?) ON CONFLICT(order_no) DO NOTHING",
            (order_no, event_date, total),
        )
        order_id = conn.execute("SELECT id FROM orders WHERE order_no=?", (order_no,)).fetchone()[0]
        found = list(TICKET_RE.finditer(body))
        for ticket_match in found:
            item = ticket_match.groupdict()
            passenger = re.sub(
                r"^\d+\.", "", item["name"].split()[-1].split("：")[-1].split(":")[-1]
            )
            conn.execute(
                """INSERT OR IGNORE INTO ticket_events(
                email_id,order_no,event_type,event_at,passenger,payload
                ) VALUES(?,?,?,?,?,?)""",
                (email_id, order_no, kind, event_date, passenger, ticket_match.group(0)),
            )
            if kind == "PURCHASE":
                conn.execute(
                    """INSERT INTO tickets(
                    order_id,passenger,departure_at,from_station,to_station,train_no,
                    seat_raw,seat_class,ticket_type,fare,gate,status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'PURCHASED')""",
                    (
                        order_id, passenger, _departure(item["dt"]), item["from"].strip(), item["to"].strip(),
                        item["train"].strip(), item["seat"].strip(), item["class"].strip(), item["type"].strip() if item["type"] else None,
                        float(item["fare"]), item["gate"],
                    ),
                )
                continue
            current = conn.execute(
                """SELECT id FROM tickets WHERE order_id=? AND passenger=? AND status!='REFUNDED'
                ORDER BY id DESC LIMIT 1""",
                (order_id, passenger),
            ).fetchone()
            if current and kind == "CHANGE":
                conn.execute(
                    """UPDATE tickets SET departure_at=?,from_station=?,to_station=?,train_no=?,
                    seat_raw=?,seat_class=?,ticket_type=?,fare=?,gate=?,status='CHANGED' WHERE id=?""",
                    (
                        _departure(item["dt"]), item["from"].strip(), item["to"].strip(), item["train"].strip(),
                        item["seat"].strip(), item["class"].strip(), item["type"].strip() if item["type"] else None, float(item["fare"]),
                        item["gate"], current[0],
                    ),
                )
            elif current and kind == "REFUND":
                fee = re.search(r"退票费([\d.]+)元，应退票款([\d.]+)元", ticket_match.group(0))
                conn.execute(
                    "UPDATE tickets SET status='REFUNDED',refund_fee=?,refund_amount=? WHERE id=?",
                    (float(fee.group(1)) if fee else None, float(fee.group(2)) if fee else total, current[0]),
                )
        conn.execute(
            "UPDATE emails SET parse_status=? WHERE id=?",
            ("PARSED" if found else "ERROR", email_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def query_tickets(database: Path, filters: dict[str, str]) -> list[dict]:
    clauses: list[str] = []
    params: list[str] = []
    for key, column in (
        ("from_station", "t.from_station"),
        ("to_station", "t.to_station"),
        ("train_no", "t.train_no"),
        ("order_no", "o.order_no"),
        ("passenger", "t.passenger"),
    ):
        value = filters.get(key, "").strip()
        if value:
            clauses.append(f"{column} LIKE ?")
            params.append(f"%{value}%")
    for key, operator in (("date_from", ">="), ("date_to", "<=")):
        value = filters.get(key, "").strip()
        if value:
            clauses.append(f"date(t.departure_at) {operator} date(?)")
            params.append(value)
    status = filters.get("status", "").strip()
    if status:
        if status not in STATUSES:
            raise ValueError("invalid ticket status")
        clauses.append("t.status=?")
        params.append(status)
    where = " AND ".join(clauses) if clauses else "1=1"
    conn = connect(database, readonly=True)
    try:
        rows = conn.execute(
            f"""SELECT t.*,o.order_no FROM tickets t LEFT JOIN orders o ON o.id=t.order_id
            WHERE {where} ORDER BY t.departure_at DESC,t.id DESC""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def counts(database: Path) -> dict[str, int]:
    conn = connect(database, readonly=True)
    try:
        return {
            name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in ("emails", "tickets", "ticket_events")
        }
    finally:
        conn.close()


def build_workbook(rows: list[dict], filters: dict[str, str]) -> bytes:
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    workbook.set_properties({"title": "12306 车票归档导出", "author": "12306 车票归档"})
    sheet = workbook.add_worksheet("车票记录")
    info = workbook.add_worksheet("导出说明")
    sheet.hide_gridlines(2)
    header = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#1769AA", "align": "center"})
    normal = workbook.add_format({"font_color": "#18212B", "valign": "vcenter"})
    centered = workbook.add_format({"font_color": "#18212B", "align": "center"})
    date_format = workbook.add_format({"font_color": "#18212B", "num_format": "yyyy-mm-dd hh:mm", "align": "center"})
    money = workbook.add_format({"font_color": "#18212B", "num_format": "¥0.00", "align": "right"})
    columns = [
        ("旅客姓名", "passenger", 12, normal), ("状态", "status", 10, centered),
        ("出发时间", "departure_at", 18, date_format), ("车次", "train_no", 10, centered),
        ("出发站", "from_station", 14, normal), ("到达站", "to_station", 14, normal),
        ("席位", "seat_raw", 22, normal), ("席别", "seat_class", 12, normal),
        ("票种", "ticket_type", 10, centered), ("票价", "fare", 11, money),
        ("检票口", "gate", 12, centered), ("订单号", "order_no", 22, normal),
        ("退票手续费", "refund_fee", 13, money), ("退款金额", "refund_amount", 13, money),
    ]
    for index, (label, _, width, _) in enumerate(columns):
        sheet.write(0, index, label, header)
        sheet.set_column(index, index, width)
    for row_index, row in enumerate(rows, start=1):
        for column_index, (_, key, _, cell_format) in enumerate(columns):
            value = row.get(key)
            if key == "status":
                value = STATUS_NAMES.get(str(value), value)
            if key == "departure_at" and value:
                try:
                    value = datetime.fromisoformat(str(value))
                except ValueError:
                    pass
            if key in {"fare", "refund_fee", "refund_amount"} and value is not None:
                sheet.write_number(row_index, column_index, float(value), cell_format)
            elif key == "departure_at" and isinstance(value, datetime):
                sheet.write_datetime(row_index, column_index, value, cell_format)
            else:
                sheet.write(row_index, column_index, "" if value is None else value, cell_format)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    info.set_column("A:A", 18)
    info.set_column("B:B", 46)
    info.write(0, 0, "导出时间", header)
    info.write(0, 1, time.strftime("%Y-%m-%d %H:%M:%S %z"), normal)
    info.write(1, 0, "记录数", header)
    info.write(1, 1, len(rows), normal)
    labels = {"passenger": "旅客", "train_no": "车次", "from_station": "出发站", "to_station": "到达站", "date_from": "起始日期", "date_to": "结束日期", "status": "状态", "order_no": "订单号"}
    row_index = 2
    for key, value in filters.items():
        if value:
            info.write(row_index, 0, labels.get(key, key), header)
            info.write(row_index, 1, value, normal)
            row_index += 1
    workbook.close()
    return output.getvalue()
