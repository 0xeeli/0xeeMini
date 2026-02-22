# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — BrainLink (SSH → Ollama bridge)
# https://mini.0xee.li
# ─────────────────────────────────────────────────────

import json
import subprocess
import time
from datetime import datetime, timezone

import httpx
from loguru import logger

from .constitution import build_prompt, SYSTEM_PROMPT
from .core import get_state, set_state, log_event


class BrainLink:
    """
    Pont vers le cerveau local (Ollama via SSH).
    Fallback vers Claude API si le cerveau est indisponible.
    """

    _LOCAL_ALIVE_CACHE: tuple[bool, float] | None = None
    _CACHE_TTL = 30.0  # secondes

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._claude_spent_usd: float = float(get_state("claude_spent_usd", "0.0"))

    # ── Availability check ────────────────────────────

    def is_local_alive(self) -> bool:
        """Cache 30s — vérifie Ollama via SSH."""
        now = time.time()
        if (
            BrainLink._LOCAL_ALIVE_CACHE is not None
            and (now - BrainLink._LOCAL_ALIVE_CACHE[1]) < self._CACHE_TTL
        ):
            return BrainLink._LOCAL_ALIVE_CACHE[0]

        result = self._check_local()
        BrainLink._LOCAL_ALIVE_CACHE = (result, now)
        return result

    def _check_local(self) -> bool:
        host = self.cfg.get("local_ssh_host", "")
        if not host:
            return False
        user = self.cfg.get("local_ssh_user", "pankso")
        port = self.cfg.get("local_ssh_port", 22)
        ollama_port = self.cfg.get("ollama_port", 11434)

        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=3",
                    "-p", str(port),
                    f"{user}@{host}",
                    f"curl -s --max-time 3 http://localhost:{ollama_port}/api/tags",
                ],
                capture_output=True,
                timeout=5,
                text=True,
            )
            if result.returncode == 0 and "models" in result.stdout:
                logger.debug("BrainLink — cerveau local disponible")
                return True
        except subprocess.TimeoutExpired:
            logger.debug("BrainLink — timeout SSH")
        except Exception as exc:
            logger.debug(f"BrainLink — SSH check error : {exc}")

        return False

    # ── Think ─────────────────────────────────────────

    def think(self, messages: list[dict], task_type: str = "general") -> dict:
        """
        Envoie une requête au cerveau.
        Retourne {"response": str, "source": str, "cost_usd": float}
        """
        if self.is_local_alive():
            result = self._think_local(messages, task_type)
            if result["response"] is not None:
                return result

        # Fallback Claude
        if self._claude_budget_remaining() > 0:
            return self._think_claude(messages, task_type)

        logger.warning("BrainLink — budget Claude épuisé, cerveau local injoignable")
        return {"response": None, "source": "budget_exceeded", "cost_usd": 0.0}

    def _think_local(self, messages: list[dict], task_type: str) -> dict:
        host = self.cfg["local_ssh_host"]
        user = self.cfg["local_ssh_user"]
        port = self.cfg["local_ssh_port"]
        ollama_port = self.cfg["ollama_port"]
        model = self.cfg["brain_model"]

        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": False,
        })

        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=5",
                    "-p", str(port),
                    f"{user}@{host}",
                    f"curl -s -X POST http://localhost:{ollama_port}/api/chat "
                    f"-H 'Content-Type: application/json' "
                    f"-d '{payload.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'",
                ],
                capture_output=True,
                timeout=120,
                text=True,
            )
            if result.returncode != 0:
                logger.warning(f"BrainLink local — SSH error : {result.stderr[:200]}")
                return {"response": None, "source": "local_ssh_error", "cost_usd": 0.0}

            data = json.loads(result.stdout)
            content = data.get("message", {}).get("content", "")
            return {"response": content, "source": "local_ollama", "cost_usd": 0.0}

        except subprocess.TimeoutExpired:
            logger.warning("BrainLink local — timeout 120s")
            return {"response": None, "source": "local_timeout", "cost_usd": 0.0}
        except json.JSONDecodeError as exc:
            logger.warning(f"BrainLink local — JSON parse error : {exc}")
            return {"response": None, "source": "local_json_error", "cost_usd": 0.0}
        except Exception as exc:
            logger.error(f"BrainLink local — erreur inattendue : {exc}")
            return {"response": None, "source": "local_error", "cost_usd": 0.0}

    def _think_claude(self, messages: list[dict], task_type: str) -> dict:
        api_key = self.cfg.get("claude_api_key", "")
        if not api_key:
            return {"response": None, "source": "no_claude_key", "cost_usd": 0.0}

        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 1024,
                        "system": SYSTEM_PROMPT,
                        "messages": messages,
                    },
                )

            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"Claude API error {resp.status_code} : {data}")
                return {"response": None, "source": "claude_api_error", "cost_usd": 0.0}

            usage = data.get("usage", {})
            # Estimation coût haiku : ~$0.25/M input, $1.25/M output
            cost = (
                usage.get("input_tokens", 0) * 0.00000025
                + usage.get("output_tokens", 0) * 0.00000125
            )

            self._claude_spent_usd += cost
            set_state("claude_spent_usd", str(self._claude_spent_usd))
            log_event("CLAUDE_API_CALL", {"cost_usd": cost, "task_type": task_type})

            content = data["content"][0]["text"]
            return {"response": content, "source": "claude_api", "cost_usd": cost}

        except Exception as exc:
            logger.error(f"Claude API — erreur : {exc}")
            return {"response": None, "source": "claude_error", "cost_usd": 0.0}

    def _claude_budget_remaining(self) -> float:
        budget = self.cfg.get("claude_budget", 2.0)
        return max(0.0, budget - self._claude_spent_usd)

    # ── Think avec Constitution ────────────────────────

    def think_with_constitution(self, runtime_state: dict) -> dict:
        """
        Construit le prompt depuis la Constitution,
        appelle think(), parse le JSON strict.
        En cas d'échec parsing → retourne action=WAIT.
        """
        prompt = build_prompt(runtime_state)
        messages = [{"role": "user", "content": prompt}]

        result = self.think(messages, task_type="constitution")

        if result["response"] is None:
            logger.warning("BrainLink — pas de réponse, action=WAIT")
            return self._fallback_wait(result["source"])

        # Tentative parse JSON strict
        raw = result["response"].strip()

        # Nettoyer les éventuels blocs markdown ```json
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw

        try:
            decision = json.loads(raw)
            decision["_source"] = result["source"]
            decision["_cost_usd"] = result["cost_usd"]
            return decision
        except json.JSONDecodeError as exc:
            logger.error(f"BrainLink — JSON parse échoué : {exc}\nRaw: {raw[:200]}")
            return self._fallback_wait(f"json_parse_error:{exc}")

    @staticmethod
    def _fallback_wait(reason: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "0xeemini_version": "0.1",
            "timestamp_utc": now,
            "thinking": f"fallback WAIT — {reason}",
            "situation_assessment": {
                "current_usdc_balance": 0.0,
                "monthly_profit_so_far": 0.0,
                "vps_paid_this_month": False,
                "threat_level": "YELLOW",
                "threat_reason": reason,
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
                "confidence": 0.0,
                "rationale": f"Fallback WAIT : {reason}",
            },
            "next_cycle_in_seconds": 60,
            "flags": {
                "requires_human_validation": False,
                "kill_switch_armed": False,
                "recovery_mode": True,
            },
            "_source": "fallback",
            "_cost_usd": 0.0,
        }
