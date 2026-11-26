"""BOSS 页面读取原语：全项目唯一的实现，适配器与导出脚本共用。

只做"从页面读出文字"，不做预算、停止、落库或字段判定。

**边界：以下结论只对 BOSS 成立。** 被动旁听、焦点仿真、``innerText`` 避水印都是
针对 zhipin.com 当前页面结构与风控的实测结果，不是通用做法——其他渠道很可能
直接打公开接口或解析静态 HTML 更稳，也可能完全不介意程序注入的 XHR。新增渠道
必须先实测自己的取数手段再写适配器，不得照抄本文件；本文件也不得被 BOSS 以外的
适配器引用（``manual.py`` 即不引用）。

设计取舍（每条都有真实页面实测依据，改动前请先复验）：

1. 列表页**不解析 DOM**。卡片薪资是字体加密（``\\ue0xx`` 私用区码点），真值不在
   文本里；而页面自身发出的 ``joblist.json`` 里 ``salaryDesc`` 是明文，还额外给出
   HR 姓名、区/商圈、技能、福利、公司规模。所以列表改走被动旁听该接口
   （见 ``boss.py::_collect_refs``），本文件只做字段映射。
2. 取正文**必须用 JS ``innerText``**。水印塞在 ``display: none`` 的随机 class
   ``span`` 里（``<span class="PtfabtMp">来自BOSS直聘</span>``，class 每次随机），
   ``innerText`` 按渲染取文本，隐藏节点天然不进；``textContent``（DrissionPage
   元素 ``.text`` 的行为）会把隐藏文字读出来，把词切断入库。
   这一点与本机 boss-scraper 库的 ``EXTRACT_DETAIL_JS`` 一致。
3. **一次 JS 取全部字段**（``DETAIL_EXTRACT_JS``）。逐字段 ``ele()`` 每次都是一次
   CDP 往返，一条详情页要十几次；合并成一次 JS 后往返只剩 1 次。同样是该库的做法。
4. 未登录时 JD 会被截断在句子中间，末尾挂 ``登录查看完整内容``。必须识别并标记
   ``jd_truncated``，不能把半截正文当完整数据（库用 ``DETAIL_LOGIN_MARKER`` 直接把
   这类页面判为登录墙，这一点比"读到了就存"更严谨）。

所有选择器常量统一带 ``css:`` 前缀：DrissionPage 对 ``a.job-name`` 这类复合选择器
会误判定位方式，显式声明 css 才能稳定命中。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote

from ..normalization import normalize_source_ref, normalize_text

# -- 搜索结果页卡片 ---------------------------------------------------------
# 列表字段不走 DOM（见模块说明第 1 条），这里只保留卡片容器用于登录态探测：
# 有卡片 = 这一页正常打开了。旧版容器保留兼容，两版不会同时命中。
CARD = "css:li.job-card-box"
CARD_LEGACY = "css:.job-card-wrapper"
CARDS = (CARD, CARD_LEGACY)
# "页面上确实有职位内容"的通用证据：详情链接。搜索页、推荐页、详情页都有，
# 登录墙页面没有。卡容器只在搜索页成立，用它单独判登录态会把推荐页误判成 unknown。
CONTENT_LINK = 'css:a[href*="/job_detail/"]'

# -- 详情页字段 -------------------------------------------------------------
TITLE = "css:.job-name"  # 详情页兜底标题
SALARY = "css:.salary"  # 详情页兜底薪资

DETAIL_TITLE = "css:.info-primary .name h1"
DETAIL_TITLE_FALLBACK = "css:.info-primary h1"
DETAIL_SALARY = "css:.info-primary .salary"
DETAIL_CITY = "css:.info-primary .text-city"
DETAIL_EXP = "css:.info-primary .text-experiece"  # BOSS 自身拼写，不是笔误
DETAIL_DEGREE = "css:.info-primary .text-degree"
DETAIL_COMPANY = "css:.sider-company .company-info a"  # title 属性是公司名
DETAIL_COMPANY_FALLBACK = "css:.company-info .name"
DETAIL_BOSS_NAME = "css:.job-boss-info .name"
DETAIL_BOSS_ATTR = "css:.job-boss-info .boss-info-attr"
DETAIL_SECTION = "css:.job-detail-section"
DETAIL_TEXT = "css:.job-sec-text"

# 一次 JS 取回详情页全部字段。
#
# 为什么要合并：DrissionPage 每次 ele() 都是一次 CDP 往返，一条详情页要取
# 标题/薪资/城市/经验/学历/公司/HR/正文共 8 个字段，逐个读就是 8~16 次往返。
# 换成一次 JS 后只剩 1 次，单条耗时能压掉一半以上。思路与本机 boss-scraper 库的
# EXTRACT_DETAIL_JS 相同。
#
# 为什么用 innerText 而不是 textContent / 元素 .text：见模块说明第 2 条，
# 隐藏水印 span 只有 innerText 能天然跳过。
DETAIL_EXTRACT_JS = """
(function(){
  function it(sel){
    var e = document.querySelector(sel);
    return e ? (e.innerText || '') : '';
  }
  function at(sel, attr){
    var e = document.querySelector(sel);
    return e ? (e.getAttribute(attr) || '') : '';
  }
  var jd = document.querySelector('.job-sec-text') || document.querySelector('.job-detail-section');
  var companyLink = document.querySelector('.sider-company .company-info a');
  return JSON.stringify({
    title: it('.info-primary .name h1') || it('.info-primary h1') || it('.job-name'),
    salary: it('.info-primary .salary') || it('.salary'),
    city: it('.info-primary .text-city'),
    experience: it('.info-primary .text-experiece'),
    education: it('.info-primary .text-degree'),
    company: at('.sider-company .company-info a', 'title') || it('.company-info .name'),
    company_href: companyLink ? (companyLink.getAttribute('href') || '') : '',
    // 02 规格 3.4：职位页侧栏的公司规模、行业、融资阶段等原文照存，
    // 不在这里结构化猜测（列表接口已有 brandScaleName 等结构化原文）。
    company_sidebar: (function(){
      var e = document.querySelector('.sider-company');
      return e ? (e.innerText || '').trim() : '';
    })(),
    recruiter: it('.job-boss-info .name'),
    boss_attr: it('.job-boss-info .boss-info-attr'),
    jd: jd ? (jd.innerText || '') : ''
  });
})()
"""

# 搜索页自身发出的列表接口。只旁听、不注入（注入的同步 XHR 会被风控识别为
# code 37「环境存在异常」，接口照常返回列表但把 salaryDesc 全部清空）。
LIST_API_PATH = "/wapi/zpgeek/search/joblist.json"

# JD 正文里被插入的防抓水印，会把词切断（"负来自BOSS直聘责"），必须剔除。
# 顺序敏感：必须从长到短删，先把「来自BOSS直聘」整段删掉，否则先删「直聘」会留下
# 一个孤零零的「来自」。
#
# 注意这只是兜底：主路径用 innerText 读正文，隐藏水印天然读不到（见模块说明第 2 条）。
# 这里防的是 BOSS 改用 visibility:hidden / clip-path 之类 innerText 也会读到的隐藏手法。
_WATERMARK_TOKENS = ("来自BOSS直聘", "BOSS直聘", "kanzhun", "直聘")

# "boss" 不做全局删除：JD 里可能出现正常的英文词，全局删会误伤。
# 水印的特征是被插进汉字中间（"公司急boss招"），只在这种位置删。
_WATERMARK_BOSS_RE = re.compile(r"(?<=[\u4e00-\u9fa5])boss(?=[\u4e00-\u9fa5])", re.IGNORECASE)

# JD 被登录墙截断时，正文末尾会挂这一行。命中就说明看到的不是全文，
# 不能当完整数据入库（BOSS 对未登录用户只给到截断为止）。
JD_TRUNCATED_MARKER = "登录查看完整内容"

# 搜索页用 /web/geek/jobs（复数）：/web/geek/job 会被 301 到这个地址。
SEARCH_URL = "https://www.zhipin.com/web/geek/jobs?query={keyword}&city={city}"
HOME_URL = "https://www.zhipin.com/web/geek/jobs"

# 只认"页面上明确告诉用户要验证"的文案。
# 不要放 captcha / geetest 这类前端库名：登录页源码里本来就有，会把"未登录"误判成"被拦"。
_CAPTCHA_HINTS = ("请完成验证", "请完成安全验证", "安全验证", "滑动验证", "拖动滑块")

# 未登录时 BOSS 会跳到 /web/user/ 或带 ka=header-login，页面出现登录表单/扫码文案。
# 这些只作为"未登录"的证据，不反过来当作已登录的证据。
_LOGIN_URL_HINTS = ("/web/user/", "ka=header-login", "/login")
_LOGIN_PAGE_HINTS = (
    "扫码登录",
    "请登录后",
    "登录/注册",
    "手机号登录",
    "验证码登录",
    "立即登录",
)

LOGIN_STATE_LOGGED_IN = "logged_in"
LOGIN_STATE_REQUIRED = "login_required"
LOGIN_STATE_CAPTCHA = "captcha"
LOGIN_STATE_UNKNOWN = "unknown"
LOGIN_STATE_UNREACHABLE = "unreachable"

LOGIN_STATE_TEXT = {
    LOGIN_STATE_LOGGED_IN: "已登录 BOSS，可采集",
    LOGIN_STATE_REQUIRED: "未登录或登录已过期，需要在浏览器里登录 BOSS",
    LOGIN_STATE_CAPTCHA: "遇到人机验证，需要在浏览器里人工完成后重试",
    LOGIN_STATE_UNKNOWN: "无法确认登录状态（页面未出现岗位内容，也没有登录提示）",
    LOGIN_STATE_UNREACHABLE: "调试端口不可用，浏览器未启动或未开启调试端口",
}

# 招聘负责人名字后面会挂"在线"/"刚刚活跃"之类的状态角标，入库前剥掉。
_BOSS_STATUS_SUFFIX = (
    "刚刚活跃",
    "今日活跃",
    "本周活跃",
    "本月活跃",
    "在线",
    "离线",
)

# 私用区码点：列表页薪资加密后的字符，留着只会污染数据。
_PUA_RE = re.compile(r"[\ue000-\uf8ff]")


def _each(selector: Any):
    """把一个或一组选择器展开成逐个尝试的序列。"""
    if isinstance(selector, (list, tuple)):
        return list(selector)
    return [selector]


def find_one(scope: Any, selector: Any) -> Any:
    """按给定顺序找第一个命中的元素；都找不到返回 None。

    同样显式 ``timeout=0``：这里只做"现在就查一次"，等待由调用方的轮询循环负责。
    用默认超时会让每个缺失字段各花 10 秒——详情页有 9 个字段，JS 路径失效时
    单条就能拖到一分钟以上。
    """
    if scope is None:
        return None
    for item in _each(selector):
        try:
            element = scope.ele(item, timeout=0)
        except Exception:  # noqa: BLE001 - 页面结构变化时按"没读到"处理
            continue
        if element:
            return element
    return None




def text_of(scope: Any, selector: Any) -> str:
    element = find_one(scope, selector)
    if not element:
        return ""
    try:
        return (element.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def attr_of(scope: Any, selector: Any, name: str) -> str:
    element = find_one(scope, selector)
    if not element:
        return ""
    try:
        return (element.attr(name) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def count_cards(scope: Any) -> int:
    """当前列表里已渲染的卡片数（两版选择器取最大值）。

    只用于登录态探测：能数到卡片说明这一页正常打开了。
    """
    return max((safe_count(scope, item) for item in CARDS), default=0)


def sanitize_salary(text: str) -> str:
    """薪资文本清洗：去掉加密私用区字符；剩不下数字就当没读到，不猜。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    cleaned = _PUA_RE.sub("", raw).strip()
    if cleaned in ("面议", "薪资面议"):
        return ""
    if not re.search(r"\d", cleaned):
        return ""
    return cleaned


