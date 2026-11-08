# llm-shadow

Replays every real `imessage-check` OpenClaw run through a local Ollama model,
with tools that only log side effects, and records how its decisions compare.
See [docs/llm-shadow.md](../../../docs/llm-shadow.md).

- `files/llm_shadow.py`: the harness (`tick`, `summarize`, `status`)
- `llm-shadow.timer`: every 2 minutes; `llm_shadow_enabled: false` stops it
- records: `{{ llm_shadow_data_dir }}` (root, 0700)
