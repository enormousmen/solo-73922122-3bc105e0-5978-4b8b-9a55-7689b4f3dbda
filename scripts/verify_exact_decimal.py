"""精确十进制容量核算的端到端验证脚本（不走 pytest，直接打 ASGI）。

覆盖：
1. 原缺陷：0.1 + 0.20000000000000001 汇聚到容量 0.3 -> 整批退回，数字精确
2. 刚好满载（合计 == 容量）-> 放行
3. 大数汇聚（1e309，超出 float64 有限范围）超载退回 / 满载放行
4. 退回保留先前放行状态，历史详情与退回响应数字一致
5. 科学计数法/原始十进制字面量在查回再录入后保留
6. 长有效数字（40 位）不被截断
"""

from __future__ import annotations

import sys
from urllib.parse import quote

sys.path.insert(0, "/workspace")

from fastapi.testclient import TestClient  # noqa: E402

from app.decimals import dumps, loads  # noqa: E402
from app.main import app  # noqa: E402
from app.storage import store  # noqa: E402

client = TestClient(app)
store._prefixes.clear()
HEADERS = {"Content-Type": "application/json"}
Q = lambda value: quote(value, safe="")  # noqa: E731

results: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    results.append((name, cond))
    print(("PASS" if cond else "FAIL"), "-", name)


def bottleneck_plan(prefix: str, capacity: str, t1: str, t2: str) -> bytes:
    payload = (
        "{"
        f'"prefix":"{prefix}","exit_node":"exit","routers":["r1","r2"],'
        '"links":['
        '{"src":"r2","dst":"r1","capacity":1e400},'
        f'{{"src":"r1","dst":"exit","capacity":{capacity}}}'
        "],"
        f'"ingresses":[{{"node":"r1","traffic":{t1}}},{{"node":"r2","traffic":{t2}}}],'
        '"routes":['
        '{"device":"r1","old_next_hop":"exit","new_next_hop":"exit"},'
        '{"device":"r2","old_next_hop":"r1","new_next_hop":"r1"}'
        "]}"
    )
    return payload.encode()


def put(payload: bytes):
    return client.put("/plans", content=payload, headers=HEADERS)


def apply(prefix: str, devices: list[str]):
    url = f"/plans/applications?prefix={Q(prefix)}"
    return client.post(url, content=dumps({"devices": devices}).encode(), headers=HEADERS)


def get_detail(prefix: str):
    return client.get(f"/plans/detail?prefix={Q(prefix)}")


# 1. 原缺陷场景：微量超载必须退回
put(bottleneck_plan("10.0.1.0/24", "0.3", "0.1", "0.20000000000000001"))
resp = apply("10.0.1.0/24", ["r1"])
body = loads(resp.text)
fault = body["failing_scenarios"][0]["faults"][0]
check(
    "微量超载退回且合计精确",
    body["decision"] == "rejected"
    and str(fault["load"]) == "0.30000000000000001"
    and str(fault["capacity"]) == "0.3",
)
check(
    "退回响应中 load/capacity 是 JSON 数字而非字符串",
    '"load":0.30000000000000001' in resp.text and '"capacity":0.3' in resp.text,
)

# 2. 刚好满载放行
put(bottleneck_plan("10.0.2.0/24", "0.3", "0.1", "0.2"))
check("满载(0.1+0.2==0.3)放行", apply("10.0.2.0/24", ["r1"]).json()["decision"] == "approved")
put(bottleneck_plan("10.0.25.0/24", "0.30000000000000001", "0.1", "0.20000000000000001"))
check("严格相等(合计==容量)放行", apply("10.0.25.0/24", ["r1"]).json()["decision"] == "approved")

