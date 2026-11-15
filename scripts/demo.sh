#!/usr/bin/env bash
# 端到端演示脚本：正常评估（能改善 / 不能改善）+ 暂停断连后连接复用。
# 用法： BASE_URL=http://localhost:8000 bash scripts/demo.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
PY="${PYTHON:-python3}"

req() {
  # req <json-file-or-inline> [extra curl args...]
  local payload="$1"; shift
  curl -sS -X POST "$BASE_URL/evaluate" \
    -H 'Content-Type: application/json' "$@" -d "$payload"
}

summarize() {
  "$PY" -c "
import json, sys
d = json.load(sys.stdin)
print('  基线总成本   :', d['baseline_total_cost'])
print('  候选总成本   :', d['hypothetical_total_cost'])
print('  成本变化     :', d['cost_delta'], '(改善比例', d['improvement_ratio'], ')')
print('  计划是否变化 :', d['plan_changed'])
print('  采纳候选     :', [c['index_name'] for c in d['adopted_candidates']])
print('  拒绝候选     :', [c['index_name'] for c in d['rejected_candidates']])
print('  基线前候选数 :', d['session_hypothetical_indexes_before_baseline'],
      '/ reset 后:', d['session_hypothetical_indexes_after_reset'])
"
}

echo "== 0. 健康检查（连接池硬上限应为 2） =="
curl -sS "$BASE_URL/health" | "$PY" -m json.tool

echo
echo "== 1. 能改善计划的候选 (warehouse_id, scan_time) =="
req '{"sample_id":"nightly_warehouse_scan",
      "candidates":[{"table":"public.package_scan","columns":["warehouse_id","scan_time"]}]}' | summarize

echo
echo "== 2. 不能改善计划的候选 (destination / weight_kg) =="
req '{"sample_id":"nightly_warehouse_scan",
      "candidates":[{"table":"public.package_scan","columns":["destination"]},
                    {"table":"public.package_scan","columns":["weight_kg"]}]}' | summarize

echo
echo "== 3. 全表聚合：B-tree 无法改善 =="
req '{"sample_id":"nightly_aggregate",
      "candidates":[{"table":"public.package_scan","columns":["warehouse_id"]},
                    {"table":"public.package_scan","columns":["scan_time"]}]}' | summarize

echo
echo "== 4. 只读守卫：UPDATE 必须被 400 拒绝 =="
req '{"sql_text":"UPDATE public.package_scan SET weight_kg=1 WHERE scan_id=1",
      "candidates":[{"table":"public.package_scan","columns":["warehouse_id"]}]}' \
  | "$PY" -c "import json,sys; print(' ', json.load(sys.stdin))"

echo
echo "== 5. 暂停期间断连，验证取消安全清理与连接复用 =="
# 30 秒暂停；2 秒后用超时强制断开 TCP（不依赖交互式 Ctrl+C）
if curl -sS --max-time 2 -X POST "$BASE_URL/evaluate" \
    -H 'Content-Type: application/json' -H 'X-Demo-Pause-Seconds: 30' \
    -d '{"sample_id":"nightly_warehouse_scan",
         "candidates":[{"table":"public.package_scan","columns":["warehouse_id","scan_time"]}]}' \
    > /tmp/demo_disconnect.out 2>/tmp/demo_disconnect.err; then
  echo "  (未发生超时，环境可能忽略了 --max-time)"
else
  echo "  客户端已在暂停 2 秒后强制断连"
fi
echo "  等待服务端完成取消安全清理 ..."
sleep 4
echo "  断连后立即发起新请求（必须成功且无候选残留）："
req '{"sample_id":"nightly_warehouse_scan",
      "candidates":[{"table":"public.package_scan","columns":["warehouse_id","scan_time"]}]}' | summarize
echo "  连接池状态："
curl -sS "$BASE_URL/health" | "$PY" -c "import json,sys; print('  ', json.dumps(json.load(sys.stdin)['pool'], ensure_ascii=False))"
