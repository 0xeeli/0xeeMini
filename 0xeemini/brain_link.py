# ─────────────────────────────────────────────────────
# 0xeeMini v0.1.0 — BrainLink
# https://mini.0xee.li
#
# Stratégie cerveau :
#   Étape 1 — Réflexe GGUF  (toujours, ~0.3s, gratuit)
#              → fast-path WAIT si throttle Claude pas expiré
#   Étape 2 — Claude Haiku  (throttlé 10min, ou si réflexe ≠ WAIT)
#              → cerveau principal, JSON fiable
#   Étape 3 — Ollama local  (optionnel, si configuré)
#   Fallback  — WAIT
# ─────────────────────────────────────────────────────

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from loguru import logger

from .constitution import build_prompt, build_reflex_prompt, SYSTEM_PROMPT, REFLEX_SYSTEM_PROMPT
from .core import get_state, set_state, log_event


class BrainLink:
    """
    Orchestre les cerveaux de 0xeeMini.
    Claude Haiku = cerveau principal (throttlé).
    GGUF réflexe = gardien bare-metal (toutes les 60s).
    Ollama = optionnel.
    """

    _LOCAL_ALIVE_CACHE: tuple[bool, float] | None = None
    _CACHE_TTL = 30.0  # secondes

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self._claude_spent_usd: float = float(get_state("claude_spent_usd", "0.0"))
        self._last_claude_call: float = 0.0  # timestamp dernier appel Claude

    # ── Claude budget & throttle ───────────────────────

    def _claude_budget_remaining(self) -> float:
        budget = self.cfg.get("claude_budget", 5.0)
        return max(0.0, budget - self._claude_spent_usd)

    def _claude_throttle_expired(self) -> bool:
        throttle = self.cfg.get("claude_throttle_secs", 600)
        return (time.time() - self._last_claude_call) >= throttle

    # ── Think avec Constitution ────────────────────────

    def think_with_constitution(self, runtime_state: dict) -> dict:
        """
        Chaîne de décision :
          Étape 1 — Réflexe GGUF (toujours, gratuit, ~0.3s)
                     Fast-path : si action=WAIT et throttle Claude pas expiré → retour direct.
          Étape 2 — Claude Haiku (si throttle expiré ou réflexe ≠ WAIT)
          Étape 3 — Ollama local (si configuré, fallback secondaire)
          Fallback  — réflexe ou WAIT
        """
        # ── Étape 1 : Réflexe GGUF ─────────────────────
        reflex = self._think_reflex(runtime_state)
        reflex_action = "WAIT"

        # Fast-path réflexe si throttle Claude pas expiré
        # (quel que soit l'action du réflexe — Claude décide 1x/10min max)
        if reflex is not None and not self._claude_throttle_expired():
            logger.debug("BrainLink — fast-path réflexe (Claude throttlé)")
            return reflex

        # ── Étape 2 : Claude Haiku ─────────────────────
        if self._claude_budget_remaining() > 0:
            prompt = build_prompt(runtime_state)
            messages = [{"role": "user", "content": prompt}]
            result = self._think_claude(messages, "constitution")
            # Throttle mis à jour même sur erreur (évite le spam)
            self._last_claude_call = time.time()
            if result["response"] is not None:
                return self._parse_json_response(result)

        # ── Étape 3 : Ollama local (optionnel) ─────────
        if self.is_local_alive():
            prompt = build_prompt(runtime_state)
            messages = [{"role": "user", "content": prompt}]
            result = self._think_local(messages, "constitution")
            if result["response"] is not None:
                return self._parse_json_response(result)

        # ── Fallback ────────────────────────────────────
        if reflex is not None:
            logger.warning("BrainLink — Claude/Ollama indisponibles, réflexe utilisé")
            return reflex

        logger.warning("BrainLink — tous les cerveaux indisponibles → WAIT")
        return self._fallback_wait("all_brains_down")

    # ── Claude API ─────────────────────────────────────

    def _think_claude(self, messages: list[dict], task_type: str) -> dict:
        api_key = self.cfg.get("claude_api_key", "")
        if not api_key:
            logger.debug("BrainLink — CLAUDE_API_KEY absent, skip")
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
            # Haiku 4.5 : $0.80/M input, $4.00/M output
            cost = (
                usage.get("input_tokens", 0) * 0.0000008
                + usage.get("output_tokens", 0) * 0.000004
            )

            self._claude_spent_usd += cost
            set_state("claude_spent_usd", str(self._claude_spent_usd))
            log_event("CLAUDE_API_CALL", {
                "cost_usd": round(cost, 6),
                "task_type": task_type,
                "budget_remaining": round(self._claude_budget_remaining(), 4),
            })
            logger.info(
                f"BrainLink — Claude Haiku répondu "
                f"(coût: ${cost:.5f}, budget restant: ${self._claude_budget_remaining():.3f})"
            )

            content = data["content"][0]["text"]
            return {"response": content, "source": "claude_api", "cost_usd": cost}

        except Exception as exc:
            logger.error(f"Claude API — erreur : {exc}")
            return {"response": None, "source": "claude_error", "cost_usd": 0.0}

    # ── Réflexe GGUF ───────────────────────────────────

    def _think_reflex(self, runtime_state: dict) -> dict | None:
        """
        Cerveau réflexe — Qwen2.5 0.5B GGUF sur VPS.
        Chargé en RAM uniquement le temps de l'inférence, puis libéré.
        Retourne un dict Constitution-compatible, ou None si indisponible.
        """
        model_path = self.cfg.get("brain_model_path", "")
        if not model_path or not Path(model_path).exists():
            logger.debug("Cerveau réflexe — modèle absent, skip")
            return None

        try:
            from llama_cpp import Llama  # import tardif — non obligatoire au démarrage
        except ImportError:
            logger.debug("Cerveau réflexe — llama-cpp-python non installé, skip")
            return None

        try:
            logger.info("🧠 Cerveau réflexe — chargement GGUF...")
            llm = Llama(
                model_path=model_path,
                n_ctx=512,
                n_threads=2,
                verbose=False,
            )

            resp = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": REFLEX_SYSTEM_PROMPT},
                    {"role": "user", "content": build_reflex_prompt(runtime_state)},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=200,
            )
            raw = resp["choices"][0]["message"]["content"].strip()

            del llm  # Libère ~400 Mo de RAM immédiatement
            logger.info("🧠 Cerveau réflexe — déchargé, réponse reçue")

            data = json.loads(raw)
            return self._normalize_reflex_response(data, runtime_state)

        except json.JSONDecodeError as exc:
            logger.warning(f"Cerveau réflexe — JSON invalide : {exc}")
            return None
        except Exception as exc:
            logger.error(f"Cerveau réflexe — erreur : {exc}")
            return None

    def _normalize_reflex_response(self, data: dict, runtime_state: dict) -> dict:
        """Convertit la réponse compacte du réflexe vers le format Constitution complet."""
        now = datetime.now(timezone.utc).isoformat()
        action = data.get("action", "WAIT")
        balance = runtime_state.get("balance_usdc", 0.0)

        if action not in {"WAIT", "EXECUTE_TRANSFER", "ALERT_OWNER", "ABORT"}:
            action = "WAIT"

        # Le réflexe ne peut jamais déclencher un transfert sans détails (to_wallet, amount)
        if action == "EXECUTE_TRANSFER" and balance <= 0:
            action = "WAIT"

        reserve = runtime_state.get("reserve_minimum", 15.0)
        vps_paid = runtime_state.get("vps_paid_this_month", False)
        profit = runtime_state.get("monthly_profit_so_far", 0.0)
        threat = data.get("threat", "YELLOW")

        return {
            "0xeemini_version": "0.1",
            "timestamp_utc": now,
            "thinking": data.get("rationale", "Cerveau réflexe"),
            "situation_assessment": {
                "current_usdc_balance": balance,
                "monthly_profit_so_far": profit,
                "vps_paid_this_month": vps_paid,
                "threat_level": threat,
                "threat_reason": data.get("rationale"),
            },
            "decision": {
                "action": action,
                "action_details": {
                    "tx_type": None,
                    "amount_usdc": None,
                    "to_wallet": None,
                    "memo": None,
                    "idempotency_key": None,
                },
                "confidence": float(data.get("confidence", 0.7)),
                "rationale": data.get("rationale", "Cerveau réflexe"),
            },
            "next_cycle_in_seconds": 60,
            "flags": {
                "requires_human_validation": data.get("kill_switch", False),
                "kill_switch_armed": data.get("kill_switch", False),
                "recovery_mode": threat == "RED",
            },
            "_source": "reflex_gguf",
            "_cost_usd": 0.0,
        }

    # ── Ollama local (optionnel) ───────────────────────

    def is_local_alive(self) -> bool:
        """Cache 30s — vérifie Ollama via tunnel ou SSH."""
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
        tunnel_port = self.cfg.get("ollama_tunnel_port", 0)
        if tunnel_port:
            try:
                import urllib.request
                urllib.request.urlopen(
                    f"http://127.0.0.1:{tunnel_port}/api/tags", timeout=3
                )
                logger.debug(f"BrainLink — tunnel Ollama actif sur :{tunnel_port}")
                return True
            except Exception:
                return False

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
                return True
        except Exception:
            pass
        return False

    def _think_local(self, messages: list[dict], task_type: str) -> dict:
        model = self.cfg["brain_model"]
        payload = json.dumps({"model": model, "messages": messages, "stream": False})

        tunnel_port = self.cfg.get("ollama_tunnel_port", 0)
        if tunnel_port:
            try:
                with httpx.Client(timeout=120) as client:
                    resp = client.post(
                        f"http://127.0.0.1:{tunnel_port}/api/chat",
                        content=payload,
                        headers={"Content-Type": "application/json"},
                    )
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                return {"response": content or None, "source": "local_ollama_tunnel", "cost_usd": 0.0}
            except Exception as exc:
                logger.warning(f"BrainLink tunnel — erreur : {exc}")
                return {"response": None, "source": "tunnel_error", "cost_usd": 0.0}

        host = self.cfg["local_ssh_host"]
        user = self.cfg["local_ssh_user"]
        port = self.cfg["local_ssh_port"]
        ollama_port = self.cfg["ollama_port"]

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
                return {"response": None, "source": "local_ssh_error", "cost_usd": 0.0}
            data = json.loads(result.stdout)
            content = data.get("message", {}).get("content", "")
            return {"response": content or None, "source": "local_ollama", "cost_usd": 0.0}
        except Exception as exc:
            logger.error(f"BrainLink local — erreur : {exc}")
            return {"response": None, "source": "local_error", "cost_usd": 0.0}

    # ── GitHub Audit LLM ───────────────────────────────

    async def analyze_github_commits(self, payload: dict) -> dict:
        """
        Analyse LLM des commits GitHub pour détecter le fake-dev.
        Priorité : Ollama local → Claude → fallback score=50.
        """
        repo = payload.get("repo", "unknown")

        system_prompt = (
            "Tu es un expert en audit technique de projets blockchain et crypto.\n"
            "Tu analyses des données de commits GitHub pour détecter les équipes\n"
            "qui simulent de l'activité de développement pour tromper les investisseurs.\n"
            "Tu réponds UNIQUEMENT en JSON valide. Zéro texte hors du JSON."
        )

        user_prompt = (
            f"Analyse ces données de commits GitHub pour le repo : {repo}\n\n"
            f"MÉTRIQUES BRUTES :\n"
            f"{json.dumps(payload['metrics'], indent=2)}\n\n"
            f"ÉCHANTILLON DES 20 DERNIERS COMMITS :\n"
            f"{json.dumps(payload['commits_sample'], indent=2)}\n\n"
            "Évalue ces signaux d'alerte :\n"
            "- Ratio de modifications cosmétiques (CSS, README, JSON de config)\n"
            "- Absence de travail sur les fichiers critiques (smart contracts .sol,\n"
            "  fichiers core, logique métier)\n"
            "- Commits vides ou quasi-vides (< 5 lignes)\n"
            "- Messages de commit vagues (\"fix\", \"update\", \"minor changes\")\n"
            "- Concentration sur 1-2 auteurs seulement\n"
            "- Pics d'activité artificiels (beaucoup de commits le même jour)\n"
            "- Ratio additions/deletions suspect (réécriture infinie du même code)\n\n"
            "Réponds avec ce JSON strict et rien d'autre :\n"
            "{\n"
            "  \"bullshit_score\": <entier 0-100>,\n"
            "  \"verdict\": \"<phrase courte max 15 mots>\",\n"
            "  \"technical_reality\": \"<description concrète de ce que fait vraiment l'équipe, max 50 mots>\",\n"
            "  \"red_flags\": [\"<flag1>\", \"<flag2>\", ...],\n"
            "  \"green_flags\": [\"<flag1>\", ...],\n"
            "  \"recommendation\": \"INVEST|CAUTION|AVOID\",\n"
            "  \"confidence\": <float 0.0-1.0>\n"
            "}\n\n"
            "Exemples de technical_reality attendus :\n"
            "- \"L'équipe modifie uniquement des fichiers CSS et README. Zéro commit sur les smart contracts depuis 3 mois.\"\n"
            "- \"Code solide. 80% des commits touchent la logique core. Plusieurs auteurs actifs.\"\n"
            "- \"Pattern classique de wash development : 15 micro-commits par jour sur des fichiers de config.\""
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # ── 1. Ollama local ──────────────────────────────
        if self.is_local_alive():
            try:
                import asyncio as _asyncio
                loop = _asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, self._think_local, messages, "github_audit"
                )
                if result.get("response"):
                    parsed = self._parse_audit_response(result, repo)
                    if parsed.get("confidence", 0) > 0:
                        return parsed
            except Exception as exc:
                logger.warning(f"analyze_github_commits — Ollama échoué : {exc}")

        # ── 2. Claude fallback ───────────────────────────
        if self._claude_budget_remaining() > 0:
            result = self._think_claude(messages, "github_audit")
            self._last_claude_call = time.time()
            if result.get("response"):
                return self._parse_audit_response(result, repo)

        # ── 3. Indisponible ──────────────────────────────
        logger.warning(f"analyze_github_commits — tous cerveaux indisponibles pour {repo}")
        return {
            "bullshit_score": 50,
            "verdict": "Analyse impossible — LLM indisponible",
            "technical_reality": "Aucun cerveau LLM disponible pour l'analyse.",
            "red_flags": [],
            "green_flags": [],
            "recommendation": "CAUTION",
            "confidence": 0.0,
            "_source": "fallback",
            "_cost_usd": 0.0,
        }

    def _parse_audit_response(self, result: dict, repo: str) -> dict:
        """Parse la réponse LLM d'un audit GitHub."""
        raw = result["response"].strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
        try:
            data = json.loads(raw)
            data.setdefault("_source", result.get("source", "unknown"))
            data.setdefault("_cost_usd", result.get("cost_usd", 0.0))
            logger.info(
                f"GitHubAudit — bullshit_score={data.get('bullshit_score')} "
                f"via {result.get('source', '?')}"
            )
            return data
        except json.JSONDecodeError as exc:
            logger.warning(f"_parse_audit_response — JSON invalide : {exc}")
            return {
                "bullshit_score": 50,
                "verdict": "Réponse LLM non parseable",
                "technical_reality": "Parsing JSON échoué.",
                "red_flags": [],
                "green_flags": [],
                "recommendation": "CAUTION",
                "confidence": 0.0,
                "_source": result.get("source", "unknown"),
                "_cost_usd": result.get("cost_usd", 0.0),
            }

    # ── Helpers ────────────────────────────────────────

    def _parse_json_response(self, result: dict) -> dict:
        """Parse une réponse JSON Constitution depuis Claude ou Ollama."""
        raw = result["response"].strip()
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
