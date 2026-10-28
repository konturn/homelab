# paperless-ai (AI tagging for paperless-ngx, local LLM only)

`paperless-ai` ([clusterzx/paperless-ai](https://github.com/clusterzx/paperless-ai),
pinned 3.0.9) suggests and applies titles, correspondents, document types,
tags and dates to paperless-ngx documents. It uses the local `ollama`
service only (docs/ollama.md).

UI: https://paperless-ai.lab.nkontur.com (lab_nginx, 10.0.0.0/8 only).

## Privacy: documents never leave the host

- **Network.** paperless-ai is on two `internal: true` bridges only:
  `paperless-ai` (with paperless-ngx and lab_nginx) and `llm-backend` (with
  ollama). It has no LAN address and no route out, so it cannot reach a cloud
  provider even if misconfigured. Ollama also has no egress.
- **Config.** `docker/paperless-ai/paperless-ai.env` is rendered by ansible
  and mounted read-only at `/app/data/.env`, so the UI's settings page cannot
  change the provider. The template task validates it with
  `homelab-config-check paperless-ai-env`: `AI_PROVIDER=ollama`,
  `OLLAMA_API_URL=http://ollama:11434`, and no OpenAI/Azure/custom key or
  URL. Otherwise the deploy fails before the file is written.
- **No RAG service.** The image's default command also starts a Python RAG
  service, which downloads Hugging Face models (with chromadb telemetry on)
  and indexes every document. Compose runs only the Node app
  (`pm2-runtime ecosystem.config.js`, `RAG_SERVICE_ENABLED=false`).
- **`/setup` is blocked in nginx.** Upstream serves `/setup` without
  authentication, even after setup is done.

## Paperless access (least privilege)

The Paperless user `paperless-ai` (not staff, not superuser, unusable
password) is in the group `paperless-ai` with: view/change document,
view/add tag, view/add correspondent, view/add document type, view custom
field, view user (paperless-ai looks up its own user ID). It cannot delete
documents or touch mail, workflows, users or settings. Its API token is in
Vault.

## Secrets (Vault `homelab/docker/paperless-ai`)

| field | used by |
|---|---|
| `paperless_api_token` | `.env` `PAPERLESS_API_TOKEN` (required in fetch-vault-secrets) |
| `api_key` | `.env` `API_KEY` (paperless-ai REST API, `x-api-key`) (required) |
| `jwt_secret` | `.env` `JWT_SECRET` (UI sessions; upstream default is a fixed string) (required) |
| `ui_password` | UI login `admin`, set once by hand (below); not read by the deploy |

### UI login (once, or after the data dir is lost)

paperless-ai keeps its single UI user in `/app/data/documents.db`; it is
normally created by the `/setup` wizard, which is blocked. Create it from the
Vault password:

```sh
PW=$(vault kv get -mount=homelab -field=ui_password docker/paperless-ai)
docker exec -e PW="$PW" paperless-ai node -e '
  const b=require("bcryptjs"), m=require("./models/document");
  b.hash(process.env.PW, 12).then(h => m.addUser("admin", h)).then(r => { console.log(r); process.exit(0); });'
```

## Review mode (current)

- Only documents tagged **`paperless-ai-test`** are processed
  (`PROCESS_PREDEFINED_DOCUMENTS=yes`, `TAGS=paperless-ai-test`). Every
  processed document also gets **`ai-processed`**.
- A scan runs at container start and every 30 minutes. Documents without the
  tag, including new ones, are never touched.
- paperless-ai records what it changed (title, tags, correspondent) in its
  own history. The UI's History page can reset a document.
- To process one more document, add the `paperless-ai-test` tag to it in
  Paperless.

### Enabling automatic processing of new documents (needs Noah's OK)

1. In Paperless, add a Workflow: trigger **Document Added**, action
   **assign tag `paperless-ai-queue`** (create the tag first).
2. In `docker/paperless-ai/paperless-ai.env` set
   `TAGS=paperless-ai-test,paperless-ai-queue`. Optionally set
   `SCAN_INTERVAL=*/10 * * * *`. Merge; the deploy restarts the container.
3. New documents are now tagged on consume and processed on the next scan.
   Existing documents stay untouched unless tagged.

Do **not** set `PROCESS_PREDEFINED_DOCUMENTS=no`. That processes every
existing document as well (all 159+ at once), not only new ones.

To turn processing off entirely, set `DISABLE_AUTOMATIC_PROCESSING=yes`.

## Review batch (2026-10-21)

14 documents tagged `paperless-ai-test` (2 per document type and 3 untyped);
1 has no OCR text and was skipped. Model `paperless-qwen3:14b`.

| | first run (empty SYSTEM_PROMPT) | with the SYSTEM_PROMPT in the .env |
|---|---|---|
| titles useful | ~6/13, with placeholders (`xxxxx`, `a`) | 10/13 (3 left unchanged) |
| correspondent set | 1/13 (a placeholder) | 9/13, ~7 right (one W-2 got the IRS, not the employer) |
| tags | none | 10/13, 1-3 sensible tags each |
| document type | 4 changed, mostly wrong | kept on 9; 1 plausible fix (contract to receipt), 3 questionable (`Certificate` on a check and on property papers) |
| date | 2 placeholder dates (1990-01-01) | no placeholders; 4 receipts moved from scan date to the printed date |

paperless-ai gets nothing useful without a system prompt: its built-in JSON
skeleton contains example values (`xxxxx`), and the model copies them. Keep
`SYSTEM_PROMPT` set.

Known limits:
- paperless-ai sizes `num_ctx` per document, and Ollama reloads the model
  whenever `num_ctx` changes. A document therefore takes 15-65 s, mostly
  model load, not ~5 s.
- Document types: the 6 existing types are coarse. Restricting to existing
  types (`RESTRICT_TO_EXISTING_DOCUMENT_TYPES=yes`) or adding types such as
  `Bill`, `Tax form`, `Property` would help before automatic processing is
  enabled.

Revert of the batch: the original title, type, correspondent, date and tags
of every batch document were saved before the run (on the router, root
only). The paperless-ai UI History page can also reset each document.

## Model

`OLLAMA_MODEL=paperless-qwen3:14b`: qwen3:14b with a template that disables
thinking (`docker/ollama/paperless-qwen3.Modelfile`). See docs/ollama.md for
why, and the benchmarks.

The model and its base `qwen3:14b-q4_K_M` are declared in the inventory
(`ollama_models`, `ollama_custom_models`) and applied on deploy by the
`configure-ollama-models` role: a new host pulls the base and creates
`paperless-qwen3:14b`, and a changed Modelfile recreates it. No manual
`ollama pull` or `ollama create` is needed (docs/ollama.md, "Declared in the
inventory").

`TOKEN_LIMIT=8192` caps `num_ctx` (paperless-ai sizes it to the prompt) and
`CONTENT_MAX_LENGTH=16000` truncates long documents, so VRAM stays around
10-11 GB and leaves room for Plex.

## Checking it

```sh
docker logs paperless-ai 2>&1 | grep -E 'Ollama|ollama|Document [0-9]+'
docker logs ollama 2>&1 | grep '/api/generate'      # one line per document
```
