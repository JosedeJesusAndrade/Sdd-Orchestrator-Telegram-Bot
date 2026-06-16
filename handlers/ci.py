"""CI/CD handlers — PR creation, self-update, and project switching.

These commands enable the full CI/CD pipeline from the street:
  /pr <title> — Create a GitHub PR with CHANGELOG.md as body
  /update    — Self-restart bot with exit code 42 for git pull + reload
  /wdir [project] — List or switch OpenCode workdir (project folder)
"""

import asyncio
import os
import re
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from handlers import authorized
from services.container import AppContainer


def _get_container(context: ContextTypes.DEFAULT_TYPE) -> AppContainer:
    """Extract the typed AppContainer from PTB context."""
    return context.application.bot_data["container"]


async def _run_git_command(workdir: str, *args: str) -> tuple[int, str, str]:
    """Execute a git command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip() if stdout else "",
        stderr.decode("utf-8", errors="replace").strip() if stderr else "",
    )


async def _run_gh_command(workdir: str, *args: str) -> tuple[int, str, str]:
    """Execute a gh (GitHub CLI) command."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workdir,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip() if stdout else "",
        stderr.decode("utf-8", errors="replace").strip() if stderr else "",
    )


def _sanitize_branch_name(title: str) -> str:
    """Convert a PR title into a valid git branch name."""
    name = title.lower().strip()
    name = re.sub(r'[^a-z0-9\s_-]', '', name)
    name = re.sub(r'\s+', '-', name)
    return name[:50] or "update"


@authorized
async def pr_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a GitHub PR: /pr <title>

    Uses per-chat workdir from settings. Reads CHANGELOG.md for PR body.
    Executes: git checkout -b → git add → git commit → git push → gh pr create
    """
    chat_id = update.effective_chat.id
    container = _get_container(context)
    sender = container.message_sender
    store = container.session_store

    # Get the workdir for this user (Week 4 per-chat config)
    workdir = await store.get_chat_setting(chat_id, "workdir", os.getcwd())

    # Parse PR title
    title = " ".join(context.args) if context.args else "Update from Telegram"
    branch_name = f"feature/{_sanitize_branch_name(title)}-{os.urandom(2).hex()}"

    # Build PR body from CHANGELOG.md if it exists
    changelog_path = Path(workdir) / "CHANGELOG.md"
    if changelog_path.exists():
        body = changelog_path.read_text(encoding="utf-8", errors="replace")
        # Clean up the changelog after reading
        changelog_path.unlink()
    else:
        body = "PR generated via Telegram Bot."

    await sender.reply_plain(update, f"📦 Creando PR: *{title[:80]}*...\nRama: `{branch_name}`")

    # Step 1: Create branch
    ret, out, err = await _run_git_command(workdir, "checkout", "-b", branch_name)
    if ret != 0:
        await sender.reply_plain(update, f"❌ Error creando rama:\n`{err}`")
        return

    # Step 2: Stage all changes
    ret, out, err = await _run_git_command(workdir, "add", ".")
    if ret != 0:
        await sender.reply_plain(update, f"❌ Error en git add:\n`{err}`")
        return

    # Step 3: Commit
    ret, out, err = await _run_git_command(workdir, "commit", "-m", title)
    if ret != 0:
        # Check if nothing to commit (no changes)
        if "nothing to commit" in (out + err).lower():
            await sender.reply_plain(update, "⚠️ No hay cambios para commitear.")
            await _run_git_command(workdir, "checkout", "main")
            return
        await sender.reply_plain(update, f"❌ Error en commit:\n`{err}`")
        return

    # Step 4: Push
    ret, out, err = await _run_git_command(workdir, "push", "-u", "origin", branch_name)
    if ret != 0:
        await sender.reply_plain(update, f"❌ Error en push:\n`{err}`")
        return

    # Step 5: Create PR via GitHub CLI
    ret, out, err = await _run_gh_command(
        workdir, "pr", "create",
        "--title", title,
        "--body", body,
        "--base", "main",
        "--head", branch_name,
    )
    if ret != 0:
        await sender.reply_plain(update, f"❌ Error creando PR:\n`{err}`")
        return

    # Extract PR URL from gh output
    pr_url = ""
    for line in out.split("\n"):
        if "github.com" in line and "/pull/" in line:
            pr_url = line.strip()
            break

    await sender.reply_plain(
        update,
        f"✅ PR creada exitosamente.\n\n"
        f"📝 *Título:* {title[:100]}\n"
        f"🔗 {pr_url or out[:200]}"
    )

    # Return to main
    await _run_git_command(workdir, "checkout", "main")


@authorized
async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Self-restart the bot: /update

    Pulls latest from GitHub and restarts via exit code 42.
    The launcher.bat babysitter detects code 42 and does git pull + restart.
    """
    container = _get_container(context)
    sender = container.message_sender

    await sender.reply_plain(
        update,
        "🔄 Recibido. Descargando última versión y reiniciando el bot...\n"
        "El bot volverá en ~5 segundos."
    )

    import logging
    logger = logging.getLogger("opencode_bot")
    logger.info("🔄 Auto-reinicio solicitado vía Telegram (exit code 42)")

    # Flush any pending I/O
    await asyncio.sleep(0.5)

    # Exit with code 42 — the launcher.bat detects this and does git pull + restart
    os._exit(42)


