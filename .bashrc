# Anthropic API Key — set this in your environment
# export ANTHROPIC_API_KEY=<your-key-here>
# Or load from a local secrets file (not committed):
[ -f ~/.anthropic_key ] && source ~/.anthropic_key

# Conch config — use Anthropic Claude Opus 4.8
export CONCH_PROVIDER=anthropic
export CONCH_MODEL=claude-opus-4-8
