#!/usr/bin/env bash
# Codex 模型切换器 —— 一键安装（macOS / Linux）
#
#   ./install.sh
#
# 做四件事：
#   1. 检查 Python 3.11+（要 tomllib）
#   2. 装 mcp 依赖
#   3. 把本仓库注册成 Codex 本地 marketplace 并安装插件
#   4. 跑 doctor.py --fix 自动体检 + 修环境
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "==> model-switcher 安装"
echo "    仓库：$REPO"

# 1) Python
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import tomllib" >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "!! 找不到 Python 3.11+（需要 tomllib）。请先安装 Python 3.11 或更高版本。" >&2
  exit 1
fi
echo "    Python：$($PY -V 2>&1)"

# 2) 依赖
if ! "$PY" -c "import mcp" >/dev/null 2>&1; then
  echo "==> 安装 mcp 依赖"
  "$PY" -m pip install --quiet mcp
fi

# 3) 注册 marketplace + 安装插件
CODEX_BIN="${CODEX_BIN:-codex}"
if command -v "$CODEX_BIN" >/dev/null 2>&1; then
  echo "==> 注册 marketplace"
  "$CODEX_BIN" plugin marketplace add "$REPO" || true
  echo "==> 安装插件"
  "$CODEX_BIN" plugin add model-switcher@codex-model-switcher || true
else
  echo "!! 找不到 codex 命令。请手动执行："
  echo "     codex plugin marketplace add \"$REPO\""
  echo "     codex plugin add model-switcher@codex-model-switcher"
fi

# 4) 体检 + 自动修
echo "==> 环境体检"
"$PY" "$REPO/doctor.py" --fix

cat <<'EOF'

安装完成。

下一步：
  1. 完全退出并重开 Codex 桌面版
  2. 在对话里说「用 deepseek，key 是 sk-xxx」即可接入
  3. 接入后建议再跑一次 doctor.py 确认

EOF
