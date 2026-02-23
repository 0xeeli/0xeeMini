# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — ProfitEngine (Solana USDC transfers)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import asyncio
import signal
import uuid
from datetime import datetime, timezone

import httpx
from loguru import logger

from .core import (
    BootGuardian,
    get_db,
    get_state,
    log_event,
    set_state,
)

# ── Constantes ───────────────────────────────────────
INFOMANIAK_PLANS = {
    "2GB": 5.0,
    "4GB": 9.0,
    "6GB": 13.0,
    "8GB": 17.0,
}

# USDC mint address (mainnet)
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Profit split
PROFIT_OWNER_RATIO = 0.80
PROFIT_RESERVE_RATIO = 0.20
PROFIT_MIN_TRANSFER_USDC = 5.0


class ProfitEngine:
    """
    Gère les transactions USDC sur Solana :
    - Transferts idempotents avec machine d'état SQLite
    - Règlement mensuel VPS + transfert profit owner
    - Évaluation upgrade VPS
    """

    _kill_armed = False
    _kill_event = asyncio.Event()

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._setup_sigterm()

    def _setup_sigterm(self) -> None:
        def _handler(signum, frame):
            if ProfitEngine._kill_armed:
                logger.warning("ProfitEngine — SIGTERM reçu, kill window annulée")
                ProfitEngine._kill_event.set()

        signal.signal(signal.SIGTERM, _handler)

    # ── Balance USDC ──────────────────────────────────

    async def get_usdc_balance(self) -> float:
        """Interroge le RPC Solana pour le solde USDC du wallet agent."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self.cfg["solana_rpc"],
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTokenAccountsByOwner",
                        "params": [
                            self.cfg["wallet_public"],
                            {"mint": USDC_MINT},
                            {"encoding": "jsonParsed"},
                        ],
                    },
                )
            data = resp.json()
            accounts = data.get("result", {}).get("value", [])
            if not accounts:
                return 0.0
            amount = (
                accounts[0]
                .get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
                .get("tokenAmount", {})
                .get("uiAmount", 0.0)
            )
            return float(amount or 0.0)
        except Exception as exc:
            logger.error(f"ProfitEngine — get_usdc_balance error : {exc}")
            return 0.0

    # ── Execute Transfer ──────────────────────────────

    async def execute_transfer(self, action_details: dict) -> str:
        """
        Exécute un transfert USDC avec machine d'état idempotente.
        Retourne tx_id (existant ou nouveau).
        """
        idem_key = action_details.get("idempotency_key")
        if not idem_key:
            idem_key = BootGuardian.generate_idempotency_key(
                action_details.get("tx_type", "TRANSFER"),
                float(action_details.get("amount_usdc", 0)),
                action_details.get("to_wallet", ""),
            )

        # Idempotency check
        with get_db() as conn:
            existing = conn.execute(
                "SELECT tx_id, status FROM transactions WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()

        if existing:
            logger.info(
                f"ProfitEngine — TX déjà existante ({existing['status']}) : {existing['tx_id']}"
            )
            return existing["tx_id"]

        tx_id = str(uuid.uuid4())
        amount = float(action_details.get("amount_usdc", 0))
        to_wallet = action_details.get("to_wallet", "")
        tx_type = action_details.get("tx_type", "TRANSFER")
        memo = action_details.get("memo", "0xeeMini transfer")

        # Vérifications de sécurité
        if amount > 5.0:
            logger.warning(f"ProfitEngine — transfert > 5 USDC : {amount} → kill_switch armé")
            ProfitEngine._kill_armed = True

        # INSERT PENDING
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO transactions
                   (tx_id, tx_type, status, amount_usdc, from_wallet, to_wallet,
                    memo, idempotency_key, created_at)
                   VALUES (?, ?, 'PENDING', ?, ?, ?, ?, ?, ?)""",
                (tx_id, tx_type, amount, self.cfg["wallet_public"],
                 to_wallet, memo, idem_key, now),
            )

        log_event("TX_INITIATED", {
            "tx_id": tx_id, "tx_type": tx_type,
            "amount_usdc": amount, "to_wallet": to_wallet,
        })
        logger.info(f"ProfitEngine — TX {tx_id} initiée ({tx_type}, {amount:.2f} USDC → {to_wallet[:8]}...)")

        # Kill window AVANT la signature — le blockhash Solana expire en ~60s
        # Si on signe avant d'attendre, le blockhash sera périmé au broadcast
        if ProfitEngine._kill_armed:
            logger.warning("ProfitEngine — kill window 60s armée. SIGTERM pour annuler.")
            ProfitEngine._kill_event.clear()
            try:
                await asyncio.wait_for(ProfitEngine._kill_event.wait(), timeout=60.0)
                self._mark_failed(tx_id, "kill_switch_triggered_by_sigterm")
                logger.warning(f"ProfitEngine — TX {tx_id} annulée par kill switch")
                return tx_id
            except asyncio.TimeoutError:
                logger.info("ProfitEngine — kill window expirée, signature avec blockhash frais")
            finally:
                ProfitEngine._kill_armed = False

        # Signer avec un blockhash frais (après la kill window si applicable)
        try:
            signed_payload = await self._sign_transaction(tx_id, amount, to_wallet, memo)
        except Exception as exc:
            self._mark_failed(tx_id, str(exc))
            raise

        # Broadcast
        tx_hash = await self._broadcast_transaction(tx_id, signed_payload)
        if not tx_hash:
            self._mark_failed(tx_id, "broadcast_failed")
            return tx_id

        # Poll confirmation
        confirmed = await self._poll_confirmation(tx_id, tx_hash)
        if confirmed:
            log_event("TX_CONFIRMED", {"tx_id": tx_id, "tx_hash": tx_hash, "amount_usdc": amount})

        return tx_id

    async def _sign_transaction(self, tx_id: str, amount: float,
                                 to_wallet: str, memo: str) -> bytes:
        """
        Construit et signe une vraie transaction SPL Token (USDC) sur Solana.
        Utilise solders 0.21 pour keypair/message/tx, httpx pour le RPC.
        """
        import base58
        import struct
        from solders.keypair import Keypair  # type: ignore
        from solders.pubkey import Pubkey  # type: ignore
        from solders.hash import Hash  # type: ignore
        from solders.instruction import Instruction, AccountMeta  # type: ignore
        from solders.message import Message  # type: ignore
        from solders.transaction import Transaction  # type: ignore

        # ── Keypair ───────────────────────────────────
        private_key_bytes = base58.b58decode(self.cfg["wallet_private"])
        keypair = Keypair.from_bytes(private_key_bytes)
        sender_pubkey = keypair.pubkey()
        usdc_mint = Pubkey.from_string(USDC_MINT)
        token_program = Pubkey.from_string(
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
        )

        async def _get_token_accounts(client, wallet: str, rpc_id: int) -> list:
            """Récupère les ATAs USDC avec backoff exponentiel sur rate-limit 429."""
            last_error = None
            for attempt in range(5):
                if attempt:
                    await asyncio.sleep(3.0 * (2 ** (attempt - 1)))  # 3s, 6s, 12s, 24s
                r = await client.post(self.cfg["solana_rpc"], json={
                    "jsonrpc": "2.0", "id": rpc_id,
                    "method": "getTokenAccountsByOwner",
                    "params": [wallet, {"mint": str(usdc_mint)}, {"encoding": "jsonParsed"}],
                })
                data = r.json()
                if data.get("error"):
                    logger.warning(f"RPC error (attempt {attempt+1}): {data['error']}")
                    last_error = data["error"]
                    continue
                # Réponse HTTP 200 — retourner même si vide ([] = vrai ATA absent)
                return data.get("result", {}).get("value", [])
            # Toutes les tentatives échouées par rate-limit, pas par absence d'ATA
            raise ValueError(f"RPC rate-limited après 5 tentatives : {last_error}")

        def _ata_cache_key(wallet: str) -> str:
            return f"ata_cache_{wallet[:8]}"

        def _get_cached_ata(wallet: str) -> str | None:
            return get_state(_ata_cache_key(wallet), "")

        def _cache_ata(wallet: str, ata: str) -> None:
            set_state(_ata_cache_key(wallet), ata)

        async with httpx.AsyncClient(timeout=30) as client:
            # ── Compte USDC de l'envoyeur ─────────────
            sender_key = str(sender_pubkey)
            sender_ata_str = _get_cached_ata(sender_key)
            if not sender_ata_str:
                sender_accounts = await _get_token_accounts(client, sender_key, 1)
                if not sender_accounts:
                    raise ValueError("Sender has no USDC token account")
                sender_ata_str = sender_accounts[0]["pubkey"]
                _cache_ata(sender_key, sender_ata_str)
                logger.debug(f"ATA sender mis en cache : {sender_ata_str[:8]}...")
                await asyncio.sleep(2.0)
            else:
                logger.debug(f"ATA sender depuis cache : {sender_ata_str[:8]}...")
            sender_ata = Pubkey.from_string(sender_ata_str)

            # ── Compte USDC du destinataire ───────────
            receiver_ata_str = _get_cached_ata(to_wallet)
            if not receiver_ata_str:
                receiver_accounts = await _get_token_accounts(client, to_wallet, 2)
                if not receiver_accounts:
                    raise ValueError(
                        f"Receiver {to_wallet[:8]}... has no USDC token account"
                    )
                receiver_ata_str = receiver_accounts[0]["pubkey"]
                _cache_ata(to_wallet, receiver_ata_str)
                logger.debug(f"ATA receiver mis en cache : {receiver_ata_str[:8]}...")
                await asyncio.sleep(2.0)
            else:
                logger.debug(f"ATA receiver depuis cache : {receiver_ata_str[:8]}...")
            receiver_ata = Pubkey.from_string(receiver_ata_str)

            # ── Blockhash récent ──────────────────────
            r = await client.post(self.cfg["solana_rpc"], json={
                "jsonrpc": "2.0", "id": 3,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}],
            })
            blockhash_str = r.json()["result"]["value"]["blockhash"]
            recent_blockhash = Hash.from_string(blockhash_str)

        # ── Instruction transfer_checked ──────────────
        # SPL Token discriminator 12 : [12] + u64 amount (LE) + [decimals]
        amount_micro = int(amount * 1_000_000)  # USDC = 6 décimales
        ix_data = bytes([12]) + struct.pack("<Q", amount_micro) + bytes([6])

        transfer_ix = Instruction(
            program_id=token_program,
            accounts=[
                AccountMeta(pubkey=sender_ata,   is_signer=False, is_writable=True),
                AccountMeta(pubkey=usdc_mint,     is_signer=False, is_writable=False),
                AccountMeta(pubkey=receiver_ata,  is_signer=False, is_writable=True),
                AccountMeta(pubkey=sender_pubkey, is_signer=True,  is_writable=False),
            ],
            data=bytes(ix_data),
        )

        # ── Transaction signée ────────────────────────
        msg = Message.new_with_blockhash(
            [transfer_ix], sender_pubkey, recent_blockhash,
        )
        tx = Transaction([keypair], msg, recent_blockhash)
        signed_bytes = bytes(tx)

        logger.info(
            f"ProfitEngine — TX {tx_id} signée : "
            f"{amount:.2f} USDC → {to_wallet[:8]}..."
        )

        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE transactions SET status='SIGNED', signed_payload=? WHERE tx_id=?",
                (signed_bytes, tx_id),
            )

        return signed_bytes

    async def _broadcast_transaction(self, tx_id: str, signed_payload: bytes) -> str | None:
        """Broadcast la transaction signée sur Solana."""
        import base64

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    self.cfg["solana_rpc"],
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "sendTransaction",
                        "params": [
                            base64.b64encode(signed_payload).decode(),
                            {"encoding": "base64", "skipPreflight": False},
                        ],
                    },
                )
            data = resp.json()
            tx_hash = data.get("result")
            if not tx_hash:
                logger.error(f"ProfitEngine — broadcast error : {data.get('error')}")
                return None

            now = datetime.now(timezone.utc).isoformat()
            with get_db() as conn:
                conn.execute(
                    """UPDATE transactions
                       SET status='SUBMITTED', submitted_at=?, solana_tx_hash=?
                       WHERE tx_id=?""",
                    (now, tx_hash, tx_id),
                )
            logger.info(f"ProfitEngine — TX {tx_id} soumise : {tx_hash}")
            return tx_hash

        except Exception as exc:
            logger.error(f"ProfitEngine — broadcast exception : {exc}")
            return None

    async def _poll_confirmation(self, tx_id: str, tx_hash: str,
                                  max_attempts: int = 60) -> bool:
        """Poll la confirmation Solana — max 60 × 2s = 120s."""
        for attempt in range(max_attempts):
            await asyncio.sleep(2)
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        self.cfg["solana_rpc"],
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "getTransaction",
                            "params": [
                                tx_hash,
                                {"encoding": "json", "maxSupportedTransactionVersion": 0},
                            ],
                        },
                    )
                data = resp.json()
                result = data.get("result")
                if result:
                    err = result.get("meta", {}).get("err")
                    now = datetime.now(timezone.utc).isoformat()
                    if err is None:
                        with get_db() as conn:
                            conn.execute(
                                """UPDATE transactions
                                   SET status='CONFIRMED', confirmed_at=?, solana_slot=?
                                   WHERE tx_id=?""",
                                (now, result.get("slot"), tx_id),
                            )
                        logger.success(f"ProfitEngine — TX {tx_id} confirmée (slot {result.get('slot')})")
                        return True
                    else:
                        self._mark_failed(tx_id, f"solana_error:{err}")
                        return False

            except Exception as exc:
                logger.debug(f"ProfitEngine — poll attempt {attempt+1} error : {exc}")

        # Timeout sans confirmation — garder SUBMITTED (pas FAILED)
        # Le TX est peut-être confirmé mais le RPC rate-limité
        # BootGuardian vérifiera au prochain redémarrage
        logger.warning(
            f"ProfitEngine — TX {tx_id} non confirmée après {max_attempts} tentatives "
            f"(laissée SUBMITTED pour recovery BootGuardian)"
        )
        return False

    def _mark_failed(self, tx_id: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                "UPDATE transactions SET status='FAILED', failed_at=?, error_message=? WHERE tx_id=?",
                (now, error, tx_id),
            )
        logger.error(f"ProfitEngine — TX {tx_id} FAILED : {error}")
        log_event("TX_FAILED", {"tx_id": tx_id, "error": error})

    # ── Monthly Settlement ────────────────────────────

    async def monthly_settlement(self) -> dict:
        """
        Appelé le 1er du mois à 00:01 UTC.
        1. Payer VPS (idempotent par mois)
        2. Calculer profit net
        3. Transférer 80% vers OWNER_SOLFLARE si > 5 USDC
        """
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        logger.info(f"ProfitEngine — settlement mensuel {month_key}")
        result = {"month": month_key, "vps_paid": False, "profit_transferred": 0.0}

        # 1. Payer VPS
        vps_idem = f"vps_payment_{month_key}"
        vps_paid_state = get_state(f"vps_paid_{month_key}", "false")

        if vps_paid_state != "true":
            vps_cost = self.cfg["vps_monthly_cost"]
            balance = await self.get_usdc_balance()

            if balance >= vps_cost + self.cfg["reserve_minimum"]:
                try:
                    await self.execute_transfer({
                        "tx_type": "VPS_PAYMENT",
                        "amount_usdc": vps_cost,
                        "to_wallet": self.cfg["wallet_public"],  # placeholder Infomaniak
                        "memo": f"0xeeMini VPS {month_key}",
                        "idempotency_key": vps_idem,
                    })
                    set_state(f"vps_paid_{month_key}", "true")
                    result["vps_paid"] = True
                    logger.success(f"ProfitEngine — VPS payé pour {month_key}")
                except Exception as exc:
                    logger.error(f"ProfitEngine — paiement VPS échoué : {exc}")
            else:
                logger.warning(f"ProfitEngine — solde insuffisant pour VPS : {balance:.2f} USDC")
        else:
            result["vps_paid"] = True
            logger.info(f"ProfitEngine — VPS déjà payé pour {month_key}")

        # 2. Calculer et transférer profit
        balance = await self.get_usdc_balance()
        reserve = self.cfg["reserve_minimum"]

        if balance > reserve:
            available_profit = balance - reserve
            transfer_amount = available_profit * PROFIT_OWNER_RATIO

            if transfer_amount >= PROFIT_MIN_TRANSFER_USDC:
                profit_idem = f"profit_transfer_{month_key}"
                try:
                    await self.execute_transfer({
                        "tx_type": "PROFIT_TRANSFER",
                        "amount_usdc": round(transfer_amount, 6),
                        "to_wallet": self.cfg["owner_address"],
                        "memo": f"0xeeMini profit {month_key} ({PROFIT_OWNER_RATIO*100:.0f}%)",
                        "idempotency_key": profit_idem,
                    })
                    result["profit_transferred"] = transfer_amount
                    set_state(f"profit_transferred_{month_key}", str(transfer_amount))
                    logger.success(
                        f"ProfitEngine — {transfer_amount:.4f} USDC transférés vers owner"
                    )
                except Exception as exc:
                    logger.error(f"ProfitEngine — transfert profit échoué : {exc}")
            else:
                logger.info(
                    f"ProfitEngine — profit {transfer_amount:.4f} USDC < seuil {PROFIT_MIN_TRANSFER_USDC} USDC"
                )

        log_event("MONTHLY_SETTLEMENT", result)
        return result

    # ── Upgrade Evaluation ────────────────────────────

    async def evaluate_upgrade(self) -> dict:
        """
        Évalue si un upgrade VPS est justifié.
        Critères : 3 mois profitables + RAM > 85% + profit > delta_cost × 3
        """
        import psutil

        current_plan = self.cfg["current_vps_plan"]
        current_cost = INFOMANIAK_PLANS.get(current_plan, 5.0)

        # Chercher le plan suivant
        plans = list(INFOMANIAK_PLANS.keys())
        current_idx = plans.index(current_plan) if current_plan in plans else 0
        if current_idx >= len(plans) - 1:
            return {
                "should_upgrade": False,
                "target_plan": None,
                "justification": "Déjà sur le plan maximum",
            }

        next_plan = plans[current_idx + 1]
        next_cost = INFOMANIAK_PLANS[next_plan]
        delta_cost = next_cost - current_cost

        # RAM
        ram = psutil.virtual_memory()
        ram_pct = ram.percent

        # Profits des 3 derniers mois
        profitable_months = 0
        total_profit = 0.0
        for i in range(1, 4):
            from datetime import timedelta
            d = datetime.now(timezone.utc).replace(day=1)
            # approx 3 mois en arrière
            month_key = (d.replace(year=d.year if d.month > i else d.year - 1,
                                    month=(d.month - i) % 12 or 12)).strftime("%Y-%m")
            p = float(get_state(f"profit_transferred_{month_key}", "0.0"))
            if p > 0:
                profitable_months += 1
                total_profit += p

        avg_monthly_profit = total_profit / 3

        should_upgrade = (
            profitable_months >= 3
            and ram_pct > 85.0
            and avg_monthly_profit > delta_cost * 3
        )

        result = {
            "should_upgrade": should_upgrade,
            "target_plan": next_plan if should_upgrade else None,
            "current_plan": current_plan,
            "current_cost_usd": current_cost,
            "next_plan_cost_usd": next_cost,
            "ram_pct": ram_pct,
            "profitable_months": profitable_months,
            "avg_monthly_profit_usdc": avg_monthly_profit,
            "justification": (
                f"RAM {ram_pct:.1f}%, {profitable_months}/3 mois profitables, "
                f"profit moyen {avg_monthly_profit:.2f} USDC/mois"
            ),
        }

        log_event("UPGRADE_EVALUATION", result)
        return result

    # ── Wallet Status ─────────────────────────────────

    def print_wallet_status(self) -> None:
        """Affiche le statut du wallet — utilisé par mini wallet."""
        import asyncio as _asyncio

        try:
            from rich.console import Console
            from rich.table import Table
            console = Console()
            use_rich = True
        except ImportError:
            use_rich = False

        balance = _asyncio.run(self.get_usdc_balance())
        reserve = self.cfg["reserve_minimum"]
        owner = self.cfg["owner_address"]
        wallet = self.cfg["wallet_public"]

        with get_db() as conn:
            last_txs = conn.execute(
                """SELECT tx_type, status, amount_usdc, to_wallet, created_at
                   FROM transactions ORDER BY created_at DESC LIMIT 5"""
            ).fetchall()

        if use_rich:
            console.print(f"\n[bold cyan]0xeeMini Wallet Status[/bold cyan] — [dim]https://mini.0xee.li[/dim]")
            console.print(f"  Agent wallet : [yellow]{wallet}[/yellow]")
            console.print(f"  Owner wallet : [green]{owner}[/green]")
            console.print(f"  Solde USDC   : [bold]{'%.4f' % balance} USDC[/bold]")
            console.print(f"  Réserve min  : {reserve:.2f} USDC")
            console.print(f"  Excédent     : [{'green' if balance > reserve else 'red'}]{max(0, balance - reserve):.4f} USDC[/]")

            if last_txs:
                table = Table(title="5 dernières transactions")
                for col in ["Type", "Statut", "Montant USDC", "Destination", "Date"]:
                    table.add_column(col)
                for tx in last_txs:
                    table.add_row(
                        tx["tx_type"], tx["status"],
                        f"{tx['amount_usdc']:.4f}",
                        (tx["to_wallet"] or "")[:16] + "...",
                        tx["created_at"],
                    )
                console.print(table)
        else:
            print(f"\n=== 0xeeMini Wallet Status ===")
            print(f"Agent : {wallet}")
            print(f"Owner : {owner}")
            print(f"Solde : {balance:.4f} USDC")
            print(f"Réserve min : {reserve:.2f} USDC")
            print(f"Excédent : {max(0, balance - reserve):.4f} USDC")
