# API 示例：回执到达前后放行结果不同

以下响应均为服务真实输出（完整 JSON 见 `demo_output.json`，可由 `python scripts/demo.py` 重新生成）。

## 拓扑（目的网段 10.11.0.0/16，出口 exit）

录入载荷见 `scripts/plan_a.json`。路由器与新旧下一跳：

| 设备 | 旧下一跳 | 新下一跳 |
| --- | --- | --- |
| r1 | r2 | r3 |
| r2 | r4 | r1 |
| r3 | r4 | **exit** |
| r4 | exit | **r3** |

入口流量：r1 注入 10；所有相关有向链路（含 `r3->exit`、`r4->r3`、`r3->r4`）容量均为 100。

### 1. 录入方案

```bash
curl -X PUT http://localhost:8000/plans \
  -H 'Content-Type: application/json' -d @scripts/plan_a.json
```

```json
{
  "prefix": "10.11.0.0/16",
  "exit_node": "exit",
  "routers": ["r1", "r2", "r3", "r4"],
  "confirmed_devices": [],
  "pending_devices": [],
  "application_count": 0
}
```

### 2. 申请 r3 —— 通过（2 种新旧组合均安全）

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r3"]}'
```

```json
{
  "application_id": "app-b1124b2ab343",
  "decision": "approved",
  "batch": ["r3"],
  "uncertain_devices": ["r3"],
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0},
  "message": "全部 2 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理"
}
```

此后 r3 进入“已放行未确认”（pending）。

### 3. 申请 r4 —— 整批退回（r3 未确认，存在 r3↔r4 环路组合）

不确定设备 = 本批 `{r4}` ∪ pending `{r3}`，共穷举 4 种组合。
当 `r3=old（->r4）` 且 `r4=new（->r3）` 时，r1 的流量沿
`r1 -> r2 -> r4 -> r3 -> r4` 形成环路：

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

```json
{
  "application_id": "app-d843973d934a",
  "decision": "rejected",
  "batch": ["r4"],
  "uncertain_devices": ["r3", "r4"],
  "scenarios_evaluated": 4,
  "failing_scenarios_count": 1,
  "fault_summary": {"断路": 0, "环路": 1, "超载": 0},
  "failing_scenarios": [
    {
      "index": 1,
      "device_states": {"r3": "old", "r4": "new"},
      "reachable": false,
      "faults": [
        {
          "type": "环路",
          "location": "r1 -> r2 -> r4 -> r3 -> r4",
          "detail": "入口 r1 的流量沿 r1 -> r2 -> r4 -> r3 -> r4 形成转发环路，无法到达出口 exit"
        }
      ]
    }
  ],
  "message": "在 1/4 种新旧组合下存在断路/环路/超载，整批退回，原有放行记录不变"
}
```

退回后查询状态，r3 的原放行记录保留：`pending_devices=["r3"]`，申请历史中同时留有
第 2、3 步两条记录。

### 4. 核算期间登记 r3 的成功回执

```bash
curl -X POST 'http://localhost:8000/plans/receipts?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"device":"r3","note":"现场切换成功"}'
```

```json
{
  "prefix": "10.11.0.0/16",
  "device": "r3",
  "receipt_id": "rcpt-4e441de29ac7",
  "status": "registered",
  "confirmed_devices": ["r3"],
  "pending_devices": []
}
```

此后 r3 强制按新路由 `r3 -> exit` 转发，不再参与新旧组合穷举。
重复登记同一设备回执返回 `"status": "already_registered"`（幂等）。

### 5. 回执到达后重新申请 r4 —— 通过（与第 3 步裁决不同）

不确定集合只剩 `{r4}`：r4=old 走 `r4 -> exit`；r4=new 走 `r4 -> r3 -> exit`，两种均安全。

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.11.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r4"]}'
```

```json
{
  "application_id": "app-8722195038e9",
  "decision": "approved",
  "batch": ["r4"],
  "uncertain_devices": ["r4"],
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0},
  "message": "全部 2 种新旧组合核算通过，本批 1 台设备放行；登记成功回执前按不确定设备处理"
}
```

**同一批设备（r4），回执前退回、回执后放行**，这就是回执改变裁决结果的完整过程。

## 附：断路 + 超载的故障定位（10.12.0.0/16）

录入载荷见 `scripts/plan_b.json`：r1 新下一跳 r3 但未录入 `r1 -> r3` 链路（断路）；
r1、r2 各注入 60，r3 -> exit 容量仅 100（超载）。

```bash
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.12.0.0%2F16' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "rejected",
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 2,
  "fault_summary": {"断路": 1, "环路": 0, "超载": 1},
  "failing_scenarios": [
    {
      "index": 0,
      "device_states": {"r1": "old"},
      "faults": [
        {"type": "超载", "location": "r3 -> exit",
         "detail": "链路 r3 -> exit 合计流量 120 超过容量 100",
         "load": 120.0, "capacity": 100.0}
      ]
    },
    {
      "index": 1,
      "device_states": {"r1": "new"},
      "faults": [
        {"type": "断路", "location": "r1 -> r3",
         "detail": "入口 r1 的流量在 r1 -> r3 处无法继续转发到出口 exit"}
      ]
    }
  ]
}
```

