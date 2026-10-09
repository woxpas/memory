"""Bootstrap CLI.

Run from a host that can reach Postgres (typically inside the worker container
or via `docker compose exec`):

  woxpas-cli init-tenant --display-name "Ada"
  woxpas-cli create-source --tenant-id <uuid> --type file_upload --name default
  woxpas-cli create-scope  --tenant-id <uuid> --name workout

`init-tenant` generates a fresh DEK, wraps it under an Argon2id-derived KEK
(passphrase from WOXPAS_INIT_PASSPHRASE or stdin), persists the encrypted
envelope, and prints the DEK as hex so the dev worker can pick it up via
WOXPAS_DEV_DEK_HEX. The plaintext DEK is *only* printed in --dev mode; in
prod the boot service receives it via Unix socket and we never log it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
from datetime import UTC
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import structlog
from woxpas_storage import (
    PostgresPool,
    ScopeRepo,
    SourceRepo,
    StorageSettings,
    init_tenant_secrets,
)

log = structlog.get_logger(__name__)


# ─── helpers ───────────────────────────────────────────────────────────────

def _read_passphrase(prompt: str = "Passphrase: ", confirm: bool = True) -> str:
    p = os.environ.get("WOXPAS_INIT_PASSPHRASE")
    if p:
        return p
    p = getpass.getpass(prompt)
    if not p:
        raise SystemExit("passphrase required")
    if confirm:
        c = getpass.getpass("Confirm: ")
        if p != c:
            raise SystemExit("passphrases do not match")
    return p


def _print_kv(d: dict[str, Any]) -> None:
    for k, v in d.items():
        print(f"  {k:>22}  {v}")


def _serialize_val(val: Any) -> Any:
    """Convert any value to a JSON-serializable form."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, (bytes, memoryview)):
        try:
            return val.hex()
        except Exception:
            return str(val)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, (dict, list)):
        return val
    return str(val)


# ─── .env helpers ────────────────────────────────────────────────────────────

def _read_env(path: str) -> dict[str, str]:
    """Read a .env file into a dict, preserving only KEY=VALUE lines.
    Trailing inline comments are stripped so 'KEY=# comment' → ('KEY', '')."""
    result: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.strip()
                # Strip inline comments. A '#' preceded by whitespace or at
                # position 0 (e.g. 'KEY=  # comment') is a comment, not a value.
                # But '#' embedded in a hex value (e.g. 'KEY=abc#def') stays.
                if v.startswith("#"):
                    v = ""
                else:
                    idx = v.find(" #")
                    if idx >= 0:
                        v = v[:idx].rstrip()
                result[k.strip()] = v
    except FileNotFoundError:
        pass
    return result


def _write_env(path: str, updates: dict[str, str]) -> None:
    """Merge *updates* into a .env file. Existing keys are replaced in-place;
    new keys are appended. Comments and blank lines are preserved."""
    updated_keys = set(updates.keys())
    lines: list[str] = []
    seen: set[str] = set()
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k in updated_keys:
                        lines.append(f"{k}={updates[k]}\n")
                        seen.add(k)
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")
    except FileNotFoundError:
        pass

    # Append any new keys not already written.
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}\n")

    # Ensure parent directory exists.
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.writelines(lines)
    os.chmod(path, 0o600)


def _prompt_cloud_key(env: dict[str, str], env_key: str, label: str) -> None:
    """Prompt for a single cloud API key. Reads current value from *env* dict
    so re-running bootstrap shows what's already set."""
    current = env.get(env_key, "")
    masked = ""
    if current:
        masked = current[:8] + "..." if len(current) > 11 else "****"
        prompt = f"  {label} [{masked}]: "
    else:
        prompt = f"  {label} [none]: "
    try:
        value = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        value = ""
    if value:
        env[env_key] = value
    elif current:
        env[env_key] = current  # keep existing


def _available_cloud_models(env: dict[str, str]) -> list[tuple[str, str]]:
    """Return (model_name, description) for cloud models whose API keys are set."""
    models: list[tuple[str, str]] = []
    if env.get("WOXPAS_KEY_ANTHROPIC"):
        models.append(("claude-haiku-4-5",    "Anthropic · fast + cheap, good for summarization"))
        models.append(("claude-sonnet-4-6",   "Anthropic · powerful, good for extraction"))
    if env.get("WOXPAS_KEY_OPENAI"):
        models.append(("openai-o3-mini",      "OpenAI · reasoning model, good for extraction"))
    if env.get("WOXPAS_KEY_DEEPSEEK"):
        models.append(("deepseek-chat",       "DeepSeek · general-purpose, good all-rounder"))
    if env.get("WOXPAS_KEY_KIMI"):
        models.append(("kimi-k2",             "Kimi / Moonshot · general-purpose"))
    return models


# ─── bootstrap ────────────────────────────────────────────────────────────────

_BANNER = """
  ██╗    ██╗ ██████╗ ██╗  ██╗██████╗  █████╗ ███████╗
  ██║    ██║██╔═══██╗╚██╗██╔╝██╔══██╗██╔══██╗██╔════╝
  ██║ █╗ ██║██║   ██║ ╚███╔╝ ██████╔╝███████║███████╗
  ██║███╗██║██║   ██║ ██╔██╗ ██╔═══╝ ██╔══██║╚════██║
  ╚███╔███╔╝╚██████╔╝██╔╝ ██╗██║     ██║  ██║███████║
   ╚══╝╚══╝  ╚═════╝ ╚═╝  ╚═╝╚═╝     ╚═╝  ╚═╝╚══════╝

        sovereign personal memory · v0.1.0
"""

_ENV_PATH = ".env"


async def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Single-command bootstrap from a fresh clone to a running stack.

    Walks through: prerequisite checks → cloud keys → infrastructure →
    workspace → tenant → models → ready. Idempotent — re-running skips
    already-completed steps.
    """
    import shutil
    from pathlib import Path

    print(_BANNER)

    # ── 1. Prerequisites ──────────────────────────────────────────────────
    print("── Prerequisites ──────────────────────────────────────────")
    ok = True

    # Python
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"  {'✓' if sys.version_info >= (3, 12) else '✗'} Python {py_ver}")

    # uv
    uv_path = shutil.which("uv")
    if uv_path:
        print(f"  ✓ uv ({uv_path})")
    else:
        print("  ✗ uv not found — install from https://github.com/astral-sh/uv")
        ok = False

    # Docker
    docker_path = shutil.which("docker")
    if docker_path:
        import subprocess as _sp
        try:
            result = _sp.run([docker_path, "info"], capture_output=True, timeout=10)
            if result.returncode == 0:
                # Grab version from the first line ("Server Version: ...").
                for line in result.stdout.decode().splitlines():
                    if "Server Version:" in line:
                        docker_ver = line.split(":", 1)[1].strip()
                        print(f"  ✓ Docker {docker_ver}")
                        break
                else:
                    print("  ✓ Docker (version unknown)")
            else:
                print("  ✗ Docker daemon not reachable — is Docker Desktop running?")
                ok = False
        except Exception:
            print("  ✗ Docker not reachable")
            ok = False
    else:
        print("  ✗ Docker not found — install Docker Desktop or Docker Engine")
        ok = False

    # Docker Compose (may be a plugin or standalone).
    compose_ok = False
    try:
        result = _sp.run(
            [docker_path or "docker", "compose", "version"], capture_output=True, timeout=10,
        )
        compose_ok = result.returncode == 0
    except Exception:
        pass
    if not compose_ok:
        try:
            result = _sp.run(
                ["docker-compose", "--version"], capture_output=True, timeout=10,
            )
            compose_ok = result.returncode == 0
        except Exception:
            pass
    print(f"  {'✓' if compose_ok else '✗'} Docker Compose")

    # Disk space estimate (models need ~5 GB free).
    try:
        cwd = Path.cwd()
        usage = shutil.disk_usage(cwd)
        free_gb = usage.free / (1024 ** 3)
        marker = "✓" if free_gb >= 10 else ("⚠" if free_gb >= 6 else "✗")
        print(f"  {marker} {free_gb:.0f} GB free disk space (6 GB recommended)")
    except Exception:
        pass

    if not ok:
        print("\n[woxpas] fix the issues above and re-run `woxpas-cli bootstrap`.")
        return 1

    # ── 2. Cloud API keys ──────────────────────────────────────────────────
    print("\n── Cloud API keys (optional — leave blank to run fully local) ──")

    env_path = Path(_ENV_PATH)
    existing_env = _read_env(str(env_path))

    _prompt_cloud_key(existing_env, "WOXPAS_KEY_ANTHROPIC", "Anthropic API key")
    _prompt_cloud_key(existing_env, "WOXPAS_KEY_OPENAI",   "OpenAI API key")
    _prompt_cloud_key(existing_env, "WOXPAS_KEY_DEEPSEEK", "DeepSeek API key")
    _prompt_cloud_key(existing_env, "WOXPAS_KEY_KIMI",     "Kimi API key")

    # Summarise what's set.
    all_keys = [
        ("WOXPAS_KEY_ANTHROPIC", "Anthropic"),
        ("WOXPAS_KEY_OPENAI",    "OpenAI"),
        ("WOXPAS_KEY_DEEPSEEK",  "DeepSeek"),
        ("WOXPAS_KEY_KIMI",      "Kimi"),
    ]
    set_providers = [label for k, label in all_keys if existing_env.get(k)]
    unset_providers = [label for k, label in all_keys if not existing_env.get(k)]
    if set_providers:
        print(f"\n  Keys set: {', '.join(set_providers)}")
    if unset_providers:
        print(f"  Keys unset: {', '.join(unset_providers)}")

    cloud_count = len(set_providers)
    if cloud_count == 0:
        print("  → Running fully local — sovereign by default.")
    else:
        print("  → Local models are the sovereign default regardless of cloud keys.")

    # Track whether the user switched any role to cloud — we'll offer to
    # flip the cloud-policy gate after the tenant step (needs Postgres).
    cloud_models_selected = False

    # ── 2b. Model routing (only when cloud keys are set) ────────────────────
    if cloud_count and not args.yes:
        print("\n── Model routing ──────────────────────────────────────────")
        print("  Default for every role: local (runs on your machine, no egress).")
        print("  With your cloud keys you can route specific roles to cloud models.")
        print("  Embeddings, ASR, and reranking always stay local.")
        print()
        print("  Recommendation:")
        print("    Summarization → keep local. Short texts use an instant")
        print("      shortcut; longer ones are fast enough on Qwen 1.5B.")
        print("    Extraction → cloud. This is the bottleneck — it runs an")
        print("      LLM over every chunk and takes 40s+ on local CPU vs")
        print("      ~2s on DeepSeek. Biggest speed gain per byte egressed.")
        print()

        available = _available_cloud_models(existing_env)
        if available:
            print("  Available cloud models (any catalog name works):")
            for name, label in available:
                print(f"    {name:<38s} {label}")
            print()

        # Summarization
        current_summary = existing_env.get("SUMMARY_MODEL_NAME", "")
        summary_display = current_summary if current_summary else "local (Qwen 1.5B) — recommended"
        print(f"  Summarization [{summary_display}]")
        try:
            choice = input("  → model name or Enter to keep: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice:
            existing_env["SUMMARY_MODEL_NAME"] = choice
            print(f"    ✓ summarization → {choice}")
            cloud_models_selected = True
        elif not current_summary:
            print("    ✓ summarization stays local")

        # Extraction
        current_extraction = existing_env.get("EXTRACTION_MODEL_NAME", "")
        extraction_display = current_extraction if current_extraction else "local (Qwen 1.5B)"
        print(f"\n  Extraction [{extraction_display}]")
        # Suggest the first available cloud model as default.
        suggested = available[0][0] if available else ""
        suggestion = f" (try: {suggested})" if suggested else ""
        try:
            choice = input(f"  → model name or Enter to keep{suggestion}: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = ""
        if choice:
            existing_env["EXTRACTION_MODEL_NAME"] = choice
            print(f"    ✓ extraction → {choice}")
            cloud_models_selected = True
        elif not current_extraction:
            print("    ✓ extraction stays local")
        print()

    # Auto-generate infra secrets + set localhost hosts if not already set.
    import secrets as _secrets
    for key in ("POSTGRES_PASSWORD", "QDRANT_API_KEY", "MINIO_ROOT_PASSWORD"):
        if not existing_env.get(key):
            existing_env[key] = _secrets.token_hex(24)
    # Local dev defaults — override Docker-internal hostnames for host-side access.
    for key, val in [
        ("POSTGRES_HOST", "127.0.0.1"),
        ("QDRANT_HOST", "127.0.0.1"),
        ("MINIO_ENDPOINT", "127.0.0.1:9000"),
        ("CLAMAV_HOST", "127.0.0.1"),
    ]:
        if not existing_env.get(key) or existing_env[key] in ("postgres", "qdrant", "minio:9000", "clamav"):
            existing_env[key] = val
    _write_env(str(env_path), existing_env)

    # Reload .env into os.environ so StorageSettings + Docker Compose see
    # the freshly generated passwords.
    from dotenv import load_dotenv
    load_dotenv(str(env_path), override=True)

    # ── 3. Infrastructure ──────────────────────────────────────────────────
    print("\n── Infrastructure ─────────────────────────────────────────")

    compose_files = ["docker-compose.yml"]
    override = Path("docker-compose.override.yml")
    if override.is_file():
        compose_files.append("docker-compose.override.yml")
    compose_args: list[str] = []
    for cf in compose_files:
        compose_args.extend(["-f", cf])

    _sp.run(
        [docker_path or "docker", "compose", *compose_args, "up", "-d", "postgres", "qdrant", "minio", "clamav"],
        check=True,
    )

    # Wait for healthy. ClamAV needs up to 120s for first signature download.
    services = ["postgres", "qdrant", "minio", "clamav"]
    for svc in services:
        print(f"  waiting for {svc}...", end=" ", flush=True)
        healthy = False
        for _ in range(120):
            result = _sp.run(
                [docker_path or "docker", "compose", *compose_args, "ps", "--format", "json", svc],
                capture_output=True,
            )
            try:
                import json as _json
                status = _json.loads(result.stdout.decode())
                if status.get("Health") == "healthy":
                    healthy = True
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        print("✓" if healthy else "✗ (check docker compose ps)")
        if not healthy and svc != "clamav":
            return 1

    # ── 4. Workspace ──────────────────────────────────────────────────────
    print("\n── Workspace ───────────────────────────────────────────────")
    _sp.run(["uv", "sync", "--all-packages"], check=True)

    # Migrations.
    migrate_script = Path("scripts/migrate.sh")
    if migrate_script.is_file():
        _sp.run(["bash", str(migrate_script)], check=True)
    print("  ✓ workspace synced + migrated")

    # ── 5. Tenant ─────────────────────────────────────────────────────────
    print("\n── Tenant ──────────────────────────────────────────────────")

    # Check for existing tenants.
    settings = StorageSettings()
    pool = await PostgresPool.from_settings(settings.postgres)
    existing_tenants: list[dict[str, Any]] = []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tenant_id, display_name, status FROM tenants ORDER BY created_at"
            )
            existing_tenants = [dict(r) for r in rows]
    except Exception:
        pass

    fresh_tenant = False
    tenant_id: UUID
    dek_hex: str

    if existing_tenants and not args.new_tenant:
        print(f"  Found {len(existing_tenants)} existing tenant(s):")
        for t in existing_tenants:
            print(f"    {t['tenant_id']}  {t['display_name']}  ({t['status']})")
        print("\n  Re-using the most recent tenant. Pass --new-tenant to create another.")
        tenant_id = existing_tenants[-1]["tenant_id"]
        dek_hex = existing_env.get("WOXPAS_DEV_DEK_HEX", "")
        if not dek_hex:
            print("  ⚠ WOXPAS_DEV_DEK_HEX not found in .env — you'll need it to decrypt data.")
            print("    If you created this tenant with --dev, the DEK was printed then.")
    else:
        display_name = args.display_name or input("  Display name: ").strip()
        if not display_name:
            print("[woxpas] display name required.")
            return 1

        print("  Choose a passphrase — it protects your data-encryption key")
        print("  (Argon2id). You'll enter it again to unlock after a restart.")
        passphrase = _read_passphrase(prompt="  Passphrase: ", confirm=True)
        if not passphrase:
            print("[woxpas] passphrase required.")
            return 1

        tenant_id = UUID(args.tenant_id) if args.tenant_id else uuid4()

        print("\n  Deriving KEK (Argon2id — this takes a moment)…")
        secrets = init_tenant_secrets(passphrase)

        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id),
                )
                await conn.execute(
                    """
                    INSERT INTO tenants (
                      tenant_id, display_name,
                      passphrase_verifier, kdf_params,
                      encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version
                    ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
                    """,
                    tenant_id, display_name,
                    secrets.passphrase_verifier, secrets.kdf_params,
                    secrets.wrapped_dek.encrypted_dek,
                    secrets.wrapped_dek.iv,
                    secrets.wrapped_dek.tag,
                    secrets.wrapped_dek.dek_version,
                )

                source_id = await SourceRepo(conn).get_or_create(
                    tenant_id, "file_upload", "default-file-upload",
                    description="HTTP file uploads via API gateway",
                )

                sid = await ScopeRepo(conn).create(
                    tenant_id, "Inbox", description="Default catch-all scope",
                    slug="inbox", color="#888888", icon="inbox",
                )

                self_entity_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO entities (
                      entity_id, tenant_id, canonical_name, type, profile,
                      first_mentioned_at
                    ) VALUES ($1, $2, $3, 'person', $4::jsonb, now())
                    """,
                    self_entity_id, tenant_id, display_name,
                    {"is_self": True, "seeded_at_init": True},
                )
                await conn.execute(
                    "UPDATE tenants SET self_entity_id = $2 WHERE tenant_id = $1",
                    tenant_id, self_entity_id,
                )

        dek_hex = secrets.dek.hex()
        fresh_tenant = True
        print(f"\n  ✓ Tenant created: {tenant_id}")
        print(f"    Display name: {display_name}")
        print(f"    Default source: {source_id}")
        print(f"    Inbox scope: {sid}")
        print(f"    Self entity: {self_entity_id}")

        secrets.dek = b"\x00" * len(secrets.dek)

    # If the user switched any role to a cloud model, offer to open the
    # cloud-policy gate now. Otherwise the cloud model won't actually work
    # (the gate is default-off for sovereignty).
    if cloud_models_selected and not args.yes:
        print("\n── Cloud policy ──────────────────────────────────────────")
        print("  Cloud models were selected, but the egress gate is OFF by default.")
        print("  Without it, cloud LLM calls will be denied at runtime.")
        try:
            answer = input("  Allow cloud egress for personal data? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if not answer or answer in ("y", "yes"):
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE tenants SET allow_cloud_for_personal_data = true WHERE tenant_id = $1",
                    tenant_id,
                )
            print("  ✓ cloud egress enabled for personal data")
            print("    (sensitive data still stays local — flip separately if needed)")
        else:
            print("  ✗ cloud egress remains OFF")
            print("    Run later: woxpas-cli set-cloud-policy --target personal on")

    await pool.close()

    # Write tenant + DEK to env.
    existing_env["WOXPAS_DEV_TENANT_ID_DEFAULT"] = str(tenant_id)
    existing_env["WOXPAS_DEV_DEK_HEX"] = dek_hex
    existing_env["API_AUTH_MODE"] = existing_env.get("API_AUTH_MODE") or "dev"
    _write_env(str(env_path), existing_env)

    if fresh_tenant:
        print(f"\n  ✓ Wrote WOXPAS_DEV_TENANT_ID_DEFAULT + WOXPAS_DEV_DEK_HEX to {_ENV_PATH}")
    else:
        print(f"\n  ✓ .env at {_ENV_PATH} is up to date")

    # ── 6. Models ─────────────────────────────────────────────────────────
    print("\n── Models ──────────────────────────────────────────────────")

    from woxpas_runtime import (
        BGE_M3,
        BGE_RERANKER_V2_M3_INT8,
        QWEN_15B_GGUF,
        WHISPER_SMALL_CT2,
    )
    from woxpas_runtime.models import cached_bytes as _cached
    from woxpas_runtime.models import estimate_download_bytes as _est

    artefacts = [BGE_M3, QWEN_15B_GGUF, WHISPER_SMALL_CT2, BGE_RERANKER_V2_M3_INT8]
    reasons = [
        "embeddings + retrieval (3-head)",
        "summarization + extraction (default)",
        "voice note transcription",
        "search result re-ranking",
    ]

    total_new = 0
    for art in artefacts:
        size = _est(art)
        cached = _cached(art)
        new = max(0, size - cached)
        total_new += new
        label = reasons[artefacts.index(art)] if artefacts.index(art) < len(reasons) else ""
        status = "(cached)" if new == 0 else ""
        print(f"  {art.model_version:<36s} {_human_bytes(size):>8s}  {label}  {status}")

    if total_new == 0:
        print("\n  ✓ All models already cached.")
    else:
        print(f"\n  Total download: {_human_bytes(total_new)} (one-time)")
        print(f"  Cache location: {_models_dir_display()}")
        if not args.yes:
            try:
                answer = input("  Proceed with download? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Skipping — run `woxpas-cli prefetch-models` later.")
                answer = "n"
            if answer and answer not in ("y", "yes"):
                print("  Skipping — run `woxpas-cli prefetch-models` later.")
            else:
                await _download_artefacts(artefacts)
        else:
            await _download_artefacts(artefacts)

    # ── 7. Ready ──────────────────────────────────────────────────────────
    print("\n── Ready ───────────────────────────────────────────────────")
    print(f"""
  Two commands and you're live:

    woxpas-cli up                      # start everything (one command)
    woxpas-cli install-claude-desktop  # wire up Claude Desktop, then restart it

  `up` starts the data services + the worker, API, and dreaming worker in the
  background (logs in ~/.woxpas/logs). Stop them any time with `woxpas-cli down`.

  Quick test once it's up:

    curl -X POST http://127.0.0.1:7800/ingest/text \\
      -H "X-Woxpas-Tenant-Id: {tenant_id}" \\
      -H "Content-Type: application/json" \\
      -d '{{"content_text":"Hello Woxpas!"}}'

  Prefer the DEK off disk? Run `woxpas-cli unlock` (boot daemon) before `up`.
  Full docs + the manual start option: docs/GETTING_STARTED.md
""")
    return 0


