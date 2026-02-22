# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — Main (boucle principale APScheduler)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import asyncio
import signal
import sys
import threading
from datetime import datetime, timezone

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
from .hustle_api import app as api_app, increment_cycle
from .brain_link import BrainLink
from .profit_engine import ProfitEngine

_VERSION = "0.1.0"
_PLATFORM = "https://mini.0xee.li"
_CYCLE_COUNT = 0
_START_TIME = datetime.now(timezone.utc)
_SHUTDOWN_EVENT = asyncio.Event()


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


async def _collect_runtime_state(profit_engine: ProfitEngine) -> dict:
    """Collecte l'état runtime pour le cycle Constitution."""
    global _CYCLE_COUNT

    balance_usdc = await profit_engine.get_usdc_balance()
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
    }


async def _route_decision(decision: dict, profit_engine: ProfitEngine) -> None:
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
        await profit_engine.execute_transfer(details)

    elif action == "RUN_HUSTLE":
        logger.info("RUN_HUSTLE — génération de contenu (non implémenté en Phase 1)")
        log_event("HUSTLE_TRIGGERED", {"details": details})

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


async def _main_cycle(brain: BrainLink, profit_engine: ProfitEngine) -> None:
    """Un cycle complet de l'agent."""
    global _CYCLE_COUNT
    _CYCLE_COUNT += 1
    increment_cycle()

    cycle_start = datetime.now(timezone.utc).isoformat()

    try:
        runtime_state = await _collect_runtime_state(profit_engine)
        decision = brain.think_with_constitution(runtime_state)
        await _route_decision(decision, profit_engine)

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

    logger.info("Démarrage du serveur HustleAPI...")
    _start_api_server()

    _setup_signal_handlers()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _main_cycle,
        "interval",
        seconds=60,
        args=[brain, profit_engine],
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
    scheduler.start()

    log_event("AGENT_STARTED", {
        "version": _VERSION,
        "platform": _PLATFORM,
        "api_port": CFG["api_port"],
    })
    logger.success(f"0xeeMini v{_VERSION} opérationnel — {_PLATFORM}")

    # Lancer un premier cycle immédiatement
    await _main_cycle(brain, profit_engine)

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
