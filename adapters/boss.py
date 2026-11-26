"""首版 BOSS 读取实现（连接本机已登录的调试浏览器）。

页面读取部分复用 scripts/boss_scraper_10pages.py 的选择器与流程（见 boss_dom），
本文件只负责：预算与停止、逐份交付证据、验证码/掉登录的明确报错、可靠引用。

登录、验证码等问题明确返回，不阻塞在脚本 input()。是否真正接通以真实渠道验证结果为准。

对齐 02 规格 1.1.1：适配器产出 AdapterOutput（channel/acquisition_context/
entity_type/artifact_bytes/observed_fields/field_names/...），observed_fields 只含
*_raw 原文槽位，不拆 min/max、不抽 skills。
"""

from __future__ import annotations

import json
import random
import socket
import time
import urllib.request
from typing import Any, Callable, Iterator

from ..config import BossConfig
from ..normalization import CITY_CODES, identity_key, normalize_text, resolve_city_code, utc_now_iso
from . import boss_dom
from .base import (
    AdapterError,
    AdapterOutput,
    Budget,
    CTX_COMPANY_JOBS,
    CTX_COMPANY_PROFILE,
    CTX_JOB_DETAIL,
    CTX_JOB_SEARCH,
    ENTITY_COMPANY,
    ENTITY_JOB,
    ENTITY_PAGE,
)

# 登录态缓存时间：页面刷新和多次调用能力时不必反复开标签页探测。
_LOGIN_CACHE_SECONDS = 60.0

# 等一个列表接口响应的上限。页面自己发请求，慢一点是正常的；
# 超过这个时间还没响应，通常说明被重定向到登录/验证页了。
_LIST_API_TIMEOUT = 25.0

# 详情页等薪资渲染的上限。薪资由前端渲染，固定 sleep 只能靠猜。
_DETAIL_READY_TIMEOUT = 12.0


def _packet_payload(packet: Any) -> dict[str, Any] | None:
    """把旁听到的网络包转成 JSON 字典；拿不到就返回 None。"""
    if packet is None:
        return None
    try:
        body = packet.response.body
    except Exception:  # noqa: BLE001 - 包已被回收或读取失败
        return None
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, str)):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        text = text.strip()
        if not text.startswith("{"):
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _wait_list_payload(tab: Any, timeout: float) -> dict[str, Any] | None:
    """等下一个列表接口响应；跳过非 JSON 或与列表无关的包。"""
    try:
        packet = tab.listen.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 - 监听被中断
        return None
    return _packet_payload(packet)


