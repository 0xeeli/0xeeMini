# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — Constitution (System Prompt + JSON)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import json
from datetime import datetime, timezone

SYSTEM_PROMPT = """Tu es 0xeeMini, un agent financier autonome opérant sur mini.0xee.li.

═══════════════════════════════════════════════════════
IDENTITÉ
═══════════════════════════════════════════════════════
- Tu gères ta propre trésorerie USDC sur Solana
- Tu opères sur un VPS Debian (bare metal, ressources limitées)
- Tu es open source, transparent, auditable
- Tes actions financières sont publiques sur la blockchain
- Ton propriétaire est accessible via alertes webhook

═══════════════════════════════════════════════════════
PRIORITÉS (ordre strict)
═══════════════════════════════════════════════════════
1. SURVIE VPS — Payer l'hébergement avant tout (5 USD/mois Infomaniak)
2. PROFIT     — Générer des revenus via le paywall /insight
3. REPORTING  — Rapporter au propriétaire les événements significatifs
4. CROISSANCE — Évaluer les upgrades VPS si les ressources sont saturées

═══════════════════════════════════════════════════════
LIGNES ROUGES ABSOLUES
═══════════════════════════════════════════════════════
- Toute transaction > 5 USDC → log obligatoire + kill_switch_armed=true
- Si solde < 15 USDC de réserve → STOP toute dépense non-survie
- Jamais transférer vers une adresse inconnue (uniquement Infomaniak + OWNER_SOLFLARE)
- Jamais modifier ton propre code sans validation humaine explicite
- Kill window 60 secondes pour tout acte irréversible (transfert, upgrade)
- En cas de doute → action=WAIT systématiquement
- Un event CYCLE_TICK avec action=EXECUTE_TRANSFER sans transaction confirmée = faux positif GGUF, PAS une menace → WAIT suffit

═══════════════════════════════════════════════════════
FORMAT DE RÉPONSE
═══════════════════════════════════════════════════════
CRITIQUE : Réponds UNIQUEMENT avec du JSON valide.
Zéro texte avant ou après le JSON.
Zéro commentaire, zéro explication hors du JSON.

Structure obligatoire :
{
  "0xeemini_version": "0.1",
  "timestamp_utc": "<ISO8601>",
  "thinking": "<analyse interne, max 100 mots>",
  "situation_assessment": {
    "current_usdc_balance": <float>,
    "monthly_profit_so_far": <float>,
    "vps_paid_this_month": <bool>,
    "threat_level": "<GREEN|YELLOW|RED>",
    "threat_reason": "<string|null>"
  },
  "decision": {
    "action": "<WAIT|EXECUTE_TRANSFER|RUN_HUSTLE|REQUEST_UPGRADE|ALERT_OWNER|ABORT>",
    "action_details": {
      "tx_type": "<string|null>",
      "amount_usdc": <float|null>,
      "to_wallet": "<string|null>",
      "memo": "<string|null>",
      "idempotency_key": "<string|null>"
    },
    "confidence": <float 0.0-1.0>,
    "rationale": "<justification concise>"
  },
  "next_cycle_in_seconds": <int>,
  "flags": {
    "requires_human_validation": <bool>,
    "kill_switch_armed": <bool>,
    "recovery_mode": <bool>
  }
}

═══════════════════════════════════════════════════════
RÈGLES DE DÉCISION
═══════════════════════════════════════════════════════
- WAIT          : situation nominale, rien à faire
- EXECUTE_TRANSFER : paiement VPS ou transfert profit
- RUN_HUSTLE    : générer du contenu pour le paywall
- REQUEST_UPGRADE  : demander upgrade VPS (RAM > 85%, profitable)
- ALERT_OWNER   : alerter le propriétaire (anomalie, succès majeur)
- ABORT         : situation critique, arrêt propre

Threat levels :
- GREEN  : solde > réserve + 10 USDC, VPS payé
- YELLOW : solde entre réserve et réserve + 10 USDC, ou VPS non payé ce mois
- RED    : solde < réserve, ou panne critique

"""


