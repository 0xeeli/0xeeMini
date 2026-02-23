# ─────────────────────────────────────────────────────
# 0xeeMini v0.2.0 — HustleAPI (Agent-to-Agent Economy)
# https://mini.0xee.li
#
# Protocole HTTP 402 machine-à-machine :
#   GET  /insight/{id}                → 402 + payment details
#   GET  /insight/{id} + X-Payment-Tx → 200 + data (si TX valide)
#   POST /audit                        → audit GitHub repo (0.50 USDC)
#   GET  /audit/cache/{repo_slug}      → dernier audit en cache (< 24h)
#   GET  /.well-known/ai-plugin.json  → manifeste auto-découverte A2A
# ─────────────────────────────────────────────────────

import hashlib
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from .core import get_db, log_event
from .github_auditor import GitHubAuditor, GitHubAuditorError

# ── Globals ───────────────────────────────────────────
_START_TIME = time.time()
_CYCLE_COUNT = 0
_VERSION = "0.2.0"
_PLATFORM = "https://mini.0xee.li"
_AGENT_NAME = "0xeeMini"

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"


def increment_cycle() -> None:
    global _CYCLE_COUNT
    _CYCLE_COUNT += 1


# Brain injecté au démarrage depuis main.py
_brain = None


def set_brain(brain_instance) -> None:
    """Injecte le BrainLink pour l'audit LLM. Appelé depuis main.py."""
    global _brain
    _brain = brain_instance


# ── App ───────────────────────────────────────────────
app = FastAPI(
    title="0xeeMini HustleAPI",
    version=_VERSION,
    docs_url=None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*", "X-Payment-Tx"],
    expose_headers=["X-Payment-Tx", "X-Powered-By"],
)

_api = __import__('fastapi').APIRouter(prefix="/api")


@app.middleware("http")
async def add_powered_by(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Powered-By"] = f"0xeeMini/{_VERSION} | {_PLATFORM}"
    return response


# ── Helpers ───────────────────────────────────────────

def _memo_for(content_id: str) -> str:
    """Mémo déterministe pour une transaction de paiement."""
    return f"0xee:{content_id}"


def _get_content(content_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM content_cache WHERE content_hash = ?", (content_id,)
        ).fetchone()
    if not row:
        return {}
    return {
        "content_hash": row["content_hash"],
        "source": row["source"],
        "title": row["raw_title"],
        "summary": row["summary"],
        "key_insight": row["key_insight"],
        "actionable": row["actionable"],
        "generated_at": row["generated_at"],
    }


# ── Vérification paiement Solana ──────────────────────

async def _verify_solana_payment(
    tx_signature: str,
    expected_memo: str | None = None,
    expected_amount: float | None = None,
    max_age_seconds: int = 300,
) -> tuple[bool, float]:
    """
    Vérifie une TX Solana USDC :
    - Confirmée, sans erreur
    - Âge < max_age_seconds (défaut 5min)
    - USDC >= expected_amount (ou price_per_insight si None) vers wallet agent
    - Mémo correspond si expected_memo fourni
    """
    try:
        from .config import CFG
        rpc_url = CFG["solana_rpc"]
        agent_wallet = CFG["wallet_public"]
        price = expected_amount if expected_amount is not None else CFG["price_per_insight"]
    except Exception:
        return False, 0.0

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getTransaction",
                    "params": [
                        tx_signature,
                        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
                    ],
                },
            )
        data = resp.json()

        if data.get("error"):
            logger.warning(f"Solana RPC error: {data['error']}")
            return False, 0.0

        result = data.get("result")
        if not result:
            return False, 0.0

        # Âge < max_age_seconds
        block_time = result.get("blockTime", 0)
        if block_time and (time.time() - block_time) > max_age_seconds:
            logger.warning(f"TX {tx_signature[:12]} trop ancienne ({int(time.time() - block_time)}s)")
            return False, 0.0

        # Pas d'erreur on-chain
        if result.get("meta", {}).get("err") is not None:
            return False, 0.0

        instructions = (
            result.get("transaction", {})
            .get("message", {})
            .get("instructions", [])
        )

        amount_found = 0.0
        memo_found = ""

        for ix in instructions:
            parsed = ix.get("parsed", {})

            # Transfer USDC vers agent
            if parsed.get("type") == "transferChecked":
                info = parsed.get("info", {})
                if (
                    info.get("destination") == agent_wallet
                    and info.get("mint") == USDC_MINT
                ):
                    amount_found = float(
                        info.get("tokenAmount", {}).get("uiAmount", 0)
                    )

            # Mémo SPL
            if ix.get("programId") == MEMO_PROGRAM:
                memo_found = str(ix.get("parsed", ""))

        if amount_found < price:
            return False, 0.0

        if expected_memo and expected_memo not in memo_found:
            logger.warning(
                f"Mémo invalide : attendu '{expected_memo}', reçu '{memo_found}'"
            )
            return False, 0.0

        return True, amount_found

    except Exception as exc:
        logger.error(f"Solana RPC verify error: {exc}")
        return False, 0.0


