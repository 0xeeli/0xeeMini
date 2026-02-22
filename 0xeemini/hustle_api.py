# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — HustleAPI (FastAPI paywall USDC)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

from .core import get_db, log_event

# ── Globals ───────────────────────────────────────────
_START_TIME = time.time()
_CYCLE_COUNT = 0
_VERSION = "0.1.0"
_PLATFORM = "https://mini.0xee.li"


def increment_cycle() -> None:
    global _CYCLE_COUNT
    _CYCLE_COUNT += 1


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
    allow_headers=["*"],
)


@app.middleware("http")
async def add_powered_by(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Powered-By"] = f"0xeeMini | {_PLATFORM}"
    return response


# ── Endpoints ─────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "alive",
        "version": _VERSION,
        "platform": _PLATFORM,
        "cycle": _CYCLE_COUNT,
        "uptime_seconds": int(time.time() - _START_TIME),
    }


@app.get("/catalog")
async def catalog(cfg: dict = None):
    """Retourne les 10 derniers contenus disponibles avec prix."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT content_hash, source, raw_title, summary,
                      key_insight, actionable, generated_at, access_count
               FROM content_cache
               ORDER BY generated_at DESC
               LIMIT 10"""
        ).fetchall()

    items = []
    for row in rows:
        items.append({
            "content_hash": row["content_hash"],
            "source": row["source"],
            "title": row["raw_title"],
            "summary": row["summary"],
            "preview_insight": (row["key_insight"] or "")[:80] + "...",
            "price_usdc": 0.10,
            "access_count": row["access_count"],
            "generated_at": row["generated_at"],
        })

    return {"items": items, "count": len(items), "platform": _PLATFORM}


class AccessRequest(BaseModel):
    tx_signature: str
    content_hash: str
    buyer_wallet: str


@app.post("/access")
async def request_access(body: AccessRequest, cfg_holder: dict = None):
    """
    Vérifie un paiement USDC et accorde l'accès au contenu.
    1. Check paid_access (idempotent)
    2. Vérif Solana RPC (>= 0.10 USDC → wallet agent, < 5min)
    3. Mock mode si tx_signature commence par MOCK_
    """
    # 1. Idempotency : déjà payé ?
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM paid_access WHERE tx_signature = ?",
            (body.tx_signature,),
        ).fetchone()

    if existing:
        content = _get_content(body.content_hash)
        return JSONResponse({"status": "already_granted", "content": content})

    # 2. Mock mode
    if body.tx_signature.startswith("MOCK_"):
        logger.warning(f"MOCK access granted for {body.buyer_wallet} → {body.content_hash}")
        return await _grant_access(body, amount_usdc=0.10, mock=True)

    # 3. Vérification Solana RPC
    verified, amount = await _verify_solana_payment(body.tx_signature)
    if not verified or amount < 0.10:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_not_verified",
                "message": "Transaction USDC invalide ou montant insuffisant (min 0.10 USDC)",
                "required_usdc": 0.10,
            },
        )

    return await _grant_access(body, amount_usdc=amount, mock=False)


async def _verify_solana_payment(tx_signature: str) -> tuple[bool, float]:
    """
    Vérifie sur Solana RPC que la transaction est valide :
    - Confirmée
    - USDC transféré >= 0.10
    - Âge < 5 minutes
    """
    # Import CFG lazily pour éviter les imports circulaires au boot
    try:
        from .config import CFG
        rpc_url = CFG["solana_rpc"]
        agent_wallet = CFG["wallet_public"]
        price = CFG["price_per_insight"]
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
        result = data.get("result")
        if not result:
            return False, 0.0

        # Vérifier l'âge (blockTime en secondes epoch)
        import time as _time
        block_time = result.get("blockTime", 0)
        if block_time and (_time.time() - block_time) > 300:
            logger.warning(f"TX {tx_signature} trop ancienne ({int(_time.time() - block_time)}s)")
            return False, 0.0

        # Pas d'erreur
        if result.get("meta", {}).get("err") is not None:
            return False, 0.0

        # Chercher le transfer USDC vers le wallet agent
        instructions = (
            result.get("transaction", {})
            .get("message", {})
            .get("instructions", [])
        )
        for ix in instructions:
            parsed = ix.get("parsed", {})
            if parsed.get("type") == "transferChecked":
                info = parsed.get("info", {})
                if (
                    info.get("destination") == agent_wallet
                    and info.get("mint") == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC mainnet
                ):
                    amount = float(info.get("tokenAmount", {}).get("uiAmount", 0))
                    if amount >= price:
                        return True, amount

        return False, 0.0

    except Exception as exc:
        logger.error(f"Solana RPC verify error : {exc}")
        return False, 0.0


async def _grant_access(body: AccessRequest, amount_usdc: float, mock: bool) -> JSONResponse:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO paid_access
               (tx_signature, content_hash, buyer_wallet, amount_usdc, granted_at)
               VALUES (?, ?, ?, ?, ?)""",
            (body.tx_signature, body.content_hash, body.buyer_wallet, amount_usdc, now),
        )
        conn.execute(
            "UPDATE content_cache SET access_count = access_count + 1 WHERE content_hash = ?",
            (body.content_hash,),
        )

    log_event("ACCESS_GRANTED", {
        "tx_signature": body.tx_signature,
        "content_hash": body.content_hash,
        "buyer_wallet": body.buyer_wallet,
        "amount_usdc": amount_usdc,
        "mock": mock,
    })

    content = _get_content(body.content_hash)
    return JSONResponse({
        "status": "access_granted",
        "mock": mock,
        "content": content,
        "platform": _PLATFORM,
    })


def _get_content(content_hash: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM content_cache WHERE content_hash = ?", (content_hash,)
        ).fetchone()
    if not row:
        return {"error": "content_not_found"}
    return {
        "content_hash": row["content_hash"],
        "title": row["raw_title"],
        "summary": row["summary"],
        "key_insight": row["key_insight"],
        "actionable": row["actionable"],
    }
