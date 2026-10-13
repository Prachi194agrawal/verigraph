#!/usr/bin/env bash
# Cold, consistent backup of the core stack. Optional tracing/Ollama services are excluded.
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f docker-compose.yml -f docker-compose.production.yml)
backup_root="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$backup_root"
backup_root="$(realpath "$backup_root")"
chmod 700 "$backup_root"
if [[ -e "$backup_root/neo4j.dump" || -e "$backup_root/results.sql" || -e "$backup_root/app-data.tar.gz" ]]; then
  echo 'Backup destination already contains a backup; choose a new directory.' >&2
  exit 1
fi
# Always restart stopped application services, including after an unsuccessful backup.
trap '"${compose[@]}" start neo4j api worker web' EXIT
"${compose[@]}" stop web api worker neo4j
"${compose[@]}" exec -T postgres pg_dump -U verigraph --clean --if-exists verigraph > "$backup_root/results.sql"
"${compose[@]}" run --rm --no-deps --user 0 -v "$backup_root:/backups" neo4j \
  neo4j-admin database dump neo4j --to-path=/backups
"${compose[@]}" run --rm --no-deps --user 0 -v "$backup_root:/backups" api \
  tar -czf /backups/app-data.tar.gz -C /data .
# Docker creates these files as root; set permissions inside a container as well.
"${compose[@]}" run --rm --no-deps --user 0 -v "$backup_root:/backups" api \
  sh -c 'chmod 600 /backups/*'
echo "Backup written to $backup_root. Store an encrypted copy off this server."
