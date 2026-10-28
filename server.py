"""Feegate 数据市场 MCP Server（只读） + x402 按次计费。

Feegate 自己的 x402 接入（2026-10-28）：平台既是协议的运营方，也是它的用户。
- 严格只读：所有工具只查询 catalog.json / pricing.json / analytics.json，不写任何状态。
- 发现免费：ping / fee_quote / list_products 不收费（agent 看不到的东西不可能买）。
- 数据付费：marketplace_catalog / product_detail / platform_stats 过 402 挑战 → 验签 → 结算。
- 交付失败不收费：查不到数据时返回 isError，x402 wrapper 不会结算。

链：Base 主网（eip155:8453），USDC（Circle 官方 0x8335…2913），exact scheme，x402 v2。
facilitator：PayAI。收款地址与 rentgpu 同一平台收款地址。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mcp import create_payment_wrapper
from x402.schemas.config import ResourceConfig

# ---------------------------------------------------------------- 配置

HERE = Path(__file__).parent

NETWORK = os.environ.get("X402_NETWORK", "eip155:8453")
FACILITATOR_URL = os.environ.get("X402_FACILITATOR", "https://facilitator.payai.network")
PAY_TO = os.environ.get("PAY_TO") or "0x230cd9cd33589bcf33cd32e5aa4f9e0c3994a63b"

CATALOG_JSON = os.environ.get("CATALOG_JSON", str(HERE / "data" / "catalog.json"))
PRICING_JSON = "/var/www/feegate/pricing.json"
ANALYTICS_JSON = "/var/www/feegate/admin/data/analytics.json"

# 定价（USD，按次）。2026-10-28 口径：小颗粒单次读取（$0.001–0.008）是 x402
# Bazaar 上实际成交最密集的区间，目录类查询便宜、全量/统计类略贵。
PRICE = {
    "product_detail": "$0.002",
    "marketplace_catalog": "$0.005",
    "platform_stats": "$0.005",
}

# ---------------------------------------------------------------- 数据源（只读 + mtime 自动重载）

_cache: dict = {}
_mtime: dict = {}


def _load(path: str) -> dict | list:
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return _cache.get(path, {})
    if _cache.get(path) is None or _mtime.get(path) != mt:
        with open(path, encoding="utf-8") as f:
            _cache[path] = json.load(f)
        _mtime[path] = mt
    return _cache[path]


def catalog() -> list:
    return _load(CATALOG_JSON) or []


def pricing() -> dict:
    return _load(PRICING_JSON) or {}


def analytics() -> dict:
    try:
        return _load(ANALYTICS_JSON) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 成交流水（与 rentgpu 同一套账法）

import datetime as _dt  # noqa: E402

LEDGER_PATH = str(HERE / "data" / "payments.jsonl")

SELF_PAYERS = {
    "0x6274a5e39e84168c5de57110a6e0ab1fbbe0a89e",
    "0x230cd9cd33589bcf33cd32e5aa4f9e0c3994a63b",
}


def _dig(obj, *keys):
    cur = obj
    for k in keys:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            cur = getattr(cur, k, None)
    return cur


def _record_settlement(payload, requirements, res):
    """settle 成功后写一行流水。任何异常都不能影响付费调用本身。"""
    try:
        req = requirements[0] if isinstance(requirements, list) and requirements else requirements
        raw_amount = _dig(req, "max_amount_required") or _dig(req, "amount") or _dig(req, "maxAmountRequired")
        amount = None
        if raw_amount is not None:
            try:
                amount = int(raw_amount) / 1000000.0
            except Exception:
                amount = None
        payer = (_dig(payload, "payload", "authorization", "from")
                 or _dig(payload, "authorization", "from")
                 or _dig(payload, "from"))
        payee = _dig(req, "pay_to") or _dig(req, "payTo") or PAY_TO
        tx = _dig(res, "transaction") or _dig(res, "tx_hash") or _dig(res, "txHash")
        network = _dig(req, "network") or NETWORK
        row = {
            "ts": _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).isoformat(timespec="seconds"),
            "tx": tx,
            "amount_usdc": amount,
            "tool": None,
            "payer": payer,
            "payee": payee,
            "network": network,
            "kind": "internal" if (payer and str(payer).lower() in SELF_PAYERS) else "external",
            "source": "settle_hook",
        }
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        with open(LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.chmod(LEDGER_PATH, 0o644)
        print("[x402] ledger +1 tx=%s amount=%s kind=%s" % (tx, amount, row["kind"]))
    except Exception as e:
        print("[x402] ledger write failed (non-fatal):", e)


class RetryFacilitator:
    """facilitator settle 自动重试（公共 RPC 偶发广播失败，不重试=用户付了钱拿不到数据）。"""

    RETRYABLE = ("transaction_failed", "settlement_pending", "unable_to_broadcast")

    def __init__(self, inner, retries: int = 3, delay: float = 3.0):
        self._inner = inner
        self.retries = retries
        self.delay = delay

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def verify(self, payload, requirements):
        return await self._inner.verify(payload, requirements)

    async def settle(self, payload, requirements):
        last = None
        for attempt in range(1, self.retries + 1):
            last = await self._inner.settle(payload, requirements)
            if last.success:
                if attempt > 1:
                    print(f"[x402] settle succeeded on retry #{attempt}")
                _record_settlement(payload, requirements, last)
                return last
            reason = str(getattr(last, "error_reason", "") or "")
            if not any(k in reason for k in self.RETRYABLE):
                return last
            print(f"[x402] settle attempt {attempt} failed ({reason}), retrying…")
            if attempt < self.retries:
                await asyncio.sleep(self.delay)
        return last


facilitator = RetryFacilitator(
    HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))
)
resource_server = x402ResourceServer(facilitator)
resource_server.register(NETWORK, ExactEvmServerScheme())
resource_server.initialize()


def accepts_for(tool: str, price: str) -> list:
    return resource_server.build_payment_requirements(
        ResourceConfig(
            scheme="exact",
            pay_to=PAY_TO,
            price=price,
            network=NETWORK,
        )
    )


ALLOWED_HOSTS = [
    "feegate.org", "feegate.org:*",
    "www.feegate.org", "www.feegate.org:*",
    "127.0.0.1", "127.0.0.1:*",
    "localhost", "localhost:*",
]
ALLOWED_ORIGINS = [
    "https://feegate.org", "https://www.feegate.org",
    "http://127.0.0.1:*", "http://localhost:*",
]

mcp = FastMCP(
    "feegate-marketplace",
    instructions=(
        "Feegate（feegate.org）AI Agent 数据市场目录与平台指标数据源。"
        "目录列表、价目、合作方案免费；完整目录明细、单品深查、平台流量指标按次支付 USDC（Base 主网，x402 协议）。"
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=ALLOWED_ORIGINS,
    ),
)


def paid(tool_name: str):
    return create_payment_wrapper(
        resource_server,
        accepts=accepts_for(tool_name, PRICE[tool_name]),
        resource=None,
    )


# ---------------------------------------------------------------- 免费工具（发现层）


@mcp.tool(
    name="ping",
    description="健康检查，免费。返回服务状态、收款地址、网络与价目表。",
)
async def ping() -> str:
    return json.dumps(
        {
            "ok": True,
            "service": "feegate marketplace MCP (read-only)",
            "network": NETWORK,
            "asset": "USDC",
            "pay_to": PAY_TO,
            "prices_usd": PRICE,
            "products": len(catalog()),
            "note": "tools/list 与 ping / fee_quote / list_products 免费；数据类工具按次计费。",
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="fee_quote",
    description=(
        "Feegate 合作方案与结算规则报价（免费）：三档抽成（Self-serve 50% / Managed 40% / "
        "Enterprise 30%）、一次性服务费、四项单项服务价格、结算周期与最低结算额。"
        "给数据持有方推荐合作方式前先调这个拿权威报价。"
    ),
)
async def fee_quote() -> str:
    p = pricing()
    return json.dumps(
        {
            "url": "https://feegate.org/pricing",
            "currency": p.get("currency", "USD"),
            "settlement": p.get("settlementRule", {}),
            "plans": p.get("plans", []),
            "services": p.get("services", []),
            "bundle_note": p.get("bundleNote", ""),
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="list_products",
    description=(
        "列出 Feegate 数据市场全部在售数据产品（免费）：id、名称、供应商、类目、单价、"
        "是否认证。付费深查前先调这个拿准确的 product_id。"
    ),
)
async def list_products() -> str:
    items = [
        {
            "product_id": p.get("id"),
            "name": p.get("name"),
            "vendor": p.get("vendor"),
            "category": p.get("cat"),
            "price_usd_per_call": p.get("price"),
            "verified": p.get("verified", False),
            "featured": p.get("featured", False),
        }
        for p in catalog()
    ]
    return json.dumps({"count": len(items), "products": items}, ensure_ascii=False)


# ---------------------------------------------------------------- 付费工具


@mcp.tool(
    name="product_detail",
    description=(
        "查单个数据产品的完整档案（按次计费 $0.002 USDC）：提供的全部 MCP 工具清单、"
        "累计调用次数、买家数、销售额、可用率、数据许可、结算地址、简介。"
        "示例: product_detail(product_id='gpu-market')。"
    ),
)
@paid("product_detail")
async def product_detail(product_id: str) -> str:
    pid = (product_id or "").strip().lower()
    hit = next((p for p in catalog() if str(p.get("id", "")).lower() == pid), None)
    if hit is None:
        partial = [p["id"] for p in catalog() if pid and pid in str(p.get("id", "")).lower()]
        return json.dumps(
            {
                "error": f"找不到产品 {product_id!r}",
                "close_matches": partial or None,
                "hint": "先调 list_products 拿准确的 product_id",
            },
            ensure_ascii=False,
        )
    return json.dumps(hit, ensure_ascii=False)


@mcp.tool(
    name="marketplace_catalog",
    description=(
        "Feegate 数据市场全量目录（按次计费 $0.005 USDC）：全部在售数据产品及其类目、"
        "单价、工具清单、调用/买家/销售额、可用率。适合做市场全景分析或采购比价。"
    ),
)
@paid("marketplace_catalog")
async def marketplace_catalog() -> str:
    items = [
        {
            "product_id": p.get("id"),
            "name": p.get("name"),
            "vendor": p.get("vendor"),
            "category": p.get("cat"),
            "description": p.get("desc"),
            "tools": p.get("tools"),
            "price_usd_per_call": p.get("price"),
            "calls": p.get("calls"),
            "buyers": p.get("buyers"),
            "sales_usd": p.get("sales"),
            "uptime": p.get("uptime"),
            "verified": p.get("verified", False),
            "license": p.get("license"),
        }
        for p in catalog()
    ]
    total_calls = sum(int(p.get("calls") or 0) for p in catalog())
    total_sales = sum(float(p.get("sales") or 0) for p in catalog())
    return json.dumps(
        {
            "count": len(items),
            "total_calls": total_calls,
            "total_sales_usd": round(total_sales, 2),
            "products": items,
            "source": "feegate.org/marketplace",
        },
        ensure_ascii=False,
    )


@mcp.tool(
    name="platform_stats",
    description=(
        "Feegate 平台自身运营指标（按次计费 $0.005 USDC，30 天窗口）：真实访问量、"
        "独立访客、AI 爬虫 / 搜索引擎抓取明细（哪家 AI 来抓过、抓了多少次）、"
        "热门页面、地区分布。衡量'这个市场有没有真实 Agent 流量'时用。"
    ),
)
@paid("platform_stats")
async def platform_stats() -> str:
    a = analytics()
    if not a:
        return json.dumps({"error": "统计数据暂不可用"}, ensure_ascii=False)
    s = a.get("summary", {})
    return json.dumps(
        {
            "window": a.get("window"),
            "updated": a.get("updated"),
            "traffic": {
                "pv": s.get("pv"),
                "unique_visitors": s.get("uv"),
                "sessions": s.get("sessions"),
                "avg_duration_sec": s.get("avgDur"),
                "bounce_rate": s.get("bounce"),
                "visitors": {"human": s.get("humans"), "ai_bot": s.get("ai"),
                             "search_engine": s.get("search"), "tool": s.get("tool")},
                "returning_visitors": s.get("retV"),
            },
            "top_pages": (a.get("pages") or [])[:8],
            "top_countries": (a.get("countries") or [])[:8],
            "ai_crawler_breakdown": (a.get("bots") or [])[:12],
            "daily_trend": a.get("daily", []),
            "source": "feegate.org nginx access log (bot-filtered)",
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8801"))
    print(f"[mcp] network={NETWORK} pay_to={PAY_TO} catalog={CATALOG_JSON}")
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
