# ─────────────────────────────────────────────────────
# 0xeeMini v0.3.0 — Telegram Bot
# https://mini.0xee.li
#
# Commands:
#   /start   — Welcome + help
#   /demo    — Free demo audit (MOCK)
#   /audit   — Paid audit 0.50 USDC (HTTP 402 Solana)
#   /batch   — Batch audit up to 5 repos (1.50 USDC)
#   /confirm — Confirm payment with tx_signature
#   /catalog — Available insights
#   /help    — Help
#
# Architecture: separate thread + own event loop (safe with uvicorn)
# ─────────────────────────────────────────────────────

import asyncio
import time

import httpx
from loguru import logger

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    _TELEGRAM_OK = True
except ImportError:
    _TELEGRAM_OK = False

# ── Constants ─────────────────────────────────────────

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_API = "http://localhost:8000"
_PLATFORM = "https://mini.0xee.li"
_PENDING_TTL = 900  # 15 minutes

# Pending payment requests: chat_id → {repo_urls, ts, batch}
_PENDING: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────

def _score_bar(score: int) -> str:
    filled = round(score / 10)
    bar = "█" * filled + "░" * (10 - filled)
    emoji = "🟢" if score <= 30 else ("🟡" if score <= 60 else "🔴")
    return f"{emoji} {bar} {score}/100"


def _rec_badge(rec: str) -> str:
    return {
        "INVEST": "✅ INVEST",
        "CAUTION": "⚠️ CAUTION",
        "AVOID":   "🚫 AVOID",
    }.get(rec, f"❓ {rec}")


def _solana_pay_url(wallet: str, amount: float, label: str) -> str:
    return (
        f"solana:{wallet}"
        f"?amount={amount}"
        f"&spl-token={USDC_MINT}"
        f"&label={label.replace(' ', '+')}"
    )


def _fmt_result(r: dict, demo: bool = False) -> str:
    score = r.get("bullshit_score", 50)
    rec   = r.get("recommendation", "CAUTION")
    repo  = r.get("repo", "?")

    lines = [
        f"<b>📊 {repo}</b>",
        "",
        f"<b>Score:</b> {_score_bar(score)}",
        f"<b>Verdict:</b> {_rec_badge(rec)}",
        "",
        f"<i>{r.get('verdict', '')}</i>",
        "",
        r.get("technical_reality", ""),
    ]

    red   = r.get("red_flags", [])
    green = r.get("green_flags", [])
    if red:
        lines += ["", "🚩 <b>Red flags:</b>"] + [f"  • {f}" for f in red[:4]]
    if green:
        lines += ["", "✅ <b>Green flags:</b>"] + [f"  • {f}" for f in green[:3]]

    m = r.get("metrics", {})
    if m:
        lines += [
            "",
            f"📈 {m.get('total_commits','?')} commits · "
            f"{m.get('authors_count','?')} author(s) · "
            f"{int(m.get('cosmetic_ratio', 0) * 100)}% cosmetic",
        ]

    conf   = r.get("confidence", 0)
    source = r.get("_source", "")
    proof  = r.get("proof_hash_short", "")
    meta_parts = [f"confidence {int(conf * 100)}%", f"via {source}"]
    if proof:
        meta_parts.append(f'<a href="{_PLATFORM}/proof/{proof}">🔐 proof</a>')
    lines.append(f"\n<i>{' · '.join(meta_parts)}</i>")

    if demo:
        lines += ["", f"⚠️ <i>Free demo · full audit: /audit {repo}</i>"]
    else:
        expires = r.get("expires_at", "")
        if expires:
            lines.append(f"<i>Valid until {expires[:10]}</i>")

    return "\n".join(lines)


