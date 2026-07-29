from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import Settings
from .mail_sync import SyncResult, latest_status, sync_once
from .storage import build_workbook, counts, initialize, query_tickets


BASE = Path(__file__).resolve().parent
SETTINGS = Settings.from_env()
SESSION_COOKIE = "ticket_archive_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
API_FIELDS = (
    "id", "passenger", "status", "departure_at", "train_no", "from_station",
    "to_station", "seat_raw", "seat_class", "ticket_type", "fare", "gate",
    "order_no", "refund_fee", "refund_amount",
)
FILTER_KEYS = (
    "from_station", "to_station", "date_from", "date_to", "train_no",
    "order_no", "status", "passenger",
)

_sync_lock = threading.Lock()
_sync_result: SyncResult | None = None
_login_attempts: dict[str, deque[float]] = defaultdict(deque)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session() -> str:
    payload = f"{SETTINGS.username}|{int(time.time()) + SESSION_SECONDS}".encode()
    signature = hmac.new(SETTINGS.secret.encode(), payload, hashlib.sha256).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_session(token: str) -> bool:
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = _decode(payload_text)
        signature = _decode(signature_text)
        expected = hmac.new(SETTINGS.secret.encode(), payload, hashlib.sha256).digest()
        username, expires = payload.decode().rsplit("|", 1)
        return (
            hmac.compare_digest(signature, expected)
            and hmac.compare_digest(username, SETTINGS.username)
            and int(expires) > int(time.time())
        )
    except (ValueError, UnicodeError):
        return False


def require_session(ticket_archive_session: Annotated[str | None, Cookie()] = None) -> str:
    if not ticket_archive_session or not verify_session(ticket_archive_session):
        raise HTTPException(401, "未登录或登录已过期")
    return SETTINGS.username


def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 API 密钥", headers={"WWW-Authenticate": "Bearer"})
    if not secrets.compare_digest(authorization[7:].strip(), SETTINGS.api_key):
        raise HTTPException(401, "API 密钥无效")


def _masked_email(address: str) -> str:
    local, separator, domain = address.partition("@")
    return f"{local[:2]}***{separator}{domain}" if separator else "***"


def _run_sync() -> None:
    global _sync_result
    if not _sync_lock.acquire(blocking=False):
        return
    try:
        _sync_result = sync_once(SETTINGS)
    finally:
        _sync_lock.release()


def request_sync() -> bool:
    if _sync_lock.locked():
        return False
    threading.Thread(target=_run_sync, name="ticket-mail-sync", daemon=True).start()
    return True


async def _scheduler() -> None:
    if SETTINGS.sync_on_start:
        request_sync()
    while True:
        await asyncio.sleep(SETTINGS.sync_interval_minutes * 60)
        request_sync()


@asynccontextmanager
async def lifespan(_: FastAPI):
    os.umask(0o077)
    SETTINGS.data_dir.mkdir(parents=True, exist_ok=True)
    SETTINGS.raw_dir.mkdir(parents=True, exist_ok=True)
    initialize(SETTINGS.database)
    task = asyncio.create_task(_scheduler())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="12306车票长存 API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)


def _filters(**values: str) -> dict[str, str]:
    return {key: values.get(key, "") for key in FILTER_KEYS}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE / "index.html", media_type="text/html")


@app.get("/favicon.png", include_in_schema=False)
def favicon():
    return FileResponse(BASE / "favicon.png", media_type="image/png")


@app.post("/api/login")
def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    client = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = _login_attempts[client]
    while attempts and attempts[0] < now - 300:
        attempts.popleft()
    if len(attempts) >= 10:
        raise HTTPException(429, "登录尝试过于频繁，请稍后重试")
    if not (
        secrets.compare_digest(username, SETTINGS.username)
        and secrets.compare_digest(password, SETTINGS.password)
    ):
        attempts.append(now)
        raise HTTPException(401, "用户名或密码错误")
    attempts.clear()
    response = JSONResponse({"ok": True})
    response.set_cookie(
        SESSION_COOKIE,
        create_session(),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=SETTINGS.cookie_secure,
        samesite="strict",
        path="/",
    )
    request_sync()
    return response


