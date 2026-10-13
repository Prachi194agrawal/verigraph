# Secure single-server deployment

This release supports a shared team installation or a public read-only demonstration. Authenticated
users share all repositories, results, labels and delete permissions; this is not a multi-tenant service.
No cloud account, domain or provider credential is created by the repository.

## Configure

Use a Linux server with Docker, Compose **2.24.4+**, a DNS hostname pointing at it, and sufficient RAM
for the six core services. The production configuration limits containers to approximately 10 GB total;
leave additional RAM for the OS and Docker builds. Only open SSH, TCP 80 and TCP 443 in the host and
cloud firewall. Caddy obtains HTTPS certificates automatically for a valid public DNS hostname.
Cloud eligibility, prices and available capacity must be checked separately before provisioning.

```bash
cp -n .env.example .env
```

Edit `.env` locally:

```dotenv
DOMAIN=your-real-dns-hostname.example
NEO4J_PASSWORD=replace-with-a-long-random-password
POSTGRES_PASSWORD=replace-with-a-long-random-URL-safe-password
API_USERS='{"demo":"replace-with-a-long-random-password","reviewer":"another-random-password"}'
REQUIRE_AUTH=true
DEMO_READ_ONLY=false
API_DAILY_WRITE_LIMIT=100
LLM_DAILY_CALL_LIMIT=100
MAX_EXPERIMENT_ANSWERS=100
MAX_REPO_FILES=5000
MAX_REPO_BYTES=100000000
INSTALL_EMBEDDINGS=false
EMBEDDING_MODEL=hash
```

Use URL-safe characters in the PostgreSQL password because Compose embeds it in database URLs.
Changing `POSTGRES_PASSWORD` does not update an existing PostgreSQL volume's database role: rotate
that role's password separately when upgrading an installation. Neo4j's initial password similarly
does not replace credentials in an existing database. Never delete volumes to rotate credentials.

Set `DEMO_READ_ONLY=true` only on an installation containing **public sample data**. Anonymous visitors
can read every analyzed repository's source, result, export and graph; all POST/PUT/PATCH/DELETE requests
still require credentials. Keep a separate private instance for private repositories.

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.production.yml ps
curl https://YOUR_DOMAIN/backend/health
```

The dashboard uses `/backend` in its production build. Caddy strips that prefix before forwarding to
FastAPI. The database, Bolt, API and dashboard ports have no host mappings in production; only Caddy
publishes ports. The local Compose configuration binds developer ports to `127.0.0.1`.
Leave the optional tracing and Ollama profiles disabled for the initial deployment.
An ARM64 installation still requires an actual image build and runtime verification on ARM hardware.

Sign in through the dashboard. Credentials are retained in memory for that browser tab and are cleared
on reload or sign-out. Scripts can send HTTP Basic credentials, for example:

```bash
curl --user demo https://YOUR_DOMAIN/backend/repos
```

`curl` prompts for the password. Do not put credentials in a public frontend environment variable.
The deployment serves the dashboard shell publicly; private repository data requires API authentication.
For a private network installation, additionally restrict access at the firewall or VPN.

## Budgets and limits

- Each authenticated user has a durable daily write-attempt quota. Failed or invalid write requests also
  count. Reads and health checks do not consume it. Exhaustion returns HTTP 429 with `Retry-After`.
- The deployment-wide LLM budget counts uncached real-provider **attempts**, including explicit 429
  retries, across API processes and Celery workers. Mock responses and cache hits consume no calls.
  When enabled, hidden LiteLLM retries are disabled so all attempts are counted. A failure is recorded
  on the answer/job when the budget is reached. This caps calls, not dollars or input-token spend.
- Experiments are rejected before scheduling when questions × models × pipelines exceeds the configured
  answer limit. Use a smaller `limit` to run a sample. Optional judge/agent calls also consume the LLM budget.
- Checkout limits reject oversized repositories before parsing, embeddings and graph writes. These are
  post-download limits, not limits on Git network transfer or mirror disk usage. Register trusted repos,
  prefer fast/shallow builds, and monitor disk space. Container memory limits constrain runtime memory.
- Daily counters use UTC midnight and persist in PostgreSQL/SQLite. API quotas are per username;
  LLM quotas are shared across the installation. Local development remains unauthenticated unless configured.

## Back up and clean up

The backup script pauses the core application to create matching PostgreSQL, Neo4j and application-data
snapshots, then restarts the application even if a backup fails. Run it without optional profiles active:

```bash
bash scripts/backup.sh
```

It writes `results.sql`, `neo4j.dump` and `app-data.tar.gz` under a timestamped `backups/` directory with
restricted permissions. Backups include source checkouts, LLM caches and potentially sensitive data.
Copy an encrypted backup off the server and monitor backup success. Back up `.env` separately in secure
storage; it is intentionally absent from these archives. Caddy can reissue its certificates.

To restore, first stop `web`, `api`, `worker` and `neo4j` using the production Compose files. Restore
`results.sql` into the running `postgres` service with `psql -U verigraph -d verigraph`, load `neo4j.dump`
using an offline `neo4j-admin database load neo4j --from-path=/backups --overwrite-destination=true`
container mounting the backup and the existing Neo4j volume, and restore `app-data.tar.gz` to `/data`
using a one-off API container. Restoring overwrites data: validate the procedure on a separate instance
before using it on a live deployment. Start the core services and verify health and a known build afterward.

Use the existing authenticated `DELETE /builds/{id}` route or `verigraph delete-build ID` to remove old
builds. Both refuse builds with active jobs and delete their graph, checkout, datasets and experiments.
Mirrors and global LLM caches are shared and are not removed by deleting a build. Review their disk usage
separately. The CLI is an operator interface and does not enforce HTTP user quotas.
