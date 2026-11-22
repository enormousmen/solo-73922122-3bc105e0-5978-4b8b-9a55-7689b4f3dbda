#!/usr/bin/env bash
# 回执到达前后放行结果不同的 API 示例（curl 版）
# 前置：docker compose up -d  或  uvicorn app.main:app --port 8000
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
PREFIX="10.20.0.0%2F16"   # 10.20.0.0/16，'/' 做百分号编码后走 query 参数

echo "== 1. 录入换路方案 =="
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d "$(sed 's/10.11.0.0\/16/10.20.0.0\/16/' "$(dirname "$0")/plan_a.json")"
echo

echo "== 2. 申请 r3 放行（预期 approved，进入已放行未确认）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}'
echo

echo "== 3. 申请 r4 放行（预期 rejected：r3=old & r4=new 时 r3<->r4 环路，整批退回）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 4. 登记 r3 成功回执（此后 r3 按新路由 r3->exit 核算）=="
curl -s -X POST "$BASE/plans/receipts?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
echo

echo "== 5. 重新申请 r4（预期 approved，与第 3 步结果不同）=="
curl -s -X POST "$BASE/plans/applications?prefix=$PREFIX" \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
echo

echo "== 6. 查询网段全量状态（含全部申请历史）=="
curl -s "$BASE/plans/detail?prefix=$PREFIX"
echo

echo "== 7. 精确十进制容量核算演示（满载 / 微量超载 / 大数汇聚）=="
# 拓扑：r2 -> r1 -> exit，两个入口的流量汇聚到瓶颈链路 r1 -> exit
# 7a. 满载：0.1 + 0.2 恰好等于容量 0.3 -> 放行
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/plan_decimal_full.json"
echo
curl -s -X POST "$BASE/plans/applications?prefix=10.30.1.0%2F24" \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
echo

# 7b. 微量超载：0.1 + 0.20000000000000001 = 0.30000000000000001 > 0.3 -> 整批退回
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/plan_decimal_overload.json"
echo
curl -s -X POST "$BASE/plans/applications?prefix=10.30.2.0%2F24" \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
echo

# 7c. 大数汇聚：1e309 + 1e309 = 2e309 > 1e309（合计超出 float64 有限范围）-> 退回
curl -s -X PUT "$BASE/plans" -H 'Content-Type: application/json' \
  -d @"$(dirname "$0")/plan_decimal_huge.json"
echo
curl -s -X POST "$BASE/plans/applications?prefix=10.30.3.0%2F24" \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
echo
