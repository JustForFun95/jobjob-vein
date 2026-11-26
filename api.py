"""HTTP 入参、回环访问限制和响应转换。

首版仅本机调用：不实现登录、令牌或角色。回环与跨站限制由程序执行，
不因为省事而放开。对齐 02 规格 1.1.1 第 6 节接口清单。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .errors import ApiError, invalid_argument, not_found
from .service import JobjobveinService

PREFIX = "/api/jobjobvein"
WEB_DIR = Path(__file__).resolve().parent / "web"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def _ok(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse({"data": data, "error": None}, status_code=status_code)


def create_app(service: JobjobveinService) -> FastAPI:
    app = FastAPI(title="jobjobvein", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    @app.middleware("http")
    async def guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        client = request.client.host if request.client else ""
        if client not in LOOPBACK:
            return JSONResponse(
                {
                    "data": None,
                    "error": {
                        "category": "无权限",
                        "error_code": "permission_denied",
                        "message": "首版只允许本机回环访问",
                        "object_id": None,
                    },
                },
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin:
            host = origin.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
            if host not in ("127.0.0.1", "localhost", "::1"):
                return JSONResponse(
                    {
                        "data": None,
                        "error": {
                            "category": "无权限",
                            "error_code": "permission_denied",
                            "message": "拒绝跨站写入",
                            "object_id": None,
                        },
                    },
                    status_code=403,
                )
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
            if content_type not in ("application/json", "multipart/form-data"):
                return JSONResponse(
                    {
                        "data": None,
                        "error": {
                            "category": "参数错误",
                            "error_code": "invalid_argument",
                            "message": "写操作必须使用 application/json 或 multipart/form-data",
                            "object_id": None,
                        },
                    },
                    status_code=400,
                )
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(exc.to_body(), status_code=exc.http_status)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {
                "data": None,
                "error": {
                    "category": "保存失败",
                    "error_code": "save_failed",
                    "message": f"未预期错误: {exc}",
                    "object_id": None,
                },
            },
            status_code=500,
        )

    # ------------------------------------------------------------------ 能力

    @app.get(PREFIX + "/capabilities")
    def capabilities(fresh: bool = False):  # type: ignore[no-untyped-def]
        return _ok(service.capabilities(fresh=fresh))

    @app.post(PREFIX + "/browser/start")
    def browser_start():  # type: ignore[no-untyped-def]
        return _ok(service.start_debug_browser())

    # ------------------------------------------------------------------ 原件

    @app.post(PREFIX + "/artifacts")
    async def upload_artifact(request: Request):  # type: ignore[no-untyped-def]
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise invalid_argument("需要 multipart 文件字段 file")
        payload = await upload.read()
        content_type = form.get("content_type")
        if not content_type:
            raise invalid_argument("需要 content_type")
        acquired_at = form.get("acquired_at") or None
        filename = getattr(upload, "filename", None)
        data = service.save_upload(payload, str(content_type), acquired_at, filename)
        return _ok(data, status_code=201)

    @app.get(PREFIX + "/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str):  # type: ignore[no-untyped-def]
        payload, row = service.get_artifact(artifact_id)
        media_types = {
            "text": "text/plain; charset=utf-8",
            "email": "text/plain; charset=utf-8",
            "html": "text/plain; charset=utf-8",
            "image": _guess_image_type(row["storage_path"]),
            "file": "application/octet-stream",
        }
        headers = {"X-Content-Type-Options": "nosniff"}
        if row["content_type"] in ("image", "file"):
            disposition = "inline" if row["content_type"] == "image" else "attachment"
            headers["Content-Disposition"] = f'{disposition}; filename="{Path(row["storage_path"]).name}"'
        return Response(
            content=payload,
            media_type=media_types.get(row["content_type"], "application/octet-stream"),
            headers=headers,
        )

    # ------------------------------------------------------------------ 任务

    @app.post(PREFIX + "/tasks")
    async def create_task(request: Request):  # type: ignore[no-untyped-def]
        body = await _json(request)
        task_type = body.get("task_type")
        if not isinstance(task_type, str) or not task_type:
            raise invalid_argument("缺少 task_type")
        data = service.register_task(task_type, body)
        return _ok(data, status_code=201)

    @app.post(PREFIX + "/tasks/{task_id}/run")
    async def run_task(task_id: str, request: Request):  # type: ignore[no-untyped-def]
        await _optional_json(request)
        data = service.run_task(task_id)
        status_code = 202 if data["status"] in ("ready", "running") else 200
        return _ok(data, status_code=status_code)

    @app.post(PREFIX + "/tasks/{task_id}/stop")
    async def stop_task(task_id: str, request: Request):  # type: ignore[no-untyped-def]
        await _optional_json(request)
        return _ok(service.stop_task(task_id))

    @app.get(PREFIX + "/tasks/{task_id}")
    async def get_task(task_id: str):  # type: ignore[no-untyped-def]
        return _ok(service.get_task(task_id))

    @app.get(PREFIX + "/tasks/{task_id}/results")
    async def get_task_results(task_id: str, request: Request):  # type: ignore[no-untyped-def]
        q = dict(request.query_params)
        data = service.get_task_results(
            task_id,
            company_source_id=q.get("company_source_id"),
            source_id=q.get("source_id"),
            acquisition_context=q.get("acquisition_context"),
            page=int(q.get("page", 1)),
            page_size=int(q.get("page_size", 20)),
        )
        return _ok(data)

    # ------------------------------------------------------------------ 来源

    @app.get(PREFIX + "/sources")
    async def list_sources(request: Request):  # type: ignore[no-untyped-def]
        return _ok(service.list_sources(dict(request.query_params)))

    @app.get(PREFIX + "/sources/{source_id}")
    async def get_source(source_id: str):  # type: ignore[no-untyped-def]
        return _ok(service.get_source(source_id))

    @app.patch(PREFIX + "/sources/{source_id}/status")
    async def patch_status(source_id: str, request: Request):  # type: ignore[no-untyped-def]
        body = await _json(request)
        data = service.patch_status(source_id, body.get("status"), body.get("expected_revision"))
        return _ok(data)

    # ------------------------------------------------------------------ 公司

    @app.get(PREFIX + "/company-sources")
    async def list_companies(request: Request):  # type: ignore[no-untyped-def]
        return _ok(service.list_companies(dict(request.query_params)))

    @app.get(PREFIX + "/company-sources/{company_source_id}")
    async def get_company(company_source_id: str):  # type: ignore[no-untyped-def]
        return _ok(service.get_company(company_source_id))

    # ------------------------------------------------------------------ 冲突

    @app.get(PREFIX + "/conflicts")
    async def list_conflicts(request: Request):  # type: ignore[no-untyped-def]
        return _ok(service.list_conflicts(dict(request.query_params)))

    @app.get(PREFIX + "/conflicts/{conflict_id}")
    async def get_conflict(conflict_id: str):  # type: ignore[no-untyped-def]
        return _ok(service.get_conflict(conflict_id))

    @app.post(PREFIX + "/conflicts/{conflict_id}/decision")
    async def decide(conflict_id: str, request: Request):  # type: ignore[no-untyped-def]
        body = await _json(request)
        data = service.decide(
            conflict_id,
            body.get("decision"),
            body.get("expected_revision"),
            body.get("fields"),
        )
        return _ok(data)

    # ------------------------------------------------------------------ 页面

    @app.get("/")
    async def index():  # type: ignore[no-untyped-def]
        page = WEB_DIR / "index.html"
        if not page.is_file():
            raise not_found("管理员页面缺失")
        return FileResponse(page)

    return app


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        raise invalid_argument("请求体不能为空")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise invalid_argument("请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise invalid_argument("请求体必须是 JSON 对象")
    return body


async def _optional_json(request: Request) -> None:
    raw = await request.body()
    if not raw:
        return
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise invalid_argument("请求体不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise invalid_argument("请求体必须是 JSON 对象")


def _guess_image_type(storage_path: str) -> str:
    suffix = Path(storage_path).suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(suffix, "application/octet-stream")
