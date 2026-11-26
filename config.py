"""端口、目录、渠道和模型配置。

配置读取顺序：显式参数 > 环境变量 JOBJOBVEIN_CONFIG > <project>/config/jobjobvein.toml。
文件不存在时使用内置默认值，不因此拒绝启动；模型默认走规则提取，本机 Ollama 需显式开启。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "jobjobvein.toml"

MiB = 1024 * 1024


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8770


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path = PROJECT_ROOT / "data" / "jobjobvein"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "source.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"


@dataclass(frozen=True)
class LimitsConfig:
    upload_max_bytes: int = 20 * MiB
    import_request_max_bytes: int = 40 * MiB
    import_items_max: int = 100
    list_page_size_max: int = 100
    keyword_min_chars: int = 1
    # 按公司取在招职位时一次最多建多少条来源。公司职位页每页 15 条，
    # 默认 10 是"够看清这家公司在招什么"的量，不为凑数翻页。
    company_job_max: int = 20
    # 单页首屏可见卡片上限（SRC-R-107，与采集总量无关）
    max_cards: int = 20


@dataclass(frozen=True)
class BudgetConfig:
    """采集预算。首版沿用文档 4.1 的待审核建议值，上限可配置。"""

    default_pages: int = 3
    default_candidates: int = 100
    default_timeout_seconds: int = 300
    max_pages: int = 30
    max_candidates: int = 500
    max_timeout_seconds: int = 900
    channel_timeout_seconds: int = 30


@dataclass(frozen=True)
class BossConfig:
    enabled: bool = True
    debug_address: str = "127.0.0.1:9222"
    page_load_seconds: float = 3.0
    click_delay_seconds: tuple[float, float] = (0.8, 1.5)
    page_delay_seconds: tuple[float, float] = (1.5, 2.5)
    # 详情页之间的随机间隔：每条详情页是一次独立的页面访问，零间隔连续打开
    # 是整套流程里最像机器的行为（实测 45 条 47 秒，约 1 秒 1 次）。
    detail_delay_seconds: tuple[float, float] = (1.0, 2.0)
    scroll_pixels: int = 500
    detail_wait_seconds: float = 1.0


@dataclass(frozen=True)
class SearchConfig:
    """采集默认搜索条件：前端预填、后端兜底都用这一份，避免两处口径不一。"""

    default_keyword: str = "ai"
    default_cities: tuple[str, ...] = ("成都",)


@dataclass(frozen=True)
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    boss: BossConfig = field(default_factory=BossConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    config_path: Path | None = None


def _get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    node: Any = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _get_pair(data: dict[str, Any], *keys: str, default: tuple[float, float]) -> tuple[float, float]:
    """读取 [下限, 上限] 形式的间隔配置；形状不对就退回默认，不静默改成单值。"""
    raw = _get(data, *keys, default=None)
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return default
    try:
        low, high = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return default
    if low < 0 or high < low:
        return default
    return (low, high)


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else Path(os.environ.get("JOBJOBVEIN_CONFIG", "")).expanduser() if os.environ.get("JOBJOBVEIN_CONFIG") else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if cfg_path and cfg_path.is_file():
        with cfg_path.open("rb") as handle:
            data = tomllib.load(handle)

    server = replace(
        ServerConfig(),
        host=_get(data, "server", "host", default=ServerConfig.host),
        port=int(_get(data, "server", "port", default=ServerConfig.port)),
    )
    data_dir_value = _get(data, "storage", "data_dir")
    if data_dir_value:
        candidate = Path(str(data_dir_value)).expanduser()
        data_dir = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
    else:
        data_dir = StorageConfig.data_dir
    storage = StorageConfig(data_dir=data_dir)
    limits = replace(
        LimitsConfig(),
        upload_max_bytes=int(_get(data, "limits", "upload_max_bytes", default=LimitsConfig.upload_max_bytes)),
        import_request_max_bytes=int(_get(data, "limits", "import_request_max_bytes", default=LimitsConfig.import_request_max_bytes)),
        import_items_max=int(_get(data, "limits", "import_items_max", default=LimitsConfig.import_items_max)),
        list_page_size_max=int(_get(data, "limits", "list_page_size_max", default=LimitsConfig.list_page_size_max)),
        company_job_max=int(_get(data, "limits", "company_job_max", default=LimitsConfig.company_job_max)),
        max_cards=int(_get(data, "limits", "max_cards", default=LimitsConfig.max_cards)),
    )
    budget = replace(
        BudgetConfig(),
        default_pages=int(_get(data, "budget", "default_pages", default=BudgetConfig.default_pages)),
        default_candidates=int(_get(data, "budget", "default_candidates", default=BudgetConfig.default_candidates)),
        default_timeout_seconds=int(_get(data, "budget", "default_timeout_seconds", default=BudgetConfig.default_timeout_seconds)),
        max_pages=int(_get(data, "budget", "max_pages", default=BudgetConfig.max_pages)),
        max_candidates=int(_get(data, "budget", "max_candidates", default=BudgetConfig.max_candidates)),
        max_timeout_seconds=int(_get(data, "budget", "max_timeout_seconds", default=BudgetConfig.max_timeout_seconds)),
        channel_timeout_seconds=int(_get(data, "budget", "channel_timeout_seconds", default=BudgetConfig.channel_timeout_seconds)),
    )
    boss = replace(
        BossConfig(),
        enabled=bool(_get(data, "channels", "boss", "enabled", default=BossConfig.enabled)),
        debug_address=str(_get(data, "channels", "boss", "debug_address", default=BossConfig.debug_address)),
        page_load_seconds=float(_get(data, "channels", "boss", "page_load_seconds", default=BossConfig.page_load_seconds)),
        page_delay_seconds=_get_pair(
            data,
            "channels",
            "boss",
            "page_delay_seconds",
            default=BossConfig.page_delay_seconds,
        ),
        detail_delay_seconds=_get_pair(
            data,
            "channels",
            "boss",
            "detail_delay_seconds",
            default=BossConfig.detail_delay_seconds,
        ),
        detail_wait_seconds=float(_get(data, "channels", "boss", "detail_wait_seconds", default=BossConfig.detail_wait_seconds)),
    )
    raw_cities = _get(data, "search", "default_cities", default=None)
    if isinstance(raw_cities, list):
        default_cities = tuple(str(c).strip() for c in raw_cities if str(c).strip())
    else:
        default_cities = SearchConfig.default_cities
    search = SearchConfig(
        default_keyword=str(_get(data, "search", "default_keyword", default=SearchConfig.default_keyword)),
        default_cities=default_cities or SearchConfig.default_cities,
    )
    return Config(
        server=server,
        storage=storage,
        limits=limits,
        budget=budget,
        boss=boss,
        search=search,
        config_path=cfg_path if cfg_path and cfg_path.is_file() else None,
    )