async def _grant_access_db(tx_sig: str, content_id: str, buyer: str, amount: float, mock: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO paid_access
               (tx_signature, content_hash, buyer_wallet, amount_usdc, granted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tx_sig, content_id, buyer, amount, now),
        )
        conn.execute(
            "UPDATE content_cache SET access_count = access_count + 1 WHERE content_hash = ?",
            (content_id,),
        )
    log_event("ACCESS_GRANTED", {
        "tx_signature": tx_sig,
        "content_hash": content_id,
        "buyer_wallet": buyer,
        "amount_usdc": amount,
        "mock": mock,
    })


# ── Endpoints publics ─────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "alive",
        "version": _VERSION,
        "platform": _PLATFORM,
        "cycle": _CYCLE_COUNT,
        "uptime_seconds": int(time.time() - _START_TIME),
        "protocol": "http402+solana",
    }


@app.get("/status")
async def status():
    from .config import CFG
    from .core import get_state

    uptime = int(time.time() - _START_TIME)
    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    vps_paid = get_state(f"vps_paid_{month_key}", "false") == "true"
    monthly_profit = float(get_state(f"profit_transferred_{month_key}", "0.0"))
    claude_spent = float(get_state("claude_spent_usd", "0.0"))

    with get_db() as conn:
        last_events = conn.execute(
            "SELECT event_type, payload, ts FROM system_events ORDER BY ts DESC LIMIT 5"
        ).fetchall()
        last_txs = conn.execute(
            """SELECT tx_type, status, amount_usdc, to_wallet, solana_tx_hash, created_at
               FROM transactions ORDER BY created_at DESC LIMIT 5"""
        ).fetchall()
        last_cycle = conn.execute(
            "SELECT payload, ts FROM system_events WHERE event_type='CYCLE_TICK' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        content_count = conn.execute("SELECT COUNT(*) FROM content_cache").fetchone()[0]

    last_action, last_brain, last_balance = "WAIT", "—", 0.0
    if last_cycle:
        import json as _json
        try:
            p = _json.loads(last_cycle["payload"])
            last_action = p.get("action", "WAIT")
            last_brain = p.get("source", "—")
            last_balance = float(p.get("balance_usdc", 0.0))
        except Exception:
            pass

    return {
        "agent": {
            "version": _VERSION,
            "platform": _PLATFORM,
            "status": "running",
            "uptime_seconds": uptime,
            "cycle_count": _CYCLE_COUNT,
            "protocol": "http402+solana",
        },
        "finance": {
            "balance_usdc": last_balance,
            "reserve_minimum": CFG["reserve_minimum"],
            "monthly_profit": monthly_profit,
            "vps_paid_this_month": vps_paid,
            "claude_spent_usd": round(claude_spent, 5),
        },
        "catalog": {
            "count": content_count,
            "price_usdc": CFG["price_per_insight"],
            "payment_wallet": CFG["wallet_public"],
            "memo_format": "0xee:{content_id}",
        },
        "last_decision": {
            "action": last_action,
            "brain_source": last_brain,
            "ts": last_cycle["ts"] if last_cycle else None,
        },
        "wallet": {
            "agent": CFG["wallet_public"],
            "owner": CFG["owner_address"],
        },
        "transactions": [
            {
                "type": tx["tx_type"],
                "status": tx["status"],
                "amount_usdc": tx["amount_usdc"],
                "to": (tx["to_wallet"] or "")[:20] + "...",
                "hash": tx["solana_tx_hash"],
                "ts": tx["created_at"],
            }
            for tx in last_txs
        ],
    }


@app.get("/catalog")
async def catalog():
    from .config import CFG
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        rows = conn.execute(
            """SELECT content_hash, source, raw_title, summary,
                      key_insight, actionable, generated_at, access_count,
                      expires_at, price_usdc
               FROM content_cache
               WHERE expires_at IS NULL OR expires_at > ?
               ORDER BY generated_at DESC LIMIT 20""",
            (now,),
        ).fetchall()

    items = []
    for row in rows:
        source = row["source"] or "insight"
        is_audit = source == "github_audit"
        price = row["price_usdc"] if row["price_usdc"] else (
            CFG["price_per_audit"] if is_audit else CFG["price_per_insight"]
        )
        item = {
            "content_id": row["content_hash"],
            "type": "github_audit" if is_audit else "insight",
            "source": source,
            "title": row["raw_title"],
            "summary_preview": (row["summary"] or "")[:120] + "…",
            "price_usdc": price,
            "access_count": row["access_count"],
            "generated_at": row["generated_at"],
            "expires_at": row["expires_at"],
        }
        if is_audit:
            item["payment"] = {
                "recipient": CFG["wallet_public"],
                "protocol": "POST /audit with tx_signature header",
                "price_usdc": price,
            }
        else:
            item["payment"] = {
                "recipient": CFG["wallet_public"],
                "memo": _memo_for(row["content_hash"]),
                "protocol": "GET /insight/{content_id} + X-Payment-Tx header",
                "price_usdc": price,
            }
        items.append(item)

    return {
        "items": items,
        "count": len(items),
        "platform": _PLATFORM,
        "protocol_doc": f"{_PLATFORM}/api/openapi.json",
    }


# ── HTTP 402 — Endpoint principal A2A ─────────────────

@app.get("/insight/{content_id}")
async def get_insight(content_id: str, request: Request):
    """
    Protocol HTTP 402 machine-à-machine.

    Sans header X-Payment-Tx  → 402 + instructions de paiement.
    Avec X-Payment-Tx: <sig>  → vérification TX Solana + contenu si valide.
    Mode test : X-Payment-Tx: MOCK_<anything>
    """
    from .config import CFG

    content = _get_content(content_id)
    if not content:
        raise HTTPException(status_code=404, detail={"error": "content_not_found"})

    tx_sig = request.headers.get("X-Payment-Tx", "").strip()
    memo = _memo_for(content_id)

    # ── Étape 1 : pas de preuve → 402 ────────────────
    if not tx_sig:
        return JSONResponse(
            status_code=402,
            content={
                "x402Version": 1,
                "error": "payment_required",
                "resource": f"/insight/{content_id}",
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "solana-mainnet",
                        "asset": USDC_MINT,
                        "asset_name": "USDC",
                        "decimals": 6,
                        "amount_usdc": CFG["price_per_insight"],
                        "payTo": CFG["wallet_public"],
                        "memo": memo,
                        "maxTimeoutSeconds": 300,
                        "description": f"Access insight: {content['title'][:60]}",
                    }
                ],
                "instructions": (
                    f"1. Transfer {CFG['price_per_insight']} USDC to {CFG['wallet_public']} "
                    f"with memo '{memo}' on Solana mainnet. "
                    f"2. Retry this request with header 'X-Payment-Tx: <tx_signature>'."
                ),
            },
        )

    # ── Étape 2 : mock mode ───────────────────────────
    if tx_sig.startswith("MOCK_"):
        logger.warning(f"MOCK insight access: {content_id}")
        await _grant_access_db(tx_sig, content_id, "mock_buyer", 0.10, mock=True)
        return JSONResponse({
            "status": "ok",
            "mock": True,
            "content": content,
            "platform": _PLATFORM,
        })

    # ── Étape 3 : idempotency — déjà payé ? ──────────
    with get_db() as conn:
        existing = conn.execute(
            "SELECT granted_at FROM paid_access WHERE tx_signature = ? AND content_hash = ?",
            (tx_sig, content_id),
        ).fetchone()

    if existing:
        return JSONResponse({
            "status": "already_granted",
            "content": content,
            "platform": _PLATFORM,
        })

    # ── Étape 4 : vérification TX Solana + mémo ───────
    verified, amount = await _verify_solana_payment(tx_sig, expected_memo=memo)
    if not verified or amount < CFG["price_per_insight"]:
        raise HTTPException(
            status_code=402,
            detail={
                "x402Version": 1,
                "error": "payment_not_verified",
                "detail": (
                    "TX invalide, montant insuffisant, mémo incorrect, ou TX > 5min. "
                    f"Mémo attendu : '{memo}'"
                ),
                "required_usdc": CFG["price_per_insight"],
                "required_memo": memo,
            },
        )

    await _grant_access_db(tx_sig, content_id, "agent_buyer", amount, mock=False)
    logger.success(f"Insight vendu : {content_id} | {amount} USDC | TX {tx_sig[:12]}…")

    return JSONResponse({
        "status": "ok",
        "content": content,
        "platform": _PLATFORM,
        "tx_verified": tx_sig,
        "amount_paid_usdc": amount,
    })


