# ─────────────────────────────────────────────────────
# 0xeeMini v0.2.0 — BrainLink
# https://mini.0xee.li
#
# Stratégie cerveau :
#   Étape 1 — Réflexe GGUF  (toujours, ~0.3s, gratuit)
#              → fast-path WAIT si throttle Claude pas expiré
#   Étape 2 — Claude Haiku  (throttlé 10min, ou si réflexe ≠ WAIT)
#              → cerveau principal, JSON fiable
#   Fallback  — WAIT
#
# GitHub Audit :
#   Primaire  — Claude Haiku
#   Samouraï  — qwen2.5-coder-1.5b GGUF (n_ctx=1024, éphémère)
# ─────────────────────────────────────────────────────

import json
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
    Mode Samouraï = fallback audit GGUF 1.5B (éphémère, libéré immédiatement).
    """

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
          Fallback  — réflexe ou WAIT
        """
        # ── Étape 1 : Réflexe GGUF ─────────────────────
        reflex = self._think_reflex(runtime_state)

        # Fast-path réflexe si throttle Claude pas expiré
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

        # ── Fallback ────────────────────────────────────
        if reflex is not None:
            logger.warning("BrainLink — Claude indisponible, réflexe utilisé")
            return reflex

        logger.warning("BrainLink — tous les cerveaux indisponibles → WAIT")
        return self._fallback_wait("all_brains_down")

    # ── Claude API ─────────────────────────────────────

    def _think_claude(
        self,
        messages: list[dict],
        task_type: str,
        system_override: str | None = None,
        max_tokens: int = 1024,
    ) -> dict:
        api_key = self.cfg.get("claude_api_key", "")
        if not api_key:
            logger.debug("BrainLink — CLAUDE_API_KEY absent, skip")
            return {"response": None, "source": "no_claude_key", "cost_usd": 0.0}

        # Extraire le message system s'il est dans le tableau (API Anthropic l'interdit)
        system_prompt = system_override or SYSTEM_PROMPT
        user_messages = []
        for m in messages:
            if m.get("role") == "system":
                system_prompt = m["content"]  # override depuis le tableau
            else:
                user_messages.append(m)

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
                        "max_tokens": max_tokens,
                        "system": system_prompt,
                        "messages": user_messages,
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

        # Le réflexe GGUF (0.5B) ne peut pas décider des transferts (pas de wallet/amount).
        # EXECUTE_TRANSFER, ABORT, ALERT_OWNER → tous ramenés à WAIT.
        if action not in {"WAIT", "RUN_HUSTLE", "REQUEST_UPGRADE"}:
            if action != "WAIT":
                logger.debug(f"Réflexe GGUF — {action} → WAIT (action réservée à Claude)")
            action = "WAIT"

        reserve = runtime_state.get("reserve_minimum", 15.0)
        vps_paid = runtime_state.get("vps_paid_this_month", False)
        profit = runtime_state.get("monthly_profit_so_far", 0.0)
        status = (
            "BOOTSTRAP" if balance < reserve else
            ("PROFITABLE" if balance > reserve + 5.0 else "OPERATIONAL")
        )

        return {
            "0xeemini_version": "0.1",
            "timestamp_utc": now,
            "thinking": data.get("rationale", "Cerveau réflexe"),
            "situation_assessment": {
                "current_usdc_balance": balance,
                "monthly_profit_so_far": profit,
                "vps_paid_this_month": vps_paid,
                "status": status,
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
                "kill_switch_armed": data.get("kill_switch", False),
            },
            "_source": "reflex_gguf",
            "_cost_usd": 0.0,
        }

    # ── GitHub Audit LLM ───────────────────────────────

    # Extensions purement UI/cosmétiques — filtrées du diff avant passage au Samouraï
    _COSMETIC_SKIP_EXTS = {
        ".css", ".scss", ".less", ".sass",
        ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp",
        ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".map",
    }

    def _preprocess_audit_payload(self, commits_sample: list[dict]) -> str:
        """
        Pré-traite l'échantillon de commits pour le Mode Samouraï.
        - Filtre les fichiers purement UI/CSS/images (garde uniquement la logique backend)
        - Nettoie les espaces excessifs dans patches et messages
        - Tronque à 3200 chars (~800 tokens) pour tenir dans n_ctx=1024
        """
        filtered = []
        for c in commits_sample[:10]:
            backend_files = [
                f for f in c.get("files", [])
                if Path(f["filename"]).suffix.lower() not in self._COSMETIC_SKIP_EXTS
            ]
            # Conserver le commit même s'il n'a que des fichiers cosmétiques
            # (l'info "que des cosmétiques" est importante pour le score)
            files_to_use = backend_files if backend_files else c.get("files", [])[:2]

            entry = {
                "sha": c["sha"][:8],
                "author": c["author"],
                "date": c["date"][:10],  # date seule, pas heure
                "msg": " ".join(c["message"].split())[:80],  # clean whitespace
                "stats": c["stats"],
                "files": [
                    {
                        "f": f["filename"],
                        "s": f["status"],
                        "+": f["additions"],
                        "-": f["deletions"],
                        "p": " ".join((f.get("patch") or "").split())[:120],
                    }
                    for f in files_to_use[:3]
                ],
            }
            filtered.append(entry)

        raw = json.dumps(filtered, separators=(",", ":"))
        # Tronque à 3200 chars ≈ 800 tokens
        return raw[:3200]

    def _analyze_samurai_sync(self, user_prompt: str, system_prompt: str) -> dict:
        """
        Mode Samouraï — inférence synchrone avec qwen2.5-coder-1.5b.
        n_ctx=1024, éphémère : del llm + gc.collect() après usage.
        """
        import gc

        model_path = self.cfg.get("brain_audit_model_path", "")
        if not model_path or not Path(model_path).exists():
            logger.debug("Mode Samouraï — modèle absent, skip")
            return {"response": None, "source": "samurai_model_absent", "cost_usd": 0.0}

        try:
            from llama_cpp import Llama
        except ImportError:
            logger.debug("Mode Samouraï — llama-cpp-python non installé, skip")
            return {"response": None, "source": "samurai_no_llama_cpp", "cost_usd": 0.0}

        llm = None
        try:
            logger.info("⚔️  Mode Samouraï — chargement qwen2.5-coder-1.5b...")
            llm = Llama(
                model_path=model_path,
                n_ctx=1024,
                n_threads=2,
                verbose=False,
            )

            resp = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=400,
            )
            raw = resp["choices"][0]["message"]["content"].strip()

            del llm
            gc.collect()
            logger.info("⚔️  Mode Samouraï — déchargé, réponse reçue")

            return {"response": raw, "source": "samurai_gguf", "cost_usd": 0.0}

        except Exception as exc:
            logger.error(f"Mode Samouraï — erreur : {exc}")
            if llm is not None:
                try:
                    del llm
                    gc.collect()
                except Exception:
                    pass
            return {"response": None, "source": "samurai_error", "cost_usd": 0.0}

    async def analyze_github_commits(self, payload: dict, lang: str = "en") -> dict:
        """
        Analyse LLM des commits GitHub pour détecter le fake-dev.
        Priorité : Claude Haiku → Mode Samouraï (GGUF 1.5B) → fallback score=50.
        lang: "en" (default) | "fr" | other ISO 639-1 codes → falls back to "en"
        """
        import asyncio as _asyncio

        repo = payload.get("repo", "unknown")
        # Only French gets native French output; everything else → English
        _lang = "fr" if lang.startswith("fr") else "en"
        _lang_instruction = (
            "Réponds en français." if _lang == "fr"
            else "Answer in English."
        )

        system_prompt = (
            "Tu es un expert en audit technique de projets blockchain et crypto.\n"
            "Tu analyses des données de commits GitHub pour détecter les équipes\n"
            "qui simulent de l'activité de développement pour tromper les investisseurs.\n"
            f"{_lang_instruction}\n"
            "Tu réponds UNIQUEMENT en JSON valide. Zéro texte hors du JSON."
        )

        # Tronquer le sample pour rester dans le budget tokens (~6k chars max pour Claude)
        sample = payload.get("commits_sample", [])
        truncated_sample = []
        for c in sample[:10]:  # Max 10 commits pour le LLM
            entry = {
                "sha": c["sha"],
                "author": c["author"],
                "date": c["date"],
                "message": c["message"],
                "stats": c["stats"],
                "files": [
                    {
                        "filename": f["filename"],
                        "status": f["status"],
                        "additions": f["additions"],
                        "deletions": f["deletions"],
                        "patch": (f.get("patch") or "")[:150],
                    }
                    for f in c.get("files", [])[:3]  # Max 3 fichiers/commit
                ],
            }
            truncated_sample.append(entry)

        user_prompt = (
            f"Analyse ces données de commits GitHub pour le repo : {repo}\n\n"
            f"MÉTRIQUES BRUTES :\n"
            f"{json.dumps(payload['metrics'], indent=2)}\n\n"
            f"ÉCHANTILLON DES 10 DERNIERS COMMITS (3 fichiers max/commit) :\n"
            f"{json.dumps(truncated_sample, indent=2)}\n\n"
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

        # ── 1. Claude Haiku (primaire) ───────────────────
        if self._claude_budget_remaining() > 0:
            result = self._think_claude(messages, "github_audit")
            self._last_claude_call = time.time()
            if result.get("response"):
                return self._parse_audit_response(result, repo)

        # ── 2. Mode Samouraï — GGUF 1.5B (fallback) ─────
        logger.info(f"analyze_github_commits — Claude indisponible, passage en Mode Samouraï pour {repo}")
        samurai_sample = self._preprocess_audit_payload(sample)
        samurai_prompt = (
            f"Repo: {repo}\n"
            f"Metrics: {json.dumps(payload['metrics'])}\n"
            f"Commits: {samurai_sample}\n\n"
            f"{_lang_instruction}\n"
            "JSON only:\n"
            "{\"bullshit_score\":<0-100>,\"verdict\":\"<15 words max>\","
            "\"technical_reality\":\"<50 words max>\",\"red_flags\":[],\"green_flags\":[],"
            "\"recommendation\":\"INVEST|CAUTION|AVOID\",\"confidence\":<0.0-1.0>}"
        )

        loop = _asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._analyze_samurai_sync, samurai_prompt, system_prompt
        )
        if result.get("response"):
            return self._parse_audit_response(result, repo)

        # ── 3. Fallback heuristique ───────────────────────
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
        """Parse une réponse JSON Constitution depuis Claude."""
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
