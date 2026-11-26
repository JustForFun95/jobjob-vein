"""连接、事务、初始化与首版基线。

对齐 02 模块规格 1.1.1 第 4-5 节：

- 六张表：raw_artifacts、company_source_records、source_records、
  source_conflicts、collection_tasks、collection_results，外加 module_meta
  记录 schema 代次与目录代次（不承载业务，不违反"六张表"口径）。
- 首版本机单管理员：一个连接 + 可重入锁，事务一律短事务；网络、模型
  调用和等待管理员期间不持有事务。
- PRAGMA user_version=110 是本版基线。发现非本版 schema 时拒绝自动升级，
  提示先备份（SRC-R-114），不做删表重建或覆盖初始化。
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 110  # 对应 PRAGMA user_version，与 02 规格 1.1.1 绑定

_DDL_SCRIPT = """
CREATE TABLE module_meta (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL DEFAULT 110 CHECK (schema_version = 110),
    catalog_generation TEXT NOT NULL
);

CREATE TABLE raw_artifacts (
    artifact_id TEXT NOT NULL PRIMARY KEY,
    content_type TEXT NOT NULL CHECK (content_type IN ('text','html','image','file','email')),
    mime_type TEXT NOT NULL,
    storage_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    captured_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE company_source_records (
    company_source_id TEXT NOT NULL PRIMARY KEY,
    channel TEXT NOT NULL,
    acquisition_context TEXT NOT NULL,
    company_ref TEXT,
    identity_key TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    source_url TEXT,
    artifact_id TEXT NOT NULL REFERENCES raw_artifacts(artifact_id) ON DELETE RESTRICT,
    company_name_raw TEXT,
    legal_name_raw TEXT,
    address_raw TEXT,
    established_raw TEXT,
    registered_capital_raw TEXT,
    scale_raw TEXT,
    profile_raw TEXT,
    business_info_raw TEXT,
    jobs_page_raw TEXT,
    jobs_reported_count_raw TEXT,
    extra_raw_json TEXT CHECK (extra_raw_json IS NULL OR (json_valid(extra_raw_json) AND json_type(extra_raw_json) = 'object')),
    issues_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(issues_json) AND json_type(issues_json) = 'array'),
    captured_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_records (
    source_id TEXT NOT NULL PRIMARY KEY,
    channel TEXT NOT NULL,
    acquisition_context TEXT NOT NULL,
    source_ref TEXT,
    identity_key TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    source_url TEXT,
    company_source_id TEXT REFERENCES company_source_records(company_source_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES raw_artifacts(artifact_id) ON DELETE RESTRICT,
    locator TEXT,
    title_raw TEXT,
    company_name_raw TEXT,
    location_raw TEXT,
    salary_raw TEXT,
    requirements_raw TEXT,
    description_raw TEXT,
    extra_raw_json TEXT CHECK (extra_raw_json IS NULL OR (json_valid(extra_raw_json) AND json_type(extra_raw_json) = 'object')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    issues_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(issues_json) AND json_type(issues_json) = 'array'),
    captured_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_conflicts (
    conflict_id TEXT NOT NULL PRIMARY KEY,
    source_id TEXT REFERENCES source_records(source_id) ON DELETE RESTRICT,
    company_source_id TEXT REFERENCES company_source_records(company_source_id) ON DELETE RESTRICT,
    old_artifact_id TEXT NOT NULL REFERENCES raw_artifacts(artifact_id) ON DELETE RESTRICT,
    new_artifact_id TEXT NOT NULL REFERENCES raw_artifacts(artifact_id) ON DELETE RESTRICT,
    old_snapshot_json TEXT NOT NULL CHECK (json_valid(old_snapshot_json) AND json_type(old_snapshot_json) = 'object'),
    new_snapshot_json TEXT NOT NULL CHECK (json_valid(new_snapshot_json) AND json_type(new_snapshot_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','keep_old','keep_new','superseded')),
    decision_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((source_id IS NOT NULL) <> (company_source_id IS NOT NULL)),
    CHECK ((status IN ('keep_old','keep_new') AND decision_at IS NOT NULL)
        OR (status IN ('pending','superseded') AND decision_at IS NULL))
);

CREATE TABLE collection_tasks (
    task_id TEXT NOT NULL PRIMARY KEY,
    task_type TEXT NOT NULL CHECK (task_type IN ('collect','import','recollect','company_collect')),
    request_json TEXT NOT NULL CHECK (json_valid(request_json) AND json_type(request_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready','running','finished','failed','stopped')),
    summary_json TEXT CHECK (summary_json IS NULL OR (json_valid(summary_json) AND json_type(summary_json) = 'object')),
    stop_requested INTEGER NOT NULL DEFAULT 0 CHECK (stop_requested IN (0,1)),
    error_text TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE collection_results (
    result_id TEXT NOT NULL PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES collection_tasks(task_id) ON DELETE RESTRICT,
    item_index INTEGER NOT NULL CHECK (item_index >= 0),
    city TEXT,
    channel TEXT NOT NULL,
    acquisition_context TEXT NOT NULL,
    result_type TEXT NOT NULL DEFAULT 'pending' CHECK (result_type IN
        ('pending','source_created','company_created','unchanged','material_saved','conflict_pending','empty','failed')),
    source_id TEXT REFERENCES source_records(source_id) ON DELETE RESTRICT,
    company_source_id TEXT REFERENCES company_source_records(company_source_id) ON DELETE RESTRICT,
    conflict_id TEXT REFERENCES source_conflicts(conflict_id) ON DELETE RESTRICT,
    artifact_id TEXT REFERENCES raw_artifacts(artifact_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object'),
    error_code TEXT,
    error_text TEXT,
    captured_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, item_index),
    CHECK (result_type IN ('pending','failed','empty') OR artifact_id IS NOT NULL),
    CHECK (result_type <> 'source_created' OR source_id IS NOT NULL),
    CHECK (result_type <> 'company_created' OR company_source_id IS NOT NULL),
    CHECK (result_type <> 'conflict_pending' OR conflict_id IS NOT NULL)
);

CREATE UNIQUE INDEX uq_source_identity ON source_records(channel, identity_key) WHERE identity_key IS NOT NULL;
CREATE UNIQUE INDEX uq_company_identity ON company_source_records(channel, identity_key) WHERE identity_key IS NOT NULL;
CREATE UNIQUE INDEX uq_conflict_job_pending ON source_conflicts(source_id) WHERE status = 'pending' AND source_id IS NOT NULL;
CREATE UNIQUE INDEX uq_conflict_company_pending ON source_conflicts(company_source_id) WHERE status = 'pending' AND company_source_id IS NOT NULL;
CREATE INDEX ix_source_company ON source_records(company_source_id);
CREATE INDEX ix_source_list ON source_records(channel, status, source_id);
CREATE INDEX ix_company_list ON company_source_records(channel, company_source_id);
CREATE INDEX ix_results_source ON collection_results(source_id, task_id);
CREATE INDEX ix_results_company_task ON collection_results(company_source_id, task_id, acquisition_context);
CREATE INDEX ix_conflicts_pending ON source_conflicts(status, created_at, conflict_id);
"""


def _split_statements(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._depth = 0
        self._conn: sqlite3.Connection | None = None

    # -- 连接 ---------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA busy_timeout = 5000")
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
                self._conn = None

    # -- 事务 ---------------------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """短事务。嵌套调用加入外层事务，不单独提交。"""
        with self._lock:
            conn = self.connect()
            if self._depth > 0:
                self._depth += 1
                try:
                    yield conn
                finally:
                    self._depth -= 1
                return
            conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield conn
            except BaseException:
                self._depth = 0
                conn.execute("ROLLBACK")
                raise
            else:
                self._depth = 0
                conn.execute("COMMIT")

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connect().execute(sql, params).fetchone()

    # -- 初始化 -------------------------------------------------------------

    def initialize(self) -> None:
        """建库或校验既有库。

        新库按六表 DDL 建，user_version=110。既有库若非本版 schema 则拒绝启动，
        不做删表重建或覆盖初始化（SRC-R-114）。
        """
        conn = self.connect()
        with self._lock:
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='module_meta'"
            ).fetchone()
            if exists is None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_statements(_DDL_SCRIPT):
                        conn.execute(statement)
                    conn.execute("PRAGMA user_version = 110")
                    conn.execute(
                        "INSERT INTO module_meta (singleton, schema_version, catalog_generation)"
                        " VALUES (1, ?, ?)",
                        (SCHEMA_VERSION, f"gen_{uuid.uuid4().hex}"),
                    )
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return

            # 既有库：校验 schema 代次。非本版直接拒绝，提示先备份。
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if user_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库 schema 版本 {user_version} 与本程序支持的 {SCHEMA_VERSION} 不一致，"
                    "拒绝自动升级；请先备份数据库与 artifacts 目录，再按迁移流程处理（SRC-R-114）"
                )


class MetaRepo:
    """module_meta：结构版本、目录代次（删除时更换的随机代次）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT schema_version, catalog_generation FROM module_meta WHERE singleton = 1"
        )
        if row is None:
            raise RuntimeError("module_meta 未初始化")
        return dict(row)

    def catalog_generation(self) -> str:
        return str(self.get()["catalog_generation"])

    def bump_generation(self, conn: sqlite3.Connection) -> str:
        generation = f"gen_{uuid.uuid4().hex}"
        conn.execute(
            "UPDATE module_meta SET catalog_generation = ? WHERE singleton = 1", (generation,)
        )
        return generation
