"""
Central configuration loader.

Rule of the project: NO other file reads os.environ directly.
Everything goes through get_settings(). Benefits:
  1. One place to see every setting the system uses.
  2. Fail fast: a missing/invalid value stops startup with a clear message,
     instead of crashing mid-run at 2 a.m.
  3. Secrets are wrapped so they cannot leak into logs by accident.

Used by BOTH the API (backend/) and the browser worker (agents/).
Run `python -m backend.config` from the repo root to check your setup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Reads ROOT/.env into os.environ. override=False means REAL environment
# variables (set by Render / GitHub Actions in production) always win over
# the file. That is how the same code works locally and when deployed.
load_dotenv(ROOT / ".env", override=False)


class ConfigError(Exception):
    """Raised at startup when configuration is missing or invalid."""


class Secret:
    """Wraps a sensitive string. Printing or logging it shows ****.
    You must call .reveal() deliberately to get the real value."""

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('****')"

    __str__ = __repr__


class _Reader:
    """Reads env vars and COLLECTS every problem, so the user sees all
    mistakes at once instead of fixing them one restart at a time."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def _raw(self, name: str) -> str:
        return os.getenv(name, "").strip()

    def text(self, name: str, default: str | None = None) -> str:
        v = self._raw(name)
        if v:
            return v
        if default is not None:
            return default
        self.errors.append(f"{name} is required but empty")
        return ""

    def optional(self, name: str) -> str | None:
        return self._raw(name) or None

    def secret(self, name: str, required: bool = True) -> Secret | None:
        v = self._raw(name)
        if v:
            return Secret(v)
        if required:
            self.errors.append(f"{name} is required but empty (secret)")
        return None

    def integer(self, name: str, default: int, minimum: int = 1) -> int:
        v = self._raw(name)
        if not v:
            return default
        try:
            n = int(v)
        except ValueError:
            self.errors.append(f"{name} must be a whole number, got '{v}'")
            return default
        if n < minimum:
            self.errors.append(f"{name} must be >= {minimum}, got {n}")
        return n

    def csv(self, name: str, required: bool = False) -> list[str]:
        items = [p.strip() for p in self._raw(name).split(",") if p.strip()]
        if required and not items:
            self.errors.append(f"{name} must list at least one value")
        return items


@dataclass(frozen=True)  # frozen = settings can't be changed after startup
class Settings:
    # general
    app_env: str
    log_level: str
    cors_origins: list[str]
    # supabase
    supabase_url: str
    supabase_service_role_key: Secret
    database_url: Secret | None
    # llm
    llm_provider: str
    gemini_api_key: Secret | None
    gemini_model: str | None
    groq_api_key: Secret | None
    groq_model: str | None
    # worker trigger
    github_token: Secret | None
    github_repo: str
    github_workflow_file: str
    cron_secret: Secret | None
    # browser policy
    sandbox_base_url: str
    allowed_domains: list[str]
    max_requests_per_minute: int
    max_steps_per_run: int
    run_timeout_seconds: int
    max_retries: int
    # monitoring
    sentry_dsn: str | None

    def to_safe_dict(self) -> dict:
        """Dict for logging/debugging. Secrets print as ****."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def load_settings() -> Settings:
    r = _Reader()

    app_env = r.text("APP_ENV", "development")
    if app_env not in {"development", "production"}:
        r.errors.append(f"APP_ENV must be development or production, got '{app_env}'")

    supabase_url = r.text("SUPABASE_URL")
    if supabase_url and not supabase_url.startswith("https://"):
        r.errors.append("SUPABASE_URL must start with https://")
    if "YOUR-PROJECT-REF" in supabase_url:
        r.errors.append("SUPABASE_URL still has the placeholder value from .env.example")

    provider = r.text("LLM_PROVIDER", "gemini").lower()
    if provider not in {"gemini", "groq"}:
        r.errors.append(f"LLM_PROVIDER must be gemini or groq, got '{provider}'")
    # Only the CHOSEN provider's key + model are mandatory.
    gemini_key = r.secret("GEMINI_API_KEY", required=(provider == "gemini"))
    gemini_model = r.text("GEMINI_MODEL", "") if provider != "gemini" else r.text("GEMINI_MODEL")
    groq_key = r.secret("GROQ_API_KEY", required=(provider == "groq"))
    groq_model = r.text("GROQ_MODEL", "") if provider != "groq" else r.text("GROQ_MODEL")

    settings = Settings(
        app_env=app_env,
        log_level=r.text("LOG_LEVEL", "INFO").upper(),
        cors_origins=r.csv("CORS_ORIGINS"),
        supabase_url=supabase_url,
        supabase_service_role_key=r.secret("SUPABASE_SERVICE_ROLE_KEY") or Secret(""),
        database_url=r.secret("DATABASE_URL", required=False),
        llm_provider=provider,
        gemini_api_key=gemini_key,
        gemini_model=gemini_model or None,
        groq_api_key=groq_key,
        groq_model=groq_model or None,
        github_token=r.secret("GITHUB_TOKEN", required=False),
        github_repo=r.text("GITHUB_REPO", ""),
        github_workflow_file=r.text("GITHUB_WORKFLOW_FILE", "browser-worker.yml"),
        cron_secret=r.secret("CRON_SECRET", required=False),
        sandbox_base_url=r.text("SANDBOX_BASE_URL", "http://localhost:8080"),
        # Allowlist is REQUIRED (fail closed): no list -> browser can go nowhere.
        allowed_domains=[d.lower() for d in r.csv("ALLOWED_DOMAINS", required=True)],
        max_requests_per_minute=r.integer("MAX_REQUESTS_PER_MINUTE", 30),
        max_steps_per_run=r.integer("MAX_STEPS_PER_RUN", 40),
        run_timeout_seconds=r.integer("RUN_TIMEOUT_SECONDS", 300),
        max_retries=r.integer("MAX_RETRIES", 2, minimum=0),
        sentry_dsn=r.optional("SENTRY_DSN"),
    )

    if r.errors:
        bullet = "\n  - ".join(r.errors)
        raise ConfigError(
            f"Configuration problems ({len(r.errors)}):\n  - {bullet}\n"
            "Fix your .env (compare with .env.example) and try again."
        )
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The one function the rest of the codebase calls. Cached: .env is
    parsed once per process."""
    return load_settings()


if __name__ == "__main__":
    # `python -m backend.config`  ->  quick health check of your .env
    try:
        s = get_settings()
    except ConfigError as e:
        print(f"CONFIG INVALID\n{e}")
        raise SystemExit(1)
    print("CONFIG OK. Loaded values (secrets masked):")
    for k, v in s.to_safe_dict().items():
        print(f"  {k:28} {v}")
