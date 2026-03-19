"""Slash command handling."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .browser import browse_conversations
from . import composio as composio_mod
from .providers import DEFAULT_API_KEY_ENVS, KNOWN_MODELS, RAW_FNS
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
            "  \033[1m/browse\033[0m              Browse conversations\n"
            "  \033[1m/new\033[0m                 Start a new conversation\n"
            "  \033[1m/convos\033[0m              List past conversations\n"
            "  \033[1m/switch <id>\033[0m         Switch conversation\n"
            "  \033[1m/delete <id>\033[0m         Delete conversation\n"
            "  \033[1m/agent\033[0m               Toggle agent mode\n"
            "  \033[1m/schedule <interval> <prompt>\033[0m  Schedule a task\n"
            "  \033[1m/tasks\033[0m               List scheduled tasks\n"
            "  \033[1m/cancel <id>\033[0m         Cancel a scheduled task\n"
            "  \033[1m/tools\033[0m               List tool groups\n"
            "  \033[1m/enable <group>\033[0m      Enable a tool group\n"
            "  \033[1m/disable <group>\033[0m     Disable a tool group\n"
            "  \033[1m/connect <app>\033[0m       Connect a service\n"
            "  \033[1m/apps\033[0m                List connectable services\n"
            "  \033[1m/rounds <n>\033[0m          Set max tool call rounds (default 25)\n"
            "  \033[1m/queue\033[0m               Toggle typeahead (type while LLM works, on by default)\n"
            "  \033[1m/cost\033[0m                Show session token usage and cost\n"
            "  \033[1m/profile [name]\033[0m      Switch tool profile (minimal, dev, comms, full)\n"
            "  \033[1m/profile [name]\033[0m      Switch tool profile (minimal, dev, comms, full)\n"
            "  \033[1m/reload\033[0m              Reload MCP tools\n"
        )
        return None

    if command == "/agent":
        if arg in ("on", "true", "1"):
            set_agent_mode(True)
        elif arg in ("off", "false", "0"):
            set_agent_mode(False)
        else:
            set_agent_mode(not get_agent_mode())
        status = "\033[1;32mON\033[0m" if get_agent_mode() else "\033[31mOFF\033[0m"
        print(f"\n  Agent mode: {status}")
        if get_agent_mode():
            print("  \033[2mLocal commands will auto-execute without confirmation.\033[0m")
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
            marker = " \033[1;33m← active\033[0m" if provider_name == provider else ""
            print(f"  \033[1;36m{provider_name}\033[0m{marker}")
            for model in models:
                prefix = "\033[1;32m●\033[0m" if model == model_name else "\033[2m○\033[0m"
                suffix = "  \033[2m(current)\033[0m" if model == model_name else ""
                print(f"    {prefix} {model}{suffix}")
        print()
        return None

    if command == "/model":
        if not arg:
            print(f"\n  \033[2mCurrent model:\033[0m \033[1m{model_name}\033[0m ({provider})\n")
            return None
        new_model = arg
        new_provider = provider
        for provider_name, models in KNOWN_MODELS.items():
            if new_model in models:
                new_provider = provider_name
                break
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
        new_model = KNOWN_MODELS[new_provider][0]
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
        profiles = list_profiles()
        if not arg:
            current = active_profile_name()
            print("\n  \033[1;36mTool profiles:\033[0m")
            for name, info in sorted(profiles.items()):
                marker = " \033[1;33m\u2190 active\033[0m" if name == current else ""
                desc = info.get("description", "")
                print(f"    \033[1m{name:<12}\033[0m \033[2m{desc}\033[0m{marker}")
            print("\n  \033[2mUsage: /profile <name>\033[0m\n")
            return None
        new_tools, desc = activate_profile(arg.lower(), all_tools, tool_map)
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

    return None