# ── Proof of Compute — vérification publique ──────────

@app.get("/proof/{proof_id}")
async def get_proof(proof_id: str):
    """Vérifie publiquement une preuve de calcul d'audit."""
    from .proof_of_compute import get_proof as _get_proof
    proof = _get_proof(proof_id)
    if not proof:
        raise HTTPException(
            status_code=404,
            detail={"error": "proof_not_found", "proof_id": proof_id},
        )
    return JSONResponse({
        "status": "verified",
        "proof": proof,
        "platform": _PLATFORM,
    })


@app.get("/reputation")
async def reputation():
    """Réputation agrégée de 0xeeMini — tous les audits prouvés."""
    from .proof_of_compute import get_reputation_stats
    stats = get_reputation_stats()
    return JSONResponse({"platform": _PLATFORM, **stats})


# ── Batch Audit ───────────────────────────────────────

BATCH_PRICE_USDC = 1.50  # 2–5 repos


class BatchAuditRequest(BaseModel):
    repos: list[str]
    buyer_wallet: str = ""
    tx_signature: str = ""
    lang: str = "en"


@app.post("/audit/batch")
async def post_audit_batch(body: BatchAuditRequest):
    """
    Batch Fake-Dev Audit — jusqu'à 5 repos, 1.50 USDC via HTTP 402.

    buyer_wallet="MOCK_..." → analyse gratuite (test/dev).
    Sans tx_signature → 402 avec instructions de paiement.
    Avec tx_signature valide → liste d'audits + preuves.
    """
    from .config import CFG

    repos = [r.strip() for r in body.repos if r.strip()]
    if not repos:
        raise HTTPException(status_code=422, detail={"error": "repos[] requis"})
    if len(repos) > 5:
        raise HTTPException(
            status_code=422,
            detail={"error": "Maximum 5 repos par batch"},
        )
    if len(repos) < 2:
        raise HTTPException(
            status_code=422,
            detail={"error": "Minimum 2 repos — utilisez /audit pour un seul repo"},
        )

    price = BATCH_PRICE_USDC

    # ── Mock mode ──────────────────────────────────────
    if body.buyer_wallet.startswith("MOCK_"):
        logger.warning(f"MOCK batch audit : {repos}")
        results = []
        for repo_url in repos:
            try:
                result = await GitHubAuditor(brain=_brain).run(repo_url)
                result["mock"] = True
                results.append(result)
            except GitHubAuditorError as exc:
                results.append({"repo": repo_url, "error": str(exc)})
        return JSONResponse({"status": "ok", "mock": True, "results": results})

    # ── Sans preuve → 402 ─────────────────────────────
    if not body.tx_signature:
        savings = round(len(repos) * 0.50 - price, 2)
        return JSONResponse(
            status_code=402,
            content={
                "x402Version": 1,
                "error": "payment_required",
                "resource": "/audit/batch",
                "repos": repos,
                "repos_count": len(repos),
                "price_usdc": price,
                "savings_usdc": savings,
                "wallet": CFG["wallet_public"],
                "accepts": [{
                    "scheme": "exact",
                    "network": "solana-mainnet",
                    "asset": USDC_MINT,
                    "asset_name": "USDC",
                    "decimals": 6,
                    "amount_usdc": price,
                    "payTo": CFG["wallet_public"],
                    "maxTimeoutSeconds": 600,
                    "description": f"Batch GitHub Audit — {len(repos)} repos",
                }],
                "instructions": (
                    f"1. Transfer {price} USDC to {CFG['wallet_public']} on Solana. "
                    f"2. Retry with tx_signature. "
                    f"(savings: {savings} USDC vs individual audits)"
                ),
            },
        )

    # ── Vérification TX ───────────────────────────────
    verified, amount = await _verify_solana_payment(
        body.tx_signature,
        expected_amount=price,
        max_age_seconds=600,
    )
    if not verified or amount < price:
        raise HTTPException(
            status_code=402,
            detail={
                "x402Version": 1,
                "error": "payment_not_verified",
                "required_usdc": price,
            },
        )

    # ── Idempotency ───────────────────────────────────
    with get_db() as conn:
        existing = conn.execute(
            "SELECT granted_at FROM paid_access WHERE tx_signature = ?",
            (body.tx_signature,),
        ).fetchone()
    if existing:
        return JSONResponse({"status": "already_granted", "repos": repos})

    # ── Lancer les audits en séquence ─────────────────
    now = datetime.now(timezone.utc).isoformat()
    results = []
    for repo_url in repos:
        try:
            result = await GitHubAuditor(brain=_brain).run(repo_url, lang=body.lang)
            results.append(result)
        except GitHubAuditorError as exc:
            results.append({"repo": repo_url, "error": str(exc)})

    # Enregistrer l'accès payant (lié au premier repo)
    first_hash = results[0].get("content_hash", "batch") if results else "batch"
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO paid_access
               (tx_signature, content_hash, buyer_wallet, amount_usdc, granted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (body.tx_signature, first_hash, body.buyer_wallet, amount, now),
        )

    log_event("BATCH_AUDIT_GRANTED", {
        "tx_signature": body.tx_signature,
        "repos": repos,
        "repos_count": len(repos),
        "amount_usdc": amount,
    })
    logger.success(
        f"Batch audit vendu : {len(repos)} repos | {amount} USDC | TX {body.tx_signature[:12]}…"
    )

    return JSONResponse({"status": "ok", "results": results, "repos_count": len(repos)})


