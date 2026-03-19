# Conch

Conch is an LLM-assisted shell with two interfaces:

- `conch-ask` for one-shot command generation
- `conch` / `conch-chat` for multi-turn chat with MCP tools, memory, and scheduling

By default, Conch now uses Cerebras inference with `zai-glm-4.7`.

## Install

```bash
./install.sh
```

The installer now looks for `CEREBRAS_API_KEY` and configures Conch to use:

- `provider = cerebras`
- `model = zai-glm-4.7`
- `chat_model = zai-glm-4.7`

You can still switch providers later in chat with `/provider openai`, `/provider anthropic`, or `/provider ollama`.

## Development

```bash
python3 -m unittest discover -s tests
```
