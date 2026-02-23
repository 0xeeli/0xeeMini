# ─────────────────────────────────────
# 0xeeMini — Tests : BrainLink Samouraï
# pytest tests/test_brain_link_samurai.py -v
# ─────────────────────────────────────

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py injecte le module config mocké + DB temp avant tout import

_brain_mod = importlib.import_module("0xeemini.brain_link")
BrainLink = _brain_mod.BrainLink

# ── Config minimale pour les tests ────────────────────

MOCK_CFG = {
    "claude_api_key": "",           # Clé absente → force Samouraï
    "claude_budget": 5.0,
    "claude_throttle_secs": 600,
    "brain_model_path": "",
    "brain_audit_model_path": "",   # Absent par défaut → modèle introuvable
}

MOCK_CFG_WITH_BUDGET = {
    **MOCK_CFG,
    "claude_api_key": "sk-ant-test",
    "claude_budget": 5.0,
}

FAKE_PAYLOAD = {
    "repo": "test/repo",
    "metrics": {
        "total_commits": 5,
        "authors_count": 1,
        "cosmetic_ratio": 0.8,
        "empty_commits": 2,
        "weekend_commits": 1,
    },
    "commits_sample": [
        {
            "sha": "abc123",
            "author": "alice",
            "date": "2026-02-22T10:00:00Z",
            "message": "update readme",
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
        },
        {
            "sha": "def456",
            "author": "alice",
            "date": "2026-02-22T10:05:00Z",
            "message": "fix styles",
            "stats": {"additions": 5, "deletions": 3, "total": 8},
            "files": [
                {
                    "filename": "styles.css",
                    "status": "modified",
                    "additions": 5,
                    "deletions": 3,
                    "patch": "@@ -1,5 +1,7 @@\n.btn { color: red; }",
                }
            ],
        },
    ],
}

SAMURAI_VALID_RESPONSE = json.dumps({
    "bullshit_score": 72,
    "verdict": "Activité cosmétique dominante",
    "technical_reality": "Commits sur README et CSS uniquement. Zéro travail sur la logique core.",
    "red_flags": ["cosmetic_ratio élevé", "auteur unique"],
    "green_flags": [],
    "recommendation": "AVOID",
    "confidence": 0.78,
})


# ── test_preprocess_audit_payload ─────────────────────

