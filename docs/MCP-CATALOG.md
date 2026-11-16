# Curated MCP catalog

`plugin-catalog/` is a small Agent Plugins marketplace for Conductor. It is not
part of the application bundle; point the deployment at it to make Discover
show these entries:

```dotenv
PLUGIN_MARKETPLACE_URL=https://github.com/LeoLin990405/Conductor/tree/main/plugin-catalog
# The Conductor repository is private in this workspace; a read-only GitHub
# token is required to fetch the catalog and its package trees.
PLUGIN_MARKETPLACE_TOKEN=github_pat_...
```

Then restart the backend and open **Plugins → Discover**. A public mirror of
the same catalog only needs the URL.

## First batch

### Playwright browser (`playwright-browser`)

- package: `mcp` stdio, `npx -y @playwright/mcp@latest --headless`
- no API key
- use when a page needs a real browser: JS-rendered filings, exchange search,
  anti-bot pages; WebFetch remains the default for simple article pages
- runs inside the existing sandbox, using the Chromium already installed there

### GitHub (`github-mcp`)

- package: GitHub's official remote MCP server
  (`https://api.githubcopilot.com/mcp/`)
- requires `GITHUB_PERSONAL_ACCESS_TOKEN`; the install wizard opens a secret
  field and the value is injected as `Authorization: Bearer …`
- read-only unless the token itself has write scopes

## Deliberately deferred

**The external datasets MCP server** is not in this batch. Its upstream
repository ships a bare `server.py` with no installable package entry point, so
a plugin wrapper cannot run it from the sandbox without vendoring the source.
Adding it properly means packaging a small MCP adapter (or publishing a wheel)
and pinning its license; that is a separate change, not a catalog URL.
