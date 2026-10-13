"""FastAPI application: `uvicorn app.main:app --reload`."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app import __version__
from app.access import AccessMiddleware
from app.api import builds, datasets, experiments, repos, results
from app.config import get_settings
from app.db import get_engine, init_db
from app.graph.store import get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("pydriller", "httpx", "LiteLLM", "litellm", "neo4j"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if get_settings().require_auth and not get_settings().api_users:
        raise RuntimeError("REQUIRE_AUTH=true requires nonempty API_USERS")
    init_db()
    try:
        get_store().ensure_schema()
    except Exception as exc:  # noqa: BLE001 - the API can still serve results without Neo4j
        logging.getLogger(__name__).warning("Neo4j not reachable at startup: %s", exc)
    yield


app = FastAPI(title="VeriGraph for Code", version=__version__, lifespan=lifespan,
              description="Code knowledge graph + LLM evaluation for software projects.")
app.add_middleware(AccessMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
                   allow_methods=["*"], allow_headers=["*"])
for module in (repos, builds, datasets, experiments, results):
    app.include_router(module.router)


@app.get("/access", tags=["meta"])
def access(request: Request) -> dict:
    settings = get_settings()
    return {"username": request.state.username, "authentication": bool(settings.api_users) or settings.require_auth,
            "demo_read_only": settings.demo_read_only, "daily_write_limit": settings.api_daily_write_limit}


@app.get("/capabilities", tags=["meta"])
def capabilities() -> dict:
    """Public configuration hints. API keys and credentials are never returned."""
    from app.graph.languages import LANGUAGE_SPECS

    settings = get_settings()
    providers = [{"name": provider, "configured": bool(os.environ.get(key, "").strip())}
                 for provider, key in (("Groq", "GROQ_API_KEY"), ("Gemini", "GEMINI_API_KEY"),
                                       ("OpenAI", "OPENAI_API_KEY"), ("Anthropic", "ANTHROPIC_API_KEY"))]
    return {"languages": [{"name": spec.name, "extensions": list(spec.extensions),
                           "level": "Python import/inference analysis" if spec.name == "python"
                           else "File inventory only" if spec.level == "file"
                           else "Syntax extraction and conservative static calls"}
                          for spec in LANGUAGE_SPECS],
            "providers": providers, "embedding_model": settings.embedding_model,
            "default_model": settings.default_model,
            "notes": ["Provider status means a key is configured, not that its validity has been verified.",
                      "Mock models verify the workflow. Choose a real model for AI evaluations.",
                      "Unknown source languages are inventoried without invented call relationships.",
                      "Cross-language API calls and dynamic dispatch are not inferred."]}


@app.get("/health", tags=["meta"])
def health() -> dict:
    status = {"version": __version__, "job_backend": get_settings().job_backend}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        status["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Database health check failed: %s", exc)
        status["database"] = "error"
    try:
        get_store().verify()
        status["neo4j"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("Neo4j health check failed: %s", exc)
        status["neo4j"] = "error"
    return status
