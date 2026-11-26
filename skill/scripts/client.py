# -*- coding: utf-8 -*-
"""jobjobvein HTTP 客户端：封装采集服务的全部接口。

只用 Python 标准库，不装第三方包。所有请求发往本机回环地址。
输出统一为 JSON，便于 agent 解析。

用法见 SKILL.md；接口参数见 references/api.md。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8770"
PREFIX = "/api/jobjobvein"
TERMINAL = {"finished", "failed", "stopped"}


def _request(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    url = base.rstrip("/") + PREFIX + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        sys.stderr.write(f"HTTP {exc.code}: {raw}\n")
        raise SystemExit(2)
    except urllib.error.URLError as exc:
        sys.stderr.write(
            f"连不上服务 {base}：{exc.reason}\n"
            "先确认服务已启动（库目录执行 python -m jobjobvein）。\n"
        )
        raise SystemExit(3)
    if not raw:
        return {}
    try:
        envelope = json.loads(raw)
    except ValueError:
        sys.stderr.write(f"服务返回不是 JSON：{raw[:300]}\n")
        raise SystemExit(4)
    error = envelope.get("error") if isinstance(envelope, dict) else None
    if error:
        sys.stderr.write(
            f"接口错误 [{error.get('category')}/{error.get('error_code')}]: "
            f"{error.get('message')} object_id={error.get('object_id')}\n"
        )
        raise SystemExit(5)
    return envelope.get("data", envelope)


def _emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_ready(args: argparse.Namespace) -> None:
    data = _request(args.base, "GET", "/capabilities")
    boss = next((c for c in data.get("channels", []) if c.get("channel") == "boss"), {})
    verdict = {
        "base": args.base,
        "boss_ready": bool(boss.get("ready")),
        "login_state": boss.get("login_state"),
        "login_state_text": boss.get("login_state_text"),
        "default_search": data.get("default_search"),
        "max_candidates": data.get("limits", {}).get("max_candidates"),
        "can_collect": bool(boss.get("ready")),
    }
    _emit(verdict)
    if not verdict["can_collect"]:
        sys.stderr.write(
            "未就绪：采集需要本机 9222 调试浏览器已登录 BOSS。\n"
            "用库的 scripts/start_all.py 启动浏览器并扫码登录，再调用本命令复查。\n"
        )
        raise SystemExit(1)


def cmd_collect(args: argparse.Namespace) -> None:
    if args.source_url and (args.cities or args.keyword):
        sys.stderr.write("source_url 不能和 cities/keyword 同时用。\n")
        raise SystemExit(2)
    cities = [c.strip() for c in args.cities.split(",") if c.strip()] if args.cities else []
    body: dict[str, Any] = {
        "task_id": args.task_id or f"agent-{uuid.uuid4().hex}",
        "task_type": "collect",
        "channel": args.channel,
        "limits": {"max_candidates": args.max_candidates, "timeout_seconds": args.timeout},
    }
    if args.keyword:
        body["keyword"] = args.keyword
    if cities:
        body["cities"] = cities
    if args.source_url:
        body["source_url"] = args.source_url
    created = _request(args.base, "POST", "/tasks", body)
    _request(args.base, "POST", f"/tasks/{created['task_id']}/run")
    _emit({"task_id": created["task_id"], "status": "running", "request": body})
    if args.wait:
        _wait_until_terminal(args.base, created["task_id"], args.timeout, args.poll)


def _wait_until_terminal(base: str, task_id: str, timeout: int, poll: float) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _request(base, "GET", f"/tasks/{task_id}")
        if last.get("status") in TERMINAL:
            _emit({"status": "finished_waiting", "last": last})
            return
        time.sleep(poll)
    _emit({"status": "timeout", "last": last, "task_id": task_id})
    raise SystemExit(1)


def cmd_wait(args: argparse.Namespace) -> None:
    _wait_until_terminal(args.base, args.task_id, args.timeout, args.poll)


def cmd_task(args: argparse.Namespace) -> None:
    _emit(_request(args.base, "GET", f"/tasks/{args.task_id}"))


def cmd_results(args: argparse.Namespace) -> None:
    q = urllib.parse.urlencode({"page": args.page, "page_size": args.page_size})
    _emit(_request(args.base, "GET", f"/tasks/{args.task_id}/results?{q}"))


def cmd_stop(args: argparse.Namespace) -> None:
    _emit(_request(args.base, "POST", f"/tasks/{args.task_id}/stop", {}))


def cmd_sources(args: argparse.Namespace) -> None:
    q = urllib.parse.urlencode(
        {k: v for k, v in vars(args).items() if k not in ("base", "cmd") and v is not None}
    )
    path = f"/sources?{q}" if q else "/sources"
    _emit(_request(args.base, "GET", path))


def cmd_source(args: argparse.Namespace) -> None:
    _emit(_request(args.base, "GET", f"/sources/{args.source_id}"))


def cmd_conflicts(args: argparse.Namespace) -> None:
    q = urllib.parse.urlencode({"status": args.status}) if args.status else ""
    _emit(_request(args.base, "GET", f"/conflicts?{q}"))


def cmd_conflict(args: argparse.Namespace) -> None:
    _emit(_request(args.base, "GET", f"/conflicts/{args.conflict_id}"))


def cmd_decide(args: argparse.Namespace) -> None:
    if args.decision not in ("keep_old", "keep_new", "keep_merge"):
        sys.stderr.write("decision 只能是 keep_old / keep_new / keep_merge。\n")
        raise SystemExit(2)
    body: dict[str, Any] = {"decision": args.decision, "expected_revision": args.revision}
    if args.fields:
        body["fields"] = json.loads(args.fields)
    if args.decision == "keep_merge" and "fields" not in body:
        sys.stderr.write("keep_merge 必须给 --fields（字段级选择映射）。\n")
        raise SystemExit(2)
    _emit(_request(args.base, "POST", f"/conflicts/{args.conflict_id}/decision", body))


def cmd_companies(args: argparse.Namespace) -> None:
    _emit(_request(args.base, "GET", "/company-sources"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="jobjobvein 采集服务 HTTP 客户端（标准库实现）")
    p.add_argument("--base", default=DEFAULT_BASE, help=f"服务地址，默认 {DEFAULT_BASE}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ready", help="检查能否采集（BOSS 就绪/登录态）").set_defaults(fn=cmd_ready)

    c = sub.add_parser("collect", help="发起一次采集")
    c.add_argument("--keyword", help="搜索关键词；留空用服务默认")
    c.add_argument("--cities", help="城市，逗号分隔；留空用服务默认")
    c.add_argument("--channel", default="boss")
    c.add_argument("--max-candidates", type=int, default=100, help="候选条数 1–500")
    c.add_argument("--timeout", type=int, default=600, help="超时秒 10–600")
    c.add_argument("--source-url", help="单条职位详情 URL，与关键词/城市互斥")
    c.add_argument("--task-id", help="自定义 task_id；留空自动生成")
    c.add_argument("--wait", action="store_true", help="等待任务到终态")
    c.add_argument("--poll", type=float, default=1.5, help="轮询间隔秒，配合 --wait")
    c.set_defaults(fn=cmd_collect)

    w = sub.add_parser("wait", help="等待任务到终态")
    w.add_argument("task_id")
    w.add_argument("--timeout", type=int, default=600)
    w.add_argument("--poll", type=float, default=1.5)
    w.set_defaults(fn=cmd_wait)

    for name, fn, label in (
        ("task", cmd_task, "查任务状态"),
        ("results", cmd_results, "查任务结果"),
        ("stop", cmd_stop, "停止任务"),
    ):
        s = sub.add_parser(name, help=label)
        s.add_argument("task_id")
        if name == "results":
            s.add_argument("--page", type=int, default=1)
            s.add_argument("--page-size", type=int, default=20)
        s.set_defaults(fn=fn)

    s = sub.add_parser("sources", help="列出来源岗位")
    for f in ("channel", "city", "status", "keyword", "salary", "conflict", "sort"):
        s.add_argument(f"--{f}".replace("_", "-"), default=None)
    s.set_defaults(fn=cmd_sources)

    s = sub.add_parser("source", help="查单条来源")
    s.add_argument("source_id")
    s.set_defaults(fn=cmd_source)

    s = sub.add_parser("conflicts", help="列出待裁决冲突")
    s.add_argument("--status", default=None, help="如 pending")
    s.set_defaults(fn=cmd_conflicts)

    s = sub.add_parser("conflict", help="查单条冲突（含字段对比）")
    s.add_argument("conflict_id")
    s.set_defaults(fn=cmd_conflict)

    s = sub.add_parser("decide", help="裁决冲突")
    s.add_argument("conflict_id")
    s.add_argument("--decision", required=True, help="keep_old / keep_new / keep_merge")
    s.add_argument("--revision", type=int, required=True, help="期望版本号（防并发覆盖）")
    s.add_argument("--fields", help='字段级选择 JSON，如 {"title_raw":"new"}')
    s.set_defaults(fn=cmd_decide)

    sub.add_parser("companies", help="列出公司来源").set_defaults(fn=cmd_companies)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