def _mcp_server_block(
    *,
    cwd: str,
    tenant_id: str,
    pg_password: str,
    qdrant_key: str,
    minio_password: str,
    dek_hex: str,
) -> dict[str, Any]:
    """The single `woxpas` MCP-server entry for a Claude Desktop config."""
    return {
        "command": "uv",
        "args": ["run", "--directory", cwd, "woxpas-mcp-server"],
        "env": {
            "WOXPAS_MCP_TENANT_ID": tenant_id,
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "woxpas",
            "POSTGRES_USER": "woxpas",
            "POSTGRES_PASSWORD": pg_password,
            "QDRANT_HOST": "127.0.0.1",
            "QDRANT_PORT": "6333",
            "QDRANT_GRPC_PORT": "6334",
            "QDRANT_API_KEY": qdrant_key,
            "MINIO_ENDPOINT": "127.0.0.1:9000",
            "MINIO_ROOT_USER": "woxpas",
            "MINIO_ROOT_PASSWORD": minio_password,
            "WOXPAS_DEV_TENANT_ID_DEFAULT": tenant_id,
            "WOXPAS_DEV_DEK_HEX": dek_hex,
        },
    }


def _claude_desktop_config_path() -> Path:
    """The Claude Desktop MCP config path for the current OS."""
    from pathlib import Path as Path
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    # Linux / other
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "Claude" / "claude_desktop_config.json"


async def cmd_init_env(args: argparse.Namespace) -> int:
    """Create `.env` with generated infrastructure secrets.

    Lets a no-`uv` Docker-only install generate its secrets inside a container
    (`docker compose run --rm cli woxpas-cli init-env`). Uses `.env.example` as
    the template when present and fills any empty POSTGRES_PASSWORD /
    QDRANT_API_KEY / MINIO_ROOT_PASSWORD with random values. Idempotent — it
    never overwrites secrets that are already set.
    """
    import secrets as _secrets

    env_path = Path(_ENV_PATH)
    example = Path(".env.example")
    if not env_path.is_file():
        if example.is_file():
            env_path.write_text(example.read_text())
            print(f"  created {env_path} from .env.example")
        else:
            env_path.write_text("")
            print(f"  created empty {env_path}")

    env = _read_env(_ENV_PATH)
    generated = []
    for key in ("POSTGRES_PASSWORD", "QDRANT_API_KEY", "MINIO_ROOT_PASSWORD"):
        if not env.get(key):
            env[key] = _secrets.token_hex(24)
            generated.append(key)
    # Sensible defaults if the template didn't carry them.
    env.setdefault("POSTGRES_DB", "woxpas")
    env.setdefault("POSTGRES_USER", "woxpas")
    env.setdefault("MINIO_ROOT_USER", "woxpas")
    if not env.get("API_AUTH_MODE"):
        env["API_AUTH_MODE"] = "dev"
    _write_env(_ENV_PATH, env)

    if generated:
        print(f"  generated infra secrets: {', '.join(generated)}")
    else:
        print("  infra secrets already present — nothing to generate")
    return 0


async def cmd_install_claude_desktop(args: argparse.Namespace) -> int:
    """Merge the Woxpas MCP server into Claude Desktop's config file.

    Reads secrets + tenant from .env, detects the OS config path, and merges
    (never clobbers) a `woxpas` entry under `mcpServers`. Backs up any existing
    file first. Pass --path to target a different config file, --print to only
    print the block.
    """
    import json as _json

    env = _read_env(_ENV_PATH)
    tenant_id = env.get("WOXPAS_DEV_TENANT_ID_DEFAULT", "")
    dek_hex = env.get("WOXPAS_DEV_DEK_HEX", "")
    if not tenant_id or not dek_hex:
        print("[woxpas] no tenant found in .env — run `woxpas-cli bootstrap` first.")
        return 1

    if args.docker:
        # Docker mode: Claude Desktop spawns the MCP server in a container via
        # `docker compose run`. Secrets + tenant flow in from .env through the
        # compose `mcp` service, so nothing sensitive is written to this config.
        compose_file = str(Path.cwd() / "docker-compose.yml")
        block: dict[str, Any] = {
            "command": "docker",
            "args": ["compose", "-f", compose_file, "--profile", "mcp",
                     "run", "--rm", "-T", "mcp"],
        }
    else:
        block = _mcp_server_block(
            cwd=str(Path.cwd()),
            tenant_id=tenant_id,
            pg_password=env.get("POSTGRES_PASSWORD", ""),
            qdrant_key=env.get("QDRANT_API_KEY", ""),
            minio_password=env.get("MINIO_ROOT_PASSWORD", ""),
            dek_hex=dek_hex,
        )

    if args.print:
        print(_json.dumps({"mcpServers": {"woxpas": block}}, indent=2))
        return 0

    cfg_path = Path(args.path) if args.path else _claude_desktop_config_path()

    existing: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            existing = _json.loads(cfg_path.read_text() or "{}")
        except (ValueError, OSError) as e:
            print(f"[woxpas] couldn't read {cfg_path}: {e}")
            print("  Fix or move the file, then re-run. (Use --print to copy the block manually.)")
            return 1
        if not isinstance(existing, dict):
            print(f"[woxpas] {cfg_path} isn't a JSON object — refusing to overwrite.")
            return 1
        # Back up before touching an existing config.
        backup = cfg_path.with_suffix(cfg_path.suffix + ".woxpas-bak")
        backup.write_text(cfg_path.read_text())
        print(f"  backed up existing config → {backup}")

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"[woxpas] 'mcpServers' in {cfg_path} isn't an object — refusing to overwrite.")
        return 1
    overwrote = "woxpas" in servers
    servers["woxpas"] = block

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_json.dumps(existing, indent=2) + "\n")

    print(f"  {'updated' if overwrote else 'added'} the 'woxpas' MCP server in {cfg_path}")
    other = [k for k in servers if k != "woxpas"]
    if other:
        print(f"  (left your other MCP servers untouched: {', '.join(other)})")
    print("\n  Restart Claude Desktop to pick it up.")
    return 0


def _woxpas_home() -> Path:
    """Runtime dir for managed-process pidfiles + logs (~/.woxpas)."""
    d = Path(os.environ.get("WOXPAS_HOME", str(Path.home() / ".woxpas")))
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


# (name, console-script) for the long-running app processes `up` manages.
_APP_PROCS = [
    ("worker",   "woxpas-worker"),
    ("api",      "woxpas-api"),
    ("dreaming", "woxpas-dreaming-worker"),
]


def _proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def cmd_up(args: argparse.Namespace) -> int:
    """One command to bring the whole instance up: data services (Docker) +
    the pipeline worker, API, and dreaming worker (managed background
    processes). Re-runnable — skips anything already running. `woxpas-cli down`
    stops the app processes again.
    """
    import subprocess as _sp

    env = _read_env(_ENV_PATH)
    if not env.get("WOXPAS_DEV_TENANT_ID_DEFAULT"):
        print("[woxpas] no tenant yet — run `woxpas-cli bootstrap` first.")
        return 1
    home = _woxpas_home()

    # 1. Data services (Postgres / Qdrant / MinIO / ClamAV) via Docker.
    docker = shutil.which("docker") or "docker"
    compose_files = ["docker-compose.yml"]
    if Path("docker-compose.override.yml").is_file():
        compose_files.append("docker-compose.override.yml")
    cargs: list[str] = []
    for cf in compose_files:
        cargs += ["-f", cf]
    print("── Data services ──────────────────────────────────────────")
    _sp.run(
        [docker, "compose", *cargs, "up", "-d",
         "postgres", "qdrant", "minio", "clamav"],
        check=True,
    )

    # 2. Migrations (idempotent — safe to run every time).
    migrate = Path("scripts/migrate.sh")
    if migrate.is_file():
        _sp.run(["bash", str(migrate)], check=False)

    # 3. App processes — detached, one log + pidfile each under ~/.woxpas.
    print("\n── App services ───────────────────────────────────────────")
    for name, script in _APP_PROCS:
        if name == "dreaming" and args.no_dreaming:
            continue
        pidf = home / f"{name}.pid"
        if pidf.is_file():
            try:
                old = int(pidf.read_text().strip())
                if _proc_alive(old):
                    print(f"  • {name} already running (pid {old})")
                    continue
            except ValueError:
                pass
        log_path = home / "logs" / f"{name}.log"
        logf = log_path.open("a")
        proc = _sp.Popen(
            ["uv", "run", script],
            stdout=logf, stderr=_sp.STDOUT, stdin=_sp.DEVNULL,
            start_new_session=True,
        )
        pidf.write_text(str(proc.pid))
        print(f"  ✓ {name:<9s} (pid {proc.pid}) → {log_path}")

    api_port = env.get("API_PORT", "7800")
    print(f"""
  Up. API → http://127.0.0.1:{api_port}

    Logs:   tail -f {home}/logs/*.log
    Stop:   woxpas-cli down
    Claude: woxpas-cli install-claude-desktop
""")
    return 0


