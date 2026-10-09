"""Entrypoint helper — loads .env into os.environ for processes that need it.

pydantic-settings reads .env on its own for BaseSettings classes, but plain
os.environ.get() lookups (e.g., for WOXPAS_KEY_<provider> inside cloud LLM
backends) don't see it. Calling load_env() at the top of main() bridges the
gap for dev / single-tenant local installs.

In production the boot service injects secrets via Unix socket and there is
no .env to load — load_env() finds none and returns silently.

Lookup order: walks up from the current working directory to find .env.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_env() -> Path | None:
    """Load .env (if present) into os.environ. Returns the path that was
    loaded, or None if no file was found. override=False so any value already
    in the process environment wins — useful for ad-hoc `WOXPAS_KEY_X=... cmd`.
    """
    here = Path.cwd()
    for candidate in [here, *here.parents]:
        env_path = candidate / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            return env_path
    return None