@app.post("/api/logout")
def logout(_: Annotated[str, Depends(require_session)]):
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/session")
def session(ticket_archive_session: Annotated[str | None, Cookie()] = None):
    authenticated = bool(ticket_archive_session and verify_session(ticket_archive_session))
    return {
        "ok": True,
        "authenticated": authenticated,
        "username": SETTINGS.username if authenticated else None,
    }


@app.get("/api/status")
def status(_: Annotated[str, Depends(require_session)]):
    stored = latest_status(SETTINGS.database)
    return {
        "syncing": _sync_lock.locked(),
        "mail_mode": SETTINGS.mail_mode,
        "mailbox": _masked_email(SETTINGS.imap_email),
        "source_email": _masked_email(SETTINGS.source_email) if SETTINGS.source_email else None,
        "auto_confirm_forwarding": SETTINGS.auto_confirm_forwarding,
        "sync_interval_minutes": SETTINGS.sync_interval_minutes,
        "last_sync": stored,
        "counts": counts(SETTINGS.database),
    }


@app.post("/api/refresh", status_code=202)
def refresh(_: Annotated[str, Depends(require_session)]):
    started = request_sync()
    return {"ok": True, "started": started, "message": "正在刷新" if started else "刷新已在进行中"}


@app.get("/api/tickets")
def tickets(
    _: Annotated[str, Depends(require_session)],
    from_station: str = "", to_station: str = "", date_from: str = "",
    date_to: str = "", train_no: str = "", order_no: str = "",
    status: str = "", passenger: str = "",
):
    try:
        items = query_tickets(SETTINGS.database, _filters(**locals()))
    except ValueError as exc:
        raise HTTPException(400, "筛选条件无效") from exc
    return {"items": items, "count": len(items)}


@app.get("/api/export.xlsx")
def export_excel(
    _: Annotated[str, Depends(require_session)],
    from_station: str = "", to_station: str = "", date_from: str = "",
    date_to: str = "", train_no: str = "", order_no: str = "",
    status: str = "", passenger: str = "",
):
    filters = _filters(**locals())
    try:
        rows = query_tickets(SETTINGS.database, filters)
    except ValueError as exc:
        raise HTTPException(400, "筛选条件无效") from exc
    filename = f"ticket-archive-{time.strftime('%Y%m%d-%H%M%S')}.xlsx"
    return Response(
        build_workbook(rows, filters),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/v1/tickets", dependencies=[Depends(require_api_key)])
def api_tickets(
    from_station: str = "", to_station: str = "", date_from: str = "",
    date_to: str = "", train_no: str = "", order_no: str = "",
    status: str = "", passenger: str = "",
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    try:
        rows = query_tickets(SETTINGS.database, _filters(**locals()))
    except ValueError as exc:
        raise HTTPException(400, "筛选条件无效") from exc
    page = rows[cursor:cursor + limit]
    items = [{key: row.get(key) for key in API_FIELDS} for row in page]
    next_cursor = cursor + len(page) if cursor + len(page) < len(rows) else None
    return {"items": items, "count": len(items), "next_cursor": next_cursor}


@app.get("/api/v1/tickets/{ticket_id}", dependencies=[Depends(require_api_key)])
def api_ticket(ticket_id: int):
    row = next((item for item in query_tickets(SETTINGS.database, {}) if item["id"] == ticket_id), None)
    if not row:
        raise HTTPException(404, "车票不存在")
    return {key: row.get(key) for key in API_FIELDS}


@app.get("/api/health")
def health():
    try:
        values = counts(SETTINGS.database)
        return {"ok": True, "tickets": values["tickets"]}
    except sqlite3.Error as exc:
        return JSONResponse({"ok": False, "error": type(exc).__name__}, status_code=503)