class TestPreprocessAuditPayload:
    def setup_method(self):
        self.brain = BrainLink(MOCK_CFG)

    def test_filters_css_files(self):
        """Les fichiers CSS doivent être filtrés des entrées quand des backend files existent."""
        commits = [
            {
                "sha": "abc",
                "author": "alice",
                "date": "2026-02-22T10:00:00Z",
                "message": "fix things",
                "stats": {"additions": 5, "deletions": 2, "total": 7},
                "files": [
                    {"filename": "main.py", "status": "modified",
                     "additions": 5, "deletions": 2, "patch": "def foo(): pass"},
                    {"filename": "styles.css", "status": "modified",
                     "additions": 10, "deletions": 0, "patch": ".btn { color: red }"},
                ],
            }
        ]
        result = self.brain._preprocess_audit_payload(commits)
        data = json.loads(result)
        # main.py doit être présent, styles.css filtré
        filenames = [f["f"] for f in data[0]["files"]]
        assert "main.py" in filenames
        assert "styles.css" not in filenames

    def test_keeps_cosmetic_only_commits_with_2_files_max(self):
        """Un commit 100% cosmétique reste (info utile pour le score), limité à 2 fichiers."""
        commits = [
            {
                "sha": "abc",
                "author": "alice",
                "date": "2026-02-22T10:00:00Z",
                "message": "style update",
                "stats": {"additions": 5, "deletions": 0, "total": 5},
                "files": [
                    {"filename": "a.css", "status": "modified", "additions": 2, "deletions": 0, "patch": ""},
                    {"filename": "b.css", "status": "modified", "additions": 3, "deletions": 0, "patch": ""},
                    {"filename": "c.svg", "status": "modified", "additions": 0, "deletions": 0, "patch": ""},
                ],
            }
        ]
        result = self.brain._preprocess_audit_payload(commits)
        data = json.loads(result)
        # Commit gardé (cosmétique only → fallback 2 fichiers max)
        assert len(data) == 1
        assert len(data[0]["files"]) <= 2

    def test_truncates_to_3200_chars(self):
        """La sortie ne doit jamais dépasser 3200 chars."""
        # Génère beaucoup de commits
        commits = [
            {
                "sha": f"sha{i:06d}",
                "author": "alice",
                "date": "2026-02-22T10:00:00Z",
                "message": "x" * 200,
                "stats": {"additions": 100, "deletions": 50, "total": 150},
                "files": [
                    {
                        "filename": f"file{j}.py",
                        "status": "modified",
                        "additions": 100,
                        "deletions": 50,
                        "patch": "a" * 500,
                    }
                    for j in range(5)
                ],
            }
            for i in range(10)
        ]
        result = self.brain._preprocess_audit_payload(commits)
        assert len(result) <= 3200

    def test_cleans_whitespace_in_patch(self):
        """Les espaces excessifs dans les patches doivent être normalisés."""
        commits = [
            {
                "sha": "abc",
                "author": "alice",
                "date": "2026-02-22T10:00:00Z",
                "message": "fix   extra   spaces",
                "stats": {"additions": 1, "deletions": 0, "total": 1},
                "files": [
                    {
                        "filename": "main.py",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "patch": "   +foo   =   bar   \n   +baz   \n",
                    }
                ],
            }
        ]
        result = self.brain._preprocess_audit_payload(commits)
        data = json.loads(result)
        patch = data[0]["files"][0]["p"]
        # Pas de double espace dans le patch
        assert "  " not in patch

    def test_filters_image_and_font_files(self):
        """PNG, SVG, WOFF, TTF doivent être filtrés."""
        cosmetic_exts = ["logo.png", "icon.svg", "font.woff2", "typeface.ttf", "sprite.ico"]
        commits = [
            {
                "sha": "abc",
                "author": "alice",
                "date": "2026-02-22T10:00:00Z",
                "message": "assets update",
                "stats": {"additions": 10, "deletions": 0, "total": 10},
                "files": [
                    {
                        "filename": fname,
                        "status": "modified",
                        "additions": 2,
                        "deletions": 0,
                        "patch": "",
                    }
                    for fname in cosmetic_exts
                ] + [
                    {
                        "filename": "core.rs",
                        "status": "modified",
                        "additions": 10,
                        "deletions": 5,
                        "patch": "fn main() {}",
                    }
                ],
            }
        ]
        result = self.brain._preprocess_audit_payload(commits)
        data = json.loads(result)
        filenames = [f["f"] for f in data[0]["files"]]
        for cosmetic in cosmetic_exts:
            assert cosmetic not in filenames
        assert "core.rs" in filenames


# ── test_analyze_github_commits flow ──────────────────

@pytest.mark.asyncio
async def test_analyze_uses_claude_when_available():
    """Si Claude disponible → doit utiliser Claude, pas Samouraï."""
    brain = BrainLink(MOCK_CFG_WITH_BUDGET)

    claude_resp = {
        "response": json.dumps({
            "bullshit_score": 30,
            "verdict": "Code solide",
            "technical_reality": "Commits sur logique core.",
            "red_flags": [],
            "green_flags": ["multi-auteurs"],
            "recommendation": "INVEST",
            "confidence": 0.88,
        }),
        "source": "claude_api",
        "cost_usd": 0.0005,
    }

    with patch.object(brain, "_think_claude", return_value=claude_resp) as mock_claude, \
         patch.object(brain, "_analyze_samurai_sync") as mock_samurai:

        result = await brain.analyze_github_commits(FAKE_PAYLOAD)

    mock_claude.assert_called_once()
    mock_samurai.assert_not_called()
    assert result["bullshit_score"] == 30
    assert result["recommendation"] == "INVEST"
    assert result["_source"] == "claude_api"


