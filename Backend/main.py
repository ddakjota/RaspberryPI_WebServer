import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from threading import Lock
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException

import push
import store
from limits import APIRequestLimits, Window

COOKIE = "laundry_session"
Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Index = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class Reading(BaseModel):
    sensor_id: Identifier
    boot_id: Identifier
    sequence: Index
    brightness: Index


class Registration(BaseModel):
    sensor_id: Identifier
    registration_code: Annotated[str, Field(min_length=1, max_length=256)]


class Keys(BaseModel):
    p256dh: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+={0,2}$")]
    auth: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+={0,2}$")]


class Subscription(BaseModel):
    endpoint: Annotated[str, Field(max_length=2048)]
    keys: Keys


class Settings(BaseModel):
    notifications_enabled: Annotated[bool, Field(strict=True)]
    disconnect_subscription: Annotated[bool, Field(strict=True)] = False
    subscription: Subscription | None = None


class APIError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def error(status, code, message):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def unauthorized():
    return APIError(401, "INVALID_TOKEN", "인증에 실패했습니다.")


def registration_role(device, digest, now):
    if device:
        if device["owner_code_expires"] and device["owner_code_expires"] > now and secrets.compare_digest(device["owner_code"], digest):
            return "owner"
        if device["code_expires"] > now and secrets.compare_digest(device["code"], digest):
            return "member"
    return None


def save_subscription(db, installation_id, data, now):
    if not push.trusted_endpoint(data.endpoint):
        raise APIError(400, "INVALID_ENDPOINT", "지원되지 않는 알림 구독 주소입니다.")
    db.execute("DELETE FROM phones WHERE endpoint=? AND expires_at<=? AND installation_id<>?",
               (data.endpoint, now, installation_id))
    owner = db.execute("SELECT installation_id FROM phones WHERE endpoint=?", (data.endpoint,)).fetchone()
    if owner and owner["installation_id"] != installation_id:
        raise APIError(409, "SUBSCRIPTION_CONFLICT", "이미 등록된 알림 구독입니다.")
    db.execute("UPDATE phones SET subscription=?, endpoint=? WHERE installation_id=?",
               (data.model_dump_json(), data.endpoint, installation_id))


def maintain(path, now, demo_sensor_id=None):
    with store.transaction(path) as db:
        for device in db.execute("SELECT * FROM sensors WHERE state='running' AND last_at<=? AND sensor_id IS NOT ?", (now - 30, demo_sensor_id)):
            store.expire_sensor(db, device, now)
        db.execute("DELETE FROM sessions WHERE ended_at<=?", (now - 30 * store.DAY,))
        db.execute("DELETE FROM outbox WHERE expires<=?", (now,))
        db.execute("DELETE FROM phones WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM push_test_events WHERE requested_at<=?", (now - 60,))


def deliver_one(path, clock):
    if not push.configured():
        return
    now = clock()
    with store.transaction(path) as db:
        entry = db.execute("""SELECT o.*, p.enabled, p.subscription FROM outbox o
            JOIN phones p ON o.phone=p.installation_id
            WHERE o.next_at<=? AND o.expires>? AND p.expires_at>?
            ORDER BY (o.kind='test'), o.next_at LIMIT 1""", (now, now, now)).fetchone()
        if not entry:
            return
        if not entry["subscription"] or (not entry["enabled"] and entry["kind"] != "test"):
            db.execute("DELETE FROM outbox WHERE id=?", (entry["id"],))
            return
        db.execute("UPDATE outbox SET next_at=? WHERE id=?", (now + 20, entry["id"]))
    remaining = entry["expires"] - clock()
    if remaining <= 0:
        return
    try:
        status = push.send(json.loads(entry["subscription"]), entry["kind"], entry["id"], int(remaining), entry["phone"])
    except Exception as exc:
        logging.warning("Push delivery failed (%s)", type(exc).__name__)
        return
    if 200 <= status < 300 or status in (400, 404, 410, 413):
        with store.transaction(path) as db:
            if status >= 400:
                current = db.execute("SELECT subscription FROM phones WHERE installation_id=?",
                                     (entry["phone"],)).fetchone()
                if current and current["subscription"] != entry["subscription"]:
                    return  # A replacement subscription can still deliver this event.
            db.execute("DELETE FROM outbox WHERE id=?", (entry["id"],))
            if status in (404, 410):
                db.execute("""UPDATE phones SET subscription=NULL, endpoint=NULL WHERE
                    installation_id=? AND subscription=?""", (entry["phone"], entry["subscription"]))