async def cmd_down(args: argparse.Namespace) -> int:
    """Stop the app processes started by `up`. Data services (Docker) are left
    running by default so the MCP server keeps working; pass --docker to stop
    those too.
    """
    import signal as _sig

    home = _woxpas_home()
    stopped = 0
    for name, _script in _APP_PROCS:
        pidf = home / f"{name}.pid"
        if not pidf.is_file():
            continue
        try:
            pid = int(pidf.read_text().strip())
        except ValueError:
            pidf.unlink(missing_ok=True)
            continue
        if _proc_alive(pid):
            try:
                os.kill(pid, _sig.SIGTERM)
                stopped += 1
                print(f"  ✓ stopped {name} (pid {pid})")
            except OSError as e:
                print(f"  ⚠ couldn't stop {name} (pid {pid}): {e}")
        pidf.unlink(missing_ok=True)
    if stopped == 0:
        print("  (no managed app processes were running)")

    if args.docker:
        import subprocess as _sp
        docker = shutil.which("docker") or "docker"
        _sp.run([docker, "compose", "stop"], check=False)
        print("  ✓ stopped data services")
    else:
        print("  Data services left running — stop them with:  docker compose stop")
    return 0


async def _download_artefacts(artefacts: list[Any]) -> None:
    """Download a list of model artefacts with progress reporting."""
    from woxpas_runtime import fetch
    for art in artefacts:
        print(f"  → {art.model_version} ({art.repo})")
        await asyncio.to_thread(fetch, art)
    print("  ✓ downloads complete")


# ─── init-tenant ───────────────────────────────────────────────────────────

async def cmd_init_tenant(args: argparse.Namespace) -> int:
    settings = StorageSettings()
    passphrase = _read_passphrase()
    tenant_id = UUID(args.tenant_id) if args.tenant_id else uuid4()

    print("\n[woxpas-cli] deriving KEK (Argon2id) — this is intentionally slow…")
    secrets = init_tenant_secrets(passphrase)

    pool = await PostgresPool.from_settings(settings.postgres)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # tenants table has FORCE RLS — set app.tenant_id to the new id
                # so the WITH CHECK passes for our own row.
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)",
                    str(tenant_id),
                )
                await conn.execute(
                    """
                    INSERT INTO tenants (
                      tenant_id, display_name,
                      passphrase_verifier, kdf_params,
                      encrypted_dek, encrypted_dek_iv, encrypted_dek_tag, dek_version
                    ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)
                    """,
                    tenant_id, args.display_name,
                    secrets.passphrase_verifier, secrets.kdf_params,
                    secrets.wrapped_dek.encrypted_dek,
                    secrets.wrapped_dek.iv,
                    secrets.wrapped_dek.tag,
                    secrets.wrapped_dek.dek_version,
                )

                # Default file_upload source so the API has something to attach to.
                source_id = await SourceRepo(conn).get_or_create(
                    tenant_id, "file_upload", "default-file-upload",
                    description="HTTP file uploads via API gateway",
                )

                seeded_scope_ids: list[UUID] = []
                if args.with_inbox:
                    sid = await ScopeRepo(conn).create(
                        tenant_id, "Inbox", description="Default catch-all scope",
                        slug="inbox", color="#888888", icon="inbox",
                    )
                    seeded_scope_ids.append(sid)

                # Self-entity — first-class entity representing the tenant owner.
                # Default owner of all ideas/tasks. Identity card reads from this.
                # Once the identity-extraction pipeline lands, profile facts
                # accrete here from any doc whose subject resolves to "me".
                self_entity_id = uuid4()
                await conn.execute(
                    """
                    INSERT INTO entities (
                      entity_id, tenant_id, canonical_name, type, profile,
                      first_mentioned_at
                    ) VALUES ($1, $2, $3, 'person', $4::jsonb, now())
                    """,
                    self_entity_id, tenant_id, args.display_name,
                    {"is_self": True, "seeded_at_init": True},
                )
                await conn.execute(
                    "UPDATE tenants SET self_entity_id = $2 WHERE tenant_id = $1",
                    tenant_id, self_entity_id,
                )

                # Optional: import an identity-export markdown into entity_profile_facts.
                # See templates/identity_export_prompt.md for the export prompt the user
                # pastes into ChatGPT / Claude / etc. The result lands here verbatim
                # (one fact per bullet); Phase 2 extraction can re-process for richer
                # structure later.
                imported_facts = 0
                if args.identity_from:
                    from pathlib import Path

                    from woxpas_pipeline.identity import parse_identity_markdown

                    md_path = Path(args.identity_from).expanduser().resolve()
                    if not md_path.is_file():
                        raise SystemExit(f"identity-from file not found: {md_path}")
                    md_text = md_path.read_text(encoding="utf-8")

                    facts = parse_identity_markdown(md_text)
                    if facts:
                        rows = [
                            (
                                self_entity_id, tenant_id,
                                f.fact_category, f.fact_kind, f.fact_value,
                                1.0, True,            # confidence, is_user_asserted
                                "identity_seed_parser", "0.1.0",
                            )
                            for f in facts
                        ]
                        await conn.executemany(
                            """
                            INSERT INTO entity_profile_facts (
                              entity_id, tenant_id, fact_category, fact_kind, fact_value,
                              confidence, is_user_asserted, extractor, extractor_version
                            ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                            """,
                            rows,
                        )
                        imported_facts = len(rows)
    finally:
        await pool.close()

    print("\n[woxpas-cli] tenant created.")
    _print_kv({
        "tenant_id":       tenant_id,
        "display_name":    args.display_name,
        "self_entity_id":  self_entity_id,
        "default_source":  source_id,
        "seeded_scopes":   seeded_scope_ids or "(none)",
        "identity_facts":  imported_facts if args.identity_from else "(none — pass --identity-from to import)",
    })

    if args.dev:
        print("\n[woxpas-cli] dev mode: copy these into your .env\n")
        print(f"WOXPAS_DEV_TENANT_ID_DEFAULT={tenant_id}")
        print(f"WOXPAS_DEV_DEK_HEX={secrets.dek.hex()}")
        print("# (the DEK is the plaintext key; treat it as a secret)\n")
    else:
        print(
            "\n[woxpas-cli] DEK was NOT printed (run with --dev to print for local use).\n"
            "             Production boot service will unwrap from Postgres + passphrase."
        )

    # Best-effort: zeroize the local copy of DEK in our process memory.
    secrets.dek = b"\x00" * len(secrets.dek)
    return 0


# ─── create-source ─────────────────────────────────────────────────────────

async def cmd_create_source(args: argparse.Namespace) -> int:
    settings = StorageSettings()
    pool = await PostgresPool.from_settings(settings.postgres)
    tenant_id = UUID(args.tenant_id)
    config = json.loads(args.config) if args.config else {}
    try:
        async with pool.tenant_session(tenant_id) as conn:
            source_id = await SourceRepo(conn).get_or_create(
                tenant_id, args.type, args.name,
                config=config,
                description=args.description,
            )
    finally:
        await pool.close()
    print(f"source_id  {source_id}")
    return 0


# ─── create-scope ──────────────────────────────────────────────────────────

async def cmd_create_scope(args: argparse.Namespace) -> int:
    """Create a scope. Also embeds (name + description) and upserts into
    Qdrant scopes_collection so the summarize stage's auto-assignment can
    find it. Without an embedding, a scope is invisible to auto-assign —
    intentional for scopes like 'Inbox' that should never auto-claim docs.
    """
    from woxpas_core.ids import content_uuid
    from woxpas_runtime import build_embedder
    from woxpas_storage import QdrantClient, ScopePoint

    settings = StorageSettings()
    tenant_id = UUID(args.tenant_id)
    parent_id = UUID(args.parent) if args.parent else None

    pool = await PostgresPool.from_settings(settings.postgres)
    qdrant = await QdrantClient.from_settings(settings.qdrant)
    try:
        async with pool.tenant_session(tenant_id) as conn:
            scope_repo = ScopeRepo(conn)
            scope_id = await scope_repo.create(
                tenant_id, args.name,
                description=args.description,
                slug=args.slug,
                parent_scope_id=parent_id,
                color=args.color,
                icon=args.icon,
            )

        embedding_ref: UUID | None = None
        if args.description or not args.no_embed:
            text = (args.name or "")
            if args.description:
                text = f"{text}: {args.description}"
            text = text.strip()
            if text:
                # Embedder backend resolved via the catalog — local ONNX today,
                # cloud (e.g., text-embedding-3-large) when EMBED_MODEL_NAME flips.
                embedder = build_embedder(model_name=args.embed_model_name)
                batch = await embedder.embed([text], batch_size=1)
                if batch.dense:
                    embedding_ref = content_uuid(
                        "scope", str(scope_id), embedder.model_version,
                    )
                    await qdrant.upsert_scope(ScopePoint(
                        point_id=embedding_ref,
                        tenant_id=tenant_id,
                        scope_id=scope_id,
                        member_count=0,
                        description_vector=batch.dense[0],
                        centroid_vector=batch.dense[0],   # bootstrap centroid = description for now
                    ))
                    async with pool.tenant_session(tenant_id) as conn:
                        await ScopeRepo(conn).update_embedding_ref(
                            tenant_id, scope_id, embedding_ref,
                        )
    finally:
        await qdrant.close()
        await pool.close()

    print(f"scope_id     {scope_id}")
    if embedding_ref:
        print(f"embedded     yes (scopes_collection point {embedding_ref})")
    else:
        print("embedded     no  (auto-assignment will skip this scope)")
    return 0


# ─── identity-template ─────────────────────────────────────────────────────

async def cmd_identity_template(args: argparse.Namespace) -> int:
    """Print the identity-export prompt template to stdout.

    The user pastes this into ChatGPT / Claude / Gemini / etc., saves the
    response as a .md file, then imports it via:

        woxpas-cli init-tenant --display-name X --identity-from <saved.md>
    """
    from pathlib import Path
    # Locate the template relative to the repo root. Walk upward from this
    # file until we find templates/identity_export_prompt.md.
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent.parent / "templates" / "identity_export_prompt.md",
        Path.cwd() / "templates" / "identity_export_prompt.md",
    ]
    for p in candidates:
        if p.is_file():
            print(p.read_text(encoding="utf-8"))
            return 0
    raise SystemExit(
        "could not locate templates/identity_export_prompt.md; "
        "expected one of: " + ", ".join(str(c) for c in candidates)
    )


# ─── status ────────────────────────────────────────────────────────────────

async def cmd_status(args: argparse.Namespace) -> int:
    settings = StorageSettings()
    pool = await PostgresPool.from_settings(settings.postgres)
    try:
        async with pool.acquire() as conn:
            tenants = await conn.fetch(
                "SELECT tenant_id, display_name, status, created_at FROM tenants"
            )
    finally:
        await pool.close()
    if not tenants:
        print("(no tenants)")
        return 0
    print(f"{'tenant_id':<38}  {'status':<10}  display_name")
    for r in tenants:
        print(f"{r['tenant_id']!s:<38}  {r['status']:<10}  {r['display_name']}")
    return 0


# ─── inspect-doc ───────────────────────────────────────────────────────────

