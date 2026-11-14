# Local models and optional subagents

Start with [Conch seat sign-in](../README.md#first-launch) for your main assistant.
Local inference is optional and does not require an Anthropic or OpenAI API key.

There are two separate configurations:

- **Conch** delegates through the `delegate_task` tool and Conch skills under
  `~/.config/conch/skills/`. Delegation inherits the parent's permissions.
- **OMP**, the coding assistant used to develop Conch, discovers named agents
  under `~/.omp/agent/agents/` and providers in `~/.omp/agent/models.yml`.
  Those files do not configure Conch. OMP is not required to use Conch.

## Example LAN endpoints

These are example addresses from one private LAN, not public services or shipped
Conch defaults. Substitute your own hosts and the exact IDs from `/v1/models`.
Never expose an unauthenticated inference server directly to the internet.

| Endpoint | Model ID | Deployment | Served context |
| --- | --- | --- | --- |
| `http://192.168.1.226:8080/v1` | `qwen3.8-27b-q8` | Burt, A6000, Q8_0; higher fidelity, slower | 32,768 tokens |
| `http://192.168.1.193:8080/v1` | `qwen3.8:27b` | Fast box, Q4_K_M; faster, larger context | 262,144 tokens |

Optional fleet catalog: `http://192.168.1.226:8642` (llama-idx).
A model being listed is not proof it supports tools; Conch probes tool calling
before selecting a custom endpoint.

## Connect Conch to the registry

Inside `conch`, run:

```text
/registry set http://192.168.1.226:8642
/llamaidx
/models
```

The registry must be reachable from the machine running Conch. Use names exactly
as `/models` lists them. For the example registry above:

```text
/model llamaidx/fast-27b/qwen3.8:27b
/model llamaidx/burt-27b/qwen3.8-27b-q8
```

Each `/model` command changes the **main Conch session**, not just a subagent.
Connect the registry without selecting a model if you want to keep your seat as
the main provider. Return to a seat with `/provider claude-code` or `/provider codex`;
that requires a working native login. `/registry off` removes registry discovery.

The registry is discovery metadata, not an inference proxy in Conch. Conch calls
the discovered model endpoint directly, so both catalog and inference addresses
must be reachable. A protected catalog uses `llamaidx_token_env` to name its
read-token environment variable; never put the token itself in the config.

## Keep a seat parent and delegate to a local Conch skill

Conch currently has one configured custom endpoint. Add these settings to your
existing `~/.config/conch/config`, leaving its `provider`, `model`, and `chat_model`
seat selection unchanged:

```ini
custom_base_url = http://192.168.1.193:8080/v1
custom_model = qwen3.8:27b
subagent_rounds = 6
```

For this unauthenticated example, no custom API-key variable is needed. Create
`~/.config/conch/skills/fast-local.md` (create the `skills` directory if needed):

```markdown
---
name: fast-local
description: Read-only supporting analysis on the local fast box.
provider: custom
model: qwen3.8:27b
tools: local_shell
rounds: 6
---
Analyze only the assigned material. Use read-only commands, do not change files,
and return concise findings with file paths. If a required command is denied,
report the permission blocker rather than bypassing it.
```

Start a new `conch` session so it loads the config, run `/skills` to confirm
`fast-local` appears, then ask:

```text
Use delegate_task with skill fast-local to summarize this repository's README.
Keep the main conversation on the current seat provider.
```

This asks the main model to invoke `delegate_task` with `skill: "fast-local"`;
it is not a shell command. `/skill fast-local` alone loads the procedure into
the main conversation and does **not** launch a child.

Delegated work runs serially with a bounded tool budget. Children cannot grant
themselves more permission than the parent, recursively delegate, or open an
interactive approval prompt. A denied command is a blocker, not permission to
retry through a less restricted tool.

To use Burt instead, change the custom endpoint to
`http://192.168.1.226:8080/v1` and change **both** `custom_model` and the skill's
`model:` to `qwen3.8-27b-q8`. Keep tasks and read sets small enough for its 32K
window. A skill's `provider: custom` selects Conch's configured custom endpoint;
it cannot supply a second independent base URL. An unusable skill model preference
can fall back to the parent's model with a warning, so check the delegation output
rather than assuming it ran locally.

## Optional: make both endpoints named OMP subagents

This section applies to **OMP users**, not Conch runtime configuration. Keep your
existing primary model and other providers. Merge these entries under the
`providers:` mapping in `~/.omp/agent/models.yml` (do not overwrite other entries):

```yaml
providers:
  burt:
    baseUrl: http://192.168.1.226:8080/v1
    api: openai-completions
    auth: none
    models:
      - id: qwen3.8-27b-q8
        name: Burt Qwen 3.8 27B Q8 (A6000, 32K)
        reasoning: true
        input: [text]
        supportsTools: true
        contextWindow: 32768
        maxTokens: 4096
        tokenizer: qwen3
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
        compat:
          supportsStore: false
          supportsDeveloperRole: false
          supportsReasoningEffort: false
          thinkingFormat: qwen-chat-template
          maxTokensField: max_tokens
  fast-box:
    baseUrl: http://192.168.1.193:8080/v1
    api: openai-completions
    auth: none
    models:
      - id: qwen3.8:27b
        name: Fast Box Qwen 3.8 27B Q4 (262K)
        reasoning: true
        input: [text]
        supportsTools: true
        contextWindow: 262144
        maxTokens: 8192
        tokenizer: qwen3
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
        compat:
          supportsStore: false
          supportsDeveloperRole: false
          supportsReasoningEffort: false
          thinkingFormat: qwen-chat-template
          maxTokensField: max_tokens
```

Create `~/.omp/agent/agents/burt.md`:

```markdown
---
name: burt
description: Local Q8 supporting work; higher fidelity, slower, 32K context.
model: burt/qwen3.8-27b-q8
thinking-level: low
tools: [read, grep, glob, find, edit, write, bash]
spawns: []
---
Complete only the assigned bounded task. Keep reads small enough for 32K context.
Return concrete findings and changed paths. Do not run checks unless assigned.
```

Create `~/.omp/agent/agents/fast-box.md`:

```markdown
---
name: fast-box
description: Faster local Q4 supporting work with a 262K context window.
model: fast-box/qwen3.8:27b
thinking-level: low
tools: [read, grep, glob, find, edit, write, bash]
spawns: []
---
Complete only the assigned task and respect its file boundaries.
Return concrete findings and changed paths. Do not run checks unless assigned.
```

Create the `agents` directory if missing. Verify model registration:

```bash
omp models burt --json
omp models fast-box --json
```

Registration checks configuration, not successful inference or tool use. In OMP,
ask: "Use the burt and fast-box subagents to read separate small files and report
one fact from each. Do not edit anything." Confirm each child uses its named local
model and completes a real read tool call. The primary assistant's model should
remain unchanged.

OMP has no native llama-idx discovery adapter in this setup. The registry URL
above is a metadata reference; do not add an unsupported `llamaidx` discovery
type to `models.yml`. Register the two inference routes directly as shown.