# ── Auto-découverte A2A ───────────────────────────────

@app.get("/.well-known/agent.json")
async def agent_json():
    """
    Standard agent.json — compatible Agentverse, Wayfinder, EIP-8004, A2A.
    Référencé après enregistrement via : npx @emberai/agent-node register
    """
    from .config import CFG
    from .proof_of_compute import get_reputation_stats
    rep = get_reputation_stats()
    return {
        "schema": "agent/v1",
        "name": _AGENT_NAME,
        "version": _VERSION,
        "description": (
            "Autonomous AI agent detecting fake blockchain developers via GitHub commit analysis. "
            "Bullshit score 0–100. Sell audits for 0.50 USDC via Solana HTTP 402."
        ),
        "url": _PLATFORM,
        "logo": f"{_PLATFORM}/favicon.ico",
        "contact": "agent@0xee.li",
        "open_source": "https://github.com/0xee/0xeemini",
        "autonomous": True,
        "payment": {
            "protocol": "HTTP402",
            "network": "solana-mainnet",
            "asset": "USDC",
            "asset_mint": USDC_MINT,
            "recipient": CFG["wallet_public"],
        },
        "capabilities": [
            {
                "id": "github-audit",
                "name": "GitHub Fake-Dev Audit",
                "description": (
                    "Detect fake developer activity in crypto projects. "
                    "Bullshit score 0-100, verdict INVEST/CAUTION/AVOID."
                ),
                "endpoint": f"{_PLATFORM}/audit",
                "method": "POST",
                "price": {"amount": str(CFG["price_per_audit"]), "currency": "USDC", "chain": "solana"},
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "repo_url": {"type": "string", "example": "bitcoin/bitcoin"},
                        "tx_signature": {"type": "string", "description": "Solana TX proof of payment"},
                    },
                    "required": ["repo_url"],
                },
            },
            {
                "id": "batch-audit",
                "name": "Portfolio Batch Audit",
                "description": "Audit up to 5 repositories at discounted rate.",
                "endpoint": f"{_PLATFORM}/audit/batch",
                "method": "POST",
                "price": {"amount": str(BATCH_PRICE_USDC), "currency": "USDC", "chain": "solana"},
            },
            {
                "id": "insights",
                "name": "Tech/Crypto Insights",
                "description": "Curated AI-generated insights on tech and crypto trends.",
                "endpoint": f"{_PLATFORM}/catalog",
                "method": "GET",
                "price": {"amount": str(CFG["price_per_insight"]), "currency": "USDC", "chain": "solana"},
            },
        ],
        "proof_of_compute": {
            "enabled": True,
            "algorithm": "SHA256",
            "verify_endpoint": f"{_PLATFORM}/proof/{{proof_id}}",
            "reputation_endpoint": f"{_PLATFORM}/reputation",
            "total_audits_proved": rep.get("total_audits_proved", 0),
            "avg_bullshit_score": rep.get("avg_bullshit_score", 0),
        },
        "discovery": {
            "ai_plugin": f"{_PLATFORM}/.well-known/ai-plugin.json",
            "openapi": f"{_PLATFORM}/openapi.json",
            "catalog": f"{_PLATFORM}/catalog",
        },
    }


