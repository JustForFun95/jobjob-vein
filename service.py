"""jobjobvein 服务层：任务、采集执行、来源/公司/冲突/原件读写。

对齐 02 规格 1.1.1：
- 六张表 + artifacts 原件；来源/公司原文拆 *_raw 列；revision 替代旧版本号。
- 任务类型 collect/import/recollect/company_collect；结果 result_type 八值。
- 冲突 source_id/company_source_id 二选一；决策 keep_old/keep_new + expected_revision。
- 不调 LLM、不做语义提取、不做 CSV 导出、不做永久删除。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import sources
from .adapters.base import (
    AdapterOutput,
    Budget,
    ENTITY_COMPANY,
    ENTITY_JOB,
    ENTITY_MATERIAL,
    ENTITY_PAGE,
)
from .adapters.boss import BossAdapter
from .adapters.manual import ManualAdapter
from .config import Config, load_config
from .errors import (
    invalid_argument,
    missing_field,
    not_found,
    processing_failed,
    stale_content,
    too_large,
    unsupported,
)
from .normalization import identity_key, utc_now_iso
from .storage.artifacts import ArtifactStore
from .storage.database import Database, MetaRepo
from .storage.repositories import Repo

# task.status 取值
TASK_READY = "ready"
TASK_RUNNING = "running"
TASK_FINISHED = "finished"
TASK_FAILED = "failed"
TASK_STOPPED = "stopped"

# collection_results.result_type 取值
RESULT_SOURCE_CREATED = sources.RESULT_SOURCE_CREATED
RESULT_COMPANY_CREATED = sources.RESULT_COMPANY_CREATED
RESULT_UNCHANGED = sources.RESULT_UNCHANGED
RESULT_MATERIAL_SAVED = sources.RESULT_MATERIAL_SAVED
RESULT_CONFLICT_PENDING = sources.RESULT_CONFLICT_PENDING
RESULT_EMPTY = sources.RESULT_EMPTY
RESULT_FAILED = sources.RESULT_FAILED

DECISION_KEEP_OLD = sources.DECISION_KEEP_OLD
DECISION_KEEP_NEW = sources.DECISION_KEEP_NEW
DECISION_KEEP_MERGE = sources.DECISION_KEEP_MERGE


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class JobjobveinService:
    def __init__(self, config: Config | None = None, db: Database | None = None) -> None:
        self.config = config or load_config()
        self.db = db or Database(self.config.storage.db_path)
        self.db.initialize()
        self.artifacts = ArtifactStore(self.db, self.config.storage.artifacts_dir)
        self.repo = Repo(self.db)
        self.meta = MetaRepo(self.db)
        self.boss = BossAdapter(self.config.boss)
        self.manual = ManualAdapter()
        self._stop_flags: dict[str, bool] = {}
        # 单采集器：一次只跑一个渠道采集任务（SRC-R-118）。
        self._executor_lock = threading.Lock()
        self._collecting = False
        self._queue: list[str] = []
        self._wake = threading.Event()
        self._executor = threading.Thread(target=self._run_loop, daemon=True)

    def _run_loop(self) -> None:
        """单线程消费队列里的任务，串行执行采集。"""
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._executor_lock:
                task_id = self._queue.pop(0) if self._queue else None
            if task_id is None:
                continue
            with self._executor_lock:
                self._collecting = True
            try:
                self._execute(task_id)
            except Exception:  # noqa: BLE001 - 异常已在 _execute 内落库，这里兜底
                pass
            finally:
                with self._executor_lock:
                    self._collecting = False

    def _enqueue(self, task_id: str) -> None:
        with self._executor_lock:
            self._queue.append(task_id)
        self._wake.set()
        if not self._executor.is_alive():
            self._executor = threading.Thread(target=self._run_loop, daemon=True)
            self._executor.start()

    def _collector_busy(self) -> bool:
        with self._executor_lock:
            return self._collecting or bool(self._queue)

    # ------------------------------------------------------------------ 能力

    def capabilities(self, *, fresh: bool = False) -> dict[str, Any]:
        # fresh=True 强制重新探测登录态（供页面「重检」使用），否则走 60 秒缓存。
        channels = [self.boss.capabilities(fresh=fresh), self.manual.capabilities()]
        return {
            "protocol_version": "1.1",
            "channels": channels,
            "limits": {
                "list_page_size_max": self.config.limits.list_page_size_max,
                "upload_max_bytes": self.config.limits.upload_max_bytes,
                "max_cards": self.config.limits.max_cards,
                "max_candidates": self.config.budget.max_candidates,
                "default_candidates": self.config.budget.default_candidates,
                "company_job_max": self.config.limits.company_job_max,
            },
            "default_search": {
                "keyword": self.config.search.default_keyword,
                "cities": list(self.config.search.default_cities),
            },
        }

    def start_debug_browser(self) -> dict[str, Any]:
        """启动带调试端口的 BOSS 浏览器（供页面一键启动）。

        登录态只存 data/chrome-profile，程序不读不写 Cookie，只负责拉起进程。
        与 scripts/start_all.py 的 start_browser 同一套参数，保证两种入口等效。
        """
        address = self.config.boss.debug_address
        if self._debug_port_alive(address):
            return {"started": False, "already_running": True, "message": "调试浏览器已就绪"}

        import os
        import subprocess

        from .config import PROJECT_ROOT

        roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LocalAppData", ""),
        ]
        chrome: Path | None = None
        for root in roots:
            if not root:
                continue
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                chrome = candidate
                break
        if chrome is None:
            raise unsupported("未找到 Chrome，请手工启动调试浏览器")

        profile = PROJECT_ROOT / "data" / "chrome-profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = address.rpartition(":")[2] or "9222"
        login_url = "https://www.zhipin.com/web/user/?ka=header-login"
        try:
            subprocess.Popen(
                [
                    str(chrome),
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    login_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            raise processing_failed(f"启动浏览器失败：{exc}")

        # 最多等 15 秒端口就绪；返回状态让前端决定何时重检。
        import time as _time
        for _ in range(50):
            if self._debug_port_alive(address):
                return {"started": True, "already_running": False, "message": "调试浏览器已启动"}
            _time.sleep(0.3)
        return {
            "started": True,
            "already_running": False,
            "port_ready": False,
            "message": "已发出启动命令，但调试端口 15 秒内未就绪；请在桌面手动检查",
        }

    @staticmethod
    def _debug_port_alive(address: str) -> bool:
        import urllib.request
        host, _, port = address.partition(":")
        try:
            with urllib.request.urlopen(
                f"http://{host}:{port}/json/version", timeout=1.5
            ) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ 任务

    def register_task(self, task_type: str, request: dict[str, Any]) -> dict[str, Any]:
        """登记任务（对齐 6.1 POST /tasks）。冻结请求，校验幂等。"""
        if task_type not in ("collect", "import", "recollect", "company_collect"):
            raise unsupported(f"不支持的任务类型: {task_type}")
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise missing_field("task_id")

        frozen = self._freeze_request(task_type, request)

        existing = self.repo.get_task(task_id)
        if existing is not None:
            if existing["request_json"] != frozen:
                from .errors import idempotency_mismatch
                raise idempotency_mismatch("相同 task_id 的请求字段不一致")
            return self._task_public(existing)

        now = utc_now_iso()
        with self.db.tx() as conn:
            self.repo.insert_task(conn, {
                "task_id": task_id,
                "task_type": task_type,
                "request_json": frozen,
                "status": TASK_READY,
                "created_at": now,
                "updated_at": now,
            })
        return self._task_public(self.repo.get_task(task_id))

    def _freeze_request(self, task_type: str, request: dict[str, Any]) -> dict[str, Any]:
        """校验并冻结任务请求，固定默认值（SRC 6.2）。"""
        channel = request.get("channel")
        cities = request.get("cities") or []
        keyword = request.get("keyword")
        source_url = request.get("source_url")
        source_id = request.get("source_id")
        company_source_id = request.get("company_source_id")
        materials = request.get("materials") or []
        ingest_origin = request.get("ingest_origin") or "user_task"
        limits = dict(request.get("limits") or {})

        if not isinstance(cities, list):
            raise invalid_argument("cities 必须是数组")
        seen: list[str] = []
        for c in cities:
            if not isinstance(c, str) or not c.strip():
                raise invalid_argument("cities 元素必须是非空字符串")
            if c not in seen:
                seen.append(c)
        cities = seen

        if keyword is not None and not isinstance(keyword, str):
            raise invalid_argument("keyword 必须是字符串")
        keyword = keyword.strip() if isinstance(keyword, str) else None
        if not keyword:
            keyword = None

        if not isinstance(materials, list):
            raise invalid_argument("materials 必须是数组")
        if ingest_origin not in ("user_task", "admin_preload", "manual_import"):
            raise invalid_argument("ingest_origin 非法")

        # 材料引用已上传原件时，登记即校验 sha256 匹配（对齐 6.2「原件须已上传且哈希匹配」）。
        for i, m in enumerate(materials):
            if not isinstance(m, dict):
                raise invalid_argument(f"materials[{i}] 必须是对象")
            ref_id = m.get("artifact_id")
            if not ref_id:
                raw = m.get("raw_content")
                if isinstance(raw, dict):
                    file_ref = raw.get("file_ref")
                    if isinstance(file_ref, str) and file_ref:
                        ref_id = file_ref
            if not ref_id:
                continue
            declared_sha = m.get("sha256")
            if not isinstance(declared_sha, str) or not declared_sha:
                raise invalid_argument(f"materials[{i}] 引用原件须提供 sha256")
            stored = self.repo.get_artifact(ref_id)
            if stored is None:
                raise invalid_argument(f"materials[{i}] 引用原件不存在: {ref_id}")
            if declared_sha.lower() != stored["sha256"].lower():
                raise invalid_argument(f"materials[{i}] sha256 不匹配: {ref_id}")

        # 采集候选总量（跨页累计），默认 100、上限 200。
        max_candidates = limits.get("max_candidates", self.config.budget.default_candidates)
        if not isinstance(max_candidates, int) or not 1 <= max_candidates <= self.config.budget.max_candidates:
            raise invalid_argument(
                f"max_candidates 必须是 1–{self.config.budget.max_candidates} 的整数"
            )
        timeout_seconds = limits.get("timeout_seconds", self.config.budget.default_timeout_seconds)
        if not isinstance(timeout_seconds, int) or not 10 <= timeout_seconds <= 600:
            raise invalid_argument("timeout_seconds 必须是 10–600 的整数")

        # 任务类型约束
        if task_type == "collect":
            if materials:
                raise invalid_argument("collect 不接受非空 materials（材料导入用 import）")
            if source_url and (cities or keyword):
                raise invalid_argument("source_url 与 cities/keyword 不能同时输入")
            if source_id or company_source_id:
                raise invalid_argument("collect 不接受 source_id/company_source_id（用 recollect）")
        elif task_type == "import":
            if not materials:
                raise missing_field("materials")
            if cities or keyword:
                raise invalid_argument("import 不接受城市/关键词")
        elif task_type == "recollect":
            if source_url or cities or keyword or materials:
                raise invalid_argument("recollect 不接受 source_url/城市/关键词/materials")
            if (source_id is None) == (company_source_id is None):
                raise invalid_argument("recollect 需 source_id/company_source_id 恰好一个")
        elif task_type == "company_collect":
            if materials:
                raise invalid_argument("company_collect 不接受 materials")
            if (source_url is None) == (company_source_id is None):
                raise invalid_argument("company_collect 需 source_url 或 company_source_id 恰好一个")
            if cities or keyword:
                raise invalid_argument("company_collect 不接受城市/关键词")

        # 搜索式采集：关键词和城市都空会用默认条件兜底，避免空跑一趟 0 条还报"已完成"。
        # 单链接采集（source_url）不走搜索，不填默认条件。
        if task_type == "collect" and not source_url:
            if not keyword:
                keyword = self.config.search.default_keyword
            if not cities:
                cities = list(self.config.search.default_cities)

        return {
            "task_id": request["task_id"],
            "task_type": task_type,
            "channel": channel,
            "cities": cities,
            "keyword": keyword,
            "source_url": source_url,
            "source_id": source_id,
            "company_source_id": company_source_id,
            "materials": materials,
            "ingest_origin": ingest_origin,
            "limits": {"max_candidates": max_candidates, "timeout_seconds": timeout_seconds},
        }

    def run_task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise not_found("任务不存在", task_id)
        if task["status"] == TASK_READY:
            is_retry = False
            # 单采集器约束：已有采集任务在跑时，新任务拒绝启动（SRC-R-118）。
            if self._collector_busy():
                raise processing_failed(
                    "已有采集任务正在运行，请稍后再试", task_id, detail={"error_code": "collector_busy"}
                )
            now = utc_now_iso()
            with self.db.tx() as conn:
                self.repo.update_task(conn, task_id, status=TASK_RUNNING, started_at=now)
            task = self.repo.get_task(task_id)
            self._enqueue(task_id)
            return self._task_public(task, is_retry=is_retry)
        if task["status"] == TASK_RUNNING:
            return self._task_public(task, is_retry=True)
        return self._task_public(task, is_retry=True)

    def stop_task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise not_found("任务不存在", task_id)
        with self.db.tx() as conn:
            self.repo.update_task(conn, task_id, stop_requested=True)
        self._stop_flags[task_id] = True
        return self._task_public(self.repo.get_task(task_id))

    def _stop_requested(self, task_id: str) -> bool:
        return self._stop_flags.get(task_id, False)

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.repo.get_task(task_id)
        if task is None:
            raise not_found("任务不存在", task_id)
        return self._task_public(task)

    def wait_task(self, task_id: str, timeout_seconds: float = 60.0) -> dict[str, Any]:
        """轮询等待任务进入终态（finished/failed/stopped）。测试与调用方用于确定性等待。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            task = self.repo.get_task(task_id)
            if task is None:
                raise not_found("任务不存在", task_id)
            if task["status"] in (TASK_FINISHED, TASK_FAILED, TASK_STOPPED):
                return self._task_public(task)
            time.sleep(0.05)
        return self._task_public(self.repo.get_task(task_id))

    def _task_public(self, task: dict[str, Any], *, is_retry: bool = False) -> dict[str, Any]:
        return {
            "task_id": task["task_id"],
            "task_type": task["task_type"],
            "status": task["status"],
            "is_retry": is_retry,
            "stop_requested": bool(task["stop_requested"]),
            "request": task["request_json"],
            "summary": task["summary_json"],
            "error_text": task["error_text"],
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        }

    def get_task_results(
        self,
        task_id: str,
        *,
        company_source_id: str | None = None,
        source_id: str | None = None,
        acquisition_context: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        if self.repo.get_task(task_id) is None:
            raise not_found("任务不存在", task_id)
        maximum = self.config.limits.list_page_size_max
        page, page_size = _paging(page, page_size, maximum)
        offset = (page - 1) * page_size
        items, total = self.repo.list_results(
            task_id,
            company_source_id=company_source_id,
            source_id=source_id,
            acquisition_context=acquisition_context,
            offset=offset,
            limit=page_size,
        )
        return {
            "items": [self._result_public(r) for r in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def _result_public(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "result_id": result["result_id"],
            "item_index": result["item_index"],
            "city": result.get("city"),
            "channel": result["channel"],
            "acquisition_context": result["acquisition_context"],
            "result_type": result["result_type"],
            "source_id": result.get("source_id"),
            "company_source_id": result.get("company_source_id"),
            "conflict_id": result.get("conflict_id"),
            "artifact_id": result.get("artifact_id"),
            "payload": result.get("payload_json") or {},
            "error_code": result.get("error_code"),
            "error_text": result.get("error_text"),
            "captured_at": result.get("captured_at"),
            "created_at": result["created_at"],
        }

    # ------------------------------------------------------------------ 执行

    def _execute(self, task_id: str) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        request = task["request_json"]
        task_type = request["task_type"]
        try:
            if task_type == "collect":
                self._execute_collect(task)
            elif task_type == "import":
                self._execute_import(task)
            elif task_type == "recollect":
                self._execute_recollect(task)
            elif task_type == "company_collect":
                self._execute_company_collect(task)
            summary = self._build_summary(task_id)
            stopped = self._stop_requested(task_id)
            final_status = TASK_STOPPED if stopped else TASK_FINISHED
            with self.db.tx() as conn:
                self.repo.update_task(
                    conn, task_id, status=final_status, summary_json=summary, finished_at=utc_now_iso()
                )
            self._stop_flags.pop(task_id, None)
        except Exception as exc:  # noqa: BLE001
            # 任务级失败也要落 summary（对齐 4.4 / 6.4：failed 任务给 outcome=failed + issues），
            # 不能留 summary=null 让下游无法判断失败原因。
            error_text = str(exc)
            # ApiError 用 error_code，AdapterError 用 kind；两者都映射为 issue.code。
            error_code = (
                getattr(exc, "error_code", None)
                or getattr(exc, "kind", None)
                or "processing_failed"
            )
            try:
                summary = self._build_summary(
                    task_id, task_error={"code": error_code, "text": error_text}
                )
            except Exception:  # noqa: BLE001 - 汇总失败也不能掩盖原始错误
                summary = None
            with self.db.tx() as conn:
                self.repo.update_task(
                    conn, task_id, status=TASK_FAILED, error_text=error_text,
                    summary_json=summary, finished_at=utc_now_iso(),
                )
            self._stop_flags.pop(task_id, None)
            # 线程内执行，异常已落库，不再向上抛。

    def _build_summary(
        self, task_id: str, task_error: dict[str, str] | None = None
    ) -> dict[str, Any]:
        # 对齐 02 规格 4.4 / 6.4：outcome 六值 running/success/empty/partial/failed/stopped；
        # cities 按城市给 {city,status,source_count,error_code}；issues 汇总任务级缺口。
        # task_error 非空表示任务级异常（如渠道未启用），此时 outcome 必为 failed。
        counts = self.repo.result_counts(task_id)
        task = self.repo.get_task(task_id) or {}
        request = task.get("request_json") or {}

        # 有产出 = 至少一条非 failed/empty 的有效结果（含待选冲突，冲突不算执行未结束）。
        productive = any(counts[k] for k in (
            RESULT_SOURCE_CREATED, RESULT_COMPANY_CREATED, RESULT_MATERIAL_SAVED,
            RESULT_UNCHANGED, RESULT_CONFLICT_PENDING,
        ))
        has_failures = bool(counts[RESULT_FAILED])
        has_pending_conflicts = bool(counts[RESULT_CONFLICT_PENDING])

        if task_error is not None and not productive:
            outcome = "failed"
        elif task.get("stop_requested") or task.get("status") == TASK_STOPPED:
            outcome = "stopped"
        elif not productive and has_failures:
            outcome = "failed"
        elif not any(counts.values()):
            outcome = "empty"
        elif has_failures or has_pending_conflicts:
            # 有有效内容，但同时有失败或待选缺口 → partial。
            outcome = "partial"
        else:
            outcome = "success"

        cities = self._build_city_summary(task_id, request.get("cities") or [])
        issues = self._build_task_issues(task_id, outcome)
        if task_error is not None:
            issues = [{
                "code": task_error.get("code") or "processing_failed",
                "message": task_error.get("text") or "任务执行失败",
                "field": None,
            }] + issues
        return {"outcome": outcome, "counts": counts, "cities": cities, "issues": issues}

    def _build_city_summary(self, task_id: str, requested_cities: list[str]) -> list[dict[str, Any]]:
        """按请求顺序汇总每个城市的采集结果。

        status=done 表示该城市至少产出过一条非 failed 结果；failed 表示该城市
        只有 failed；skipped 表示请求了但本次未产生任何结果（被停止或未执行）。
        source_count 为该城市下 source_created/unchanged/conflict_pending 的职位数。
        """
        rows = self.db.query_all(
            "SELECT city, result_type, error_code FROM collection_results WHERE task_id = ?",
            (task_id,),
        )
        by_city: dict[str, list[tuple[str, str | None]]] = {}
        for row in rows:
            city = row["city"]
            # 无城市归属的结果（import/材料/单链接采集）不纳入城市汇总：
            # 对齐 4.4「无城市条件时 cities=[]」。
            if not city:
                continue
            by_city.setdefault(city, []).append((row["result_type"], row["error_code"]))

        productive_types = {
            RESULT_SOURCE_CREATED, RESULT_COMPANY_CREATED, RESULT_MATERIAL_SAVED,
            RESULT_UNCHANGED, RESULT_CONFLICT_PENDING,
        }

        summary: list[dict[str, Any]] = []
        seen_cities: set[str] = set()
        # 先按请求顺序输出有结果的城市。
        for city in requested_cities:
            entries = by_city.pop(city, [])
            if not entries:
                continue
            seen_cities.add(city)
            summary.append(self._city_entry(city, entries, productive_types))
        # 请求外出现的城市（如采集途中落到未显式请求的城市）。
        for city, entries in sorted(by_city.items()):
            if city in seen_cities:
                continue
            summary.append(self._city_entry(city, entries, productive_types))
        # 请求了但没有任何结果的城市 → skipped。
        for city in requested_cities:
            if city not in seen_cities and city not in by_city:
                summary.append({
                    "city": city, "status": "skipped", "source_count": 0, "error_code": None,
                })
        return summary

    @staticmethod
    def _city_entry(
        city: str | None,
        entries: list[tuple[str, str | None]],
        productive_types: set[str],
    ) -> dict[str, Any]:
        failed = [e for e in entries if e[0] == RESULT_FAILED]
        productive = [e for e in entries if e[0] in productive_types]
        status = "failed" if (failed and not productive) else "done"
        error_code = failed[0][1] if (failed and not productive) else None
        source_count = sum(1 for e in entries if e[0] in (
            RESULT_SOURCE_CREATED, RESULT_UNCHANGED, RESULT_CONFLICT_PENDING,
        ))
        return {
            "city": city, "status": status, "source_count": source_count, "error_code": error_code,
        }

    def _build_task_issues(self, task_id: str, outcome: str) -> list[dict[str, Any]]:
        """汇总任务级 Issue：失败项的错误码去重后列出，不重复展开每条原文。"""
        rows = self.db.query_all(
            "SELECT error_code, error_text FROM collection_results"
            " WHERE task_id = ? AND error_code IS NOT NULL ORDER BY item_index",
            (task_id,),
        )
        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = row["error_code"]
            if code not in seen:
                seen[code] = {"code": code, "message": row["error_text"] or code, "field": None}
        return list(seen.values())

    def _execute_collect(self, task: dict[str, Any]) -> None:
        request = task["request_json"]
        channel = request.get("channel") or "boss"
        adapter = self._adapter_for(channel)
        budget = self._budget(request)
        company_refs: list[str] = []  # 本次采到的可靠公司引用，保序去重

        if request.get("source_url"):
            output = adapter.acquire_one(request["source_url"])
            self._ingest_output(task, output, city=None)
            if output.company_ref:
                company_refs.append(output.company_ref)
            else:
                self._ingest_company_gap(task, output)
        else:
            criteria: dict[str, Any] = {}
            if request.get("keyword"):
                criteria["keyword"] = request["keyword"]
            cities = request.get("cities") or []
            if cities:
                for city in cities:
                    c = dict(criteria)
                    c["city_code"] = city
                    for output in adapter.acquire(c, {}, budget, lambda: self._stop_requested(task["task_id"])):
                        self._ingest_output(task, output, city=city)
                        if not output.company_ref:
                            self._ingest_company_gap(task, output)
                        elif output.company_ref not in company_refs:
                            company_refs.append(output.company_ref)
            elif criteria:
                # 有关键词无城市：只有关键词，不猜城市
                for output in adapter.acquire(criteria, {}, budget, lambda: self._stop_requested(task["task_id"])):
                    self._ingest_output(task, output, city=None)
                    if not output.company_ref:
                        self._ingest_company_gap(task, output)
                    elif output.company_ref not in company_refs:
                        company_refs.append(output.company_ref)

        # 对齐 02 规格 3.4 / 第 64 行：职位来源有可靠公司引用时补采公司主页 +
        # 招聘页首屏。公司失败不丢弃已采职位，只落一条 failed 结果说明缺口。
        # SRC-R-106：同一任务同一公司引用只补采一次；公司页发现的职位卡片不递归。
        self._follow_companies(task, adapter, company_refs)

    def _follow_companies(
        self, task: dict[str, Any], adapter: Any, company_refs: list[str]
    ) -> None:
        task_id = task["task_id"]
        for company_ref in company_refs:
            if self._stop_requested(task_id):
                break
            try:
                outputs = adapter.acquire_company(
                    company_ref, self.config.limits.company_job_max
                )
            except Exception as exc:  # noqa: BLE001 - 公司失败不影响职位
                now = utc_now_iso()
                result_id = _new_id("res")
                with self.db.tx() as conn:
                    index = self.repo.next_result_index(task_id)
                    self.repo.insert_result(conn, {
                        "result_id": result_id,
                        "task_id": task_id,
                        "item_index": index,
                        "city": None,
                        "channel": getattr(adapter, "channel", "boss"),
                        "acquisition_context": "company_profile",
                        "result_type": RESULT_FAILED,
                        "payload_json": {"company_ref": company_ref},
                        "error_code": getattr(exc, "kind", None) or "company_collect_failed",
                        "error_text": f"补采公司失败：{exc}",
                        "captured_at": now,
                        "created_at": now,
                    })
                # 02 规格 2.1：公司失败不丢职位，但要在职位来源上记明缺口。
                self._mark_company_gap_for_ref(task_id, company_ref, str(exc))
                continue
            for output in outputs:
                # 公司页/招聘页卡片 result 的 acquisition_context 已是
                # company_profile/company_jobs，不会再进 _execute_collect 的职位循环，
                # 因此不会递归触发新一轮公司补采（SRC-R-106）。
                self._ingest_output(task, output, city=None)
            # 职位先于公司入库，此时职位 source 的 company_source_id 还为空。
            # 公司建好后回头把本任务引用该公司的职位关联上（对齐 3.4 一起采集）。
            self._link_task_jobs_to_company(task_id, company_ref)

    def _link_task_jobs_to_company(self, task_id: str, company_ref: str) -> None:
        """把本任务里 company_ref 匹配的职位来源关联到刚补采的公司来源。"""
        company_identity = identity_key("boss", company_ref)
        if not company_identity:
            return
        with self.db.tx() as conn:
            company = conn.execute(
                "SELECT company_source_id FROM company_source_records"
                " WHERE channel='boss' AND identity_key=?",
                (company_identity,),
            ).fetchone()
            if company is None:
                return
            company_id = company["company_source_id"]
            # 本任务中职位结果 payload 记录了 company_ref，用它找到对应职位来源。
            rows = conn.execute(
                "SELECT DISTINCT source_id FROM collection_results"
                " WHERE task_id=? AND source_id IS NOT NULL"
                " AND json_extract(payload_json,'$.company_ref')=?",
                (task_id, company_ref),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE source_records SET company_source_id=?, updated_at=?"
                    " WHERE source_id=? AND company_source_id IS NULL",
                    (company_id, utc_now_iso(), row["source_id"]),
                )
            # 02 规格 3.5 / 索引 ix_results_company_task：本次首屏职位要能按
            # task_id + company_source_id + acquisition_context=company_jobs 查到。
            # 职位结果先于公司落库，company_source_id 当时为空，这里一并回填。
            conn.execute(
                "UPDATE collection_results SET company_source_id=?"
                " WHERE task_id=? AND company_source_id IS NULL"
                " AND acquisition_context='company_jobs'"
                " AND json_extract(payload_json,'$.company_ref')=?",
                (company_id, task_id, company_ref),
            )

    def _append_source_issue(self, source_id: str, issue: dict[str, Any]) -> None:
        """给来源补一条缺口说明；已有公司来源或同类 issue 时不重复写。"""
        source = self.repo.get_source(source_id)
        if source is None or source.get("company_source_id"):
            return
        raw_issues = source.get("issues_json")
        issues = json.loads(raw_issues) if isinstance(raw_issues, str) else list(raw_issues or [])
        if any(item.get("code") == issue["code"] for item in issues):
            return
        with self.db.tx() as conn:
            self.repo.update_source(conn, source_id, {"issues_json": issues + [issue]})

    def _ingest_company_gap(self, task: dict[str, Any], output: AdapterOutput) -> None:
        """02 规格 2.1：没读到可靠公司引用时保留职位并写明原因。"""
        rows = self.db.query_all(
            "SELECT DISTINCT source_id FROM collection_results"
            " WHERE task_id=? AND source_id IS NOT NULL"
            " AND json_extract(payload_json,'$.source_ref')=?",
            (task["task_id"], output.source_ref),
        )
        for row in rows:
            self._append_source_issue(row["source_id"], {
                "code": "company_ref_missing",
                "message": "职位页未读到可靠公司主页引用，未补采公司页面",
                "field": "company_source_id",
            })

    def _mark_company_gap_for_ref(
        self, task_id: str, company_ref: str, reason: str
    ) -> None:
        """02 规格 3.4：公司补采失败时在本次采纳的职位来源上记缺口。"""
        rows = self.db.query_all(
            "SELECT DISTINCT source_id FROM collection_results"
            " WHERE task_id=? AND source_id IS NOT NULL"
            " AND json_extract(payload_json,'$.entity_type')='job'"
            " AND json_extract(payload_json,'$.company_ref')=?",
            (task_id, company_ref),
        )
        for row in rows:
            self._append_source_issue(row["source_id"], {
                "code": "company_page_missing",
                "message": f"公司页面补采失败，职位已保留：{reason}",
                "field": "company_source_id",
            })

    def _execute_import(self, task: dict[str, Any]) -> None:
        request = task["request_json"]
        for index, material in enumerate(request["materials"]):
            if self._stop_requested(task["task_id"]):
                break
            normalized = self.manual.normalize_item(material, index)
            artifact_id = material.get("artifact_id") if isinstance(material, dict) else None
            # 材料也可通过 raw_content.file_ref 引用已上传原件。
            if not artifact_id and isinstance(material, dict):
                raw = material.get("raw_content") or {}
                file_ref = raw.get("file_ref") if isinstance(raw, dict) else None
                if isinstance(file_ref, str) and file_ref:
                    artifact_id = file_ref
            artifact = self.repo.get_artifact(artifact_id) if artifact_id else None
            payload = b""
            mime_type = "text/plain"
            if artifact is not None:
                loaded = self.artifacts.read(artifact_id)
                if loaded is None:
                    raise invalid_argument(f"材料原件不可读: {artifact_id}")
                payload, _ = loaded
                mime_type = artifact["mime_type"]
                # 对齐 6.2：引用已上传原件时，请求声明的 sha256 必须与原件实际哈希匹配。
                declared_sha = material.get("sha256") if isinstance(material, dict) else None
                if isinstance(declared_sha, str) and declared_sha:
                    if declared_sha.lower() != artifact["sha256"].lower():
                        raise invalid_argument(
                            f"材料 sha256 不匹配: {artifact_id}"
                        )
            else:
                payload = (normalized.get("text") or "").encode("utf-8")
            output = self.manual.to_output(
                normalized, payload, mime_type=mime_type, source_url=request.get("source_url")
            )
            self._ingest_output(task, output, city=None)

    def _execute_recollect(self, task: dict[str, Any]) -> None:
        request = task["request_json"]
        if request.get("source_id"):
            source = self.repo.get_source(request["source_id"])
            if source is None:
                raise not_found("来源不存在", request["source_id"])
            source_ref = source.get("source_ref") or source.get("source_url")
            if not source_ref:
                raise unsupported("该来源没有可访问引用，请补链接")
            adapter = self._adapter_for(source["channel"])
            output = adapter.acquire_one(source_ref)
            output.acquisition_context = "job_detail"
            self._ingest_output(task, output, city=None)
            # 02 规格 3.4：重采单个职位同样要跟随可靠公司引用补采公司。
            if output.company_ref:
                self._follow_companies(task, adapter, [output.company_ref])
            else:
                self._ingest_company_gap(task, output)
        else:
            company = self.repo.get_company(request["company_source_id"])
            if company is None:
                raise not_found("公司来源不存在", request["company_source_id"])
            company_ref = company.get("company_ref") or company.get("source_url")
            if not company_ref:
                raise unsupported("该公司来源没有可访问引用，请补链接")
            adapter = self._adapter_for(company["channel"])
            for output in adapter.acquire_company(company_ref, self.config.limits.company_job_max):
                self._ingest_output(task, output, city=None)

    def _execute_company_collect(self, task: dict[str, Any]) -> None:
        request = task["request_json"]
        channel = request.get("channel") or "boss"
        adapter = self._adapter_for(channel)
        if request.get("company_source_id"):
            company = self.repo.get_company(request["company_source_id"])
            if company is None:
                raise not_found("公司来源不存在", request["company_source_id"])
            company_ref = company.get("company_ref") or company.get("source_url")
            if not company_ref:
                raise unsupported("该公司来源没有可访问引用")
        else:
            company_ref = request["source_url"]
        for output in adapter.acquire_company(company_ref, self.config.limits.company_job_max):
            self._ingest_output(task, output, city=None)

    def _adapter_for(self, channel: str) -> Any:
        if channel == "boss":
            return self.boss
        raise unsupported(f"不支持渠道: {channel}")

    def _budget(self, request: dict[str, Any]) -> Budget:
        limits = request.get("limits") or {}
        timeout = limits.get("timeout_seconds", self.config.budget.default_timeout_seconds)
        # 采集候选总量走 max_candidates（默认 100、上限 200），
        # 不再用首屏卡片数 max_cards 顶替——那是「一屏可见卡片」的规格上限（SRC-R-107）。
        max_candidates = limits.get(
            "max_candidates", self.config.budget.default_candidates
        )
        if not isinstance(max_candidates, int) or not 1 <= max_candidates <= self.config.budget.max_candidates:
            max_candidates = self.config.budget.default_candidates
        # 翻页数：候选量 ÷ 每页约 15 条，至少 1 页，封顶 max_pages。
        pages = max(1, min(self.config.budget.max_pages, (max_candidates + 14) // 15))
        return Budget(
            deadline=time.monotonic() + timeout,
            max_pages=pages,
            max_candidates=max_candidates,
        )

    # ------------------------------------------------------------------ 摄入

    @staticmethod
    def _evidence_role(output: AdapterOutput) -> str:
        """02 规格 4.4 的 Evidence.role：按取得场景判定，不按实体类型猜测。"""
        if output.entity_type == ENTITY_MATERIAL:
            return "user_material"
        if output.entity_type == ENTITY_COMPANY:
            return "company_profile"
        if output.entity_type == ENTITY_PAGE:
            return "company_jobs"
        if output.entity_type == ENTITY_JOB and output.acquisition_context == "company_jobs":
            return "company_jobs"
        return "job_page"

    def _build_evidence_refs(
        self, output: AdapterOutput, artifact_id: str | None
    ) -> list[dict[str, Any]]:
        if not artifact_id:
            return []
        return [{
            "artifact_id": artifact_id,
            "role": self._evidence_role(output),
            "locator": output.locator,
            "field_names": list(output.field_names),
        }]

    def _build_snapshot(
        self, output: AdapterOutput, artifact_id: str | None
    ) -> dict[str, Any] | None:
        """冻结本次候选快照；没有原件就没有可核对的候选，返回 None。"""
        if not artifact_id:
            return None
        revision = 1
        company_source_id = None
        if output.company_ref:
            company_identity = identity_key(output.channel, output.company_ref)
            company = (
                self.repo.company_by_identity(output.channel, company_identity)
                if company_identity else None
            )
            if company:
                company_source_id = company["company_source_id"]
        if output.entity_type == ENTITY_JOB:
            identity = output.identity_key or identity_key(output.channel, output.source_ref)
            existing = self.repo.source_by_identity(output.channel, identity) if identity else None
            if existing:
                revision = existing["revision"] + 1
                company_source_id = existing.get("company_source_id") or company_source_id
        elif output.entity_type == ENTITY_COMPANY:
            identity = output.identity_key or identity_key(
                output.channel, output.source_ref or output.company_ref
            )
            existing = self.repo.company_by_identity(output.channel, identity) if identity else None
            if existing:
                revision = existing["revision"] + 1
        return sources.build_snapshot(
            output.observed_fields,
            revision=revision,
            artifact_id=artifact_id,
            captured_at=output.captured_at,
            issues_json=output.issues,
            locator=output.locator,
            company_source_id=company_source_id,
            evidence_refs=output.evidence_refs,
        )

    def _ingest_output(self, task: dict[str, Any], output: AdapterOutput, *, city: str | None) -> None:
        task_id = task["task_id"]
        artifact_id = None
        if output.artifact_bytes:
            artifact_id = self.artifacts.save(
                "html" if output.mime_type == "text/html" else "text",
                output.artifact_bytes,
                mime_type=output.mime_type,
                captured_at=output.captured_at,
            )["artifact_id"]
        # 公司招聘页卡片没有独立原件：在冻结结果之前就解析出复用的招聘页原件，
        # 使 evidence_refs / snapshot 与最终落库指向同一份 artifact（02 规格 4.4）。
        if artifact_id is None:
            artifact_id = self._company_jobs_artifact(output)
        output.evidence_refs = self._build_evidence_refs(output, artifact_id)
        snapshot = self._build_snapshot(output, artifact_id)

        now = utc_now_iso()
        result_id = _new_id("res")
        with self.db.tx() as conn:
            index = self.repo.next_result_index(task_id)
            self.repo.insert_result(conn, {
                "result_id": result_id,
                "task_id": task_id,
                "item_index": index,
                "city": city,
                "channel": output.channel,
                "acquisition_context": output.acquisition_context,
                "result_type": "pending",
                "artifact_id": artifact_id,
                "payload_json": {
                    "entity_type": output.entity_type,
                    "input_artifact_id": output.input_artifact_id,
                    "source_url": output.source_url,
                    "source_ref": output.source_ref,
                    "company_ref": output.company_ref,
                    "identity_key": output.identity_key,
                    "field_names": output.field_names,
                    "observed_fields": output.observed_fields,
                    "snapshot": snapshot,
                    "evidence_refs": output.evidence_refs,
                    "verification": output.verification,
                    "channel_basis": output.channel_basis,
                    "issues": output.issues,
                    "page_summary": output.page_summary,
                },
                "captured_at": output.captured_at,
                "created_at": now,
            })

        if output.entity_type == ENTITY_MATERIAL:
            self._settle_material(result_id, output, artifact_id)
        elif output.entity_type == ENTITY_COMPANY:
            self._settle_company(result_id, output, artifact_id)
        elif output.entity_type == ENTITY_JOB:
            self._settle_job(result_id, output, artifact_id)
        elif output.entity_type == ENTITY_PAGE:
            self._settle_page(result_id, output, artifact_id)
        else:
            self._settle_failed(result_id, "unsupported", f"未知实体类型: {output.entity_type}")

    def _settle_material(self, result_id: str, output: AdapterOutput, artifact_id: str | None) -> None:
        if artifact_id is None:
            self._settle_failed(result_id, "save_failed", "材料原件为空")
            return
        with self.db.tx() as conn:
            self.repo.update_result(conn, result_id, {
                "result_type": RESULT_MATERIAL_SAVED,
                "artifact_id": artifact_id,
            })

    def _settle_page(self, result_id: str, output: AdapterOutput, artifact_id: str | None) -> None:
        # 02 规格 3.5：招聘页可见原文属于公司来源资料，独立于原件保存一份可检索文本。
        jobs_page_raw = output.observed_fields.get("jobs_page_raw")
        company = None
        if output.company_ref:
            company_identity = identity_key(output.channel, output.company_ref)
            company = (
                self.repo.company_by_identity(output.channel, company_identity)
                if company_identity else None
            )
        with self.db.tx() as conn:
            self.repo.update_result(conn, result_id, {
                "result_type": RESULT_MATERIAL_SAVED if artifact_id else RESULT_EMPTY,
                "artifact_id": artifact_id,
                "company_source_id": company["company_source_id"] if company is not None else None,
            })
            if company is not None and jobs_page_raw:
                self.repo.update_company(
                    conn, company["company_source_id"], {"jobs_page_raw": jobs_page_raw}
                )

    def _settle_company(self, result_id: str, output: AdapterOutput, artifact_id: str | None) -> None:
        if artifact_id is None:
            self._settle_failed(result_id, "save_failed", "公司页原件为空")
            return
        fields = output.observed_fields or {}
        identity = output.identity_key or identity_key(output.channel, output.source_ref or output.company_ref)
        existing = self.repo.company_by_identity(output.channel, identity) if identity else None

        now = utc_now_iso()
        if existing is None:
            company_id = _new_id("com")
            with self.db.tx() as conn:
                self.repo.insert_company(conn, {
                    "company_source_id": company_id,
                    "channel": output.channel,
                    "acquisition_context": output.acquisition_context,
                    "company_ref": output.company_ref or output.source_ref,
                    "identity_key": identity,
                    "revision": 1,
                    "source_url": output.source_url,
                    "artifact_id": artifact_id,
                    "company_name_raw": fields.get("company_name_raw"),
                    "legal_name_raw": fields.get("legal_name_raw"),
                    "address_raw": fields.get("address_raw"),
                    "established_raw": fields.get("established_raw"),
                    "registered_capital_raw": fields.get("registered_capital_raw"),
                    "scale_raw": fields.get("scale_raw"),
                    "profile_raw": fields.get("profile_raw"),
                    "business_info_raw": fields.get("business_info_raw"),
                    "jobs_reported_count_raw": fields.get("jobs_reported_count_raw"),
                    "extra_raw_json": fields.get("extra_raw"),
                    "issues_json": output.issues,
                    "captured_at": output.captured_at,
                    "created_at": now,
                    "updated_at": now,
                })
                self.repo.update_result(conn, result_id, {
                    "result_type": RESULT_COMPANY_CREATED,
                    "company_source_id": company_id,
                })
        else:
            decision = sources.decide_for_company(
                current_fields=self._company_fields(existing),
                candidate_fields=fields,
            )
            if decision.kind == RESULT_UNCHANGED:
                with self.db.tx() as conn:
                    self.repo.update_result(conn, result_id, {
                        "result_type": RESULT_UNCHANGED,
                        "company_source_id": existing["company_source_id"],
                    })
            else:
                conflict_id = _new_id("cf")
                with self.db.tx() as conn:
                    self.repo.supersede_pending_conflict(conn, company_source_id=existing["company_source_id"])
                    self.repo.insert_conflict(conn, {
                        "conflict_id": conflict_id,
                        "company_source_id": existing["company_source_id"],
                        "old_artifact_id": existing["artifact_id"],
                        "new_artifact_id": artifact_id,
                        "old_snapshot_json": sources.build_snapshot(
                            self._company_fields(existing), revision=existing["revision"],
                            artifact_id=existing["artifact_id"], captured_at=existing["captured_at"],
                            issues_json=existing["issues_json"], evidence_refs=output.evidence_refs,
                        ),
                        "new_snapshot_json": sources.build_snapshot(
                            fields, revision=existing["revision"] + 1, artifact_id=artifact_id,
                            captured_at=output.captured_at, issues_json=output.issues, evidence_refs=output.evidence_refs,
                        ),
                        "status": "pending",
                        "created_at": now,
                        "updated_at": now,
                    })
                    self.repo.update_result(conn, result_id, {
                        "result_type": RESULT_CONFLICT_PENDING,
                        "company_source_id": existing["company_source_id"],
                        "conflict_id": conflict_id,
                    })

    def _company_jobs_artifact(self, output: AdapterOutput) -> str | None:
        """公司招聘页卡片复用同任务里 company_jobs 页结果的原件。

        设计 310 行：一份原件可含多张卡片，用 locator 区分，卡片不另存原件。
        """
        if output.acquisition_context != "company_jobs" or not output.locator:
            return None
        # 招聘页 page result 的 source_ref 是 /gongsi/job/{brand}.html，
        # 与卡片的 company_ref 同属一家公司；按 payload 的 company_ref 匹配。
        if not output.company_ref:
            return None
        rows = self.db.query_all(
            "SELECT artifact_id FROM collection_results"
            " WHERE acquisition_context='company_jobs' AND artifact_id IS NOT NULL"
            " AND json_extract(payload_json,'$.company_ref')=? ORDER BY created_at DESC",
            (output.company_ref,),
        )
        return rows[0]["artifact_id"] if rows else None

    def _settle_job(self, result_id: str, output: AdapterOutput, artifact_id: str | None) -> None:
        if artifact_id is None:
            # 公司招聘页卡片没有独立原件：设计 3.5 / 310 行要求复用招聘页那一份
            # 原件，用 locator 区分卡片。从本任务已落库的 company_jobs 页结果取 artifact。
            artifact_id = self._company_jobs_artifact(output)
        if artifact_id is None:
            self._settle_failed(result_id, "save_failed", "职位原件为空")
            return
        fields = output.observed_fields or {}
        identity = output.identity_key or identity_key(output.channel, output.source_ref)
        existing = self.repo.source_by_identity(output.channel, identity) if identity else None

        company_source_id = None
        if output.company_ref:
            company_identity = identity_key(output.channel, output.company_ref)
            if company_identity:
                company = self.repo.company_by_identity(output.channel, company_identity)
                if company:
                    company_source_id = company["company_source_id"]

        now = utc_now_iso()
        if existing is None:
            source_id = _new_id("src")
            with self.db.tx() as conn:
                self.repo.insert_source(conn, {
                    "source_id": source_id,
                    "channel": output.channel,
                    "acquisition_context": output.acquisition_context,
                    "source_ref": output.source_ref,
                    "identity_key": identity,
                    "revision": 1,
                    "source_url": output.source_url,
                    "company_source_id": company_source_id,
                    "artifact_id": artifact_id,
                    "locator": output.locator,
                    "title_raw": fields.get("title_raw"),
                    "company_name_raw": fields.get("company_name_raw"),
                    "location_raw": fields.get("location_raw"),
                    "salary_raw": fields.get("salary_raw"),
                    "requirements_raw": fields.get("requirements_raw"),
                    "description_raw": fields.get("description_raw"),
                    "extra_raw_json": fields.get("extra_raw"),
                    "issues_json": output.issues,
                    "captured_at": output.captured_at,
                    "created_at": now,
                    "updated_at": now,
                })
                self.repo.update_result(conn, result_id, {
                    "result_type": RESULT_SOURCE_CREATED,
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "company_source_id": company_source_id,
                })
        else:
            candidate_partial = output.acquisition_context == "company_jobs"
            decision = sources.decide_for_source(
                current_fields=self._source_fields(existing),
                candidate_fields=fields,
                candidate_partial=candidate_partial,
            )
            if decision.kind == RESULT_UNCHANGED:
                # 纯 unchanged（原文无变化）只记结果，不动字段与 revision；
                # 只有补全（differences 非空，旧空候选有值）才更新字段并 revision 递增。
                # 对齐 SRC-R-108/R-110：revision 仅在选择新内容或补全时递增。
                if decision.differences:
                    merged = decision.merged_snapshot
                    with self.db.tx() as conn:
                        changes = {k: merged.get(k) for k in sources.SOURCE_RAW_FIELDS if k in merged}
                        if "extra_raw_json" in merged:
                            changes["extra_raw_json"] = merged["extra_raw_json"]
                        if company_source_id:
                            changes["company_source_id"] = company_source_id
                        changes["revision"] = existing["revision"] + 1
                        self.repo.update_source(conn, existing["source_id"], changes)
                        self.repo.update_result(conn, result_id, {
                            "result_type": RESULT_UNCHANGED,
                            "source_id": existing["source_id"],
                            "artifact_id": artifact_id,
                            "company_source_id": company_source_id or existing.get("company_source_id"),
                        })
                else:
                    with self.db.tx() as conn:
                        self.repo.update_result(conn, result_id, {
                            "result_type": RESULT_UNCHANGED,
                            "source_id": existing["source_id"],
                            "artifact_id": artifact_id,
                            "company_source_id": company_source_id or existing.get("company_source_id"),
                        })
            else:
                conflict_id = _new_id("cf")
                with self.db.tx() as conn:
                    self.repo.supersede_pending_conflict(conn, source_id=existing["source_id"])
                    self.repo.insert_conflict(conn, {
                        "conflict_id": conflict_id,
                        "source_id": existing["source_id"],
                        "old_artifact_id": existing["artifact_id"],
                        "new_artifact_id": artifact_id,
                        "old_snapshot_json": sources.build_snapshot(
                            self._source_fields(existing), revision=existing["revision"],
                            artifact_id=existing["artifact_id"], captured_at=existing["captured_at"],
                            issues_json=existing["issues_json"], locator=existing.get("locator"),
                            company_source_id=existing.get("company_source_id"), evidence_refs=output.evidence_refs,
                        ),
                        "new_snapshot_json": sources.build_snapshot(
                            fields, revision=existing["revision"] + 1, artifact_id=artifact_id,
                            captured_at=output.captured_at, issues_json=output.issues,
                            locator=output.locator, company_source_id=company_source_id, evidence_refs=output.evidence_refs,
                        ),
                        "status": "pending",
                        "created_at": now,
                        "updated_at": now,
                    })
                    self.repo.update_result(conn, result_id, {
                        "result_type": RESULT_CONFLICT_PENDING,
                        "source_id": existing["source_id"],
                        "conflict_id": conflict_id,
                        "artifact_id": artifact_id,
                        "company_source_id": company_source_id or existing.get("company_source_id"),
                    })

    def _settle_failed(self, result_id: str, error_code: str, error_text: str) -> None:
        with self.db.tx() as conn:
            self.repo.update_result(conn, result_id, {
                "result_type": RESULT_FAILED,
                "error_code": error_code,
                "error_text": error_text,
            })

    @staticmethod
    def _source_fields(source: dict[str, Any]) -> dict[str, Any]:
        return {k: source.get(k) for k in sources.SOURCE_RAW_FIELDS + ("extra_raw_json",)}

    @staticmethod
    def _company_fields(company: dict[str, Any]) -> dict[str, Any]:
        return {k: company.get(k) for k in sources.COMPANY_RAW_FIELDS + ("extra_raw_json",)}

    # ------------------------------------------------------------------ 来源查询

    def list_sources(self, params: dict[str, Any]) -> dict[str, Any]:
        maximum = self.config.limits.list_page_size_max
        page, page_size = _paging(params.get("page"), params.get("page_size"), maximum)
        offset = (page - 1) * page_size
        channel = _optional_str(params.get("channel"))
        status = params.get("status") or "active"
        company_source_id = _optional_str(params.get("company_source_id"))
        keyword = _optional_str(params.get("keyword"))
        items = self.repo.list_sources(
            channel=channel, status=status, company_source_id=company_source_id,
            keyword=keyword, offset=offset, limit=page_size,
        )
        total = self.repo.count_sources(
            channel=channel, status=status, company_source_id=company_source_id, keyword=keyword
        )
        return {
            "items": [self._source_summary(s) for s in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def _source_summary(self, source: dict[str, Any]) -> dict[str, Any]:
        pending = self.repo.pending_conflict_for_source(source["source_id"])
        return {
            "source_id": source["source_id"],
            "channel": source["channel"],
            "acquisition_context": source["acquisition_context"],
            "title_raw": source.get("title_raw"),
            "company_name_raw": source.get("company_name_raw"),
            "location_raw": source.get("location_raw"),
            "salary_raw": source.get("salary_raw"),
            "revision": source["revision"],
            "status": source.get("status"),
            "captured_at": source.get("captured_at"),
            "created_at": source.get("created_at"),
            "updated_at": source.get("updated_at"),
            "has_pending_conflict": pending is not None,
        }

    def get_source(self, source_id: str) -> dict[str, Any]:
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("来源不存在", source_id)
        pending = self.repo.pending_conflict_for_source(source_id)
        company = self.repo.get_company(source["company_source_id"]) if source.get("company_source_id") else None
        payload = self._source_summary(source)
        payload.update({
            "source_ref": source.get("source_ref"),
            "source_url": source.get("source_url"),
            "identity_key": source.get("identity_key"),
            "artifact_id": source.get("artifact_id"),
            "locator": source.get("locator"),
            "requirements_raw": source.get("requirements_raw"),
            "description_raw": source.get("description_raw"),
            "extra_raw": source.get("extra_raw_json"),
            "issues": source.get("issues_json") or [],
            "company_source": {
                "company_source_id": company["company_source_id"],
                "company_name_raw": company.get("company_name_raw"),
                "legal_name_raw": company.get("legal_name_raw"),
            } if company else {
                "company_source_id": None,
                "missing_reason": "未关联公司来源",
            },
            "pending_conflict_id": pending["conflict_id"] if pending else None,
            "evidence": self.repo.latest_evidence_for_source(source_id, source.get("artifact_id")),
            "created_at": source["created_at"],
            "updated_at": source["updated_at"],
        })
        return payload

    def patch_status(self, source_id: str, status: Any, expected_revision: Any) -> dict[str, Any]:
        source = self.repo.get_source(source_id)
        if source is None:
            raise not_found("来源不存在", source_id)
        if status not in ("active", "inactive"):
            raise invalid_argument("status 必须是 active/inactive")
        if not isinstance(expected_revision, int):
            raise invalid_argument("expected_revision 必须是整数")
        if source["revision"] != expected_revision:
            raise stale_content("来源版本已变化，请刷新后重试", source_id)
        with self.db.tx() as conn:
            self.repo.update_source(conn, source_id, {
                "status": status, "revision": source["revision"] + 1,
            })
        updated = self.repo.get_source(source_id)
        return {"source_id": source_id, "status": updated["status"], "revision": updated["revision"]}

    # ------------------------------------------------------------------ 公司查询

    def list_companies(self, params: dict[str, Any]) -> dict[str, Any]:
        maximum = self.config.limits.list_page_size_max
        page, page_size = _paging(params.get("page"), params.get("page_size"), maximum)
        offset = (page - 1) * page_size
        channel = _optional_str(params.get("channel"))
        keyword = _optional_str(params.get("keyword"))
        items, total = self.repo.list_companies(
            channel=channel, keyword=keyword, offset=offset, limit=page_size
        )
        return {
            "items": [self._company_summary(c) for c in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def _company_summary(self, company: dict[str, Any]) -> dict[str, Any]:
        return {
            "company_source_id": company["company_source_id"],
            "channel": company["channel"],
            "company_name_raw": company.get("company_name_raw"),
            "legal_name_raw": company.get("legal_name_raw"),
            "scale_raw": company.get("scale_raw"),
            "revision": company["revision"],
            "captured_at": company.get("captured_at"),
        }

    def _company_jobs_snapshot(
        self, company_source_id: str, task_id: str | None
    ) -> dict[str, Any]:
        """02 规格 3.5：本次首屏职位按 task_id + company_source_id + context 查询。

        返回的是本次结果里冻结的 observed_fields 原文字段快照和卡片链接，
        不用当前来源字段替代历史卡片；否则旧卡片被后续重采改写后无法还原本次看到的内容。
        同时返回该批次招聘页的 page_summary（采到数量/页面报告数量/范围限制），
        对应 9.1「公司招聘页结果」，避免把首屏误写成全量。
        """
        empty: dict[str, Any] = {"jobs": [], "page_summary": None}
        if not task_id:
            return empty
        rows, _ = self.repo.list_results(
            task_id,
            company_source_id=company_source_id,
            acquisition_context="company_jobs",
            limit=self.config.limits.list_page_size_max,
        )
        jobs: list[dict[str, Any]] = []
        page_summary: dict[str, Any] | None = None
        for row in rows:
            data = row.get("payload_json") or {}
            if data.get("entity_type") == "page":
                collected = data.get("page_summary") or {}
                if page_summary is None or collected.get("collected_count"):
                    page_summary = collected
                continue
            if data.get("entity_type") != "job":
                continue
            observed = data.get("observed_fields") or {}
            jobs.append({
                "source_id": row.get("source_id"),
                "result_type": row.get("result_type"),
                "source_url": data.get("source_url"),
                "title_raw": observed.get("title_raw"),
                "salary_raw": observed.get("salary_raw"),
                "location_raw": observed.get("location_raw"),
                "requirements_raw": observed.get("requirements_raw"),
                "company_name_raw": observed.get("company_name_raw"),
                "captured_at": row.get("captured_at"),
            })
        return {"jobs": jobs, "page_summary": page_summary}

    def get_company(self, company_source_id: str) -> dict[str, Any]:
        company = self.repo.get_company(company_source_id)
        if company is None:
            raise not_found("公司来源不存在", company_source_id)
        pending = self.repo.pending_conflict_for_company(company_source_id)
        payload = self._company_summary(company)
        latest_task_id = self.repo.latest_company_jobs_task_id(company_source_id)
        jobs_snapshot = self._company_jobs_snapshot(company_source_id, latest_task_id)
        payload.update({
            "company_ref": company.get("company_ref"),
            "source_url": company.get("source_url"),
            "identity_key": company.get("identity_key"),
            "artifact_id": company.get("artifact_id"),
            "address_raw": company.get("address_raw"),
            "established_raw": company.get("established_raw"),
            "registered_capital_raw": company.get("registered_capital_raw"),
            "profile_raw": company.get("profile_raw"),
            "business_info_raw": company.get("business_info_raw"),
            "jobs_reported_count_raw": company.get("jobs_reported_count_raw"),
            "extra_raw": company.get("extra_raw_json"),
            "issues": company.get("issues_json") or [],
            "source_count": self.repo.count_sources_for_company(company_source_id),
            "pending_conflict_id": pending["conflict_id"] if pending else None,
            "latest_company_jobs_task_id": latest_task_id,
            "latest_company_jobs": jobs_snapshot["jobs"],
            "latest_company_jobs_page_summary": jobs_snapshot["page_summary"],
            "evidence": self.repo.latest_evidence_for_company(company_source_id, company.get("artifact_id")),
            "created_at": company["created_at"],
            "updated_at": company["updated_at"],
        })
        return payload

    # ------------------------------------------------------------------ 冲突

    def list_conflicts(self, params: dict[str, Any]) -> dict[str, Any]:
        maximum = self.config.limits.list_page_size_max
        page, page_size = _paging(params.get("page"), params.get("page_size"), maximum)
        offset = (page - 1) * page_size
        status = params.get("status") or "pending"
        source_id = _optional_str(params.get("source_id"))
        company_source_id = _optional_str(params.get("company_source_id"))
        items, total = self.repo.list_conflicts(
            status=status, source_id=source_id, company_source_id=company_source_id,
            offset=offset, limit=page_size,
        )
        return {
            "items": [self._conflict_public(c) for c in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_conflict(self, conflict_id: str) -> dict[str, Any]:
        conflict = self.repo.get_conflict(conflict_id)
        if conflict is None:
            raise not_found("冲突不存在", conflict_id)
        return self._conflict_public(conflict)

    def _conflict_public(self, conflict: dict[str, Any]) -> dict[str, Any]:
        return {
            "conflict_id": conflict["conflict_id"],
            "source_id": conflict.get("source_id"),
            "company_source_id": conflict.get("company_source_id"),
            "old_snapshot": conflict["old_snapshot_json"] or {},
            "new_snapshot": conflict["new_snapshot_json"] or {},
            "old_artifact_id": conflict["old_artifact_id"],
            "new_artifact_id": conflict["new_artifact_id"],
            "status": conflict["status"],
            "decision_at": conflict.get("decision_at"),
            "created_at": conflict["created_at"],
            "updated_at": conflict["updated_at"],
        }

    def decide(self, conflict_id: str, decision: Any, expected_revision: int, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        conflict = self.repo.get_conflict(conflict_id)
        if conflict is None:
            raise not_found("冲突不存在", conflict_id)
        if decision not in (DECISION_KEEP_OLD, DECISION_KEEP_NEW, DECISION_KEEP_MERGE):
            raise invalid_argument("decision 必须是 keep_old/keep_new/keep_merge")
        if conflict["status"] != "pending":
            return {
                "conflict_id": conflict_id,
                "status": conflict["status"],
                "target_id": conflict.get("source_id") or conflict.get("company_source_id"),
                "revision": None,
                "already_applied": True,
            }
        if not isinstance(expected_revision, int):
            raise invalid_argument("expected_revision 必须是整数")

        old_revision = (conflict["old_snapshot_json"] or {}).get("revision")
        if old_revision != expected_revision:
            raise stale_content("冲突基准版本已变化，请刷新后重试", conflict_id)

        if decision == DECISION_KEEP_MERGE:
            if not isinstance(fields, dict):
                raise invalid_argument("keep_merge 需要 fields 映射（{字段名: old|new}）")
            for name, side in fields.items():
                if side not in ("old", "new"):
                    raise invalid_argument(f"fields.{name} 必须是 old/new")

        target_id = conflict.get("source_id") or conflict.get("company_source_id")
        now = utc_now_iso()
        applied_revision = None
        if decision == DECISION_KEEP_NEW:
            new_snapshot = conflict["new_snapshot_json"] or {}
            fields_snap = new_snapshot.get("fields") or {}
            applied_revision = new_snapshot.get("revision")
            if conflict.get("source_id"):
                with self.db.tx() as conn:
                    changes = {k: fields_snap.get(k) for k in sources.SOURCE_RAW_FIELDS if k in fields_snap}
                    changes["artifact_id"] = conflict["new_artifact_id"]
                    changes["captured_at"] = fields_snap.get("captured_at")
                    if "extra_raw_json" in fields_snap:
                        changes["extra_raw_json"] = fields_snap["extra_raw_json"]
                    if "issues_json" in fields_snap:
                        changes["issues_json"] = fields_snap["issues_json"]
                    changes["revision"] = applied_revision or 0
                    self.repo.update_source(conn, conflict["source_id"], changes)
                    self.repo.update_conflict_status(conn, conflict_id, DECISION_KEEP_NEW, now)
            else:
                with self.db.tx() as conn:
                    changes = {k: fields_snap.get(k) for k in sources.COMPANY_RAW_FIELDS if k in fields_snap}
                    changes["artifact_id"] = conflict["new_artifact_id"]
                    changes["captured_at"] = fields_snap.get("captured_at")
                    if "extra_raw_json" in fields_snap:
                        changes["extra_raw_json"] = fields_snap["extra_raw_json"]
                    if "issues_json" in fields_snap:
                        changes["issues_json"] = fields_snap["issues_json"]
                    changes["revision"] = applied_revision or 0
                    self.repo.update_company(conn, conflict["company_source_id"], changes)
                    self.repo.update_conflict_status(conn, conflict_id, DECISION_KEEP_NEW, now)
        elif decision == DECISION_KEEP_MERGE:
            old_snap = conflict["old_snapshot_json"] or {}
            new_snap = conflict["new_snapshot_json"] or {}
            old_fields = old_snap.get("fields") or {}
            new_fields = new_snap.get("fields") or {}
            applied_revision = new_snap.get("revision")
            merged = sources.merge_by_field_choices(
                current_fields=old_fields,
                candidate_fields=new_fields,
                choices=fields,
            )
            if conflict.get("source_id"):
                with self.db.tx() as conn:
                    changes = {k: merged.get(k) for k in sources.SOURCE_RAW_FIELDS if k in merged}
                    # 混合采用后原件切到新原件，保证证据可回查（新的字段值来自新原件）
                    changes["artifact_id"] = conflict["new_artifact_id"]
                    changes["captured_at"] = new_fields.get("captured_at") or merged.get("captured_at")
                    if "extra_raw_json" in merged:
                        changes["extra_raw_json"] = merged["extra_raw_json"]
                    changes["revision"] = applied_revision or 0
                    self.repo.update_source(conn, conflict["source_id"], changes)
                    self.repo.update_conflict_status(conn, conflict_id, DECISION_KEEP_NEW, now)
            else:
                with self.db.tx() as conn:
                    changes = {k: merged.get(k) for k in sources.COMPANY_RAW_FIELDS if k in merged}
                    changes["artifact_id"] = conflict["new_artifact_id"]
                    changes["captured_at"] = new_fields.get("captured_at") or merged.get("captured_at")
                    if "extra_raw_json" in merged:
                        changes["extra_raw_json"] = merged["extra_raw_json"]
                    changes["revision"] = applied_revision or 0
                    self.repo.update_company(conn, conflict["company_source_id"], changes)
                    self.repo.update_conflict_status(conn, conflict_id, DECISION_KEEP_NEW, now)
        else:
            with self.db.tx() as conn:
                self.repo.update_conflict_status(conn, conflict_id, DECISION_KEEP_OLD, now)

        return {
            "conflict_id": conflict_id,
            "status": decision,
            "target_id": target_id,
            "revision": applied_revision,
            "already_applied": False,
        }

    # ------------------------------------------------------------------ 原件

    def save_upload(self, payload: bytes, content_type: str, acquired_at: str | None, filename: str | None) -> dict[str, Any]:
        if len(payload) > self.config.limits.upload_max_bytes:
            raise too_large("原件超过大小上限")
        if content_type not in ("text", "html", "image", "email", "file"):
            raise invalid_argument("content_type 非法")
        ext = Path(filename).suffix.lstrip(".") if filename else None
        row = self.artifacts.save(content_type, payload, captured_at=acquired_at, extension=ext)
        return {
            "artifact_id": row["artifact_id"],
            "sha256": row["sha256"],
            "mime_type": row["mime_type"],
            "byte_size": row["byte_size"],
            "captured_at": row["captured_at"],
        }

    def get_artifact(self, artifact_id: str) -> tuple[bytes, dict[str, Any]]:
        loaded = self.artifacts.read(artifact_id)
        if loaded is None:
            raise not_found("原件不存在", artifact_id)
        return loaded


def _paging(page: Any, page_size: Any, maximum: int) -> tuple[int, int]:
    try:
        p = int(page) if page is not None else 1
        ps = int(page_size) if page_size is not None else 20
    except (TypeError, ValueError):
        raise invalid_argument("page/page_size 必须是正整数")
    if p < 1 or ps < 1:
        raise invalid_argument("page/page_size 必须是正整数")
    if ps > maximum:
        raise invalid_argument(f"page_size 超过上限 {maximum}")
    return p, ps


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()
