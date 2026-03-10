# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — Main (boucle principale APScheduler)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import asyncio
import json
import signal
import sys
import threading
from datetime import datetime, timezone
from importlib import import_module

import psutil
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from .config import CFG
from .core import (
    DB_PATH,
    BootGuardian,
    init_db,
    log_event,
    set_state,
    get_state,
    setup_logging,
)
from .hustle_api import app as api_app, increment_cycle, set_brain as _api_set_brain
from .brain_link import BrainLink
from .profit_engine import ProfitEngine
from .hustle_engine import HustleEngine
from .github_auditor import GitHubAuditor, GitHubAuditorError

_VERSION = "0.1.0"
_PLATFORM = "https://mini.0xee.li"
_CYCLE_COUNT = 0
_START_TIME = datetime.now(timezone.utc)
_SHUTDOWN_EVENT = asyncio.Event()

# Cache balance USDC — 1 appel RPC toutes les 5min au lieu de chaque 60s
_BALANCE_CACHE_TTL = 300  # secondes
_cached_balance: float = 0.0
_cached_balance_at: datetime | None = None


def _banner() -> None:
    print(f"""
╔══════════════════════════════════════════╗
║  0xeeMini v{_VERSION} — Démarrage         ║
║  Platform : {_PLATFORM}    ║
║  DB       : {str(DB_PATH)[:36]}
║  Config   : ~/.config/0xeeMini/.env      ║
╚══════════════════════════════════════════╝
""")


def _start_api_server() -> None:
    """Lance uvicorn dans un thread séparé (daemon)."""
    config = uvicorn.Config(
        app=api_app,
        host=CFG["api_host"],
        port=CFG["api_port"],
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    thread.start()
    logger.info(f"HustleAPI démarrée sur {CFG['api_host']}:{CFG['api_port']}")


async def _get_balance_cached(profit_engine: ProfitEngine) -> float:
    """Retourne le solde USDC — RPC appelé max 1x/5min, sinon cache mémoire."""
    global _cached_balance, _cached_balance_at
    now = datetime.now(timezone.utc)
    if (
        _cached_balance_at is None
        or (now - _cached_balance_at).total_seconds() >= _BALANCE_CACHE_TTL
    ):
        _cached_balance = await profit_engine.get_usdc_balance()
        _cached_balance_at = now
        logger.debug(f"Balance RPC refresh : {_cached_balance:.4f} USDC")
    return _cached_balance


async def _collect_runtime_state(profit_engine: ProfitEngine) -> dict:
    """Collecte l'état runtime pour le cycle Constitution."""
    global _CYCLE_COUNT

    balance_usdc = await _get_balance_cached(profit_engine)
    ram = psutil.virtual_memory()
    uptime = int((datetime.now(timezone.utc) - _START_TIME).total_seconds())

    month_key = datetime.now(timezone.utc).strftime("%Y-%m")
    vps_paid = get_state(f"vps_paid_{month_key}", "false") == "true"
    monthly_profit = float(get_state(f"profit_transferred_{month_key}", "0.0"))
    recovery_mode = get_state("last_boot_was_clean", "true") != "true"

    # Derniers événements système
    from .core import get_db
    with get_db() as conn:
        rows = conn.execute(
            "SELECT event_type, payload, ts FROM system_events ORDER BY ts DESC LIMIT 5"
        ).fetchall()
    last_events = [
        {"event_type": r["event_type"], "payload": r["payload"], "ts": r["ts"]}
        for r in rows
    ]

    # Stats catalogue de contenu
    catalog = HustleEngine.get_catalog_stats()

    return {
        "balance_usdc": balance_usdc,
        "ram_pct": ram.percent,
        "vps_paid_this_month": vps_paid,
        "monthly_profit_so_far": monthly_profit,
        "cycle_count": _CYCLE_COUNT,
        "uptime_seconds": uptime,
        "last_events": last_events,
        "reserve_minimum": CFG["reserve_minimum"],
        "vps_monthly_cost": CFG["vps_monthly_cost"],
        "recovery_mode": recovery_mode,
        "owner_address": CFG["owner_address"],
        "agent_wallet": CFG["wallet_public"],
        "content_count": catalog["content_count"],
        "last_content_ts": catalog["last_content_ts"],
    }


async def _route_decision(decision: dict, profit_engine: ProfitEngine, hustle_engine: HustleEngine) -> None:
    """Route la décision du cerveau vers l'action appropriée."""
    action = decision.get("decision", {}).get("action", "WAIT")
    details = decision.get("decision", {}).get("action_details", {})
    flags = decision.get("flags", {})

    logger.info(
        f"Cycle — action={action} | "
        f"confidence={decision.get('decision', {}).get('confidence', 0):.2f} | "
        f"source={decision.get('_source', '?')} | "
        f"threat={decision.get('situation_assessment', {}).get('threat_level', '?')}"
    )

    if action == "WAIT":
        return

    elif action == "EXECUTE_TRANSFER":
        if not details.get("to_wallet") or not details.get("amount_usdc"):
            logger.warning("EXECUTE_TRANSFER sans détails valides → WAIT")
            return
        # Injecter un idempotency_key basé sur la journée si le cerveau n'en fournit pas.
        # Empêche les re-déclenchements multiples du même transfert dans la même journée.
        if not details.get("idempotency_key"):
            day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            wallet_short = (details.get("to_wallet") or "")[:8]
            amount_str = str(details.get("amount_usdc", "")).replace(".", "_")
            details["idempotency_key"] = f"brain_{wallet_short}_{amount_str}_{day_key}"
            logger.debug(f"EXECUTE_TRANSFER — idempotency auto : {details['idempotency_key']}")
        # Marquer VPS comme payé AVANT l'exécution (idempotent, persiste même en cas de crash)
        tx_type = (details.get("tx_type") or "").lower()
        if "vps" in tx_type:
            month_key = datetime.now(timezone.utc).strftime("%Y-%m")
            set_state(f"vps_paid_{month_key}", "true")
            logger.info(f"VPS marqué payé pour {month_key}")
        await profit_engine.execute_transfer(details)
        # Invalider le cache balance après un transfert
        global _cached_balance_at
        _cached_balance_at = None

    elif action == "RUN_HUSTLE":
        log_event("HUSTLE_TRIGGERED", {"details": details})
        result = await hustle_engine.run_hustle(details)
        logger.info(f"RUN_HUSTLE terminé : {result['generated']} insights générés")

    elif action == "REQUEST_UPGRADE":
        eval_result = await profit_engine.evaluate_upgrade()
        if eval_result["should_upgrade"]:
            log_event("UPGRADE_REQUESTED", eval_result)
            logger.warning(
                f"Upgrade recommandé : {eval_result['current_plan']} → {eval_result['target_plan']}"
            )

    elif action == "ALERT_OWNER":
        rationale = decision.get("decision", {}).get("rationale", "")
        webhook_url = CFG.get("webhook_url", "")
        if webhook_url and not webhook_url.startswith("REMPLACER"):
            import httpx
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        webhook_url,
                        json={
                            "content": f"🤖 **0xeeMini Alert** | {rationale}",
                            "username": "0xeeMini",
                        },
                    )
                logger.info(f"Alert envoyée au propriétaire : {rationale[:80]}")
            except Exception as exc:
                logger.error(f"Webhook alert échoué : {exc}")
        log_event("OWNER_ALERT", {"rationale": rationale})

    elif action == "ABORT":
        logger.critical("Action ABORT reçue — arrêt propre initié")
        log_event("ABORT_REQUESTED", decision.get("decision", {}))
        _SHUTDOWN_EVENT.set()


