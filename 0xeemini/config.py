# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — Config Loader
# https://mini.0xee.li
# ─────────────────────────────────────────────────────
# Charge les secrets depuis ~/.config/0xeeMini/.env
# Jamais depuis le répertoire projet
# ─────────────────────────────────────────────────────

import os
from pathlib import Path
from dotenv import load_dotenv


def load_config() -> dict:
    """
    Charge la config depuis ~/.config/0xeeMini/.env
    Fallback : variables d'environnement système (pour CI/CD futur)
    Ne charge JAMAIS depuis le répertoire projet.
    """
    config_path = Path.home() / ".config" / "0xeeMini" / ".env"

    if not config_path.exists():
        raise FileNotFoundError(
            f"\n❌ Config introuvable : {config_path}\n"
            f"   Lance d'abord : bash setup_secrets.sh\n"
            f"   Puis remplis : nano {config_path}\n"
        )

    load_dotenv(dotenv_path=config_path, override=False)

    cfg = {
        # Wallets
        "wallet_public":     _require("OXEEMINI_WALLET_PUBLIC_KEY"),
        "wallet_private":    _require("OXEEMINI_WALLET_PRIVATE_KEY"),
        "owner_address":     _require("OWNER_SOLFLARE_ADDRESS"),

        # Solana
        "solana_rpc":        os.getenv("SOLANA_RPC_URL",
                             "https://api.mainnet-beta.solana.com"),

        # Cerveau local
        "local_ssh_host":    os.getenv("LOCAL_SSH_HOST", ""),
        "local_ssh_user":    os.getenv("LOCAL_SSH_USER", "pankso"),
        "local_ssh_port":    int(os.getenv("LOCAL_SSH_PORT", "22")),
        "ollama_port":       int(os.getenv("OLLAMA_PORT", "11434")),
        "brain_model":       os.getenv("BRAIN_MODEL", "qwen2.5-coder:7b"),

        # Fallback LLM
        "claude_api_key":    os.getenv("CLAUDE_API_KEY", ""),
        "claude_budget":     float(os.getenv("CLAUDE_BUDGET_MONTHLY_USD", "2.0")),

        # Alertes
        "webhook_url":       os.getenv("WEBHOOK_ALERT_URL", ""),

        # Finance
        "reserve_minimum":   float(os.getenv("RESERVE_MINIMUM_USDC", "15.0")),
        "price_per_insight": float(os.getenv("PRICE_PER_INSIGHT_USDC", "0.10")),
        "vps_monthly_cost":  float(os.getenv("VPS_MONTHLY_COST_USD", "5.00")),
        "current_vps_plan":  os.getenv("CURRENT_VPS_PLAN", "2GB"),

        # API
        "api_host":          os.getenv("API_HOST", "0.0.0.0"),
        "api_port":          int(os.getenv("API_PORT", "8000")),
        "platform_url":      os.getenv("PLATFORM_URL", "https://mini.0xee.li"),
    }

    return cfg


def _require(key: str) -> str:
    val = os.getenv(key, "")
    if not val or val.startswith("REMPLACER_"):
        raise ValueError(
            f"\n❌ Variable obligatoire non configurée : {key}\n"
            f"   Édite : ~/.config/0xeeMini/.env\n"
        )
    return val


# Instance globale — importée par tous les modules
# Usage : from oxeemini.config import CFG
CFG = load_config()