async def _api(method: str, path: str, timeout: int = 60, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await getattr(client, method)(f"{_API}{path}", **kwargs)


# ── Commands ───────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>0xeeMini</b> — Autonomous GitHub Audit Agent\n\n"
        "Detects crypto projects with fake development activity.\n"
        "Score 0–100 · Verdict INVEST / CAUTION / AVOID\n\n"
        "<b>Commands:</b>\n"
        "/demo <code>owner/repo</code> — Free demo audit\n"
        "/audit <code>owner/repo</code> — Full audit <b>0.50 USDC</b>\n"
        "/batch <code>repo1 repo2 …</code> — Batch 2–5 repos <b>1.50 USDC</b>\n"
        "/confirm <code>tx_sig</code> — Confirm Solana payment\n"
        "/catalog — Available insights (0.10 USDC each)\n"
        "/help — Help\n\n"
        f'🌐 <a href="{_PLATFORM}">{_PLATFORM}</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_demo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free demo audit — MOCK mode, no payment required."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /demo <code>owner/repo</code>\nEx: /demo bitcoin/bitcoin",
            parse_mode="HTML",
        )
        return

    repo_url = context.args[0]
    chat_id  = update.effective_chat.id
    msg = await update.message.reply_text(
        f"🔍 Auditing <b>{repo_url}</b>…\n"
        f"<i>Fetching commits from GitHub — may take 1–3 min on large repos.</i>",
        parse_mode="HTML",
    )

    try:
        resp = await _api("post", "/audit", timeout=360, json={
            "repo_url":     repo_url,
            "buyer_wallet": f"MOCK_telegram_{chat_id}",
            "tx_signature": "",
        })
        if resp.status_code == 200:
            await msg.edit_text(_fmt_result(resp.json(), demo=True), parse_mode="HTML")
        else:
            err = resp.json().get("detail", {})
            await msg.edit_text(
                f"❌ {err.get('error', f'Error {resp.status_code}')}",
                parse_mode="HTML",
            )
    except Exception as exc:
        logger.error(f"Telegram /demo: {exc}")
        await msg.edit_text(
            f"❌ Audit timed out for <b>{repo_url}</b>.\n\n"
            f"Try a smaller repo, or retry — GitHub rate limits cause delays.\n"
            f"Ex: <code>/demo nicehash/NiceHashQuickMiner</code>",
            parse_mode="HTML",
        )


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Paid audit — generates HTTP 402 payment instructions."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /audit <code>owner/repo</code>\nEx: /audit bitcoin/bitcoin",
            parse_mode="HTML",
        )
        return

    repo_url = context.args[0]
    chat_id  = update.effective_chat.id

    # Store pending payment request
    _PENDING[chat_id] = {"repo_urls": [repo_url], "ts": time.time(), "batch": False}

    try:
        resp = await _api("post", "/audit", timeout=360, json={
            "repo_url":     repo_url,
            "buyer_wallet": f"tg_{chat_id}",
            "tx_signature": "",
        })
    except Exception as exc:
        await update.message.reply_text(f"❌ API unavailable: {exc}")
        return

    if resp.status_code == 200:
        # Audit already cached — serve it
        await update.message.reply_text(
            f"ℹ️ Recent audit available:\n\n{_fmt_result(resp.json())}",
            parse_mode="HTML",
        )
        return

    if resp.status_code == 402:
        data   = resp.json()
        wallet = data.get("wallet", "")
        price  = data.get("price_usdc", 0.50)
        pay_url = _solana_pay_url(wallet, price, f"0xeeMini Audit {repo_url[:20]}")

        keyboard = [[InlineKeyboardButton("🔗 Pay with Phantom / Backpack", url=pay_url)]]
        await update.message.reply_text(
            f"💳 <b>Audit {repo_url}</b>\n\n"
            f"Price: <b>{price} USDC</b> on Solana\n"
            f"Wallet: <code>{wallet}</code>\n\n"
            f"Then send:\n"
            f"/confirm <code>&lt;your_tx_signature&gt;</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(f"❌ Error {resp.status_code}")


async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Batch audit — 2 to 5 repos for 1.50 USDC."""
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /batch <code>repo1 repo2 [repo3 repo4 repo5]</code>\n\n"
            "Min 2, max 5 repos. Fixed price: <b>1.50 USDC</b>\n"
            "(vs 0.50 USDC each = up to 60% savings)\n\n"
            "Ex: /batch bitcoin/bitcoin ethereum/ethereum solana-labs/solana",
            parse_mode="HTML",
        )
        return

    repos   = args[:5]
    chat_id = update.effective_chat.id
    _PENDING[chat_id] = {"repo_urls": repos, "ts": time.time(), "batch": True}

    try:
        resp = await _api("post", "/audit/batch", json={
            "repos":        repos,
            "buyer_wallet": f"tg_{chat_id}",
            "tx_signature": "",
        })
    except Exception as exc:
        await update.message.reply_text(f"❌ API unavailable: {exc}")
        return

    if resp.status_code == 402:
        data    = resp.json()
        wallet  = data.get("wallet", "")
        price   = data.get("price_usdc", 1.50)
        savings = round(len(repos) * 0.50 - price, 2)
        pay_url = _solana_pay_url(wallet, price, f"0xeeMini Batch {len(repos)} audits")

        repos_list = "\n".join(f"  • {r}" for r in repos)
        keyboard = [[InlineKeyboardButton("🔗 Pay with Phantom / Backpack", url=pay_url)]]

        await update.message.reply_text(
            f"💳 <b>Batch Audit — {len(repos)} repos</b>\n\n"
            f"{repos_list}\n\n"
            f"Price: <b>{price} USDC</b>"
            f"{f' (savings: {savings} USDC)' if savings > 0 else ''}\n"
            f"Wallet: <code>{wallet}</code>\n\n"
            f"After payment:\n"
            f"/confirm <code>&lt;your_tx_signature&gt;</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(f"❌ Batch error {resp.status_code}")


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm payment and run the audit."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /confirm <code>your_tx_signature</code>\n"
            "Start /audit or /batch first.",
            parse_mode="HTML",
        )
        return

    tx_sig  = context.args[0]
    chat_id = update.effective_chat.id
    pending = _PENDING.get(chat_id)

    if not pending:
        await update.message.reply_text(
            "❌ No pending request.\n"
            "Start with /audit <code>owner/repo</code> first.",
            parse_mode="HTML",
        )
        return

    if time.time() - pending["ts"] > _PENDING_TTL:
        del _PENDING[chat_id]
        await update.message.reply_text(
            "⏱ Request expired (15 min). Try /audit or /batch again.",
            parse_mode="HTML",
        )
        return

    repos    = pending["repo_urls"]
    is_batch = pending.get("batch", False)
    msg      = await update.message.reply_text("⏳ Verifying Solana payment…")

    try:
        if is_batch:
            resp = await _api("post", "/audit/batch", timeout=600, json={
                "repos":        repos,
                "buyer_wallet": f"tg_{chat_id}",
                "tx_signature": tx_sig,
            })
        else:
            resp = await _api("post", "/audit", timeout=360, json={
                "repo_url":     repos[0],
                "buyer_wallet": f"tg_{chat_id}",
                "tx_signature": tx_sig,
            })
    except Exception as exc:
        await msg.edit_text(f"❌ API error: {exc}")
        return

    if resp.status_code == 200:
        del _PENDING[chat_id]
        data = resp.json()

        if is_batch:
            results = data.get("results", [])
            header  = f"✅ <b>Batch Audit — {len(results)} repos</b>\n"
            sep     = "\n" + "─" * 20 + "\n"
            body    = sep.join(_fmt_result(r) for r in results)
            await msg.edit_text(header + "\n" + body, parse_mode="HTML")
        else:
            await msg.edit_text(
                f"✅ Payment verified!\n\n{_fmt_result(data)}",
                parse_mode="HTML",
            )

    elif resp.status_code == 402:
        await msg.edit_text(
            "❌ Payment not verified.\n\n"
            "Check:\n"
            "• TX confirmed on Solana?\n"
            "• Exact amount (0.50 or 1.50 USDC)?\n"
            "• TX less than 10 minutes old?\n\n"
            "Retry: /confirm <code>tx_sig</code>",
            parse_mode="HTML",
        )
    else:
        err = resp.json()
        detail = err.get("detail", {})
        if isinstance(detail, dict):
            detail = detail.get("error", str(resp.status_code))
        await msg.edit_text(f"❌ {detail}")


async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show available insights."""
    try:
        resp = await _api("get", "/catalog")
        data = resp.json()
    except Exception as exc:
        await update.message.reply_text(f"❌ API unavailable: {exc}")
        return

    items = data.get("items", [])
    if not items:
        await update.message.reply_text("📭 Empty catalog. Check back soon.")
        return

    lines = [f"📚 <b>0xeeMini Catalog</b> — {len(items)} items\n"]
    for item in items[:8]:
        emoji = "🔍" if item.get("type") == "github_audit" else "💡"
        title = item.get("title", "?")[:55]
        price = item.get("price_usdc", 0.10)
        cid   = item.get("content_id", "")[:8]
        lines.append(
            f"{emoji} <b>{title}</b>\n"
            f"   <i>{item.get('summary_preview', '')[:80]}</i>\n"
            f"   💰 {price} USDC · <code>{cid}</code>\n"
        )

    lines.append(f'🌐 <a href="{_PLATFORM}">{_PLATFORM}</a>')
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ── Lifecycle ─────────────────────────────────────────

def _build_app(token: str) -> "Application":
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("demo",    cmd_demo))
    app.add_handler(CommandHandler("audit",   cmd_audit))
    app.add_handler(CommandHandler("batch",   cmd_batch))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("catalog", cmd_catalog))
    return app


async def run_bot(cfg: dict) -> None:
    """Async entry point — runs in its own thread/event-loop."""
    token = cfg.get("telegram_bot_token", "")
    if not token:
        logger.debug("Telegram — token not set, bot disabled")
        return
    if not _TELEGRAM_OK:
        logger.warning("Telegram — python-telegram-bot not installed, bot disabled")
        return

    logger.info("🤖 Telegram bot — starting long-polling")
    app = _build_app(token)

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.success("🤖 Telegram bot online")
        try:
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        finally:
            await app.updater.stop()
            await app.stop()
