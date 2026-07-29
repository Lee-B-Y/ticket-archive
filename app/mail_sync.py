from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import json
import os
import re
import ssl
import sqlite3
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .config import Settings
from .forward_verification import (
    ConfirmationResult,
    confirm_forward_verification,
    detect_forward_verification,
)
from .forwarded_mail import OFFICIAL_SENDERS, header_addresses, route_forwarded_message
from .storage import connect, import_message


imaplib.Commands.setdefault("ID", ("AUTH",))
TRANSIENT_ERRORS = (TimeoutError, ConnectionError, ssl.SSLError, imaplib.IMAP4.abort)


@dataclass
class SyncResult:
    ok: bool
    scanned: int = 0
    new_messages: int = 0
    rejected: int = 0
    verifications_confirmed: int = 0
    verifications_failed: int = 0
    verifications_ambiguous: int = 0
    error: str = ""


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _same_mailbox(state: dict, settings: Settings, uidvalidity: int) -> bool:
    return (
        state.get("email", "").casefold() == settings.imap_email.casefold()
        and state.get("host", "").casefold() == settings.imap_host.casefold()
        and state.get("folder") == settings.imap_folder
        and state.get("mode") == settings.mail_mode
        and state.get("source_email", "").casefold() == settings.source_email.casefold()
        and state.get("uidvalidity") == uidvalidity
    )


def _store_raw(settings: Settings, raw: bytes) -> bool:
    settings.raw_dir.mkdir(parents=True, exist_ok=True)
    path = settings.raw_dir / f"{hashlib.sha256(raw).hexdigest()}.eml"
    if not path.exists():
        path.write_bytes(raw)
        os.chmod(path, 0o600)
    return import_message(settings.database, path)


