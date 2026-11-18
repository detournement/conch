# Databricks Capitol Builder MCP

Last verified: 2026-10-14

## Live resources

- Local gateway: `http://localhost:8135/capitol-gateway/gateway/mcp`
- Durable public endpoint:
  `https://capitol-builder.usecapitol.com/capitol-gateway/gateway/mcp`
- Cloudflare tunnel: `capitol-databricks-builder`
  (`6d63c023-e600-4611-adfb-d8fdd4429c51`)
- Tunnel supervisor: LaunchAgent
  `ai.capitol.cloudflare-builder-gateway`
- Databricks HTTP connection:
  `workspace.default.capitol_builder_gateway`
  (`e23c2a3f-cc5b-424b-ad61-1bcffc90e1b1`)
- Databricks MCP Service: `workspace.default.capitol_builder`
  (`ec1f436a-4c88-4323-959f-f07ee36964b6`)
- Databricks Unity Gateway endpoint:
  `https://dbc-a4a1ea74-ea51.cloud.databricks.com/ai-gateway/mcp-services/workspace.default.capitol_builder`

The pre-existing `workspace.default.capitol_bridge` service and its
`capitol_gateway_bridge` connection are separate and must not be changed.

## Security boundary

The HTTP connection holds a dedicated `ctr_` bearer. The gateway stores only
its hash. The key is bound to server slug `gateway`, organization
`7d577196-ffad-46ba-8d28-ac7f4c85294d`, and capability scopes `run` and
`builder`. Its key id is
`1f33821e1af9fad48132b0573c026e9eb3ab7075af8aa9b044628fec5ee077f7`.
No plaintext copy is kept in this repository or in the state file.

The generic gateway projection is active with `runs_enabled=true` and
`writes_enabled=true`; its generic client enables current-state runs for any
workflow in the same organization. Databricks narrows that broader
scope to exactly:

1. `list_workflows`
2. `describe_workflow`
3. `create_workflow_draft`
4. `run_workflow`
5. `get_run_status`

`publish_workflow_draft`, agent tools, data tools, and file tools are not
selected. Drafts are runnable without publishing.

The current Databricks user, `tom@capitol.ai`, owns both the connection and
MCP Service and is in the workspace `admins` group, which owns
`workspace.default`. No redundant grants were added. A successful
Unity Gateway `tools/list` and `list_workflows` call proved effective
`USE_CATALOG`, `USE_SCHEMA`, and service execution access.

## Health checks

```bash
curl -fsS https://capitol-builder.usecapitol.com/health
cloudflared tunnel info capitol-databricks-builder
launchctl print \
  "gui/$(id -u)/ai.capitol.cloudflare-builder-gateway"
```

Expected tunnel state: one Darwin connector with four edge connections.
Expected Databricks `tools/list`: exactly the five tools listed above.

Evidence:

- `/Users/thom/composer/databricks-capitol-builder-gateway-tools-20261014.json`
- `/Users/thom/composer/databricks-capitol-builder-e2e-20261014T190900Z.json`
- `/Users/thom/composer/databricks-state.json`

## Bearer rotation

1. Mint a new `gateway`-slug key with only `run` and `builder` scopes.
2. Update the `workspace.default.capitol_builder_gateway` connection's
   `bearer_token`; do not update `capitol_gateway_bridge`.
3. Verify the Databricks service with `tools/list` and a read-only
   `list_workflows` call.
4. Revoke the old key by its key id only after verification succeeds.
5. Never print or commit either bearer.

## Recovery

The hostname is stable; a connector restart does not require a Databricks
connection update.

```bash
launchctl kickstart -k \
  "gui/$(id -u)/ai.capitol.cloudflare-builder-gateway"
```

Tunnel configuration is in
`~/.cloudflared/capitol-databricks-builder.yml`; its credential JSON and the
gateway bearer are secrets and must remain outside source control.