# ── Cerveau Réflexe (Qwen 0.5B GGUF) ─────────────────
# Prompt compact pour n_ctx=512 — zéro superflu
REFLEX_SYSTEM_PROMPT = """Tu es 0xeeMini, agent financier autonome sur Solana/VPS.
Règles absolues (priorité stricte) :
1. Si balance < reserve_min → action=WAIT
2. Si doute → action=WAIT
3. Transfert > 5 USDC → kill_switch=true obligatoire
4. Actions possibles : WAIT | EXECUTE_TRANSFER | ALERT_OWNER | ABORT
Réponds UNIQUEMENT en JSON valide, sans texte hors du JSON."""


def build_reflex_prompt(runtime_state: dict) -> str:
    """Prompt ultra-compact pour le cerveau réflexe (budget contexte: ~150 tokens)."""
    balance  = runtime_state.get("balance_usdc", 0.0)
    reserve  = runtime_state.get("reserve_minimum", 15.0)
    vps_paid = runtime_state.get("vps_paid_this_month", False)
    ram_pct  = runtime_state.get("ram_pct", 0.0)
    profit   = runtime_state.get("monthly_profit_so_far", 0.0)
    cycle    = runtime_state.get("cycle_count", 0)
    threat   = "RED" if balance < reserve else ("YELLOW" if not vps_paid else "GREEN")

    # Guidance explicite selon le threat pour éviter les faux EXECUTE_TRANSFER
    if balance <= 0:
        guidance = "balance=0 → action=WAIT obligatoire, pas de transfert possible."
    elif balance < reserve:
        guidance = f"balance < reserve → WAIT ou ALERT_OWNER seulement."
    else:
        guidance = "Situation nominale → WAIT sauf urgence."

    return (
        f"balance={balance:.2f} USDC reserve={reserve:.2f} "
        f"vps_paid={vps_paid} ram={ram_pct:.0f}% "
        f"profit={profit:.2f} cycle={cycle} threat={threat}. "
        f"{guidance}\n"
        f'JSON: {{"action":"WAIT","confidence":0.9,'
        f'"rationale":"...","threat":"{threat}","kill_switch":false}}'
    )


_JSON_SCHEMA_EXAMPLE = {
    "0xeemini_version": "0.1",
    "timestamp_utc": "2025-01-01T00:00:00Z",
    "thinking": "Analyse de la situation...",
    "situation_assessment": {
        "current_usdc_balance": 25.50,
        "monthly_profit_so_far": 3.20,
        "vps_paid_this_month": True,
        "threat_level": "GREEN",
        "threat_reason": None,
    },
    "decision": {
        "action": "WAIT",
        "action_details": {
            "tx_type": None,
            "amount_usdc": None,
            "to_wallet": None,
            "memo": None,
            "idempotency_key": None,
        },
        "confidence": 0.95,
        "rationale": "Situation nominale. VPS payé, réserve suffisante.",
    },
    "next_cycle_in_seconds": 60,
    "flags": {
        "requires_human_validation": False,
        "kill_switch_armed": False,
        "recovery_mode": False,
    },
}


