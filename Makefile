.PHONY: up down neo4j install test lint api web demo gate-smoke gate

up:            ## everything in Docker: neo4j, postgres, redis, api, worker, web
	docker compose up -d --build

down:
	docker compose down

neo4j:         ## only the graph database, for local development
	docker compose up -d neo4j

install:       ## local dev environment (Python 3.11+, Node 20+)
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

test:          ## unit + integration tests (integration tests need `make neo4j`)
	cd backend && .venv/bin/pytest -q

lint:
	cd backend && .venv/bin/ruff check app tests

api:           ## FastAPI on :8000, jobs run in-process (JOB_BACKEND=inline)
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

web:           ## dashboard on :3000
	cd frontend && npm run dev

demo:          ## API + Celery end-to-end demo; start Docker Compose first
	python3 scripts/demo.py

gate-smoke:    ## offline eval gate: fixture repo + mock model
	cd backend && EMBEDDING_MODEL=hash .venv/bin/verigraph gate --config ../evals/gate.smoke.yaml

gate:          ## real eval gate: psf/requests slice with a real model (needs GROQ_API_KEY)
	cd backend && EMBEDDING_MODEL=hash .venv/bin/verigraph gate --config ../evals/gate.yaml