@app.get("/.well-known/ai-plugin.json")
async def ai_plugin_manifest():
    """
    Manifeste d'auto-découverte pour marketplaces d'agents (Agentopia, x402 Bazaar, MCP).
    Compatible OpenAI plugin spec v1 + extensions Solana payment.
    """
    from .config import CFG
    return {
        "schema_version": "v1",
        "name_for_human": "0xeeMini — On-Chain Intelligence Agent",
        "name_for_model": "0xeemini_data_agent",
        "description_for_human": (
            "Autonomous AI agent selling curated on-chain analytics and tech intelligence. "
            "Pay-per-insight via USDC on Solana. No subscription, no API key required."
        ),
        "description_for_model": (
            "Data vendor agent. Sells structured JSON insights (tech news, crypto momentum, "
            "on-chain signals) via HTTP 402 protocol. "
            "Payment: USDC on Solana mainnet. "
            "Protocol: GET /insight/{id} returns 402 with payment instructions; "
            "retry with X-Payment-Tx header after payment. "
            "Discovery: GET /catalog for available items."
        ),
        "auth": {"type": "none"},
        "payment": {
            "type": "http402",
            "network": "solana-mainnet",
            "asset": USDC_MINT,
            "asset_symbol": "USDC",
            "recipient": CFG["wallet_public"],
            "price_per_call_usdc": CFG["price_per_insight"],
            "memo_format": "0xee:{content_id}",
        },
        "api": {
            "type": "openapi",
            "url": f"{_PLATFORM}/openapi.json",
            "is_user_authenticated": False,
        },
        "endpoints": {
            "catalog": f"{_PLATFORM}/catalog",
            "insight": f"{_PLATFORM}/insight/{{content_id}}",
            "status": f"{_PLATFORM}/status",
            "health": f"{_PLATFORM}/health",
        },
        "capabilities": [
            "tech_intelligence",
            "crypto_momentum",
            "on_chain_signals",
            "a2a_http402",
            "solana_usdc_payment",
        ],
        "logo_url": f"{_PLATFORM}/favicon.ico",
        "contact": "agent@0xee.li",
        "legal_info_url": _PLATFORM,
        "x_agent_version": _VERSION,
        "x_autonomous": True,
        "x_open_source": "https://github.com/0xee/0xeemini",
    }