async def _main_cycle(brain: BrainLink, profit_engine: ProfitEngine, hustle_engine: HustleEngine) -> None:
    """Un cycle complet de l'agent."""
    global _CYCLE_COUNT
    _CYCLE_COUNT += 1
    increment_cycle()

    cycle_start = datetime.now(timezone.utc).isoformat()

    try:
        runtime_state = await _collect_runtime_state(profit_engine)
        decision = brain.think_with_constitution(runtime_state)
        await _route_decision(decision, profit_engine, hustle_engine)

        log_event("CYCLE_TICK", {
            "cycle": _CYCLE_COUNT,
            "ts": cycle_start,
            "action": decision.get("decision", {}).get("action", "?"),
            "balance_usdc": runtime_state.get("balance_usdc", 0.0),
            "source": decision.get("_source", "?"),
        })

    except Exception as exc:
        logger.error(f"Erreur cycle {_CYCLE_COUNT} : {exc}")
        log_event("CYCLE_ERROR", {"cycle": _CYCLE_COUNT, "error": str(exc)})


async def _audit_queue_job(brain: BrainLink) -> None:
    """
    Traite la file d'audits GitHub en attente.
    Max 3 audits/heure (rate limit GitHub 60 req/h, ~20 req/audit).
    system_state key "audit_queue" = JSON list de repo_url.
    """
    from .core import get_state, set_state
    raw = get_state("audit_queue", "[]")
    try:
        queue: list = json.loads(raw)
    except Exception:
        queue = []

    if not queue:
        return

    MAX_PER_RUN = 1  # 1 audit par passage (20 appels GitHub, safe)
    processed = 0
    remaining = []

    for repo_url in queue:
        if processed >= MAX_PER_RUN:
            remaining.append(repo_url)
            continue
        try:
            logger.info(f"AuditQueue — démarrage audit : {repo_url}")
            auditor = GitHubAuditor(brain=brain)
            result = await auditor.run(repo_url)
            log_event("AUDIT_QUEUE_PROCESSED", {
                "repo": result["repo"],
                "bullshit_score": result["bullshit_score"],
            })
            logger.success(f"AuditQueue — {result['repo']} → {result['recommendation']}")
            processed += 1
        except GitHubAuditorError as exc:
            logger.error(f"AuditQueue — échec {repo_url} : {exc}")
            log_event("AUDIT_QUEUE_ERROR", {"repo_url": repo_url, "error": str(exc)})
        except Exception as exc:
            logger.error(f"AuditQueue — erreur inattendue {repo_url} : {exc}")
            remaining.append(repo_url)

    set_state("audit_queue", json.dumps(remaining))
    if processed:
        logger.info(f"AuditQueue — {processed} audit(s) traité(s), {len(remaining)} en attente")


