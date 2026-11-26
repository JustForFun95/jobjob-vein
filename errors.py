"""公共错误类别与错误码。

类别取自 contracts/03 第 6 节，不新增类别；HTTP 状态码取自模块文档 4.5。
"""

from __future__ import annotations

from typing import Any

# contracts/03 第 6 节的公共类别 -> HTTP 状态码（模块文档 4.5）
CATEGORY_STATUS: dict[str, int] = {
    "参数错误": 400,
    "无权限": 403,
    "未找到": 404,
    "内容已变": 409,
    "不支持": 422,
    "处理失败": 422,
    "渠道失败": 502,
    "保存失败": 500,
}


class ApiError(Exception):
    """业务错误。所有对外错误都经此处转换成统一响应体。"""

    def __init__(
        self,
        category: str,
        error_code: str,
        message: str,
        object_id: str | None = None,
        http_status: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        if category not in CATEGORY_STATUS:
            raise ValueError(f"未知错误类别: {category}")
        self.category = category
        self.error_code = error_code
        self.message = message
        self.object_id = object_id
        self.http_status = http_status or CATEGORY_STATUS[category]
        self.detail = detail

    def to_body(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "category": self.category,
            "error_code": self.error_code,
            "message": self.message,
            "object_id": self.object_id,
        }
        if self.detail is not None:
            error["detail"] = self.detail
        return {"data": None, "error": error}


def invalid_argument(message: str, object_id: str | None = None, detail: Any = None) -> ApiError:
    return ApiError("参数错误", "invalid_argument", message, object_id, detail=detail)


def missing_field(field: str, object_id: str | None = None) -> ApiError:
    return ApiError("参数错误", "missing_field", f"缺少必填字段: {field}", object_id)


def id_reused(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("参数错误", "id_reused", message, object_id)


def idempotency_mismatch(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("参数错误", "idempotency_mismatch", message, object_id)


def not_found(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("未找到", "not_found", message, object_id)


def unsupported(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("不支持", "unsupported", message, object_id)


def stale_content(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("内容已变", "stale_content", message, object_id)


def cursor_invalid(message: str = "游标已失效，需要重新全量核对") -> ApiError:
    return ApiError("内容已变", "cursor_invalid", message)


def channel_failed(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("渠道失败", "channel_failed", message, object_id)


def channel_mismatch(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("参数错误", "channel_mismatch", message, object_id)


def too_large(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("参数错误", "too_large", message, object_id)


def processing_failed(message: str, object_id: str | None = None, detail: Any = None) -> ApiError:
    return ApiError("处理失败", "processing_failed", message, object_id, detail=detail)


def save_failed(message: str, object_id: str | None = None) -> ApiError:
    return ApiError("保存失败", "save_failed", message, object_id)
