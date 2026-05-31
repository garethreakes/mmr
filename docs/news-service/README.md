# News Service — Disaster Recovery & Rebuild Guide

> **Owner:** G (garethreakes)  
> **Location:** Mac Mini (`openclaw`, Tailscale IP `100.91.155.72`)  
> **Last verified:** 2026-05-31  
> **Purpose:** Financial news scraping + LLM extraction service, used by the MMR trading platform and Pit (Hatch AI agent)

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [Directory Layout](#directory-layout)
4. [Source Repositories](#source-repositories)
5. [Environment Variables](#environment-variables)
6. [Step-by-Step Rebuild](#step-by-step-rebuild)
7. [The llmvm_lite Gotcha (CRITICAL)](#the-llmvm_lite-gotcha-critical)
8. [SSH Tunnel from Hatch VM](#ssh-tunnel-from-hatch-vm)
9. [Verification](#verification)
10. [Container Lifecycle & Gotchas](#container-lifecycle--gotchas)
11. [Configuration Reference](#configuration-reference)
12. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
┌─────────────────┐         SSH tunnel (port 8089)        ┌──────────────────────────┐
│   Hatch VM      │ ──────────────────────────────────▶   │  Mac Mini (openclaw)     │
│  (Pit agent)    │        100.91.155.72                   │                          │
│                 │                                        │  Docker: news-news-1     │
│  curl localhost │                                        │  ├─ news service (uvicorn)│
│     :8089       │                                        │  ├─ llmvm_lite           │
│                 │                                        │  ├─ rustdown             │
└─────────────────┘                                        │  └─ Playwright/Chromium  │
                                                           └──────────────────────────┘
```

The news service runs in a Docker container on the Mac Mini. It provides:
- **`/v1/scrape`** — fetch and convert web pages to clean markdown
- **`/v1/scrape` + `extract`** — fetch + LLM extraction (Mode 2/3)
- **`/v1/search`** — SerpAPI/DuckDuckGo web search
- **`/v1/health`** — health check endpoint
- **`/v1/pdf`** — PDF→Markdown via Mathpix (optional)

The Hatch VM connects via an SSH tunnel that forwards port 8089.

---

## Prerequisites

On the Mac Mini:
- **macOS** with admin access
- **Docker Desktop** installed (provides `docker` + `docker compose`)
  - Docker CLI path: `/Applications/Docker.app/Contents/Resources/bin/`
- **Rust toolchain** (installed via `rustup` — needed for building rustdown)
- **Git** with SSH key authenticated to GitHub
- **Tailscale** connected (for SSH access from Hatch VM)

---

## Directory Layout

```
/opt/trading-tools/
├── news/                  # News service (Joel's repo: 9600dev/news, private)
│   ├── docker-compose.yml
│   ├── docker.sh          # Build/manage helper script
│   ├── Dockerfile
│   ├── configs/
│   │   ├── news.yaml      # Main config
│   │   └── logging.yaml
│   ├── scripts/
│   │   └── docker-entrypoint.sh
│   ├── news/              # Python package source
│   ├── requirements.txt
│   └── ...
├── llmvm_lite/            # Correct llmvm_lite package (see CRITICAL section below)
│   ├── pyproject.toml     # name = "llmvm_lite", v0.0.1, hatchling build
│   ├── README.md          # MUST exist for hatchling builds
│   ├── llmvm_lite/        # Python package
│   │   ├── providers/
│   │   │   └── anthropic_provider.py  # PATCHED — see below
│   │   └── ...
│   └── requirements.txt
├── llmvm_lite_OLD/        # Backup of wrong package (llmvm-cli) — ignore
├── mmr/                   # MMR trading platform (9600dev/mmr, public)
├── llmvm/                 # Full llmvm CLI (separate from llmvm_lite)
├── docker-cmd.sh          # Wrapper: sets PATH and runs as garethreakes
└── .github-config         # Notes on SSH key setup for 9600dev repos

/Users/garethreakes/dev/
└── rustdown/              # Rustdown (HTML→Markdown, Rust+Python)
    ├── Cargo.toml
    ├── Cargo.lock
    ├── rustdown_core/
    ├── rustdown_cli/
    └── rustdown_py/       # Python bindings (built via maturin)

Symlink: /opt/trading-tools/rustdown → /Users/garethreakes/dev/rustdown
```

---

## Source Repositories

| Component | Repo | Access |
|-----------|------|--------|
| News service | `git@github.com:9600dev/news.git` (private) | SSH key `id_ed25519_github` |
| MMR | `git@github.com:9600dev/mmr.git` (public) | Same key |
| llmvm_lite | Originally from Joel's Meta-internal `fbsource`. We have a local copy at `/opt/trading-tools/llmvm_lite/` | Local only |
| rustdown | Joel's repo (exact GitHub location TBD — may be private/internal) | Local copy at `/Users/garethreakes/dev/rustdown/` |
| This README | `git@github.com:garethreakes/mmr.git` | Same key |

**Important:** The `id_ed25519_github` SSH key lives in `/Users/hatch/.ssh/` on the Mac Mini and authenticates as `garethreakes` on GitHub.

---

## Environment Variables

These must be exported in `~/.zshrc` (garethreakes' shell). Docker Compose reads them at `docker compose up` time and passes them into the container.

```bash
# Required for LLM extraction (Mode 2/3)
export ANTHROPIC_API_KEY="sk-ant-api03-XXXXX"

# Optional — other LLM providers
export OPENAI_API_KEY="sk-XXXXX"

# Required for Google search (falls back to DuckDuckGo without it)
export SERPAPI_API_KEY="XXXXX"

# Optional — PDF conversion
export MATHPIX_API_KEY="XXXXX"
```

The `docker-compose.yml` forwards these via `${VAR_NAME:-}` syntax. The container also accepts `ANT_API_KEY`, `OAI_API_KEY` as short-name aliases.

**Key gotcha:** When you run `docker compose up`, compose reads env vars from the **current shell session**. If you just edited `.zshrc`, you must either `source ~/.zshrc` first or start a new shell. The most reliable approach is:

```bash
# Start a fresh login shell that sources .zshrc
zsh -lic "cd /opt/trading-tools/news && docker compose up -d"
```

---

## Step-by-Step Rebuild

### 1. Install Docker Desktop

Download from https://www.docker.com/products/docker-desktop/ and install. Ensure it's running.

### 2. Set up Tailscale

Install Tailscale, authenticate to the tailnet (`tailf46aa2.ts.net`). The Mac Mini should appear as `openclaw` at `100.91.155.72`.

### 3. Create directory structure

```bash
sudo mkdir -p /opt/trading-tools
sudo chown $(whoami):staff /opt/trading-tools
```

### 4. Set up GitHub SSH key

```bash
# Generate or restore the SSH key
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_github -C "openclaw-mac-mini"

# Add to GitHub: Settings → SSH Keys → New SSH Key
# Test: ssh -i ~/.ssh/id_ed25519_github -T git@github.com

# Configure SSH to use this key for GitHub
cat >> ~/.ssh/config << 'EOF'
Host github.com
  IdentityFile ~/.ssh/id_ed25519_github
  IdentitiesOnly yes
EOF
```

### 5. Clone repositories

```bash
cd /opt/trading-tools
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_github" git clone git@github.com:9600dev/news.git
GIT_SSH_COMMAND="ssh -i ~/.ssh/id_ed25519_github" git clone git@github.com:9600dev/mmr.git

# rustdown — check Joel's repos or restore from backup
mkdir -p ~/dev
cd ~/dev
# git clone git@github.com:9600dev/rustdown.git  # if available
# Otherwise restore from backup

# Symlink rustdown into trading-tools
ln -s ~/dev/rustdown /opt/trading-tools/rustdown
```

### 6. Install llmvm_lite (see CRITICAL section below)

Restore `/opt/trading-tools/llmvm_lite/` from backup. This is **not** the same as `pip install llmvm-cli`.

### 7. Set environment variables

Edit `~/.zshrc` and add the exports listed in [Environment Variables](#environment-variables).

### 8. Build and start

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
cd /opt/trading-tools/news

# Full build + start
./docker.sh -g

# Or manually:
./docker.sh -b    # Build image (stages llmvm_lite + rustdown into build context)
./docker.sh -u    # Start container
```

### 9. Verify

```bash
curl -s http://127.0.0.1:8089/v1/health | python3 -m json.tool
# Should show: "status": "ok", "llm_enabled": true, "llm_verified": true
```

---

## The llmvm_lite Gotcha (CRITICAL)

This is the single biggest trap in the entire setup. There are **two different issues** and both must be addressed.

### Issue 1: Wrong Package

The news service imports `from llmvm_lite.llm import acall`, `from llmvm_lite.provider_utils`, etc. The Python module must be named `llmvm_lite`.

**WRONG:** `pip install llmvm-cli` — this installs a package whose Python module is `llmvm` (not `llmvm_lite`). It's a completely different codebase (v1.5.6, by the same author but different module namespace). You'll get `ModuleNotFoundError: No module named 'llmvm_lite'`.

**RIGHT:** The correct package is a hatchling-based project at `/opt/trading-tools/llmvm_lite/` with:
- `pyproject.toml` containing `name = "llmvm_lite"`, `version = "0.0.1"`
- Build system: `hatchling`
- The actual `llmvm_lite/` Python package directory
- **A `README.md` file MUST exist** (even if empty) — hatchling's sdist builder requires it per `pyproject.toml`

This package originally came from Joel's Meta-internal `fbsource/fbcode/scripts/joelp/llmvm_lite`. We have a local copy. **Back this up.**

### Issue 2: Llama API Passthrough URL (MUST PATCH)

Even with the correct `llmvm_lite` package installed, LLM calls will fail with `401 Authentication Error` because:

In `llmvm_lite/providers/anthropic_provider.py`, the `_execute_api` method creates the Anthropic client with:

```python
client = AsyncAnthropicPassthrough(
    api_key=resolved_key,
    base_url="https://api.llama.com/experimental/passthrough/anthropic",  # ← WRONG
    ...
)
```

This hardcodes the **Meta Llama API passthrough endpoint**. Joel's internal setup routes Anthropic calls through Meta's Llama API, which requires a Llama API key, not an Anthropic API key. With a standard Anthropic API key, this gives a `401`.

**Fix:** Change the `base_url` to the real Anthropic API:

```python
client = AsyncAnthropicPassthrough(
    api_key=resolved_key,
    base_url="https://api.anthropic.com",  # ← CORRECT
    ...
)
```

**The patch must be applied in TWO places:**

1. **On-disk source** (`/opt/trading-tools/llmvm_lite/llmvm_lite/providers/anthropic_provider.py`) — so future `docker.sh -g` rebuilds and `pip install` pick it up
2. **Inside the running container** (if you pip-installed into an existing container without rebuilding the image)

Quick one-liner for the on-disk fix:
```bash
sed -i '' 's|base_url="https://api.llama.com/experimental/passthrough/anthropic"|base_url="https://api.anthropic.com"|' \
  /opt/trading-tools/llmvm_lite/llmvm_lite/providers/anthropic_provider.py
```

### If the Docker image is rebuilt from scratch

The `docker.sh -b` build script stages `llmvm_lite` from `/opt/trading-tools/llmvm_lite/` (or `$LLMVM_SRC`) into the Docker build context, then the Dockerfile runs `pip install /tmp/llmvm_lite`. **If the on-disk source has the patch applied, a fresh image build will include it automatically.** No post-build patching needed.

### If pip-installing into a running container (ephemeral fix)

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"

# Copy package into container
docker cp /opt/trading-tools/llmvm_lite news-news-1:/tmp/llmvm_lite_pkg

# Ensure README.md exists (hatchling requirement)
docker exec -u root news-news-1 bash -c "echo '# llmvm_lite' > /tmp/llmvm_lite_pkg/README.md && chown -R news:news /tmp/llmvm_lite_pkg"

# Install without pulling dependencies (they're already in the container)
docker exec news-news-1 bash -c 'source /home/news/.venv/bin/activate && pip install --no-deps /tmp/llmvm_lite_pkg'

# Verify
docker exec news-news-1 bash -c 'source /home/news/.venv/bin/activate && python3 -c "from llmvm_lite.llm import acall; print(\"llmvm_lite OK\")"'

# Restart container to pick up new code
docker restart news-news-1
```

**⚠️ This pip install is ephemeral — it survives `docker restart` but is lost on `docker compose down && docker compose up` (which recreates the container).** A full image rebuild (`docker.sh -b`) bakes it in permanently.

---

## SSH Tunnel from Hatch VM

The Hatch VM (Pit's runtime) connects to the news service via SSH tunnel:

```bash
ssh -f -N -L 8089:localhost:8089 hatch@100.91.155.72
```

A more robust version with auto-restart lives at `~/workspace/scripts/mmr-tunnel.sh` on the Hatch VM. It forwards multiple ports:

| Port | Service |
|------|---------|
| 8089 | News service HTTP |
| 42001 | MMR trader RPC |
| 42002 | MMR ticker PubSub |
| 42003 | MMR data RPC |
| 42005 | MMR strategy RPC |
| 42006 | MMR strategy MessageBus |

**Gotcha:** The tunnel dies frequently. When news service calls from Hatch fail with connection refused, restart the tunnel first:
```bash
pkill -f "ssh.*8089" 2>/dev/null; sleep 1
ssh -f -N -L 8089:localhost:8089 hatch@100.91.155.72
```

When calling the news API from the Hatch VM, use `NO_PROXY=localhost,127.0.0.1` to bypass any HTTP proxy:
```bash
NO_PROXY=localhost,127.0.0.1 curl -s http://127.0.0.1:8089/v1/health
```

---

## Verification

### Health check
```bash
curl -s http://127.0.0.1:8089/v1/health | python3 -m json.tool
```
Expected:
```json
{
    "status": "ok",
    "version": "0.1.0",
    "llm_enabled": true,
    "llm_verified": true,
    "mathpix_enabled": false
}
```

- `llm_verified: true` — Anthropic key works AND llmvm_lite base_url is correct
- `llm_verified: null` — hasn't been tested yet (first call will verify)
- `llm_enabled: false` — no API key found in container env

### Scrape test (no LLM)
```bash
curl -s -X POST http://127.0.0.1:8089/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://en.wikipedia.org/wiki/Nvidia"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'ok={d[\"ok\"]} chars={len(d.get(\"article\",{}).get(\"markdown\",\"\"))}')
"
```

### Scrape + LLM extraction test
```bash
curl -s --max-time 120 -X POST http://127.0.0.1:8089/v1/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://en.wikipedia.org/wiki/Nvidia","extract":"Who is the CEO?"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
ext=d.get('extraction',{})
print(f'ok={d[\"ok\"]} extraction={ext.get(\"text\",\"NONE\")[:200]}')
"
```

### Search test
```bash
curl -s -X POST http://127.0.0.1:8089/v1/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"NVIDIA earnings 2026","engine":"google"}' | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'ok={d[\"ok\"]} results={len(d.get(\"results\",[]))}')
"
```

---

## Container Lifecycle & Gotchas

### `docker restart` vs `docker compose down/up`

| Action | Container filesystem | pip installs | Patched files |
|--------|---------------------|-------------|---------------|
| `docker restart news-news-1` | ✅ Preserved | ✅ Preserved | ✅ Preserved |
| `docker compose down && up` | ❌ Recreated | ❌ Lost | ❌ Lost |
| `docker.sh -g` (full rebuild) | ❌ New image | ✅ Baked in | ✅ If on-disk source patched |

**Rule of thumb:** Use `docker restart` for quick restarts. Only use `docker compose down/up` if you need to change env vars or compose config. Use `docker.sh -g` for full rebuilds.

### Mac /tmp sticky bit

Docker commands that copy files from `/tmp` on macOS fail with `Permission denied` due to the sticky bit. Use `/var/tmp/` or `~/` instead:

```bash
# FAILS:
docker cp /tmp/myfile.py news-news-1:/tmp/

# WORKS:
cp /tmp/myfile.py ~/myfile.py
docker cp ~/myfile.py news-news-1:/tmp/
```

### Docker Desktop path

Docker CLI is at `/Applications/Docker.app/Contents/Resources/bin/`. Not in the default PATH for non-interactive shells. Always set:
```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
```

### Running Docker as garethreakes

Docker Desktop runs under the garethreakes user. From the `hatch` SSH user:
```bash
sudo -u garethreakes bash -c "
  export PATH=/Applications/Docker.app/Contents/Resources/bin:\$PATH
  export HOME=/Users/garethreakes
  docker ps
"
```

Or use the wrapper script:
```bash
/opt/trading-tools/docker-cmd.sh docker ps
```

---

## Configuration Reference

### news.yaml (key settings)

```yaml
# HTTP server
http_port: 8089
http_request_timeout_seconds: 120

# LLM — the critical triple (must match each other)
llm_enabled: true
llm_provider: anthropic               # "anthropic" or "openai"
llm_base_url: https://api.anthropic.com  # Must match provider
llm_api_key_env: ANTHROPIC_API_KEY     # Env var to read for the key
llm_model_name: claude-opus-4-7    # Model to use
llm_max_completion_tokens: 8000
llm_timeout_seconds: 120

# Search
# SERPAPI_API_KEY env var enables Google search; without it, falls back to DuckDuckGo

# Playwright
playwright_headless: true
playwright_max_concurrency: 4

# Mathpix (optional PDF conversion)
mathpix_enabled: true                  # Set false if no key
```

### docker-compose.yml (key settings)

```yaml
services:
  news:
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}   # From host shell
      SERPAPI_API_KEY: ${SERPAPI_API_KEY:-}
      TZ: ${TIME_ZONE:-America/New_York}
    ports:
      - "127.0.0.1:${NEWS_HOST_PORT:-8089}:8089"  # Localhost only
    shm_size: "2gb"     # Playwright needs this
    mem_limit: "4gb"    # Container memory cap
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'llmvm_lite'` | Wrong package installed (llmvm-cli) or pip install lost after container recreation | Reinstall correct package from `/opt/trading-tools/llmvm_lite/` |
| `401 Authentication Error` from LLM extraction | llmvm_lite sending Anthropic key to Llama API passthrough | Patch `anthropic_provider.py` base_url (see CRITICAL section) |
| `401 Authentication Error` (key is correct, base_url is patched) | Container has old code in memory | `docker restart news-news-1` |
| `llm_enabled: false` in health check | No API key in container env | Check `.zshrc` exports, restart with `zsh -lic "docker compose up -d"` |
| `llm_verified: null` | Key not tested yet | Make a scrape request with `extract` param |
| `/v1/scrape` returns `status: blocked` | Target site has bot protection | Not a service issue — try a different URL |
| Connection refused on port 8089 from Hatch | SSH tunnel died | Restart: `ssh -f -N -L 8089:localhost:8089 hatch@100.91.155.72` |
| `hatchling` build fails | Missing `README.md` in llmvm_lite dir | `echo '# llmvm_lite' > /opt/trading-tools/llmvm_lite/README.md` |
| `docker cp` permission denied from `/tmp` | macOS sticky bit | Copy to `~/` or `/var/tmp/` first |

---

## Backup Checklist

If the Mac Mini dies, you need:

1. **This README** (in `garethreakes/mmr` on GitHub)
2. **News service source** — clone from `9600dev/news` (private, need access)
3. **MMR source** — clone from `9600dev/mmr` (public)
4. **llmvm_lite package** — `/opt/trading-tools/llmvm_lite/` (**local only, no public repo**)
   - Back up the entire directory including the patched `anthropic_provider.py`
   - The `llmvm_lite_minimal.zip` that G provided is the canonical source
5. **rustdown** — check Joel's GitHub or restore from backup
6. **API keys** — stored in password manager, not in this doc
7. **SSH key for GitHub** — `id_ed25519_github` (generate new + add to GitHub if lost)
8. **Tailscale** — re-authenticate to tailnet

---

*Last updated: 2026-05-31 by Pit ⛏️*
