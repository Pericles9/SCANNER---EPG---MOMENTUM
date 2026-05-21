<!-- fullWidth: false tocVisible: false tableWrap: true -->
# Docker + PostgreSQL Crash Course

### Scoped to the EPG Live Trading System

---

## Part 1 — Docker

### What Docker actually is

Docker lets you run software in an isolated box called a **container**. The container has its own OS, its own files, its own network — completely separate from your Windows machine.

Why this matters for you: PostgreSQL runs inside a container. You don't install Postgres on Windows. You don't deal with Windows service managers or registry entries. You just start the container and it runs.

---

### Key concepts

**Image** — a blueprint. `postgres:16` is the official Postgres image. Docker downloads it once from the internet. Think of it like an installer that never changes.

**Container** — a running instance of an image. You can start, stop, and restart it. Your data survives restarts because of volumes (see below).

**Volume** — persistent storage that lives outside the container. In your setup: `pgdata` is a Docker-managed volume. Even if you delete and recreate the container, your database data survives. This is what keeps your trades, ticks, and sessions intact.

**docker-compose.yml** — the config file that defines your whole system. It says: "start these two containers (`db` and `trading`), connect them, give them these environment variables." One file, one command, whole system up.

**.env file** — a plain text file in the same folder as your `docker-compose.yml`. Docker reads it automatically and fills in your secrets. You never hardcode passwords or API keys in the compose file itself.

---

### The commands you'll actually use

```bash
# Start everything (detached = runs in background)
docker compose up -d

# Stop everything
docker compose down

# Stop AND wipe the database volume (nuclear — deletes all your data)
docker compose down -v

# See what's running
docker ps

# See logs from the db container
docker logs epg-db-1

# See logs from the trading container
docker logs epg-trading-1

# Follow logs live (like tail -f)
docker logs -f epg-trading-1

# Restart just one service
docker compose restart db

# Rebuild the trading container after a code change
docker compose up -d --build trading

```

You run all of these from the folder that contains your `docker-compose.yml`. In a terminal (PowerShell or CMD works fine).

---

### How the two containers talk to each other

Inside Docker's network, containers find each other by **service name**. In your compose file the database service is called `db`. So the trading app connects to Postgres at the address `db:5432` — not `localhost:5432`.

That's exactly what `DB_URL` is set to:

```
postgresql://epg:yourpassword@db:5432/epg_live

```

`db` resolves to the Postgres container's internal IP automatically. You never need to know the actual IP.

---

### Windows-specific: `network_mode: host` doesn't work

On Linux, `network_mode: host` lets a container use your machine's network directly — needed for reaching IBKR TWS which runs on localhost.

**On Docker Desktop for Windows this silently does nothing.** The container still gets its own isolated network.

The fix: use `host.docker.internal` instead of `127.0.0.1` when connecting to TWS from inside the trading container. So your IBKR config should be:

```
IBKR_HOST=host.docker.internal

```

Docker Desktop automatically resolves `host.docker.internal` to your Windows host's IP. This is the correct way to do it on Windows/Mac.

---

### The .env file

Create a file literally called `.env` (no filename, just the extension) in the same directory as `docker-compose.yml`:

```env
POSTGRES_PASSWORD=pick_a_strong_password
POLYGON_API_KEY=your_polygon_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
IBKR_HOST=host.docker.internal
IBKR_PORT=7497
IBKR_CLIENT_ID=1

```

Docker reads this file automatically when you run `docker compose up`. Never commit this file to Git — add `.env` to your `.gitignore`.

---

### What happens on first `docker compose up -d`

1. Docker downloads the `postgres:16` image (\~150MB, one time only)
2. Starts the `db` container
3. Postgres runs the init script (`init.sql`) **once**, on first startup only — this creates all your tables
4. The `trading` container waits for `db` to be healthy before starting
5. Both are running in the background

On every subsequent `docker compose up -d`, Postgres skips the init script because the data volume already exists.

---

### Checking if it's working

```bash
# Both containers should show "Up" and db should show "(healthy)"
docker ps

```

You should see something like:

```
CONTAINER ID   IMAGE         STATUS                    NAMES
abc123         epg-trading   Up 10 seconds             epg-trading-1
def456         postgres:16   Up 15 seconds (healthy)   epg-db-1

```

---

---

## Part 2 — PostgreSQL

### What PostgreSQL is (in one sentence)

A database that stores your data in tables — like Excel sheets, but queryable with SQL and capable of handling thousands of writes per second safely.

---

### Your database structure

