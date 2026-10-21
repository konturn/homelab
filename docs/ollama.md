# Ollama (local LLM on the Quadro P5000)

`ollama` in `docker/docker-compose.yml` serves local models on the router's
Quadro P5000 (16 GB, Pascal). The GPU is shared with Plex hardware
transcoding (docs/plex-gpu.md).

## Version pin

The image is pinned to `ollama/ollama:0.40.6` and Renovate holds it
(`renovate.json`, dashboard approval).

- The P5000 has compute capability 6.1. CUDA 13 dropped Maxwell, Pascal and
  Volta (< 7.5).
- The image ships two CUDA runners. `lib/ollama/cuda_v13` is built for
  compute 7.5 and up and skips the card:
  `skipping CUDA device — compute capability not in compiled architectures
  device="Quadro P5000" cc=610`. `lib/ollama/cuda_v12` (CUDA 12.8) still
  includes sm_61 and drives it:
  `inference compute ... library=CUDA compute=6.1 ... libdirs=ollama,cuda_v12`.
- Upstream supports compute 5.0-6.2 only with driver >= 570 (the router has
  580). The day an image drops `cuda_v12`, Ollama falls back to the CPU
  without failing. So an update needs a manual check, below.

### Testing an update

```sh
docker pull ollama/ollama:<new>
docker run --rm --entrypoint ls ollama/ollama:<new> /usr/lib/ollama   # cuda_v12 must exist
docker run -d --name ollama-test --gpus all -e OLLAMA_DEBUG=1 \
  -v /mpool/ollama:/root/.ollama ollama/ollama:<new>
docker logs ollama-test 2>&1 | grep -E 'inference compute|skipping CUDA'
#   want: library=CUDA compute=6.1
docker exec ollama-test ollama run qwen3.5:9b-q4_K_M "hi" >/dev/null
docker exec ollama-test ollama ps                                       # want: 100% GPU
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
docker rm -f ollama-test
```

Then bump the tag and digest in compose (Renovate MR or by hand).

## Network

Ollama has no authentication. It is only on `llm-backend`, an internal bridge
(172.30.3.0/24, no egress, not on any VLAN, not behind nginx). A container
that needs it joins `llm-backend` and uses `http://ollama:11434`. Today that
is paperless-ai. To give Home Assistant or OpenClaw access later, attach
them to `llm-backend` as well. Do not publish the port.

## Models

Stored in `/mpool/ollama` (outside every restic folder, so not backed up;
they can be downloaded again).

| model | file | VRAM (8k ctx) | generation | prompt (cold) | load from pool |
|---|---|---|---|---|---|
| `qwen3:8b-q4_K_M` | 5.2 GB | 6.2 GB | 33.6 tok/s | ~620 tok/s | ~5 s |
| `qwen3.5:9b-q4_K_M` | 6.6 GB | 6.6 GB | 28.6 tok/s | ~645 tok/s | ~27 s |
| `qwen3:14b-q4_K_M` | 9.3 GB | 10.2 GB | 18.6 tok/s | ~347 tok/s | ~38 s |

Measured 2026-10-21 on the P5000: ~2k-token prompt, num_ctx 8192,
`think: false`, while the pool was resilvering (the cold loads of the first two
were from disk; qwen3:8b was still in page cache). All three run with
`100% GPU`.

**paperless-ai uses `paperless-qwen3:14b`** (qwen3:14b without thinking). For
batch tagging in the background, accuracy matters more than speed. At 10 GB it
still leaves ~6 GB for Plex, and it unloads after 10 idle minutes.

### Thinking models and paperless-ai

qwen3 and qwen3.5 think by default. paperless-ai 3.0.9 sends no `think` flag
and caps answers at `num_predict: 256`, so the model spends all 256 tokens
thinking and returns an empty answer. Ollama has no Modelfile parameter for
`think`. `docker/ollama/paperless-qwen3.Modelfile` therefore gives qwen3:14b a
ChatML template that always opens the answer with an empty
`<think></think>` block, the same thing qwen3's own template does for
`think=false`. qwen3.5 uses a built-in renderer that ignores a custom
template, so it cannot be used this way.

### Sharing the GPU with Plex

Tested 2026-10-21 with qwen3:14b loaded (10.2 GB): a Plex 360p HLS transcode
ran on the P5000 at the same time. `Plex Transcoder` used 135-207 MiB of VRAM,
with NVENC at 9-12% and NVDEC at 18-21%. Total VRAM was about 10.3 of 16.4 GB.
The model stayed at `100% GPU`.

### Pulling a model

The container has no egress, so attach it to the default bridge for the pull:

```sh
docker network connect bridge ollama
docker exec ollama ollama pull <model>
docker network disconnect bridge ollama
```

## Settings

| env | value | why |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | 10m | unload when idle, VRAM back to Plex |
| `OLLAMA_MAX_LOADED_MODELS` | 1 | 16 GB shared with Plex |
| `OLLAMA_NUM_PARALLEL` | 1 | same; requests queue |
| `OLLAMA_CONTEXT_LENGTH` | 8192 | fits documents; KV cache stays small |
| `OLLAMA_NO_CLOUD` | true | local inference only |

## Monitoring

- The healthcheck (`ollama list`) feeds the `container-health-failure` alert,
  like every other container.
- Router telegraf `[[inputs.nvidia_smi]]` writes `nvidia_smi` (GPU, memory,
  encoder and decoder utilization, VRAM, temperature, power) and
  `nvidia_smi_process` (VRAM per process) to InfluxDB.
