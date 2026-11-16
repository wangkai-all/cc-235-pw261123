#!/usr/bin/env bash
# 启动采集工作台（仅依赖 Python 3 标准库）
set -euo pipefail
cd "$(dirname "$0")/app"
exec python3 server.py
