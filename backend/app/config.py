"""Runtime configuration. Every field can be set as an environment variable (upper case) or in `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Call edges resolved by these resolvers may become gold answers. Name-only edges never do.
PRECISE_RESOLVERS = ("scip", "jedi", "import")
ALL_RESOLVERS = ("scip", "jedi", "import", "name")


class Settings(BaseSettings):
    # The repo-root .env (shared with docker compose) and then backend/.env, which wins.
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    # Storage
    data_dir: Path = Path("data")  # clones, traces, LLM cache, sandbox logs
    database_url: str | None = None  # default: SQLite file inside data_dir

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "verigraph-password"
    neo4j_database: str = "neo4j"

    # Background jobs: "inline" runs jobs in a thread of the API process; "celery" sends them to Redis.
    job_backend: Literal["inline", "celery"] = "inline"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: str = "http://localhost:3000"

    # Ingest
    # Comma-separated directories that local-path repositories must live under, e.g. "/fixtures,/repos". The API
    # serves files from build checkouts, so without this list any server folder could be registered and read.
    # Empty allows every local path (single-user local development only).
    local_repo_roots: str = ""
    github_token: str | None = None
    max_history_commits: int = 1000
    max_pull_requests: int = 300
    github_unauthenticated_budget: int = 40  # API calls allowed without a token (limit is 60/hour)

    # Graph
    resolvers: str = ",".join(ALL_RESOLVERS)  # tried in this order for every call site
    scip_python_bin: str = "scip-python"

    # Semantic layer
    embedding_model: str = "nomic-ai/CodeRankEmbed"  # or "hash" for the dependency-free embedder
    embedding_batch_size: int = 16
    embedding_dim: int = 768  # must match the model; the Neo4j vector index is created with it
    embedding_max_tokens: int = 512  # longer inputs are truncated; attention cost grows quadratically on CPU

    # LLM gateway
    ollama_api_base: str | None = None
    llm_cache: bool = True
    llm_timeout_s: float = 120.0
    llm_num_retries: int = 2
    # Free tiers allow few requests and tokens per minute. Concurrent calls per provider (the model-name prefix,
    # "groq" for "groq/..."), per process, as "provider=n,..."; unlisted providers are not limited.
    llm_provider_concurrency: str = "groq=2,gemini=2,openrouter=2"
    llm_rate_limit_retries: int = 6  # further attempts after an HTTP 429, waiting as the provider asks
    llm_rate_limit_max_wait_s: float = 60.0

    # Tracing (Langfuse is used when both keys are set; JSONL traces are always written)
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None

    # Test sandbox (stretch goal)
    sandbox_image_prefix: str = "verigraph-sandbox"
    sandbox_timeout_s: int = 600

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'verigraph.db').resolve()}"

    @property
    def resolver_order(self) -> list[str]:
        order = [r.strip() for r in self.resolvers.split(",") if r.strip()]
        unknown = set(order) - set(ALL_RESOLVERS)
        if unknown:
            raise ValueError(f"Unknown resolvers {sorted(unknown)}; choose from {ALL_RESOLVERS}")
        return order

    @property
    def provider_concurrency(self) -> dict[str, int]:
        limits = {}
        for item in self.llm_provider_concurrency.split(","):
            if item.strip():
                provider, _, n = item.partition("=")
                limits[provider.strip()] = max(1, int(n))
        return limits

    @property
    def local_roots(self) -> list[Path]:
        return [Path(r.strip()).expanduser().resolve() for r in self.local_repo_roots.split(",") if r.strip()]

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