def clean_boss_name(text: str) -> str:
    """招聘负责人姓名：剥掉"在线/刚刚活跃"这类状态角标。"""
    name = (text or "").strip()
    for suffix in _BOSS_STATUS_SUFFIX:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def strip_watermark(text: str) -> str:
    """剔除 JD 正文里的防抓水印。

    BOSS 会把 ``BOSS直聘`` / ``来自BOSS直聘`` / ``直聘`` / ``kanzhun`` / ``boss``
    直接插进词中间，例如「负来自BOSS直聘责」「公司急boss招」「产品经直聘理」。
    水印本身没有语义，整段删掉后两侧文字自然接回，所以是替换而不是截断。

    顺序不能乱：必须先把「来自BOSS直聘」整段删掉，否则先删 ``boss``/``直聘``
    会留下一个孤零零的「来自」。
    """
    cleaned = text or ""
    for token in _WATERMARK_TOKENS:
        cleaned = re.sub(re.escape(token), "", cleaned, flags=re.IGNORECASE)
    cleaned = _WATERMARK_BOSS_RE.sub("", cleaned)
    return _PUA_RE.sub("", cleaned)


def _join(value: Any, sep: str = " | ") -> str:
    if isinstance(value, (list, tuple)):
        return sep.join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def map_api_job(raw: Any) -> dict[str, Any]:
    """把 joblist.json 的单条记录映射成 1.1.1 的 *_raw 原文槽位。

    列表接口除 JD 之外字段齐全，且薪资是明文；比列表页 DOM 可靠得多
    （DOM 的薪资是加密字体、``.boss-name`` 位置也换成了公司名）。

    这些值是渠道页面直接提供的原文，直接落进 *_raw，不拆 min/max、不抽 skills
    成结构化字段（02 不做语义提取）。extra_raw 放渠道特有的结构化原文
    （HR 姓名、技能、福利、公司规模/阶段/行业）。
    """
    if not isinstance(raw, dict):
        return {}
    job_id = str(raw.get("encryptJobId") or "").strip()
    brand_id = str(raw.get("encryptBrandId") or "").strip()
    location = "·".join(
        part
        for part in (
            str(raw.get("cityName") or "").strip(),
            str(raw.get("areaDistrict") or "").strip(),
            str(raw.get("businessDistrict") or "").strip(),
        )
        if part
    )
    fields: dict[str, Any] = {
        "title_raw": str(raw.get("jobName") or "").strip(),
        "salary_raw": sanitize_salary(str(raw.get("salaryDesc") or "")),
        "company_name_raw": str(raw.get("brandName") or "").strip(),
        "location_raw": location,
        # 经验、学历是页面原生给出的要求，合并进 requirements_raw 原样保留
        "requirements_raw": "·".join(
            p for p in (
                str(raw.get("jobExperience") or "").strip(),
                str(raw.get("jobDegree") or "").strip(),
            ) if p
        ),
        "source_ref": (
            f"https://www.zhipin.com/job_detail/{job_id}.html" if job_id else ""
        ),
    }
    # 渠道特有的结构化原文，不属于标准 *_raw 槽位，进 extra_raw
    extra: dict[str, Any] = {}
    for key, value in (
        ("recruiter", clean_boss_name(str(raw.get("bossName") or ""))),
        ("recruiter_title", str(raw.get("bossTitle") or "").strip()),
        ("skills", _join(raw.get("skills"))),
        ("welfare", _join(raw.get("welfareList"))),
        ("company_scale", str(raw.get("brandScaleName") or "").strip()),
        ("company_stage", str(raw.get("brandStageName") or "").strip()),
        ("company_industry", str(raw.get("brandIndustry") or "").strip()),
        ("active_status", str(raw.get("activeTimeDesc") or "").strip()
            or ("在线" if raw.get("bossOnline") else "")),
    ):
        if value:
            extra[key] = value
    if extra:
        fields["extra_raw"] = extra
    if brand_id:
        fields["company_ref"] = f"https://www.zhipin.com/gongsi/{brand_id}.html"
    return {key: value for key, value in fields.items() if value}


