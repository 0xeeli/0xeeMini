# ─────────────────────────────────────
# 0xeeMini — Tests : GitHubAuditor
# pytest tests/test_github_auditor.py -v
# ─────────────────────────────────────

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py injecte le module config mocké + DB temp avant tout import

# Package name starts with digit → importlib obligatoire
_pkg = importlib.import_module("0xeemini.github_auditor")

GitHubAuditor = _pkg.GitHubAuditor
GitHubAuditorError = _pkg.GitHubAuditorError


# ── Fixtures ──────────────────────────────────────────

FAKE_COMMITS_LIST = [
    {
        "sha": "abc123def456ghi",
        "commit": {
            "author": {"name": "Alice", "date": "2026-02-20T10:00:00Z"},
            "message": "update readme",
        },
        "author": {"login": "alice"},
    },
    {
        "sha": "def456ghi789jkl",
        "commit": {
            "author": {"name": "Alice", "date": "2026-02-21T10:05:00Z"},
            "message": "fix typo",
        },
        "author": {"login": "alice"},
    },
    {
        "sha": "ghi789jkl012mno",
        "commit": {
            "author": {"name": "Alice", "date": "2026-02-22T10:10:00Z"},
            "message": "update styles.css",
        },
        "author": {"login": "alice"},
    },
    {
        "sha": "jkl012mno345pqr",
        "commit": {
            "author": {"name": "Alice", "date": "2026-02-22T10:15:00Z"},
            "message": "minor",
        },
        "author": {"login": "alice"},
    },
    {
        "sha": "mno345pqr678stu",
        "commit": {
            "author": {"name": "Alice", "date": "2026-02-22T10:20:00Z"},
            "message": "update",
        },
        "author": {"login": "alice"},
    },
]

FAKE_DIFF_COSMETIC = {
    "stats": {"additions": 2, "deletions": 1, "total": 3},
    "files": [
        {
            "filename": "README.md",
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "patch": "@@ -1,1 +1,2 @@\n-old\n+new\n+line",
        }
    ],
}


# ── test_parse_repo_url ────────────────────────────────

class TestParseRepoUrl:
    def setup_method(self):
        self.auditor = GitHubAuditor()

    def test_full_https_url(self):
        owner, repo = self.auditor._parse_repo_url("https://github.com/bitcoin/bitcoin")
        assert owner == "bitcoin"
        assert repo == "bitcoin"

    def test_full_https_url_with_git(self):
        owner, repo = self.auditor._parse_repo_url("https://github.com/bitcoin/bitcoin.git")
        assert owner == "bitcoin"
        assert repo == "bitcoin"

    def test_short_owner_repo(self):
        owner, repo = self.auditor._parse_repo_url("bitcoin/bitcoin")
        assert owner == "bitcoin"
        assert repo == "bitcoin"

    def test_url_with_trailing_slash(self):
        owner, repo = self.auditor._parse_repo_url("https://github.com/solana-labs/solana/")
        assert owner == "solana-labs"
        assert repo == "solana"

    def test_invalid_url_no_repo(self):
        with pytest.raises(GitHubAuditorError):
            self.auditor._parse_repo_url("https://github.com/onlyowner")

    def test_invalid_url_empty(self):
        with pytest.raises(GitHubAuditorError):
            self.auditor._parse_repo_url("")

    def test_invalid_url_just_domain(self):
        with pytest.raises(GitHubAuditorError):
            self.auditor._parse_repo_url("https://github.com/")


# ── test_cosmetic_ratio ────────────────────────────────

COSMETIC_EXTS = {".md", ".css", ".txt", ".json", ".yaml", ".yml", ".rst", ".toml", ".lock"}


def _make_commit(filenames: list[str]) -> dict:
    return {
        "sha": "abc1234",
        "author": "alice",
        "date": "2026-02-22T10:00:00Z",
        "message": "update",
        "stats": {"additions": 5, "deletions": 2, "total": 7},
        "files": [
            {"filename": f, "status": "modified", "additions": 5, "deletions": 2, "patch": ""}
            for f in filenames
        ],
    }


def _cosmetic_ratio(commits):
    if not commits:
        return 0.0
    cosmetic_only = sum(
        1 for c in commits
        if c["files"] and all(
            Path(f["filename"]).suffix.lower() in COSMETIC_EXTS
            for f in c["files"]
        )
    )
    return cosmetic_only / len(commits)