async def cmd_inspect_doc(args: argparse.Namespace) -> int:
    """Print everything we know about a single document.

    Useful for ops/debugging — confirms what landed in Postgres + Qdrant +
    object store for a given doc_id. With --with-raw / --with-canonical it
    decrypts and prints (or saves) the original / CDM payloads.
    """
    settings = StorageSettings()

    tenant_id = _resolve_tenant_id(args.tenant_id)
    doc_id    = UUID(args.doc_id)

    pool = await PostgresPool.from_settings(settings.postgres)
    try:
        async with pool.tenant_session(tenant_id) as conn:
            doc = await conn.fetchrow(
                """
                SELECT doc_id, tenant_id, status, processing_profile,
                       processing_profile_version, source_channel, captured_by,
                       thread_id, filename, mime, size_bytes, page_count,
                       duration_ms, language, language_detected,
                       raw_uri, canonical_uri, raw_iv, canonical_iv,
                       sha256, summary, topics_inferred,
                       summary_model_version, summary_embedding_ref,
                       supersedes_candidate_doc_id, supersedes_doc_id,
                       created_at, updated_at
                  FROM documents
                 WHERE tenant_id = $1 AND doc_id = $2 AND deleted_at IS NULL
                """,
                tenant_id, doc_id,
            )
            if doc is None:
                print(f"document {doc_id} not found for tenant {tenant_id}")
                return 1

            runs = await conn.fetch(
                "SELECT stage, status, duration_ms, error_class, error_message "
                "  FROM pipeline_runs "
                " WHERE tenant_id = $1 AND doc_id = $2 ORDER BY started_at",
                tenant_id, doc_id,
            )
            block_count = await conn.fetchval(
                "SELECT count(*) FROM canonical_blocks WHERE tenant_id = $1 AND doc_id = $2",
                tenant_id, doc_id,
            )
            chunk_count = await conn.fetchval(
                "SELECT count(*) FROM chunks WHERE tenant_id = $1 AND doc_id = $2",
                tenant_id, doc_id,
            )
            span_count = await conn.fetchval(
                "SELECT count(*) FROM spans WHERE tenant_id = $1 AND doc_id = $2",
                tenant_id, doc_id,
            )
            scope_rows = await conn.fetch(
                "SELECT s.name, s.slug FROM doc_scopes ds "
                "JOIN scopes s USING (scope_id, tenant_id) "
                "WHERE ds.tenant_id = $1 AND ds.doc_id = $2",
                tenant_id, doc_id,
            )
            blocks = []
            if args.with_blocks or args.with_canonical:
                rows = await conn.fetch(
                    "SELECT ordinal, type, level, text, source_locator "
                    "  FROM canonical_blocks "
                    " WHERE tenant_id = $1 AND doc_id = $2 ORDER BY ordinal",
                    tenant_id, doc_id,
                )
                blocks = [dict(r) for r in rows]
    finally:
        await pool.close()

    # ─── render the report ─────────────────────────────────────────────────
    print()
    print(f"Document {doc['doc_id']}")
    _print_kv({
        "tenant":               doc["tenant_id"],
        "status":               doc["status"],
        "processing_profile":   doc["processing_profile"],
        "profile_version":      doc["processing_profile_version"],
        "source_channel":       doc["source_channel"],
        "captured_by":          doc["captured_by"],
        "thread_id":            doc["thread_id"] or "(none)",
        "filename":             doc["filename"] or "(none)",
        "mime":                 doc["mime"] or "(none)",
        "size":                 _fmt_size(doc["size_bytes"]),
        "page_count":           doc["page_count"] if doc["page_count"] is not None else "—",
        "duration_ms":          doc["duration_ms"] if doc["duration_ms"] is not None else "—",
        "language":             doc["language_detected"] or doc["language"] or "—",
        "sha256":               doc["sha256"].hex() if doc["sha256"] else "(none)",
        "created_at":           doc["created_at"],
        "updated_at":           doc["updated_at"],
    })

    print("\nSummary:")
    if doc["summary"]:
        _print_kv({
            "model":             doc["summary_model_version"] or "—",
            "embedding_ref":     doc["summary_embedding_ref"] or "(none)",
            "topics":            ", ".join(doc["topics_inferred"] or []) or "(none)",
        })
        print(f"  {doc['summary']}")
    else:
        print("  (none)")

    print("\nProvenance:")
    _print_kv({
        "raw_uri":               doc["raw_uri"] or "(none)",
        "canonical_uri":         doc["canonical_uri"] or "(none)",
    })

    print("\nPipeline runs:")
    for r in runs:
        line = f"  {r['stage']:<22} | {r['status']:<10} | "
        line += f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "—"
        if r["status"] == "failed":
            line += f"  ({r['error_class']}: {r['error_message']})"
        print(line)

    print("\nStorage counts:")
    _print_kv({
        "canonical_blocks": block_count,
        "chunks":           chunk_count,
        "spans":            span_count,
    })

    print("\nSupersession:")
    cand = doc.get("supersedes_candidate_doc_id")
    sup  = doc.get("supersedes_doc_id")
    _print_kv({
        "candidate":  cand or "(none)",
        "confirmed":  sup  or "(none)",
    })

    print("\nScopes:")
    if scope_rows:
        for s in scope_rows:
            print(f"  - {s['name']}  ({s['slug']})")
    else:
        print("  (none)")

    if args.with_blocks:
        print("\nCanonical blocks:")
        for b in blocks:
            text = (b["text"] or "").replace("\n", " ").strip()
            text = (text[:100] + "…") if len(text) > 100 else text
            level = f"h{b['level']}" if b["type"] == "heading" and b["level"] else ""
            print(f"  [{b['ordinal']:>3}] {b['type']:<14} {level:<4} {text!r}")

    if args.with_raw or args.with_canonical:
        dek = _resolve_dek()
        from woxpas_storage import ObjectStore
        obj = ObjectStore(settings.objstore)
        if args.with_raw:
            print("\nRaw bytes (decrypted):")
            sha_hex = doc["sha256"].hex()
            try:
                raw = await obj.get_raw_bytes(sha_hex, dek)
                _print_payload(raw, mime=doc["mime"], limit=args.payload_limit,
                               save_to=args.save_raw)
            except Exception as e:
                print(f"  ERROR: {e}")
        if args.with_canonical:
            print("\nCanonical CDM (decrypted, v1):")
            try:
                cdm = await obj.get_canonical(doc["doc_id"], 1, dek)
                _print_payload(cdm, mime="application/json", limit=args.payload_limit,
                               save_to=args.save_canonical)
            except Exception as e:
                print(f"  ERROR: {e}")

    return 0


def _resolve_tenant_id(arg: str | None) -> UUID:
    if arg:
        return UUID(arg)
    env = os.environ.get("WOXPAS_DEV_TENANT_ID_DEFAULT")
    if env:
        return UUID(env)
    raise SystemExit(
        "tenant id required: pass --tenant-id or set WOXPAS_DEV_TENANT_ID_DEFAULT"
    )


def _resolve_dek() -> bytes:
    """Read the dev-mode DEK from env. Production uses the boot service socket."""
    hex_ = os.environ.get("WOXPAS_DEV_DEK_HEX")
    if not hex_:
        raise SystemExit(
            "WOXPAS_DEV_DEK_HEX is not set; cannot decrypt object-store payloads. "
            "Run 'woxpas-cli init-tenant --dev' or set the env var."
        )
    return bytes.fromhex(hex_)


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024:
            return f"{val:.1f}{unit}"
        val /= 1024
    return f"{val:.1f}TB"


def _print_payload(data: bytes, *, mime: str | None, limit: int, save_to: str | None) -> None:
    if save_to:
        with open(save_to, "wb") as f:
            f.write(data)
        print(f"  saved {len(data)} bytes to {save_to}")
        return
    is_text = (mime or "").startswith("text/") or (mime or "") == "application/json"
    if is_text:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
        if len(text) > limit:
            print("  " + text[:limit].replace("\n", "\n  "))
            print(f"  … ({len(text) - limit} more chars; pass --payload-limit or --save-* to capture)")
        else:
            print("  " + text.replace("\n", "\n  "))
    else:
        head = data[:32].hex()
        print(f"  binary, {len(data)} bytes, first 32 = {head}")
        print("  (use --save-raw / --save-canonical to write to disk)")


# ─── prefetch-models ───────────────────────────────────────────────────────

async def cmd_prefetch_models(args: argparse.Namespace) -> int:
    """Download all default model artefacts into the local cache.

    Shows a download-size budget table BEFORE downloading and prompts for
    confirmation, so a sovereign install never silently consumes gigabytes.
    Idempotent: re-running re-uses the cache (only newly-needed bytes pulled).
    """
    from woxpas_runtime import (
        BGE_M3,
        BGE_RERANKER_V2_M3_INT8,
        QWEN_15B_GGUF,
        WHISPER_SMALL_CT2,
        fetch,
    )
    from woxpas_runtime.models import cached_bytes, estimate_download_bytes

    artefacts = [BGE_M3, QWEN_15B_GGUF, WHISPER_SMALL_CT2, BGE_RERANKER_V2_M3_INT8]
    if args.with_extraction_model:
        from woxpas_runtime import QWEN_3B_GGUF
        artefacts.append(QWEN_3B_GGUF)

    # Surface the optional-upgrade trade-off so users on small disks /
    # without strong CPUs can opt out informedly.
    if not args.with_extraction_model:
        print(
            "\n[woxpas-cli] tip: --with-extraction-model adds Qwen2.5-3B (+2 GB).\n"
            "             you gain: noticeably better Phase 2 extraction —\n"
            "             cleaner JSON output, more reliable claim/task/idea\n"
            "             capture, fewer dropped fields on multi-section docs.\n"
            "             you pay:  ~2 GB extra disk, ~2-3× slower extraction\n"
            "             per chunk on CPU (130s vs 50s on i9). Default is\n"
            "             the smaller Qwen2.5-1.5B which works for short notes\n"
            "             but is noisy on rich documents."
        )

    # Compute per-artefact: total size (estimate) + already-cached + new bytes.
    print(f"\n[woxpas-cli] inspecting {len(artefacts)} model(s) for download budget…\n")
    rows = []
    total_size = 0
    total_cached = 0
    for art in artefacts:
        size = estimate_download_bytes(art)
        cached = cached_bytes(art)
        total_size += size
        total_cached += cached
        rows.append((art, size, cached))

    new_bytes = max(0, total_size - total_cached)

    # Render budget table.
    print(f"  {'MODEL':<38s}  {'SIZE':>9s}  {'CACHED':>9s}  {'TO DOWNLOAD':>12s}")
    print(f"  {'-'*38}  {'-'*9}  {'-'*9}  {'-'*12}")
    for art, size, cached in rows:
        new = max(0, size - cached)
        marker = "(cached)" if cached >= size and size > 0 else ""
        print(f"  {art.model_version:<38s}  {_human_bytes(size):>9s}  "
              f"{_human_bytes(cached):>9s}  {_human_bytes(new):>12s}  {marker}")
    print(f"  {'-'*38}  {'-'*9}  {'-'*9}  {'-'*12}")
    print(f"  {'TOTAL':<38s}  {_human_bytes(total_size):>9s}  "
          f"{_human_bytes(total_cached):>9s}  {_human_bytes(new_bytes):>12s}")

    if args.list_only:
        print("\n[woxpas-cli] --list-only: no downloads run.")
        return 0

    if new_bytes == 0:
        print("\n[woxpas-cli] all models already cached. No download needed.")
        return 0

    if not args.yes:
        print(f"\n[woxpas-cli] this will download approximately {_human_bytes(new_bytes)}")
        print(f"             into {_models_dir_display()}")
        try:
            answer = input("[woxpas-cli] proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[woxpas-cli] aborted.")
            return 1
        if answer not in ("y", "yes"):
            print("[woxpas-cli] aborted (use --yes to skip this prompt in scripts).")
            return 1

    print(f"\n[woxpas-cli] prefetching {len(artefacts)} model(s)…")
    for art in artefacts:
        print(f"  → {art.model_version}  (repo: {art.repo})")
        path = fetch(art)
        size_str = _path_size(path)
        print(f"    cached at {path}  ({size_str})")
    print("\n[woxpas-cli] all models cached. Workers will load from disk on first call.")
    return 0


