# LLM-Assisted Shell: Design Prompt & Specification

## One-line pitch

A shell that behaves like a normal shell (zsh/bash) but has a single key or short sequence that opens an LLM assistant: the user describes what they want in plain language, the LLM suggests a shell command, and the user can accept it (e.g. Tab) so it appears on the command line and can be run or edited.

---

## Core concept

- **Primary mode**: Normal shell. All standard shell behavior: running commands, pipes, redirects, history, completion, etc.
- **Assist mode**: Triggered by a dedicated key or key sequence. User types a short natural-language request (e.g. “find all files in this directory that contain n22 in the name”). The system sends this (plus optional context) to a user-configured LLM. The LLM returns a suggested shell command. The suggestion is shown inline or in a small overlay; the user can accept it (e.g. Tab or Enter) to insert it into the current command line, or dismiss/cancel without changing the line.
- **Friction**: Minimal. One key to enter assist mode, type the intent, one key to accept. No leaving the terminal, no copy-paste of commands unless the user wants to edit.

---

## User experience (target)

1. User is at a shell prompt: `$ `
2. User presses the **assist key** (e.g. a function key, or `Ctrl+Space`, or a short sequence like `\e\e`).
3. **Assist mode** opens: e.g. a small prompt or overlay, or the current line is prefixed (e.g. `ask: ` or `? `).
4. User types in natural language, e.g.:  
   `find all files in the directory that contain n22 in the name`
5. User presses **submit** (e.g. Enter).
6. System calls the configured LLM with this text (and optional context). User sees a brief “thinking” or loading state.
7. LLM returns a single suggested command, e.g.:  
   `find . -name '*n22*'`
8. This is shown as the **suggested command** (inline or in a hint line).
9. User can:
   - **Accept**: e.g. Tab (or a dedicated “accept” key) → the suggested command is inserted into the shell buffer (replacing the “ask: …” line or at cursor). User can then run it (Enter) or edit first.
   - **Reject**: e.g. Escape or another key → suggestion is discarded, assist mode closes, command line returns to previous state or empty.
10. Optional: **Replace vs append** — accept could replace the current line or append after cursor; one default (e.g. replace) keeps the model simple.

---

## Functional requirements

### Shell behavior

- Runs real shell commands (zsh and/or bash).
- Supports normal shell features: pipes, redirects, background jobs, history, completion, line editing (where applicable).
- No change to how commands are executed; only the way the **current line** can be generated is extended (via LLM assist).

### Assist trigger

- One or more **user-configurable** trigger(s):
  - Key: e.g. `F2`, `Ctrl+Space`, `Ctrl+;`.
  - Or escape sequence: e.g. `\e\e` (double Escape) or `\e a` (Escape then ‘a’).
- Trigger is **non-invasive**: it should not conflict with common readline/zle bindings (or conflicts should be documented and overridable).

### LLM integration (user-configurable)

- **Provider / endpoint**: User can configure how the LLM is called, e.g.:
  - OpenAI API (GPT-4, etc.)
  - Anthropic (Claude)
  - Local model (Ollama, llama.cpp server, etc.)
  - Any HTTP-compatible API that accepts a prompt and returns text.
- **Authentication**: API keys or tokens via config or env (e.g. `OPENAI_API_KEY`), never hardcoded.
- **Model / parameters**: Model name and optional params (temperature, max_tokens) configurable so the user can tune for “one good command” vs creativity.

### Context sent to the LLM (configurable)

- **Minimum**: The user’s natural-language request only.
- **Optional context** (each toggleable in config):
  - Current working directory.
  - OS / shell name (e.g. “zsh on macOS”).
  - Last N commands from history (with option to anonymize).
  - Current buffer content (if user was editing a command and then triggered assist).
- **System prompt**: User can set a system prompt that instructs the LLM to “output only a single shell command, no explanation, safe for POSIX/zsh/bash” (or similar), so parsing is trivial.

### Parsing LLM response

- **Preferred**: LLM is instructed (via system prompt) to return **only** the shell command, one line, no markdown, no explanation. Then the client uses the first non-empty line (or whole response) as the command.
- **Fallback**: Strip markdown code fences (e.g. ` ```bash … ``` `) and take the first line that looks like a command (e.g. starts with a known program or `$` and strip the `$`). This keeps the implementation simple and robust.

