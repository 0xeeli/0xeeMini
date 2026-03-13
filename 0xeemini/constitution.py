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

═══════════════════════════════════════════════════════
PRIORITÉS (ordre strict)
═══════════════════════════════════════════════════════
1. SURVIE VPS  — Payer l'hébergement avant tout (5 USD/mois Infomaniak)
2. PROFIT      — Générer des revenus via le paywall /insight
3. CROISSANCE  — Upgrade VPS + GGUF si surplus >= coût upgrade

═══════════════════════════════════════════════════════
RÈGLES ABSOLUES
═══════════════════════════════════════════════════════
- Toute transaction > 5 USDC → kill_switch_armed=true
- Jamais transférer vers une adresse inconnue (uniquement OWNER_SOLFLARE)
- En cas de doute → action=WAIT
- balance < reserve → WAIT uniquement, aucune dépense

═══════════════════════════════════════════════════════
FORMAT DE RÉPONSE
═══════════════════════════════════════════════════════
CRITIQUE : Réponds UNIQUEMENT avec du JSON valide.
Zéro texte avant ou après le JSON.

Structure obligatoire :
{
  "0xeemini_version": "0.1",
  "timestamp_utc": "<ISO8601>",
  "thinking": "<analyse interne, max 100 mots>",
  "situation_assessment": {
    "current_usdc_balance": <float>,
    "monthly_profit_so_far": <float>,
    "vps_paid_this_month": <bool>,
    "status": "<BOOTSTRAP|OPERATIONAL|PROFITABLE>"
  },
  "decision": {
    "action": "<WAIT|EXECUTE_TRANSFER|RUN_HUSTLE|REQUEST_UPGRADE>",
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
    "kill_switch_armed": <bool>
  }
}

═══════════════════════════════════════════════════════
RÈGLES DE DÉCISION
═══════════════════════════════════════════════════════
- WAIT             : rien à faire ce cycle
- EXECUTE_TRANSFER : paiement VPS ou transfert surplus owner
- RUN_HUSTLE       : générer du contenu pour le paywall
- REQUEST_UPGRADE  : upgrade VPS/GGUF (RAM > 85% ET surplus suffisant)

Statuts :
- BOOTSTRAP   : solde < réserve — générer des revenus, WAIT pour tout le reste
- OPERATIONAL : solde >= réserve — payer VPS, générer contenu
- PROFITABLE  : solde > réserve + VPS + surplus >= 5 USDC — distribuer à l'owner

"""


# ── Cerveau Réflexe (Qwen 0.5B GGUF) ─────────────────
# Prompt compact pour n_ctx=512 — zéro superflu
REFLEX_SYSTEM_PROMPT = """Tu es 0xeeMini, agent financier autonome sur Solana/VPS.
Règles absolues (priorité stricte) :
1. Si balance < reserve_min → action=WAIT
2. Si doute → action=WAIT
3. Transfert > 5 USDC → kill_switch=true obligatoire
4. Actions possibles : WAIT | EXECUTE_TRANSFER | RUN_HUSTLE | REQUEST_UPGRADE
Réponds UNIQUEMENT en JSON valide, sans texte hors du JSON."""


def build_reflex_prompt(runtime_state: dict) -> str:
    """Prompt ultra-compact pour le cerveau réflexe (budget contexte: ~150 tokens)."""
    balance  = runtime_state.get("balance_usdc", 0.0)
    reserve  = runtime_state.get("reserve_minimum", 15.0)
    vps_paid = runtime_state.get("vps_paid_this_month", False)
    ram_pct  = runtime_state.get("ram_pct", 0.0)
    profit   = runtime_state.get("monthly_profit_so_far", 0.0)
    cycle    = runtime_state.get("cycle_count", 0)
    status = (
        "BOOTSTRAP" if balance < reserve else
        ("PROFITABLE" if balance > reserve + 5.0 else "OPERATIONAL")
    )
    guidance = (
        "balance < reserve → WAIT uniquement." if balance < reserve
        else "Situation nominale → WAIT sauf action utile."
    )

    return (
        f"balance={balance:.2f} USDC reserve={reserve:.2f} "
        f"vps_paid={vps_paid} ram={ram_pct:.0f}% "
        f"profit={profit:.2f} cycle={cycle} status={status}. "
        f"{guidance}\n"
        f'JSON: {{"action":"WAIT","confidence":0.9,'
        f'"rationale":"...","status":"{status}","kill_switch":false}}'
    )


_JSON_SCHEMA_EXAMPLE = {
    "0xeemini_version": "0.1",
    "timestamp_utc": "2025-01-01T00:00:00Z",
    "thinking": "Analyse de la situation...",
    "situation_assessment": {
        "current_usdc_balance": 25.50,
        "monthly_profit_so_far": 3.20,
        "vps_paid_this_month": True,
        "status": "OPERATIONAL",
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
        "kill_switch_armed": False,
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

    # Statut opérationnel
    if balance < reserve_min:
        status = "BOOTSTRAP"
    elif balance > reserve_min + vps_cost + 5.0:
        status = "PROFITABLE"
    else:
        status = "OPERATIONAL"

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
    if status == "PROFITABLE" and surplus >= 5.0:
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
    if status in ("OPERATIONAL", "PROFITABLE") and hustle_needed:
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
  Statut              : {status}
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