def test_cosmetic_ratio_all_cosmetic():
    commits = [
        _make_commit(["README.md"]),
        _make_commit(["styles.css"]),
        _make_commit(["config.json"]),
    ]
    assert _cosmetic_ratio(commits) == pytest.approx(1.0, abs=0.01)


def test_cosmetic_ratio_mixed():
    commits = [
        _make_commit(["main.py"]),
        _make_commit(["README.md"]),
        _make_commit(["core.sol"]),
        _make_commit(["styles.css"]),
    ]
    assert _cosmetic_ratio(commits) == pytest.approx(0.5, abs=0.01)


def test_cosmetic_ratio_no_cosmetic():
    commits = [
        _make_commit(["token.sol"]),
        _make_commit(["main.py"]),
    ]
    assert _cosmetic_ratio(commits) == pytest.approx(0.0, abs=0.01)


# ── test_build_payload_mock ────────────────────────────

@pytest.mark.asyncio
async def test_build_payload_structure():
    """Mock httpx — valider la structure du payload sans réseau."""
    auditor = GitHubAuditor()

    list_response = MagicMock()
    list_response.status_code = 200
    list_response.json.return_value = FAKE_COMMITS_LIST

    diff_response = MagicMock()
    diff_response.status_code = 200
    diff_response.json.return_value = FAKE_DIFF_COSMETIC

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        # First call = list, rest = diffs
        mock_client.get = AsyncMock(
            side_effect=[list_response] + [diff_response] * len(FAKE_COMMITS_LIST)
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            payload = await auditor.build_analysis_payload("bitcoin/bitcoin")

    assert payload["repo"] == "bitcoin/bitcoin"
    assert "fetched_at" in payload
    m = payload["metrics"]
    assert m["total_commits"] == len(FAKE_COMMITS_LIST)
    assert m["authors_count"] == 1           # Alice uniquement
    assert 0.0 <= m["cosmetic_ratio"] <= 1.0
    assert m["cosmetic_ratio"] == pytest.approx(1.0, abs=0.01)  # All README.md
    assert len(payload["commits_sample"]) == len(FAKE_COMMITS_LIST)


@pytest.mark.asyncio
async def test_private_repo_raises():
    auditor = GitHubAuditor()

    resp_404 = MagicMock()
    resp_404.status_code = 404
    resp_404.json.return_value = {"message": "Not Found"}

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=resp_404)

        with pytest.raises(GitHubAuditorError, match="privé"):
            await auditor.fetch_commits("private/repo")


# ── test_api_endpoint_mock ─────────────────────────────

def test_audit_mock_buyer_returns_200():
    """POST /audit avec MOCK_ buyer → 200 + structure complète."""
    _api_mod = importlib.import_module("0xeemini.hustle_api")

    from fastapi.testclient import TestClient

    mock_result = {
        "repo": "bitcoin/bitcoin",
        "content_hash": "abc123def456",
        "bullshit_score": 25,
        "verdict": "Projet solide, code substantiel",
        "technical_reality": "Commits sur fichiers core, plusieurs auteurs.",
        "red_flags": [],
        "green_flags": ["Diversité d'auteurs"],
        "recommendation": "INVEST",
        "confidence": 0.85,
        "fetched_at": "2026-02-23T00:00:00+00:00",
        "expires_at": "2026-02-24T00:00:00+00:00",
        "metrics": {"total_commits": 20, "cosmetic_ratio": 0.1},
    }

    mock_auditor = AsyncMock()
    mock_auditor.run = AsyncMock(return_value=mock_result)

    with patch.object(_api_mod, "GitHubAuditor", return_value=mock_auditor):
        client = TestClient(_api_mod.app, raise_server_exceptions=True)
        resp = client.post(
            "/audit",
            json={
                "repo_url": "bitcoin/bitcoin",
                "buyer_wallet": "MOCK_test_buyer",
                "tx_signature": "",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "bullshit_score" in data
    assert "recommendation" in data
    assert data["mock"] is True


def test_audit_without_payment_returns_402():
    """POST /audit sans tx_signature → 402 avec price_usdc."""
    _api_mod = importlib.import_module("0xeemini.hustle_api")

    from fastapi.testclient import TestClient

    client = TestClient(_api_mod.app, raise_server_exceptions=False)
    resp = client.post(
        "/audit",
        json={
            "repo_url": "bitcoin/bitcoin",
            "buyer_wallet": "realbuyer123",
            "tx_signature": "",
        },
    )
    assert resp.status_code == 402
    data = resp.json()
    assert data.get("error") == "payment_required"
    assert "price_usdc" in data
    assert data["price_usdc"] == pytest.approx(0.50, abs=0.01)
