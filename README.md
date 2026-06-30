# Home Assistant MCP Add-on Repository

Public Home Assistant add-on repository for the MCP Companion add-on.

This repository must not contain tokens, private Home Assistant configuration,
backup files, logs, entity dumps, or private URLs.

## Installation

1. Open Home Assistant.
2. Go to Settings -> Add-ons -> Add-on Store.
3. Open the menu and choose Repositories.
4. Add the public repository URL:

```text
https://github.com/pbadinger/homeassistant-mcp-addon
```

5. Install the MCP Companion add-on.
6. Set a strong `companion_token` in the add-on configuration.
7. Start the add-on.

The local private MCP server must use the same companion token in its local
environment. Do not commit that token.

## Add-ons

- `mcp-companion`: small authenticated service that gives the local MCP server
  tightly scoped Home Assistant internal capabilities.
