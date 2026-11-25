# magicDataHub Docker Quickstart

`ghcr.io/muidea/datahub-server` pre-installs `postgres_scanner` and `mysql_scanner` so PostgreSQL, Greenplum, KingbaseES, MySQL, MariaDB, StarRocks, Web UI/Management API, and MQTT work out of the box (~360 MiB compressed pull ~130 MiB).

Optional database families (`sqlserver`, `oracle`, `db2`, `hana`, `dameng`) are distributed as separate OCI Init Container images and mounted read-only into `/opt/magicdatahub/plugins`.

## 1. Docker Compose (Recommended)

```sh
cp .env.example .env
# Edit FEDENGINE_PASSWORD in .env

# Base mode (PG / Greenplum / KingbaseES / MySQL / MariaDB / StarRocks + MQTT + Web UI)
docker compose up -d

# Enable Oracle and Dameng (DM8) on demand
docker compose --profile oracle --profile dameng up -d

# Enable all database plugins
docker compose --profile all up -d
```

Exposed ports (configurable in `.env`):

| Service | Container Port | Default Host Binding | Protocol |
|---|---|---|---|
| Virtual PG Server | `5432` | `0.0.0.0:5432` | PostgreSQL wire / DBeaver / JDBC |
| Management & Web UI | `8080` | `0.0.0.0:8080` | HTTP (`/`, `/api/v1/plugins`, `/api/v1/sources`) |
| MQTT Broker | `1883` | `0.0.0.0:1883` | MQTT 3.1.1 / 5.0 |

Check installed database plugin readiness at any time:

```sh
curl -fsS http://127.0.0.1:8080/api/v1/plugins
```

## 2. Single Container (`docker run`, Base Mode)

```sh
docker run -d --name magicdatahub \
  -p 5432:5432 -p 8080:8080 -p 1883:1883 \
  -e FEDENGINE_PASSWORD='change-me-please' \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --stop-signal SIGTERM --stop-timeout 60 \
  -v datahub-state:/opt/magicdatahub/var \
  ghcr.io/muidea/datahub-server:v0.1.0
```

## 3. Graceful Shutdown

Wait for DuckDB state checkpointing before stopping (`stop_grace_period: 60s` is pre-configured in `docker-compose.yml`):

```sh
docker compose stop -t 60
```

Data and installed plugins persist across upgrades in named volumes `datahub-state` and `datahub-plugins`.