def map_api_jobs(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从捕获到的列表响应里取出职位列表与分页信息。"""
    if not isinstance(payload, dict):
        return [], {}
    data = payload.get("zpData") or {}
    raw_jobs = data.get("jobList")
    if not isinstance(raw_jobs, list):
        return [], {"code": payload.get("code"), "message": payload.get("message")}
    jobs = [job for job in (map_api_job(item) for item in raw_jobs) if job]
    meta = {
        "code": payload.get("code"),
        "message": str(payload.get("message") or ""),
        "has_more": data.get("hasMore"),
        "total": data.get("resCount"),
        "lid": data.get("lid"),
        "raw_count": len(raw_jobs),
    }
    return jobs, meta


def api_risk_reason(payload: Any) -> str:
    """判定列表响应是不是被明确的信号挡住了。

    这里只认两类硬信号：接口返回非 0 的 code，或 message 里明确写了环境异常、
    访问频繁、安全校验。命中任一才中断采集。

    **列表薪资全空不再算失败。** 已登录会话里，BOSS 对后台/新开的搜索页标签
    也可能只返回职位、不返回 ``salaryDesc``，而同一会话的详情页仍读得到薪资。
    把薪资空判成未登录会误杀正常登录的采集——这正是此前反复出现的失败。
    薪资属于原文字段，缺失就让它缺失，由 _to_output 逐条记 no_salary issue；
    登录态另有 _probe_login_state 专门判定，两者不能混为一谈。
    """
    if not isinstance(payload, dict):
        return ""
    code = payload.get("code")
    message = str(payload.get("message") or "")
    if code not in (0, None):
        return f"BOSS 返回 code={code}：{message or '未提供原因'}"
    for hint in ("环境存在异常", "访问频繁", "安全校验"):
        if hint in message:
            return f"BOSS 风控：{message}"
    return ""



def inner_text(page: Any, selector: str) -> str:
    """用 JS ``innerText`` 读文本，而不是元素的 ``.text``。

    这个区别是必须的：BOSS 把水印塞在 ``display: none`` 的随机 class ``span`` 里
    （如 ``<span class="PtfabtMp">来自BOSS直聘</span>``）。``innerText`` 按渲染结果
    取文本，隐藏节点天然不进来；而 ``textContent``（DrissionPage ``.text`` 的行为）
    会把隐藏文字一并读出来，于是水印被切断的词就混进了正文。
    """
    if not hasattr(page, "run_js"):
        return ""
    css = selector[len("css:") :] if selector.startswith("css:") else selector
    script = (
        "var els=document.querySelectorAll(%s);"
        "if(!els.length){return '';}"
        "return els[0].innerText||'';" % repr(css)
    )
    try:
        return (page.run_js(script) or "").strip()
    except Exception:  # noqa: BLE001 - JS 不可用时退回元素读取
        return ""


def _run_return(page: Any, script: str) -> Any:
    """执行一个"要返回值的 JS"，并规避两个会静默失败的坑。

    坑 1：DrissionPage 的 ``run_js`` 把脚本当**函数体**执行，结尾的表达式不算返回
    值——``run_js("1+1")`` 得到 ``None``，必须写成 ``return 1+1``。
    坑 2：``return`` 后紧跟换行会触发 JS 自动分号插入（ASI），
    ``"return\\n(function(){…})()"`` 实际是 ``return;``，返回 ``undefined``。
    所以这里统一去首尾空白后再拼 ``return``。

    JS 执行失败按"这条路径不可用"返回 ``None``，由调用方决定退回哪种读法。
    """
    if not hasattr(page, "run_js"):
        return None
    try:
        return page.run_js("return " + script.strip())
    except Exception:  # noqa: BLE001 - JS 不可用时由调用方兜底
        return None


def read_detail_text(page: Any) -> str:
    """职位详情正文。先取 .job-sec-text（干净），退回整段 .job-detail-section。"""
    for selector in _each(DETAIL_TEXT) + _each(DETAIL_SECTION):
        value = inner_text(page, selector)
        if value:
            return value
    return text_of(page, DETAIL_TEXT) or text_of(page, DETAIL_SECTION)


def _loads_json(raw: Any) -> dict[str, Any]:
    """把 ``run_js`` 的返回值解析成字典；不是合法 JSON 就返回空。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_line(value: Any) -> str:
    """单行字段清洗：去掉首尾空白，把内部换行压成空格。"""
    text = str(value or "").replace("\r", "").strip()
    return re.sub(r"[ \t]*\n+[ \t]*", " ", text).strip()


def extract_detail_fields(page: Any) -> dict[str, Any] | None:
    """一次 JS 取回详情页全部原始字段。

    返回值有三态，调用方要区分：
      * ``None``：这条快速路径不可用（没有 ``run_js``，或 JS 执行失败）→ 应退回元素读取；
      * ``{}``：JS 执行了但页面还没渲染出内容 → 应继续等，不要退回元素读取白费往返；
      * 非空字典：取到了。
    """
    if not hasattr(page, "run_js"):
        return None
    raw = _run_return(page, DETAIL_EXTRACT_JS)
    if raw is None:
        return None
    # DrissionPage 会把合法 JSON 字符串自动解析成 dict；两条路径都要认。
    if isinstance(raw, dict):
        return raw if (raw.get("title") or raw.get("jd")) else {}
    # JS 没按约定返回 JSON（页面结构异常、被替换成错误页等）：宁可退回逐元素
    # 读取，也不要把整条数据判成"没有内容"。
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return None
    data = _loads_json(raw)
    if not data:
        return {}
    if not (str(data.get("title") or "").strip() or str(data.get("jd") or "").strip()):
        return {}
    return data


def normalize_company_ref(href: Any) -> str:
    """把详情页读到的公司链接规范化为统一的 company_ref。

    列表接口产出 ``https://www.zhipin.com/gongsi/{brand_id}.html``；详情页 href
    可能是相对路径、带查询串或带协议，统一提取品牌 ID 后拼成同一形式，
    才能让同公司在不同采集入口命中同一个 identity_key。
    """
    text = _clean_line(href)
    if not text:
        return ""
    match = re.search(r"/?gongsi/(?!job/)([^/?#]+?)\.html", text)
    if match:
        return f"https://www.zhipin.com/gongsi/{match.group(1)}.html"
    return ""


def fields_from_extract(data: dict[str, Any]) -> dict[str, Any]:
    """把 JS 取回的原始文本整理成 *_raw 原文槽位：清洗、去空、标记截断。"""
    fields: dict[str, Any] = {
        "title_raw": _clean_line(data.get("title")),
        "salary_raw": sanitize_salary(_clean_line(data.get("salary"))),
        "location_raw": _clean_line(data.get("city")),
        # 经验、学历是页面原生要求，合并进 requirements_raw 原样保留
        "requirements_raw": "·".join(
            p for p in (_clean_line(data.get("experience")), _clean_line(data.get("education"))) if p
        ),
        "company_name_raw": _clean_line(data.get("company")),
    }
    # 招聘负责人的"公司·职位"一行，只在公司名缺失时兜底。
    boss_attr = _clean_line(data.get("boss_attr"))
    if boss_attr and not fields.get("company_name_raw"):
        fields["company_name_raw"] = boss_attr.split("·")[0].strip()
    # 详情页公司链接：列表接口用 encryptBrandId 拼 /gongsi/{brand}.html，
    # 详情页这里读到的是页面 href，统一规范化成同一种 company_ref 才能命中同一公司。
    company_href = _clean_line(data.get("company_href"))
    if company_href:
        ref = normalize_company_ref(company_href)
        if ref:
            fields["company_ref"] = ref
    recruiter = clean_boss_name(_clean_line(data.get("recruiter")))
    if recruiter:
        fields.setdefault("extra_raw", {})["recruiter"] = recruiter
    # 职位详情页侧栏公司原文（规模/行业/融资阶段等）。列表接口走
    # map_api_job 的 brandScaleName 等结构化槽位；详情页只保留原文，不猜。
    sidebar = normalize_text(str(data.get("company_sidebar") or ""))
    if sidebar:
        fields.setdefault("extra_raw", {})["company_sidebar"] = sidebar

    raw_detail = str(data.get("jd") or "")
    detail = sanitize_detail_text(raw_detail)
    if detail:
        fields["description_raw"] = detail
        if JD_TRUNCATED_MARKER in raw_detail:
            # 只看到截断部分。标记出来，让上层决定是记问题还是拒绝入库，
            # 不静默把半截 JD 当成完整数据。
            fields["jd_truncated"] = True
    return {key: value for key, value in fields.items() if value}


def read_detail_page(page: Any) -> dict[str, Any]:
    """读取当前详情页的字段。

    优先走一次 JS 取全（``extract_detail_fields``）；这条路径不可用时退回
    逐元素读取。页面还没渲染时返回空字典，由调用方继续等。
    """
    fast = extract_detail_fields(page)
    if fast is None:
        return read_detail_by_elements(page)
    if not fast:
        return {}
    return fields_from_extract(fast)


def read_detail_when_ready(
    page: Any,
    timeout: float = 10.0,
    poll: float = 0.15,
    jd_grace: float = 2.5,
) -> dict[str, Any]:
    """等详情页渲染完成后直接返回字段，把"等"和"取"合并成同一次 JS。

    原来分两步：先轮询薪资是否出现（每轮两次 ``ele``），再逐字段读取（8~16 次
    ``ele``）。合并后每轮只发一次 JS，就绪时那次的结果直接就是字段，省掉最后的
    整轮读取。

    正文再多给 ``jd_grace`` 秒：标题和薪资先出来、正文稍后补是常见的，
    刚渲染就抓会拿到半截 JD。
    """
    deadline = time.monotonic() + timeout
    fields: dict[str, Any] = {}
    while time.monotonic() < deadline:
        current = read_detail_page(page)
        if current:
            fields = current
        if fields.get("title_raw") and fields.get("salary_raw"):
            if fields.get("description_raw"):
                return fields
            grace_deadline = time.monotonic() + jd_grace
            while time.monotonic() < grace_deadline:
                time.sleep(poll)
                again = read_detail_page(page)
                if again.get("description_raw"):
                    return again
                if again.get("title_raw"):
                    fields = again
            return fields
        time.sleep(poll)
    return fields


def read_detail_by_elements(page: Any) -> dict[str, Any]:
    """逐元素读取详情页字段（JS 不可用时的兜底路径）。"""
    fields: dict[str, Any] = {
        "title_raw": text_of(page, (DETAIL_TITLE, DETAIL_TITLE_FALLBACK, TITLE)),
        "salary_raw": sanitize_salary(text_of(page, (DETAIL_SALARY, SALARY))),
        "location_raw": text_of(page, DETAIL_CITY),
        "requirements_raw": "·".join(
            p for p in (text_of(page, DETAIL_EXP), text_of(page, DETAIL_DEGREE)) if p
        ),
        "company_name_raw": attr_of(page, DETAIL_COMPANY, "title") or text_of(page, DETAIL_COMPANY_FALLBACK),
    }
    company_href = normalize_company_ref(attr_of(page, DETAIL_COMPANY, "href"))
    if company_href:
        fields["company_ref"] = company_href
    recruiter = clean_boss_name(text_of(page, DETAIL_BOSS_NAME))
    if recruiter:
        fields.setdefault("extra_raw", {})["recruiter"] = recruiter
    sidebar = normalize_text(str(text_of(page, (".sider-company",)) or ""))
    if sidebar:
        fields.setdefault("extra_raw", {})["company_sidebar"] = sidebar
    boss_attr = text_of(page, DETAIL_BOSS_ATTR)
    if boss_attr and not fields.get("company_name_raw"):
        fields["company_name_raw"] = boss_attr.split("·")[0].strip()

    # 正文先把防抓水印剔掉，再交给上层规范化：否则"负来自BOSS直聘责"
    # 会被当成原文入库，后续抽取和比对全是脏的。
    raw_detail = read_detail_text(page)
    detail = sanitize_detail_text(raw_detail)
    if detail:
        fields["description_raw"] = detail
        if JD_TRUNCATED_MARKER in raw_detail:
            fields["jd_truncated"] = True
    return {key: value for key, value in fields.items() if value}


def sanitize_detail_text(text: str) -> str:
    """详情正文清洗：去水印、去截断提示行、压缩空行。"""
    cleaned = strip_watermark(text or "")
    lines = []
    for line in cleaned.replace("\r\n", "\n").splitlines():
        # "登录查看完整内容"是登录墙的提示，不是 JD 内容。
        if line.strip() == JD_TRUNCATED_MARKER:
            continue
        lines.append(line.rstrip())
    joined = "\n".join(lines).strip()
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return re.sub(r"[ \t]{2,}", " ", joined)


def compose_text(fields: dict[str, Any]) -> str:
    """把 *_raw 原文槽位拼成一份可读、可取证的文字摘要（仅用于展示与取证，
    不作为原件——原件是页面 HTML 字节，见 artifacts）。"""
    labels = (
        ("title_raw", "职位"),
        ("company_name_raw", "公司"),
        ("location_raw", "工作地点"),
        ("salary_raw", "薪资"),
        ("requirements_raw", "经验学历"),
    )
    lines = [f"{label}：{fields[key]}" for key, label in labels if fields.get(key)]
    if fields.get("description_raw"):
        lines.append("")
        lines.append(str(fields["description_raw"]))
    return "\n".join(lines)


def is_captcha(html: str | None) -> bool:
    if not html:
        return False
    return any(hint in html for hint in _CAPTCHA_HINTS)


def safe_count(scope: Any, selector: Any) -> int:
    """页面上某类元素的数量；读不到按 0 处理。

    必须显式传 ``timeout=0``：DrissionPage 的 ``eles`` 默认会等元素出现，
    单个选择器落空就白等 10 秒。登录态探测要试 3 个选择器，全部落空时
    会串成 30 秒，而这一步在每次打开页面时都会走一遍。
    """
    if scope is None:
        return 0
    for item in _each(selector):
        try:
            return len(scope.eles(item, timeout=0) or [])
        except Exception:  # noqa: BLE001
            continue
    return 0


def detect_login_state(
    url: str | None,
    html: str | None,
    content_hits: int = 0,
    visible_text: str | None = None,
) -> str:
    """判定 BOSS 登录态，只用页面上能观察到的事实，不靠猜。

    content_hits 是"这类页面本该出现的正文元素数量"：搜索结果页传卡片数，
    职位详情页传详情正文数。

    visible_text 是页面可见文本（innerText），优先于 html 源码判断登录提示：
    BOSS 已登录时源码里仍同时保留"登录/注册"等未登录态 DOM（靠 CSS 隐藏），
    只搜源码会把已登录误判成未登录。可见文本里才会真实反映当前显示的状态。

    顺序很关键：
      1. **明确的未登录证据**（登录页 URL 或可见"登录/注册"）优先——未登录时
         搜索页照样可能渲染职位卡片，卡片数不能盖过这个证据；
      2. 不在登录页却出现明确验证文案，才是真的被风控拦住；
      3. 有正文卡片 = 这一页正常打开了，判已登录；
      4. 都不符合就是 unknown——宁可说不知道，也不假装已登录。
    """
    lowered = (url or "").lower()
    on_login_page = any(hint in lowered for hint in _LOGIN_URL_HINTS)
    if on_login_page:
        return LOGIN_STATE_REQUIRED

    # 登录提示只用可见文本判断；没有可见文本时才退回源码（此时宁可信源码而非卡片）。
    text = visible_text if visible_text is not None else (html or "")
    if any(hint in text for hint in _LOGIN_PAGE_HINTS):
        return LOGIN_STATE_REQUIRED

    if is_captcha(html):
        return LOGIN_STATE_CAPTCHA
    if content_hits > 0:
        return LOGIN_STATE_LOGGED_IN
    return LOGIN_STATE_UNKNOWN


def search_url(keyword: str, city_code: str) -> str:
    """拼搜索页地址。

    关键词必须编码：中文关键词在浏览器里会被自动转义，但带空格、``&``、``#`` 的
    关键词（"AI 产品经理"、"C++"）不编码会把后半截当成新参数，搜出来的结果和
    用户输入的关键词不一致——这种事不会报错，只会悄悄搜错。
    """
    return SEARCH_URL.format(keyword=quote(keyword.strip(), safe=""), city=city_code)


def prepare_background_tab(tab: Any) -> bool:
    """让后台标签页在页面看来"可见且有焦点"。

    后台标签页的 ``document.hidden`` 是 true，BOSS 搜索页的无限滚动加载会被
    这个状态挡住，页面根本不发下一页请求。Chrome 的焦点仿真只改变页面能观察到
    的状态，不激活窗口、不抢用户前台焦点（区别于 ``Target.activateTarget``）。

    CDP 命令不可用时返回 False，调用方可以据此判断"拿不到数据"的原因，
    而不是把空结果当成"没岗位"。
    """
    try:
        tab.run_cdp("Emulation.setFocusEmulationEnabled", enabled=True)
        return True
    except Exception:  # noqa: BLE001 - 老版本浏览器/CDP 不支持
        return False


def scroll_to_bottom(tab: Any) -> bool:
    """滚到底部，触发页面自身的无限滚动加载。"""
    try:
        tab.run_js("window.scrollTo(0, document.body.scrollHeight); void 0;")
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 公司页
# ---------------------------------------------------------------------------
# 边界同上：以下类名只对 BOSS 公司页成立。公司页是服务端渲染的静态 HTML，
# 没有 XHR 可旁听，所以这里**直接解析 DOM**（与职位列表页相反，原因见模块说明第 1 条）。

COMPANY_BRAND_NAME = "css:.company-name"
COMPANY_FULL_NAME = "css:.company-full-name"
COMPANY_BANNER = "css:.company-banner"
COMPANY_STAT = "css:.company-stat"
COMPANY_JOB_CARD = "css:li.job-card-box"
COMPANY_JOBS_PER_PAGE = 15  # 实测：公司职位列表每页 15 条，?page=N 可翻页

# 公司职位列表页（与公司简介页同一 brand_id，路径多一段 /job/）
COMPANY_JOBS_URL = "https://www.zhipin.com/gongsi/job/{brand}.html"

# 一次 JS 取回公司页全部信息。按区块标题定位而不是按类名顺序：公司页各区块
# 用的都是 ``.job-sec``，靠标题区分，类名顺序改版就会错位。
COMPANY_EXTRACT_JS = """
(function(){
  function txt(sel){
    var e = document.querySelector(sel);
    return e ? (e.innerText || '').trim() : '';
  }
  // 按标题找 .job-sec 区块："公司简介""企业文化""工作时间及福利"……
  function sec(title){
    var list = document.querySelectorAll('.job-sec');
    for (var i = 0; i < list.length; i++) {
      var t = (list[i].innerText || '').trim();
      if (t.indexOf(title) === 0) return t;
    }
    return '';
  }
  // 工商信息：``.business-detail-*`` 每项都是"标签： 值"；但「经营范围」不在
  // 这个类里，它在展开后的 ``.col-auto`` 块中，所以两类一起扫，按标签去重。
  var biz = {};
  var nodes = document.querySelectorAll('[class*=business-detail], [class*=col-auto]');
  for (var i = 0; i < nodes.length; i++) {
    var t = (nodes[i].innerText || '').trim();
    var p = t.indexOf('\uff1a');
    if (p < 0 || p > 12) continue;
    var k = t.slice(0, p).trim();
    var v = t.slice(p + 1).trim();
    if (k && !biz[k]) biz[k] = v;
  }
  return JSON.stringify({
    brand_name: (function(){
      var n = txt('.company-name');
      if (n) return n.replace(/\u6536\u85cf$/, '').trim();
      var m = (document.title || '').match(/\u300c(.+?)\u62db\u8058\u300d/);
      return m ? m[1] : '';
    })(),
    full_name: txt('.company-full-name'),
    banner: txt('.company-banner'),
    stat: txt('.company-stat'),
    intro: sec('\u516c\u53f8\u7b80\u4ecb'),
    culture: sec('\u4f01\u4e1a\u6587\u5316'),
    products: sec('\u4ea7\u54c1\u4ecb\u7ecd'),
    address: sec('\u516c\u53f8\u5730\u5740'),
    worktime: sec('\u5de5\u4f5c\u65f6\u95f4\u53ca\u798f\u5229'),
    managers: sec('\u9ad8\u7ba1\u4ecb\u7ecd'),
    business: biz
  });
})()
"""

# 公司职位列表页：每页 15 张卡片，字段全在卡片里，不需要打开详情页。
COMPANY_JOBS_EXTRACT_JS = """
(function(){
  function it(root, sel){
    var e = root.querySelector(sel);
    return e ? (e.innerText || '').trim() : '';
  }
  var vh = window.innerHeight || 900;
  var cards = document.querySelectorAll('li.job-card-box');
  var out = [];
  for (var i = 0; i < cards.length; i++) {
    var c = cards[i];
    // SRC-R-107：只保存当前视口已展示的卡片。与视口相交即算可见；
    // DOM 里已渲染但在屏幕外的卡片不冒充首屏，也不主动滚动补齐。
    var box = c.getBoundingClientRect();
    if (box.top >= vh || box.bottom <= 0) continue;
    var a = c.querySelector('.job-name');
    var tags = [];
    var lis = c.querySelectorAll('ul.tag-list li');
    for (var j = 0; j < lis.length; j++) tags.push((lis[j].innerText || '').trim());
    out.push({
      title: a ? (a.innerText || '').trim() : '',
      href: a ? (a.getAttribute('href') || '') : '',
      salary: it(c, '.job-salary'),
      tags: tags,
      boss: it(c, '.boss-info'),
      area: it(c, '.job-area'),
      text: (c.innerText || '').trim()
    });
  }
  return JSON.stringify(out);
})()
"""

# SRC-R-107：视口以适配器实际报告为准，默认 1365×900 只在无法读取时使用。
COMPANY_JOBS_META_JS = """
(function(){
  return JSON.stringify({
    viewport: {width: window.innerWidth || 0, height: window.innerHeight || 0}
  });
})()
"""

# 融资阶段措辞：只认这些，认不出就留空，不从 banner 里猜。
_STAGES = (
    "不需要融资", "未融资", "天使轮", "A轮", "B轮", "C轮", "D轮", "E轮",
    "战略融资", "已上市",
)
_SCALE_RE = re.compile(r"\d+-\d+人|\d+人以上|少于\d+人|\d+人")
_OPEN_JOBS_RE = re.compile(r"(\d+)\s*在招职位")


def company_jobs_url(brand_ref: str, page: int = 1) -> str:
    """把公司页链接换成公司职位列表链接。

    BOSS 的公司简介和公司职位在同一路径下，只差一段 ``/job/``：
    ``/gongsi/{id}.html`` -> ``/gongsi/job/{id}.html``。品牌 ID 里带 ``~``，
    不能再用通用 URL 规则改写。
    """
    match = re.search(r"/gongsi/(?!job/)([^/?#]+?)\.html", brand_ref or "")
    if not match:
        return ""
    url = COMPANY_JOBS_URL.format(brand=match.group(1))
    return url if page <= 1 else f"{url}?page={page}"


def read_company_page(page: Any) -> dict[str, Any]:
    """读公司页原始信息；JS 不可用时返回空字典（不猜、不逐元素兜底）。"""
    data = _loads(_run_return(page, COMPANY_EXTRACT_JS))
    return data if isinstance(data, dict) else {}


def read_company_job_cards(page: Any) -> list[dict[str, Any]]:
    data = _loads(_run_return(page, COMPANY_JOBS_EXTRACT_JS))
    return data if isinstance(data, list) else []


def read_company_jobs_meta(page: Any) -> dict[str, Any]:
    """招聘页实际视口；读不到时返回空字典，由调用方使用 1365×900 默认值。"""
    data = _loads(_run_return(page, COMPANY_JOBS_META_JS))
    return data if isinstance(data, dict) else {}


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def map_company(raw: dict[str, Any], company_ref: str) -> dict[str, Any]:
    """把公司页原始信息映射成 1.1.1 的 *_raw 原文槽位。

    规模/融资/行业只从 banner 的尾部文字里取，取不到就留空——**不用职位列表
    接口的值来补**：列表接口给的是招聘该职位的那个主体，公司页给的是工商主体，
    两者合并会让人分不清来源。

    落位到 company_source_records 的标准列：
      company_name_raw / legal_name_raw / address_raw / established_raw /
      registered_capital_raw / scale_raw / profile_raw / business_info_raw /
      jobs_reported_count_raw；其余（融资阶段、行业、文化、产品、待遇、高管、
      工商杂项）进 extra_raw。
    """
    banner = normalize_text(str(raw.get("banner") or ""))
    scale = ""
    stage = ""
    industry = ""
    match = _SCALE_RE.search(banner)
    if match:
        scale = match.group(0)
    for item in _STAGES:
        if item in banner:
            stage = item
            break
    tail = banner.rsplit(scale, 1)[-1].strip() if scale else banner
    if stage and tail.startswith(stage):
        tail = tail[len(stage):].strip()
    if tail and tail != banner:
        industry = tail.split()[0] if tail.split() else ""

    stat = normalize_text(str(raw.get("stat") or ""))
    open_match = _OPEN_JOBS_RE.search(stat)

    business = raw.get("business") if isinstance(raw.get("business"), dict) else {}
    biz = {normalize_text(str(k)): normalize_text(str(v)) for k, v in business.items()}

    fields: dict[str, Any] = {
        "company_name_raw": normalize_text(str(raw.get("brand_name") or "")),
        "legal_name_raw": normalize_text(str(raw.get("full_name") or biz.get("企业名称", ""))),
        "scale_raw": scale,
        "company_ref": company_ref,
    }
    # 工商「经营范围」落 business_info_raw
    scope = biz.get("经营范围")
    if scope and scope != "-":
        fields["business_info_raw"] = scope
    # 工商「成立时间」「注册资本」「注册地址」
    for biz_key, col in (
        ("成立时间", "established_raw"),
        ("注册资本", "registered_capital_raw"),
        ("注册地址", "address_raw"),
    ):
        value = biz.get(biz_key)
        if value and value != "-":
            fields[col] = value
    # 公司简介
    intro = normalize_text(str(raw.get("intro") or ""))
    if intro:
        fields["profile_raw"] = intro
    # 页面显示的在招总数原文（"89 在招职位"里的 89 是数字，这里存原文数字字符串）
    if open_match:
        fields["jobs_reported_count_raw"] = open_match.group(1)

    # 其余渠道特有原文进 extra_raw：地址区块、文化、产品、待遇、高管、融资/行业、工商杂项
    extra: dict[str, Any] = {}
    if stage:
        extra["company_stage"] = stage
    if industry:
        extra["company_industry"] = industry
    for key, label in (
        ("address", "公司地址"),
        ("culture", "企业文化"),
        ("products", "产品介绍"),
        ("worktime", "工作时间及福利"),
        ("managers", "高管介绍"),
    ):
        value = normalize_text(str(raw.get(key) or ""))
        if value:
            extra[label] = value
    for label, key in (
        ("法定代表人", "legal_person"),
        ("企业类型", "enterprise_type"),
        ("经营状态", "business_status"),
        ("营业期限", "business_term"),
        ("所属地区", "region"),
        ("统一社会信用代码", "credit_code"),
        ("核准日期", "approved_at"),
        ("曾用名", "former_name"),
        ("登记机关", "registration_authority"),
    ):
        value = biz.get(label)
        # 工商信息里的 "-" 是"没有"，不是"有值叫横杠"
        if value and value != "-":
            extra[key] = value
    if extra:
        fields["extra_raw"] = extra
    return {k: v for k, v in fields.items() if v not in ("", None)}


def _card_location(card: dict[str, Any]) -> str:
    """卡片上的工作地点。

    ``.job-area`` 在部分卡片上是空的（城市直接拼在标题尾部），但卡片文本里
    仍有一行 ``成都 ·双流区 ·华阳``。取那一行而不是"没有地点"——地点是判断
    是否本地岗位的必要信息，静默丢掉会让一条职位看起来全国可投。
    """
    area = normalize_text(str(card.get("area") or ""))
    if area:
        return area
    for line in reversed(str(card.get("text") or "").splitlines()):
        text = line.strip()
        if "·" in text and "招聘" not in text and "K" not in text:
            return re.sub(r"\s*·\s*", "·", text)
    return ""


def map_company_job_card(
    card: dict[str, Any], company_ref: str, company_name: str = ""
) -> dict[str, Any]:
    """把一张公司职位卡片映射成 *_raw 原文槽位。

    列表卡片**没有 JD 正文**，也不去详情页补——这是本路径的既定口径：只取待遇、
    薪资和来源链接。没有正文必须显式记 issue，不能当成"这份职位没有招聘要求"。

    ``company_name`` 来自公司页：卡片本身不写公司名（整页都是同一家），但来源离开
    公司页后还要能看出是哪家，列表、导出、搜索都靠它。
    """
    href = str(card.get("href") or "").strip()
    if href.startswith("/"):
        href = "https://www.zhipin.com" + href
    href = normalize_source_ref("boss", href) or href

    title = normalize_text(str(card.get("title") or ""))
    tags = [normalize_text(str(t)) for t in (card.get("tags") or []) if str(t).strip()]
    boss_text = normalize_text(str(card.get("boss") or ""))
    boss_name = ""
    boss_title = ""
    if boss_text:
        parts = [p.strip() for p in boss_text.split("·") if p.strip()]
        boss_name = clean_boss_name(parts[0]) if parts else ""
        boss_title = parts[1] if len(parts) > 1 else ""

    fields: dict[str, Any] = {
        "title_raw": title,
        "company_name_raw": normalize_text(company_name),
        "salary_raw": sanitize_salary(str(card.get("salary") or "")),
        # 卡片 tags 前两项是经验、学历要求，合并进 requirements_raw 原样保留
        "requirements_raw": "·".join(tags[:2]),
        "location_raw": _card_location(card),
        "company_ref": company_ref,
        "source_ref": href,
    }
    # 招聘负责人是渠道特有的结构化原文，进 extra_raw
    extra: dict[str, Any] = {}
    if boss_name:
        extra["recruiter"] = boss_name
    if boss_title:
        extra["recruiter_title"] = boss_title
    if extra:
        fields["extra_raw"] = extra
    return {k: v for k, v in fields.items() if v not in ("", None)}
