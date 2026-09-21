#!/bin/sh
# 抓一个省：取地区树 → 生成分市脚本 → 逐市抓。令牌只输一次，撞额度/中断后重跑同一条命令会续上。
#   scripts/crawl_province.sh 340000
set -eu
[ $# -eq 1 ] || { echo "用法：scripts/crawl_province.sh <省编码>，如 340000" >&2; exit 1; }
PROV=$1
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

if [ -z "${I_ESG_TOKEN:-}" ]; then
  printf '青绿令牌（不回显）：' >&2
  # shellcheck disable=SC2039
  stty -echo 2>/dev/null || true
  read -r I_ESG_TOKEN
  stty echo 2>/dev/null || true
  echo >&2
  export I_ESG_TOKEN
fi

TREE=$(ls trees/"$PROV"_*.json 2>/dev/null | head -1 || true)
if [ -z "$TREE" ]; then
  "$PY" crawl_jiangsu_eia.py --province "$PROV" --tree-only
  TREE=$(ls trees/"$PROV"_*.json | head -1)
fi
"$PY" gen_city_scripts.py --tree "$TREE"

PREFIX=$(printf %s "$PROV" | cut -c1-2)
for f in cities/crawl_"$PREFIX"*.py; do
  "$PY" "$f" || exit 1
done
echo "全部市抓完：raw/$PREFIX*.json"