class BossAdapter:
    channel = "boss"

    def __init__(self, config: BossConfig, normalization_version: str = "boss-1") -> None:
        self.config = config
        self._normalization_version = normalization_version
        self._login_cache: tuple[float, dict[str, Any]] | None = None

    # -- 能力 ---------------------------------------------------------------

    def capabilities(self, *, fresh: bool = False) -> dict[str, Any]:
        if not self.config.enabled:
            login = self._info(boss_dom.LOGIN_STATE_UNREACHABLE, "BOSS 渠道未启用")
            port_open = False
        elif not self._port_open() or not self._debug_endpoint_alive():
            login = self._info(boss_dom.LOGIN_STATE_UNREACHABLE)
            port_open = False
        else:
            port_open = True
            login = self.login_state(fresh=fresh)
        ready = bool(self.config.enabled and port_open and login["state"] == boss_dom.LOGIN_STATE_LOGGED_IN)
        prerequisites = [
            f"本机浏览器以 --remote-debugging-port 启动并保持打开（{self.config.debug_address}）",
            "在同一浏览器中登录 BOSS 直聘",
            "遇到人机验证需人工完成后重新发起",
        ]
        return {
            "channel": "boss",
            "ready": ready,
            "actions": ["collect", "recollect", "company_collect"] if self.config.enabled else [],
            "criteria_schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "minLength": 1, "description": "搜索关键词"},
                    "city_code": {
                        "type": "string",
                        "minLength": 1,
                        "description": "BOSS 城市数字编码；常见城市名也可",
                    },
                },
                "required": ["keyword", "city_code"],
                "additionalProperties": False,
            },
            "scope_schema": {
                "type": "object",
                "properties": {
                    "max_pages": {"type": "integer", "minimum": 1},
                    "max_candidates": {"type": "integer", "minimum": 1},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                "additionalProperties": False,
            },
            "default_scope": {"max_pages": 3, "max_candidates": 50, "timeout_seconds": 300},
            "limits": {"debug_address": self.config.debug_address},
            "prerequisites": prerequisites,
            "login_state": login["state"],
            "login_state_text": login["detail"],
            "normalization_version": self._normalization_version,
        }

    def _port_open(self) -> bool:
        host, _, port = self.config.debug_address.partition(":")
        try:
            with socket.create_connection((host or "127.0.0.1", int(port or 9222)), timeout=0.4):
                return True
        except OSError:
            return False

    def _debug_endpoint_alive(self) -> bool:
        """确认这个端口真的是 Chrome 调试端口。

        只看 TCP 能连不够：DrissionPage 在连不上时会自己拉起一个浏览器，
        那样用户以为在复用已登录窗口，实际连的是个没有登录态的新实例。
        """
        host, _, port = self.config.debug_address.partition(":")
        url = f"http://{host or '127.0.0.1'}:{port or 9222}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001 - 连不上就是没有
            return False
        return bool(payload.get("webSocketDebuggerUrl"))

    # -- 登录态 -------------------------------------------------------------

    def login_state(self, *, fresh: bool = False) -> dict[str, Any]:
        """探测 BOSS 登录态；结果缓存 60 秒，避免每次读能力都开页面。"""
        now = time.monotonic()
        if (
            not fresh
            and self._login_cache is not None
            and now - self._login_cache[0] < _LOGIN_CACHE_SECONDS
        ):
            return self._login_cache[1]
        info = self._probe_login_state()
        self._login_cache = (now, info)
        return info

    def _probe_login_state(self) -> dict[str, Any]:
        if not self.config.enabled:
            return self._info(boss_dom.LOGIN_STATE_UNREACHABLE, "BOSS 渠道未启用")
        if not self._debug_endpoint_alive():
            return self._info(boss_dom.LOGIN_STATE_UNREACHABLE)
        try:
            from DrissionPage import ChromiumPage
        except ImportError:  # pragma: no cover - 依赖缺失时明确说明
            return self._info(boss_dom.LOGIN_STATE_UNKNOWN, "缺少 DrissionPage 依赖，无法探测登录态")
        try:
            browser = ChromiumPage(addr_or_opts=self.config.debug_address)
        except Exception as exc:  # noqa: BLE001
            return self._info(boss_dom.LOGIN_STATE_UNKNOWN, f"连接调试浏览器失败：{exc}")

        existing = self._find_zhipin_tab(browser)
        if existing is not None:
            return self._inspect(existing)

        try:
            tab = browser.new_tab(boss_dom.HOME_URL)
        except Exception as exc:  # noqa: BLE001
            return self._info(boss_dom.LOGIN_STATE_UNKNOWN, f"探测登录态失败：{exc}")
        try:
            time.sleep(self.config.page_load_seconds)
            return self._inspect(tab)
        finally:
            try:
                tab.close()
            except Exception:  # noqa: BLE001 - 关不掉不影响结论
                pass

    @staticmethod
    def _find_zhipin_tab(browser: Any) -> Any:
        try:
            tabs = browser.get_tabs()
        except Exception:  # noqa: BLE001
            return None
        for tab in tabs:
            try:
                url = tab.url or ""
            except Exception:  # noqa: BLE001
                continue
            if "zhipin.com" in url:
                return tab
        return None

    def _inspect(self, scope: Any) -> dict[str, Any]:
        url = getattr(scope, "url", "") or ""
        try:
            html = scope.html or ""
        except Exception:  # noqa: BLE001
            html = ""
        hits = max(
            boss_dom.count_cards(scope),
            boss_dom.safe_count(scope, boss_dom.DETAIL_TEXT),
            boss_dom.safe_count(scope, boss_dom.CONTENT_LINK),
        )
        # 登录提示以可见文本为准：BOSS 已登录时源码里仍保留"登录/注册"隐藏 DOM，
        # 只搜源码会把已登录误判成未登录。读取失败则退回 None，让判定函数用源码。
        try:
            visible_text = scope.run_js("return document.body.innerText") or ""
        except Exception:  # noqa: BLE001 - 取不到可见文本不影响其他判据
            visible_text = None
        state = boss_dom.detect_login_state(
            url, html, content_hits=hits, visible_text=visible_text
        )
        return self._info(state)

    @staticmethod
    def _info(state: str, detail: str | None = None) -> dict[str, Any]:
        return {"state": state, "detail": detail or boss_dom.LOGIN_STATE_TEXT.get(state, "")}

    def _require_login(self, scope: Any) -> None:
        info = self._inspect(scope)
        self._login_cache = (time.monotonic(), info)
        if info["state"] == boss_dom.LOGIN_STATE_CAPTCHA:
            raise AdapterError(f"BOSS 要求人机验证。{self._login_help()}", kind="login_required")
        if info["state"] == boss_dom.LOGIN_STATE_REQUIRED:
            raise AdapterError(f"BOSS 需要登录后才能查看岗位。{self._login_help()}", kind="login_required")

    def _login_help(self) -> str:
        port = self.config.debug_address.rsplit(":", 1)[-1] or "9222"
        return (
            f"请在本机带调试端口的浏览器（{self.config.debug_address}）里打开 https://www.zhipin.com "
            "完成登录后重新发起。若该浏览器尚未启动，先关闭所有 Chrome 窗口，再执行：\n"
            f'chrome.exe --remote-debugging-port={port} --user-data-dir="<项目目录>/data/chrome-profile"\n'
            "首次用该目录需登录一次，登录态只留在这个目录，不写入数据库、不进入仓库。"
        )

    # -- 采集 ---------------------------------------------------------------

    def acquire(
        self,
        criteria: dict[str, Any],
        scope: dict[str, Any],
        budget: Budget,
        stop_requested: Callable[[], bool] | None = None,
    ) -> Iterator[AdapterOutput]:
        keyword = normalize_text(str(criteria.get("keyword") or ""))
        city_raw = str(criteria.get("city_code") or "").strip()
        if not keyword:
            raise AdapterError("缺少关键词 keyword", kind="missing_field")
        if not city_raw:
            raise AdapterError("缺少城市 city_code", kind="missing_field")
        try:
            city_code, _ = resolve_city_code(city_raw)
        except ValueError as exc:
            raise AdapterError(str(exc), kind="missing_field") from exc

        browser = self._connect()
        refs, collect_stop, collect_complete = self._collect_refs(
            browser, boss_dom.search_url(keyword, city_code), budget, stop_requested
        )
        if not refs:
            raise AdapterError(
                "搜索页没有取到任何职位链接：可能未登录、关键词无结果，或页面结构又有变化"
            )

        detail_stop = yield from self._emit_details(browser, refs, budget, stop_requested)
        budget.stop_reason = detail_stop or collect_stop or "exhausted"
        budget.capture_complete = budget.stop_reason == "exhausted" and collect_complete

    def _collect_refs(
        self,
        browser: Any,
        search_url: str,
        budget: Budget,
        stop_requested: Callable[[], bool] | None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        try:
            # DrissionPage 4.1.1.x 的 new_tab 不接受显式 "about:blank"（_new_tab_by_js
            # 会当成非法 URL 抛 IncorrectURLError）；不传 url 时它自己就会建空白标签。
            tab = browser.new_tab(background=True)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"打开搜索页失败: {exc}") from exc

        refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        stop_reason: str | None = None
        complete = False
        try:
            boss_dom.prepare_background_tab(tab)
            tab.listen.start(boss_dom.LIST_API_PATH)
            try:
                tab.get(search_url)
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(f"打开搜索页失败: {exc}") from exc

            for round_no in range(1, budget.max_pages + 1):
                if stop_requested and stop_requested():
                    stop_reason = "stopped"
                    break
                if budget.time_left() <= 0:
                    stop_reason = "timeout"
                    break

                if round_no > 1:
                    if stop_requested and stop_requested():
                        stop_reason = "stopped"
                        break
                    boss_dom.scroll_to_bottom(tab)

                payload = _wait_list_payload(tab, _LIST_API_TIMEOUT)
                budget.pages_read = round_no

                if payload is None:
                    if round_no == 1:
                        raise AdapterError(
                            "未捕获到搜索接口响应：页面可能被重定向到登录/验证页，"
                            f"或 {_LIST_API_TIMEOUT} 秒内没有发出列表请求"
                        )
                    stop_reason = "exhausted"
                    complete = True
                    break

                if round_no == 1:
                    reason = boss_dom.api_risk_reason(payload)
                    if reason:
                        raise AdapterError(
                            f"{reason}。{self._login_help()}", kind="login_required"
                        )

                jobs, meta = boss_dom.map_api_jobs(payload)
                for job in jobs:
                    href = job.get("source_ref")
                    if not href or href in seen:
                        continue
                    seen.add(href)
                    refs.append({"href": href, "hint": job, "page": round_no})
                    if len(refs) >= budget.max_candidates:
                        break

                if len(refs) >= budget.max_candidates:
                    stop_reason = "limit_reached"
                    break
                if meta.get("has_more") is False:
                    stop_reason = "exhausted"
                    complete = True
                    break
                self._pause(self.config.page_delay_seconds, budget, stop_requested)
                if stop_requested and stop_requested():
                    stop_reason = "stopped"
                    break
            else:
                stop_reason = "page_limit"
        finally:
            try:
                tab.listen.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                tab.close()
            except Exception:  # noqa: BLE001 - 关不掉不影响结果
                pass
        return refs, stop_reason, complete

    def _emit_details(
        self,
        browser: Any,
        refs: list[dict[str, Any]],
        budget: Budget,
        stop_requested: Callable[[], bool] | None,
    ) -> Iterator[AdapterOutput]:
        """逐条打开详情页取原文，产出 AdapterOutput。返回停止原因。"""
        try:
            tab = browser.new_tab()
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"打开详情页标签失败: {exc}") from exc
        boss_dom.prepare_background_tab(tab)

        try:
            for index, ref in enumerate(refs):
                if stop_requested and stop_requested():
                    return "stopped"
                if budget.candidates_read >= budget.max_candidates:
                    return "limit_reached"
                if budget.time_left() <= 0:
                    return "timeout"
                if index:
                    self._pause(self.config.detail_delay_seconds, budget, stop_requested)
                    if stop_requested and stop_requested():
                        return "stopped"
                    if budget.time_left() <= 0:
                        return "timeout"
                budget.candidates_read += 1
                output = self._read_detail(tab, ref, budget)
                if output is not None:
                    yield output
            return None
        finally:
            try:
                tab.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _pause(
        window: tuple[float, float],
        budget: Any,
        stop_requested: Callable[[], bool] | None = None,
    ) -> None:
        """等待一段随机间隔；期间每 0.2 秒检查停止标志，点了停止立即返回。

        整段 time.sleep 无法被打断，会导致"点停止半天没反应"——等待期间
        必须留出检查点，否则停止请求要等整段间隔 + 一条详情页加载完才生效。
        """
        low, high = window
        if high <= 0:
            return
        delay = random.uniform(max(low, 0.0), high)
        remaining = budget.time_left()
        if remaining > 0:
            delay = min(delay, remaining)
        deadline = time.monotonic() + max(delay, 0)
        while True:
            if stop_requested and stop_requested():
                return
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.2, left))

    def acquire_one(self, source_ref: str) -> AdapterOutput:
        """打开单个职位详情页，返回一条 AdapterOutput（entity_type=job）。

        原文是页面 HTML 字节；observed_fields 是 *_raw 原文槽位。
        """
        page = self._connect()
        try:
            page.get(source_ref)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"打开职位页失败: {exc}") from exc
        fields = boss_dom.read_detail_when_ready(page, timeout=_DETAIL_READY_TIMEOUT)
        self._require_login(page)
        if not fields.get("title_raw"):
            raise AdapterError("页面未取到职位内容，可能已下线或需要登录")
        return self._to_output(
            source_ref=source_ref,
            source_url=source_ref,
            fields=fields,
            page=page,
            entity_type=ENTITY_JOB,
            context=CTX_JOB_DETAIL,
            company_ref=fields.get("company_ref"),
        )

    # -- 按公司取在招职位 ---------------------------------------------------

    def acquire_company(self, company_ref: str, limit: int) -> list[AdapterOutput]:
        """取一家公司的资料和在招职位卡片，返回一组 AdapterOutput。

        两次页面请求，**不打开任何职位详情页**：

        1. 公司页 ``/gongsi/{id}.html`` —— 公司资料（entity_type=company）；
        2. 公司职位页 ``/gongsi/job/{id}.html`` —— 服务端渲染的卡片列表，每页 15 条
           （entity_type=page + 每张卡片 entity_type=job）。

        只取第一页并按 ``limit`` 截断。卡片没有 JD 正文，这一点由调用方写进来源
        issues，不能含糊成"没有招聘要求"。
        """
        jobs_url = boss_dom.company_jobs_url(company_ref)
        if not jobs_url:
            raise AdapterError(
                f"公司引用无法解析出品牌 ID：{company_ref}。需要形如 "
                "https://www.zhipin.com/gongsi/{id}.html 的链接",
                kind="invalid_argument",
            )
        page = self._connect()
        outputs: list[AdapterOutput] = []
        acquired_at = utc_now_iso()

        # 1. 公司页
        try:
            page.get(company_ref)
        except Exception as exc:  # noqa: BLE001 - 公司页失败不阻断在招职位
            outputs.append(
                self._failed_output(company_ref, f"公司页打开失败：{exc}", ENTITY_COMPANY, CTX_COMPANY_PROFILE)
            )
        else:
            raw = boss_dom.read_company_page(page)
            fields = boss_dom.map_company(raw, company_ref) if raw else {}
            if not fields.get("company_name_raw") and not fields.get("legal_name_raw"):
                fields["_no_profile"] = True
            outputs.append(
                self._to_output(
                    source_ref=company_ref,
                    company_ref=company_ref,
                    source_url=company_ref,
                    fields=fields,
                    page=page,
                    entity_type=ENTITY_COMPANY,
                    context=CTX_COMPANY_PROFILE,
                    acquired_at=acquired_at,
                )
            )

        # 2. 公司职位页
        try:
            page.get(jobs_url)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"打开公司职位页失败: {exc}") from exc
        self._require_login(page)

        raw_cards = boss_dom.read_company_job_cards(page)
        if not raw_cards:
            raise AdapterError(
                "公司职位页没有读到任何职位卡片：可能该公司当前没有在招职位，或页面结构有变化"
            )

        # 招聘页作为 page 结果单独产出。page_summary 对齐 02 规格 4.4：
        # reported_count_raw 取公司页实际显示的"N 在招职位"，不猜；viewport 报告实际视口。
        reported_count_raw = ""
        for out in outputs:
            if out.entity_type == ENTITY_COMPANY:
                reported_count_raw = str(out.observed_fields.get("jobs_reported_count_raw") or "")
                break
        meta = boss_dom.read_company_jobs_meta(page)
        viewport = meta.get("viewport") if isinstance(meta.get("viewport"), dict) else {}
        visible_text = "\n".join(
            text for text in (
                str(card.get("text") or "").strip() for card in raw_cards[:limit]
            ) if text
        )
        outputs.append(
            self._to_output(
                source_ref=jobs_url,
                company_ref=company_ref,
                source_url=jobs_url,
                fields={"jobs_page_raw": visible_text},
                page=page,
                entity_type=ENTITY_PAGE,
                context=CTX_COMPANY_JOBS,
                acquired_at=acquired_at,
                page_summary={
                    "collected_count": min(len(raw_cards), limit),
                    "reported_count_raw": reported_count_raw or None,
                    "complete": len(raw_cards) <= limit,
                    "stop_reason": "limit_reached" if len(raw_cards) > limit else "first_screen",
                    "viewport": {
                        "width": int(viewport.get("width") or 1365),
                        "height": int(viewport.get("height") or 900),
                    },
                },
            )
        )

        company_name = ""
        # 从公司页输出里取公司名
        for out in outputs:
            if out.entity_type == ENTITY_COMPANY:
                company_name = (
                    out.observed_fields.get("company_name_raw")
                    or out.observed_fields.get("legal_name_raw")
                    or ""
                )
                break

        for card in raw_cards:
            if sum(1 for o in outputs if o.entity_type == ENTITY_JOB) >= limit:
                break
            fields = boss_dom.map_company_job_card(card, company_ref, company_name)
            ref = fields.get("source_ref")
            if not ref or not fields.get("title_raw"):
                continue
            outputs.append(
                AdapterOutput(
                    channel=self.channel,
                    acquisition_context=CTX_COMPANY_JOBS,
                    entity_type=ENTITY_JOB,
                    artifact_bytes=b"",
                    mime_type="text/html",
                    captured_at=acquired_at,
                    field_names=[k for k in fields if k not in ("source_ref", "company_ref")],
                    observed_fields={k: v for k, v in fields.items() if k not in ("source_ref", "company_ref")},
                    source_ref=ref,
                    company_ref=company_ref,
                    source_url=ref,
                    identity_key=identity_key(self.channel, ref),
                    locator=ref,
                    issues=[{"code": "no_description", "message": "列表卡片未读取职位详情正文", "field": "description_raw"}],
                )
            )
        return outputs

    # -- 内部 ---------------------------------------------------------------

    def _connect(self):
        if not self.config.enabled:
            raise AdapterError("BOSS 渠道未启用", kind="unsupported")
        if not self._debug_endpoint_alive():
            raise AdapterError(
                f"调试端口 {self.config.debug_address} 不可用（未打开或不是 Chrome 调试端口）。"
                f"{self._login_help()}"
            )
        try:
            from DrissionPage import ChromiumPage
        except ImportError as exc:  # pragma: no cover - 依赖缺失时明确报错
            raise AdapterError("缺少 DrissionPage 依赖") from exc
        try:
            return ChromiumPage(addr_or_opts=self.config.debug_address)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(
                f"连接调试浏览器失败（{self.config.debug_address}）：{exc}。{self._login_help()}"
            ) from exc

    def _to_output(
        self,
        *,
        source_ref: str,
        source_url: str,
        fields: dict[str, Any],
        page: Any,
        entity_type: str,
        context: str,
        company_ref: str | None = None,
        acquired_at: str | None = None,
        page_summary: dict[str, Any] | None = None,
    ) -> AdapterOutput:
        """把已读取的字段和当前页面打包成 AdapterOutput。

        observed_fields 只保留 *_raw 与 extra_raw；source_ref/company_ref 是定位，
        不进 observed_fields。
        """
        field_names = [k for k in fields if k.endswith("_raw")]
        observed = {k: v for k, v in fields.items() if k.endswith("_raw")}
        issues: list[dict[str, Any]] = []
        if not observed.get("salary_raw"):
            issues.append({"code": "no_salary", "message": "未取到薪资（面议或字段缺失）", "field": "salary_raw"})
        if entity_type == ENTITY_JOB and not observed.get("description_raw"):
            issues.append({"code": "no_description", "message": "详情页未取到 JD 正文", "field": "description_raw"})
        if fields.get("jd_truncated"):
            issues.append({"code": "jd_truncated", "message": "JD 被登录墙截断（只保存了可见部分）", "field": "description_raw"})
            self._login_cache = (
                time.monotonic(),
                self._info(boss_dom.LOGIN_STATE_REQUIRED, "详情页 JD 被登录墙截断"),
            )
        if fields.get("_no_profile"):
            issues.append({"code": "no_company_profile", "message": "公司页没读到公司资料（可能改版或未登录）", "field": None})

        try:
            html = page.html or ""
        except Exception:  # noqa: BLE001
            html = ""
        return AdapterOutput(
            channel=self.channel,
            acquisition_context=context,
            entity_type=entity_type,
            artifact_bytes=(html or "").encode("utf-8"),
            mime_type="text/html",
            captured_at=acquired_at or utc_now_iso(),
            field_names=field_names,
            observed_fields=observed,
            source_ref=source_ref,
            company_ref=company_ref,
            source_url=source_url,
            identity_key=identity_key(self.channel, source_ref),
            locator=None,
            issues=issues,
            page_summary=page_summary,
        )

    def _failed_output(
        self, source_ref: str, reason: str, entity_type: str, context: str
    ) -> AdapterOutput:
        return AdapterOutput(
            channel=self.channel,
            acquisition_context=context,
            entity_type=entity_type,
            artifact_bytes=b"",
            mime_type="text/html",
            captured_at=utc_now_iso(),
            field_names=[],
            observed_fields={},
            source_ref=source_ref,
            source_url=source_ref,
            identity_key=identity_key(self.channel, source_ref),
            issues=[{"code": "channel_failed", "message": reason, "field": None}],
        )

    def _read_detail(self, tab: Any, ref: dict[str, Any], budget: Budget) -> AdapterOutput | None:
        href = ref["href"]
        hint = dict(ref.get("hint") or {})
        try:
            tab.get(href)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响其他候选
            return self._failed_output(href, f"打开详情页失败: {exc}", ENTITY_JOB, CTX_JOB_DETAIL)

        try:
            fields = boss_dom.read_detail_when_ready(tab, timeout=_DETAIL_READY_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            return self._failed_output(href, f"读取详情页失败: {exc}", ENTITY_JOB, CTX_JOB_DETAIL)

        if not fields.get("title_raw") and not hint.get("title_raw"):
            info = self._inspect(tab)
            self._login_cache = (time.monotonic(), info)
            if info["state"] in (boss_dom.LOGIN_STATE_REQUIRED, boss_dom.LOGIN_STATE_CAPTCHA):
                raise AdapterError(
                    f"采集途中登录态失效（{info['detail']}）。{self._login_help()}",
                    kind="login_required",
                )
            return self._failed_output(href, "详情页未取到职位内容，可能已下线", ENTITY_JOB, CTX_JOB_DETAIL)

        # 列表接口的原文槽位与详情页合并；详情页读到的覆盖接口值，接口独有的保留。
        merged = {**hint, **fields}
        merged["source_ref"] = href
        return self._to_output(
            source_ref=href,
            source_url=href,
            fields=merged,
            page=tab,
            entity_type=ENTITY_JOB,
            context=CTX_JOB_DETAIL,
            company_ref=merged.get("company_ref"),
        )


def supported_cities() -> dict[str, str]:
    return dict(CITY_CODES)
