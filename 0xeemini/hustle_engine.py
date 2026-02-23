# ─────────────────────────────────────────────────────
# 0xeeMini v0.2.0 — HustleEngine (générateur de contenu paywall)
# https://mini.0xee.li
#
# Pipeline :
#   1. Fetch sources (Hacker News, CoinGecko) — gratuit, sans clé
#   2. Générer insights via GGUF VPS (0.5B) > Claude Haiku (fallback)
#   3. Stocker dans content_cache → disponible sur /catalog
# ─────────────────────────────────────────────────────

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from loguru import logger

from .core import get_db, log_event, get_state, set_state

INSIGHT_SYSTEM_PROMPT = """Tu es un analyste tech/crypto concis pour mini.0xee.li.
À partir d'un titre et contexte, génère un insight structuré UNIQUEMENT en JSON valide.
Zéro texte hors du JSON. Langue : français.

Format strict :
{
  "summary": "<2-3 phrases résumant l'essentiel, max 120 mots>",
  "key_insight": "<1 insight unique et percutant, max 80 mots>",
  "actionable": "<que faire concrètement avec cette info, max 50 mots>"
}"""


class HustleEngine:
    """Génère du contenu curé (tech/crypto) pour le paywall 0xeeMini."""

    MAX_PER_HUSTLE = 3   # Insights max par hustle
    HN_MIN_SCORE = 50    # Score HN minimum

    def __init__(self, cfg: dict, brain=None) -> None:
        self.cfg = cfg
        self._brain = brain  # BrainLink optionnel — inférence locale gratuite

    # ── Point d'entrée ─────────────────────────────────

    async def run_hustle(self, action_details: dict) -> dict:
        """
        Pipeline complet : fetch → filter → generate → store.
        Retourne {"generated": int, "skipped": int}.
        """
        logger.info("HustleEngine — démarrage de la génération de contenu")

        items = []
        items.extend(await self._fetch_hn_items())
        items.extend(await self._fetch_coingecko_trending())

        if not items:
            logger.warning("HustleEngine — aucune source disponible")
            return {"generated": 0, "skipped": 0}

        new_items = [i for i in items if not self._already_processed(i)]
        logger.info(f"HustleEngine — {len(new_items)}/{len(items)} nouveaux items")

        generated = 0
        skipped = 0
        for item in new_items[:self.MAX_PER_HUSTLE]:
            insight = await self._generate_insight(item)
            if insight:
                self._store_insight(insight)
                generated += 1
                logger.success(f"HustleEngine — insight : {insight['raw_title'][:60]}…")
            else:
                skipped += 1

        set_state("last_hustle_ts", datetime.now(timezone.utc).isoformat())
        log_event("HUSTLE_COMPLETED", {
            "generated": generated, "skipped": skipped,
            "total_fetched": len(items),
        })
        logger.info(f"HustleEngine — terminé : {generated} générés, {skipped} ignorés")
        return {"generated": generated, "skipped": skipped}

    # ── Sources ────────────────────────────────────────

    async def _fetch_hn_items(self) -> list[dict]:
        """Top stories Hacker News — gratuit, sans clé API."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://hacker-news.firebaseio.com/v0/topstories.json"
                )
                story_ids = resp.json()[:20]

                items = []
                for sid in story_ids:
                    if len(items) >= 4:
                        break
                    r = await client.get(
                        f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                        timeout=5,
                    )
                    data = r.json()
                    if (
                        data.get("type") == "story"
                        and data.get("title")
                        and data.get("score", 0) >= self.HN_MIN_SCORE
                    ):
                        items.append({
                            "source": "hackernews",
                            "title": data["title"],
                            "url": data.get("url", f"https://news.ycombinator.com/item?id={sid}"),
                            "context": (
                                f"Score HN : {data['score']} pts, "
                                f"{data.get('descendants', 0)} commentaires."
                            ),
                        })

            logger.debug(f"HustleEngine — HN : {len(items)} items")
            return items

        except Exception as exc:
            logger.warning(f"HustleEngine — HN fetch échoué : {exc}")
            return []

    async def _fetch_coingecko_trending(self) -> list[dict]:
        """Trending coins CoinGecko — gratuit, sans clé API."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/search/trending",
                    headers={"accept": "application/json"},
                )
                data = resp.json()

            items = []
            for coin_data in data.get("coins", [])[:3]:
                item = coin_data.get("item", {})
                cg_data = item.get("data", {})
                price_change = cg_data.get("price_change_percentage_24h", {})
                change_pct = (
                    price_change.get("usd", 0)
                    if isinstance(price_change, dict)
                    else 0
                )
                items.append({
                    "source": "coingecko",
                    "title": (
                        f"{item['name']} ({item['symbol'].upper()}) — "
                        f"Trending #{(item.get('score') or 0) + 1} CoinGecko"
                    ),
                    "url": f"https://www.coingecko.com/en/coins/{item.get('id', '')}",
                    "context": (
                        f"Trending CoinGecko. "
                        f"Rang market cap : #{item.get('market_cap_rank', '?')}. "
                        f"Variation 24h : {change_pct:+.1f}%. "
                        f"Prix : {cg_data.get('price', '?')} USD."
                    ),
                })

            logger.debug(f"HustleEngine — CoinGecko : {len(items)} items")
            return items

        except Exception as exc:
            logger.warning(f"HustleEngine — CoinGecko fetch échoué : {exc}")
            return []

    # ── Génération : orchestrateur ─────────────────────

    async def _generate_insight(self, item: dict) -> dict | None:
        """
        Génération d'insight. Ordre de priorité :
          1. GGUF sur VPS (Qwen2.5-0.5B) — coût zéro
          2. Claude Haiku                  — fallback payant
        """
        messages = [
            {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"Titre : {item['title']}\n"
                f"Source : {item['source']}\n"
                f"Contexte : {item.get('context', '')}\n"
                f"URL : {item['url']}\n\n"
                f"Génère l'insight en JSON."
            )},
        ]

        # ── 1. GGUF sur VPS ──────────────────────────────
        gguf = await self._generate_gguf(item, messages)
        if gguf:
            return gguf

        # ── 2. Claude Haiku (fallback) ───────────────────
        api_key = self.cfg.get("claude_api_key", "")
        if api_key:
            return await self._generate_claude(item, messages[1]["content"])

        logger.debug("HustleEngine — aucun cerveau disponible pour l'insight")
        return None

    # ── Génération : GGUF local ────────────────────────

    async def _generate_gguf(self, item: dict, messages: list) -> dict | None:
        """Wrapper async pour l'inférence GGUF synchrone (Qwen2.5-0.5B)."""
        model_path = self.cfg.get("brain_model_path", "")
        if not model_path or not Path(model_path).exists():
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._generate_gguf_sync, item, messages
        )

    def _generate_gguf_sync(self, item: dict, messages: list) -> dict | None:
        """Inférence GGUF synchrone — chargement + déchargement immédiat."""
        model_path = self.cfg.get("brain_model_path", "")
        try:
            from llama_cpp import Llama
        except ImportError:
            return None

        try:
            logger.info("HustleEngine — GGUF chargé pour insight...")
            llm = Llama(
                model_path=model_path,
                n_ctx=512,
                n_threads=2,
                verbose=False,
            )
            resp = llm.create_chat_completion(
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=300,
            )
            raw = resp["choices"][0]["message"]["content"].strip()
            del llm  # Libère RAM immédiatement
            logger.info("HustleEngine — GGUF déchargé")

            parsed = self._parse_insight_json(raw, item)
            if parsed:
                log_event("HUSTLE_INSIGHT_SOURCE", {
                    "source_brain": "gguf_local",
                    "title": item["title"][:60],
                })
            return parsed

        except Exception as exc:
            logger.warning(f"HustleEngine — GGUF insight échoué : {exc}")
            return None

    # ── Génération : Claude Haiku (fallback) ───────────

    async def _generate_claude(self, item: dict, prompt: str) -> dict | None:
        """Appel Claude Haiku — uniquement si aucun cerveau local disponible."""
        api_key = self.cfg.get("claude_api_key", "")
        if not api_key:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 512,
                        "system": INSIGHT_SYSTEM_PROMPT,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )

            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"HustleEngine — Claude API {resp.status_code} : {data}")
                return None

            # Comptabiliser le coût
            usage = data.get("usage", {})
            cost = (
                usage.get("input_tokens", 0) * 0.0000008
                + usage.get("output_tokens", 0) * 0.000004
            )
            spent = float(get_state("claude_spent_usd", "0.0")) + cost
            set_state("claude_spent_usd", str(spent))
            log_event("CLAUDE_API_CALL", {
                "cost_usd": round(cost, 6),
                "task_type": "hustle_insight",
                "title": item["title"][:60],
            })

            raw = data["content"][0]["text"].strip()
            parsed = self._parse_insight_json(raw, item)
            if parsed:
                log_event("HUSTLE_INSIGHT_SOURCE", {
                    "source_brain": "claude_haiku",
                    "title": item["title"][:60],
                })
            return parsed

        except json.JSONDecodeError as exc:
            logger.warning(f"HustleEngine — Claude JSON parse échoué : {exc}")
            return None
        except Exception as exc:
            logger.error(f"HustleEngine — Claude generate error : {exc}")
            return None

    # ── Helpers ────────────────────────────────────────

    def _parse_insight_json(self, raw: str, item: dict) -> dict | None:
        """Parse une réponse JSON insight depuis n'importe quel cerveau."""
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(f"HustleEngine — JSON parse échoué : {exc} | raw: {raw[:100]}")
            return None

        summary = parsed.get("summary", "")
        key_insight = parsed.get("key_insight", "")
        if not summary and not key_insight:
            logger.warning("HustleEngine — insight vide, ignoré")
            return None

        return {
            "content_hash": self._content_hash(item),
            "source": item["source"],
            "raw_title": item["title"],
            "summary": summary,
            "key_insight": key_insight,
            "actionable": parsed.get("actionable", ""),
        }

    def _content_hash(self, item: dict) -> str:
        raw = f"{item['source']}:{item['title']}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _already_processed(self, item: dict) -> bool:
        h = self._content_hash(item)
        with get_db() as conn:
            row = conn.execute(
                "SELECT content_hash FROM content_cache WHERE content_hash = ?", (h,)
            ).fetchone()
        return row is not None

    def _store_insight(self, insight: dict) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO content_cache
                   (content_hash, source, raw_title, summary, key_insight, actionable, generated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    insight["content_hash"],
                    insight["source"],
                    insight["raw_title"],
                    insight["summary"],
                    insight["key_insight"],
                    insight["actionable"],
                    now,
                ),
            )
        log_event("CONTENT_GENERATED", {
            "content_hash": insight["content_hash"],
            "source": insight["source"],
            "title": insight["raw_title"][:80],
        })

    # ── Stats ──────────────────────────────────────────

    @staticmethod
    def get_catalog_stats() -> dict:
        """Stats du catalogue pour le runtime_state."""
        with get_db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM content_cache").fetchone()[0]
            last_row = conn.execute(
                "SELECT generated_at FROM content_cache ORDER BY generated_at DESC LIMIT 1"
            ).fetchone()
        last_ts = last_row[0] if last_row else None
        return {"content_count": count, "last_content_ts": last_ts}
