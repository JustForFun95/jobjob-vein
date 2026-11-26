"""启动入口：python -m jobjobvein [--config 路径] [--host] [--port]。"""

from __future__ import annotations

import argparse
from dataclasses import replace

import uvicorn

from .api import create_app
from .config import load_config
from .service import JobjobveinService


def main() -> None:
    parser = argparse.ArgumentParser(description="职位来源与采集模块（本机）")
    parser.add_argument("--config", help="配置文件路径，默认 project/config/jobjobvein.toml")
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8770")
    parser.add_argument("--reset-interrupted", action="store_true", help="仅把遗留 running 操作标为 interrupted 后退出")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.host:
        config = replace(config, server=replace(config.server, host=args.host))
    if args.port:
        config = replace(config, server=replace(config.server, port=args.port))

    service = JobjobveinService(config)
    if args.reset_interrupted:
        print("已把遗留 running 操作标记为 interrupted；未自动执行。")
        return

    app = create_app(service)
    print(f"数据目录: {config.storage.data_dir}")
    print(f"配置文件: {config.config_path or '(内置默认值)'}")
    print(f"管理员页面: http://{config.server.host}:{config.server.port}/")
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


if __name__ == "__main__":
    main()