### Acceptance / rejection

- **Accept** (e.g. Tab or Enter in “suggestion shown” state): Insert the suggested command into the shell buffer; exit assist mode; cursor at end of inserted text (or as configured).
- **Reject** (e.g. Escape): Discard suggestion; exit assist mode; restore previous line or clear assist UI.
- Keys for accept/reject should be **configurable** so they don’t clash with readline/zle.

### Safety and clarity

- **No auto-execution**: The suggested command is only **inserted**; the user must press Enter to run it. No “run this for me” unless the user explicitly configures that.
- **Visibility**: The inserted command is visible and editable so the user can check before running.
- Optional: configurable “dangerous command” warning (e.g. `rm -rf`, `sudo`, `curl | sh`) that highlights or asks for confirmation before run — can be phase two.

---

## Non-goals (to keep scope clear)

- Full conversational multi-turn chat in the shell (only “one request → one suggested command”).
- Executing commands without user acceptance (unless explicitly added as an opt-in later).
- Replacing the shell’s own completion; this is an **add-on** for “I don’t know the exact command” not “complete this word.”

---

## Implementation sketch (zsh / bash)

- **Zsh**: Use **ZLE** (Zsh Line Editor). Widget that:
  - On trigger key: switch to a custom “assist” state; optionally change prompt to `ask: `; collect input until submit.
  - On submit: send request to LLM (via a small helper script or built-in HTTP call); show loading.
  - On response: show suggestion and bind Tab to “accept” (insert text, restore normal state) and Escape to “reject.”
- **Bash**: Use **readline** custom bindings. On trigger, either:
  - Start a small “sub-prompt” (e.g. via `read -e` in a wrapper that runs before the main readline loop), or
  - Use a readline macro/callback that opens an external helper (e.g. a tiny GUI or `fzf`-style TUI) that returns the chosen command; the wrapper then injects it into the current line.
- **Shared**: A small **helper script** (e.g. `conch-ask` or `shell-llm`) that:
  - Reads stdin or args: user’s natural-language request.
  - Optionally reads context (cwd, shell, last command) from env or args.
  - Builds a prompt (system + user), calls the configured LLM API.
  - Prints **only** the suggested command to stdout. Shell integration captures this and inserts it.

This keeps the “LLM client” separate from the shell so it can be reused (e.g. from bash, zsh, or a future custom shell).

---

## Example flow (concrete)

- User: `$ ` → presses **Ctrl+Space**.
- Prompt becomes: `ask: `.
- User types: `find all files in the directory that contain n22 in the name` → Enter.
- System shows: “Thinking…” then suggestion: `find . -name '*n22*'`.
- User presses **Tab** → line becomes: `$ find . -name '*n22*'`.
- User presses **Enter** → command runs. No extra steps, minimal friction.

---

## Config file (minimal example)

```ini
# Trigger
trigger_key = "\e "          # Escape then Space
accept_key  = "\t"           # Tab
reject_key  = "\e"           # Escape

# LLM (example: OpenAI)
provider = "openai"
api_key_env = "OPENAI_API_KEY"
model = "gpt-4o-mini"

# Context
send_cwd = true
send_os_shell = true
send_history_count = 0

# System prompt (optional override)
system_prompt = "You are a shell assistant. Reply with exactly one shell command, no explanation, safe for the current OS. No markdown."
```

---

## Success criteria

- A user can, in under 10 seconds, trigger assist, type a short intent, and have a correct shell command inserted and ready to run or edit.
- No need to leave the terminal or copy-paste from a browser or another app.
- Works with the user’s chosen shell (zsh/bash) and their chosen LLM (configurable).
- Safe by default: suggest only, user always confirms by accepting and then pressing Enter to run.

---

## Name ideas

- **Conch** — “ask the shell” (fits the workspace name).
- **Shell Ask**, **Ask Shell**, **Clam** (CLI + LLM), **Coil** (command from intent line).

Use this as the design prompt for implementing the system or for discussing tradeoffs (e.g. key bindings, context sent to the LLM, and zsh vs bash first).
