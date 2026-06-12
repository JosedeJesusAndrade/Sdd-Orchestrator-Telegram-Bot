"""Markdown formatting utilities for Telegram-OpenCode bridge."""
import re
import logging

from config import TELEGRAM_MAX_MESSAGE_LENGTH

logger = logging.getLogger("opencode_bot")
from telegram.constants import ParseMode


def _filter_stderr(stderr: str) -> str:
    """Filter stderr to only include meaningful response lines (not metadata)."""
    if not stderr or not stderr.strip():
        return ""
    lines = []
    for line in stderr.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if 'build' in lower and chr(183) in stripped:
            continue
        if stripped.startswith('[INFO]') or stripped.startswith('[DEBUG]'):
            continue
        if stripped.startswith('[WARN'):
            continue
        lines.append(stripped)
    return '\n'.join(lines)


def _remove_tool_traces(text: str) -> str:
    """Remove tool call trace lines (single and multi-line JSON).
    
    Detects lines starting with ⚙ (or its cp1252-mangled form âš™) 
    and skips them plus any multi-line JSON that follows.
    """
    result = []
    in_tool_trace = False
    brace_depth = 0
    
    for line in text.split('\n'):
        stripped = line.strip()
        
        # Detect start of tool trace: ⚙ (U+2699) or âš™ (mangled UTF-8 via cp1252)
        if stripped.startswith('⚙') or stripped.startswith('âš™'):
            in_tool_trace = True
            brace_depth = 0
        
        if in_tool_trace:
            brace_depth += stripped.count('{') - stripped.count('}')
            if brace_depth <= 0 and ('{' in stripped or '}' in stripped):
                in_tool_trace = False
            continue
        
        result.append(line)
    
    return '\n'.join(result)


def clean_opencode_output(text: str) -> str:
    """Remove ANSI escape codes and clean up terminal output for Telegram."""
    # 1. Remove ANSI escape sequences (ESC + CSI codes)
    ansi_pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_pattern.sub('', text)

    # 2. Remove build lines (model info like "> build · deepseek-v4-pro")
    text = re.sub(r'^> build .*$', '', text, flags=re.MULTILINE)

    # 3. Remove tool call traces (single and multi-line JSON with ⚙ marker)
    text = _remove_tool_traces(text)

    # 4. Clean up trailing --- separator (leftover when stderr was only tool traces)
    text = re.sub(r'\n*---\s*$', '', text)

    # 5. Replace Unicode symbols with text equivalents
    text = text.replace('\u2731', '\u2192')

    # 6. Collapse multiple blank lines into one
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 7. Split into lines, strip whitespace
    lines = [line.strip() for line in text.split('\n')]

    # 8. Trim everything before first line starting with ">" (opencode logo/header)
    for i, line in enumerate(lines):
        if line.startswith('>'):
            lines = lines[i:]
            break

    # 9. Remove empty lines and filter out noise
    filtered = []
    for line in lines:
        if not line:
            continue
        lower = line.lower()
        if 'auto-rejecting' in lower or 'permission requested' in lower:
            continue
        if 'user rejected permission' in lower:
            continue
        filtered.append(line)

    return '\n'.join(filtered)


def telegramify_markdown(text: str) -> str:
    """Convert markdown to Telegram MarkdownV2 compatible format.

    Telegram MarkdownV2 supports: *bold*, _italic_, __underline__, ~strikethrough~,
    ||spoiler||, `code`, ```pre```, [links](url)

    It does NOT support: tables, HTML, images.
    """
    lines = text.split('\n')
    result = []
    in_table = False
    table_lines = []

    def flush_table():
        nonlocal table_lines
        if table_lines:
            result.append('```')
            result.extend(table_lines)
            result.append('```')
            table_lines = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        next_stripped = lines[i + 1].strip() if i + 1 < len(lines) else ""

        # Detect table: line starts/ends with | AND next line is separator (|---|)
        if stripped.startswith('|') and stripped.endswith('|'):
            if not in_table:
                if next_stripped.startswith('|') and '---' in next_stripped:
                    in_table = True
            if in_table:
                # Skip separator lines (|---|---|)
                if not all(c in '|-: ' for c in stripped.replace('|', '-')):
                    clean = stripped.strip('|').strip()
                    table_lines.append("│ {clean} │".format(clean=clean))
                continue
        else:
            if in_table:
                flush_table()
                in_table = False
            result.append(line)

    if in_table:
        flush_table()

    final = '\n'.join(result)
    final = re.sub(r'\|[-:\s|]+\|', '', final)

    return final


