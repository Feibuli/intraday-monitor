#!/usr/bin/env bash
# Stock Monitor Launcher (macOS / Linux)
# 选择最合适的 Python 解释器并运行 stock_monitor.py
#   1) 环境变量 WB_PYTHON（显式覆盖，最高优先级）
#   2) WorkBuddy 内置 Python：$HOME/.workbuddy/binaries/python/versions/<最新版本>/python（已含 requests/schedule，开箱即用）
#   3) PATH 中的 python3 / python
#   4) 以上都没有 → 给出可读错误
set -u

resolve_python() {
  if [ -n "${WB_PYTHON:-}" ]; then
    echo "$WB_PYTHON"; return
  fi
  local wb_root="$HOME/.workbuddy/binaries/python/versions"
  if [ -d "$wb_root" ]; then
    local latest
    latest=$(ls -1 "$wb_root" 2>/dev/null | sort -V | tail -1)
    if [ -n "$latest" ] && [ -x "$wb_root/$latest/python" ]; then
      echo "$wb_root/$latest/python"; return
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"; return
  fi
  if command -v python >/dev/null 2>&1; then
    echo "python"; return
  fi
  echo ""
}

PY=$(resolve_python)
if [ -z "$PY" ]; then
  echo "未找到 Python。请安装 Python 3 并加入 PATH，或设置环境变量 WB_PYTHON 指向 python 可执行文件。" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

echo "使用 Python: $PY"
echo "工作目录:   $SCRIPT_DIR"
echo "=============================="
echo "  Stock Monitor"
echo "=============================="
echo ""

# 自动检测并安装第三方依赖（小白友好，无需手动 pip install）
echo "[1/2] 检查依赖 (requests, schedule) ..."
if ! "$PY" -c "import requests, schedule" >/dev/null 2>&1; then
  echo "  缺少依赖，正在自动安装（首次需联网，稍候）..."
  if ! "$PY" -m pip install -r "$SCRIPT_DIR/requirements.txt"; then
    echo "依赖自动安装失败。请手动运行： $PY -m pip install -r requirements.txt" >&2
    exit 1
  fi
  echo "  依赖安装完成。"
else
  echo "  依赖已就绪。"
fi

echo ""
echo "[2/2] 启动监控 ..."
"$PY" "$SCRIPT_DIR/stock_monitor.py"
EXIT_CODE=$?
echo ""
echo "Monitor stopped."
if [ "$EXIT_CODE" -ne 0 ]; then
  echo "警告: 监控进程异常退出（退出码 $EXIT_CODE）。请确认本机已安装 Python 3，或设置环境变量 WB_PYTHON 指向 python 可执行文件。" >&2
fi
