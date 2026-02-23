# ─────────────────────────────────────
# 0xeeMini v0.2.0 — GitHub Auditor
# https://mini.0xee.li
#
# Fake-Dev Detector : analyse les commits GitHub d'un repo crypto
# et détecte si l'équipe simule de l'activité pour tromper les investisseurs.
# Vendu 0.50 USDC via HTTP 402.
# ─────────────────────────────────────

import asyncio
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from loguru import logger

from .core import get_db, log_event


class GitHubAuditorError(Exception):
    """Erreur métier du GitHubAuditor."""


COSMETIC_EXTENSIONS = {".md", ".css", ".txt", ".json", ".yaml", ".yml", ".rst", ".toml", ".lock"}
CORE_EXTENSIONS = {".sol", ".py", ".js", ".ts", ".go", ".rs", ".java", ".cpp", ".c", ".h"}
GENERIC_MESSAGES = {
    "fix", "update", "minor", "wip", "test", "init", "add", "remove",
    "change", "edit", "refactor", "cleanup", "clean", "misc", "bump",
    "version", "release", "hotfix", "patch", "typo", "formatting",
    "style", "lint", "merge", "revert", "temp", "todo", "fixup",
}


class GitHubAuditor:
    """Analyse les commits GitHub pour détecter le fake-dev."""

    _HEADERS = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "0xeeMini-Auditor/0.1",
    }
    _BASE = "https://api.github.com"
    _DIFF_DELAY = 1.2   # secondes entre appels diff (rate limit 60 req/h)
    _PATCH_MAX = 500    # chars max par patch (protéger RAM VPS 2GB)
    AUDIT_PRICE_USDC = 0.50
    AUDIT_TTL_HOURS = 24

    def __init__(self, brain=None) -> None:
        self._brain = brain  # BrainLink pour analyse LLM

    # ── URL Parsing ────────────────────────────────────

    def _parse_repo_url(self, repo_url: str) -> tuple[str, str]:
        """
        Extrait owner/repo depuis :
          https://github.com/owner/repo
          https://github.com/owner/repo.git
          owner/repo
        """
        url = repo_url.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]

        if "github.com" in url:
            path = urlparse(url).path.strip("/")
        else:
            path = url.strip("/")

        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            raise GitHubAuditorError(
                f"URL invalide : '{repo_url}'. Formats acceptés : "
                f"https://github.com/owner/repo | owner/repo"
            )
        owner, repo = parts[0], parts[1]
        if not re.match(r"^[\w.\-]+$", owner) or not re.match(r"^[\w.\-]+$", repo):
            raise GitHubAuditorError(f"owner/repo invalides : '{owner}/{repo}'")
        return owner, repo

    # ── Fetch ──────────────────────────────────────────

    async def fetch_commits(self, repo_url: str) -> list[dict]:
        """
        Récupère les 20 derniers commits avec leurs diffs.
        Respecte le rate limit GitHub : 1.2s entre chaque appel diff.
        Retry unique sur 429/403.
        """
        owner, repo = self._parse_repo_url(repo_url)
        logger.info(f"GitHubAuditor — fetch : {owner}/{repo}")

        async with httpx.AsyncClient(headers=self._HEADERS, timeout=20) as client:
            # ── Fetch liste des commits ────────────────
            resp = await client.get(
                f"{self._BASE}/repos/{owner}/{repo}/commits",
                params={"per_page": 20},
            )

            if resp.status_code == 404:
                raise GitHubAuditorError(f"Repo introuvable ou privé : {owner}/{repo}")

            if resp.status_code in (403, 429):
                logger.warning(
                    f"GitHubAuditor — rate limit HTTP {resp.status_code} "
                    f"sur liste commits, attente 60s..."
                )
                await asyncio.sleep(60)
                resp = await client.get(
                    f"{self._BASE}/repos/{owner}/{repo}/commits",
                    params={"per_page": 20},
                )
                if resp.status_code != 200:
                    raise GitHubAuditorError(
                        f"Rate limit persistant après retry : HTTP {resp.status_code}"
                    )

            if resp.status_code != 200:
                raise GitHubAuditorError(f"GitHub API erreur : HTTP {resp.status_code}")

            commits_raw = resp.json()
            if not isinstance(commits_raw, list):
                raise GitHubAuditorError("Réponse GitHub inattendue (pas une liste de commits)")

            logger.info(f"GitHubAuditor — {len(commits_raw)} commits en liste")

            # ── Fetch diff pour chaque commit ──────────
            commits = []
            for raw in commits_raw:
                sha = raw.get("sha", "")
                if not sha:
                    continue

                commit_info = raw.get("commit", {})
                author_info = commit_info.get("author", {})
                author = (
                    (raw.get("author") or {}).get("login")
                    or author_info.get("name", "unknown")
                )

                # Rate limit delay
                await asyncio.sleep(self._DIFF_DELAY)

                diff_resp = await client.get(
                    f"{self._BASE}/repos/{owner}/{repo}/commits/{sha}"
                )

                if diff_resp.status_code in (403, 429):
                    logger.warning(
                        f"GitHubAuditor — rate limit sur diff {sha[:7]}, attente 60s..."
                    )
                    await asyncio.sleep(60)
                    diff_resp = await client.get(
                        f"{self._BASE}/repos/{owner}/{repo}/commits/{sha}"
                    )

                if diff_resp.status_code != 200:
                    logger.warning(
                        f"GitHubAuditor — diff {sha[:7]} inaccessible "
                        f"(HTTP {diff_resp.status_code}), skip"
                    )
                    continue

                diff_data = diff_resp.json()
                stats = diff_data.get("stats", {})
                files_raw = diff_data.get("files", [])

                files = [
                    {
                        "filename": f.get("filename", ""),
                        "status": f.get("status", "modified"),
                        "additions": f.get("additions", 0),
                        "deletions": f.get("deletions", 0),
                        "patch": (f.get("patch") or "")[: self._PATCH_MAX],
                    }
                    for f in files_raw
                ]

                commits.append({
                    "sha": sha[:7],
                    "author": author,
                    "date": author_info.get("date", ""),
                    "message": commit_info.get("message", "").split("\n")[0][:120],
                    "stats": {
                        "additions": stats.get("additions", 0),
                        "deletions": stats.get("deletions", 0),
                        "total": stats.get("total", 0),
                    },
                    "files": files,
                })

                logger.debug(
                    f"GitHubAuditor — {sha[:7]} "
                    f"+{stats.get('additions',0)} -{stats.get('deletions',0)}"
                )

        logger.info(f"GitHubAuditor — {len(commits)} commits récupérés avec diffs")
        return commits

    # ── Métriques ──────────────────────────────────────

    async def build_analysis_payload(self, repo_url: str) -> dict:
        """Calcule les métriques brutes à partir des commits."""
        owner, repo = self._parse_repo_url(repo_url)
        commits = await self.fetch_commits(repo_url)

        if not commits:
            raise GitHubAuditorError(f"Aucun commit récupérable pour {owner}/{repo}")

        fetched_at = datetime.now(timezone.utc).isoformat()

        # Métriques agrégées
        total_commits = len(commits)
        additions_list = [c["stats"]["additions"] for c in commits]
        deletions_list = [c["stats"]["deletions"] for c in commits]
        avg_additions = sum(additions_list) / total_commits if total_commits else 0.0
        avg_deletions = sum(deletions_list) / total_commits if total_commits else 0.0

        # Extensions des fichiers modifiés
        ext_counter: Counter = Counter()
        file_counter: Counter = Counter()
        for commit in commits:
            for f in commit["files"]:
                fname = f["filename"]
                ext = Path(fname).suffix.lower() or ".noext"
                ext_counter[ext] += 1
                file_counter[fname] += 1

        top_modified_files = [f for f, _ in file_counter.most_common(5)]

        # Auteurs uniques
        authors = {c["author"] for c in commits}
        authors_count = len(authors)

        # Commits week-end (samedi=5, dimanche=6)
        weekend_commits = 0
        for c in commits:
            date_str = c.get("date", "")
            if date_str:
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    if dt.weekday() >= 5:
                        weekend_commits += 1
                except Exception:
                    pass

        # Commits vides (0 lignes changées)
        empty_commits = sum(1 for c in commits if c["stats"]["total"] == 0)

        # Ratio cosmétique : commits ne touchant QUE des fichiers cosmétiques
        cosmetic_only_count = 0
        for commit in commits:
            if not commit["files"]:
                continue
            all_cosmetic = all(
                Path(f["filename"]).suffix.lower() in COSMETIC_EXTENSIONS
                for f in commit["files"]
            )
            if all_cosmetic:
                cosmetic_only_count += 1
        cosmetic_ratio = cosmetic_only_count / total_commits if total_commits else 0.0

        metrics = {
            "total_commits": total_commits,
            "avg_additions": round(avg_additions, 1),
            "avg_deletions": round(avg_deletions, 1),
            "file_types": dict(ext_counter.most_common(10)),
            "top_modified_files": top_modified_files,
            "authors_count": authors_count,
            "weekend_commits": weekend_commits,
            "empty_commits": empty_commits,
            "cosmetic_ratio": round(cosmetic_ratio, 3),
        }

        logger.info(
            f"GitHubAuditor — métriques : "
            f"authors={authors_count} empty={empty_commits} "
            f"cosmetic={cosmetic_ratio:.1%} weekend={weekend_commits}"
        )

        return {
            "repo": f"{owner}/{repo}",
            "fetched_at": fetched_at,
            "metrics": metrics,
            "commits_sample": commits,
        }

    # ── Pipeline principal ─────────────────────────────

    async def run(self, repo_url: str) -> dict:
        """
        Pipeline complet : fetch → métriques → LLM → stockage SQLite.
        Retourne le rapport final.
        """
        owner, repo = self._parse_repo_url(repo_url)
        logger.info(f"GitHubAuditor.run — démarrage : {owner}/{repo}")

        # ── Fetch + métriques ──────────────────────────
        payload = await self.build_analysis_payload(repo_url)

        # ── Analyse LLM ───────────────────────────────
        if self._brain is not None:
            analysis = await self._brain.analyze_github_commits(payload)
        else:
            logger.warning("GitHubAuditor — brain absent, analyse heuristique uniquement")
            analysis = self._heuristic_fallback(payload)

        bullshit_score = analysis.get("bullshit_score", 50)
        verdict = analysis.get("verdict", "Analyse incomplète")
        technical_reality = analysis.get("technical_reality", "")
        red_flags = analysis.get("red_flags", [])
        green_flags = analysis.get("green_flags", [])
        recommendation = analysis.get("recommendation", "CAUTION")

        # ── Stockage SQLite ────────────────────────────
        fetched_at = payload["fetched_at"]
        raw = f"{repo_url}:{fetched_at}"
        content_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self.AUDIT_TTL_HOURS)
        ).isoformat()

        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO content_cache
                   (content_hash, source, raw_title, summary, key_insight,
                    actionable, generated_at, expires_at, price_usdc)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    content_hash,
                    "github_audit",
                    f"Audit {owner}/{repo} — Score: {bullshit_score}/100",
                    verdict,
                    technical_reality,
                    json.dumps({
                        "red_flags": red_flags,
                        "green_flags": green_flags,
                        "recommendation": recommendation,
                        "confidence": analysis.get("confidence", 0.5),
                        "metrics": payload["metrics"],
                    }),
                    now,
                    expires_at,
                    self.AUDIT_PRICE_USDC,
                ),
            )

        log_event("GITHUB_AUDIT_COMPLETED", {
            "repo": f"{owner}/{repo}",
            "bullshit_score": bullshit_score,
            "recommendation": recommendation,
            "content_hash": content_hash,
            "source_brain": analysis.get("_source", "unknown"),
        })

        logger.success(
            f"GitHubAuditor — {owner}/{repo} : "
            f"score={bullshit_score}/100 → {recommendation} "
            f"(via {analysis.get('_source', '?')})"
        )

        return {
            "repo": f"{owner}/{repo}",
            "content_hash": content_hash,
            "bullshit_score": bullshit_score,
            "verdict": verdict,
            "technical_reality": technical_reality,
            "red_flags": red_flags,
            "green_flags": green_flags,
            "recommendation": recommendation,
            "confidence": analysis.get("confidence", 0.5),
            "fetched_at": fetched_at,
            "expires_at": expires_at,
            "metrics": payload["metrics"],
        }

    # ── Fallback heuristique (sans LLM) ───────────────

    def _heuristic_fallback(self, payload: dict) -> dict:
        """Score heuristique basique si aucun LLM disponible."""
        m = payload["metrics"]
        score = 0
        red_flags = []
        green_flags = []

        if m["cosmetic_ratio"] > 0.6:
            score += 35
            red_flags.append(f"Ratio cosmétique élevé : {m['cosmetic_ratio']:.0%} des commits")
        if m["empty_commits"] > 3:
            score += 25
            red_flags.append(f"{m['empty_commits']} commits vides ou quasi-vides")
        if m["authors_count"] <= 1:
            score += 15
            red_flags.append("Auteur unique — aucune diversité d'équipe")
        if m["avg_additions"] < 5:
            score += 15
            red_flags.append(f"Commits minuscules : {m['avg_additions']:.1f} lignes/commit en moyenne")
        if m["weekend_commits"] > m["total_commits"] * 0.5:
            score += 10
            red_flags.append(f"{m['weekend_commits']} commits week-end — pattern forcé")

        if m["authors_count"] >= 3:
            green_flags.append("Diversité d'auteurs")
        if m["avg_additions"] > 30:
            green_flags.append("Commits substantiels en volume")

        score = min(100, score)
        if score >= 70:
            recommendation = "AVOID"
        elif score >= 40:
            recommendation = "CAUTION"
        else:
            recommendation = "INVEST"

        return {
            "bullshit_score": score,
            "verdict": f"Score heuristique : {score}/100",
            "technical_reality": "Analyse heuristique (LLM indisponible).",
            "red_flags": red_flags,
            "green_flags": green_flags,
            "recommendation": recommendation,
            "confidence": 0.4,
            "_source": "heuristic",
        }

    # ── Helpers cache ──────────────────────────────────

    @staticmethod
    def get_cached_audit(repo_key: str) -> dict | None:
        """
        Retourne le dernier audit valide (< 24h) pour ce repo.
        repo_key format : "owner/repo"
        """
        now = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            row = conn.execute(
                """SELECT content_hash, raw_title, summary, key_insight,
                          actionable, generated_at, expires_at, price_usdc
                   FROM content_cache
                   WHERE source = 'github_audit'
                     AND raw_title LIKE ?
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY generated_at DESC LIMIT 1""",
                (f"%{repo_key}%", now),
            ).fetchone()
        if not row:
            return None

        actionable_data = {}
        try:
            actionable_data = json.loads(row["actionable"] or "{}")
        except Exception:
            pass

        return {
            "content_hash": row["content_hash"],
            "repo": repo_key,
            "title": row["raw_title"],
            "verdict": row["summary"],
            "technical_reality": row["key_insight"],
            "red_flags": actionable_data.get("red_flags", []),
            "green_flags": actionable_data.get("green_flags", []),
            "recommendation": actionable_data.get("recommendation", "CAUTION"),
            "confidence": actionable_data.get("confidence", 0.5),
            "metrics": actionable_data.get("metrics", {}),
            "generated_at": row["generated_at"],
            "expires_at": row["expires_at"],
            "price_usdc": row["price_usdc"] or 0.50,
        }
