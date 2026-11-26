"""渠道引用规范化、身份键与通用时间/哈希工具。

对齐 02 规格 1.1.1 的边界：02 只存原文、不提取语义。

- 身份键：identity_key 只由渠道的可靠引用决定（ref:/url: 前缀），不参与
  语义归并；SRC-R-101 规定"有明确渠道 ID 用 ref:，否则用稳定详情链接 url:，
  都不可靠时为 null，不得通过删参数猜测相同"。
- 正文：SRC-R-108 只统一换行与外围空白，不改写正文、不删词、不做薪资/
  城市等语义等价判断。因此这里没有 canonical_text / parse_salary /
  diff_fields 之类的东西——它们属于 04 语义分析或已被规格否决。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None, field_name: str = "time") -> str | None:
    """接受 ISO 8601；无时区按 UTC 处理（本机单人使用）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是字符串")
    text = value.strip()
    if not text:
        return None
    match = _TIME_RE.match(text)
    if not match:
        raise ValueError(f"{field_name} 不是合法的 ISO 8601 时间: {value}")
    year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
    second = int(match.group(6) or 0)
    tz = match.group(7)
    if tz is None or tz == "Z":
        tzinfo = timezone.utc
    else:
        from datetime import timedelta

        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset_minutes = sign * (int(digits[:2]) * 60 + int(digits[2:4]))
        tzinfo = timezone(timedelta(minutes=offset_minutes))
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tzinfo)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u00ad"), None)


def normalize_text(value: str) -> str:
    """NFKC + 去零宽 + 统一换行与外围空白；用于展示与原文槽位取值。

    SRC-R-108：只统一换行和外围空白，不改写正文或删词。
    """
    text = unicodedata.normalize("NFKC", value or "")
    text = text.translate(_ZERO_WIDTH)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 城市
# ---------------------------------------------------------------------------

# BOSS 搜索页 city 数字编码。常见城市，未逐一验证；未知城市请直接传数字编码。
CITY_CODES: dict[str, str] = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "西安": "101110100",
    "苏州": "101190400",
    "天津": "101030100",
    "重庆": "101040100",
    "长沙": "101250100",
    "郑州": "101180100",
    "东莞": "101281600",
    "佛山": "101280800",
    "合肥": "101220100",
    "福州": "101230100",
    "厦门": "101230200",
    "济南": "101120100",
    "青岛": "101120200",
    "宁波": "101210400",
    "无锡": "101190200",
    "昆明": "101290100",
    "大连": "101070200",
    "沈阳": "101070100",
    "哈尔滨": "101050100",
    "长春": "101060100",
    "石家庄": "101090100",
    "南昌": "101240100",
    "贵阳": "101260100",
    "南宁": "101300100",
    "太原": "101100100",
    "兰州": "101160100",
    "乌鲁木齐": "101130100",
    "海口": "101310100",
    "珠海": "101280700",
    "中山": "101281700",
    "惠州": "101280300",
    "温州": "101210700",
}


def resolve_city_code(value: str) -> tuple[str, str]:
    """城市名或数字编码 -> (code, 规范化城市名)。未知城市抛 ValueError。"""
    text = normalize_text(value or "")
    if not text:
        raise ValueError("城市不能为空")
    if text.isdigit():
        for name, code in CITY_CODES.items():
            if code == text:
                return code, name
        return text, text
    name = text[:-1] if text.endswith("市") and len(text) > 2 else text
    if name in CITY_CODES:
        return CITY_CODES[name], name
    raise ValueError(f"未收录的城市: {value}；请直接传 BOSS 城市数字编码")


# ---------------------------------------------------------------------------
# 来源身份（SRC-R-101）
# ---------------------------------------------------------------------------

DEFAULT_NORMALIZATION_VERSION = "generic-1"


def normalize_source_ref(channel: str, source_ref: str | None) -> str | None:
    """按渠道声明规范化可靠引用；未知参数默认保留。"""
    if not source_ref:
        return None
    ref = normalize_text(source_ref)
    if not ref:
        return None
    if channel == "boss":
        return _normalize_boss_ref(ref)
    parts = urlsplit(ref)
    if parts.scheme in {"http", "https"} and parts.netloc:
        host = parts.netloc.lower()
        if host.endswith(":80") and parts.scheme == "http":
            host = host[:-3]
        if host.endswith(":443") and parts.scheme == "https":
            host = host[:-4]
        path = parts.path or "/"
        query = f"?{parts.query}" if parts.query else ""
        fragment = f"#{parts.fragment}" if parts.fragment else ""
        return f"{parts.scheme}://{host}{path}{query}{fragment}"
    return ref


def _normalize_boss_ref(ref: str) -> str:
    """BOSS 职位链接：路径里的加密职位 ID 才稳定。

    查询串（securityId 等）每次会话变化，不构成身份；渠道自己声明该规则，
    不使用通用规则统一删除。
    """
    parts = urlsplit(ref)
    if parts.netloc and "zhipin.com" in parts.netloc.lower():
        match = re.search(r"/job_detail/([^/?#]+?)\.html", parts.path)
        if match:
            return f"https://www.zhipin.com/job_detail/{match.group(1)}.html"
        match = re.search(r"/job_detail/([^/?#]+)", parts.path)
        if match:
            return f"https://www.zhipin.com/job_detail/{match.group(1)}.html"
    return ref


def normalization_version(channel: str) -> str:
    return "boss-1" if channel == "boss" else DEFAULT_NORMALIZATION_VERSION


def identity_key(channel: str, source_ref: str | None) -> str | None:
    """按 SRC-R-101 生成身份键。

    有可靠引用时用 url: 前缀（详情链接）；渠道提供明确 ID 时调用方应传
    ref: 前缀。这里只在 normalize_source_ref 得到的稳定链接上统一加 url:。
    不可靠引用返回 None，交 03 语义归并。
    """
    normalized = normalize_source_ref(channel, source_ref)
    if not normalized:
        return None
    # 已带 ref:/url: 前缀则原样返回，否则按链接补 url:
    if normalized.startswith(("ref:", "url:")):
        return f"{channel}|{normalized}"
    return f"{channel}|url:{normalized}"


def ref_identity_key(channel: str, channel_ref_id: str) -> str:
    """渠道提供明确职位/公司 ID 时的身份键（ref: 前缀，优先级高于 url:）。"""
    return f"{channel}|ref:{normalize_text(channel_ref_id)}"