@pytest.mark.asyncio
async def test_samurai_fallback_when_claude_budget_zero():
    """Si budget Claude = 0 → Samouraï doit être appelé."""
    cfg_no_budget = {**MOCK_CFG_WITH_BUDGET, "claude_budget": 0.0}
    brain = BrainLink(cfg_no_budget)

    samurai_resp = {
        "response": SAMURAI_VALID_RESPONSE,
        "source": "samurai_gguf",
        "cost_usd": 0.0,
    }

    with patch.object(brain, "_think_claude") as mock_claude, \
         patch.object(brain, "_analyze_samurai_sync", return_value=samurai_resp) as mock_samurai:

        result = await brain.analyze_github_commits(FAKE_PAYLOAD)

    mock_claude.assert_not_called()
    mock_samurai.assert_called_once()
    assert result["bullshit_score"] == 72
    assert result["recommendation"] == "AVOID"
    assert result["_source"] == "samurai_gguf"


@pytest.mark.asyncio
async def test_samurai_fallback_when_claude_no_key():
    """Si pas de clé Claude → Samouraï doit être appelé."""
    brain = BrainLink(MOCK_CFG)  # claude_api_key = ""

    samurai_resp = {
        "response": SAMURAI_VALID_RESPONSE,
        "source": "samurai_gguf",
        "cost_usd": 0.0,
    }

    with patch.object(brain, "_analyze_samurai_sync", return_value=samurai_resp) as mock_samurai:
        result = await brain.analyze_github_commits(FAKE_PAYLOAD)

    mock_samurai.assert_called_once()
    assert result["_source"] == "samurai_gguf"
    assert result["bullshit_score"] == 72


@pytest.mark.asyncio
async def test_fallback_heuristic_when_both_unavailable():
    """Si Claude + Samouraï indisponibles → score=50, confidence=0, CAUTION."""
    brain = BrainLink(MOCK_CFG)  # claude_api_key = ""

    samurai_none = {"response": None, "source": "samurai_model_absent", "cost_usd": 0.0}

    with patch.object(brain, "_analyze_samurai_sync", return_value=samurai_none):
        result = await brain.analyze_github_commits(FAKE_PAYLOAD)

    assert result["bullshit_score"] == 50
    assert result["confidence"] == 0.0
    assert result["recommendation"] == "CAUTION"
    assert result["_source"] == "fallback"


@pytest.mark.asyncio
async def test_samurai_fallback_when_claude_api_error():
    """Si Claude répond None (erreur API) → Samouraï doit prendre le relais."""
    brain = BrainLink(MOCK_CFG_WITH_BUDGET)

    claude_error = {"response": None, "source": "claude_api_error", "cost_usd": 0.0}
    samurai_resp = {
        "response": SAMURAI_VALID_RESPONSE,
        "source": "samurai_gguf",
        "cost_usd": 0.0,
    }

    with patch.object(brain, "_think_claude", return_value=claude_error), \
         patch.object(brain, "_analyze_samurai_sync", return_value=samurai_resp) as mock_samurai:

        result = await brain.analyze_github_commits(FAKE_PAYLOAD)

    mock_samurai.assert_called_once()
    assert result["_source"] == "samurai_gguf"
    assert result["recommendation"] == "AVOID"


# ── test_analyze_samurai_sync (unit) ──────────────────

def test_samurai_sync_model_absent():
    """Si le modèle GGUF est absent → retour None sans crash."""
    brain = BrainLink({**MOCK_CFG, "brain_audit_model_path": "/tmp/nonexistent.gguf"})
    result = brain._analyze_samurai_sync("test prompt", "system prompt")
    assert result["response"] is None
    assert result["source"] == "samurai_model_absent"
    assert result["cost_usd"] == 0.0


def test_samurai_sync_no_llama_cpp():
    """Si llama-cpp-python n'est pas installé → retour None sans crash."""
    import builtins
    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "llama_cpp":
            raise ImportError("No module named 'llama_cpp'")
        return original_import(name, *args, **kwargs)

    # Utilise un path qui "existe" fictif pour bypasser le premier check
    brain = BrainLink({**MOCK_CFG, "brain_audit_model_path": "/tmp/fake.gguf"})
    with patch("pathlib.Path.exists", return_value=True), \
         patch("builtins.__import__", side_effect=mock_import):
        result = brain._analyze_samurai_sync("test prompt", "system prompt")

    assert result["response"] is None
    assert result["source"] == "samurai_no_llama_cpp"
