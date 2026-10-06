#!/usr/bin/env bash
# 多时段有限储水供水调度预演 —— 端到端调用示例（curl + jq/python3）。
# 连续等长时段：每期先补水至库容（超出计溢出），再按现有阀位/单向管段/
# 共享容量/优先级分配；水源出水不超过额定上限与可用储水，欠供不结转。
# 预演只读：不改变管网、阀位、计划或修订号。
# 用法：bash examples/storage_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

# 不依赖 jq 也能取 revision（优先 jq，回退 python3，再回退 grep/sed）
get_field() {  # $1=JSON文件 $2=字段名
  if [ -n "$JQ" ]; then jq -r ".$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$1" "$2"
  else
    grep -o "\"$2\": *[0-9-]*" "$1" | head -1 | sed 's/.*: *//'
  fi
}

echo "== 0. 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 1. 整批录入示例管网（原子覆盖），取得当前修订号 =="
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" > /tmp/storage_load.json
pp < /tmp/storage_load.json
REV="$(get_field /tmp/storage_load.json revision)"
echo "(当前修订号 revision=$REV)"

echo
echo "== 2. 两期储水预演：S1 库存15/库容15，S2 库存5/库容8 =="
echo "   第1期 S2 补4（5+4>8，溢出1）；第2期不补水，吃库存直至耗尽"
curl -sS -X POST "$BASE/api/storage/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 15, \"storage_capacity\": 15},
          {\"source_id\": \"S2\", \"initial_storage\": 5,  \"storage_capacity\": 8}
        ],
        \"periods\": [
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0},
                               {\"source_id\": \"S2\", \"amount\": 4}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D2\", \"amount\": 8},
                         {\"demand_id\": \"D3\", \"amount\": 6}]},
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0},
                               {\"source_id\": \"S2\", \"amount\": 0}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D2\", \"amount\": 8},
                         {\"demand_id\": \"D3\", \"amount\": 6}]}
        ]
      }" | pp

echo
echo "== 3. 第2期补水恢复供水：S1 补3、S2 补20（超出库容部分计入溢出） =="
curl -sS -X POST "$BASE/api/storage/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 15, \"storage_capacity\": 15},
          {\"source_id\": \"S2\", \"initial_storage\": 5,  \"storage_capacity\": 8}
        ],
        \"periods\": [
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0},
                               {\"source_id\": \"S2\", \"amount\": 0}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D2\", \"amount\": 8},
                         {\"demand_id\": \"D3\", \"amount\": 6}]},
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 3},
                               {\"source_id\": \"S2\", \"amount\": 20}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D2\", \"amount\": 8},
                         {\"demand_id\": \"D3\", \"amount\": 6}]}
        ]
      }" | pp

echo
echo "== 4. 初始储水超过库容（400，定位 sources[0].initial_storage） =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/storage/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 99, \"storage_capacity\": 15},
          {\"source_id\": \"S2\", \"initial_storage\": 5,  \"storage_capacity\": 8}
        ],
        \"periods\": [
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0},
                               {\"source_id\": \"S2\", \"amount\": 0}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D2\", \"amount\": 8},
                         {\"demand_id\": \"D3\", \"amount\": 6}]}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 5. 时段条目缺失/重复/未知（400，一次性带定位，整单拒绝） =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/storage/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $REV,
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 15, \"storage_capacity\": 15},
          {\"source_id\": \"S2\", \"initial_storage\": 5,  \"storage_capacity\": 8}
        ],
        \"periods\": [
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"GHOST\", \"amount\": 1}]}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 6. 过期修订号（409，整单拒绝，不返回混合版本结果） =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/storage/preview" \
  -H 'Content-Type: application/json' \
  -d "{
        \"revision\": $((REV - 1)),
        \"sources\": [
          {\"source_id\": \"S1\", \"initial_storage\": 15, \"storage_capacity\": 15},
          {\"source_id\": \"S2\", \"initial_storage\": 5,  \"storage_capacity\": 8}
        ],
        \"periods\": [
          {\"replenishment\": [{\"source_id\": \"S1\", \"amount\": 0},
                               {\"source_id\": \"S2\", \"amount\": 0}],
           \"demands\": [{\"demand_id\": \"D1\", \"amount\": 10},
                         {\"demand_id\": \"D2\", \"amount\": 8},
                         {\"demand_id\": \"D3\", \"amount\": 6}]}
        ]
      }"
pp < /tmp/resp.json

echo
echo "== 7. 确认预演不写入：修订号与管网/阀位均未变化 =="
curl -sS "$BASE/api/network" | pp
