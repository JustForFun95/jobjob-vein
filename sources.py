"""同一来源的重采判定：原文一致 / 补全 / 冲突，以及快照合并。

对齐 02 规格 1.1.1 第 3.6、4.4、5.1：

- 比较对象是 *_raw 原文槽位，不是语义提取字段。
- SRC-R-108：正文只统一换行与外围空白，不改写、不删词、不做薪资/城市等价判断。
  因此没有"可证明等价自动采用"——原文相同则 unchanged，原文不同则进冲突由用户选。
- 字段级补全（旧值空、候选有值）不构成冲突，自动采用该字段，沿用原原件与 locator。
- 冲突快照 = {revision, fields, evidence_refs}；采用新候选时保存完整合并快照。

结果类型（collection_results.result_type）：
    source_created / company_created / unchanged / material_saved /
    conflict_pending / empty / failed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .normalization import normalize_text

# collection_results.result_type 取值
RESULT_SOURCE_CREATED = "source_created"
RESULT_COMPANY_CREATED = "company_created"
RESULT_UNCHANGED = "unchanged"
RESULT_MATERIAL_SAVED = "material_saved"
RESULT_CONFLICT_PENDING = "conflict_pending"
RESULT_EMPTY = "empty"
RESULT_FAILED = "failed"

# 冲突决策
DECISION_KEEP_OLD = "keep_old"
DECISION_KEEP_NEW = "keep_new"
DECISION_KEEP_MERGE = "keep_merge"  # 字段级：按 fields 映射逐字段选 old/new

# source_records / company_source_records 的 *_raw 槽位
SOURCE_RAW_FIELDS = (
    "title_raw", "company_name_raw", "location_raw", "salary_raw",
    "requirements_raw", "description_raw",
)
COMPANY_RAW_FIELDS = (
    "company_name_raw", "legal_name_raw", "address_raw", "established_raw",
    "registered_capital_raw", "scale_raw", "profile_raw", "business_info_raw",
    "jobs_page_raw", "jobs_reported_count_raw",
)


@dataclass
class Decision:
    kind: str
    reason: str = ""
    differences: dict[str, Any] = field(default_factory=dict)
    merged_snapshot: dict[str, Any] = field(default_factory=dict)
    keeps_current_artifact: bool = False
    issues: list[str] = field(default_factory=list)


def _is_blank(value: Any) -> bool:
    return value is None or value == ""


def normalize_raw_text(value: Any) -> str | None:
    """SRC-R-108：只统一换行与外围空白。不改写正文、不删词。"""
    if value is None:
        return None
    return normalize_text(str(value))


# 快照里的证据元数据：每次采集必然变化，不是岗位/公司内容，不参与差异判定。
SNAPSHOT_META_FIELDS = frozenset({
    "artifact_id", "captured_at", "issues_json", "locator", "company_source_id",
    "revision", "evidence_refs",
})


def normalize_field_keys(fields: dict[str, Any]) -> dict[str, Any]:
    """统一 extra 槽位键名：渠道侧写 extra_raw，落库列是 extra_raw_json。

    历史上两次采集分别用了两个键，直接比较会把同一份数据判成"一删一增"
    两个差异，刷出大量假冲突。比较与合并前一律归一到 extra_raw_json。
    """
    normalized = dict(fields)
    if "extra_raw" in normalized and "extra_raw_json" not in normalized:
        normalized["extra_raw_json"] = normalized.pop("extra_raw")
    return normalized


def compare_raw_fields(
    *,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    candidate_partial: bool = False,
) -> dict[str, dict[str, Any]]:
    """逐 *_raw 槽位比较，返回有差异的字段。

    candidate_partial=True（列表卡片）时，候选没给的字段不算差异。
    比较就是"规范化后的原文字符串是否相等"，不做语义等价。
    """
    current_fields = normalize_field_keys(current_fields)
    candidate_fields = normalize_field_keys(candidate_fields)
    differences: dict[str, dict[str, Any]] = {}
    names = (set(current_fields) | set(candidate_fields)) - SNAPSHOT_META_FIELDS
    for name in sorted(names):
        before = normalize_raw_text(current_fields.get(name))
        after = normalize_raw_text(candidate_fields.get(name))
        if candidate_partial and name not in candidate_fields:
            continue
        if before == after:
            continue
        differences[name] = {
            "current": current_fields.get(name),
            "candidate": candidate_fields.get(name),
            "candidate_present": name in candidate_fields,
        }
    return differences


def decide_for_source(
    *,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    candidate_partial: bool = False,
) -> Decision:
    """职位来源重采判定。

    规则（对齐 3.6 / 4.4 快照合并）：
      1. 无差异 → unchanged；
      2. candidate_partial 且差异都是"旧值空、候选有值" → 补全（updated 语义，
         但 result_type 仍由调用方映射为 source_created/unchanged，补全不产生冲突）；
      3. 其余 → conflict_pending（用户 keep_old/keep_new）。
    """
    differences = compare_raw_fields(
        current_fields=current_fields,
        candidate_fields=candidate_fields,
        candidate_partial=candidate_partial,
    )
    if not differences:
        return Decision(
            kind=RESULT_UNCHANGED,
            reason="原文无变化",
            merged_snapshot=dict(current_fields),
            keeps_current_artifact=True,
        )

    if candidate_partial:
        fills = {
            name: detail
            for name, detail in differences.items()
            if _is_blank(detail["current"]) and detail["candidate"] is not None
        }
        rest = {name: detail for name, detail in differences.items() if name not in fills}
        if not rest:
            merged = dict(current_fields)
            for name, detail in fills.items():
                merged[name] = detail["candidate"]
            return Decision(
                kind=RESULT_UNCHANGED,
                reason="列表卡片补齐了原本为空的字段（沿用原原件）: " + ", ".join(sorted(fills)),
                differences=differences,
                merged_snapshot=merged,
                keeps_current_artifact=True,
                issues=[f"列表卡片补齐 {name}，沿用原原件" for name in sorted(fills)],
            )

    return Decision(
        kind=RESULT_CONFLICT_PENDING,
        reason="存在原文差异，需用户选择: " + ", ".join(sorted(differences)),
        differences=differences,
    )


def decide_for_company(
    *,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
) -> Decision:
    """公司来源重采判定：与职位同规则，但不区分 partial（公司页一次取全）。"""
    return decide_for_source(
        current_fields=current_fields,
        candidate_fields=candidate_fields,
        candidate_partial=False,
    )


def merge_snapshot(
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    field_names: list[str],
) -> dict[str, Any]:
    """按 4.4 快照合并规则：field_names 内的槽位用候选值覆盖，其余保留旧值。

    extra_raw_json 按本次明确读取的键处理，不清空其他键。
    """
    merged = dict(current_fields)
    for name in field_names:
        if name == "extra_raw_json":
            # 键级合并：候选明确给的键覆盖，其余保留
            cur_extra = dict(current_fields.get("extra_raw_json") or {})
            cand_extra = dict(candidate_fields.get("extra_raw_json") or {})
            for k, v in cand_extra.items():
                if k in field_names or v is not None:
                    cur_extra[k] = v
            merged["extra_raw_json"] = cur_extra
            continue
        if name in candidate_fields:
            merged[name] = candidate_fields[name]
    return merged


def merge_by_field_choices(
    *,
    current_fields: dict[str, Any],
    candidate_fields: dict[str, Any],
    choices: dict[str, str],
) -> dict[str, Any]:
    """按字段级选择合并：choices[字段名] = "old" | "new"。

    未在 choices 里出现的字段保留旧值；extra_raw_json 走键级合并（选 new 的键覆盖）。
    返回合并后的完整字段字典（含 extra_raw_json 键）。
    """
    merged = dict(current_fields)
    for name, side in choices.items():
        if side == "new" and name in candidate_fields:
            merged[name] = candidate_fields[name]
        # side == "old" 或候选无此键：保留旧值
    # extra_raw_json 键级合并：候选 extra 里被选 new 的键覆盖
    if "extra_raw_json" in merged or "extra_raw_json" in candidate_fields:
        cur_extra = dict(current_fields.get("extra_raw_json") or {})
        cand_extra = dict(candidate_fields.get("extra_raw_json") or {})
        for k, v in cand_extra.items():
            if k in choices and choices[k] == "new":
                cur_extra[k] = v
        merged["extra_raw_json"] = cur_extra
    return merged


def build_snapshot(
    fields: dict[str, Any],
    *,
    revision: int,
    artifact_id: str,
    captured_at: str | None,
    issues_json: list[dict[str, Any]] | None,
    locator: str | None = None,
    company_source_id: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造冲突/来源的完整快照 {revision, fields, evidence_refs}。"""
    snapshot_fields = dict(fields)
    snapshot_fields["artifact_id"] = artifact_id
    snapshot_fields["captured_at"] = captured_at
    snapshot_fields["issues_json"] = issues_json or []
    if locator is not None:
        snapshot_fields["locator"] = locator
    if company_source_id is not None:
        snapshot_fields["company_source_id"] = company_source_id
    return {
        "revision": revision,
        "fields": snapshot_fields,
        "evidence_refs": evidence_refs or [],
    }
