---
name: vaultswap-trade-assistant
description: >-
  Provides runbooks and query workflows for inspecting MTG cards that pod members have put up for trade,
  checking card metadata and Scryfall valuations, finding Mighty Meeple buylist snapshots, and exploring
  pod trades using the VaultSwap MCP server tools.
---

# VaultSwap Trade Assistant Skill

Use this skill when the user asks questions about MTG cards up for trade in the pod, card valuations, Mighty Meeple buylist values, or available trade partners.

---

## 1. Prerequisites: MCP Server Setup

To enable Gemini/Antigravity to call the tools in this skill, register the VaultSwap MCP server in `~/.gemini/config/mcp_config.json` (or workspace `.agents/mcp_config.json`):

```json
{
  "mcpServers": {
    "vaultswap-trades": {
      "command": "C:\\Users\\jpmcl\\Python Development\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\jpmcl\\Python Development\\VaultSwap\\mcp_server.py"]
    }
  }
}
```

Once registered, Gemini will automatically load the tools below into its toolset.

---

## 2. Available MCP Tools Reference

Tool Name | Description | Key Parameters
:--- | :--- | :---
`list_trade_cards` | Query and filter active trade listings across the pod. | `query`, `status` (`all`/`for_trade`/`meeple_bound`), `owner`, `set_code`, `type_line`, `finish`, `condition`, `min_price`, `max_price`, `meeple_in_stock`, `sort_by`, `limit`, `offset`
`get_trade_card_metadata` | Deep dive into specific listings and cross-printing availability. | `item_id`, `scryfall_id`, `card_name`, `include_live_scryfall` (bool), `include_cached_meeple` (bool)
`list_pod_traders` | Overview of all active traders, their handles, and binder valuations. | _None_
`get_pod_trade_summary` | Aggregate pod trade metrics, top cards by value, and multi-copy cards. | _None_

---

## 3. Workflows & Playbooks

### Workflow A: "What cards are currently up for trade?"
1. Call `list_trade_cards(status="all", sort_by="priority", limit=20)`.
2. Cards with status `meeple_bound` are presented first because they are about to be sold to Mighty Meeple unless claimed by someone in the pod.
3. For each listing, show:
   - Card name, set code, collector number, finish (`foil`, `etched`, `nonfoil`), condition.
   - Quantity available.
   - Trader handle (e.g. `@Jason` or Discord handle).
   - Scryfall unit market price and Mighty Meeple buylist cash/credit quotes (if present).

### Workflow B: "Who has [Card Name] for trade?"
1. Call `get_trade_card_metadata(card_name="<Card Name>", include_live_scryfall=True, include_cached_meeple=True)`.
2. Inspect the returned `listings`:
   - Lists exact matches with trader handle, finish, and condition.
3. Check `other_printings_in_pod`:
   - If other pod members have different printings of the same card (grouped by `oracle_id`), mention them as alternate options!
4. Note any Mighty Meeple buylist quotes or in-stock retail prices.

### Workflow C: "Who has the biggest trade binder / what's in the pod trade pool?"
1. Call `get_pod_trade_summary()` for macro statistics (total listings, cards available, aggregate Scryfall market value, total store credit value).
2. Call `list_pod_traders()` to list all members with active listings, ranked by available cards and market value.

---

## 4. Privacy & Safety Guidelines
- Cards with `status == "private"` are never returned by any tool.
- Respect user contact boundaries: use `handle_label` and `discord_handle` for trader mentions; never assume or query private email credentials.
