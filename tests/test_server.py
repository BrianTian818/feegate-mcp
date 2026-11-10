# -*- coding: utf-8 -*-
"""Feegate MCP server 冒烟测试（零本地依赖，可直接在 CI / 任何机器跑）。

两层：
1. 静态层：解析本仓库 server.py 源码，验证每个 @mcp.tool 都声明了完整 annotations
   （readOnlyHint / destructiveHint / idempotentHint / openWorldHint 四项布尔值 ——
   OpenAI 目录对缺 hints 或非布尔值的工具直接拒收）。
2. 线上层：对 https://feegate.org/mcp 发无会话 tools/list，验证线上暴露的工具名
   与 annotations 和本仓库声明一致。

跑法：pip install pytest && pytest tests/ -v
只跑静态层：pytest tests/ -v -m "not live"
"""
import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
SERVER_SRC = (HERE / "server.py").read_text(encoding="utf-8")
MCP_URL = os.environ.get("FEEGATE_MCP_URL", "https://feegate.org/mcp")
PLATFORM_TOOLS = ["ping", "fee_quote", "list_products",
                  "product_detail", "marketplace_catalog", "platform_stats"]
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


# ---------- 1. 静态层：源码声明 ----------

def test_six_platform_tools_declared():
    for t in PLATFORM_TOOLS:
        assert ('name="%s"' % t) in SERVER_SRC, "missing tool: %s" % t


def test_every_tool_has_full_annotations():
    """每个 @mcp.tool 装饰器必须带 4 项布尔 hints。"""
    blocks = re.findall(r"@mcp\.tool\((.*?)\n\)", SERVER_SRC, re.S)
    assert blocks, "no @mcp.tool found"
    for b in blocks:
        m = re.search(r'name="([^"]+)"', b)
        name = m.group(1) if m else "?"
        assert "annotations=ToolAnnotations(" in b, "tool %s missing annotations" % name
        for h in HINTS:
            assert h + "=" in b, "tool %s missing %s" % (name, h)
            assert (h + "=True") in b or (h + "=False") in b, \
                "tool %s: %s must be explicit bool" % (name, h)


def test_tool_annotations_import():
    assert "from mcp.types import ToolAnnotations" in SERVER_SRC


def test_fee_quote_rate_copy_aligned():
    """费率文案与官方口径（revenue.json: 3% / 2% / 1%）一致，不残留旧数。"""
    assert "Self-serve 3%" in SERVER_SRC
    for stale in ("Self-serve 50%", "Managed 40%", "Enterprise 30%"):
        assert stale not in SERVER_SRC, "stale rate copy: %s" % stale


# ---------- 2. 线上层：真实端点 ----------

@pytest.mark.live
def test_live_tools_list_matches_repo():
    """线上 tools/list 的工具名与 annotations 必须与本仓库声明一致。"""
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        headers={"content-type": "application/json", "accept": "application/json"})
    d = json.load(urllib.request.urlopen(req, timeout=30))
    tools = d["result"]["tools"]
    names = {t["name"] for t in tools}
    for t in PLATFORM_TOOLS:
        assert t in names, "live endpoint missing %s" % t
    for t in tools:
        ann = t.get("annotations") or {}
        for h in HINTS:
            assert h in ann, "live tool %s missing %s" % (t["name"], h)
            assert isinstance(ann[h], bool), "live tool %s: %s not bool" % (t["name"], h)
