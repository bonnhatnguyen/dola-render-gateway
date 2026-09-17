"""Dola Pool: OpenAI-compatible Video API (FastAPI) and Admin Dashboard.

Endpoints (Asynchronous 2-stage):
POST /v1/videos/generations -> Create task (status=queued)
GET  /v1/videos/<id>         -> Query task status (queued/processing/completed/failed)
GET  /videos/<file>          -> Static video download server

Admin Dashboard: GET / -> web/index.html; Admin API /api/admin/*
"""
import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        import uvicorn.loops.asyncio
        uvicorn.loops.asyncio.asyncio_loop_factory = lambda use_subprocess=False: asyncio.ProactorEventLoop
    except Exception:
        pass
import hashlib
import json
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
from add_account import add_account_flow
from browser_pool import AllAccountsLimitedError, AllAccountsQuotaBlockedError, BrowserPool
from media import download_reference_images, validate_reference_urls
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="dola-pool", version="0.4.0")

store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")

# Background jobs (add/verify), in-memory
JOBS: dict[str, dict] = {}
BATCH_JOBS: dict[str, dict] = {}
ACTIVE_BATCH_ID: str | None = None

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
SUPPORTED_DURATIONS = (10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConcurrencyLimiter:
    """Concurrency limits per API Key; 0 = unlimited."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()


# ===== Authentication =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "Anonymous",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"Env Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _auth(authorization):
    """Returns client policy for caller; empty key enables dev mode."""
    if not config.API_KEYS and not store.has_enabled_keys():
        return _anonymous_client()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "missing bearer token")
    if key in config.API_KEYS:
        return _env_client(key)
    record = store.get_key(key)
    if not record or not store.is_key_valid(key):
        raise HTTPException(401, "invalid api key")
    store.touch_key(key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "Unnamed Client",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
    }


def _admin_auth(x_admin_key: str | None):
    if not config.ADMIN_KEY:
        return
    if x_admin_key != config.ADMIN_KEY:
        raise HTTPException(401, "invalid admin key")


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, "allowed_durations must be an array of 10, 15, or 30")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(422, "allowed_durations must contain at least one of 10, 15, 30")
    return normalized


# ===== Client API =====


class VideoGenRequest(BaseModel):
    model: str = "seedance-2.5"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=10, le=30)
    # Accepts durations: 10, 15, 30 seconds.
    reference_images: list[str] = Field(default_factory=list)


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client):
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_root = None
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        store.update(task_id, status="processing", started_at=time.time())

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        reference_root, reference_paths = await download_reference_images(
            reference_images or [], task_id)
        result = await pool.generate_video(
            prompt, ratio, duration, model,
            on_conversation_id=on_conversation_id, on_poll=on_poll,
            reference_image_paths=reference_paths)
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError) as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     failure_code="429", finished_at=time.time())
    except Exception as e:
        err_msg = str(e).strip() or repr(e)
        store.update(task_id, status="failed", error=err_msg[:500],
                     finished_at=time.time())
    finally:
        if reference_root:
            shutil.rmtree(reference_root, ignore_errors=True)
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row: dict):
    task_id = row["id"]
    deadline = row.get("deadline_at") or (
        time.time() + (1800 if row.get("duration") == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll)
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     finished_at=time.time())
    finally:
        if acquired:
            await key_limiter.release(api_key_hash)


def _task_client(row: dict) -> dict:
    """Restores client context from task snapshot."""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "Historical Task",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


@app.on_event("startup")
async def resume_incomplete_tasks():
    """Recovers accepted sessions on startup and requeues pending tasks."""
    for row in store.recoverable_tasks():
        asyncio.create_task(_resume_task(row))
    for row in store.recoverable_queued_tasks():
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        asyncio.create_task(_run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
        ))


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(req: VideoGenRequest, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "Currently supports durations of 10s, 15s, and 30s")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"Current API Key is not allowed to generate {duration}s videos")
    model_key = req.model.lower().replace("-", "_")
    if model_key not in (
        "seedance_2.0", "seedance_2.5", "seedance_v2.0", "seedance_v2.5",
        "seedance_20", "seedance_25", "seedance_v20", "seedance_v25",
    ):
        raise HTTPException(422, "Supported models are seedance-2.0 and seedance-2.5")
    try:
        reference_images = await validate_reference_urls(req.reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Queue task when accounts are busy; reject only when pool is fully exhausted.
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "Rate limited: All accounts reached Dola daily video limit, please try again tomorrow")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "Insufficient credits: All accounts lack points, waiting for refresh")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    try:
        store.create(
            task_id,
            req.model,
            req.prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    asyncio.create_task(_run_task(
        task_id, req.model, req.prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(id=task_id, status="queued", model=req.model, prompt=req.prompt)


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=row["video_url"], error=row["error"],
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "accounts": pool.account_status(),
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


# ===== Unwatermarked Video Extraction API =====


class ExtractCleanRequest(BaseModel):
    url_or_id: str
    output_name: str | None = None


@app.post("/api/extract_unwatermarked")
async def api_extract_unwatermarked(body: ExtractCleanRequest):
    cid_or_url = body.url_or_id.strip()
    if not cid_or_url:
        raise HTTPException(400, "Vui lòng nhập link chat hoặc ID đoạn chat Dola!")
    try:
        from unwatermark import download_clean_video
        res = await download_clean_video(cid_or_url, output_name=body.output_name)
        return res
    except Exception as e:
        raise HTTPException(400, f"Trích xuất video không watermark thất bại: {str(e)}")


@app.get("/api/downloads")
async def api_get_downloads():
    from unwatermark import list_downloaded_videos
    return {"files": list_downloaded_videos()}


# ===== Admin Dashboard API =====


class AdminLogin(BaseModel):
    key: str


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None


class AccountAdd(BaseModel):
    name: str
    email: str
    password: str
    totp: str


class CookieImportBody(BaseModel):
    name: str = "acc1"
    data: dict | list | str


class LoginBrowserBody(BaseModel):
    name: str = "acc1"


class BulkLoginRequest(BaseModel):
    raw_text: str = ""
    prefix: str = "acc"
    start_num: int = 1
    delay_seconds: int = 2


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    if not config.ADMIN_KEY:
        return {"ok": True, "auth_required": False}
    if body.key == config.ADMIN_KEY:
        return {"ok": True, "auth_required": True}
    raise HTTPException(401, "wrong admin key")


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"accounts": pool.list_accounts()}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        pool.delete_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        ok = await pool.verify_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": ok}


async def _run_add_job(name: str, email: str, password: str, totp: str):
    JOBS[name] = {
        "kind": "add",
        "status": "running",
        "error": "",
        "started_at": time.time(),
        "message": "Đang mở trình duyệt và tự động điền thông tin đăng nhập Google...",
    }
    try:
        import importlib
        import add_account
        importlib.reload(add_account)
        await add_account.add_account_flow(name, email, password, totp)
        pool.set_email(name, email)
        pool.set_login_status(name, True)
        JOBS[name] = {
            **JOBS[name],
            "status": "success",
            "message": f"Tài khoản {name} đã tự động đăng nhập thành công!",
        }
    except Exception as e:
        JOBS[name] = {
            **JOBS[name],
            "status": "failed",
            "error": str(e)[:300],
        }


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "account exists")
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
    asyncio.create_task(_run_add_job(body.name, body.email, body.password, body.totp))
    return {"ok": True, "job": "running"}


@app.post("/api/admin/accounts/import_cookie")
async def admin_account_import_cookie(body: CookieImportBody, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name (use letters, numbers, underscores)")
    from import_cookie import import_account_from_data
    try:
        res = await import_account_from_data(body.name, body.data)
        return res
    except Exception as e:
        raise HTTPException(400, f"Import failed: {str(e)}")


async def _run_headful_login_job(name: str):
    JOBS[name] = {
        "kind": "headful_login",
        "status": "running",
        "error": "",
        "message": "Đang khởi chạy trình duyệt Chrome...",
        "started_at": time.time(),
    }
    profile_dir = Path("accounts") / name
    profile_dir.mkdir(parents=True, exist_ok=True)

    from patchright.async_api import async_playwright
    from browser import LAUNCH_ARGS

    try:
        async with async_playwright() as p:
            kwargs = {
                "headless": False,
                "args": LAUNCH_ARGS,
                "locale": "ja-JP",
                "timezone_id": "Asia/Tokyo",
            }
            if config.PROXY:
                kwargs["proxy"] = {"server": config.PROXY}

            context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                JOBS[name]["message"] = "Đang mở trang Dola, vui lòng đăng nhập Google trên cửa sổ Chrome..."
                try:
                    await page.goto("https://www.dola.com/chat", timeout=60000)
                except Exception as e:
                    print(f"[{name}] goto note: {e}", flush=True)

                # Check if already logged in
                cookies = await context.cookies("https://www.dola.com")
                if any(c["name"] in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies):
                    pool._ensure_meta(name)
                    pool.set_email(name, f"google_{name}")
                    pool.set_login_status(name, True)
                    JOBS[name] = {
                        **JOBS[name],
                        "status": "success",
                        "message": "Tài khoản đã có session đăng nhập hợp lệ!",
                    }
                    await asyncio.sleep(2)
                    return

                # Dismiss cookie banner and click login entry if needed
                try:
                    for sel in [
                        "button:has-text('OK')",
                        "[role='button']:has-text('OK')",
                        "text=OK",
                        "button:has-text('Accept')",
                        "button:has-text('同意')",
                    ]:
                        cand = page.locator(sel).first
                        if await cand.count() and await cand.is_visible():
                            await cand.click(timeout=2000)
                            break
                except Exception:
                    pass

                try:
                    headful_google_candidates = [
                        "button:has-text('Googleで続ける')",
                        "[role='button']:has-text('Googleで続ける')",
                        "text=Googleで続ける",
                        "button:has-text('Continue with Google')",
                        "[role='button']:has-text('Continue with Google')",
                        "text=Continue with Google",
                        "button:has-text('Google')",
                        "[role='button']:has-text('Google')",
                    ]

                    async def _find_headful_google():
                        for sel in headful_google_candidates:
                            try:
                                cand = page.locator(sel).first
                                if await cand.count() and await cand.is_visible():
                                    return cand
                            except Exception:
                                continue
                        return None

                    google_btn = await _find_headful_google()
                    if not google_btn:
                        for sel in [
                            "button:has-text('ログイン')",
                            "[role='button']:has-text('ログイン')",
                            "text=ログイン",
                            "button:has-text('Log in')",
                            "[role='button']:has-text('Log in')",
                            "text=Log in",
                            "button:has-text('Sign in')",
                            "[role='button']:has-text('Sign in')",
                            "text=Sign in",
                            "button:has-text('Đăng nhập')",
                            "[role='button']:has-text('Đăng nhập')",
                            "text=Đăng nhập",
                        ]:
                            try:
                                btn = page.locator(sel).first
                                if await btn.count() and await btn.is_visible():
                                    await btn.click(timeout=2000)
                                    break
                            except Exception:
                                continue
                        await page.wait_for_timeout(1200)
                        google_btn = await _find_headful_google()

                    if google_btn:
                        await google_btn.click(timeout=3000)
                except Exception:
                    pass

                # Monitor context cookies for up to 600s (10 min)
                logged_in = False
                for _ in range(300):
                    await asyncio.sleep(2)
                    if JOBS.get(name, {}).get("status") == "cancelled":
                        return
                    if not context.pages:
                        break
                    try:
                        cookies = await context.cookies("https://www.dola.com")
                        if any(c["name"] in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies):
                            logged_in = True
                            c_parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("value")]
                            try:
                                with open("cookies.txt", "a", encoding="utf-8") as f:
                                    f.write("; ".join(c_parts) + "\n")
                            except Exception:
                                pass
                            break
                    except Exception:
                        break

                if logged_in:
                    pool._ensure_meta(name)
                    pool.set_email(name, f"google_{name}")
                    pool.set_login_status(name, True)
                    JOBS[name] = {
                        **JOBS[name],
                        "status": "success",
                        "message": f"Đăng nhập thành công! Tài khoản {name} đã sẵn sàng trong Pool.",
                    }
                    await asyncio.sleep(3)
                else:
                    if JOBS.get(name, {}).get("status") == "cancelled":
                        return
                    JOBS[name] = {
                        **JOBS[name],
                        "status": "failed",
                        "error": "Trình duyệt bị đóng hoặc quá thời gian chờ (10 phút) trước khi nhận được session đăng nhập.",
                    }
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
    except Exception as e:
        JOBS[name] = {
            **JOBS[name],
            "status": "failed",
            "error": f"Lỗi khởi chạy trình duyệt: {str(e)[:300]}",
        }


@app.post("/api/admin/accounts/login-browser")
async def admin_account_login_browser(body: LoginBrowserBody, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name (use letters, numbers, underscores)")
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, f"Tiến trình cho tài khoản '{body.name}' đang chạy")
    asyncio.create_task(_run_headful_login_job(body.name))
    return {"ok": True, "status": "running", "account": body.name}


@app.get("/api/admin/accounts/login-browser/status")
async def admin_account_login_browser_status(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    job = JOBS.get(name)
    if not job:
        return {"status": "none", "account": name}
    return {
        "status": job.get("status", "none"),
        "account": name,
        "message": job.get("message", ""),
        "error": job.get("error", ""),
        "started_at": job.get("started_at", 0),
    }


@app.post("/api/admin/accounts/login-browser/cancel")
async def admin_account_login_browser_cancel(body: LoginBrowserBody, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if body.name in JOBS and JOBS[body.name].get("status") == "running":
        JOBS[body.name]["status"] = "cancelled"
        JOBS[body.name]["message"] = "Đã hủy tiến trình đăng nhập."
    return {"ok": True}


def parse_bulk_accounts(raw_text: str, default_prefix: str = "acc", start_num: int = 1, existing_names: set[str] | None = None) -> list[dict]:
    if existing_names is None:
        existing_names = set(pool.accounts)
    used_names = set(existing_names)
    results = []

    clean_prefix = re.sub(r"[^A-Za-z0-9_-]", "", default_prefix) or "acc"
    counter = max(1, start_num)

    def get_next_name():
        nonlocal counter
        while f"{clean_prefix}{counter}" in used_names:
            counter += 1
        name = f"{clean_prefix}{counter}"
        used_names.add(name)
        counter += 1
        return name

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        parts = []
        if "----" in line:
            parts = [p.strip() for p in line.split("----")]
        elif "|" in line:
            parts = [p.strip() for p in line.split("|")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        elif ":::" in line:
            parts = [p.strip() for p in line.split(":::")]
        elif ":" in line and len(line.split(":")) >= 3:
            parts = [p.strip() for p in line.split(":")]
        else:
            parts = [p.strip() for p in line.split() if p.strip()]

        if not parts:
            continue

        name = ""
        email = ""
        password = ""
        totp = ""

        # Case 1: First part contains '@' -> It's the email
        if "@" in parts[0]:
            email = parts[0]
            password = parts[1] if len(parts) > 1 else ""
            totp = parts[2] if len(parts) > 2 else ""
            name = get_next_name()
        # Case 2: First part is a profile name, second part is email
        elif len(parts) > 1 and "@" in parts[1]:
            cand_name = re.sub(r"[^A-Za-z0-9_-]", "", parts[0])[:32]
            email = parts[1]
            password = parts[2] if len(parts) > 2 else ""
            totp = parts[3] if len(parts) > 3 else ""
            if cand_name and cand_name not in used_names:
                name = cand_name
                used_names.add(name)
            else:
                name = get_next_name()
        else:
            if len(parts) >= 2:
                email = parts[0]
                password = parts[1]
                totp = parts[2] if len(parts) > 2 else ""
                name = get_next_name()
            else:
                continue

        if email and password:
            results.append({
                "name": name,
                "email": email,
                "password": password,
                "totp": totp,
            })

    return results


def _sanitize_batch(batch: dict) -> dict:
    return {
        "id": batch.get("id", ""),
        "status": batch.get("status", "none"),
        "total": batch.get("total", 0),
        "completed_count": batch.get("completed_count", 0),
        "success_count": batch.get("success_count", 0),
        "failed_count": batch.get("failed_count", 0),
        "current_account": batch.get("current_account", ""),
        "current_index": batch.get("current_index", 0),
        "started_at": batch.get("started_at", 0),
        "finished_at": batch.get("finished_at"),
        "items": [
            {
                "name": it.get("name", ""),
                "email": it.get("email", ""),
                "has_totp": bool(it.get("totp")),
                "status": it.get("status", "pending"),
                "error": it.get("error", ""),
                "message": it.get("message", ""),
                "started_at": it.get("started_at", 0),
                "finished_at": it.get("finished_at", 0),
            }
            for it in batch.get("items", [])
        ],
    }


async def _run_bulk_login_worker(batch_id: str, delay_seconds: int = 2):
    global ACTIVE_BATCH_ID
    batch = BATCH_JOBS.get(batch_id)
    if not batch:
        return

    import importlib
    import add_account

    try:
        for idx, item in enumerate(batch["items"]):
            if batch.get("status") == "cancelled":
                break

            batch["current_index"] = idx + 1
            batch["current_account"] = item["name"]
            item["status"] = "running"
            item["started_at"] = time.time()
            item["message"] = "Đang mở trình duyệt và đăng nhập Google..."

            JOBS[item["name"]] = {
                "kind": "bulk_add",
                "status": "running",
                "batch_id": batch_id,
                "error": "",
                "started_at": time.time(),
                "message": f"Đang đăng nhập hàng loạt ({idx + 1}/{batch['total']})...",
            }

            try:
                importlib.reload(add_account)
                await add_account.add_account_flow(
                    item["name"], item["email"], item["password"], item["totp"]
                )
                pool.set_email(item["name"], item["email"])
                pool.set_login_status(item["name"], True)
                item["status"] = "success"
                item["message"] = "Đăng nhập thành công!"
                item["finished_at"] = time.time()
                batch["success_count"] += 1
                if item["name"] in JOBS:
                    JOBS[item["name"]]["status"] = "success"
                    JOBS[item["name"]]["message"] = "Tài khoản đã đăng nhập thành công!"
            except Exception as e:
                err_msg = str(e)[:250]
                item["status"] = "failed"
                item["error"] = err_msg
                item["message"] = f"Lỗi: {err_msg}"
                item["finished_at"] = time.time()
                batch["failed_count"] += 1
                if item["name"] in JOBS:
                    JOBS[item["name"]]["status"] = "failed"
                    JOBS[item["name"]]["error"] = err_msg

            batch["completed_count"] = batch["success_count"] + batch["failed_count"]

            if batch.get("status") == "cancelled":
                break

            if idx < len(batch["items"]) - 1:
                await asyncio.sleep(max(1, delay_seconds))
    finally:
        if batch.get("status") == "cancelled":
            for rem in batch["items"]:
                if rem.get("status") == "pending":
                    rem["status"] = "cancelled"
                    rem["message"] = "Đã bị hủy bởi người dùng"
        else:
            batch["status"] = "completed"
        batch["finished_at"] = time.time()
        if ACTIVE_BATCH_ID == batch_id:
            ACTIVE_BATCH_ID = None


@app.post("/api/admin/accounts/bulk-parse")
async def admin_account_bulk_parse(body: BulkLoginRequest, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    parsed = parse_bulk_accounts(body.raw_text, body.prefix, body.start_num, set(pool.accounts))
    return {
        "ok": True,
        "total": len(parsed),
        "items": [
            {
                "name": it["name"],
                "email": it["email"],
                "has_totp": bool(it["totp"]),
            }
            for it in parsed
        ],
    }


@app.post("/api/admin/accounts/bulk-login", status_code=202)
async def admin_account_bulk_login(body: BulkLoginRequest, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    global ACTIVE_BATCH_ID
    if ACTIVE_BATCH_ID and BATCH_JOBS.get(ACTIVE_BATCH_ID, {}).get("status") == "running":
        raise HTTPException(409, "Một tiến trình đăng nhập hàng loạt khác đang chạy!")

    items = parse_bulk_accounts(body.raw_text, body.prefix, body.start_num, set(pool.accounts))
    if not items:
        raise HTTPException(400, "Không tìm thấy tài khoản hợp lệ nào trong nội dung dán vào.")

    batch_id = f"batch_{int(time.time())}"
    BATCH_JOBS[batch_id] = {
        "id": batch_id,
        "status": "running",
        "total": len(items),
        "completed_count": 0,
        "success_count": 0,
        "failed_count": 0,
        "current_account": "",
        "current_index": 0,
        "started_at": time.time(),
        "finished_at": None,
        "items": [
            {
                "name": it["name"],
                "email": it["email"],
                "password": it["password"],
                "totp": it["totp"],
                "status": "pending",
                "error": "",
                "message": "Chờ xử lý...",
                "started_at": 0,
                "finished_at": 0,
            }
            for it in items
        ],
    }
    ACTIVE_BATCH_ID = batch_id
    asyncio.create_task(_run_bulk_login_worker(batch_id, body.delay_seconds))
    return {"ok": True, "batch": _sanitize_batch(BATCH_JOBS[batch_id])}


@app.get("/api/admin/accounts/bulk-login/status")
async def admin_account_bulk_login_status(batch_id: str | None = None, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    target_id = batch_id or ACTIVE_BATCH_ID
    batch = None
    if target_id and target_id in BATCH_JOBS:
        batch = BATCH_JOBS[target_id]
    elif BATCH_JOBS:
        latest_key = list(BATCH_JOBS.keys())[-1]
        batch = BATCH_JOBS[latest_key]

    if not batch:
        return {"ok": True, "status": "none"}
    return {"ok": True, "batch": _sanitize_batch(batch)}


@app.post("/api/admin/accounts/bulk-login/cancel")
async def admin_account_bulk_login_cancel(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    global ACTIVE_BATCH_ID
    if ACTIVE_BATCH_ID and ACTIVE_BATCH_ID in BATCH_JOBS:
        BATCH_JOBS[ACTIVE_BATCH_ID]["status"] = "cancelled"
        return {"ok": True, "message": "Đã gửi lệnh hủy tiến trình hàng loạt."}
    return {"ok": True, "message": "Không có tiến trình hàng loạt nào đang chạy."}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": JOBS}


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = 50, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"tasks": store.recent_tasks(min(max(limit, 1), 200))}


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = store.stats()
    accs = pool.list_accounts()
    sched = [a for a in accs if a["scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if a["remaining"] > 0)
    st["busy_accounts"] = sum(1 for a in accs if a.get("busy"))
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    st["proxy"] = config.PROXY or "Direct / None"
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    keys = []
    for key in store.list_keys():
        usage = store.key_usage(store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {"keys": keys, "env_keys": len(config.API_KEYS)}


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    store.update_key(key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


@app.get("/")
@app.get("/web")
@app.get("/web/")
async def serve_index():
    index_path = Path("web/index.html")
    if not index_path.exists():
        raise HTTPException(404, "Frontend index.html not found")
    content = index_path.read_text(encoding="utf-8")
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


# Static assets fallback
app.mount("/static", StaticFiles(directory="web"), name="web_static")
app.mount("/", StaticFiles(directory="web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=config.HOST, port=config.PORT, reload=False)