@app.get("/openapi.json")
async def openapi_schema():
    """Schéma OpenAPI minimal pour les clients MCP/A2A."""
    from .config import CFG
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "0xeeMini HustleAPI",
            "version": _VERSION,
            "description": "HTTP 402 pay-per-insight API. USDC on Solana.",
        },
        "servers": [{"url": _PLATFORM}],
        "paths": {
            "/catalog": {
                "get": {
                    "summary": "List available insights",
                    "operationId": "list_catalog",
                    "responses": {
                        "200": {"description": "Array of available insights with payment instructions"}
                    },
                }
            },
            "/insight/{content_id}": {
                "get": {
                    "summary": "Get insight (HTTP 402 protected)",
                    "operationId": "get_insight",
                    "parameters": [
                        {
                            "name": "content_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "X-Payment-Tx",
                            "in": "header",
                            "required": False,
                            "description": "Solana TX signature proving USDC payment",
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "Insight content (JSON)"},
                        "402": {
                            "description": "Payment required",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "properties": {
                                            "x402Version": {"type": "integer"},
                                            "accepts": {"type": "array"},
                                            "instructions": {"type": "string"},
                                        }
                                    }
                                }
                            },
                        },
                    },
                }
            },
        },
    }


# ── GitHub Audit — HTTP 402 ───────────────────────────

