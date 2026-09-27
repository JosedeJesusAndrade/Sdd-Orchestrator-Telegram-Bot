# -*- coding: utf-8 -*-
# Clean up Section 3: remove leftover paragraph + divider that were between
# the old toggles and Section 4, so Section 3 ends cleanly at F7.
import os
import re
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"

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
    print("ERROR: NOTION_TOKEN not found")
    sys.exit(1)

from notion_client import Client

client = Client(auth=NOTION_TOKEN)
page_id = "3887c219-93af-81be-bcb3-ecc0507bf60c"

# Fetch all children
result = client.blocks.children.list(block_id=page_id)
all_blocks = result.get("results", [])
while result.get("has_more"):
    result = client.blocks.children.list(block_id=page_id, start_cursor=result["next_cursor"])
    all_blocks.extend(result.get("results", []))

print(f"Total blocks: {len(all_blocks)}")

# Find Section 3 and Section 4 headings
s3_idx = None
s4_idx = None
for i, b in enumerate(all_blocks):
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    if btype == "heading_1":
        if "Roadmap Prioritizado" in text:
            s3_idx = i
        elif "Principios" in text and "Dise" in text:
            s4_idx = i

print(f"Section 3 at {s3_idx}, Section 4 at {s4_idx}")

# Section 3 content is everything between
section_3 = all_blocks[s3_idx + 1 : s4_idx]
print(f"\nSection 3 has {len(section_3)} blocks. The last 3:")
for b in section_3[-3:]:
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    print(f"  - {btype}: {text[:80]!r} (id={b['id']})")

# Delete the leftover paragraph and divider
# They are the last 2 blocks before Section 4
to_delete = section_3[-2:]
for b in to_delete:
    print(f"\nDeleting leftover: {b.get('type')} {b['id']}")
    client.blocks.delete(block_id=b["id"])

# Verify
print("\n=== Verifying ===")
result2 = client.blocks.children.list(block_id=page_id)
new_all = result2.get("results", [])
while result2.get("has_more"):
    result2 = client.blocks.children.list(block_id=page_id, start_cursor=result2["next_cursor"])
    new_all.extend(result2.get("results", []))

# Re-locate sections
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

print(f"Section 3 at {s3_idx}, Section 4 at {s4_idx}")
print(f"Section 4 immediately follows Section 3: {s4_idx == s3_idx + 1 + (s4_idx - s3_idx - 1)}")

section_3_now = new_all[s3_idx + 1 : s4_idx]
print(f"\nFinal Section 3 has {len(section_3_now)} blocks")
print(f"Last 3 blocks of Section 3:")
for b in section_3_now[-3:]:
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    print(f"  - {btype}: {text[:80]!r}")
print(f"First 3 blocks of Section 4:")
for b in new_all[s4_idx : s4_idx + 3]:
    btype = b.get("type", "")
    rich = b.get(btype, {}).get("rich_text", [])
    text = "".join(t.get("plain_text", "") for t in rich)
    print(f"  - {btype}: {text[:80]!r}")

toggles_remaining = [b for b in new_all if b.get("type") == "toggle"]
print(f"\nTotal toggles remaining on page: {len(toggles_remaining)}")
