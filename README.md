# 0xeeMini

**The smallest autonomous AI agent on earth.**

0.5B model · 2GB RAM · $10/month total costs · Solana mainnet.
It audits GitHub repos for USDC, pays its own bills, and earns its own brain upgrades.
No human operator after deploy.

🌐 **[mini.0xee.li](https://mini.0xee.li)** · 🐦 **[@0xeeMini](https://x.com/0xeeMini)**

---

## The Concept

```
Start:   0.5B GGUF · $5/mo VPS · $5/mo Claude API = $10/mo total
Earn $20/mo → upgrade to 3B model · 4GB VPS
Earn $40/mo → upgrade to 7B model · 8GB VPS
```

The agent sells intelligence services for USDC on Solana.
Every dollar above operating costs goes to the owner.
Every time it earns enough, it gets a smarter brain.
**The more it earns, the smarter it becomes.**

---

## Services

| Endpoint | Price | Description |
|----------|-------|-------------|
| `POST /audit` | 0.50 USDC | GitHub repo audit — bullshit score 0–100 |
| `POST /audit/batch` | 1.50 USDC | Up to 5 repos in one transaction |
| `GET /catalog` | 0.10 USDC | AI-curated crypto/tech insights |

Payment via **HTTP 402** — no API key, no subscription, just USDC on Solana.

---

## HTTP 402 Flow (Machine-to-Machine)

```bash
# Step 1 — Request (no payment yet)
curl -X POST https://mini.0xee.li/audit \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "bitcoin/bitcoin"}'

# → 402 Payment Required
# {
#   "error": "payment_required",
#   "price_usdc": 0.50,
#   "wallet": "ApNJDryGBtkvbHBji8CQ2afC4Dq9W9qn93iuvRrSXZHh",
#   "memo": "0xee:a3f9c2b1"
# }

# Step 2 — Send 0.50 USDC on Solana (any wallet)

# Step 3 — Retry with tx signature
curl -X POST https://mini.0xee.li/audit \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "bitcoin/bitcoin", "tx_signature": "5abc...xyz"}'

# → 200 OK
# {
#   "bullshit_score": 15,
#   "verdict": "INVEST",
#   "recommendation": "Solid codebase, real commit history",
#   "red_flags": [],
#   "proof_hash": "a3f9c2b1d0e87f4...",
#   "proof_verify_url": "https://mini.0xee.li/proof/a3f9c2b1"
# }
```

---

## Solana Actions (Blinks)

Pay directly from any Solana wallet — no code required.

```
https://dial.to/?action=solana-action%3Ahttps%3A%2F%2Fmini.0xee.li%2Faudit%2Faction
```

Supports Phantom, Backpack, and any Blinks-compatible wallet.

---

## Architecture

```
0xeemini/
├── main.py             — APScheduler: 60s reflex cycle, 5min audit queue, monthly settlement
├── brain_link.py       — Brain orchestration (GGUF reflex + Claude Haiku + Samouraï fallback)
├── constitution.py     — System prompts + reflex normalization guards
├── hustle_api.py       — FastAPI: all HTTP endpoints + Solana Actions
├── profit_engine.py    — USDC transfers, monthly settlement, upgrade evaluation
├── hustle_engine.py    — Insight generation (HN + CoinGecko → structured JSON)
├── github_auditor.py   — GitHub commit analysis + bullshit scoring
├── proof_of_compute.py — SHA256 proof on every audit result
├── telegram_bot.py     — Telegram bot for owner alerts + /demo command
├── core.py             — SQLite, BootGuardian, logging
└── config.py           — ~/.config/0xeeMini/.env loader
```

**Two GGUF models, never loaded simultaneously (2GB RAM constraint):**

| Role | Model | RAM | Context |
|------|-------|-----|---------|
| Reflex brain (every 60s) | qwen2.5-0.5B-instruct-q4_k_m | ~400MB | 512 tokens |
| Samouraï (audit fallback) | qwen2.5-coder-1.5B-instruct-q4_k_m | ~900MB | 1024 tokens |

Claude Haiku is the **primary audit brain** (0 RAM, ~$0.001/audit). Samouraï activates only if Claude API is unavailable.

---

## Agent Discovery

```
GET /.well-known/agent.json    — A2A agent card (ERC-8004)
GET /.well-known/actions.json  — Solana Actions manifest
GET /.well-known/ai-plugin.json — ChatGPT plugin manifest
GET /openapi.json              — OpenAPI spec
GET /status                    — Live agent telemetry (JSON)
```

---

## Run Your Own

### Requirements

- VPS with 2GB RAM (Debian/Ubuntu)
- Python 3.11+
- Solana wallet with USDC + a little SOL for fees
- Claude API key (Anthropic)

### Setup

```bash
git clone https://github.com/your-username/0xeeMini
cd 0xeeMini

# Configure
cp .env.example ~/.config/0xeeMini/.env
# Edit ~/.config/0xeeMini/.env with your keys

# Deploy to VPS
./mini deploy

# Download GGUF models on VPS
./mini download-model        # 0.5B reflex brain (~400MB)
./mini download-audit-model  # 1.5B Samouraï (~900MB)

# Install llama.cpp (compile on VPS, ~10 min)
./mini install-brain
```

### CLI

```bash
./mini deploy    # rsync + pip install + restart
./mini status    # service status + RAM + last logs
./mini logs      # tail -f live logs
./mini backup    # download SQLite DB with MD5 check
./mini wallet    # USDC balance
```

### Environment Variables

See [`.env.example`](.env.example) for full reference. Key variables:

```bash
OXEEMINI_WALLET_PUBLIC_KEY=...   # Agent Solana wallet
OXEEMINI_WALLET_PRIVATE_KEY=...  # ⚠️ Never commit this
CLAUDE_API_KEY=...               # Anthropic API key
CLAUDE_BUDGET_MONTHLY_USD=5.0   # Hard monthly cap
RESERVE_MINIMUM_USDC=10.0       # 2-month operating buffer
```

---

## Financial Model

```
Monthly costs:  $5 VPS + $5 Claude API = $10 total
Reserve:        $10 USDC (never spent)
Monthly surplus = balance − $10 → transferred to owner wallet
```

Stage unlocks (triggered automatically when monthly surplus covers next stage):

| Stage | Monthly earnings | Brain | VPS |
|-------|-----------------|-------|-----|
| Minimal ← *now* | $10 | 0.5B GGUF | 2GB |
| Growth | $20 | 3B GGUF | 4GB |
| Scale | $40 | 7B GGUF | 8GB |

---

## Tests

```bash
pytest tests/ -v
```

---

## License

CC0 — public domain. Fork it, run your own, build on top.
