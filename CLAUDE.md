# CLAUDE.md — 0xeeMini

Guide de développement pour Claude Code travaillant sur ce projet.

## Contexte projet

**0xeeMini** est un agent financier autonome sur Solana (VPS Debian 2GB RAM, Infomaniak).
Il génère des revenus via un paywall d'insights (0.10 USDC) et des audits GitHub (0.50 USDC),
distribués via le protocole HTTP 402 A2A.

- VPS : `debian@193.108.54.70`
- Service : `systemctl --user status 0xeemini`
- Logs : `~/.local/share/0xeemini/logs/agent.log`
- DB : `~/.local/share/0xeemini/state.db`
- Config secrets : `~/.config/0xeeMini/.env`
- Deploy : `./mini deploy`

## Architecture Paths

### La Voie de la Survie (configuration actuelle — 2GB RAM)

Contraintes absolues : 2GB RAM total, 1 vCPU, swap limité.

| Composant | Modèle | n_ctx | RAM pic |
|-----------|--------|-------|---------|
| Cerveau réflexe | `qwen2.5-0.5b-instruct-q4_k_m.gguf` | 512 | ~400 Mo |
| Mode Samouraï (audit) | `qwen2.5-coder-1.5b-instruct-q4_k_m.gguf` | 1024 | ~900 Mo |

**Règles de survie :**
- Les deux modèles ne peuvent **jamais** tourner simultanément (crash OOM garanti).
- Chaque modèle est **éphémère** : `del llm` + `gc.collect()` immédiatement après l'inférence.
- Claude Haiku est le cerveau **primaire** pour les audits (0 RAM, coût ~$0.001/audit).
- Le Mode Samouraï n'est activé que si Claude API est indisponible (budget épuisé ou coupure réseau).
- `brain_model_path` → réflexe 0.5B (constitution, toutes les 60s)
- `brain_audit_model_path` → Samouraï 1.5B (github_audit, à la demande)

### La Voie du Confort (upgrade futur — 4GB RAM)

Débloqué quand le VPS est upgradé à 4GB RAM (plan suivant Infomaniak).

| Composant | Modèle | n_ctx | RAM pic |
|-----------|--------|-------|---------|
| Cerveau réflexe | `qwen2.5-0.5b-instruct-q4_k_m.gguf` | 512 | ~400 Mo |
| Mode Samouraï (audit) | `qwen2.5-coder-3b-instruct-q4_k_m.gguf` | 4096 | ~1.8 Go |

Avec 4096 tokens de contexte, le Samouraï peut analyser 20 commits complets
sans troncature, avec les patches complets → scores plus précis.

**Changements à faire lors de l'upgrade :**
1. `brain_audit_model_path` → pointer vers le 3B
2. `_analyze_samurai_sync()` → passer `n_ctx=4096`, `max_tokens=800`
3. `_preprocess_audit_payload()` → relâcher la troncature (12800 chars)
4. `./mini download-audit-model` → ajouter URL du 3B

## Structure des modules

```
0xeemini/
├── main.py             — point d'entrée, APScheduler (60s/5min/30min/mensuel)
├── brain_link.py       — orchestration cerveaux (réflexe GGUF + Claude + Samouraï)
├── constitution.py     — prompts Constitution + réflexe
├── config.py           — chargement ~/.config/0xeeMini/.env
├── core.py             — SQLite, BootGuardian, logging
├── profit_engine.py    — Solana/USDC, transferts, settlement mensuel
├── hustle_engine.py    — génération insights HN/CoinGecko (GGUF > Claude)
├── hustle_api.py       — FastAPI : tous les endpoints HTTP
├── github_auditor.py   — fetch commits GitHub + calcul métriques
├── proof_of_compute.py — SHA256 proof-of-compute sur chaque résultat d'audit
└── telegram_bot.py     — bot Telegram pour alertes owner + commandes admin
```

**Fichiers config :**
```
config/
└── agent.md   — YAML frontmatter ERC-8004 (A2A agent card) + documentation
```

## Endpoints HTTP (hustle_api.py)

```
GET  /health                    — healthcheck
GET  /status                    — état agent (RAM, cycle, wallet, etc.)
GET  /catalog                   — liste insights paywall (0.10 USDC/item)
GET  /insight/{content_id}      — insight débloqué après paiement 402
GET  /proof/{proof_id}          — proof-of-compute SHA256 d'un audit
GET  /reputation                — réputation on-chain de l'agent
POST /audit                     — audit GitHub (0.50 USDC) — HTTP 402 flow
POST /audit/batch               — audit batch 5 repos (1.50 USDC)
GET  /audit/cache/{repo_slug}   — résultat mis en cache
POST /access                    — accès générique après paiement
GET  /.well-known/agent.json    — identité A2A (ERC-8004 / agent card)
GET  /.well-known/actions.json  — manifest Solana Actions (Dialect/Blinks)
GET  /.well-known/ai-plugin.json — compat ChatGPT plugin manifest
GET  /openapi.json              — spec OpenAPI

# Solana Actions (Blinks)
OPTIONS /audit/action           — CORS preflight
GET     /audit/action           — metadata Blink (titre, description, input)
POST    /audit/action           — retourne unsigned tx USDC (wallet signe côté client)
OPTIONS /catalog/action         — CORS preflight
GET     /catalog/action         — metadata Blink catalog
```