@authorized
async def wdir_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List or switch OpenCode workdir: /wdir [project]

    Without args: lists all projects (folders with .git) in the parent dir.
    With arg: switches the per-chat workdir to that project.
    Accepts full name, partial match, or alias (lowercase, no spaces).
    """
    chat_id = update.effective_chat.id
    container = _get_container(context)
    sender = container.message_sender
    store = container.session_store
    args = context.args

    # Default parent dir — where all projects live
    from config import OPENCODE_WORKDIR
    parent = Path(OPENCODE_WORKDIR)
    if not parent.exists():
        await sender.reply_plain(update, f"❌ Directorio no encontrado: `{parent}`")
        return

    # Discover projects (folders with .git)
    projects: dict[str, Path] = {}
    try:
        for entry in sorted(parent.iterdir()):
            if entry.is_dir() and (entry / ".git").exists():
                name = entry.name
                key = name.lower().replace(" ", "-")
                # Short alias: first word or first segment before dash
                short = key.split("-")[0]
                projects[name] = entry
                projects[key] = entry
                if short not in projects:
                    projects[short] = entry
    except PermissionError:
        await sender.reply_plain(update, "❌ Sin permisos para listar directorios.")
        return

    if not projects:
        await sender.reply_plain(update, "⚠️ No se encontraron proyectos con `.git` en el directorio.")
        return

    # Get current workdir
    current = await store.get_chat_setting(chat_id, "workdir", OPENCODE_WORKDIR)
    current_path = Path(current)

    if not args:
        # List all projects with current marked
        seen = set()
        lines = ["⚙️ *Proyectos disponibles:*\n"]
        for entry in sorted(parent.iterdir()):
            if entry.is_dir() and (entry / ".git").exists():
                name = entry.name
                if name in seen:
                    continue
                seen.add(name)
                marker = "🟢" if str(entry) == str(current_path) else "  "
                alias = name.lower().replace(" ", "-").split("-")[0]
                lines.append(f"{marker} `{alias}` → {name}")
        await sender.reply_plain(update, "\n".join(lines))
        return

    # Switch to project
    query = " ".join(args).lower().replace(" ", "-")
    target = projects.get(query)

    if target is None:
        # Try partial match
        matches = [k for k in projects if k.startswith(query)]
        if len(matches) == 1:
            target = projects[matches[0]]
        elif len(matches) > 1:
            match_list = ", ".join(matches[:5])
            await sender.reply_plain(update, f"⚠️ Múltiples coincidencias: {match_list}. Sé más específico.")
            return
        else:
            await sender.reply_plain(update, f"❌ Proyecto no encontrado: `{query}`")
            return

    workdir = str(target)
    await store.set_chat_setting(chat_id, "workdir", workdir)
    await sender.reply_plain(
        update,
        f"✅ Workdir cambiado a:\n`{workdir}`\n\n"
        f"El próximo prompt usará este proyecto."
    )
