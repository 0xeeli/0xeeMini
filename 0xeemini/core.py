# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — Core (BootGuardian + SQLite)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

# ── Chemins ──────────────────────────────────────────
DB_PATH = Path.home() / ".local" / "share" / "0xeemini" / "state.db"
LOG_PATH = Path.home() / ".local" / "share" / "0xeemini" / "logs" / "agent.log"


def setup_logging() -> None:
    """Configure Loguru : fichier rotatif + console colorée."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        LOG_PATH,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{line} | {message}",
        rotation="10 MB",
        retention="7 days",
        level="DEBUG",
    )
    logger.add(
        lambda msg: print(msg, end=""),
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
        colorize=True,
        level="INFO",
    )


# ── Schema SQLite ─────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tx_id             TEXT UNIQUE NOT NULL,
    tx_type           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    amount_usdc       REAL NOT NULL,
    from_wallet       TEXT NOT NULL,
    to_wallet         TEXT NOT NULL,
    memo              TEXT,
    solana_tx_hash    TEXT UNIQUE,
    solana_slot       INTEGER,
    idempotency_key   TEXT UNIQUE NOT NULL,
    signed_payload    BLOB,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    submitted_at      TIMESTAMP,
    confirmed_at      TIMESTAMP,
    failed_at         TIMESTAMP,
    retry_count       INTEGER DEFAULT 0,
    error_message     TEXT,
    recovery_note     TEXT
);

CREATE TABLE IF NOT EXISTS system_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    payload    TEXT,
    ts         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS content_cache (
    content_hash  TEXT PRIMARY KEY,
    source        TEXT,
    raw_title     TEXT,
    summary       TEXT,
    key_insight   TEXT,
    actionable    TEXT,
    generated_at  TIMESTAMP,
    access_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paid_access (
    tx_signature  TEXT PRIMARY KEY,
    content_hash  TEXT,
    buyer_wallet  TEXT,
    amount_usdc   REAL,
    granted_at    TIMESTAMP
);
"""


def get_db() -> sqlite3.Connection:
    """Retourne une connexion SQLite avec row_factory."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialise le schéma (IF NOT EXISTS — idempotent)."""
    with get_db() as conn:
        conn.executescript(_SCHEMA)
    logger.debug(f"DB initialisée : {DB_PATH}")


# ── Helpers globaux ───────────────────────────────────

