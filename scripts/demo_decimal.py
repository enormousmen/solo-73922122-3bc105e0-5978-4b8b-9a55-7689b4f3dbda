"""精确十进制容量核算演示：满载通过、微量超载退回、大数汇聚。

三个场景（拓扑相同：r2 -> r1 -> exit，两入口流量汇聚到瓶颈链路 r1->exit）：

1. 满载（plan_decimal_full.json）：0.1 + 0.2 恰好等于容量 0.3 -> 放行；
2. 微量超载（plan_decimal_overload.json）：0.1 + 0.20000000000000001 =
   0.30000000000000001 > 0.3 -> 整批退回，响应给出精确的链路合计与容量；
3. 大数汇聚（plan_decimal_huge.json）：1e309 + 1e309 = 2e309 > 1e309，
   合计已超出 float64 的有限范围（>1.7976931348623157e308），仍按精确十进制裁决。

用法：
  # 不启动服务，进程内 ASGI 演示（默认）
  python scripts/demo_decimal.py

  # 对已启动的 Docker Compose 服务发真实 HTTP 请求
  BASE=http://localhost:8000 python scripts/demo_decimal.py --http
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR.parent))

from app.decimals import dumps, loads  # noqa: E402

SCENARIOS = [
    ("满载：0.1 + 0.2 == 容量 0.3 -> 放行", "plan_decimal_full.json", "approved"),
    (
        "微量超载：0.1 + 0.20000000000000001 > 容量 0.3 -> 整批退回",
        "plan_decimal_overload.json",
        "rejected",
    ),
    (
        "大数汇聚：1e309 + 1e309 = 2e309 > 容量 1e309（超 float64 有限范围）-> 退回",
        "plan_decimal_huge.json",
        "rejected",
    ),
]

HEADERS = {"Content-Type": "application/json"}


def _quote(prefix: str) -> str:
    return urllib.parse.quote(prefix, safe="")


def _print_faults(body: dict) -> None:
    for scenario in body.get("failing_scenarios", [])[:2]:
        states = ", ".join(f"{d}={s}" for d, s in sorted(scenario["device_states"].items()))
        print(f"  - 故障场景#{scenario['index']} [{states}]")
        for fault in scenario["faults"]:
            if fault["type"] == "超载":
                print(
                    f"      超载 @ {fault['location']}："
                    f"合计流量 {fault['load']} > 容量 {fault['capacity']}"
                )
            else:
                print(f"      {fault['type']} @ {fault['location']}: {fault['detail']}")


async def run_inprocess() -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://route-approval") as client:
        await _run(client)


async def run_http(base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10) as client:
        await _run(client)


async def _run(client: httpx.AsyncClient) -> None:
    for title, filename, expected in SCENARIOS:
        payload = loads((SCRIPTS_DIR / filename).read_text(encoding="utf-8"))
        prefix = payload["prefix"]
        print("=" * 72)
        print(title)
        print("=" * 72)

        put = await client.put("/plans", content=dumps(payload).encode(), headers=HEADERS)
        print(f"  PUT /plans -> {put.status_code}")

        resp = await client.post(
            f"/plans/applications?prefix={_quote(prefix)}",
            content=dumps({"devices": ["r1"]}).encode(),
            headers=HEADERS,
        )
        # 用精确十进制解析响应，避免 stdlib json 把 2E+309 读成 inf
        body = loads(resp.text)
        print(f"  裁决: {body['decision']}（预期 {expected}），"
              f"评估场景 {body['scenarios_evaluated']} 个，"
              f"故障场景 {body['failing_scenarios_count']} 个")
        _print_faults(body)
        assert body["decision"] == expected, f"{filename} 裁决不符合预期"
        print()

    print("三个场景全部符合预期。")


def main() -> None:
    parser = argparse.ArgumentParser(description="精确十进制容量核算演示")
    parser.add_argument("--http", action="store_true", help="对真实 HTTP 服务发请求（需先启动服务）")
    parser.add_argument("--base", default=None, help="服务地址，默认读 BASE 环境变量或 http://localhost:8000")
    args = parser.parse_args()

    if args.http:
        import asyncio

        base = args.base or __import__("os").environ.get("BASE", "http://localhost:8000")
        asyncio.run(run_http(base))
    else:
        import asyncio

        asyncio.run(run_inprocess())


if __name__ == "__main__":
    main()