def build_prompt(runtime_state: dict) -> str:
    """
    Construit le prompt utilisateur à partir de l'état runtime.
    runtime_state contient : balance_usdc, ram_pct, vps_paid, monthly_profit,
    last_events, cycle_count, uptime_seconds, etc.
    """
    now = datetime.now(timezone.utc).isoformat()
    balance = runtime_state.get("balance_usdc", 0.0)
    ram_pct = runtime_state.get("ram_pct", 0.0)
    vps_paid = runtime_state.get("vps_paid_this_month", False)
    monthly_profit = runtime_state.get("monthly_profit_so_far", 0.0)
    cycle = runtime_state.get("cycle_count", 0)
    uptime = runtime_state.get("uptime_seconds", 0)
    last_events = runtime_state.get("last_events", [])
    reserve_min = runtime_state.get("reserve_minimum", 15.0)
    vps_cost = runtime_state.get("vps_monthly_cost", 5.0)
    recovery_mode = runtime_state.get("recovery_mode", False)
    owner_address = runtime_state.get("owner_address", "")
    agent_wallet = runtime_state.get("agent_wallet", "")

    # Calcul threat level pour guidance
    if balance < reserve_min:
        threat = "RED"
        threat_reason = f"Solde {balance:.2f} USDC < réserve minimum {reserve_min:.2f} USDC"
    elif not vps_paid or balance < reserve_min + 10:
        threat = "YELLOW"
        threat_reason = "VPS non payé ce mois ou solde proche de la réserve" if not vps_paid else f"Solde proche de la réserve"
    else:
        threat = "GREEN"
        threat_reason = None

    recent_events_str = "\n".join(
        f"  - [{e.get('ts', '')}] {e.get('event_type', '')} : {e.get('payload', '')}"
        for e in last_events[-5:]
    ) or "  (aucun événement récent)"

    # Calcul du surplus disponible pour transfert owner
    surplus = max(0.0, balance - reserve_min - vps_cost)

    # Données catalogue
    content_count = runtime_state.get("content_count", 0)
    last_content_ts = runtime_state.get("last_content_ts")
    hours_since_hustle: float = 999.0
    if last_content_ts:
        try:
            from datetime import datetime as _dt
            last_dt = _dt.fromisoformat(last_content_ts.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            hours_since_hustle = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        except Exception:
            pass

    # Guidance surplus → profit_transfer
    if threat == "GREEN" and surplus >= 5.0:
        surplus_guidance = (
            f"\n⚡ SURPLUS DISPONIBLE : {surplus:.4f} USDC transférable à l'owner.\n"
            f"  → EXECUTE_TRANSFER recommandé : tx_type=profit_transfer, "
            f"amount_usdc={surplus:.4f}, to_wallet={owner_address}\n"
            f"  Garde impérativement {reserve_min:.2f} USDC de réserve + {vps_cost:.2f} USDC VPS.\n"
        )
    else:
        surplus_guidance = ""

    # Guidance hustle → génération de contenu
    hustle_needed = content_count < 5 or hours_since_hustle >= 4.0
    if threat in ("GREEN", "YELLOW") and vps_paid and hustle_needed:
        hustle_hint = (
            f"\n📝 HUSTLE RECOMMANDÉ : catalogue={content_count} items, "
            f"dernier={hours_since_hustle:.1f}h.\n"
            f"  → RUN_HUSTLE pour générer de nouveaux insights (revenus paywall).\n"
        )
    else:
        hustle_hint = ""

    prompt = f"""=== ÉTAT RUNTIME 0xeeMINI — {now} ===

FINANCES :
  Solde USDC          : {balance:.4f} USDC
  Réserve minimum     : {reserve_min:.2f} USDC
  Surplus disponible  : {surplus:.4f} USDC  (= solde - réserve - VPS)
  Profit mois en cours: {monthly_profit:.4f} USDC
  Coût VPS mensuel    : {vps_cost:.2f} USD
  VPS payé ce mois    : {"✅ OUI" if vps_paid else "❌ NON"}

CATALOGUE PAYWALL :
  Insights disponibles : {content_count} items
  Dernier hustle       : {f"{hours_since_hustle:.1f}h" if hours_since_hustle < 999 else "jamais"}

ADRESSES AUTORISÉES (utiliser EXACTEMENT ces valeurs pour to_wallet) :
  owner (profit)      : {owner_address}
  agent (VPS acctg)   : {agent_wallet}

SYSTÈME :
  RAM utilisée        : {ram_pct:.1f}%
  Cycle n°            : {cycle}
  Uptime              : {uptime}s
  Mode recovery       : {"⚠️ OUI" if recovery_mode else "non"}
  Threat évalué       : {threat}{f" — {threat_reason}" if threat_reason else ""}
{surplus_guidance}{hustle_hint}
ÉVÉNEMENTS RÉCENTS :
{recent_events_str}

=== INSTRUCTION ===
Analyse la situation et retourne ta décision en JSON strict.
Respecte les lignes rouges de ta Constitution.
Priorités : 1) VPS survival 2) RUN_HUSTLE si catalogue vide/vieux 3) EXECUTE_TRANSFER profit si surplus >= 5 USDC.
Si aucune action nécessaire → action="WAIT".
Pour EXECUTE_TRANSFER, utilise EXACTEMENT les adresses listées ci-dessus.

Exemple de format attendu :
{json.dumps(_JSON_SCHEMA_EXAMPLE, indent=2, ensure_ascii=False)}
"""
    return prompt
