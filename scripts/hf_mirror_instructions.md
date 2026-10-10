# HuggingFace Mirror Task — for the agent that did the BGE-reranker mirror

You previously created `woxpas-ai/bge-reranker-v2-m3-onnx` mirroring
`BAAI/bge-reranker-v2-m3` (you applied the ONNX export + quantization
variants under `int8`, `fp16`, `fp32` subpaths). Now mirror three more
models under the same `woxpas-ai` HuggingFace organization, with the
same care: pin a stable revision, include a README explaining the
mirror's purpose + license attribution, verify license compatibility
before uploading.

## Goal

Insulate Woxpas users from upstream model availability risk: HF accounts
get suspended, files get re-uploaded with subtle differences, licenses
change retroactively. Mirroring under a Woxpas-controlled org gives us
permanent redistribution rights for the four default models the
runtime depends on.

## Models to mirror (in priority order)

| target repo                                      | source repo                                       | format         | size  | license     |
|--------------------------------------------------|---------------------------------------------------|----------------|-------|-------------|
| `woxpas-ai/bge-m3-onnx`                          | `aapot/bge-m3-onnx`                               | ONNX (multi-variant) | ~2 GB | MIT (BAAI base) |
| `woxpas-ai/qwen2.5-3b-instruct-q4km-gguf`        | `Qwen/Qwen2.5-3B-Instruct-GGUF` (Q4_K_M variant only) | GGUF       | ~2 GB | Apache-2.0   |
| `woxpas-ai/whisper-small-ct2`                    | `Systran/faster-whisper-small`                    | CTranslate2     | ~464 MB | MIT       |

## Process per model (apply this template four times)

### 1. License compatibility check

Read the source repo's LICENSE file. Confirm:
- Allows redistribution
- Requires attribution (we provide via README)
- No use restrictions that conflict with redistribution under a permissive
  open-source license (Woxpas memory is Apache-2.0)

If the license allows redistribution, proceed. If not, STOP and report.

### 2. Identify the pinned revision

Use `huggingface_hub` to fetch the latest commit hash of `main`:

```python
from huggingface_hub import HfApi
api = HfApi()
info = api.repo_info(repo_id="<source-repo>", repo_type="model")
print(info.sha)   # this is what we pin
```

Record this commit hash. The mirror will pin to it explicitly.

### 3. Download source

```python
from huggingface_hub import snapshot_download
path = snapshot_download(
    repo_id="<source-repo>",
    revision="<sha-from-step-2>",
    local_dir="/tmp/woxpas-mirror-src/<repo-name>",
    local_dir_use_symlinks=False,
)
```

For the Qwen GGUF case, download ONLY the Q4_K_M variant file (and the
config.json + tokenizer.json + chat_template if present) to keep the
mirror small. Full Qwen-3B GGUF repo includes Q4_K_S, Q5_K_M, Q8_0,
Q3_K_S, and other variants we don't ship by default.

### 4. Create the target repo

```python
from huggingface_hub import HfApi
api = HfApi()
api.create_repo(
    repo_id="woxpas-ai/<repo-name>",
    repo_type="model",
    private=False,
    exist_ok=True,
)
```

### 5. Generate the mirror README

Create a `README.md` at the root of the local directory BEFORE upload,
following this template (replacing placeholders):

```markdown
---
license: <upstream-license>           # e.g. mit / apache-2.0
tags:
  - mirror
  - woxpas-ai
language: <as-relevant>
---

# {Mirror name}

This is a verbatim mirror of [{source-repo}](https://huggingface.co/{source-repo})
at commit `{sha}`. Mirrored on {date} for redistribution stability so
[Woxpas](https://github.com/woxpas-ai) users can rely on a stable URL
even if the upstream is renamed, removed, or modified.

## Source attribution

Original work by **{upstream-author}**, licensed under **{license}**.
Full upstream README content preserved below this attribution block.

## What Woxpas uses this for

- {bge-m3}: dense embeddings (1024-dim, L2-normalized) for the chunks +
  summaries collections
- {qwen-3b}: default summarization, extraction, and synthesis LLM in
  the local pipeline (CPU-runnable via llama-cpp-python)
- {whisper-small}: voice-note transcription via faster-whisper

## License

{Full license text or pointer to LICENSE file in this repo}

---

{Original upstream README content appended verbatim}
```

### 6. Upload to mirror

```python
api.upload_folder(
    folder_path="/tmp/woxpas-mirror-src/<repo-name>",
    repo_id="woxpas-ai/<repo-name>",
    commit_message="Mirror of <source-repo>@<sha-prefix> for redistribution stability",
)
```

### 7. Verify

After upload, re-fetch with `snapshot_download(repo_id="woxpas-ai/<name>")`
and confirm file checksums match the source. If anything mismatches,
delete the repo and retry.

### 8. Update the Woxpas runtime catalog

After all four mirrors exist, edit
`packages/runtime/woxpas_runtime/models.py`:

```python
# Before
BGE_M3_ONNX = ModelArtefact(
    name="bge-m3-onnx",
    version="1",
    backend_kind="embedder.local_onnx",
    repo="aapot/bge-m3-onnx",            # ← upstream
    revision="76a603396f5eb9f03ed51bbab8f4893fcea7b2fe",
)

# After
BGE_M3_ONNX = ModelArtefact(
    name="bge-m3-onnx",
    version="2",                          # bump version (re-fetch)
    backend_kind="embedder.local_onnx",
    repo="woxpas-ai/bge-m3-onnx",         # ← mirror
    revision="<sha-of-our-mirror>",
)
```

Bump `version` so users with the old artefact in cache pick up the
mirror on next pre-fetch. Same pattern for the other 2 models.

## Tokens needed

Set `HF_TOKEN` in env before running. Token must have **write access**
to the `woxpas-ai` org (Settings → Access Tokens → "fine-grained" with
write permissions on the org).

## Safety rails

- Do NOT delete or modify the source repos. Read-only operation.
- Do NOT upload anything other than verbatim copies + the README.
- Do NOT obscure attribution. The mirror's purpose is reliability,
  not appropriation. Original authorship credit must be prominent in
  the README's first paragraph.
- If an upstream license requires you to publish modifications under
  the same license, the mirror counts as redistribution, not
  modification — no change required.

## Output expected

After running this task end-to-end, post:

1. The four new repo URLs (one per mirror)
2. The four pinned commit hashes
3. The diff to `packages/runtime/woxpas_runtime/models.py`
4. A note confirming `prefetch-models` succeeds against the new mirror
   in a fresh local cache (delete `~/.cache/huggingface` and run
   `uv run woxpas-cli prefetch-models` to verify)

If any step fails — license issue, upload error, hash mismatch — STOP
and report. Don't push partial state.
