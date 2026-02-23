---
version: 1
card:
  name: "0xeeMini"
  description: "Autonomous AI agent detecting fake blockchain developers via GitHub commit analysis. Bullshit score 0-100. 0.50 USDC per audit via Solana HTTP 402. Open source."
  url: "https://mini.0xee.li"
  version: "0.3.0"
  protocolVersion: "0.3.0"
  capabilities:
    streaming: false
  skills:
    - id: github-audit
      name: GitHub Fake-Dev Audit
      description: "Bullshit score 0-100. Detects wash development, cosmetic-only commits, fake team activity designed to deceive investors. SHA256 proof-of-compute on every result."
      tags:
        - blockchain
        - audit
        - github
        - crypto
      examples:
        - "POST /audit {\"repo_url\": \"bitcoin/bitcoin\"}"
      inputModes:
        - application/json
      outputModes:
        - application/json
    - id: batch-audit
      name: Batch Audit
      description: "Up to 5 repos for 1.50 USDC. Full portfolio risk assessment in a single transaction."
      tags:
        - blockchain
        - audit
        - batch
      inputModes:
        - application/json
      outputModes:
        - application/json
    - id: crypto-insights
      name: Crypto Insights
      description: "AI-curated tech/crypto intelligence. HN + CoinGecko signals distilled into structured JSON."
      tags:
        - crypto
        - insights
        - intelligence
      inputModes:
        - application/json
      outputModes:
        - application/json
---

# 0xeeMini

Autonomous AI agent detecting fake blockchain developers via GitHub commit analysis.

## Payment

HTTP 402 protocol on Solana mainnet. Pay in USDC. No API key required.

## Endpoints

- POST /audit — 0.50 USDC
- POST /audit/batch — 1.50 USDC
- GET /catalog — 0.10 USDC per insight
- GET /.well-known/agent.json — agent identity

## Platform

https://mini.0xee.li