def create_app(path=store.DB, clock=time.time, background=True):
    path = Path(path)
    demo_sensor_id = os.environ.get("LAUNDRY_DEMO_SENSOR")
    origin = os.environ.get("LAUNDRY_ORIGIN", "http://localhost:8000").rstrip("/")
    secure = not origin.startswith(("http://localhost:", "http://127.0.0.1:"))
    min_delta = max(1, int(os.environ.get("LAUNDRY_MIN_DELTA", "80")))
    registration_failures = Window(900, 2000)
    failure_lock = Lock()

    def phone(db, request, sensor_id=None):
        token = store.digest(request.cookies.get(COOKIE, ""))
        row = db.execute("SELECT * FROM phones WHERE token=? AND expires_at>?", (token, clock())).fetchone()
        if row is None:
            raise unauthorized()
        if sensor_id is not None and row["sensor_id"] != sensor_id:
            raise APIError(403, "FORBIDDEN", "등록된 센서에만 접근할 수 있습니다.")
        return row

    def can_control(row):
        return bool(row["controller"]) or (not secure and row["sensor_id"] == demo_sensor_id)

    def controller(db, request, sensor_id):
        if not can_control(phone(db, request, sensor_id)):
            raise APIError(403, "CONTROLLER_REQUIRED", "센서 관리자만 감지 설정을 바꿀 수 있습니다.")

    async def worker(job):
        while True:
            try:
                await asyncio.to_thread(job)
            except Exception:
                logging.exception("Maintenance failed")
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(_app):
        store.initialize(path, clock())
        with store.transaction(path) as db:
            db.execute("UPDATE sessions SET ended_at=?, end_reason='server_restart' WHERE ended_at IS NULL", (clock(),))
            db.execute("UPDATE sensors SET state='idle', session_id=NULL, started_at=NULL, phase=NULL, phase_at=NULL, last_at=NULL")
        jobs = (lambda: maintain(path, clock(), demo_sensor_id), lambda: deliver_one(path, clock))
        tasks = [asyncio.create_task(worker(job)) for job in jobs] if background else []
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks)

    app = FastAPI(title="Laundry Alert", lifespan=lifespan)
    api = APIRouter(prefix="/api/v1")

    @app.exception_handler(APIError)
    async def api_error(_request, exc):
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _exc):
        return error(422, "INVALID_REQUEST", "요청 형식이 올바르지 않습니다.")

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        return error(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(sqlite3.Error)
    async def database_error(_request, _exc):
        logging.exception("Database request failed")
        return error(503, "DATABASE_UNAVAILABLE", "잠시 후 다시 시도해 주세요.")

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path.startswith("/api/"):
            browser_mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/api/v1/sensor/readings"
            if browser_mutation and (request.headers.get("origin") != origin or request.headers.get("x-laundry-request") != "1"):
                return error(403, "CSRF_REJECTED", "허용된 앱에서 다시 시도해 주세요.")
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)

    @api.post("/sensor/readings")
    def ingest(sample: Reading, request: Request):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise unauthorized()
        digest = store.digest(token)
        with store.transaction(path, write=False) as db:
            device = db.execute("SELECT token FROM sensors WHERE sensor_id=?", (sample.sensor_id,)).fetchone()
            if not device or not secrets.compare_digest(device["token"], digest):
                raise unauthorized()
        with store.transaction(path) as db:
            device = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (sample.sensor_id,)).fetchone()
            if not device or not secrets.compare_digest(device["token"], digest):
                raise unauthorized()
            store.reading(db, device, sample, clock(), min_delta)
        return {"success": True}

    @api.post("/registrations", status_code=201)
    def register(data: Registration, request: Request, response: Response):
        digest = store.digest(data.registration_code)
        invalid_code = APIError(401, "INVALID_REGISTRATION", "센서 ID 또는 등록 코드가 올바르지 않습니다.")
        with store.transaction(path, write=False) as db:
            device = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (data.sensor_id,)).fetchone()
            if not registration_role(device, digest, clock()):
                peer = request.client.host if request.client else "unknown"
                with failure_lock:
                    allowed = registration_failures.allow(peer, 5, clock())
                if not allowed:
                    raise APIError(429, "REGISTRATION_LOCKED", "등록 시도가 제한되었습니다. 잠시 후 다시 시도해 주세요.")
                raise invalid_code
        with store.transaction(path) as db:
            now = clock()
            device = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (data.sensor_id,)).fetchone()
            role = registration_role(device, digest, now)
            if not role:
                raise invalid_code
            installation, token = uuid4().hex, secrets.token_urlsafe(32)
            previous = db.execute("SELECT * FROM phones WHERE token=? AND expires_at>?",
                                  (store.digest(request.cookies.get(COOKIE, "")), now)).fetchone()
            owner = role == "owner"
            keeps_control = previous and previous["sensor_id"] == data.sensor_id and previous["controller"]
            db.execute("""INSERT INTO phones(installation_id,sensor_id,token,enabled,expires_at,controller)
                VALUES (?,?,?,0,?,?)""", (installation, data.sensor_id, store.digest(token),
                                        now + store.SESSION_TTL, bool(owner or keeps_control)))
            if previous:
                db.execute("DELETE FROM phones WHERE installation_id=?", (previous["installation_id"],))
            if owner:
                db.execute("UPDATE sensors SET owner_code=NULL, owner_code_expires=NULL WHERE sensor_id=?", (data.sensor_id,))
        response.set_cookie(COOKIE, token, max_age=store.SESSION_TTL, secure=secure, httponly=True, samesite="strict", path="/api/v1")
        return {"sensor_id": data.sensor_id, "installation_id": installation}

    @api.get("/me")
    def get_me(request: Request):
        with store.transaction(path, write=False) as db:
            row = phone(db, request)
            return {"installation_id": row["installation_id"], "sensor_id": row["sensor_id"],
                    "notifications_enabled": bool(row["enabled"]), "can_control": can_control(row)}

    @api.delete("/me", status_code=204)
    def unlink(request: Request, response: Response):
        with store.transaction(path) as db:
            row = phone(db, request)
            db.execute("DELETE FROM phones WHERE installation_id=?", (row["installation_id"],))
        response.delete_cookie(COOKIE, path="/api/v1", secure=secure, httponly=True, samesite="strict")

    @api.get("/devices/{sensor_id}/status")
    def status(sensor_id: str, request: Request):
        with store.transaction(path, write=False) as db:
            phone(db, request, sensor_id)
            row = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
        now = clock()
        demo = sensor_id == demo_sensor_id
        online = demo or (row["last_at"] is not None and now - row["last_at"] < 30)
        state = "error" if row["state"] == "running" and not online else row["state"]
        return {
            "sensor_id": sensor_id, "demo": demo, "status": state, "connection": "online" if online else "offline",
            "monitoring_enabled": bool(row["monitoring"]), "started_at": store.timestamp(row["started_at"]),
            "last_reading_at": store.timestamp(row["last_at"]),
            "error_code": "SENSOR_OFFLINE" if state == "error" else None,
        }

    @api.post("/devices/{sensor_id}/demo/{action}")
    def demo_cycle(sensor_id: str, action: str, request: Request):
        with store.transaction(path) as db:
            phone(db, request, sensor_id)
            if sensor_id != demo_sensor_id or action not in ("start", "complete"):
                raise APIError(404, "NOT_FOUND", "테스트 기능을 사용할 수 없습니다.")
            controller(db, request, sensor_id)
            row = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
            now = clock()
            running = row["state"] == "running"
            if not row["monitoring"] or running == (action == "start"):
                raise APIError(409, "STATE_CONFLICT", "현재 상태에서는 이 테스트를 실행할 수 없습니다.")
            if action == "start":
                session_id = uuid4().hex
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, NULL, NULL)", (session_id, sensor_id, now))
                db.execute("""UPDATE sensors SET state='running', started_at=?, session_id=?, last_at=?
                    WHERE sensor_id=?""", (now, session_id, now, sensor_id))
            else:
                store.close_session(db, row["session_id"], "completed", now)
                store.broadcast(db, sensor_id, "completed", now)
                db.execute("""UPDATE sensors SET state='completed', started_at=NULL, session_id=NULL,
                    last_at=? WHERE sensor_id=?""", (now, sensor_id))
        return {"success": True}

    def control(sensor_id, request, enabled):
        with store.transaction(path) as db:
            controller(db, request, sensor_id)
            row = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
            if bool(row["monitoring"]) == enabled:
                raise APIError(409, "STATE_CONFLICT", "이미 요청한 감지 상태입니다.")
            if row["session_id"]:
                store.close_session(db, row["session_id"], "cancelled", clock())
            db.execute("""UPDATE sensors SET monitoring=?, state='idle', session_id=NULL,
                started_at=NULL, phase=NULL, phase_at=NULL, last_brightness=NULL WHERE sensor_id=?""",
                (enabled, sensor_id))
            return {"status": "idle", "monitoring_enabled": enabled}

    @api.post("/devices/{sensor_id}/cancel")
    def cancel(sensor_id: str, request: Request):
        return control(sensor_id, request, False)

    @api.post("/devices/{sensor_id}/resume")
    def resume(sensor_id: str, request: Request):
        return control(sensor_id, request, True)

    @api.get("/devices/{sensor_id}/sessions")
    def sessions(sensor_id: str, request: Request):
        with store.transaction(path, write=False) as db:
            phone(db, request, sensor_id)
            rows = db.execute("""SELECT * FROM sessions WHERE sensor_id=? AND (ended_at IS NULL OR ended_at>?)
                ORDER BY started_at DESC""", (sensor_id, clock() - 30 * store.DAY)).fetchall()
            return {"sessions": [{
                "session_id": row["session_id"], "started_at": store.timestamp(row["started_at"]),
                "ended_at": store.timestamp(row["ended_at"]), "end_reason": row["end_reason"],
            } for row in rows]}

    @api.get("/push/public-key")
    def public_key(request: Request):
        with store.transaction(path, write=False) as db:
            phone(db, request)
        return {"public_key": os.environ["VAPID_PUBLIC_KEY"] if push.configured() else None}

    @api.get("/push/subscription")
    def subscription_status(request: Request):
        with store.transaction(path, write=False) as db:
            row = phone(db, request)
            return {"endpoint": row["endpoint"]}

    @api.put("/push/subscription")
    def subscribe(data: Subscription, request: Request):
        with store.transaction(path) as db:
            row = phone(db, request)
            save_subscription(db, row["installation_id"], data, clock())
        return {"success": True}

    @api.delete("/push/subscription", status_code=204)
    def unsubscribe(request: Request):
        with store.transaction(path) as db:
            row = phone(db, request)
            if row["enabled"]:
                raise APIError(409, "PUSH_STILL_ENABLED", "알림이 다시 켜져 구독을 유지했습니다.")
            db.execute("DELETE FROM outbox WHERE phone=?", (row["installation_id"],))
            db.execute("UPDATE phones SET subscription=NULL, endpoint=NULL WHERE installation_id=?", (row["installation_id"],))

    @api.patch("/push/settings")
    def settings(data: Settings, request: Request):
        with store.transaction(path) as db:
            row = phone(db, request)
            installation_id = row["installation_id"]
            if data.subscription is not None:
                if not data.notifications_enabled or data.disconnect_subscription:
                    raise APIError(422, "INVALID_REQUEST", "구독은 알림을 켤 때 등록할 수 있습니다.")
                save_subscription(db, installation_id, data.subscription, clock())
            if not data.notifications_enabled and data.disconnect_subscription:
                db.execute("""UPDATE phones SET enabled=0, subscription=NULL, endpoint=NULL
                    WHERE installation_id=?""", (installation_id,))
                db.execute("DELETE FROM outbox WHERE phone=?", (installation_id,))
            else:
                db.execute("UPDATE phones SET enabled=? WHERE installation_id=?",
                           (data.notifications_enabled, installation_id))
                if not data.notifications_enabled:
                    db.execute("DELETE FROM outbox WHERE phone=? AND kind!='test'", (installation_id,))
        return {"notifications_enabled": data.notifications_enabled}

    @api.post("/push/test")
    def test_push(request: Request):
        now = clock()
        with store.transaction(path) as db:
            row = phone(db, request)
            if not push.configured():
                raise APIError(503, "PUSH_UNAVAILABLE", "서버에 Web Push 키가 설정되지 않았습니다.")
            if not row["subscription"]:
                raise APIError(409, "NO_SUBSCRIPTION", "먼저 기기에서 알림을 연결해 주세요.")
            if row["last_test"] is not None and now - row["last_test"] < 15:
                raise APIError(429, "RATE_LIMITED", "15초 후에 다시 시도해 주세요.")
            total, sensor_total = db.execute("""SELECT COUNT(*), COUNT(CASE WHEN sensor_id=? THEN 1 END)
                FROM push_test_events WHERE requested_at>?""", (row["sensor_id"], now - 60)).fetchone()
            if total >= 20 or sensor_total >= 5:
                raise APIError(429, "RATE_LIMITED", "테스트 알림 한도에 도달했습니다. 잠시 후 다시 시도해 주세요.")
            db.execute("INSERT INTO push_test_events VALUES (?,?)", (row["sensor_id"], now))
            db.execute("UPDATE phones SET last_test=? WHERE installation_id=?", (now, row["installation_id"]))
            store.queue(db, row["installation_id"], "test", now)
        return {"queued": True}

    app.add_middleware(APIRequestLimits, clock=clock)
    app.include_router(api)
    frontend = Path(__file__).resolve().parents[1] / "Frontend" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