# 3. 大数汇聚（超 float64 有限范围）
put(bottleneck_plan("10.0.3.0/24", "1e309", "1e309", "1e309"))
resp = apply("10.0.3.0/24", ["r1"])
body = loads(resp.text)
fault = body["failing_scenarios"][0]["faults"][0]
check(
    "大数(1e309)超载退回，合计 2E+309 精确",
    body["decision"] == "rejected"
    and str(fault["load"]) == "2E+309"
    and str(fault["capacity"]) == "1E+309",
)
check(
    "大数响应为精确 JSON 数字，容量保留录入原文 1e309",
    '"load":2E+309' in resp.text and '"capacity":1e309' in resp.text,
)
put(bottleneck_plan("10.0.4.0/24", "2e309", "1e309", "1e309"))
check("大数满载(2e309)放行", apply("10.0.4.0/24", ["r1"]).json()["decision"] == "approved")

# 4. 退回保留先前放行状态，历史详情数字与退回响应一致
state_payload = (
    '{"prefix":"10.0.5.0/24","exit_node":"exit","routers":["r1","r2","r3"],'
    '"links":['
    '{"src":"r1","dst":"exit","capacity":0.3},'
    '{"src":"r2","dst":"r1","capacity":1e400},'
    '{"src":"r2","dst":"r3","capacity":1e400},'
    '{"src":"r3","dst":"exit","capacity":1e400}'
    '],'
    '"ingresses":[{"node":"r1","traffic":0.1},{"node":"r2","traffic":0.20000000000000001}],'
    '"routes":['
    '{"device":"r1","old_next_hop":"exit","new_next_hop":"exit"},'
    '{"device":"r2","old_next_hop":"r3","new_next_hop":"r1"},'
    '{"device":"r3","old_next_hop":"exit","new_next_hop":"exit"}'
    ']}'
).encode()
put(state_payload)
check("第一批 r1 放行", apply("10.0.5.0/24", ["r1"]).json()["decision"] == "approved")
second = apply("10.0.5.0/24", ["r2"])
rejected = loads(second.text)
check("第二批 r2 超载退回", rejected["decision"] == "rejected")
detail = loads(get_detail("10.0.5.0/24").text)
check("退回后 pending 保留为 [r1]", detail["pending_devices"] == ["r1"])
hist_fault = detail["applications"][1]["failing_scenarios"][0]["faults"][0]
resp_fault = rejected["failing_scenarios"][0]["faults"][0]
check(
    "历史详情与退回响应的 load/capacity 相同且准确",
    str(hist_fault["load"]) == str(resp_fault["load"]) == "0.30000000000000001"
    and str(hist_fault["capacity"]) == str(resp_fault["capacity"]) == "0.3",
)

# 5. 查回方案再录入，原始字面量保留
put(bottleneck_plan("10.0.6.0/24", "0.3000000000000000000", "1.5e-1", "0.20000000000000001"))
detail_text = get_detail("10.0.6.0/24").text
check(
    "详情保留原始十进制/科学计数法字面量",
    all(token in detail_text for token in ("0.3000000000000000000", "1.5e-1", "0.20000000000000001")),
)
roundtrip = loads(detail_text)["plan"]
roundtrip["prefix"] = "10.0.7.0/24"
put(dumps(roundtrip).encode())
detail2 = get_detail("10.0.7.0/24").text
check(
    "查回再录入后字面量仍保留",
    all(token in detail2 for token in ("0.3000000000000000000", "1.5e-1", "0.20000000000000001")),
)

# 6. 长有效数字不被 28 位默认精度截断
put(bottleneck_plan("10.0.8.0/24", "0.3", "0.12345678901234567890123456789012345678901", "0.1"))
long_detail = loads(get_detail("10.0.8.0/24").text)
check(
    "40 位有效数字完整保留",
    str(long_detail["plan"]["ingresses"][0]["traffic"])
    == "0.12345678901234567890123456789012345678901",
)

print()
passed = sum(1 for _, ok in results if ok)
print(f"总计 {passed}/{len(results)} 项通过")
if passed != len(results):
    sys.exit(1)
