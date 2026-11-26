"""不可覆盖的原件文件保存与读取（SRC-INV-101）。

一次取得 = 一份有独立时间的证据。文件先完整写入并校验 SHA-256，再原子改名
为最终原件；随后在短事务里登记 raw_artifacts 与 pending 结果。同内容再次
取得仍生成新 ID，不复用旧原件的时间。

对齐 02 规格 1.1.1：表字段为 artifact_id/mime_type/storage_path（相对路径），
原件落在数据目录下的 artifacts/<artifact_id>/original。本模块不再保存 issues
或提取缓存——那些属于来源记录或已从 02 移除。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from ..normalization import sha256_hex, utc_now_iso
from .database import Database

DEFAULT_EXTENSIONS = {
    "text": "txt",
    "html": "html",
    "image": "bin",
    "email": "eml",
    "file": "bin",
}

# 只接受这些扩展名，避免落入可执行后缀
ALLOWED_EXTENSIONS = {
    "txt", "md", "html", "htm", "eml", "json", "xml", "csv",
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "pdf",
    "doc", "docx", "rtf", "bin",
}


class ArtifactStore:
    def __init__(self, db: Database, root: Path) -> None:
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- 写入 ---------------------------------------------------------------

    def save(
        self,
        content_type: str,
        payload: bytes,
        *,
        mime_type: str | None = None,
        captured_at: str | None = None,
        extension: str | None = None,
    ) -> dict[str, Any]:
        if content_type not in DEFAULT_EXTENSIONS:
            raise ValueError(f"不支持的原件类型: {content_type}")
        ext = (extension or DEFAULT_EXTENSIONS[content_type]).lower().lstrip(".")
        if ext not in ALLOWED_EXTENSIONS:
            ext = DEFAULT_EXTENSIONS[content_type]

        artifact_id = f"art_{uuid.uuid4().hex}"
        relative = f"artifacts/{artifact_id}/original"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        # 完整写入临时文件后原子改名
        tmp = target.with_name(target.name + ".part")
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)

        digest = sha256_hex(payload)
        row = {
            "artifact_id": artifact_id,
            "content_type": content_type,
            "mime_type": mime_type or self._default_mime(content_type, ext),
            "storage_path": relative,
            "sha256": digest,
            "byte_size": len(payload),
            "captured_at": captured_at,
            "created_at": utc_now_iso(),
        }
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO raw_artifacts (artifact_id, content_type, mime_type, storage_path,"
                " sha256, byte_size, captured_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["artifact_id"],
                    row["content_type"],
                    row["mime_type"],
                    row["storage_path"],
                    digest,
                    len(payload),
                    captured_at,
                    row["created_at"],
                ),
            )
        return row

    @staticmethod
    def _default_mime(content_type: str, ext: str) -> str:
        if content_type == "html":
            return "text/html"
        if content_type == "image":
            return {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
                "bmp": "image/bmp",
            }.get(ext, "application/octet-stream")
        if content_type == "email":
            return "message/rfc822"
        if content_type == "text":
            return "text/plain"
        return "application/octet-stream"

    # -- 读取 ---------------------------------------------------------------

    def row(self, artifact_id: str) -> dict[str, Any] | None:
        found = self.db.query_one("SELECT * FROM raw_artifacts WHERE artifact_id = ?", (artifact_id,))
        return dict(found) if found else None

    def path_of(self, artifact_id: str) -> Path | None:
        row = self.row(artifact_id)
        if row is None:
            return None
        return self.root / row["storage_path"]

    def read(self, artifact_id: str) -> tuple[bytes, dict[str, Any]] | None:
        row = self.row(artifact_id)
        if row is None:
            return None
        path = self.root / row["storage_path"]
        if not path.is_file():
            return None
        data = path.read_bytes()
        if sha256_hex(data) != row["sha256"]:
            raise IOError(f"原件哈希不一致: {artifact_id}")
        return data, row

    def text(self, artifact_id: str) -> str | None:
        loaded = self.read(artifact_id)
        if loaded is None:
            return None
        return loaded[0].decode("utf-8", errors="replace")

    # -- 遗留文件扫描（只列不删） -------------------------------------------

    def scan_leftovers(self, known_paths: set[str]) -> list[str]:
        """列出未登记的遗留文件；只列出，不自动删除（SRC-R-114）。"""
        leftovers: list[str] = []
        for child in sorted(self.root.glob("artifacts/*/original")):
            relative = child.relative_to(self.root).as_posix()
            if relative not in known_paths:
                leftovers.append(relative)
        return leftovers
