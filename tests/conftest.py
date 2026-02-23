# ─────────────────────────────────────
# conftest.py — fixtures globales pytest
# ─────────────────────────────────────

import importlib
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

# Env minimales pour config.py
os.environ.setdefault("OXEEMINI_WALLET_PUBLIC_KEY", "Hz1Dfq6E1FArSETestXxxxxxxxxxxxxxxxxxxxxxxxx")
os.environ.setdefault("OXEEMINI_WALLET_PRIVATE_KEY", "1" * 64)
os.environ.setdefault("OWNER_SOLFLARE_ADDRESS", "ByEuwudJZ1vJyyogUabk7fTP68KvhtUJpNCkuVb4SRZ5")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Mock config module pour les tests — évite d'avoir besoin du .env local
_MOCK_CFG = {
    "wallet_public":    os.environ["OXEEMINI_WALLET_PUBLIC_KEY"],
    "wallet_private":   os.environ["OXEEMINI_WALLET_PRIVATE_KEY"],
    "owner_address":    os.environ["OWNER_SOLFLARE_ADDRESS"],
    "solana_rpc":       "https://api.mainnet-beta.solana.com",
    "claude_api_key":   "",
    "claude_budget":    5.0,
    "claude_throttle_secs": 600,
    "brain_model_path": "",
    "ollama_tunnel_port": 0,
    "local_ssh_host":   "",
    "local_ssh_user":   "pankso",
    "local_ssh_port":   22,
    "ollama_port":      11434,
    "brain_model":      "qwen2.5-coder:7b",
    "webhook_url":      "",
    "reserve_minimum":  15.0,
    "price_per_insight": 0.10,
    "price_per_audit":  0.50,
    "vps_monthly_cost": 5.00,
    "current_vps_plan": "2GB",
    "api_host":         "0.0.0.0",
    "api_port":         8000,
    "platform_url":     "https://mini.0xee.li",
}

# Pré-injecter le module config mocké avant tout autre import
_config_mod = ModuleType("0xeemini.config")
_config_mod.CFG = _MOCK_CFG
_config_mod.load_config = lambda: _MOCK_CFG
sys.modules["0xeemini.config"] = _config_mod

# Pré-injecter core avec un DB en mémoire
with patch("dotenv.load_dotenv"):
    _core_mod = importlib.import_module("0xeemini.core")

# Patch DB_PATH vers /tmp pour les tests
import tempfile
_tmp_db = Path(tempfile.mkdtemp()) / "test_state.db"
_core_mod.DB_PATH = _tmp_db
_core_mod.init_db()
