#!/usr/bin/env bash
# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — Initialisation des secrets
# https://mini.0xee.li
# À exécuter UNE SEULE FOIS sur chaque machine (local + VPS)
# Usage : bash setup_secrets.sh
# ─────────────────────────────────────────────────────

set -e
CONFIG_DIR="$HOME/.config/0xeeMini"
ENV_FILE="$CONFIG_DIR/.env"

echo "🔐 Initialisation des secrets 0xeeMini..."

# Créer le répertoire config sécurisé
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# Copier le template si .env absent
if [ -f "$ENV_FILE" ]; then
    echo "⚠️  $ENV_FILE existe déjà. Abandon pour ne pas écraser."
    echo "   Édite manuellement : nano $ENV_FILE"
    exit 0
fi

cp .env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo ""
echo "✅ Template copié vers $ENV_FILE"
echo "🔑 Permissions : 600 (lecture owner uniquement)"
echo ""
echo "PROCHAINE ÉTAPE : remplis les valeurs dans le fichier :"
echo "  nano $ENV_FILE"
echo ""
echo "Valeurs minimales pour démarrer :"
echo "  OXEEMINI_WALLET_PUBLIC_KEY"
echo "  OXEEMINI_WALLET_PRIVATE_KEY"
echo "  OWNER_SOLFLARE_ADDRESS"
echo "  LOCAL_SSH_HOST"
