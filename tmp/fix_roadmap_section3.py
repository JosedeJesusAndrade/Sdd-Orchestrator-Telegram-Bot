# -*- coding: utf-8 -*-
# Replace toggle blocks in Section 3 of a Notion page with plain text blocks.
# Uses notion-client Python SDK directly (notion_API MCP wrapper has a bug).
# Run from project directory to avoid inspect.py shadowing.
import os
import re
import sys

# Force UTF-8 for PowerShell cp1252 issues with emojis
os.environ["PYTHONIOENCODING"] = "utf-8"

# === Step 1: Find NOTION_TOKEN ===
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
if not NOTION_TOKEN:
    env_path = r"C:\Users\marie\.config\opencode\opencode.json"
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            content = f.read()
            match = re.search(r'NOTION_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)', content)
            if match:
                NOTION_TOKEN = match.group(1)

if not NOTION_TOKEN:
    print("ERROR: NOTION_TOKEN not found in env or opencode.json")
    sys.exit(1)

print(f"OK: NOTION_TOKEN loaded (len={len(NOTION_TOKEN)})")

# === Step 2: Get current page children ===
from notion_client import Client

client = Client(auth=NOTION_TOKEN)
page_id = "3887c219-93af-81be-bcb3-ecc0507bf60c"

print(f"\n=== Fetching children of page {page_id} ===")
result = client.blocks.children.list(block_id=page_id)
all_blocks = result.get("results", [])

# Paginate if needed
while result.get("has_more"):
    result = client.blocks.children.list(block_id=page_id, start_cursor=result["next_cursor"])
    all_blocks.extend(result.get("results", []))

print(f"Total blocks on page: {len(all_blocks)}")

# === Step 3: Identify Section 3 blocks ===
# Find indices of Section 3 heading and Section 4 heading
section_3_idx = None
section_4_idx = None
for i, b in enumerate(all_blocks):
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    if btype == "heading_1":
        if "Roadmap Prioritizado" in text:
            section_3_idx = i
        elif "Principios" in text and "Dise" in text:
            section_4_idx = i

print(f"Section 3 heading index: {section_3_idx}")
print(f"Section 4 heading index: {section_4_idx}")

if section_3_idx is None:
    print("ERROR: Could not find Section 3 heading 'Roadmap Prioritizado'")
    sys.exit(1)
if section_4_idx is None:
    print("WARNING: Could not find Section 4 heading 'Principios de Diseno Aplicados'")
    print("Will use end of page as Section 3 boundary")
    section_4_idx = len(all_blocks)

section_3_blocks = all_blocks[section_3_idx + 1 : section_4_idx]
print(f"\nBlocks in Section 3: {len(section_3_blocks)}")
for b in section_3_blocks:
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    has_children = b.get("has_children", False)
    print(f"  - {b['id']} type={btype} has_children={has_children} text={text[:80]!r}")

section_3_heading_id = all_blocks[section_3_idx]["id"]
print(f"\nSection 3 heading ID: {section_3_heading_id}")

# === Step 4: Delete existing toggle blocks in Section 3 ===
toggles_to_delete = [b for b in section_3_blocks if b.get("type") == "toggle"]
print(f"\n=== Deleting {len(toggles_to_delete)} toggle blocks ===")

deleted_count = 0
for t in toggles_to_delete:
    try:
        client.blocks.delete(block_id=t["id"])
        deleted_count += 1
        print(f"  Deleted toggle {t['id']}")
    except Exception as e:
        print(f"  ERROR deleting {t['id']}: {e}")

# === Step 5: Build new plain-text blocks ===
def p(text):
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }

def h3(text):
    return {
        "object": "block",
        "type": "heading_3",
        "heading_3": {
            "rich_text": [{"type": "text", "text": {"content": text}}]
        },
    }

new_blocks = []

new_blocks.append(h3("F1 \u2014 Pre-flight checks en `/update` (P1)"))
new_blocks.append(p("Why P1: solves the most common failure (uncommitted changes) without adding complexity"))
new_blocks.append(p("Orthogonality: pure addition, doesn't touch existing happy path"))
new_blocks.append(p("SOLID: SRP (one job: validate state), OCP (extensible via strategy pattern)"))
new_blocks.append(p("Implementation: in `launcher.bat`, BEFORE `git pull`, run `git status --porcelain` and check for non-empty output. Exit with code 43 (= 'uncommitted changes, refusing to update') if dirty. Bot catches code 43 and tells user."))
new_blocks.append(p("Risks: race condition between check and pull (mitigable with `--ff-only`)"))
new_blocks.append(p("Effort: S"))

new_blocks.append(h3("F2 \u2014 Enhanced post-restart notification (P2)"))
new_blocks.append(p("Why P2: gives user confidence about what version is running"))
new_blocks.append(p("Orthogonality: independent feature, doesn't touch `/update` logic"))
new_blocks.append(p("SOLID: SRP (only logs version)"))
new_blocks.append(p("Implementation: in `run_bot()`, store `current_commit = subprocess.run(['git', 'rev-parse', 'HEAD'], ...).stdout` in `bot_data`. After startup notification, also send commit hash + short message."))
new_blocks.append(p("Risks: very low"))
new_blocks.append(p("Effort: S"))

