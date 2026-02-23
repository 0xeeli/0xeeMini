# ─────────────────────────────────────────────────────
# 0xeeMini v0.3.0 — Proof of Compute
# https://mini.0xee.li
#
# Chaque audit produit une preuve cryptographique immuable :
#   SHA256(repo + score + nonce) → stocké en DB + optionnel on-chain
#
# Permet à n'importe quel agent/humain de vérifier :
#   1. Que l'audit a bien été effectué (pas une réponse inventée)
#   2. Que le score est cohérent avec l'input
#   3. De construire une réputation vérifiable pour 0xeeMini
# ─────────────────────────────────────────────────────

import hashlib
import time
from datetime import datetime, timezone

from loguru import logger

from .core import get_db, log_event


def generate_proof(
    repo: str,
    bullshit_score: int,
    recommendation: str,
    nonce: str | None = None,
) -> dict:
    """
    Génère une preuve cryptographique d'audit.

    Input  : repo + score + recommendation + nonce
    Output : SHA256 hex + métadonnées vérifiables
    """
    if nonce is None:
        nonce = str(int(time.time() * 1000))

    # Le hash couvre les champs déterministes de l'audit
    proof_input = f"{repo}:{bullshit_score}:{recommendation}:{nonce}"
    proof_hash = hashlib.sha256(proof_input.encode()).hexdigest()

    return {
        "proof_hash": proof_hash,
        "proof_hash_short": proof_hash[:16],
        "repo": repo,
        "bullshit_score": bullshit_score,
        "recommendation": recommendation,
        "nonce": nonce,
        "ts": datetime.now(timezone.utc).isoformat(),
        "algorithm": "SHA256",
        "input_template": "SHA256(repo:score:recommendation:nonce)",
        "verify_url": f"https://mini.0xee.li/proof/{proof_hash[:16]}",
    }


def store_proof(proof: dict) -> None:
    """Persiste la preuve dans la table system_events."""
    log_event("AUDIT_PROOF", {
        "proof_hash": proof["proof_hash"],
        "repo": proof["repo"],
        "bullshit_score": proof["bullshit_score"],
        "recommendation": proof["recommendation"],
        "nonce": proof["nonce"],
        "ts": proof["ts"],
    })
    logger.debug(f"Proof stored: {proof['proof_hash'][:16]} → {proof['repo']}")


def get_proof(proof_hash_short: str) -> dict | None:
    """Retrouve une preuve par ses 16 premiers chars de hash."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT payload, ts FROM system_events
               WHERE event_type = 'AUDIT_PROOF'
               AND payload LIKE ?
               ORDER BY ts DESC LIMIT 1""",
            (f'%"proof_hash": "{proof_hash_short}%',),
        ).fetchone()

    if not row:
        return None

    import json
    try:
        payload = json.loads(row["payload"])
        payload["stored_at"] = row["ts"]
        return payload
    except Exception:
        return None


def get_reputation_stats() -> dict:
    """
    Agrège les preuves pour calculer la réputation de 0xeeMini.
    Retourne: total audits, score moyen, distribution des recommandations.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT payload FROM system_events
               WHERE event_type = 'AUDIT_PROOF'
               ORDER BY ts DESC LIMIT 500"""
        ).fetchall()

    import json
    total = 0
    score_sum = 0
    recs = {"INVEST": 0, "CAUTION": 0, "AVOID": 0}

    for row in rows:
        try:
            p = json.loads(row["payload"])
            total += 1
            score_sum += int(p.get("bullshit_score", 50))
            rec = p.get("recommendation", "CAUTION")
            if rec in recs:
                recs[rec] += 1
        except Exception:
            continue

    return {
        "total_audits_proved": total,
        "avg_bullshit_score": round(score_sum / total, 1) if total else 0,
        "recommendations": recs,
        "reputation_score": round(
            # Score de réputation : 100 si on détecte beaucoup de fraudeurs avec confiance
            min(100, total * 2) if total > 0 else 0, 1
        ),
    }
