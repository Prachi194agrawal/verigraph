FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# INSTALL_EMBEDDINGS=false gives a much smaller image; then set EMBEDDING_MODEL=hash.
ARG INSTALL_EMBEDDINGS=true
RUN if [ "$INSTALL_EMBEDDINGS" = "true" ]; then \
      pip install torch --index-url https://download.pytorch.org/whl/cpu; fi

# Dependencies first (cached until pyproject.toml changes), then the source.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py && \
    EXTRAS="tracing" && if [ "$INSTALL_EMBEDDINGS" = "true" ]; then EXTRAS="tracing,embeddings"; fi && \
    pip install ".[$EXTRAS]" && pip uninstall -y verigraph-code && rm -rf app build *.egg-info
COPY app ./app
RUN pip install --no-deps .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