## 并发申请语义

所有申请/回执写操作在同一把 asyncio 锁内串行裁决。两个各自安全的批次并发提交时，
先裁决者放行并进入 pending，后裁决者必须把先裁决的批次计入不确定集合——若联合穷举
出现风险则退回（自测 `test_api_receipt_changes_decision_concurrency` 验证：并发结果
恰为一个 approved、一个 rejected，且只有先裁决的设备出现在 pending 中）。

## 精确十进制容量核算（满载 / 微量超载 / 大数汇聚）

链路容量与入口流量全程以 **JSON 数字** 传递（支持科学计数法），服务端用
`Decimal` 精确十进制核算，不经过 float、不使用比较容差：

- 合计 **恰好等于** 容量（刚好满载）通过容量检查；
- 合计严格大于容量才判超载，任一链路超载即整批退回；
- 录入数字的原始十进制字面量（如 `0.20000000000000001`、`1e309`）在查回方案、
  重新录入后原样保留；
- 即使合计超出 float64 的有限范围（> 1.7976931348623157e308），仍按精确十进制
  裁决并在响应中以合法 JSON 数字给出合计与容量（如 `2E+309`）。

三个演示拓扑相同：`r2 -> r1 -> exit`，两个入口（r1、r2）的流量汇聚到瓶颈链路
`r1 -> exit`。载荷见 `scripts/plan_decimal_full.json`、`plan_decimal_overload.json`、
`plan_decimal_huge.json`，可由 `python scripts/demo_decimal.py` 一键复现。

### 1. 刚好满载 —— 0.1 + 0.2 == 容量 0.3，放行

```bash
curl -X PUT http://localhost:8000/plans -H 'Content-Type: application/json' \
  -d @scripts/plan_decimal_full.json
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.30.1.0%2F24' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "approved",
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 0,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 0}
}
```

> 旧实现用 float 累加并带 `1e-9` 容差，`0.1 + 0.2` 在二进制浮点下是
> `0.30000000000000004`，满载判定不可靠；现在按十进制严格比较，相等即放行。

### 2. 微量超载 —— 0.1 + 0.20000000000000001 = 0.30000000000000001 > 0.3，整批退回

```bash
curl -X PUT http://localhost:8000/plans -H 'Content-Type: application/json' \
  -d @scripts/plan_decimal_overload.json
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.30.2.0%2F24' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "rejected",
  "scenarios_evaluated": 2,
  "failing_scenarios_count": 2,
  "fault_summary": {"断路": 0, "环路": 0, "超载": 2},
  "failing_scenarios": [
    {
      "index": 0,
      "device_states": {"r1": "old"},
      "reachable": false,
      "faults": [
        {
          "type": "超载",
          "location": "r1 -> exit",
          "detail": "链路 r1 -> exit 合计流量 0.30000000000000001 超过容量 0.3",
          "load": 0.30000000000000001,
          "capacity": 0.3
        }
      ]
    }
  ]
}
```

`load`、`capacity` 都是 **JSON 数字**（无引号），数值精确到最后一位。
`GET /plans/detail` 的申请历史中给出的合计与容量与退回响应完全一致。

### 3. 大数汇聚 —— 1e309 + 1e309 = 2e309，超出 float64 有限范围仍精确退回

```bash
curl -X PUT http://localhost:8000/plans -H 'Content-Type: application/json' \
  -d @scripts/plan_decimal_huge.json
curl -X POST 'http://localhost:8000/plans/applications?prefix=10.30.3.0%2F24' \
  -H 'Content-Type: application/json' -d '{"devices":["r1"]}'
```

```json
{
  "decision": "rejected",
  "fault_summary": {"断路": 0, "环路": 0, "超载": 2},
  "failing_scenarios": [
    {
      "index": 0,
      "device_states": {"r1": "old"},
      "reachable": false,
      "faults": [
        {
          "type": "超载",
          "location": "r1 -> exit",
          "detail": "链路 r1 -> exit 合计流量 2E+309 超过容量 1E+309",
          "load": 2E+309,
          "capacity": 1e309
        }
      ]
    }
  ]
}
```

`1e309` 已超出 float64 的最大有限值（约 1.8e308），用 float 解析会变成 `Infinity`
而无法比较；Decimal 把它作为有限十进制数精确处理。注意 `capacity` 保留录入时的
原文写法 `1e309`，而合计 `2E+309` 是运算后规范化的十进制表示——两者都是合法
JSON 数字，数值准确。把容量改为 `2e309` 再申请则刚好满载、予以放行。