```
Database: epg_live
│
├── strategies          — registry of strategies (epg_v1, etc.)
├── scanner_snapshots   — every Polygon scanner poll result
│
├── ticks               — every trade tick from Polygon WS  ← high volume
├── quotes              — every quote from Polygon WS       ← high volume
│
├── positions           — current open positions per strategy
├── orders              — every order submitted to IBKR
├── trades              — completed round-trips (entry + exit)
├── sessions            — one row per ticker-per-day per strategy
├── hawkes_refits       — Hawkes model refits during session
└── signal_events       — key EPG state transitions

```

**Shared tables** (`ticks`, `quotes`, `scanner_snapshots`) have no `strategy_id` — they're market data, strategy-agnostic.

**Strategy-tagged tables** (`orders`, `trades`, `sessions`, etc.) all have a `strategy_id` column that references `strategies.id`.

---

### How writes work in your system

Two separate write paths by design:

**Path 1 — Critical writes (immediate, transactional)** Orders, fills, position updates. Written by the Order Worker with an explicit transaction. If it fails, it rolls back. Nothing is half-written.

```
Order filled → write to orders table → update positions → write to trades
All inside one transaction. Succeeds fully or fails fully.

```

**Path 2 — Bulk writes (batched, every 1 second)** Ticks, quotes, signal events. Collected in memory, flushed every second using PostgreSQL `COPY` — the fastest way to bulk-insert. A missed flush loses at most 1 second of ticks, which is acceptable.

---

### The connection string

Everything talks to Postgres through one env var:

```
DB_URL=postgresql://epg:yourpassword@db:5432/epg_live

```

Breaking it down:

- `epg` — username
- `yourpassword` — password (from your .env file)
- `db` — hostname (the Docker service name)
- `5432` — default Postgres port
- `epg_live` — database name

If you ever move to AWS RDS, you change **only this one variable** to point at the RDS endpoint. No code changes anywhere.

---

### Connecting to the database manually

Sometimes you'll want to look at your data directly. Two options:

**Option 1 — psql inside the container (no install needed)**

```bash
docker exec -it epg-db-1 psql -U epg -d epg_live

```

Then you can run SQL:

```sql
SELECT * FROM trades ORDER BY entry_ns DESC LIMIT 10;
SELECT COUNT(*) FROM ticks WHERE session_date = CURRENT_DATE;
\q   -- quit

```

**Option 2 — pgAdmin (GUI, easier)** Download pgAdmin 4 (free). Connect with:

- Host: `localhost`
- Port: `5432`
- Username: `epg`
- Password: whatever you set in .env
- Database: `epg_live`

This works because your compose file exposes port `5432` to your Windows machine.

---

### The init script runs once

`init.sql` (mounted into the container) runs automatically the **first time** the container starts with an empty data volume. It creates all your tables.

**It will NOT re-run** on subsequent startups — Postgres knows data already exists.

If you ever need to reset everything and re-run the init script from scratch:

```bash
docker compose down -v   # -v deletes the pgdata volume
docker compose up -d     # fresh start, init.sql runs again

```

**Warning:** `down -v` deletes all your data. Don't run this on a system with real trade history.

---

### What to do if the db container won't start

```bash
# Check the logs
docker logs epg-db-1

# Common causes:
# - Port 5432 already in use (another Postgres install on Windows?)
# - Bad password characters in .env (avoid @ # $ in passwords)
# - Volume permissions issue (rare on Windows)

```

---

### Backup (when you have real data worth keeping)

```bash
# Dump the whole database to a file
docker exec epg-db-1 pg_dump -U epg epg_live > backup_$(date +%Y%m%d).sql

# Restore from backup
docker exec -i epg-db-1 psql -U epg epg_live < backup_20260520.sql

```

Run the backup before any `docker compose down -v` if you want to keep the data.

---

## Quick Reference

| Task                      | Command                                                   |
| ------------------------- | --------------------------------------------------------- |
| Start system              | `docker compose up -d`                                    |
| Stop system               | `docker compose down`                                     |
| View running containers   | `docker ps`                                               |
| View db logs              | `docker logs epg-db-1`                                    |
| Follow trading logs       | `docker logs -f epg-trading-1`                            |
| Open SQL console          | `docker exec -it epg-db-1 psql -U epg -d epg_live`        |
| Rebuild after code change | `docker compose up -d --build trading`                    |
| Full reset (deletes data) | `docker compose down -v`                                  |
| Backup database           | `docker exec epg-db-1 pg_dump -U epg epg_live > backup.sql` |

---

## The Only Things You Touch

1. **`.env` file** — your secrets. Fill it in once.
2. **`docker compose up -d`** — start the system.
3. **`docker compose down`** — stop it.
4. Everything else is handled by the code.