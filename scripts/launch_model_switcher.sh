#!/usr/bin/env bash
# Codex 模型切换器 MCP server 启动器（macOS / Linux）
# 底层复用现成轮子 codex-token 的 switcher.py（不重复实现切换逻辑）
# switcher.py 的位置按以下顺序自动探测：
#   1) 环境变量 CODEX_SWITCHER_HOME
#   2) 插件目录下的 vendor/
#   3) 插件同级的 codex-token/
set -e
cd "$(dirname "$0")/.."

: "${CODEX_HOME:=$HOME/.codex}"
export CODEX_HOME

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import tomllib" >/dev/null 2>&1; then PY="$cand"; break; fi
  fi
done
if [ -z "$PY" ]; then
  echo "[model-switcher] 找不到 Python 3.11+ 解释器（需要 tomllib）" >&2
  echo "[model-switcher] 请安装 Python 后重试，或用 doctor.py --fix 自动修" >&2
  exit 1
fi

exec "$PY" ./server.py
