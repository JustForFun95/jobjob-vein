"""本模块表的读写与查询。所有写函数都要求调用方传入同一事务的连接。

对齐 02 规格 1.1.1：source_records / company_source_records 的原文拆 *_raw 列，
source_conflicts 的 source_id / company_source_id 二选一，revision 替代旧的
content_stamp + change_seq。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from ..normalization import utc_now_iso
from .database import Database

# source_records / company_source_records 的 JSON 列（对外命名为 *_json 存储）
SOURCE_JSON_FIELDS = ("extra_raw_json", "issues_json")
COMPANY_JSON_FIELDS = ("extra_raw_json", "issues_json")


def _load(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _row_to_dict(table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for field in ("extra_raw_json", "issues_json", "payload_json", "request_json",
                  "summary_json", "old_snapshot_json", "new_snapshot_json"):
        if field in data:
            data[field] = _load(data[field])
    return data


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ 原件

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM raw_artifacts WHERE artifact_id = ?", (artifact_id,))
        return dict(row) if row else None

    # ------------------------------------------------------------------ 来源

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "source_records",
            self.db.query_one("SELECT * FROM source_records WHERE source_id = ?", (source_id,)),
        )

    def source_by_identity(self, channel: str, identity: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "source_records",
            self.db.query_one(
                "SELECT * FROM source_records WHERE channel = ? AND identity_key = ?",
                (channel, identity),
            ),
        )

    def insert_source(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO source_records (source_id, channel, acquisition_context, source_ref,"
            " identity_key, revision, source_url, company_source_id, artifact_id, locator,"
            " title_raw, company_name_raw, location_raw, salary_raw, requirements_raw,"
            " description_raw, extra_raw_json, status, issues_json, captured_at,"
            " created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["source_id"],
                data["channel"],
                data["acquisition_context"],
                data.get("source_ref"),
                data.get("identity_key"),
                data.get("revision", 1),
                data.get("source_url"),
                data.get("company_source_id"),
                data["artifact_id"],
                data.get("locator"),
                data.get("title_raw"),
                data.get("company_name_raw"),
                data.get("location_raw"),
                data.get("salary_raw"),
                data.get("requirements_raw"),
                data.get("description_raw"),
                _dump(data["extra_raw_json"]) if data.get("extra_raw_json") is not None else None,
                data.get("status", "active"),
                _dump(data.get("issues_json") or []),
                data.get("captured_at"),
                data["created_at"],
                data["updated_at"],
            ),
        )

    def update_source(self, conn: sqlite3.Connection, source_id: str, changes: dict[str, Any]) -> None:
        allowed = {
            "source_ref", "identity_key", "source_url", "company_source_id", "artifact_id",
            "locator", "title_raw", "company_name_raw", "location_raw", "salary_raw",
            "requirements_raw", "description_raw", "extra_raw_json", "status",
            "issues_json", "captured_at", "revision",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"不允许直接更新字段: {key}")
            if key in ("extra_raw_json",):
                assignments.append(f"{key} = ?")
                params.append(_dump(value) if value is not None else None)
            elif key == "issues_json":
                assignments.append(f"{key} = ?")
                params.append(_dump(value or []))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(changes.get("_now") or utc_now_iso())
        params.append(source_id)
        conn.execute(f"UPDATE source_records SET {', '.join(assignments)} WHERE source_id = ?", params)

    def list_sources(
        self,
        *,
        channel: str | None = None,
        status: str | None = "active",
        company_source_id: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if company_source_id:
            clauses.append("company_source_id = ?")
            params.append(company_source_id)
        if keyword:
            clauses.append("(title_raw LIKE ? OR company_name_raw LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        rows = self.db.query_all(
            f"SELECT * FROM source_records {where} ORDER BY source_id LIMIT ? OFFSET ?", tuple(params)
        )
        return [_row_to_dict("source_records", row) for row in rows]  # type: ignore[misc]

    def count_sources(
        self,
        *,
        channel: str | None = None,
        status: str | None = "active",
        company_source_id: str | None = None,
        keyword: str | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if company_source_id:
            clauses.append("company_source_id = ?")
            params.append(company_source_id)
        if keyword:
            clauses.append("(title_raw LIKE ? OR company_name_raw LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.db.query_one(f"SELECT COUNT(*) AS n FROM source_records {where}", tuple(params))
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ 公司

    def get_company(self, company_source_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "company_source_records",
            self.db.query_one(
                "SELECT * FROM company_source_records WHERE company_source_id = ?", (company_source_id,)
            ),
        )

    def company_by_identity(self, channel: str, identity: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "company_source_records",
            self.db.query_one(
                "SELECT * FROM company_source_records WHERE channel = ? AND identity_key = ?",
                (channel, identity),
            ),
        )

    def insert_company(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO company_source_records (company_source_id, channel, acquisition_context,"
            " company_ref, identity_key, revision, source_url, artifact_id, company_name_raw,"
            " legal_name_raw, address_raw, established_raw, registered_capital_raw, scale_raw,"
            " profile_raw, business_info_raw, jobs_page_raw, jobs_reported_count_raw,"
            " extra_raw_json, issues_json, captured_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["company_source_id"],
                data["channel"],
                data["acquisition_context"],
                data.get("company_ref"),
                data.get("identity_key"),
                data.get("revision", 1),
                data.get("source_url"),
                data["artifact_id"],
                data.get("company_name_raw"),
                data.get("legal_name_raw"),
                data.get("address_raw"),
                data.get("established_raw"),
                data.get("registered_capital_raw"),
                data.get("scale_raw"),
                data.get("profile_raw"),
                data.get("business_info_raw"),
                data.get("jobs_page_raw"),
                data.get("jobs_reported_count_raw"),
                _dump(data["extra_raw_json"]) if data.get("extra_raw_json") is not None else None,
                _dump(data.get("issues_json") or []),
                data.get("captured_at"),
                data["created_at"],
                data["updated_at"],
            ),
        )

    def update_company(self, conn: sqlite3.Connection, company_source_id: str, changes: dict[str, Any]) -> None:
        allowed = {
            "company_ref", "identity_key", "source_url", "artifact_id", "company_name_raw",
            "legal_name_raw", "address_raw", "established_raw", "registered_capital_raw",
            "scale_raw", "profile_raw", "business_info_raw", "jobs_page_raw",
            "jobs_reported_count_raw", "extra_raw_json", "issues_json", "captured_at", "revision",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"不允许直接更新字段: {key}")
            if key == "extra_raw_json":
                assignments.append(f"{key} = ?")
                params.append(_dump(value) if value is not None else None)
            elif key == "issues_json":
                assignments.append(f"{key} = ?")
                params.append(_dump(value or []))
            else:
                assignments.append(f"{key} = ?")
                params.append(value)
        assignments.append("updated_at = ?")
        params.append(changes.get("_now") or utc_now_iso())
        params.append(company_source_id)
        conn.execute(
            f"UPDATE company_source_records SET {', '.join(assignments)} WHERE company_source_id = ?", params
        )

    def list_companies(
        self,
        *,
        channel: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if keyword:
            clauses.append("(company_name_raw LIKE ? OR legal_name_raw LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM company_source_records {where}", tuple(params)
        )
        rows = self.db.query_all(
            f"SELECT * FROM company_source_records {where} ORDER BY company_source_id LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        items = [_row_to_dict("company_source_records", row) or {} for row in rows]
        return items, int(total_row["n"]) if total_row else 0

    def count_sources_for_company(self, company_source_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM source_records WHERE company_source_id = ?", (company_source_id,)
        )
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ 冲突

    def get_conflict(self, conflict_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "source_conflicts",
            self.db.query_one("SELECT * FROM source_conflicts WHERE conflict_id = ?", (conflict_id,)),
        )

    def pending_conflict_for_source(self, source_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "source_conflicts",
            self.db.query_one(
                "SELECT * FROM source_conflicts WHERE source_id = ? AND status = 'pending'", (source_id,)
            ),
        )

    def pending_conflict_for_company(self, company_source_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "source_conflicts",
            self.db.query_one(
                "SELECT * FROM source_conflicts WHERE company_source_id = ? AND status = 'pending'",
                (company_source_id,),
            ),
        )

    def supersede_pending_conflict(self, conn: sqlite3.Connection, *, source_id: str | None = None, company_source_id: str | None = None) -> None:
        """把同一 subject 上已存在的 pending 冲突标为 superseded，为新冲突让位。

        source_conflicts 对每个 subject 只允许一条 pending（唯一索引），
        重复采集同一职位/公司再次产生差异时，必须先作废旧 pending，
        否则 insert 会触发 UNIQUE constraint 导致整任务失败。
        """
        if source_id:
            conn.execute(
                "UPDATE source_conflicts SET status = 'superseded', updated_at = ?"
                " WHERE source_id = ? AND status = 'pending'",
                (utc_now_iso(), source_id),
            )
        if company_source_id:
            conn.execute(
                "UPDATE source_conflicts SET status = 'superseded', updated_at = ?"
                " WHERE company_source_id = ? AND status = 'pending'",
                (utc_now_iso(), company_source_id),
            )

    def insert_conflict(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO source_conflicts (conflict_id, source_id, company_source_id,"
            " old_artifact_id, new_artifact_id, old_snapshot_json, new_snapshot_json,"
            " status, decision_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["conflict_id"],
                data.get("source_id"),
                data.get("company_source_id"),
                data["old_artifact_id"],
                data["new_artifact_id"],
                _dump(data["old_snapshot_json"]),
                _dump(data["new_snapshot_json"]),
                data.get("status", "pending"),
                data.get("decision_at"),
                data["created_at"],
                data["updated_at"],
            ),
        )

    def update_conflict_status(
        self, conn: sqlite3.Connection, conflict_id: str, status: str, decision_at: str
    ) -> None:
        conn.execute(
            "UPDATE source_conflicts SET status = ?, decision_at = ?, updated_at = ? WHERE conflict_id = ?",
            (status, decision_at, utc_now_iso(), conflict_id),
        )

    def list_conflicts(
        self,
        *,
        status: str = "pending",
        source_id: str | None = None,
        company_source_id: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["status = ?"]
        params: list[Any] = [status]
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)
        if company_source_id:
            clauses.append("company_source_id = ?")
            params.append(company_source_id)
        where = "WHERE " + " AND ".join(clauses)
        total_row = self.db.query_one(f"SELECT COUNT(*) AS n FROM source_conflicts {where}", tuple(params))
        rows = self.db.query_all(
            f"SELECT * FROM source_conflicts {where} ORDER BY created_at, conflict_id LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        items = [_row_to_dict("source_conflicts", row) or {} for row in rows]
        return items, int(total_row["n"]) if total_row else 0

    # ------------------------------------------------------------------ 任务

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "collection_tasks",
            self.db.query_one("SELECT * FROM collection_tasks WHERE task_id = ?", (task_id,)),
        )

    def insert_task(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO collection_tasks (task_id, task_type, request_json, status, summary_json,"
            " stop_requested, error_text, started_at, finished_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["task_id"],
                data["task_type"],
                _dump(data["request_json"]),
                data.get("status", "ready"),
                _dump(data["summary_json"]) if data.get("summary_json") is not None else None,
                1 if data.get("stop_requested") else 0,
                data.get("error_text"),
                data.get("started_at"),
                data.get("finished_at"),
                data["created_at"],
                data["updated_at"],
            ),
        )

    def update_task(self, conn: sqlite3.Connection, task_id: str, **changes: Any) -> None:
        allowed = {"status", "summary_json", "stop_requested", "error_text", "started_at", "finished_at"}
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"不允许更新任务字段: {key}")
            if key == "summary_json":
                params.append(_dump(value) if value is not None else None)
            elif key == "stop_requested":
                params.append(1 if value else 0)
            else:
                params.append(value)
            assignments.append(f"{key} = ?")
        assignments.append("updated_at = ?")
        params.extend([utc_now_iso(), task_id])
        conn.execute(f"UPDATE collection_tasks SET {', '.join(assignments)} WHERE task_id = ?", params)

    # ------------------------------------------------------------------ 结果

    def insert_result(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO collection_results (result_id, task_id, item_index, city, channel,"
            " acquisition_context, result_type, source_id, company_source_id, conflict_id,"
            " artifact_id, payload_json, error_code, error_text, captured_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                data["result_id"],
                data["task_id"],
                data["item_index"],
                data.get("city"),
                data["channel"],
                data["acquisition_context"],
                data.get("result_type", "pending"),
                data.get("source_id"),
                data.get("company_source_id"),
                data.get("conflict_id"),
                data.get("artifact_id"),
                _dump(data.get("payload_json") or {}),
                data.get("error_code"),
                data.get("error_text"),
                data.get("captured_at"),
                data["created_at"],
            ),
        )

    def update_result(self, conn: sqlite3.Connection, result_id: str, changes: dict[str, Any]) -> None:
        allowed = {
            "result_type", "source_id", "company_source_id", "conflict_id", "artifact_id",
            "payload_json", "error_code", "error_text",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError(f"不允许更新结果字段: {key}")
            if key == "payload_json":
                params.append(_dump(value or {}))
            else:
                params.append(value)
            assignments.append(f"{key} = ?")
        params.append(result_id)
        conn.execute(f"UPDATE collection_results SET {', '.join(assignments)} WHERE result_id = ?", params)

    def get_result(self, result_id: str) -> dict[str, Any] | None:
        return _row_to_dict(
            "collection_results",
            self.db.query_one("SELECT * FROM collection_results WHERE result_id = ?", (result_id,)),
        )

    def next_result_index(self, task_id: str) -> int:
        row = self.db.query_one(
            "SELECT MAX(item_index) AS n FROM collection_results WHERE task_id = ?", (task_id,)
        )
        return 0 if row is None or row["n"] is None else int(row["n"]) + 1

    def list_results(
        self,
        task_id: str,
        *,
        company_source_id: str | None = None,
        source_id: str | None = None,
        acquisition_context: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses = ["task_id = ?"]
        params: list[Any] = [task_id]
        if company_source_id:
            clauses.append("company_source_id = ?")
            params.append(company_source_id)
        if source_id:
            clauses.append("source_id = ?")
            params.append(source_id)
        if acquisition_context:
            clauses.append("acquisition_context = ?")
            params.append(acquisition_context)
        where = "WHERE " + " AND ".join(clauses)
        total_row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM collection_results {where}", tuple(params)
        )
        rows = self.db.query_all(
            f"SELECT * FROM collection_results {where} ORDER BY item_index LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )
        items = [_row_to_dict("collection_results", row) or {} for row in rows]
        return items, int(total_row["n"]) if total_row else 0

    def _merged_evidence(self, column: str, key: str, artifact_id: str | None) -> dict[str, Any] | None:
        """合并该对象所有终态结果的证据，按 artifact_id 去重。

        02 规格 4.4：详情的 evidence_refs 合并当前已采用快照对应证据；
        公司主页、招聘页、截图等多份原件保留各自角色，不能只取最近一条
        （职位卡片最后落库会把职位页证据挤掉）。取得时间优先取当前采用原件那条。
        """
        rows = self.db.query_all(
            f"SELECT payload_json, captured_at, acquisition_context, artifact_id"
            f" FROM collection_results WHERE {column} = ?"
            f" AND result_type NOT IN ('pending','failed','empty')"
            f" ORDER BY created_at DESC, item_index DESC",
            (key,),
        )
        if not rows:
            return None
        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        captured_at = None
        context = rows[0]["acquisition_context"]
        for row in rows:
            data = _load(row["payload_json"], {}) or {}
            if captured_at is None and artifact_id and row["artifact_id"] == artifact_id:
                captured_at = row["captured_at"]
                context = row["acquisition_context"]
            for ref in data.get("evidence_refs") or []:
                rid = ref.get("artifact_id") if isinstance(ref, dict) else None
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                refs.append(ref)
        return {
            "evidence_refs": refs,
            "input_artifact_id": (_load(rows[0]["payload_json"], {}) or {}).get("input_artifact_id"),
            "acquisition_context": context,
            "captured_at": captured_at or rows[0]["captured_at"],
        }

    def latest_evidence_for_source(self, source_id: str, artifact_id: str | None = None) -> dict[str, Any] | None:
        return self._merged_evidence("source_id", source_id, artifact_id)

    def latest_evidence_for_company(self, company_source_id: str, artifact_id: str | None = None) -> dict[str, Any] | None:
        return self._merged_evidence("company_source_id", company_source_id, artifact_id)

    def result_counts(self, task_id: str) -> dict[str, int]:
        counts = {
            key: 0
            for key in (
                "pending", "source_created", "company_created", "unchanged",
                "material_saved", "conflict_pending", "empty", "failed",
            )
        }
        rows = self.db.query_all(
            "SELECT result_type, COUNT(*) AS n FROM collection_results WHERE task_id = ? GROUP BY result_type",
            (task_id,),
        )
        for row in rows:
            if row["result_type"] in counts:
                counts[row["result_type"]] = int(row["n"])
        return counts

    def latest_company_jobs_task_id(self, company_source_id: str) -> str | None:
        row = self.db.query_one(
            "SELECT r.task_id FROM collection_results r"
            " WHERE r.company_source_id = ? AND r.acquisition_context = 'company_jobs'"
            " ORDER BY r.created_at DESC, r.task_id DESC, r.item_index DESC LIMIT 1",
            (company_source_id,),
        )
        return row["task_id"] if row else None