async def _refresh_catalog_job() -> None:
    """
    Marque les audits expirés dans le catalogue (expires_at < NOW).
    Les items expirés sont retirés du /catalog mais restent en DB pour archive.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        expired = conn.execute(
            """SELECT COUNT(*) FROM content_cache
               WHERE source = 'github_audit' AND expires_at IS NOT NULL AND expires_at < ?""",
            (now,),
        ).fetchone()[0]
    if expired:
        logger.info(f"RefreshCatalog — {expired} audit(s) expiré(s) retirés du catalogue")
        log_event("CATALOG_REFRESH", {"expired_audits": expired})


async def _monthly_job(profit_engine: ProfitEngine) -> None:
    """Settlement mensuel — 1er du mois 00:01 UTC."""
    logger.info("Settlement mensuel démarré")
    result = await profit_engine.monthly_settlement()
    logger.success(f"Settlement terminé : {result}")


def _setup_signal_handlers() -> None:
    """SIGTERM propre."""
    def _handler(signum, frame):
        logger.info("SIGTERM reçu — arrêt propre en cours...")
        set_state("last_clean_shutdown", datetime.now(timezone.utc).isoformat())
        log_event("CLEAN_SHUTDOWN", {"signal": signum})
        asyncio.get_event_loop().call_soon_threadsafe(_SHUTDOWN_EVENT.set)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


async def run() -> None:
    """Point d'entrée principal."""
    setup_logging()
    _banner()

    logger.info("Initialisation de la base de données...")
    init_db()

    logger.info("BootGuardian — lancement de la séquence de recovery...")
    guardian = BootGuardian(CFG)
    await guardian.run_recovery_sequence()

    brain = BrainLink(CFG)
    profit_engine = ProfitEngine(CFG)
    hustle_engine = HustleEngine(CFG, brain=brain)

    # Injecter le brain dans l'API (pour audit LLM)
    _api_set_brain(brain)

    logger.info("Démarrage du serveur HustleAPI...")
    _start_api_server()

    # ── Telegram bot (optionnel — activé si TELEGRAM_BOT_TOKEN défini) ──
    if CFG.get("telegram_bot_token"):
        def _telegram_thread():
            from .telegram_bot import run_bot
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_bot(CFG))
            except Exception as exc:
                logger.error(f"Telegram bot arrêté : {exc}")
            finally:
                loop.close()

        t = threading.Thread(target=_telegram_thread, daemon=True, name="telegram")
        t.start()
        logger.info("🤖 Telegram bot démarré")

    _setup_signal_handlers()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _main_cycle,
        "interval",
        seconds=60,
        args=[brain, profit_engine, hustle_engine],
        id="main_cycle",
        max_instances=1,
        misfire_grace_time=10,
    )
    scheduler.add_job(
        _monthly_job,
        "cron",
        day=1,
        hour=0,
        minute=1,
        args=[profit_engine],
        id="monthly_settlement",
    )
    scheduler.add_job(
        _audit_queue_job,
        "interval",
        minutes=5,
        args=[brain],
        id="audit_queue",
        max_instances=1,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        _refresh_catalog_job,
        "interval",
        minutes=30,
        id="refresh_catalog",
        max_instances=1,
    )
    scheduler.start()

    log_event("AGENT_STARTED", {
        "version": _VERSION,
        "platform": _PLATFORM,
        "api_port": CFG["api_port"],
    })
    logger.success(f"0xeeMini v{_VERSION} opérationnel — {_PLATFORM}")

    # Lancer un premier cycle immédiatement
    await _main_cycle(brain, profit_engine, hustle_engine)

    # Attendre arrêt
    await _SHUTDOWN_EVENT.wait()
    scheduler.shutdown(wait=False)
    logger.info("0xeeMini arrêté proprement.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
