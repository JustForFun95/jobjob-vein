"""接收已有文字、截图、文件和邮件（import 任务的材料）。

已有资料不先搜索渠道，也不因交互入口变成另一个渠道：微信截图导入后 channel 仍是微信相关渠道。

对齐 02 规格 1.1.1：import 的材料产出 AdapterOutput（entity_type=material），
原文就是 artifact_bytes，不做任何语义提取。材料是否与渠道语义相符留给 03/04。
"""

from __future__ import annotations

from typing import Any

from .base import AdapterError, AdapterOutput, ENTITY_MATERIAL, CTX_USER_MATERIAL

ALLOWED_TYPES = ("text", "html", "image", "email", "file")
CONTENT_TYPES = {
    "text": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "email": "message/rfc822",
    "image": "application/octet-stream",
    "file": "application/octet-stream",
}


class ManualAdapter:
    channel = "manual"

    def capabilities(self) -> dict[str, Any]:
        return {
            "channel": "manual",
            "ready": True,
            "actions": ["import"],
            "criteria_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "scope_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "default_scope": {},
            "limits": {},
            "prerequisites": [],
            "normalization_version": "generic-1",
            "note": "已有资料通过 import 操作提交，不搜索渠道",
        }

    def normalize_item(self, item: Any, index: int) -> dict[str, Any]:
        """校验并规范化一份 import 材料；返回规范化字典（不含字节，字节由调用方提供）。"""
        if not isinstance(item, dict):
            raise AdapterError(f"第 {index} 项不是对象", kind="invalid_argument")
        channel = item.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            raise AdapterError(f"第 {index} 项缺少 channel", kind="missing_field")
        raw = item.get("raw_content")
        if not isinstance(raw, dict):
            raise AdapterError(f"第 {index} 项缺少 raw_content", kind="missing_field")
        content_type = raw.get("type")
        if content_type not in ALLOWED_TYPES:
            raise AdapterError(f"第 {index} 项 raw_content.type 不支持: {content_type}", kind="invalid_argument")
        has_text = "text" in raw
        has_file = "file_ref" in raw
        if has_text and has_file:
            raise AdapterError(f"第 {index} 项 text 与 file_ref 必须二选一", kind="invalid_argument")
        if not has_text and not has_file:
            raise AdapterError(f"第 {index} 项需要 text 或 file_ref", kind="missing_field")
        if has_text and not isinstance(raw["text"], str):
            raise AdapterError(f"第 {index} 项 text 必须是字符串", kind="invalid_argument")
        if has_file and not isinstance(raw["file_ref"], str):
            raise AdapterError(f"第 {index} 项 file_ref 必须是字符串", kind="invalid_argument")
        if content_type == "file" and has_text and not has_file:
            raise AdapterError(f"第 {index} 项 file 类型必须使用 file_ref", kind="invalid_argument")

        source_ref = item.get("source_ref")
        if source_ref is not None and not isinstance(source_ref, str):
            raise AdapterError(f"第 {index} 项 source_ref 必须是字符串", kind="invalid_argument")
        issues = item.get("issues")
        if issues is not None and not isinstance(issues, list):
            raise AdapterError(f"第 {index} 项 issues 必须是数组", kind="invalid_argument")
        origin = item.get("ingest_origin") or "user_task"
        if origin not in ("user_task", "admin_preload", "manual_import"):
            raise AdapterError(f"第 {index} 项 ingest_origin 非法: {origin}", kind="invalid_argument")
        acquired_at = item.get("acquired_at")
        if acquired_at is not None and not isinstance(acquired_at, str):
            raise AdapterError(f"第 {index} 项 acquired_at 必须是字符串", kind="invalid_argument")

        return {
            "channel": channel.strip(),
            "source_ref": source_ref.strip() if isinstance(source_ref, str) and source_ref.strip() else None,
            "content_type": content_type,
            "text": raw.get("text"),
            "file_ref": raw.get("file_ref"),
            "acquired_at": acquired_at,
            "issues": issues or [],
            "ingest_origin": origin,
        }

    def to_output(
        self,
        normalized: dict[str, Any],
        payload: bytes,
        mime_type: str | None = None,
        source_url: str | None = None,
    ) -> AdapterOutput:
        return AdapterOutput(
            channel=normalized["channel"],
            acquisition_context=CTX_USER_MATERIAL,
            entity_type=ENTITY_MATERIAL,
            artifact_bytes=payload,
            mime_type=mime_type or CONTENT_TYPES.get(normalized["content_type"], "application/octet-stream"),
            captured_at=normalized["acquired_at"],
            field_names=[],
            observed_fields={},
            source_ref=normalized["source_ref"],
            source_url=source_url,
            identity_key=None,
            issues=[
                {"code": "unverified", "message": "材料尚未回到渠道核对", "field": None}
            ],
        )
