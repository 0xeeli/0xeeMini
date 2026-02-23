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
├── main.py           — point d'entrée, APScheduler (60s/5min/30min/mensuel)
├── brain_link.py     — orchestration cerveaux (réflexe GGUF + Claude + Samouraï)
├── constitution.py   — prompts Constitution + réflexe
├── config.py         — chargement ~/.config/0xeeMini/.env
├── core.py           — SQLite, BootGuardian, logging
├── profit_engine.py  — Solana/USDC, transferts, settlement mensuel
├── hustle_engine.py  — génération insights HN/CoinGecko (GGUF > Claude)
├── hustle_api.py     — FastAPI : /audit, /catalog, /buy, /health, /status
└── github_auditor.py — fetch commits GitHub + calcul métriques
```

## Protocole HTTP 402 A2A

```
POST /audit
  → 402 { error: "payment_required", price_usdc: 0.50, memo: "0xee:{content_id}" }
  → buyer paie on-chain, renvoie avec X-Payment-Tx header (ou tx_signature body)
  → 200 { bullshit_score, verdict, recommendation, ... }
```

## Variables d'environnement clés

```bash
CLAUDE_API_KEY=sk-ant-...          # Primaire pour audits et constitution
CLAUDE_BUDGET_MONTHLY_USD=5.0      # Budget mensuel strict
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