def _record_rejection(
    database: Path,
    uidvalidity: int,
    uid: int,
    digest: str,
    reason: str,
    subject: str,
) -> None:
    conn = connect(database)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO forward_quarantine(
            uidvalidity,imap_uid,digest,reason,subject
            ) VALUES(?,?,?,?,?)""",
            (uidvalidity, uid, digest, reason, subject[:500]),
        )
        conn.commit()
    finally:
        conn.close()


def _record_verification(
    database: Path,
    uidvalidity: int,
    uid: int,
    detection,
    result: ConfirmationResult,
) -> None:
    parsed = urllib.parse.urlsplit(detection.action_url) if detection.action_url else None
    digest = hashlib.sha256(detection.action_url.encode()).hexdigest() if detection.action_url else None
    conn = connect(database)
    try:
        conn.execute(
            """INSERT INTO forward_verification_events(
            uidvalidity,imap_uid,subject,sender_domain,action_host,action_sha256,
            status,http_status,detail
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(uidvalidity,imap_uid) DO UPDATE SET
            status=excluded.status,http_status=excluded.http_status,detail=excluded.detail""",
            (
                uidvalidity,
                uid,
                detection.subject[:500],
                detection.sender_domain[:255],
                parsed.hostname[:255] if parsed and parsed.hostname else None,
                digest,
                result.status,
                result.http_status,
                result.detail[:500],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _begin_run(database: Path) -> int:
    conn = connect(database)
    try:
        cursor = conn.execute(
            "INSERT INTO sync_runs(started_at,status) VALUES(?, 'running')",
            (datetime.now().astimezone().isoformat(timespec="seconds"),),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _finish_run(database: Path, run_id: int, result: SyncResult) -> None:
    conn = connect(database)
    try:
        conn.execute(
            """UPDATE sync_runs SET finished_at=?,status=?,new_messages=?,error=? WHERE id=?""",
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                "success" if result.ok else "failed",
                result.new_messages,
                result.error[:500] or None,
                run_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _sync_attempt(settings: Settings) -> SyncResult:
    state = _load_state(settings.state_file)
    client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=20)
    selected = False
    try:
        client.login(settings.imap_email, settings.imap_auth_code)
        if settings.imap_host.casefold() in {"imap.163.com", "imap.126.com", "imap.yeah.net"}:
            client._simple_command(
                "ID", '("name" "ticket-archive" "version" "1.0" "vendor" "self-hosted")'
            )
        status, _ = client.select(settings.imap_folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot select IMAP folder {settings.imap_folder}")
        selected = True
        uidvalidity = int(client.response("UIDVALIDITY")[1][0])
        same = _same_mailbox(state, settings, uidvalidity)
        last_uid = int(state.get("last_uid", 0)) if same else 0
        verification_last_uid = (
            int(state.get("verification_last_uid", 0))
            if same and settings.mail_mode == "forward"
            else last_uid
        )
        start_uid = min(last_uid, verification_last_uid) + 1
        status, data = client.uid("SEARCH", None, f"UID {start_uid}:*")
        ids = data[0].split() if status == "OK" and data and data[0] else []
        result = SyncResult(ok=True)
        max_uid = last_uid
        verification_max_uid = verification_last_uid
        for uid_bytes in ids:
            uid = int(uid_bytes)
            status, fetched = client.uid("FETCH", uid_bytes, "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError(f"cannot fetch IMAP UID {uid}")
            raw = next((item[1] for item in fetched if isinstance(item, tuple)), b"")
            if not raw:
                raise RuntimeError(f"empty IMAP UID {uid}")
            result.scanned += 1
            recognized_verification = False
            if settings.mail_mode == "forward" and uid > verification_last_uid:
                detection = detect_forward_verification(raw)
                recognized_verification = detection.recognized
                if detection.recognized:
                    if not detection.action_url:
                        confirmation = ConfirmationResult("ambiguous", None, detection.reason)
                        result.verifications_ambiguous += 1
                    elif not settings.auto_confirm_forwarding:
                        confirmation = ConfirmationResult("disabled", None, "automatic confirmation disabled")
                    else:
                        try:
                            confirmation = confirm_forward_verification(detection.action_url)
                        except Exception as exc:
                            confirmation = ConfirmationResult("failed", None, type(exc).__name__)
                        if confirmation.status == "confirmed":
                            result.verifications_confirmed += 1
                        else:
                            result.verifications_failed += 1
                    _record_verification(settings.database, uidvalidity, uid, detection, confirmation)
                verification_max_uid = max(verification_max_uid, uid)
            if uid <= last_uid or recognized_verification:
                max_uid = max(max_uid, uid)
                continue
            if settings.mail_mode == "direct":
                message = email.message_from_bytes(raw, policy=email.policy.default)
                senders = set(header_addresses(message, "From", "Sender"))
                if senders and senders.issubset(OFFICIAL_SENDERS) and _store_raw(settings, raw):
                    result.new_messages += 1
            else:
                batch = route_forwarded_message(raw, {settings.source_email: "owner"})
                outer_digest = hashlib.sha256(raw).hexdigest()
                for routed in batch.routed:
                    if _store_raw(settings, routed.canonical_raw):
                        result.new_messages += 1
                for rejected in batch.rejected:
                    _record_rejection(
                        settings.database,
                        uidvalidity,
                        uid,
                        outer_digest,
                        rejected.reason,
                        rejected.subject,
                    )
                    result.rejected += 1
            max_uid = max(max_uid, uid)
        _write_state(
            settings.state_file,
            {
                "email": settings.imap_email,
                "host": settings.imap_host,
                "folder": settings.imap_folder,
                "mode": settings.mail_mode,
                "source_email": settings.source_email,
                "uidvalidity": uidvalidity,
                "last_uid": max_uid,
                "verification_last_uid": verification_max_uid,
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
        return result
    finally:
        try:
            if selected:
                client.close()
        finally:
            try:
                client.logout()
            except Exception:
                pass


def sync_once(settings: Settings, attempts: int = 3) -> SyncResult:
    run_id = _begin_run(settings.database)
    result: SyncResult | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = _sync_attempt(settings)
            break
        except TRANSIENT_ERRORS as exc:
            if attempt == attempts:
                result = SyncResult(ok=False, error=f"{type(exc).__name__}: {exc}")
                break
            time.sleep(attempt)
        except Exception as exc:
            detail = re.sub(r"[\r\n\t]+", " ", str(exc)).strip()[:400]
            result = SyncResult(ok=False, error=f"{type(exc).__name__}: {detail}")
            break
    if result is None:
        result = SyncResult(ok=False, error="sync retry loop ended unexpectedly")
    _finish_run(settings.database, run_id, result)
    return result


def latest_status(database: Path) -> dict:
    conn = connect(database, readonly=True)
    try:
        row = conn.execute(
            """SELECT started_at,finished_at,status,new_messages,error
            FROM sync_runs ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return dict(row) if row else {
            "started_at": None,
            "finished_at": None,
            "status": "never",
            "new_messages": 0,
            "error": None,
        }
    finally:
        conn.close()