def get_state(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO system_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=excluded.updated_at""",
            (key, value, now),
        )


def log_event(event_type: str, payload_dict: dict) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO system_events (event_type, payload) VALUES (?, ?)",
            (event_type, json.dumps(payload_dict)),
        )
    logger.debug(f"Event logged : {event_type}")


# ── BootGuardian ──────────────────────────────────────

class BootGuardian:
    """
    Séquence de recovery au démarrage.
    Scanne les transactions orphelines et tente de les résoudre.
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    async def run_recovery_sequence(self) -> bool:
        logger.info("BootGuardian — démarrage de la séquence de recovery...")
        log_event("BOOT", {"ts": datetime.now(timezone.utc).isoformat()})

        success = True
        try:
            orphans = self._get_orphan_transactions()
            logger.info(f"BootGuardian — {len(orphans)} transaction(s) orpheline(s) trouvée(s)")

            for tx in orphans:
                await self._recover_transaction(tx)

            log_event("RECOVERY_SUCCESS", {"orphans_processed": len(orphans)})
            set_state("last_boot_was_clean", "true")
            logger.info("BootGuardian — recovery terminé avec succès")

        except Exception as exc:
            success = False
            logger.error(f"BootGuardian — recovery échoué : {exc}")
            log_event("RECOVERY_FAILED", {"error": str(exc)})
            set_state("last_boot_was_clean", "false")

        return success

    def _get_orphan_transactions(self) -> list:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT * FROM transactions
                   WHERE status IN ('PENDING', 'SIGNED', 'SUBMITTED')
                   ORDER BY created_at ASC"""
            ).fetchall()
        return [dict(r) for r in rows]

    async def _recover_transaction(self, tx: dict) -> None:
        tx_id = tx["tx_id"]
        status = tx["status"]
        logger.warning(f"BootGuardian — recovery TX {tx_id} (status={status})")

        try:
            if status == "PENDING":
                await self._recover_pending(tx)
            elif status == "SIGNED":
                await self._recover_signed(tx)
            elif status == "SUBMITTED":
                await self._recover_submitted(tx)
        except Exception as exc:
            logger.error(f"BootGuardian — échec recovery {tx_id} : {exc}")
            self._mark_failed(tx_id, str(exc), "recovery_failed_at_boot")

    async def _recover_pending(self, tx: dict) -> None:
        """PENDING : vérifier idempotency, re-submit ou marquer FAILED."""
        existing = self._check_idempotency(tx["idempotency_key"])
        if existing and existing["status"] == "CONFIRMED":
            self._mark_failed(tx["tx_id"], "duplicate_idempotency_key_confirmed",
                              "autre tx confirmée avec même clé")
        else:
            self._mark_failed(tx["tx_id"], "pending_at_boot_no_payload",
                              "PENDING sans payload au boot — annulée")

    async def _recover_signed(self, tx: dict) -> None:
        """SIGNED : payload présent → broadcaster, sinon FAILED."""
        if tx.get("signed_payload"):
            logger.info(f"BootGuardian — broadcast TX signée {tx['tx_id']}")
            # Broadcast délégué au ProfitEngine — marquer pour re-submit
            with get_db() as conn:
                conn.execute(
                    "UPDATE transactions SET recovery_note=? WHERE tx_id=?",
                    ("rebroadcast_needed_at_boot", tx["tx_id"]),
                )
        else:
            self._mark_failed(tx["tx_id"], "signed_no_payload",
                              "SIGNED sans payload au boot")

    async def _recover_submitted(self, tx: dict) -> None:
        """SUBMITTED : query RPC Solana pour vérifier la confirmation."""
        import httpx
        if not tx.get("solana_tx_hash"):
            self._mark_failed(tx["tx_id"], "submitted_no_hash", "SUBMITTED sans hash")
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    self.cfg["solana_rpc"],
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            tx["solana_tx_hash"],
                            {"encoding": "json", "maxSupportedTransactionVersion": 0},
                        ],
                    },
                )
            data = resp.json()
            result = data.get("result")
            if result and result.get("meta", {}).get("err") is None:
                now = datetime.now(timezone.utc).isoformat()
                with get_db() as conn:
                    conn.execute(
                        """UPDATE transactions
                           SET status='CONFIRMED', confirmed_at=?,
                               solana_slot=?, recovery_note='confirmed_at_boot'
                           WHERE tx_id=?""",
                        (now, result.get("slot"), tx["tx_id"]),
                    )
                logger.info(f"BootGuardian — TX {tx['tx_id']} confirmée via RPC")
            else:
                self._mark_failed(tx["tx_id"], "rpc_not_confirmed",
                                  "non confirmée sur Solana au boot")
        except Exception as exc:
            logger.warning(f"BootGuardian — RPC query échoué pour {tx['tx_id']} : {exc}")

    def _check_idempotency(self, key: str) -> dict | None:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE idempotency_key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def _mark_failed(self, tx_id: str, error: str, note: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """UPDATE transactions
                   SET status='FAILED', failed_at=?,
                       error_message=?, recovery_note=?
                   WHERE tx_id=?""",
                (now, error, note, tx_id),
            )
        logger.warning(f"BootGuardian — TX {tx_id} marquée FAILED : {error}")

    @staticmethod
    def generate_idempotency_key(tx_type: str, amount: float, to_wallet: str) -> str:
        """sha256(tx_type:amount:.2f:to_wallet:YYYY-MM)"""
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        raw = f"{tx_type}:{amount:.2f}:{to_wallet}:{month}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