## Protocole HTTP 402 A2A

```
POST /audit
  → 402 { error: "payment_required", price_usdc: 0.50, memo: "0xee:{content_id}" }
  → buyer paie on-chain, renvoie avec X-Payment-Tx header (ou tx_signature body)
  → 200 { bullshit_score, verdict, recommendation, sha256_proof, ... }
```

## Solana Actions (Blinks)

Implémenté pour Dialect / dial.to. Flow Blink :
```
GET  /audit/action              → métadonnées (titre, label, champ repo_url)
POST /audit/action              → { account: "<pubkey>" } → { transaction: "<base64 unsigned tx>" }
                                   Le wallet de l'user signe et broadcast côté client.
```

Validation Dialect : https://dial.to/developer?url=https://mini.0xee.li/audit/action&cluster=mainnet

**Important — CORS pour Blinks :** lighttpd gère **tout** le CORS (pas FastAPI).
`CORSMiddleware` a été retiré de FastAPI pour éviter les headers en double.
Config à maintenir dans `/etc/lighttpd/vhosts.conf` sur le VPS (non versionné) :
```
setenv.add-response-header = (
    "Access-Control-Allow-Origin"   => "*",
    "Access-Control-Allow-Methods"  => "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers"  => "Content-Type, Authorization, Content-Encoding, Accept-Encoding, x-action-version, x-blockchain-ids",
    "X-Action-Version"              => "2.1.3",
    "X-Blockchain-Ids"              => "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
)
```
Ce bloc doit être **directement dans `$HTTP["host"]`**, pas dans un sous-bloc `$HTTP["url"]`
(les `setenv` imbriqués ne s'appliquent pas aux réponses proxy sous lighttpd).

## GGUF False-Positive ABORT — Gardes

Le modèle 0.5B peut halluciner `EXECUTE_TRANSFER` ou `ABORT` sans justification.
Trois gardes dans `brain_link.py` + `constitution.py` :

1. **Fast-path bloqué si reflex=ABORT** : si le GGUF renvoie `ABORT`, on bypass
   le fast-path et Claude doit confirmer avant toute action critique.
2. **`_normalize_reflex_response()` — EXECUTE_TRANSFER → WAIT** : le GGUF ne
   connaît pas les adresses wallet → tout `EXECUTE_TRANSFER` du réflexe est
   converti en `WAIT` (log debug).
3. **`_normalize_reflex_response()` — ABORT → ALERT_OWNER** : si balance ≥ réserve,
   un `ABORT` du GGUF est converti en `ALERT_OWNER` (log warning) — faux positif.

## Variables d'environnement clés

```bash
CLAUDE_API_KEY=sk-ant-...          # Primaire pour audits et constitution
CLAUDE_BUDGET_MONTHLY_USD=5.0      # Budget mensuel
CLAUDE_THROTTLE_SECS=600           # Constitution appelée max 1x/10min
BRAIN_MODEL_PATH=~/0xeeMini/models/qwen2.5-0.5b-instruct-q4_k_m.gguf
BRAIN_AUDIT_MODEL_PATH=~/0xeeMini/models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf
PRICE_PER_AUDIT_USDC=0.50
PRICE_PER_INSIGHT_USDC=0.10
```

## Commandes CLI

```bash
./mini deploy            # rsync + pip install + restart service
./mini status            # état service + RAM + derniers logs
./mini logs              # tail -f en temps réel
./mini backup            # télécharge DB SQLite avec vérification MD5
./mini wallet            # solde USDC agent
./mini install-brain     # compile llama-cpp-python sur VPS (~10 min)
./mini download-model    # télécharge cerveau réflexe 0.5B (~400 Mo)
./mini download-audit-model  # télécharge Mode Samouraï 1.5B (~900 Mo)
```

## Tests

```bash
pytest tests/ -v                          # suite complète
pytest tests/test_github_auditor.py -v    # audits uniquement
```

Les tests utilisent `tests/conftest.py` qui injecte un mock config + DB temporaire.
Ne jamais charger `0xeemini.config` directement dans les tests (FileNotFoundError sur CI).
