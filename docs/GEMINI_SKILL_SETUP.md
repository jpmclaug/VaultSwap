# How to Set Up the VaultSwap Trade MCP & Gemini Skill

This guide explains how to connect Gemini / Antigravity to the **VaultSwap Trade MCP Server** and activate the custom skill.

---

## 1. How the Architecture Works

There are two primary environments where you can use this MCP server with Gemini:
1. **Desktop / Antigravity IDE**: Runs the server directly as a local Python process over standard I/O (`stdio`).
2. **Gemini Mobile App (iOS / Android)**: Mobile operating systems cannot run local Python processes directly. Google routes custom MCP tools through your personal Google Account sync: you link your server's public HTTPS endpoint on the web, and it instantly syncs to the Gemini mobile app.

---

## 2. Using with the Gemini Mobile App (iOS & Android)

### Why this flow is required
On mobile devices (phones/tablets), you cannot paste local Windows `.exe` paths or command lines. Instead, Google uses **Connected Apps** synced across your account.

### Step-by-Step Mobile Setup:
1. **Host or Tunnel your MCP Server**:
   The MCP server needs to be accessible via HTTPS. You can:
   - Run VaultSwap in Streamable HTTP mode on Cloud Run:
     ```bash
     python mcp_server.py --transport streamable-http --port 8000
     ```
   - Or use a free tunnel (e.g. ngrok or Cloudflare Tunnel) while developing locally:
     ```bash
     ngrok http 8000
     ```
2. **Link the MCP URL on Gemini Web**:
   - Open a browser (on your phone or computer) and go to **[gemini.google.com](https://gemini.google.com)** or **[gemini.google.com/devmode](https://gemini.google.com/devmode)**.
   - Ensure you are logged into the **same personal Google account** as your mobile app.
   - Go to **Settings > Connected Apps** and tap **+ Create new / Add Custom App**.
   - Paste your server's public HTTPS URL (e.g., `https://your-service.run.app/mcp` or `https://xyz.ngrok-free.app/mcp`).
   - Save the app (e.g., named **VaultSwap**).
3. **Use on your Phone**:
   - Open the **Gemini mobile app** on your iOS or Android device.
   - In any chat, type `@VaultSwap` or simply ask:
     - *"What cards are currently up for trade in the pod?"*
     - *"Who has a Sol Ring or Rhystic Study?"*
     - *"Show me all foil cards available for trade over $10."*
   - Gemini on your phone will execute the tools on your MCP server and respond with real-time trade listings and Mighty Meeple quotes!

---

## 3. Using on Desktop / Antigravity (Local stdio)

To make the tools callable by Gemini across all sessions, add the server to your Gemini config.

### Global Configuration (Recommended)
Edit `C:\Users\jpmcl\.gemini\config\mcp_config.json`:

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

> [!TIP]
> You can also place this in your project root at `.agents/mcp_config.json` if you prefer project-local scoping.

---

## 3. Step 2: Set Up the Skill

The skill instruction file has been placed in your workspace at:
👉 **[SKILL.md](file:///c:/Users/jpmcl/Python%20Development/VaultSwap/.agents/skills/vaultswap-trade-assistant/SKILL.md)**

### Directory Structure
```text
c:\Users\jpmcl\Python Development\VaultSwap\
└── .agents\
    └── skills\
        └── vaultswap-trade-assistant\
            └── SKILL.md
```

### What's Inside `SKILL.md`:
- **Trigger**: Activates automatically whenever the user asks about MTG pod trades, card availability, pricing, or buylist values.
- **Tool Mapping**: Directs Gemini to use:
  - `list_trade_cards` to browse and filter cards.
  - `get_trade_card_metadata` to look up individual cards, cross-printing alternatives, and Mighty Meeple quotes.
  - `list_pod_traders` to find trade partners and binder sizes.
  - `get_pod_trade_summary` for high-level pod trade stats.
- **Best Practices**: Ensures `meeple_bound` cards surface with priority (cards about to leave the pod) and reinforces private binder privacy.

---

## 4. Step 3: Verifying the Setup

1. **Verify MCP Execution**:
   Run the test command in PowerShell to verify the server runs smoothly:
   ```powershell
   & "..\.venv\Scripts\python.exe" mcp_server.py --help
   ```
2. **Restart Gemini / Antigravity Session**:
   Once `mcp_config.json` is saved, reload or start a new conversation.
3. **Ask Natural Language Questions**:
   You can now ask questions like:
   - *"What cards are currently up for trade in the pod?"*
   - *"Does anyone have a Rhystic Study or Cyclonic Rift for trade?"*
   - *"Who has cards marked for the Mighty Meeple buylist?"*
   - *"Show me all foil cards available for trade over $10."*
