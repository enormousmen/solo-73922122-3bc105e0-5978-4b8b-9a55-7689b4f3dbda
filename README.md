# 机房换路放行服务（FastAPI）

值班员为一个目的网段录入出口、最多 12 台路由器的新旧下一跳、各入口流量和有向链路容量，
按批申请下发新配置。服务端对“本批设备 + 已放行但尚未确认的设备”穷举所有新旧路由组合，
保证无论这些设备以什么先后顺序切换，流量都能到达出口，且每条有向链路上的合计流量不超过容量。
任一组合出现**断路、环路、超载**即整批退回，保留原放行记录，并在响应中标出故障位置。

## 裁决模型

对每个目的网段维护三组设备：

| 分组 | 含义 | 裁决时使用的路由 |
| --- | --- | --- |
| `confirmed` | 已放行且已登记成功回执 | 强制**新路由** |
| `pending` | 已放行但尚未确认 | 新、旧路由都可能生效 |
| 本批申请设备 | 本次申请下发新配置 | 裁决期间尚未切换，新、旧都可能 |
| 其余设备 | 不在任何批次中 | 保持**旧路由** |

不确定设备集合 `U = pending ∪ 本批`（最多 12 台），穷举 `2^|U|`（最多 4096）种组合，
逐场景沿路由表逐跳追踪每个入口的流量：

- 下一跳不存在或两者间没有录入有向链路 → **断路**，位置为中断处（如 `r1 -> r3`）；
- 再次进入路径上已出现的节点 → **环路**，位置为从入口开始的完整转发路径（如 `r1 -> r2 -> r4 -> r3 -> r4`）；
- 链路上汇聚的入口流量合计超过容量 → **超载**，位置为该链路并附 `load/capacity`。

### 精确十进制容量核算

流量与容量以 JSON 数字传递（支持科学计数法），服务端全程使用 `Decimal` 精确
十进制运算，**不经过 float、不设比较容差**：合计恰好等于容量（刚好满载）放行，
严格大于容量才判超载。这避免了 `0.1 + 0.2 = 0.30000000000000004` 这类二进制
浮点误差导致的误判（例如 0.1 与 0.20000000000000001 汇聚到容量 0.3 的链路）。

- 请求数字保留原始十进制字面量（`raw`），查回方案再录入仍是原来的值；
- 合计超出 float64 有限范围（> ~1.8e308，float 下为 Infinity）时，Decimal 仍按
  有限十进制数精确裁决，响应以合法 JSON 数字给出合计（如 `2E+309`）与容量；
- 退回响应与 `/plans/detail` 历史详情中的链路合计、容量是相同且准确的 JSON 数字。

全部场景安全才放行；放行后本批设备进入 `pending`。登记成功回执后设备转入 `confirmed`，
之后的裁决按新路由计算。退回不改变任何放行状态，申请记录仍留痕可审计。

并发安全：所有写操作在同一把 `asyncio.Lock` 内完成，并发申请按裁决时刻已登记的
批次和回执串行判断，后一个申请看到的一定是前一个落库后的状态。

## 目录结构

```
app/
  decimals.py  # 精确十进制：JSON 数字解析/序列化、原始字面量保留、超 float64 支持
  models.py    # Pydantic 模型与录入校验（12 台上限、节点/链路/路由一致性）
  engine.py    # 核算引擎：2^n 场景穷举 + 断路/环路/超载检测（Decimal 精确比较）
  storage.py   # 进程内存储、设备分组、asyncio 锁
  main.py      # FastAPI 路由（精确 JSON 请求/响应通道）
tests/         # pytest：引擎单测 + API 全流程 + 并发裁决 + 十进制回归
scripts/
  demo.py                  # 端到端演示（in-process ASGI，标准库 + httpx）
  demo_decimal.py          # 精确十进制演示：满载/微量超载/大数汇聚
  verify_exact_decimal.py  # 十进制核算 14 项端到端核对
  curl_example.sh          # 等价的 curl 调用示例
  plan_a.json / plan_b.json          # 环路 / 断路+超载场景录入载荷
  plan_decimal_full.json             # 刚好满载（0.1+0.2==0.3）
  plan_decimal_overload.json         # 微量超载（0.1+0.20000000000000001>0.3）
  plan_decimal_huge.json             # 大数汇聚（1e309，超 float64 范围）
Dockerfile
docker-compose.yml
requirements.txt
```

## 启动方式

Docker Compose（推荐）：

```bash
docker compose up -d --build
curl http://localhost:8000/health
# 交互式文档：http://localhost:8000/docs
```

本地直接运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> 存储为进程内内存，且并发裁决依赖单进程内的 asyncio 锁，因此 Compose/uvicorn 均固定 1 个 worker。

## API 一览

网段前缀（如 `10.11.0.0/16`）含 `/`，作为 query 参数 `prefix` 传递（`/` 需百分号编码为 `%2F`）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| PUT | `/plans` | 录入或全量覆盖一个目的网段的方案（覆盖后放行状态重置） |
| GET | `/plans` | 列出所有网段摘要 |
| GET | `/plans/detail?prefix=` | 查询网段全量状态，含全部申请历史 |
| POST | `/plans/applications?prefix=` | 提交一批设备的换路申请，整批通过或整批退回 |
| POST | `/plans/receipts?prefix=` | 登记已放行设备的成功回执（幂等） |

错误码：网段不存在 → 404；设备不属于该网段/未放行先登记回执 → 400/409；
重复申请已在 `pending` 或 `confirmed` 中的设备 → 409。

## 自测

```bash
pip install -r requirements.txt
pytest -q
python scripts/demo.py
python scripts/demo_decimal.py            # 满载 / 微量超载 / 大数汇聚三个场景
python scripts/verify_exact_decimal.py    # 14 项端到端核对（含字面量保留、历史一致性）
bash scripts/curl_example.sh   # 需要先启动服务
```

## 回执到达前后放行结果不同的示例

见 [API_EXAMPLES.md](API_EXAMPLES.md)。简述：申请 `r4` 时因 `r3` 已放行未确认，
`r3=old & r4=new` 组合出现 `r3 <-> r4` 环路而被整批退回；登记 `r3` 成功回执后，
`r3` 强制按新路由 `r3 -> exit` 转发，重新申请 `r4` 即通过。
