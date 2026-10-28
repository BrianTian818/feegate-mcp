# Feegate Marketplace MCP Server

Official **Model Context Protocol** server of the [Feegate](https://feegate.org) data marketplace — listed in the [official MCP Registry](https://registry.modelcontextprotocol.io) as **`org.feegate/marketplace`**.

Feegate helps AI-agent-facing data providers monetize APIs with [x402](https://www.x402.org): buyers pay per call in **USDC on Base**, no accounts, no API keys, no subscriptions. This server exposes Feegate's own product catalog and platform metrics over MCP with the same pay-per-call model — Feegate runs on the protocol it sells.

## Connect

Remote server (streamable HTTP, no installation):

```
https://feegate.org/mcp
```

**Claude Code / CLI**

```bash
claude mcp add --transport http feegate-marketplace https://feegate.org/mcp
```

**Cursor / Claude Desktop (mcpServers JSON)**

```json
{
  "mcpServers": {
    "feegate-marketplace": {
      "url": "https://feegate.org/mcp"
    }
  }
}
```

## Tools

| Tool | Price (USD / call) | Description |
|---|---|---|
| `ping` | Free | Liveness check + price list |
| `fee_quote` | Free | Current x402 pay-per-call prices |
| `list_products` | Free | Product catalog (id, title, category, price per call) |
| `product_detail` | $0.002 | Full detail for one data product |
| `marketplace_catalog` | $0.005 | Full catalog export with seller economics |
| `platform_stats` | $0.005 | Live platform traffic & AI-crawler analytics |

## How payment works (x402)

1. Your MCP client calls a paid tool.
2. The server responds `HTTP 402` with a signed payment request (EIP-3009 `transferWithAuthorization`).
3. The client pays **USDC on Base** (`eip155:8453`) — gas is sponsored by the facilitator; buyers never need ETH.
4. The call is retried with the payment header and returns the data. Settlement is verified on-chain.

No account, no API key, no minimum — one HTTP round trip, one micropayment.

## Links

- Website: <https://feegate.org>
- MCP directory (all Feegate MCP servers): <https://feegate.org/connect>
- Pricing: <https://feegate.org/pricing>
- Docs: <https://feegate.org/docs>
- Machine-readable price catalog: <https://feegate.org/pricing.json>

## For data providers

If you have data (market data, research, rankings, monitoring, anything an agent would pay for), Feegate handles hosting, x402 payment rails, promotion and settlement. See [feegate.org/jieshao](https://feegate.org/jieshao/).

## License

MIT
