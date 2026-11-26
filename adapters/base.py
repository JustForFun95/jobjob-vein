"""适配协议与能力声明。

对齐 02 规格 1.1.1 第 6.3 节适配器公开输出（版本 1.1）：

    channel, acquisition_context, source_ref/company_ref/source_url,
    identity_key, artifact_bytes, mime_type, captured_at, entity_type,
    field_names, observed_fields, issues

observed_fields 只含 *_raw 字段与 extra_raw_json，不得用模型填充推测值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol

# entity_type 取值（与 collection_results 的 result 处理对象一致）
ENTITY_JOB = "job"
ENTITY_COMPANY = "company"
ENTITY_MATERIAL = "material"
ENTITY_PAGE = "page"

# acquisition_context 取值（SRC 6.2 任务类型约束）
CTX_JOB_SEARCH = "job_search"
CTX_JOB_DETAIL = "job_detail"
CTX_COMPANY_PROFILE = "company_profile"
CTX_COMPANY_JOBS = "company_jobs"
CTX_USER_LINK = "user_link"
CTX_USER_MATERIAL = "user_material"
CTX_MANUAL_IMPORT = "manual_import"


@dataclass
class AdapterOutput:
    """适配器取回一份页面/卡片后的标准化输出。

    observed_fields 是"原样读取的槽位"，键只允许 *_raw 与 extra_raw_json；
    缺失值为 null。artifact_bytes 是本次取得页面的原始字节，由存储层生成
    artifact_id。
    """

    channel: str
    acquisition_context: str
    entity_type: str
    artifact_bytes: bytes
    mime_type: str
    captured_at: str
    field_names: list[str] = field(default_factory=list)
    observed_fields: dict[str, Any] = field(default_factory=dict)
    source_ref: str | None = None
    company_ref: str | None = None
    source_url: str | None = None
    identity_key: str | None = None
    locator: str | None = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    page_summary: dict[str, Any] | None = None
    # 02 规格 4.4：用户材料触发关系与核对声明。由调用方在冻结请求时填入，
    # 采集器不得自行宣称「已核对」之外的含义。
    input_artifact_id: str | None = None
    verification: str = "channel_checked"
    channel_basis: str | None = None
    # 由摄入层按本次取得的原件构造，写入 payload 的 evidence_refs；
    # 角色由 entity_type + acquisition_context 决定，不在适配器里猜测。
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Budget:
    deadline: float
    max_pages: int
    max_candidates: int
    pages_read: int = 0
    candidates_read: int = 0
    capture_complete: bool = False
    stop_reason: str | None = None
    issues: list[str] = field(default_factory=list)

    def time_left(self) -> float:
        import time

        return self.deadline - time.monotonic()

    def exhausted(self) -> bool:
        if self.stop_reason:
            return True
        if self.time_left() <= 0:
            self.stop_reason = "timeout"
            return True
        if self.candidates_read >= self.max_candidates:
            self.stop_reason = "limit_reached"
            return True
        if self.pages_read >= self.max_pages:
            self.stop_reason = "limit_reached"
            return True
        return False


class AdapterError(Exception):
    def __init__(self, message: str, *, kind: str = "channel_failed") -> None:
        super().__init__(message)
        self.kind = kind


class Adapter(Protocol):
    channel: str

    def capabilities(self) -> dict[str, Any]: ...

    def acquire(
        self,
        criteria: dict[str, Any],
        scope: dict[str, Any],
        budget: Budget,
        stop_requested,
    ) -> Iterator[AdapterOutput]: ...

    def acquire_company(self, company_ref: str, limit: int) -> list[AdapterOutput]: ...
