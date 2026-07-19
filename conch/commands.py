"""Slash command handling."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .browser import browse_conversations
from . import composio as composio_mod
from .providers import (
    DEFAULT_API_KEY_ENVS,
    KNOWN_MODELS,
    RAW_FNS,
    get_fallback_model,
    get_ollama_base_url,
    list_ollama_models,
    ollama_model_matches,
    validate_ollama_model,
)
from .scheduler import _format_interval, _parse_interval
from .tooling import (
    activate_profile,
    active_profile_name,
    get_agent_mode,
    group_tools,
    list_profiles,
    load_tool_prefs,
    save_tool_prefs,
)


# ---------------------------------------------------------------------------
# User-defined slash commands (plan 1.7): markdown files in
# ~/.config/conch/commands/ become /name commands; the file body is a prompt
# template with $ARGUMENTS interpolation.
# ---------------------------------------------------------------------------

_USER_COMMAND_NAME_RE = re.compile(r"[a-z0-9_-]+")


def user_commands_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "conch" / "commands"


def load_user_commands() -> Dict[str, str]:
    """Return {name: prompt template} for every *.md file in the commands dir."""
    commands: Dict[str, str] = {}
    directory = user_commands_dir()
    if not directory.is_dir():
        return commands
    for path in sorted(directory.glob("*.md")):
        name = path.stem.strip().lower()
        if not name or not _USER_COMMAND_NAME_RE.fullmatch(name):
            continue
        try:
            body = path.read_text().strip()
        except OSError:
            continue
        if body:
            commands[name] = body
    return commands


def render_user_command(template: str, arguments: str) -> str:
    """Interpolate $ARGUMENTS; append trailing args when no placeholder."""
    if "$ARGUMENTS" in template:
        return template.replace("$ARGUMENTS", arguments)
    if arguments:
        return template + "\n\n" + arguments
    return template


# ---------------------------------------------------------------------------
# Slash-command registry: single source for tab completion and the
# conch_introspect capabilities report (so neither goes stale).
# ---------------------------------------------------------------------------

SLASH_COMMANDS = [
    ("/help", "Show all commands"),
    ("/models", "List available models"),
    ("/model <name>", "Switch model"),
    ("/provider <name>", "Switch provider (cerebras, openai, anthropic, ollama, custom)"),
    ("/remember <text>", "Save a persistent memory"),
    ("/memories", "List memories"),
    ("/forget <id>", "Delete a memory"),
    ("/fact <text>", "Save an always-loaded fact (facts.md)"),
    ("/facts", "Show the always-loaded facts"),
    ("/skills", "List saved skills"),
    ("/skill <name> [task]", "Run a skill's procedure on a task"),
    ("/search <query>", "Search conversations, memories, and config"),
    ("/browse", "Interactive conversation browser"),
    ("/new", "Start a new conversation"),
    ("/convos", "List past conversations"),
    ("/switch <id>", "Switch conversation"),
    ("/delete <id>", "Delete conversation"),
    ("/clear", "Wipe conversation history (keep conversation)"),
    ("/agent", "Toggle agent mode (auto-execute shell)"),
    ("/yolo", "Alias for /agent"),
    ("/verbose", "Toggle showing tool args and results"),
    ("/schedule <interval> <prompt>", "Schedule a recurring task"),
    ("/tasks", "List scheduled tasks"),
    ("/cancel <id>", "Cancel a scheduled task"),
    ("/tools", "List tool groups"),
    ("/enable <group>", "Enable a tool group"),
    ("/disable <group>", "Disable a tool group"),
    ("/profile [name]", "Switch tool profile"),
    ("/profiles", "List tool profiles"),
    ("/connect <app>", "Connect a service via OAuth (Composio)"),
    ("/apps", "List connectable services"),
    ("/reload", "Reload MCP tools"),
    ("/rounds <n>", "Set max tool call rounds"),
    ("/queue", "Toggle typeahead input"),
    ("/status", "Show version, provider, model, context window, and config"),
    ("/cost", "Show session token usage and cost"),
]


def slash_command_names() -> List[str]:
    """Bare command names (first word of each registry entry)."""
    return [entry[0].split()[0] for entry in SLASH_COMMANDS]


def handle_slash_command(
    cmd: str,
    config: dict,
    provider: str,
    model_name: str,
    set_agent_mode,
    memory=None,
    all_tools: Optional[List[dict]] = None,
    tool_map: Optional[Dict[str, Any]] = None,
    sched=None,
    conv_mgr=None,
    current_conv=None,
    session_usage=None,
    messages=None,
) -> Optional[tuple]:
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/help", "/h", "/?"):
        print(
            "\n\033[1;36mSlash commands:\033[0m\n"
            "  \033[1m/models\033[0m              List available models\n"
            "  \033[1m/model <name>\033[0m        Switch model\n"
            "  \033[1m/provider <name>\033[0m     Switch provider (cerebras, openai, anthropic, ollama)\n"
            "  \033[1m/remember <text>\033[0m     Save a persistent memory\n"
            "  \033[1m/memories\033[0m            List memories\n"
            "  \033[1m/forget <id>\033[0m         Delete a memory\n"
            "  \033[1m/fact <text>\033[0m         Save an always-loaded fact (facts.md)\n"
            "  \033[1m/facts\033[0m               Show the always-loaded facts\n"
            "  \033[1m/skills\033[0m              List saved skills\n"
            "  \033[1m/skill <name> [task]\033[0m Run a skill's procedure on a task\n"
            "  \033[1m/search <query>\033[0m      Search conversations, memories, and config\n"
            "  \033[1m/browse\033[0m              Interactive conversation browser\n"
            "  \033[1m/new\033[0m                 Start a new conversation\n"
            "  \033[1m/convos\033[0m              List past conversations\n"
            "  \033[1m/switch <id>\033[0m         Switch conversation\n"
            "  \033[1m/delete <id>\033[0m         Delete conversation\n"
            "  \033[1m/clear\033[0m               Wipe conversation history (keep conversation)\n"
            "  \033[1m/agent\033[0m, \033[1m/yolo\033[0m       Toggle agent mode (auto-execute shell)\n"
            "  \033[1m/verbose\033[0m             Toggle showing tool args + output\n"
            "  \033[1m/schedule <interval> <prompt>\033[0m  Schedule a task\n"
            "  \033[1m/tasks\033[0m               List scheduled tasks\n"
            "  \033[1m/cancel <id>\033[0m         Cancel a scheduled task\n"
            "  \033[1m/tools\033[0m               List tool groups\n"
            "  \033[1m/enable <group>\033[0m      Enable a tool group\n"
            "  \033[1m/disable <group>\033[0m     Disable a tool group\n"
            "  \033[1m/profile [name]\033[0m      Switch tool profile (minimal, dev, comms, full)\n"
            "  \033[1m/connect <app>\033[0m       Connect a service\n"
            "  \033[1m/apps\033[0m                List connectable services\n"
            "  \033[1m/rounds <n>\033[0m          Set max tool call rounds (default 25)\n"
            "  \033[1m/queue\033[0m               Toggle typeahead (type while LLM works, on by default)\n"
            "  \033[1m/status\033[0m              Show provider, model, context window, and config\n"
            "  \033[1m/cost\033[0m                Show session token usage and cost\n"
            "  \033[1m/reload\033[0m              Reload MCP tools\n"
            "\n  Shell approval: \033[1my\033[0m/\033[1mEnter\033[0m=run  \033[1mn\033[0m=decline  \033[1me\033[0m=edit  \033[1ma\033[0m=always allow  \033[1mA\033[0m=agent mode on\n"
        )
        return None

    if command in ("/agent", "/yolo"):
        if arg in ("on", "true", "1"):
            set_agent_mode(True)
        elif arg in ("off", "false", "0"):
            set_agent_mode(False)
        else:
            set_agent_mode(not get_agent_mode())
        status = "\033[1;32mON\033[0m" if get_agent_mode() else "\033[31mOFF\033[0m"
        label = "YOLO mode" if command == "/yolo" else "Agent mode"
        print(f"\n  {label}: {status}")
        if get_agent_mode():
            print("  \033[2mLocal commands will auto-execute without confirmation.\033[0m")
        print()
        return "agent_mode_changed"

    if command == "/verbose":
        if arg in ("on", "true", "1"):
            return "verbose_on"
        if arg in ("off", "false", "0"):
            return "verbose_off"
        return "verbose_toggle"

    if command in ("/search", "/s", "/find", "/grep") and conv_mgr is not None:
        if not arg:
            print("\n  \033[2mUsage: /search <query>\033[0m\n")
            return None
        results = conv_mgr.search(arg)
        if not results:
            print(f"\n  \033[2mNo results for '{arg}'.\033[0m\n")
            return None
        print(f"\n  \033[1;36mSearch results for '{arg}' ({len(results)} conversations):\033[0m\n")
        for r in results:
            current = " \033[1;33m← current\033[0m" if current_conv and r["id"] == current_conv.id else ""
            print(f"  \033[1m{r['id']}\033[0m  {r['title']}  \033[2m({r['message_count']} msgs, score:{r['score']})\033[0m{current}")
            for m in r["matches"][:3]:
                role_color = "\033[33m" if m["role"] == "user" else "\033[36m"
                snippet = m["snippet"]
                for kw in arg.lower().split():
                    import re as _re
                    snippet = _re.sub(
                        f"({_re.escape(kw)})",
                        "\033[1;33m\\1\033[0m",
                        snippet,
                        flags=_re.IGNORECASE,
                    )
                print(f"    {role_color}{m['role']}\033[0m: {snippet}")
            print()
        return None

    if command in ("/browse", "/b") and conv_mgr is not None:
        current_id = current_conv.id if current_conv else ""
        result = browse_conversations(conv_mgr, current_id=current_id)
        if result == "new":
            return "new_conversation"
        if result and result != current_id:
            return ("switch_conversation", result)
        return None

    if command == "/clear":
        return "clear_conversation"

    if command == "/new" and conv_mgr is not None:
        return "new_conversation"

    if command == "/convos" and conv_mgr is not None:
        conversations = conv_mgr.list_all()
        if not conversations:
            print("\n  \033[2mNo past conversations.\033[0m\n")
            return None
        print(f"\n  \033[1;36mConversations ({len(conversations)}):\033[0m")
        for conversation in conversations[:20]:
            current = " \033[1;33m← current\033[0m" if current_conv and conversation["id"] == current_conv.id else ""
            print(
                f"    \033[1m{conversation['id']}\033[0m  {conversation.get('title', 'untitled')}"
                f"  \033[2m({conversation.get('message_count', 0)} msgs, {conversation.get('updated_at', '')[:16]})\033[0m{current}"
            )
        print()
        return None

    if command == "/switch" and conv_mgr is not None:
        if not arg:
            print("\n  \033[2mUsage: /switch <id>\033[0m\n")
            return None
        return ("switch_conversation", arg.strip())

    if command == "/delete" and conv_mgr is not None:
        if not arg:
            print("\n  \033[2mUsage: /delete <id>\033[0m\n")
            return None
        conv_id = arg.strip()
        if current_conv and conv_id == current_conv.id:
            print("\n  \033[31mCan't delete the current conversation. Switch first.\033[0m\n")
            return None
        if conv_mgr.delete(conv_id):
            print(f"\n  \033[1;32m✓ Deleted conversation {conv_id}\033[0m\n")
        else:
            print(f"\n  \033[31mNo conversation with ID {conv_id}\033[0m\n")
        return None

    if command == "/schedule" and sched is not None:
        if not arg:
            print("\n  \033[2mUsage: /schedule <description>\033[0m\n")
            return None
        first_word = arg.split()[0]
        run_once = False
        if first_word == "once":
            rest = arg.split(None, 1)[1] if " " in arg else ""
            first_word = rest.split()[0] if rest else ""
            run_once = True
        interval = _parse_interval(first_word)
        if interval and " " in arg:
            prompt = arg.split(None, 2 if run_once else 1)[-1]
            task = sched.add(prompt, interval, run_once=run_once)
            kind = "one-time" if run_once else f"every {_format_interval(interval)}"
            print(f"\n  \033[1;32m✓ Scheduled task #{task.id}\033[0m ({kind})")
            print(f"  \033[2m{prompt}\033[0m\n")
            return None
        parse_prompt = (
            "Extract the interval and task from this schedule request. "
            "Reply with ONLY a JSON object:\n"
            '{"interval_seconds": <number>, "prompt": "<task>", "run_once": <true/false>}\n\n'
            f"Request: {arg}"
        )
        raw_fn = RAW_FNS.get(provider)
        if raw_fn:
            response = raw_fn(config, [
                {"role": "system", "content": "You extract schedule parameters. Reply with ONLY valid JSON."},
                {"role": "user", "content": parse_prompt},
            ], None)
            match = re.search(r"\{[^}]+\}", response.get("content", ""))
            if match:
                try:
                    parsed = json.loads(match.group())
                    interval = int(parsed.get("interval_seconds", 0))
                    prompt = parsed.get("prompt", arg)
                    run_once = bool(parsed.get("run_once", False))
                    if interval > 0:
                        task = sched.add(prompt, interval, run_once=run_once)
                        kind = "one-time" if run_once else f"every {_format_interval(interval)}"
                        print(f"\n  \033[1;32m✓ Scheduled task #{task.id}\033[0m ({kind})")
                        print(f"  \033[2m{prompt}\033[0m\n")
                        return None
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
        print("\n  \033[31mCouldn't parse schedule. Try '/schedule 10m <task>'.\033[0m\n")
        return None

    if command == "/tasks" and sched is not None:
        tasks = sched.list_tasks()
        if not tasks:
            print("\n  \033[2mNo scheduled tasks.\033[0m\n")
            return None
        print(f"\n  \033[1;36mScheduled tasks ({len(tasks)}):\033[0m")
        for task in tasks:
            status = "\033[32mactive\033[0m" if task.active else "\033[31mstopped\033[0m"
            print(f"    \033[1m#{task.id}\033[0m [{status}] every {_format_interval(task.interval)} — {task.prompt}")
        print()
        return None

    if command == "/cancel" and sched is not None:
        if not arg:
            print("\n  \033[2mUsage: /cancel <id>\033[0m\n")
            return None
        try:
            task_id = int(arg.lstrip("#"))
        except ValueError:
            print(f"\n  \033[31mInvalid ID: {arg}\033[0m\n")
            return None
        if sched.cancel(task_id):
            print(f"\n  \033[1;32m✓ Cancelled task #{task_id}\033[0m\n")
        else:
            print(f"\n  \033[31mNo task with ID #{task_id}\033[0m\n")
        return None

    if command == "/remember" and memory is not None:
        if not arg:
            print("\n  \033[2mUsage: /remember <text>\033[0m\n")
            return None
        entry = memory.add(arg)
        print(f"\n  \033[1;32m✓ Saved memory #{entry['id']}:\033[0m {entry['content']}\n")
        return None

    if command in ("/memories", "/mem") and memory is not None:
        memories = memory.get_all()
        if not memories:
            print("\n  \033[2mNo saved memories yet.\033[0m\n")
            return None
        print(f"\n  \033[1;36mSaved memories ({len(memories)}):\033[0m")
        for item in memories:
            print(f"    \033[1m#{item['id']}\033[0m  {item['content']}  \033[2m({item['created_at']})\033[0m")
        print()
        return None

    if command == "/fact":
        from .memory import append_fact, facts_path
        if not arg:
            print(f"\n  \033[2mUsage: /fact <text> — appends to {facts_path()}\033[0m\n")
            return None
        if append_fact(arg):
            print(f"\n  \033[1;32m✓ Fact saved\033[0m \033[2m(always loaded; edit {facts_path()})\033[0m\n")
        else:
            print("\n  \033[31mNothing to save\033[0m\n")
        return None

    if command == "/facts":
        from .memory import facts_path, load_facts
        facts = load_facts()
        if not facts:
            print(f"\n  \033[2mNo facts yet. Add with /fact <text> or edit {facts_path()}\033[0m\n")
            return None
        print()
        for line in facts.splitlines():
            print(f"  {line}")
        print()
        return None

    if command == "/skills":
        from .skills import load_skills, skills_dir
        skills = load_skills()
        if not skills:
            print(f"\n  \033[2mNo skills yet. Ask conch to 'turn what we just did "
                  f"into a skill', or drop .md files in {skills_dir()}\033[0m\n")
            return None
        print(f"\n  \033[1;36mSkills ({len(skills)}):\033[0m")
        for name, skill in sorted(skills.items()):
            scope = "all tools" if skill["tools"] is None else ", ".join(skill["tools"])
            model = f"  \033[2mmodel={skill['model']}\033[0m" if skill["model"] else ""
            print(f"    \033[1m{name:<20}\033[0m {skill['description'] or ''}"
                  f"  \033[2m[{scope}]\033[0m{model}")
        print("\n  \033[2mUse: /skill <name> [task], or delegate with "
              "delegate_task(skill=...)\033[0m\n")
        return None

    if command == "/skill":
        from .skills import get_skill, render_skill
        if not arg:
            print("\n  \033[2mUsage: /skill <name> [task for this skill]\033[0m\n")
            return None
        parts_ = arg.split(None, 1)
        skill = get_skill(parts_[0])
        if skill is None:
            print(f"\n  \033[31mUnknown skill '{parts_[0]}'. /skills to list.\033[0m\n")
            return None
        prompt = render_skill(skill) + "\n\nFollow this skill's procedure."
        if len(parts_) > 1:
            prompt += f"\n\nTask: {parts_[1]}"
        return ("user_prompt", prompt)

    if command == "/forget" and memory is not None:
        try:
            entry_id = int(arg.lstrip("#"))
        except ValueError:
            print(f"\n  \033[31mInvalid ID: {arg}\033[0m\n")
            return None
        if memory.forget(entry_id):
            print(f"\n  \033[1;32m✓ Forgot memory #{entry_id}\033[0m\n")
        else:
            print(f"\n  \033[31mNo memory with ID #{entry_id}\033[0m\n")
        return None

    if command in ("/models", "/ls"):
        print()
        for provider_name, models in KNOWN_MODELS.items():
            if provider_name == "ollama":
                models = list_ollama_models(config)
            elif provider_name == "custom":
                custom_model = (config.get("custom_model") or "").strip()
                models = [custom_model] if custom_model else []
            marker = " \033[1;33m← active\033[0m" if provider_name == provider else ""
            print(f"  \033[1;36m{provider_name}\033[0m{marker}")
            if provider_name == "ollama":
                if models is None:
                    print(f"    \033[2m(unreachable at {get_ollama_base_url(config)})\033[0m")
                    continue
                if not models:
                    print("    \033[2m(no tool-capable models installed)\033[0m")
                    continue
            if provider_name == "custom" and not models:
                print("    \033[2m(not configured — set custom_base_url + custom_model)\033[0m")
                continue
            for model in models:
                current = model == model_name or (
                    provider_name == "ollama" and provider == "ollama"
                    and ollama_model_matches(model_name, [model])
                )
                prefix = "\033[1;32m●\033[0m" if current else "\033[2m○\033[0m"
                suffix = "  \033[2m(current)\033[0m" if current else ""
                print(f"    {prefix} {model}{suffix}")
        print()
        return None

    if command == "/model":
        if not arg:
            print(f"\n  \033[2mCurrent model:\033[0m \033[1m{model_name}\033[0m ({provider})\n")
            return None
        new_model = arg
        new_provider = None
        for provider_name, models in KNOWN_MODELS.items():
            if provider_name != "ollama" and new_model in models:
                new_provider = provider_name
                break
        if new_provider is None:
            ollama_models = list_ollama_models(config)
            if ollama_models and ollama_model_matches(new_model, ollama_models):
                new_provider = "ollama"
        if new_provider is None:
            # Not in any catalog — assume the current provider, but for Ollama
            # the model must actually exist on the server and support tools.
            new_provider = provider
        if new_provider == "ollama":
            ok, reason = validate_ollama_model(new_model, config)
            if ok is None:
                print(f"\n  \033[31m{reason} — cannot verify model '{new_model}'\033[0m\n")
                return None
            if not ok:
                print(f"\n  \033[31mCannot switch: {reason}\033[0m")
                available = list_ollama_models(config) or []
                if available:
                    print(f"  \033[2mAvailable: {', '.join(available)}\033[0m\n")
                else:
                    print("  \033[2mNo tool-capable models installed on the server.\033[0m\n")
                return None
        new_fn = RAW_FNS.get(new_provider)
        if not new_fn:
            print(f"\n  \033[31mUnknown provider for model '{new_model}'\033[0m\n")
            return None
        key_env = DEFAULT_API_KEY_ENVS.get(new_provider, "")
        if key_env and not os.environ.get(key_env, "").strip():
            print(f"\n  \033[31m{key_env} not set — cannot switch to {new_provider}\033[0m\n")
            return None
        config["provider"] = new_provider
        config["api_key_env"] = key_env
        config["chat_model"] = new_model
        config["model"] = new_model
        print(f"\n  \033[1;32mSwitched to {new_provider}/{new_model}\033[0m\n")
        return (new_provider, new_model, new_fn)

    if command == "/provider":
        if not arg:
            print(f"\n  \033[2mCurrent provider:\033[0m \033[1m{provider}\033[0m\n")
            return None
        new_provider = arg.lower()
        if new_provider not in RAW_FNS:
            print(f"\n  \033[31mUnknown provider '{new_provider}'\033[0m\n")
            return None
        key_env = DEFAULT_API_KEY_ENVS.get(new_provider, "")
        if key_env and not os.environ.get(key_env, "").strip():
            print(f"\n  \033[31m{key_env} not set — cannot switch to {new_provider}\033[0m\n")
            return None
        new_model = get_fallback_model(new_provider, config)
        if new_provider == "ollama" and not new_model:
            if list_ollama_models(config) is None:
                print(f"\n  \033[31mOllama server unreachable at {get_ollama_base_url(config)} — cannot switch\033[0m\n")
            else:
                print("\n  \033[31mNo tool-capable models installed on the Ollama server — cannot switch\033[0m\n")
            return None
        if new_provider == "custom":
            from .providers import get_custom_base_url, probe_custom_provider
            if not new_model or not get_custom_base_url(config):
                print("\n  \033[31mSet custom_base_url and custom_model in "
                      "~/.config/conch/config before switching to custom\033[0m\n")
                return None
            ok, reason = probe_custom_provider(config)
            if not ok:
                print(f"\n  \033[31mCustom endpoint probe failed: {reason}\033[0m\n")
                return None
        config["provider"] = new_provider
        config["api_key_env"] = key_env
        config["chat_model"] = new_model
        config["model"] = new_model
        print(f"\n  \033[1;32mSwitched to {new_provider}/{new_model}\033[0m\n")
        return (new_provider, new_model, RAW_FNS[new_provider])

    if command == "/tools" and all_tools is not None and tool_map is not None:
        prefs = load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))
        groups = group_tools(all_tools, tool_map)
        print(f"\n  \033[1;36mTool groups:\033[0m")
        for grp in sorted(groups):
            status = "\033[31m OFF\033[0m" if grp in disabled else "\033[32m ON \033[0m"
            print(f"    {status}  \033[1m{grp:<20}\033[0m \033[2m{len(groups[grp])} tools\033[0m")
        print()
        return None

    if command == "/enable" and all_tools is not None:
        prefs = load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))
        target = arg.lower()
        if target == "all":
            disabled.clear()
        else:
            disabled.discard(target)
        prefs["disabled_groups"] = sorted(disabled)
        save_tool_prefs(prefs)
        print(f"\n  \033[1;32m✓ Enabled {target or 'all'}\033[0m\n")
        return "reload_tools"

    if command == "/disable" and all_tools is not None and tool_map is not None:
        prefs = load_tool_prefs()
        disabled = set(prefs.get("disabled_groups", []))
        groups = group_tools(all_tools, tool_map)
        target = arg.lower()
        if target == "all":
            disabled = set(groups) - {"local_shell", "manage_tools"}
        elif target in groups:
            disabled.add(target)
        else:
            print(f"\n  \033[31mUnknown group '{target}'. Use /tools to see groups.\033[0m\n")
            return None
        prefs["disabled_groups"] = sorted(disabled)
        save_tool_prefs(prefs)
        print(f"\n  \033[1;32m✓ Disabled {target}\033[0m\n")
        return "reload_tools"

    if command == "/rounds":
        if not arg:
            print("\n  \033[2mMax tool rounds: currently set via /rounds <n>\033[0m\n")
            return None
        try:
            n = int(arg)
            if n < 1:
                raise ValueError
        except ValueError:
            print(f"\n  \033[31mInvalid number: {arg}\033[0m\n")
            return None
        print(f"\n  \033[1;32m✓ Max tool rounds set to {n}\033[0m\n")
        return n

    if command == "/queue":
        if arg in ("on", "true", "1"):
            print("\n  \033[1;32m✓ Typeahead enabled\033[0m")
            print("  \033[2mType while the LLM is working — input runs next.\033[0m\n")
            return "queue_on"
        if arg in ("off", "false", "0"):
            print("\n  \033[1;32m✓ Typeahead disabled\033[0m\n")
            return "queue_off"
        print("\n  \033[2mUsage: /queue on | /queue off  (on by default)\033[0m\n")
        return None

    if command == "/status":
        from .config import get_config_path
        from .providers import get_context_window
        from .runtime import estimate_tokens

        from . import __version__
        window = get_context_window(provider, model_name, config)
        print(f"\n  \033[1;36mConch status:\033[0m")
        print(f"    Version:        {__version__}")
        print(f"    Provider:       {provider}")
        print(f"    Model:          {model_name}")
        print(f"    Context window: {window:,} tokens")
        if messages is not None:
            used = estimate_tokens(messages)
            pct = (used / window * 100) if window else 0
            bar_color = "\033[31m" if pct >= 80 else "\033[33m" if pct >= 60 else "\033[32m"
            print(f"    Context used:   ~{used:,} tokens ({bar_color}{pct:.0f}%\033[0m of window, estimated)")
        if session_usage:
            total_in = session_usage.get("input_tokens", 0)
            total_out = session_usage.get("output_tokens", 0)
            turns = session_usage.get("turns", 0)
            print(f"    Session:        {turns} turns, {total_in:,} in / {total_out:,} out tokens")
        agent_status = "on" if get_agent_mode() else "off"
        print(f"    Agent mode:     {agent_status}")
        config_path = get_config_path()
        exists = "" if os.path.isfile(config_path) else "  \033[2m(not created yet — using defaults)\033[0m"
        print(f"    Config file:    {config_path}{exists}")
        if provider == "ollama":
            print(f"    Ollama server:  {get_ollama_base_url(config)}")
        _skip_keys = {"provider", "model", "chat_model"}
        _hide = ("token", "key", "password", "secret", "credential")

        def _is_secret(k: str) -> bool:
            # api_key_env holds an env var *name*, not a secret value
            return not k.endswith("_env") and any(h in k.lower() for h in _hide)

        extras = [
            f"{k}={v}" for k, v in sorted(config.items())
            if k not in _skip_keys and not _is_secret(k)
        ]
        if extras:
            print(f"    Settings:       \033[2m{', '.join(extras)}\033[0m")
        print()
        return None

    if command == "/cost":
        if session_usage is None:
            session_usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0, "turns": 0}
        total_in = session_usage.get("input_tokens", 0)
        total_out = session_usage.get("output_tokens", 0)
        total_cost = session_usage.get("cost", 0.0)
        turns = session_usage.get("turns", 0)
        print(f"\n  \033[1;36mSession usage:\033[0m")
        print(f"    Turns:         {turns}")
        print(f"    Input tokens:  {total_in:,}")
        print(f"    Output tokens: {total_out:,}")
        if total_cost > 0.0001:
            print(f"    Est. cost:     ${total_cost:.4f}")
        else:
            print(f"    Est. cost:     free")
        print()
        return None

    if command == "/apps":
        if not composio_mod.is_available():
            print("\n  \033[31mCOMPOSIO_API_KEY not set.\033[0m\n")
            return None
        apps = composio_mod.list_apps()
        print(f"\n  \033[1;36mConnectable services ({len(apps)}):\033[0m")
        for slug, desc in apps:
            print(f"    \033[1m{slug:<20}\033[0m \033[2m{desc}\033[0m")
        print()
        return None

    if command in ("/profile", "/profiles") and all_tools is not None and tool_map is not None:
        profiles = list_profiles(config)
        if not arg:
            current = active_profile_name()
            print("\n  \033[1;36mTool profiles:\033[0m")
            for name, info in sorted(profiles.items()):
                marker = " \033[1;33m\u2190 active\033[0m" if name == current else ""
                desc = info.get("description", "")
                print(f"    \033[1m{name:<12}\033[0m \033[2m{desc}\033[0m{marker}")
            print("\n  \033[2mUsage: /profile <name>\033[0m\n")
            return None
        new_tools, desc = activate_profile(arg.lower(), all_tools, tool_map, config)
        if not new_tools and desc.startswith("Unknown"):
            print(f"\n  \033[31m{desc}\033[0m\n")
            return None
        print(f"\n  \033[1;32m\u2713 Profile \'{arg.lower()}\' activated\033[0m \u2014 {desc}")
        print(f"  \033[2m{len(new_tools)} tools active\033[0m\n")
        return "reload_tools"

    if command == "/reload":
        return "reload_tools"

    if command == "/connect":
        if not composio_mod.is_available():
            print("\n  \033[31mCOMPOSIO_API_KEY not set.\033[0m\n")
            return None
        if not arg:
            print("\n  \033[2mUsage: /connect <app>\033[0m\n")
            return None
        success, message = composio_mod.connect(arg.lower().replace(" ", "_"))
        color = "\033[1;32m" if success else "\033[31m"
        prefix = "✓" if success else "✗"
        print(f"\n  {color}{prefix} {message}\033[0m\n")
        return None

    # User-defined slash commands (builtins above always take precedence)
    user_commands = load_user_commands()
    custom_name = command.lstrip("/")
    if custom_name in user_commands:
        return ("user_prompt", render_user_command(user_commands[custom_name], arg))

    return None