new_blocks.append(h3("F3 \u2014 `/update --rebase` (P2)"))
new_blocks.append(p("Why P2: solves the unpushed-commits failure mode"))
new_blocks.append(p("Orthogonality: orthogonal to F1, both can coexist"))
new_blocks.append(p("SOLID: SRP (only handles rebase case)"))
new_blocks.append(p("Implementation: in `update_command`, parse `--rebase` flag from `context.args`. Set `exit_code[0] = 44` (= 'rebase mode'). `launcher.bat` checks if code is 42 OR 44, runs `git pull --rebase` for code 44."))
new_blocks.append(p("Risks: rebase can create conflicts (user must handle manually, but at least state is preserved)"))
new_blocks.append(p("Effort: S"))

new_blocks.append(h3("F4 \u2014 `/sync` command (P3)"))
new_blocks.append(p("Why P3: nice-to-have, separates 'update code' from 'restart bot'"))
new_blocks.append(p("Orthogonality: new command, totally independent"))
new_blocks.append(p("SOLID: SRP (only syncs, doesn't restart)"))
new_blocks.append(p("Implementation: new `sync_command` in `handlers/ci.py`. Runs `git pull --rebase` directly (not via exit code). Reports success/failure with commit info."))
new_blocks.append(p("Risks: low"))
new_blocks.append(p("Effort: S"))

new_blocks.append(h3("F5 \u2014 `/log` command (P3)"))
new_blocks.append(p("Why P3: visibility tool, helps user decide if `/update` is worth it"))
new_blocks.append(p("Orthogonality: pure read-only command"))
new_blocks.append(p("SOLID: SRP (only shows git log)"))
new_blocks.append(p("Implementation: new `log_command` in `handlers/ci.py`. Runs `git log --oneline -N`. Format output nicely. Default N=5, configurable via arg."))
new_blocks.append(p("Risks: very low"))
new_blocks.append(p("Effort: S"))

new_blocks.append(h3("F6 \u2014 Enhanced `/status` with git info (P3)"))
new_blocks.append(p("Why P3: complements F2, makes version visible always"))
new_blocks.append(p("Orthogonality: additive to existing status"))
new_blocks.append(p("SOLID: OCP (extends without modifying)"))
new_blocks.append(p("Implementation: in `status_command`, add sections for: current commit, ahead/behind origin/main, last update timestamp. Reuse the `current_commit` from F2."))
new_blocks.append(p("Risks: low"))
new_blocks.append(p("Effort: S"))

new_blocks.append(h3("F7 \u2014 `/update --force` (stash + reset) (P4)"))
new_blocks.append(p("Why P4: dangerous, last resort. Use only when you know what you're losing."))
new_blocks.append(p("Orthogonality: orthogonal but dangerous"))
new_blocks.append(p("SOLID: not great for SRP (mixes concerns), but pragmatic"))
new_blocks.append(p("Implementation: in `launcher.bat`, for code 45 (= 'force mode'), do `git stash && git reset --hard origin/main && git stash pop`. If stash pop conflicts, abort."))
new_blocks.append(p("Risks: HIGH \u2014 can lose uncommitted work. Should require explicit confirmation."))
new_blocks.append(p("Effort: M"))

print(f"\n=== Inserting {len(new_blocks)} new plain-text blocks ===")

# === Step 6: Insert in chunks of 100, using `after` parameter ===
chunk_size = 100
for i in range(0, len(new_blocks), chunk_size):
    chunk = new_blocks[i : i + chunk_size]
    try:
        client.blocks.children.append(
            block_id=page_id,
            children=chunk,
            after=section_3_heading_id,
        )
        print(f"  Inserted chunk {i // chunk_size + 1} ({len(chunk)} blocks)")
    except Exception as e:
        print(f"  ERROR inserting chunk: {e}")
        sys.exit(1)

# === Step 7: Verify ===
print(f"\n=== Verifying ===")
result2 = client.blocks.children.list(block_id=page_id)
new_all = result2.get("results", [])
while result2.get("has_more"):
    result2 = client.blocks.children.list(block_id=page_id, start_cursor=result2["next_cursor"])
    new_all.extend(result2.get("results", []))

print(f"Total blocks now: {len(new_all)}")

# Find Section 3 again
s3_idx = None
s4_idx = None
for i, b in enumerate(new_all):
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    if btype == "heading_1":
        if "Roadmap Prioritizado" in text:
            s3_idx = i
        elif "Principios" in text and "Dise" in text:
            s4_idx = i

print(f"Section 3 heading at index: {s3_idx}")
print(f"Section 4 heading at index: {s4_idx}")

new_section_3 = []
if s3_idx is not None and s4_idx is not None:
    new_section_3 = new_all[s3_idx + 1 : s4_idx]
    print(f"\nNew Section 3 has {len(new_section_3)} blocks:")
    for b in new_section_3:
        btype = b.get("type", "")
        rich = b.get(btype, {}).get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich)
        print(f"  - {btype}: {text[:90]!r}")
    
    toggles_remaining = [b for b in new_section_3 if b.get("type") == "toggle"]
    print(f"\nToggles remaining in Section 3: {len(toggles_remaining)}")

print(f"\n=== SUMMARY ===")
print(f"Toggles deleted: {deleted_count}")
print(f"New blocks added: {len(new_blocks)}")
if s3_idx is not None and s4_idx is not None:
    print(f"Section 3 now contains: {len(new_section_3)} blocks")
    print(f"Section 4 follows Section 3: {s4_idx == s3_idx + 1 + len(new_blocks)}")
else:
    print(f"Section 3 now contains: UNKNOWN (could not locate section markers)")
