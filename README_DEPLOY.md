# 0xeeMini — Guide de déploiement
**Platform** : https://mini.0xee.li

Agent autonome Python — gère sa trésorerie USDC sur Solana, paie son hébergement, transfère ses profits.

---

## Déploiement en 5 étapes

### 1. Obtenir le projet

```bash
git clone <repo-url> /home/debian/0xeemini
# ou : copier via mini deploy depuis la machine locale
```

### 2. Configurer les secrets

```bash
cd /home/debian/0xeemini
bash setup_secrets.sh
nano ~/.config/0xeeMini/.env
```

Valeurs minimales obligatoires :
- `OXEEMINI_WALLET_PUBLIC_KEY` — clé publique Solana de l'agent
- `OXEEMINI_WALLET_PRIVATE_KEY` — clé privée Base58 (garder secret absolu)
- `OWNER_SOLFLARE_ADDRESS` — adresse Solflare destination des profits
- `LOCAL_SSH_HOST` — IP de ta machine de dev (pour Ollama)

### 3. Installer les dépendances

```bash
pip install -r requirements.txt --user
```

### 4. Déployer via CLI (depuis la machine locale)

```bash
./mini deploy
```

### 5. Vérifier

```bash
./mini status
```

Tu dois voir 0xeeMini respirer : cycle toutes les 60s, logs verts.

---

## Structure des chemins

| Chemin | Rôle |
|--------|------|
| `/home/debian/0xeemini/` | Code source (git) |
| `~/.config/0xeeMini/.env` | Secrets (hors git, chmod 600) |
| `~/.local/share/0xeemini/state.db` | Base SQLite de l'agent |
| `~/.local/share/0xeemini/logs/agent.log` | Logs rotatifs |

## Service systemd

```bash
# Activation
mkdir -p ~/.config/systemd/user/
cp 0xeemini.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now 0xeemini.service
loginctl enable-linger debian

# Commandes utiles
systemctl --user status 0xeemini.service
journalctl --user -u 0xeemini.service -f
```

## CLI mini (machine locale)

```bash
./mini deploy      # Déploie + redémarre
./mini status      # Statut service + RAM + logs
./mini logs        # Logs temps réel
./mini backup      # Télécharge la DB avec vérif MD5
./mini wallet      # Solde USDC + dernières TX
./mini stop        # Arrête l'agent
./mini start       # Démarre l'agent
./mini setup-vps   # Configure les secrets sur le VPS
```

## Architecture

```
[Machine locale]              [VPS Debian — mini.0xee.li]
  Ollama (brain)    ←SSH──    BrainLink
  mini CLI          ──SSH──►  0xeeMini agent
                              ├── HustleAPI :8000
                              ├── ProfitEngine (USDC)
                              └── state.db (SQLite)
```

---
*Licence MIT — https://mini.0xee.li*