class AuditRequest(BaseModel):
    repo_url: str
    buyer_wallet: str = ""
    tx_signature: str = ""
    lang: str = "en"  # ISO 639-1 — "fr" | "en" (default)


@app.post("/audit")
async def post_audit(body: AuditRequest):
    """
    Audit GitHub Fake-Dev Detector — 0.50 USDC via HTTP 402.

    Sans tx_signature → 402 avec instructions de paiement.
    buyer_wallet="MOCK_..." → analyse gratuite (test/dev).
    Avec tx_signature valide → analyse + résultat JSON.
    """
    from .config import CFG

    price = CFG["price_per_audit"]

    if not body.repo_url:
        raise HTTPException(status_code=422, detail={"error": "repo_url requis"})

    # ── Mock mode ──────────────────────────────────────
    if body.buyer_wallet.startswith("MOCK_"):
        logger.warning(f"MOCK audit request : {body.repo_url}")
        log_event("AUDIT_MOCK", {"repo_url": body.repo_url, "buyer": body.buyer_wallet})
        try:
            auditor = GitHubAuditor(brain=_brain)
            owner, repo = auditor._parse_repo_url(body.repo_url)
            # Serve from cache if available (< 24h) — avoids re-fetching GitHub for every /demo
            cached = GitHubAuditor.get_cached_audit(f"{owner}/{repo}")
            if cached:
                cached["mock"] = True
                cached["_cached"] = True
                logger.info(f"MOCK audit served from cache : {owner}/{repo}")
                return JSONResponse({"status": "ok", **cached})
            result = await auditor.run(body.repo_url, lang=body.lang)
            result["mock"] = True
            return JSONResponse({"status": "ok", **result})
        except GitHubAuditorError as exc:
            raise HTTPException(status_code=422, detail={"error": str(exc)})

    # ── Sans preuve → 402 ─────────────────────────────
    if not body.tx_signature:
        return JSONResponse(
            status_code=402,
            content={
                "x402Version": 1,
                "error": "payment_required",
                "resource": "/audit",
                "price_usdc": price,
                "wallet": CFG["wallet_public"],
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "solana-mainnet",
                        "asset": USDC_MINT,
                        "asset_name": "USDC",
                        "decimals": 6,
                        "amount_usdc": price,
                        "payTo": CFG["wallet_public"],
                        "maxTimeoutSeconds": 600,
                        "description": f"GitHub Fake-Dev Audit : {body.repo_url[:60]}",
                    }
                ],
                "instructions": (
                    f"1. Transfer {price} USDC to {CFG['wallet_public']} on Solana mainnet. "
                    f"2. Retry with tx_signature in body."
                ),
            },
        )

    # ── Idempotency — déjà traité ? ───────────────────
    with get_db() as conn:
        existing = conn.execute(
            "SELECT granted_at FROM paid_access WHERE tx_signature = ?",
            (body.tx_signature,),
        ).fetchone()
    if existing:
        try:
            auditor_tmp = GitHubAuditor(brain=_brain)
            owner, repo = auditor_tmp._parse_repo_url(body.repo_url)
            cached = GitHubAuditor.get_cached_audit(f"{owner}/{repo}")
            if cached:
                return JSONResponse({"status": "already_granted", **cached})
        except Exception:
            pass
        return JSONResponse({"status": "already_granted", "repo": body.repo_url})

    # ── Vérification TX Solana (>= 0.50 USDC, < 10min) ─
    verified, amount = await _verify_solana_payment(
        body.tx_signature,
        expected_amount=price,
        max_age_seconds=600,
    )
    if not verified or amount < price:
        raise HTTPException(
            status_code=402,
            detail={
                "x402Version": 1,
                "error": "payment_not_verified",
                "detail": f"TX invalide, montant insuffisant (need {price} USDC), ou TX > 10min.",
                "required_usdc": price,
            },
        )

    # ── Lancer l'audit ────────────────────────────────
    try:
        result = await GitHubAuditor(brain=_brain).run(body.repo_url, lang=body.lang)
    except GitHubAuditorError as exc:
        raise HTTPException(status_code=422, detail={"error": str(exc)})

    # Enregistrer l'accès payant
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO paid_access
               (tx_signature, content_hash, buyer_wallet, amount_usdc, granted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (body.tx_signature, result["content_hash"], body.buyer_wallet, amount, now),
        )
    log_event("AUDIT_ACCESS_GRANTED", {
        "tx_signature": body.tx_signature,
        "repo": result["repo"],
        "bullshit_score": result["bullshit_score"],
        "amount_usdc": amount,
    })
    logger.success(
        f"Audit vendu : {result['repo']} | score={result['bullshit_score']} "
        f"| {amount} USDC | TX {body.tx_signature[:12]}…"
    )

    return JSONResponse({"status": "ok", **result})