def sanitize_markdownv2(text: str) -> str:
    """Escape MarkdownV2 special characters for Telegram.
    
    Escape rules per Telegram Bot API:
    - Outside code blocks/inline: _ * [ ] ( ) ~ ` > # + - = | { } . !
    - Inside code blocks/inline: ` (backtick) and \\ (backslash)
    """
    result = []
    in_code_block = False
    in_inline_code = False
    
    i = 0
    while i < len(text):
        if text[i:i+3] == '```':
            in_code_block = not in_code_block
            result.append('```')
            i += 3
            continue
        
        if not in_code_block and text[i] == '`':
            in_inline_code = not in_inline_code
            result.append('`')
            i += 1
            continue
        
        ch = text[i]
        
        if in_code_block or in_inline_code:
            if ch in ('`', '\\'):
                result.append('\\' + ch)
            else:
                result.append(ch)
        else:
            if ch in '_*[]()~`>#+-=|{}.!':
                result.append('\\' + ch)
            else:
                result.append(ch)
        
        i += 1
    
    return ''.join(result)


def minimal_escape_mdv2(text: str) -> str:
    """Escape underscores for MarkdownV2, preserving code blocks.

    Escapes ALL underscores outside code blocks and inline code.
    This is safe for OpenCode output where underscores are typically
    literal (variable_names, file_names) rather than formatting.
    """
    parts = re.split(r'(```[\s\S]*?```|`[^`\n]*?`)', text)
    result = []
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2 == 1:
            result.append(part)
        else:
            # Escape MarkdownV2 reserved chars that commonly appear as literals
            # _ (underscore): variable_names, __init__, _private
            # . (period): end of sentences, file extensions
            # ! (exclamation): end of sentences
            # Escape ALL MarkdownV2 reserved chars that are commonly literal in OpenCode output
            # Safe to escape everywhere outside code: they're almost NEVER intentional formatting
            for ch in ('_', '.', '!', '+', '-', '=', '#', '>', '(', ')'):
                part = part.replace(ch, '\\' + ch)
            result.append(part)
    return ''.join(result)


def split_message(text: str, max_len: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split long text into Telegram-friendly chunks at natural boundaries.

    P5: Optimization opportunity — the current O(n) rfind loop could be replaced
    with a single regex split, but rfind is acceptable for typical response sizes.
    """
    if len(text) <= max_len:
        return [text]

    parts = []
    remaining = text
    total = (len(text) + max_len - 1) // max_len

    idx = 1
    while remaining:
        if len(remaining) <= max_len:
            parts.append(f"(parte {idx}/{total})\n{remaining}")
            break

        chunk = remaining[:max_len]
        split_at = max(
            chunk.rfind("\n\n"),
            chunk.rfind(". "),
            chunk.rfind("\n"),
            chunk.rfind(" "),
        )

        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len

        chunk = remaining[: split_at + 1].rstrip()
        parts.append(f"(parte {idx}/{total})\n{chunk}")
        remaining = remaining[split_at + 1:].lstrip()
        idx += 1

    return parts


async def send_telegram_mdv2(bot, chat_id: int, text: str) -> None:
    """Send a message with MarkdownV2 formatting. Falls back to plain text."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except Exception as e:
        logger.debug(f"MarkdownV2 failed for chat {chat_id}: {e}")
        # B5: Strip markdown formatting before plain-text fallback
        clean = text.replace('*', '').replace('`', '').replace('#', '').replace('_', '')
        clean = clean.replace('\\', '')
        try:
            await bot.send_message(chat_id=chat_id, text=clean)
        except Exception as e2:
            logger.error(f"Failed to send message to {chat_id}: {e2}")


def _assemble_response(raw_stdout: str, raw_stderr: str) -> str:
    """Build the final response from stdout and stderr, filtering metadata."""
    stdout_text = raw_stdout.strip() if raw_stdout else ""
    meaningful_stderr = _filter_stderr(raw_stderr)

    if stdout_text:
        response = stdout_text
        if meaningful_stderr:
            response += "\n\n---\n" + meaningful_stderr
    else:
        response = meaningful_stderr if meaningful_stderr else raw_stderr.strip()

    return clean_opencode_output(response)