def _human_bytes(n: int | float) -> str:
    """Human-readable byte count; works on raw bytes (not pre-divided)."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _models_dir_display() -> str:
    """Best-effort path string for the models cache (matches _models_root)."""
    return os.environ.get("WOXPAS_MODELS_DIR") or "./models"


def _path_size(p) -> str:  # type: ignore[no-untyped-def]
    """Best-effort human-readable size of a file or directory."""
    if p.is_file():
        n = p.stat().st_size
    else:
        n = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return _human_bytes(n)


# ─── cache-info ────────────────────────────────────────────────────────────

async def cmd_cache_info(args: argparse.Namespace) -> int:
    """Inspect what's currently cached + what would be needed for the
    configured workload. Useful for ops + for users on small disks."""
    from woxpas_runtime import (
        BGE_M3,
        BGE_RERANKER_V2_M3_INT8,
        MODEL_CATALOG,
        QWEN_15B_GGUF,
        WHISPER_SMALL_CT2,
    )
    from woxpas_runtime.models import _models_root, cached_bytes, estimate_download_bytes

    cache = _models_root()
    print(f"\n[woxpas-cli] models cache: {cache}\n")

    # Required-for-default-config artefacts.
    required = [BGE_M3, QWEN_15B_GGUF, WHISPER_SMALL_CT2, BGE_RERANKER_V2_M3_INT8]
    print(f"  {'ARTEFACT (required default config)':<45s}  {'CACHED':>10s}  {'EXPECTED':>10s}")
    print(f"  {'-'*45}  {'-'*10}  {'-'*10}")
    for art in required:
        size = estimate_download_bytes(art) if args.live else 0
        cached = cached_bytes(art)
        expected = _human_bytes(size) if args.live else "(use --live)"
        print(f"  {art.model_version:<45s}  {_human_bytes(cached):>10s}  {expected:>10s}")
    print()

    # Everything else in the catalog. Multiple artefact NAMES can point at the
    # same `repo` dir (e.g., the v2-m3 fp16/fp32/int8 trio). De-dup by repo
    # so we don't triple-count the same bytes. Show all the artefact names
    # that share a dir as a single grouped row.
    required_repos = {a.repo for a in required if a.repo}
    by_repo: dict[str, list] = {}                       # type: ignore[type-arg]
    for name in sorted(set(MODEL_CATALOG) - {a.name for a in required}):
        art = MODEL_CATALOG[name]
        if not art.repo or art.repo in required_repos:
            continue
        by_repo.setdefault(art.repo, []).append(art)

    other_groups: list[tuple[str, list[str], int]] = []
    for repo, arts in by_repo.items():
        c = cached_bytes(arts[0])    # any artefact with this repo gives the same dir size
        if c > 0:
            other_groups.append((repo, [a.model_version for a in arts], c))

    if other_groups:
        print(f"  {'OTHER REPOS (cached but not used by default config)':<45s}  {'CACHED':>10s}")
        print(f"  {'-'*45}  {'-'*10}")
        for repo, names, c in other_groups:
            label = repo if len(names) == 1 else f"{repo} ({len(names)} variants)"
            print(f"  {label:<45s}  {_human_bytes(c):>10s}")
            if len(names) > 1:
                for n in names:
                    print(f"    └─ {n}")
        total_other = sum(c for _, _, c in other_groups)
        print(f"  {'-'*45}  {'-'*10}")
        print(f"  {'subtotal (deduped by repo)':<45s}  {_human_bytes(total_other):>10s}")
    else:
        print("  (no extra repos cached beyond required)")

    # Total cache size.
    if cache.exists():
        total = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file() and not f.is_symlink())
        print(f"\n  total models/ on disk: {_human_bytes(total)}")
    else:
        print("\n  (cache dir does not exist yet)")
    return 0


# ─── set-cloud-policy ──────────────────────────────────────────────────────

_TARGET_TO_COLUMN = {
    "personal":  "allow_cloud_for_personal_data",
    "sensitive": "allow_cloud_for_sensitive_data",
}


async def cmd_set_cloud_policy(args: argparse.Namespace) -> int:
    """Flip a tenant's cloud-policy switch on or off.

    Two independent switches:
      --target personal   → allow_cloud_for_personal_data
                            gates GATED_PROFILES (personal, identity_seed,
                            correspondence) when off
      --target sensitive  → allow_cloud_for_sensitive_data
                            gates docs with sensitivity_class='sensitive'
                            (any profile) when off — STRICTER

    Switches are independent: opting into personal-data cloud does NOT
    auto-unlock sensitive-data cloud. Audit log records every flip + every
    cloud egress (success/denied/error).
    """
    column = _TARGET_TO_COLUMN[args.target]
    pool = await PostgresPool.from_settings(StorageSettings().postgres)
    try:
        async with pool.acquire() as conn:
            current = await conn.fetchval(
                f"SELECT {column} FROM tenants WHERE tenant_id = $1",
                UUID(args.tenant_id),
            )
            if current is None:
                print(f"[woxpas-cli] tenant {args.tenant_id} not found")
                return 1
            new_val = (args.value.lower() in {"true", "on", "yes", "1"})
            if new_val == bool(current):
                print(f"[woxpas-cli] {column} already {new_val} "
                      f"for tenant {args.tenant_id}")
                return 0
            await conn.execute(
                f"UPDATE tenants SET {column} = $1 WHERE tenant_id = $2",
                new_val, UUID(args.tenant_id),
            )
            await conn.execute(
                """
                INSERT INTO audit_log (tenant_id, actor, actor_kind, action,
                                       target_kind, outcome, metadata)
                VALUES ($1, 'cli', 'user', 'tenant.cloud_policy_set',
                        'tenant', 'success', $2)
                """,
                UUID(args.tenant_id),
                {"target": args.target, "column": column,
                 "old": bool(current), "new": new_val},
            )
        print(f"[woxpas-cli] {column}: "
              f"{bool(current)} → {new_val}  (tenant {args.tenant_id})")
        return 0
    finally:
        await pool.close()


# ─── llm-test ──────────────────────────────────────────────────────────────

async def cmd_llm_test(args: argparse.Namespace) -> int:
    """Round-trip a small prompt through one logical model.

    Sanity-check before running real ingests. Verifies: catalog resolution,
    backend dispatch, env-var auth (cloud), and chat-completion shape.
    Does NOT touch the cloud-policy gate — that lives in the summarize stage.
    """
    from woxpas_runtime import Message, build_llm_backend

    backend = build_llm_backend(args.model)
    print(f"[woxpas-cli] backend     : {type(backend).__name__}")
    print(f"[woxpas-cli] model       : {backend.name} (version {backend.model_version})")
    print(f"[woxpas-cli] backend_kind: {backend.backend_kind}")
    print(f"[woxpas-cli] is_cloud    : {backend.is_cloud}")
    print(f"[woxpas-cli] prompt      : {args.prompt!r}")
    print()

    messages = [
        Message(role="system", content="You are a concise assistant. Reply in one sentence."),
        Message(role="user", content=args.prompt),
    ]
    try:
        response = await backend.generate(
            messages, max_tokens=args.max_tokens, temperature=0.0,
            timeout_s=args.timeout,
        )
    except Exception as e:
        print(f"[woxpas-cli] FAILED: {type(e).__name__}: {e}")
        return 1

    print("─" * 60)
    print(response.text.strip())
    print("─" * 60)
    print(f"[woxpas-cli] finish_reason : {response.finish_reason}")
    print(f"[woxpas-cli] tokens        : "
          f"{response.usage.prompt_tokens} prompt + "
          f"{response.usage.completion_tokens} completion = "
          f"{response.usage.total_tokens} total")
    return 0


# ─── mark-sensitive ────────────────────────────────────────────────────────

async def cmd_mark_sensitive(args: argparse.Namespace) -> int:
    """Override a document's sensitivity_class.

    Use this when:
      - User wants to upgrade a doc to sensitive after upload
      - User wants to DOWNGRADE a doc the auto-tiers wrongly flagged
        (e.g., a fitness journal that tripped a soft-keyword corroborated
        topic match)

    Auto-tiers (3a heuristic, 2 LLM, 3 refine) only UPGRADE — they will
    never reverse a manual downgrade. To re-enable auto-classification on
    a doc, set --value standard then re-ingest, or just live with the
    manual override (preferred).

    Audit log records the change with actor='cli'.
    """
    pool = await PostgresPool.from_settings(StorageSettings().postgres)
    tenant_id = UUID(args.tenant_id) if args.tenant_id else \
        UUID(os.environ["WOXPAS_DEV_TENANT_ID_DEFAULT"])
    doc_id = UUID(args.doc_id)
    try:
        async with pool.tenant_session(tenant_id) as conn:
            row = await conn.fetchrow(
                "SELECT sensitivity_class, sensitivity_reason "
                "FROM documents WHERE tenant_id = $1 AND doc_id = $2",
                tenant_id, doc_id,
            )
            if row is None:
                print(f"[woxpas-cli] doc {doc_id} not found in tenant {tenant_id}")
                return 1
            old_class  = row["sensitivity_class"]
            old_reason = row["sensitivity_reason"]
            new_reason = f"manual:cli:{args.reason}" if args.reason else "manual:cli"
            from woxpas_storage import DocumentRepo
            await DocumentRepo(conn).set_sensitivity_class(
                tenant_id, doc_id,
                sensitivity_class=args.value,
                reason=new_reason,
            )
            await conn.execute(
                """
                INSERT INTO audit_log (tenant_id, actor, actor_kind, action,
                                       target_kind, target_id, outcome, metadata)
                VALUES ($1, 'cli', 'user', 'doc.sensitivity_set',
                        'document', $2, 'success', $3)
                """,
                tenant_id, doc_id,
                {"old_class": old_class, "old_reason": old_reason,
                 "new_class": args.value, "new_reason": new_reason},
            )
        print(f"[woxpas-cli] doc {doc_id}: sensitivity_class "
              f"{old_class!r} → {args.value!r}")
        print(f"[woxpas-cli]   old reason: {old_reason!r}")
        print(f"[woxpas-cli]   new reason: {new_reason!r}")
        return 0
    finally:
        await pool.close()


# ─── policy-gate-test ──────────────────────────────────────────────────────

async def cmd_policy_gate_test(args: argparse.Namespace) -> int:
    """Exercise the cloud-policy gate against a real backend.

    Doesn't actually call the LLM — the gate fires before egress. With policy
    off and a gated profile, you'll see CloudPolicyDenied + a 'denied' audit
    row. With policy on (or a non-gated profile), you'll see ALLOWED and no
    audit row (audit happens post-call in the real pipeline; the gate alone
    only writes on deny).

    Usage:
      # 1. Ensure policy off for the tenant
      woxpas-cli set-cloud-policy --tenant-id <uuid> off
      # 2. Confirm gate denies a personal-profile cloud call
      woxpas-cli policy-gate-test --tenant-id <uuid> --model deepseek-chat --profile personal
      # 3. Flip policy on, re-run, confirm allowed
      woxpas-cli set-cloud-policy --tenant-id <uuid> on
      woxpas-cli policy-gate-test --tenant-id <uuid> --model deepseek-chat --profile personal
    """
    from woxpas_runtime import build_llm_backend

    from woxpas_pipeline.cloud_policy import (
        GATED_PROFILES,
        CloudPolicyDenied,
        assert_cloud_allowed,
    )

    tenant_id = UUID(args.tenant_id)
    pool = await PostgresPool.from_settings(StorageSettings().postgres)
    try:
        backend = build_llm_backend(args.model)
        print(f"[woxpas-cli] tenant       : {tenant_id}")
        print(f"[woxpas-cli] backend      : {backend.name} (kind={backend.backend_kind}, cloud={backend.is_cloud})")
        print(f"[woxpas-cli] profile      : {args.profile} "
              f"({'GATED' if args.profile in GATED_PROFILES else 'not gated'})")

        async with pool.acquire() as conn:
            policies = await conn.fetchrow(
                """
                SELECT allow_cloud_for_personal_data,
                       allow_cloud_for_sensitive_data
                  FROM tenants WHERE tenant_id = $1
                """,
                tenant_id,
            )
        if policies is None:
            print(f"[woxpas-cli] FAILED: tenant {tenant_id} not found")
            return 1
        print(f"[woxpas-cli] sensitivity  : {args.sensitivity}")
        print(f"[woxpas-cli] personal pol.: allow_cloud_for_personal_data  = "
              f"{bool(policies['allow_cloud_for_personal_data'])}")
        print(f"[woxpas-cli] sensitive pol: allow_cloud_for_sensitive_data = "
              f"{bool(policies['allow_cloud_for_sensitive_data'])}")
        print()

        try:
            await assert_cloud_allowed(
                pool,
                tenant_id=tenant_id,
                processing_profile=args.profile,
                sensitivity_class=args.sensitivity,
                backend=backend,
            )
            print("[woxpas-cli] result       : ALLOWED — gate passed, real call would proceed")
            outcome = "allowed"
        except CloudPolicyDenied as e:
            print(f"[woxpas-cli] result       : DENIED ({e.gate}) — {e}")
            outcome = "denied"

        # Show the most recent cloud-egress audit row for this tenant.
        async with pool.acquire() as conn:
            audit = await conn.fetchrow(
                """
                SELECT audit_id, occurred_at, outcome, metadata
                  FROM audit_log
                 WHERE tenant_id = $1 AND action = 'llm.cloud_egress'
                 ORDER BY occurred_at DESC
                 LIMIT 1
                """,
                tenant_id,
            )
        if audit is not None:
            print()
            print(f"[woxpas-cli] latest audit : id={audit['audit_id']} "
                  f"at={audit['occurred_at']} outcome={audit['outcome']}")
            print(f"[woxpas-cli] metadata     : {audit['metadata']}")
        else:
            print()
            print("[woxpas-cli] no cloud-egress audit rows for this tenant yet")
        return 0 if outcome == "allowed" or args.expect == "denied" else 1
    finally:
        await pool.close()


# ─── recall ────────────────────────────────────────────────────────────────

async def cmd_recall(args: argparse.Namespace) -> int:
    """Run a retrieval query through the local engine. Useful for ops +
    testing without spinning up the API.

    Pretty-prints the result for terminal consumption. --json emits the raw
    RetrievalResult for scripting.
    """
    from uuid import UUID

    from woxpas_retrieval import (
        Filters,
        Intent,
        RetrievalEngine,
        RetrievalRequest,
        SensitivityFilter,
        TimeFilter,
    )
    from woxpas_storage import StorageSettings

    from woxpas_pipeline.context import PipelineContext
    from woxpas_pipeline.settings import (
        ClamAVSettings,
        DevDekSettings,
        PipelineSettings,
    )

    tenant_id = UUID(args.tenant_id) if args.tenant_id else \
        UUID(os.environ["WOXPAS_DEV_TENANT_ID_DEFAULT"])

    ctx = await PipelineContext.construct(
        storage=StorageSettings(), pipeline=PipelineSettings(),
        clamav=ClamAVSettings(), dev=DevDekSettings(),
    )
    try:
        embedder = await ctx.embedder()
        reranker = await ctx.reranker()
        intent_llm = None
        if args.use_llm_classifier:
            try:
                intent_llm = await ctx.llm_backend("summary")
            except Exception:
                pass

        engine = RetrievalEngine(
            pool=ctx.pool, qdrant=ctx.qdrant,
            embedder=embedder, reranker=reranker, intent_llm=intent_llm,
        )

        # Compose the request from CLI flags.
        intent_hint = Intent(args.intent) if args.intent else Intent.AUTO
        sensitivity = SensitivityFilter(args.sensitivity) if args.sensitivity \
                      else SensitivityFilter.ANY
        time_filter = TimeFilter(relative=args.time) if args.time else None

        req = RetrievalRequest(
            query_text  = args.query,
            intent_hint = intent_hint,
            limit       = args.limit,
            rerank      = not args.no_rerank,
            graph_depth = args.graph_depth,
            filters     = Filters(
                time         = time_filter,
                sensitivity  = sensitivity,
                scope_slugs  = args.scope or [],
                processing_profiles = args.profile or [],
            ),
        )

        result = await engine.recall(req, tenant_id=tenant_id)

        if args.json:
            print(result.model_dump_json(indent=2))
            return 0

        # Pretty terminal output
        print(f"\n[woxpas-cli] tenant      : {tenant_id}")
        print(f"[woxpas-cli] query       : {args.query!r}")
        print(f"[woxpas-cli] intent      : {result.intent_used.value}"
              + ("  (uncertain)" if result.intent_uncertain else ""))
        print(f"[woxpas-cli] shape       : {result.shape.value}")
        ms = result.metrics.get("total_ms")
        print(f"[woxpas-cli] latency     : {ms}ms total"
              + (f"  (vector {result.metrics.get('vector_search_ms')}ms"
                 f" + rerank {result.metrics.get('rerank_ms')}ms)"
                 if result.metrics.get("vector_search_ms") else ""))

        # Body — depends on shape
        if result.items:
            print(f"\n[woxpas-cli] {len(result.items)} item(s):")
            for i, it in enumerate(result.items[:10], 1):
                kind = it.kind.upper()
                preview = (it.snippet or it.title or it.summary or "")[:75]
                score = f"  (score={it.score:.2f})" if it.score is not None else ""
                tags = []
                if it.provenance:
                    if it.provenance.open_conflicts:
                        tags.append(f"⚠ {len(it.provenance.open_conflicts)} open conflict")
                    if it.provenance.resolutions:
                        tags.append(f"⟳ {len(it.provenance.resolutions)} resolved")
                tag_str = "  " + " ".join(tags) if tags else ""
                print(f"  [{i:2d}] {kind:6s} {preview}{score}{tag_str}")
            if len(result.items) > 10:
                print(f"  ... +{len(result.items) - 10} more")

        if result.card:
            c = result.card
            print(f"\n[woxpas-cli] card        : {c.canonical_name} ({c.type})")
            print(f"[woxpas-cli] mention_count: {c.mention_count}")
            print(f"[woxpas-cli] counts      : {c.counts}")
            if c.recent_relations:
                print(f"\n             Recent relations ({len(c.recent_relations)}):")
                for r in c.recent_relations[:5]:
                    print(f"               • {(r.snippet or '')[:75]}")
            if c.recent_ideas:
                print(f"             Recent ideas ({len(c.recent_ideas)}):")
                for r in c.recent_ideas[:5]:
                    print(f"               • {r.title or (r.summary or '')[:75]}")
            if c.recent_tasks:
                print(f"             Recent tasks ({len(c.recent_tasks)}):")
                for r in c.recent_tasks[:5]:
                    print(f"               • {r.title} (status={r.metadata.get('status')})")
            if c.profile_facts:
                print(f"             Profile facts ({len(c.profile_facts)}):")
                for f in c.profile_facts[:8]:
                    val = f["value"].get("text") if isinstance(f["value"], dict) else f["value"]
                    print(f"               · {f['category']}/{f['kind']:15s} = {val}")

        if result.detail:
            d = result.detail
            print(f"\n[woxpas-cli] file        : {d.title or d.filename}")
            print(f"[woxpas-cli] sensitivity : {d.sensitivity_class}")
            print(f"[woxpas-cli] profile     : {d.processing_profile}")
            print(f"[woxpas-cli] topics      : {', '.join(d.topics) if d.topics else '(none)'}")
            if d.summary:
                print(f"\n             Summary:\n               {d.summary}")
            if d.chunks_preview:
                print(f"\n             Chunks ({len(d.chunks_preview)}):")
                for ch in d.chunks_preview[:3]:
                    print(f"               • {(ch.snippet or '')[:75]}")

        if result.graph:
            g = result.graph
            print(f"\n[woxpas-cli] graph       : {len(g.nodes)} nodes / {len(g.edges)} edges")
            print(f"[woxpas-cli] depth       : {g.depth}  found={g.found}")
            if g.target_entity_id:
                print(f"[woxpas-cli] from→to    : {g.root_entity_id} → {g.target_entity_id}")
            if g.paths:
                print(f"\n             Paths ({len(g.paths)}):")
                node_lookup = {n.entity_id: n.canonical_name for n in g.nodes}
                for p in g.paths[:3]:
                    chain = " → ".join(node_lookup.get(nid, str(nid)[:8]) for nid in p.node_ids)
                    print(f"               • {chain}  (length={p.length})")
            if g.truncated_at_level:
                print(f"             ⚠ truncated at level: {g.truncated_at_level}")

        if result.hint:
            print(f"\n[woxpas-cli] hint        : {result.hint.message}")
            if result.hint.suggested_action:
                print(f"             suggested action: {result.hint.suggested_action}")
            if result.hint.primary_doc_ids:
                print(f"             primary docs: {[str(d)[:8] for d in result.hint.primary_doc_ids]}")
        return 0
    finally:
        await ctx.close()


# ─── ask (synthesis CLI showcase) ─────────────────────────────────────────

async def cmd_ask(args: argparse.Namespace) -> int:
    """Streamed citation-bound synthesis through the local engine.

    Direct path — does not require the API daemon to be running. Mirrors
    the /ask endpoint's defaults (lean envelope, post-stream verification).
    Output goes to stdout as the LLM streams; sources panel + verification
    summary print at the end.

    --json emits the full SynthesizedAnswer as JSON (for scripting).
    --no-color disables ANSI codes (auto-disabled when stdout isn't a TTY).
    """
    import json as _json
    import sys as _sys
    from uuid import UUID as _UUID

    from woxpas_retrieval import RetrievalEngine
    from woxpas_runtime import MODEL_CATALOG, build_llm_backend
    from woxpas_storage import StorageSettings
    from woxpas_synthesis import (
        AnswerEvent,
        CitationVerifier,
        ContinuationEvent,
        DeltaEvent,
        DoneEvent,
        ErrorEvent,
        SourcesEvent,
        SynthesisEngine,
        SynthesisRequest,
        VerifyEvent,
        render_terminal_with_ansi,
    )

    from woxpas_pipeline.context import PipelineContext
    from woxpas_pipeline.settings import (
        ClamAVSettings,
        DevDekSettings,
        PipelineSettings,
    )

    # ─── Input validation: fail fast with friendly errors ──────────────
    # Empty query — no point spinning up retrieval + LLM for nothing.
    if not args.query or not args.query.strip():
        print("error: query must not be empty.", file=_sys.stderr)
        return 2

    # Tenant resolution — explicit flag wins; otherwise env. Either way,
    # show a usable message instead of a KeyError.
    raw_tenant = args.tenant_id or os.environ.get("WOXPAS_DEV_TENANT_ID_DEFAULT")
    if not raw_tenant:
        print("error: tenant id missing. Pass --tenant-id <uuid> OR set "
              "WOXPAS_DEV_TENANT_ID_DEFAULT env var.", file=_sys.stderr)
        return 2
    try:
        tenant_id = _UUID(raw_tenant)
    except ValueError as e:
        print(f"error: invalid tenant id {raw_tenant!r}: {e}", file=_sys.stderr)
        return 2

    # Pinned chunk ids — surface bad UUIDs cleanly (not as a traceback).
    pinned: list[_UUID] = []
    for raw in (args.pinned_chunk_id or []):
        try:
            pinned.append(_UUID(raw))
        except ValueError:
            print(f"error: --pinned-chunk-id {raw!r} is not a valid UUID.",
                  file=_sys.stderr)
            return 2

    # Pinned-only sanity: empty pin set + pinned_only=True means "skip
    # retrieval AND have no sources" → LLM will say "I don't have info".
    # Warn the user; this is almost always a misconfigured agent, not
    # the intended behavior.
    if args.pinned_only and not pinned:
        print("warning: --pinned-only set with no --pinned-chunk-id values; "
              "no sources will be available to the model.", file=_sys.stderr)

    # Model existence — catch typos before the engine starts. Cloud
    # backends without an API key still fail at .generate() time (we let
    # that happen since the key check is per-call), but typos like
    # 'deepsek-chat' get caught here instead of mid-stream.
    if args.model and args.model not in MODEL_CATALOG:
        valid = sorted(MODEL_CATALOG.keys())
        print(f"error: unknown model {args.model!r}.", file=_sys.stderr)
        print(f"  known models: {', '.join(valid)}", file=_sys.stderr)
        print("  tip: 'woxpas-cli prefetch-models --list-only' shows model sizes.",
              file=_sys.stderr)
        return 2

    # TTY-aware color: respect --no-color, else auto-detect
    use_color = (not args.no_color) and _sys.stdout.isatty()
    DIM    = "\x1b[2m"  if use_color else ""
    BOLD   = "\x1b[1m"  if use_color else ""
    YELLOW = "\x1b[33m" if use_color else ""
    RED    = "\x1b[31m" if use_color else ""
    GREEN  = "\x1b[32m" if use_color else ""
    RESET  = "\x1b[0m"  if use_color else ""

    ctx = await PipelineContext.construct(
        storage=StorageSettings(), pipeline=PipelineSettings(),
        clamav=ClamAVSettings(), dev=DevDekSettings(),
    )
    try:
        embedder = await ctx.embedder()
        reranker = await ctx.reranker()
        try:
            intent_llm = await ctx.llm_backend("summary")
        except Exception:
            intent_llm = None

        retrieval = RetrievalEngine(
            pool=ctx.pool, qdrant=ctx.qdrant,
            embedder=embedder, reranker=reranker,
            intent_llm=intent_llm,
        )
        verifier = CitationVerifier(embedder)

        # Same caching pattern as the API: keep a per-process backend
        # cache by name so re-using the default model doesn't re-load
        # weights.
        _backend_cache = {intent_llm.name: intent_llm} if intent_llm else {}

        def llm_factory(name: str | None) -> Any:
            if name is None and intent_llm is not None:
                return intent_llm
            resolved = name or "qwen2.5-3b-instruct-q4km"
            if resolved in _backend_cache:
                return _backend_cache[resolved]
            backend = build_llm_backend(resolved)
            _backend_cache[resolved] = backend
            return backend

        engine = SynthesisEngine(
            retrieval=retrieval,
            llm_factory=llm_factory,
            pool=ctx.pool,
            verifier=verifier,
        )

        req = SynthesisRequest(
            query_text         = args.query,
            max_sources        = args.max_sources,
            model_name         = args.model,
            max_tokens         = args.max_tokens,
            persona            = args.persona,
            pinned_chunk_ids   = pinned,
            pinned_only        = args.pinned_only,
            include_chunk_text = args.include_chunk_text,
        )

        # ─── Stream + render ──────────────────────────────────────────────
        if not args.json:
            print(f"{DIM}[woxpas-cli] tenant : {tenant_id}{RESET}")
            print(f"{DIM}[woxpas-cli] query  : {args.query!r}{RESET}")
            print(f"{DIM}[woxpas-cli] model  : {args.model or '(default)'}{RESET}")
            print()

        final_answer = None
        sources_evt = None
        had_error: ErrorEvent | None = None

        # Streaming-tag substitution: as deltas arrive, citation tags are
        # often split across multiple tokens (LLM emits `<c`, ` id`, `=\"`,
        # `1`, etc. as separate tokens). We buffer deltas, replace any
        # COMPLETE `<c id="N"/>` tag with `[N]` for terminal cleanliness,
        # and hold back any INCOMPLETE trailing tag to print on the next
        # delta. Result: user sees clean `[N]` markers in real time without
        # the raw XML noise.
        import re as _re_cli
        _tag_complete = _re_cli.compile(r'<c\s+id\s*=\s*[\"\']?(\d+)[\"\']?\s*/>')
        # Hold back ANY trailing `<` followed by characters that don't close
        # the tag — covers bare `<`, `<c`, `<c id="3"/`, etc. This is wider
        # than the engine's strip regex (which is XML-aware) because here
        # we're protecting the visual stream from emitting a partial tag.
        _tag_incomplete_tail = _re_cli.compile(r'<[^>]*$', _re_cli.IGNORECASE)
        _buf: list[str] = [""]   # mutable container so the helper can update

        def _emit(delta_text: str) -> None:
            _buf[0] += delta_text
            cleaned = _tag_complete.sub(lambda m: f"[{int(m.group(1))}]", _buf[0])
            m = _tag_incomplete_tail.search(cleaned)
            if m:
                safe = cleaned[:m.start()]
                hold = cleaned[m.start():]
            else:
                safe = cleaned
                hold = ""
            if safe:
                print(safe, end="", flush=True)
            _buf[0] = hold

        async for ev in engine.synthesize_stream(req, tenant_id=tenant_id):
            if isinstance(ev, SourcesEvent):
                sources_evt = ev
                if not args.json:
                    n = ev.chunks_used
                    print(f"{DIM}↳ {n} source{'s' if n != 1 else ''} retrieved (intent={ev.retrieval_intent}){RESET}\n")
            elif isinstance(ev, DeltaEvent):
                if not args.json:
                    _emit(ev.text)
            elif isinstance(ev, ContinuationEvent):
                if not args.json:
                    # Subtle "..." indicator that a continuation kicked in.
                    print(f"{DIM} …{RESET}", end="", flush=True)
            elif isinstance(ev, DoneEvent):
                if not args.json:
                    # Flush any held-back partial tag (model truncated mid-tag)
                    if _buf[0]:
                        print(_buf[0], end="", flush=True)
                        _buf[0] = ""
                    print()  # newline after streaming text
                    extra = (f"  ({ev.continuation_count} continuation"
                             f"{'s' if ev.continuation_count != 1 else ''})") \
                            if ev.continuation_count else ""
                    print(f"\n{DIM}[stream done] tokens={ev.usage.total_tokens}{extra}{RESET}")
            elif isinstance(ev, AnswerEvent):
                final_answer = ev.answer
            elif isinstance(ev, VerifyEvent):
                # Verification arrives post-answer; fold updates into the
                # answer so render_terminal_with_ansi shows the right
                # status colors in the sources panel.
                if final_answer:
                    for cid_str, upd in ev.citation_updates.items():
                        cid = int(cid_str) if isinstance(cid_str, str) else cid_str
                        if cid in final_answer.citations:
                            c = final_answer.citations[cid]
                            c.status = upd["status"]
                            c.cosine = upd.get("cosine")
                            c.reason = upd.get("reason")
            elif isinstance(ev, ErrorEvent):
                had_error = ev

        # ─── JSON output ──────────────────────────────────────────────────
        if args.json:
            from dataclasses import asdict, is_dataclass
            from datetime import datetime as _dt
            from uuid import UUID as _UUID_LOCAL

            def _safe(v: Any) -> Any:
                """Recursive coercion: dataclasses → dict, UUIDs/datetimes
                → str, sets → list. Falls through to str() for unknown
                types so json.dumps never raises."""
                if v is None or isinstance(v, (str, bool, int, float)):
                    return v
                if isinstance(v, _UUID_LOCAL):
                    return str(v)
                if isinstance(v, _dt):
                    return v.isoformat()
                if is_dataclass(v) and not isinstance(v, type):
                    return _safe(asdict(v))
                if isinstance(v, dict):
                    return {str(k): _safe(x) for k, x in v.items()}
                if isinstance(v, (list, tuple, set)):
                    return [_safe(x) for x in v]
                return str(v)

            payload = {
                "answer":       _safe(final_answer),
                "error":        _safe(had_error),
                "sources_used": sources_evt.chunks_used if sources_evt else 0,
            }
            print(_json.dumps(payload, indent=2, ensure_ascii=False))
            return 1 if had_error else 0

        # ─── Pretty terminal output ───────────────────────────────────────
        if had_error:
            print(f"\n{RED}[error] {had_error.error_class}: "
                  f"{had_error.error_message}{RESET}")
            print(f"{DIM}retriable={had_error.retriable}{RESET}")
            return 1

        if final_answer:
            # Replace the streamed plain prose with the colored render
            # that includes references panel + status tags.
            print()
            rendered = render_terminal_with_ansi(
                final_answer, use_color=use_color, show_references=True,
            )
            # The streamed prose was already shown; we render the SOURCES
            # panel only here (avoid printing the answer twice).
            # Quick parse: split on the "Sources:" anchor render() emits.
            if "\nSources:" in rendered:
                _, refs = rendered.split("\nSources:", 1)
                print(f"{BOLD}Sources:{RESET}{refs}")
            else:
                # No references (e.g., no citations) — nothing extra to print
                pass

            # Status summary
            n_claims = len(final_answer.claims)
            n_verified = sum(1 for c in final_answer.claims if c.status == "verified")
            n_weak     = sum(1 for c in final_answer.claims if c.status == "weak")
            n_unver    = sum(1 for c in final_answer.claims if c.status in ("unverified", "fabricated", "uncited"))
            color = GREEN if n_verified == n_claims else (YELLOW if n_unver == 0 else RED)
            summary = f"{color}{n_verified}/{n_claims} claims verified{RESET}"
            if n_weak:
                summary += f"{DIM}, {n_weak} weak (semantic only){RESET}"
            if n_unver:
                summary += f"{RED}, {n_unver} ungrounded{RESET}"
            print(f"\n{summary}")

            if final_answer.truncated_after_continuations:
                print(f"{YELLOW}⚠ answer was truncated even after "
                      f"{final_answer.continuation_count} continuations — "
                      f"raise --max-tokens for a fuller response{RESET}")

        return 0
    finally:
        await ctx.close()


# ─── unlock ─────────────────────────────────────────────────────────────────

async def cmd_unlock(args: argparse.Namespace) -> int:
    """Start the boot daemon (woxpas-unlock) as a background subprocess.

    The daemon prompts for the passphrase on the controlling TTY, derives the KEK,
    unwraps the DEK, and serves it over a Unix socket. Once the daemon is running,
    the worker / API / MCP server will pick up the DEK from the socket instead of
    WOXPAS_DEV_DEK_HEX.
    """
    import shlex
    import subprocess as _subprocess

    cmd_parts = [sys.executable, "-m", "woxpas_boot.daemon"]
    if args.tenant_id:
        cmd_parts.extend(["--tenant-id", args.tenant_id])
    if args.socket:
        cmd_parts.extend(["--socket", args.socket])
    if args.idle_lock_sec:
        cmd_parts.extend(["--idle-lock-sec", str(args.idle_lock_sec)])
    if args.foreground:
        cmd_parts.append("--foreground")

    print(f"[woxpas-cli] starting: {' '.join(map(shlex.quote, cmd_parts))}", file=sys.stderr)

    # Daemonize: spawn and detach so it survives the CLI process.
    # All args are built from typed CLI parameters + sys.executable — no
    # untrusted input reaches the subprocess boundary.
    proc = _subprocess.Popen(
        cmd_parts,
        stdin=sys.stdin,   # passphrase prompt needs TTY
        stdout=_subprocess.DEVNULL,
        stderr=_subprocess.DEVNULL,
        start_new_session=True,  # detach from our process group
    )
    print(f"[woxpas-cli] daemon started (pid={proc.pid}). "
          f"Listening on {args.socket or '~/.woxpas/dek.sock'}", file=sys.stderr)
    return 0


# ─── lock ───────────────────────────────────────────────────────────────────

async def cmd_lock(args: argparse.Namespace) -> int:
    """Tell a running unlock daemon to zeroize and shut down."""
    from pathlib import Path

    from woxpas_boot.client import send_lock

    socket_path = args.socket or str(Path.home() / ".woxpas" / "dek.sock")
    result = await send_lock(socket_path=socket_path)
    status = result.get("status", "unknown")
    if status == "locked":
        print(f"[woxpas-cli] daemon locked and shut down ({socket_path})", file=sys.stderr)
        return 0
    elif status == "not_running":
        print(f"[woxpas-cli] no daemon running at {socket_path}", file=sys.stderr)
        return 0
    else:
        msg = result.get("message", "unknown error")
        print(f"[woxpas-cli] lock failed: {msg}", file=sys.stderr)
        return 1


# ─── serve-mcp ─────────────────────────────────────────────────────────────

async def cmd_serve_mcp(args: argparse.Namespace) -> int:
    """Start the MCP server in HTTP/SSE mode for remote access."""
    import subprocess as _sp

    tenant_id = args.tenant_id or os.environ.get("WOXPAS_DEV_TENANT_ID_DEFAULT")
    if not tenant_id:
        print("[woxpas-cli] ERROR: --tenant-id is required "
              "(or set WOXPAS_DEV_TENANT_ID_DEFAULT)", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env["WOXPAS_MCP_TENANT_ID"] = tenant_id
    env["WOXPAS_MCP_TRANSPORT"] = "http"
    env["WOXPAS_MCP_HTTP_HOST"] = args.host
    env["WOXPAS_MCP_HTTP_PORT"] = str(args.port)
    env["WOXPAS_MCP_HTTP_MODE"] = args.http_mode

    print("[woxpas-cli] Starting MCP server in HTTP mode...", file=sys.stderr)
    print(f"  HTTP mode:  {args.http_mode} (MCP {'2025-03-26+' if args.http_mode == 'streamable' else '2024-11-05'})", file=sys.stderr)
    print(f"  Tenant:     {tenant_id}", file=sys.stderr)
    print(f"  Listening:  {args.host}:{args.port}", file=sys.stderr)
    print("  Endpoints:  GET /sse  |  POST /messages", file=sys.stderr)
    print("", file=sys.stderr)
    print("  For remote access, expose with a tunnel:", file=sys.stderr)
    print(f"    tailscale funnel {args.port}", file=sys.stderr)
    print(f"    cloudflared tunnel --url http://127.0.0.1:{args.port}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Connect any MCP client to:", file=sys.stderr)
    print("    https://<your-tunnel>/sse", file=sys.stderr)
    print("", file=sys.stderr)
    print("  Press Ctrl-C to stop.", file=sys.stderr)

    _sp.run([sys.executable, "-m", "woxpas_mcp.main"], env=env, check=True)
    return 0


# ─── provision-tenant ─────────────────────────────────────────────────────

async def cmd_provision_tenant(args: argparse.Namespace) -> int:
    """Create a tenant for cloud/hosted Woxpas deployments."""
    passphrase = _read_passphrase(prompt="Passphrase: ", confirm=True)
    if not passphrase:
        raise SystemExit("passphrase required")
    tenant_id = UUID(args.tenant_id) if args.tenant_id else uuid4()
    secrets = init_tenant_secrets(passphrase)

    settings = StorageSettings()
    pool = await PostgresPool.from_settings(settings.postgres)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id),
                )
                await conn.execute(
                    """INSERT INTO tenants (tenant_id, display_name,
                       passphrase_verifier, kdf_params, encrypted_dek,
                       encrypted_dek_iv, encrypted_dek_tag, dek_version)
                       VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8)""",
                    tenant_id, args.display_name,
                    secrets.passphrase_verifier, secrets.kdf_params,
                    secrets.wrapped_dek.encrypted_dek,
                    secrets.wrapped_dek.iv,
                    secrets.wrapped_dek.tag,
                    secrets.wrapped_dek.dek_version,
                )
                self_id = uuid4()
                await conn.execute(
                    """INSERT INTO entities (entity_id, tenant_id, canonical_name,
                       type, profile, first_mentioned_at)
                       VALUES ($1, $2, $3, 'person', $4::jsonb, now())""",
                    self_id, tenant_id, args.display_name,
                    {"is_self": True, "seeded_at_provision": True},
                )
                await conn.execute(
                    "UPDATE tenants SET self_entity_id = $2 WHERE tenant_id = $1",
                    tenant_id, self_id,
                )
    finally:
        await pool.close()

    print("\n[woxpas-cli] tenant provisioned.\n")
    print(f"  tenant_id: {tenant_id}")
    print(f"  display:   {args.display_name}")
    print("\n  ┌──────────────────────────────────────────────────────────┐")
    print(f"  │  DEK: {secrets.dek.hex()}  │")
    print("  └──────────────────────────────────────────────────────────┘")
    print("\n  Store this DEK securely — it will NOT be shown again.")
    print("\n  Path A (local first, sovereign):")
    print("    User sends tenant_id to your product.")
    print("  Path B (product first):")
    print("    Give tenant_id + DEK to user. User runs:")
    print(f"    woxpas-cli link-tenant --tenant-id {tenant_id} --dek-hex <dek>")

    secrets.dek = b"\x00" * len(secrets.dek)
    return 0


# ─── export-tenant ─────────────────────────────────────────────────────────

async def cmd_export_tenant(args: argparse.Namespace) -> int:
    """Export all data for a tenant to a portable NDJSON file.

    Reads every tenant-scoped table and writes one JSON line per row. The
    resulting file can be imported into another Woxpas instance via
    import-tenant. Used for cloud→local sync (Path B → A transition).
    """
    import json as _json
    from datetime import datetime

    settings = StorageSettings()
    tenant_id = UUID(args.tenant_id)
    pool = await PostgresPool.from_settings(settings.postgres)

    # Tables to export (all tenant-scoped tables in dependency order).
    tables = [
        "tenants", "entity_profile_facts", "entities", "relations",
        "ideas", "tasks", "conflicts", "sources", "scopes",
        "documents", "canonical_blocks", "chunks", "spans",
        "doc_scope_memberships", "pipeline_runs", "timeline_entries",
        "outbox", "audit_log", "resurface_observations",
        "resurface_patterns_cache", "persona_tone", "prepared_actions",
        "predicted_questions", "mcp_api_tokens",
    ]

    output_path = args.output
    exported = 0
    with open(output_path, "w") as f:
        # Header
        header = _json.dumps({
            "format": "woxpas-tenant-export",
            "version": "0.1.0",
            "tenant_id": str(tenant_id),
            "exported_at": datetime.now(UTC).isoformat(),
        })
        f.write(header + "\n")

        for table in tables:
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        f"SELECT * FROM {table} WHERE tenant_id = $1", tenant_id,
                    )
            except Exception:
                continue  # table might not exist yet (e.g., mcp_api_tokens)

            for row in rows:
                record = {"_table": table}
                for key, val in dict(row).items():
                    record[key] = _serialize_val(val)
                f.write(_json.dumps(record, default=str) + "\n")
                exported += 1

    await pool.close()
    print(f"\n[woxpas-cli] exported {exported} rows from {len(tables)} tables")
    print(f"  File: {output_path}")
    print(f"  Import with: woxpas-cli import-tenant --from {output_path}")
    return 0


# ─── import-tenant ─────────────────────────────────────────────────────────

async def cmd_import_tenant(args: argparse.Namespace) -> int:
    """Import a tenant export file into this Woxpas instance.

    Replays an NDJSON export produced by export-tenant. The target tenant
    must already exist (via bootstrap or link-tenant). Rows are inserted
    in dependency order — tables with foreign keys are loaded after their
    parents.
    """
    import json as _json

    settings = StorageSettings()
    tenant_id = UUID(args.tenant_id)
    pool = await PostgresPool.from_settings(settings.postgres)

    imported = 0
    with open(args.from_path) as f:
        header_line = f.readline()
        header = _json.loads(header_line)
        if header.get("format") != "woxpas-tenant-export":
            raise SystemExit(f"unrecognized export format: {header.get('format')}")

        table_rows: dict[str, list[dict[str, Any]]] = {}
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = _json.loads(line)
            table = record.pop("_table")
            table_rows.setdefault(table, []).append(record)

    # Insert in dependency order: tenants first, then FK children
    insert_order = [
        "tenants", "entity_profile_facts", "entities", "relations",
        "ideas", "tasks", "conflicts", "sources", "scopes",
        "documents", "canonical_blocks", "chunks", "spans",
        "doc_scope_memberships", "pipeline_runs", "timeline_entries",
        "outbox", "audit_log", "resurface_observations",
        "resurface_patterns_cache", "persona_tone", "prepared_actions",
        "predicted_questions", "mcp_api_tokens",
    ]

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Set RLS context for the target tenant
            await conn.execute(
                "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id),
            )

            for table in insert_order:
                rows = table_rows.get(table, [])
                for record in rows:
                    record["tenant_id"] = str(tenant_id)
                    columns = list(record.keys())
                    values = list(record.values())
                    placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
                    cols_sql = ", ".join(columns)
                    try:
                        await conn.execute(
                            f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders}) "
                            "ON CONFLICT DO NOTHING",
                            *values,
                        )
                        imported += 1
                    except Exception as e:
                        print(f"  skipping {table} row: {e}", file=sys.stderr)

    await pool.close()
    print(f"\n[woxpas-cli] imported {imported} rows")
    return 0


# ─── link-tenant ──────────────────────────────────────────────────────────

async def cmd_link_tenant(args: argparse.Namespace) -> int:
    """Link local Woxpas to a cloud-provisioned tenant (Path B)."""
    from pathlib import Path
    env_path = Path(".env")
    env = _read_env(str(env_path))
    env["WOXPAS_DEV_TENANT_ID_DEFAULT"] = args.tenant_id
    env["WOXPAS_DEV_DEK_HEX"] = args.dek_hex
    env["API_AUTH_MODE"] = "dev"
    _write_env(str(env_path), env)

    print(f"\n[woxpas-cli] linked to tenant {args.tenant_id}")
    print("  ✓ WOXPAS_DEV_TENANT_ID_DEFAULT + WOXPAS_DEV_DEK_HEX written to .env")
    print("\n  Security note (Path B): the product knows your DEK.")
    print(f"  To reclaim sovereignty later: woxpas-cli reclaim-tenant --tenant-id {args.tenant_id}")
    return 0


# ─── reclaim-tenant ────────────────────────────────────────────────────────

async def cmd_reclaim_tenant(args: argparse.Namespace) -> int:
    """Path B → Path A: rotate the DEK under a new user-chosen passphrase.

    After this, the old DEK (known to the product) stops working. The tenant
    UUID and all stored data remain intact — only the encryption key changes.
    The user now has sovereign control.
    """
    from pathlib import Path
    settings = StorageSettings()
    tenant_id = UUID(args.tenant_id)

    print("\n[woxpas-cli] You are taking sovereign ownership of this tenant.")
    print("  This rotates the encryption key. The old DEK will stop working.")
    print("  No data is lost — everything is re-encrypted transparently.\n")
    new_passphrase = _read_passphrase(prompt="New passphrase: ", confirm=True)
    if not new_passphrase:
        raise SystemExit("passphrase required")

    secrets = init_tenant_secrets(new_passphrase)

    pool = await PostgresPool.from_settings(settings.postgres)
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id),
                )
                result = await conn.execute(
                    """UPDATE tenants SET passphrase_verifier = $2, kdf_params = $3::jsonb,
                       encrypted_dek = $4, encrypted_dek_iv = $5, encrypted_dek_tag = $6,
                       dek_version = dek_version + 1
                       WHERE tenant_id = $1""",
                    tenant_id,
                    secrets.passphrase_verifier, secrets.kdf_params,
                    secrets.wrapped_dek.encrypted_dek,
                    secrets.wrapped_dek.iv,
                    secrets.wrapped_dek.tag,
                )
                if result == "UPDATE 0":
                    raise SystemExit(f"tenant {tenant_id} not found")
    finally:
        await pool.close()

    dek_hex = secrets.dek.hex()
    env_path = Path(".env")
    env = _read_env(str(env_path))
    env["WOXPAS_DEV_TENANT_ID_DEFAULT"] = str(tenant_id)
    env["WOXPAS_DEV_DEK_HEX"] = dek_hex
    _write_env(str(env_path), env)

    print("\n[woxpas-cli] sovereignty reclaimed. Path A active.\n")
    print(f"  tenant_id: {tenant_id}")
    print("  ✓ DEK rotated — old key invalidated")
    print("  ✓ New DEK written to .env")
    print("\n  The product can no longer access your data.")
    print("  Your memory, your key. Path A from here on.")

    secrets.dek = b"\x00" * len(secrets.dek)
    return 0


# ─── argparse wiring ───────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="woxpas-cli", description="Woxpas bootstrap CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    bs = sub.add_parser("bootstrap",
                        help="Single-command setup from fresh clone to running stack "
                             "(prerequisites → infra → tenant → models → ready)")
    bs.add_argument("--display-name", default=None,
                    help="Your name (prompts if omitted)")
    bs.add_argument("--tenant-id", default=None,
                    help="Override the generated tenant id (must be a UUID)")
    bs.add_argument("--new-tenant", action="store_true",
                    help="Force creating a new tenant even if one already exists")
    bs.add_argument("--yes", "-y", action="store_true",
                    help="Skip confirmation prompts (for scripted installs)")
    bs.set_defaults(func=cmd_bootstrap)

    up = sub.add_parser("up",
                        help="Start everything: data services (Docker) + the worker, "
                             "API, and dreaming worker (one command, no extra terminals)")
    up.add_argument("--no-dreaming", action="store_true",
                    help="Don't start the optional background dreaming worker")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down",
                          help="Stop the app processes started by `up` "
                               "(data services stay up unless --docker)")
    down.add_argument("--docker", action="store_true",
                      help="Also stop the Docker data services")
    down.set_defaults(func=cmd_down)

    ienv = sub.add_parser("init-env",
                          help="Create .env with generated infra secrets "
                               "(for a no-uv Docker-only install, via `docker compose run cli`)")
    ienv.set_defaults(func=cmd_init_env)

    icd = sub.add_parser("install-claude-desktop",
                         help="Add the Woxpas MCP server to Claude Desktop's config "
                              "(merges safely; backs up any existing file)")
    icd.add_argument("--path", default=None,
                     help="Write to this config file instead of the OS default")
    icd.add_argument("--print", action="store_true",
                     help="Only print the JSON block; don't modify any file")
    icd.add_argument("--docker", action="store_true",
                     help="Run the MCP server in a container (docker compose run) "
                          "instead of on the host via uv — no host Python/uv needed")
    icd.set_defaults(func=cmd_install_claude_desktop)

    init = sub.add_parser("init-tenant",
                          help="Create a new tenant + default file source + self-entity")
    init.add_argument("--display-name", required=True)
    init.add_argument("--tenant-id", default=None,
                      help="Override the generated tenant id (must be a UUID)")
    init.add_argument("--with-inbox", action="store_true",
                      help="Seed a default 'Inbox' scope")
    init.add_argument("--identity-from", default=None, metavar="PATH",
                      help="Markdown file to seed the self-entity profile from. "
                           "Use 'woxpas-cli identity-template' to print the export prompt.")
    init.add_argument("--dev", action="store_true",
                      help="Print plaintext DEK hex for local development")
    init.set_defaults(func=cmd_init_tenant)

    idt = sub.add_parser("identity-template",
                         help="Print the identity-export prompt to stdout. "
                              "Paste it into your AI of choice; save the response; "
                              "import via init-tenant --identity-from.")
    idt.set_defaults(func=cmd_identity_template)

    src = sub.add_parser("create-source", help="Register a new source for a tenant")
    src.add_argument("--tenant-id", required=True)
    src.add_argument("--type", required=True,
                     choices=["file_upload", "voice_note", "mcp_inbound", "mcp_outbound",
                              "browser_extension", "slack", "email", "agent",
                              "calendar", "api_custom"])
    src.add_argument("--name", required=True)
    src.add_argument("--description", default=None)
    src.add_argument("--config", default=None,
                     help="JSON config blob for the source")
    src.set_defaults(func=cmd_create_source)

    scp = sub.add_parser("create-scope", help="Create a scope")
    scp.add_argument("--tenant-id", required=True)
    scp.add_argument("--name", required=True)
    scp.add_argument("--slug", default=None)
    scp.add_argument("--description", default=None)
    scp.add_argument("--parent", default=None,
                     help="Parent scope id (UUID), if hierarchical")
    scp.add_argument("--color", default=None)
    scp.add_argument("--icon", default=None)
    scp.add_argument("--no-embed", action="store_true",
                     help="Skip embedding the scope; it won't be eligible for "
                          "auto-assignment (use for catch-all scopes like Inbox).")
    scp.add_argument("--embed-model-name", default=None,
                     help="Override embedder model (default: runtime default; "
                          "see MODEL_CATALOG for valid names).")
    scp.set_defaults(func=cmd_create_scope)

    st = sub.add_parser("status", help="List tenants")
    st.set_defaults(func=cmd_status)

    pf = sub.add_parser("prefetch-models",
                        help="Download all default model artefacts into the local cache "
                             "(shows download budget + prompts before pulling bytes)")
    pf.add_argument("--with-extraction-model", action="store_true",
                    help="Also fetch Qwen2.5-3B Q4_K_M (~2GB). Improves Phase 2 "
                         "extraction quality (cleaner JSON, fewer dropped fields) "
                         "at the cost of ~2-3x slower extraction per chunk on CPU. "
                         "Run prefetch-models without this flag first to see the "
                         "trade-off explainer.")
    pf.add_argument("--list-only", action="store_true",
                    help="Just print the size budget; don't download anything")
    pf.add_argument("--yes", "-y", action="store_true",
                    help="Skip the confirmation prompt (for scripted installs)")
    pf.set_defaults(func=cmd_prefetch_models)

    ci = sub.add_parser("cache-info",
                        help="Inspect what's in the models cache vs. what's needed "
                             "for the configured workload")
    ci.add_argument("--live", action="store_true",
                    help="Query HuggingFace for expected sizes (one HTTP call per "
                         "artefact). Without this flag, only on-disk cache is shown.")
    ci.set_defaults(func=cmd_cache_info)

    insp = sub.add_parser("inspect-doc",
                          help="Print everything we know about a single document")
    insp.add_argument("doc_id")
    insp.add_argument("--tenant-id", default=None,
                      help="Tenant uuid (defaults to WOXPAS_DEV_TENANT_ID_DEFAULT env)")
    insp.add_argument("--with-blocks", action="store_true",
                      help="Also list canonical_blocks (ordinal, type, level, text snippet)")
    insp.add_argument("--with-raw", action="store_true",
                      help="Decrypt + print the original bytes from the raw bucket")
    insp.add_argument("--with-canonical", action="store_true",
                      help="Decrypt + print the CDM JSON v1 from the canonical bucket")
    insp.add_argument("--payload-limit", type=int, default=2000,
                      help="Max chars to print for raw/canonical payloads (default 2000)")
    insp.add_argument("--save-raw", default=None, metavar="PATH",
                      help="Save decrypted raw bytes to this path instead of printing")
    insp.add_argument("--save-canonical", default=None, metavar="PATH",
                      help="Save decrypted canonical JSON to this path instead of printing")
    insp.set_defaults(func=cmd_inspect_doc)

    scp_pol = sub.add_parser("set-cloud-policy",
                             help="Flip a tenant's cloud-policy switch on/off "
                                  "(--target personal | sensitive)")
    scp_pol.add_argument("--tenant-id", required=True)
    scp_pol.add_argument("--target", choices=["personal", "sensitive"], default="personal",
                         help="Which switch to flip: 'personal' = "
                              "allow_cloud_for_personal_data (default); "
                              "'sensitive' = allow_cloud_for_sensitive_data (stricter)")
    scp_pol.add_argument("value", choices=["true", "false", "on", "off", "yes", "no", "1", "0"],
                         help="New policy value")
    scp_pol.set_defaults(func=cmd_set_cloud_policy)

    ms = sub.add_parser("mark-sensitive",
                        help="Manually set a document's sensitivity_class "
                             "(overrides auto-tiers; appears in audit log)")
    ms.add_argument("--tenant-id", default=None,
                    help="Tenant uuid (defaults to WOXPAS_DEV_TENANT_ID_DEFAULT env)")
    ms.add_argument("--doc-id", required=True)
    ms.add_argument("--value", choices=["standard", "sensitive"], required=True)
    ms.add_argument("--reason", default=None,
                    help="Optional free-form reason (logged + persisted in sensitivity_reason)")
    ms.set_defaults(func=cmd_mark_sensitive)

    lt = sub.add_parser("llm-test",
                        help="Round-trip a prompt through one model "
                             "(use to verify cloud keys + backend dispatch)")
    lt.add_argument("--model", required=True,
                    help="Logical model name from MODEL_CATALOG (e.g., "
                         "qwen2.5-1.5b-instruct-q4km, deepseek-chat, claude-sonnet-4-6)")
    lt.add_argument("--prompt", default="In one sentence: what is sovereign personal memory?")
    lt.add_argument("--max-tokens", type=int, default=128)
    lt.add_argument("--timeout", type=float, default=60.0)
    lt.set_defaults(func=cmd_llm_test)

    rc = sub.add_parser("recall",
                        help="Run a retrieval query through the local engine "
                             "(useful for ops + testing without spinning up the API)")
    rc.add_argument("query", help="The natural-language query (or doc:<uuid> / entity:<uuid>)")
    rc.add_argument("--tenant-id", default=None,
                    help="Tenant uuid (defaults to WOXPAS_DEV_TENANT_ID_DEFAULT env)")
    rc.add_argument("--intent",
                    choices=["auto", "semantic", "timeline", "type_listing",
                             "file_lookup", "entity_card", "graph"],
                    help="Override the classifier's intent decision")
    rc.add_argument("--limit", type=int, default=10,
                    help="Max items to return (default: 10)")
    rc.add_argument("--time", default=None,
                    help="Time-window relative phrase: yesterday, last_week, "
                         "last_2_days, this_month, etc.")
    rc.add_argument("--sensitivity", choices=["any", "exclude_sensitive", "only_sensitive"],
                    help="Filter by document sensitivity_class")
    rc.add_argument("--scope", action="append",
                    help="Restrict to a scope slug (repeatable)")
    rc.add_argument("--profile", action="append",
                    help="Restrict to a processing_profile (repeatable)")
    rc.add_argument("--graph-depth", type=int, default=2,
                    help="Max hops for GRAPH intent (default: 2)")
    rc.add_argument("--no-rerank", action="store_true",
                    help="Skip the cross-encoder rerank pass (faster, less precise)")
    rc.add_argument("--use-llm-classifier", action="store_true",
                    help="Enable LLM fallback for low-confidence intent classification")
    rc.add_argument("--json", action="store_true",
                    help="Emit raw RetrievalResult JSON instead of pretty output")
    rc.set_defaults(func=cmd_recall)

    ask = sub.add_parser("ask",
                         help="Ask a citation-bound question. Streams the answer to "
                              "stdout, then shows sources + verification summary. "
                              "Mirrors POST /ask but works without the API daemon.")
    ask.add_argument("query", help="The question to answer")
    ask.add_argument("--tenant-id", default=None,
                     help="Tenant uuid (defaults to WOXPAS_DEV_TENANT_ID_DEFAULT env)")
    ask.add_argument("--model", default=None,
                     help="LLM model name (default: configured summary model). "
                          "Examples: deepseek-chat, claude-sonnet-4-6, "
                          "qwen2.5-3b-instruct-q4km, openai-o3-mini.")
    ask.add_argument("--max-sources", type=int, default=8,
                     help="Top-N retrieved chunks sent to the LLM (default: 8)")
    ask.add_argument("--max-tokens", type=int, default=1024,
                     help="LLM output budget per call. If exceeded, the engine "
                          "transparently continues for up to 3 iterations.")
    ask.add_argument("--persona", default=None,
                     help="Optional stylistic directive ('Be concise', 'Use bullet points'). "
                          "Sanitized + treated as advisory; can't override system rules.")
    ask.add_argument("--pinned-chunk-id", action="append", default=None, metavar="UUID",
                     help="Chunk_id to pin from a prior answer (preserves stable numeric "
                          "ids across follow-up turns). Repeatable.")
    ask.add_argument("--pinned-only", action="store_true",
                     help="Skip retrieval; only use --pinned-chunk-id sources. "
                          "Fast path for follow-up drill-downs.")
    ask.add_argument("--include-chunk-text", action="store_true",
                     help="Heavy envelope — include full chunk text in citation objects "
                          "(default lean: only attribution + chunk_label)")
    ask.add_argument("--no-color", action="store_true",
                     help="Disable ANSI color (auto-disabled when stdout isn't a TTY)")
    ask.add_argument("--json", action="store_true",
                     help="Emit the SynthesizedAnswer as JSON instead of streamed prose")
    ask.set_defaults(func=cmd_ask)

    pg = sub.add_parser("policy-gate-test",
                        help="Exercise the cloud-policy gate (no LLM call); "
                             "shows allowed/denied + the audit row written")
    pg.add_argument("--tenant-id", required=True)
    pg.add_argument("--model", required=True,
                    help="Cloud model from the catalog (deepseek-chat, claude-sonnet-4-6, ...)")
    pg.add_argument("--profile", default="personal",
                    help="processing_profile to test (default: personal)")
    pg.add_argument("--sensitivity", choices=["standard", "sensitive"], default="standard",
                    help="sensitivity_class to test (default: standard)")
    pg.add_argument("--expect", choices=["allowed", "denied"], default="allowed",
                    help="Exit non-zero if outcome doesn't match (for scripted tests)")
    pg.set_defaults(func=cmd_policy_gate_test)

    ul = sub.add_parser("unlock",
                        help="Start the boot daemon (passphrase → KEK → DEK over Unix socket). "
                             "Once running, the worker/API/MCP will pick up the DEK from "
                             "the socket instead of WOXPAS_DEV_DEK_HEX.")
    ul.add_argument("--tenant-id", default=None,
                    help="Tenant UUID (default: $WOXPAS_DEV_TENANT_ID_DEFAULT)")
    ul.add_argument("--socket", default=None,
                    help="Unix socket path (default: ~/.woxpas/dek.sock)")
    ul.add_argument("--idle-lock-sec", type=int, default=None,
                    help="Seconds of inactivity before auto-lock (default: 900)")
    ul.add_argument("--foreground", action="store_true",
                    help="Stay in foreground (default: daemonize)")
    ul.set_defaults(func=cmd_unlock)

    lk = sub.add_parser("lock",
                        help="Tell a running unlock daemon to zeroize the DEK and shut down")
    lk.add_argument("--socket", default=None,
                    help="Unix socket path (default: ~/.woxpas/dek.sock)")
    lk.set_defaults(func=cmd_lock)

    sm = sub.add_parser("serve-mcp",
                        help="Start the MCP server in HTTP/SSE mode for remote "
                             "access (mobile, web, other machines)")
    sm.add_argument("--tenant-id", default=None,
                    help="Tenant UUID (default: $WOXPAS_DEV_TENANT_ID_DEFAULT)")
    sm.add_argument("--host", default="127.0.0.1",
                    help="Bind address (default: 127.0.0.1)")
    sm.add_argument("--port", type=int, default=7801,
                    help="Port (default: 7801)")
    sm.add_argument("--http-mode", default="streamable",
                    choices=["streamable", "sse"],
                    help="streamable (default, MCP 2025-03-26+) | sse (legacy)")
    sm.add_argument("--auth-mode", default="env",
                    choices=["env", "jwt"],
                    help="env (single-tenant, default) | jwt (multi-tenant)")
    sm.set_defaults(func=cmd_serve_mcp)

    pt = sub.add_parser("provision-tenant",
                        help="Provision a tenant for a cloud-hosted Woxpas instance "
                             "(multi-tenant deployments)")
    pt.add_argument("--display-name", required=True)
    pt.add_argument("--tenant-id", default=None,
                    help="Optional tenant UUID (generated if omitted)")
    pt.set_defaults(func=cmd_provision_tenant)

    lt = sub.add_parser("link-tenant",
                        help="Link this local Woxpas install to a cloud-provisioned "
                             "tenant (Path B: product → local)")
    lt.add_argument("--tenant-id", required=True)
    lt.add_argument("--dek-hex", required=True)
    lt.set_defaults(func=cmd_link_tenant)

    rt = sub.add_parser("reclaim-tenant",
                        help="Path B → Path A: rotate the DEK under your own "
                             "passphrase, revoking the product's access")
    rt.add_argument("--tenant-id", required=True)
    rt.set_defaults(func=cmd_reclaim_tenant)

    ex = sub.add_parser("export-tenant",
                        help="Export all tenant data to a portable NDJSON file")
    ex.add_argument("--tenant-id", required=True)
    ex.add_argument("--output", default="woxpas-export.jsonl",
                    help="Output file path (default: woxpas-export.jsonl)")
    ex.set_defaults(func=cmd_export_tenant)

    im = sub.add_parser("import-tenant",
                        help="Import a tenant export file into this Woxpas instance")
    im.add_argument("--tenant-id", required=True)
    im.add_argument("--from", dest="from_path", required=True, metavar="PATH",
                    help="Export file to import")
    im.set_defaults(func=cmd_import_tenant)

    return p


def main() -> None:
    from woxpas_pipeline.boot import load_env
    load_env()

    # SIGPIPE → SIG_DFL so a broken downstream pipe (e.g. `... | head -5`)
    # exits silently with the standard 141 (128 + SIGPIPE=13) instead of
    # raising BrokenPipeError up through Python and printing a traceback.
    # macOS / Linux only; Windows doesn't have SIGPIPE.
    import signal as _signal
    if hasattr(_signal, "SIGPIPE"):
        _signal.signal(_signal.SIGPIPE, _signal.SIG_DFL)

    # Route structlog to stderr for all CLI subcommands. This is the
    # Unix convention — programmatic output (--json, plain text) goes to
    # stdout; logs/diagnostics go to stderr. Without this, structlog's
    # default stdout would interleave with CLI output and break consumers
    # piping output into other tools (jq, less, etc.). Pipeline workers
    # configure their own structlog routing in their entrypoints.
    import structlog as _structlog
    _structlog.configure(
        logger_factory=_structlog.PrintLoggerFactory(file=sys.stderr),
    )

    parser = _build_parser()
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(args.func(args)))
    except KeyboardInterrupt:
        # Ctrl-C: print a clean message + exit with 130 (the standard
        # SIGINT exit code). asyncio.run handles task cancellation
        # internally; our cmd_*'s `finally: ctx.close()` blocks run as
        # part of that cleanup.
        print("\n[interrupted]", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        # If SIGPIPE handler didn't catch it (e.g. exception inside a
        # context manager that interferes), exit silently.
        sys.exit(141)


if __name__ == "__main__":
    main()