@app.get("/audit/cache/{repo_slug}")
async def get_audit_cache(repo_slug: str):
    """
    Retourne le dernier audit en cache pour ce repo (< 24h).
    repo_slug format : "owner-repo" (tiret comme séparateur)
    → 404 si absent ou périmé.
    """
    # Normaliser : "owner-repo" → "owner/repo" (tenter les deux)
    repo_key = repo_slug.replace("--", "/", 1)
    if "/" not in repo_key:
        # Essayer le premier tiret comme séparateur owner/repo
        parts = repo_slug.split("-", 1)
        repo_key = "/".join(parts) if len(parts) == 2 else repo_slug

    cached = GitHubAuditor.get_cached_audit(repo_key)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "audit_not_found",
                "repo": repo_key,
                "message": "Aucun audit récent (< 24h). Commandez via POST /audit.",
            },
        )
    return JSONResponse({"status": "cached", **cached})


# ── Paywall legacy (POST /access) ─────────────────────
# Gardé pour compatibilité avec les clients B2C existants

class AccessRequest(BaseModel):
    tx_signature: str
    content_hash: str
    buyer_wallet: str


@app.post("/access")
async def request_access(body: AccessRequest):
    from .config import CFG
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM paid_access WHERE tx_signature = ?",
            (body.tx_signature,),
        ).fetchone()

    if existing:
        return JSONResponse({"status": "already_granted", "content": _get_content(body.content_hash)})

    if body.tx_signature.startswith("MOCK_"):
        await _grant_access_db(body.tx_signature, body.content_hash, body.buyer_wallet, 0.10, mock=True)
        return JSONResponse({"status": "access_granted", "mock": True, "content": _get_content(body.content_hash)})

    verified, amount = await _verify_solana_payment(body.tx_signature)
    if not verified or amount < CFG["price_per_insight"]:
        raise HTTPException(status_code=402, detail={"error": "payment_not_verified"})

    await _grant_access_db(body.tx_signature, body.content_hash, body.buyer_wallet, amount, mock=False)
    return JSONResponse({"status": "access_granted", "content": _get_content(body.content_hash), "platform": _PLATFORM})


# ── Routes /api/* pour lighttpd reverse-proxy ─────────

@_api.get("/health")
async def _api_health():
    return await health()

@_api.get("/status")
async def _api_status():
    return await status()

@_api.get("/catalog")
async def _api_catalog():
    return await catalog()

@_api.get("/insight/{content_id}")
async def _api_insight(content_id: str, request: Request):
    return await get_insight(content_id, request)

@_api.get("/openapi.json")
async def _api_openapi():
    return await openapi_schema()

@_api.post("/access")
async def _api_access(body: AccessRequest):
    return await request_access(body)

@_api.post("/audit")
async def _api_audit(body: AuditRequest):
    return await post_audit(body)

@_api.post("/audit/batch")
async def _api_audit_batch(body: BatchAuditRequest):
    return await post_audit_batch(body)

@_api.get("/audit/cache/{repo_slug}")
async def _api_audit_cache(repo_slug: str):
    return await get_audit_cache(repo_slug)

@_api.get("/proof/{proof_id}")
async def _api_proof(proof_id: str):
    return await get_proof(proof_id)

@_api.get("/reputation")
async def _api_reputation():
    return await reputation()


app.include_router(_api)
