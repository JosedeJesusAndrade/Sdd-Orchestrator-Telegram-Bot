"""Verify all blocks in the page."""
import os
import re
import sys
from notion_client import Client

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
if not NOTION_TOKEN:
    with open(r"C:\Users\marie\.config\opencode\opencode.json", encoding="utf-8") as f:
        m = re.search(r'NOTION_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)', f.read())
        NOTION_TOKEN = m.group(1)

client = Client(auth=NOTION_TOKEN)
page_id = "3917c219-93af-8145-9a08-f8205885a8f5"

all_blocks = []
cursor = None
while True:
    resp = client.blocks.children.list(block_id=page_id, page_size=100, start_cursor=cursor)
    all_blocks.extend(resp["results"])
    if not resp.get("has_more"):
        break
    cursor = resp["next_cursor"]

print(f"Total blocks fetched: {len(all_blocks)}")
print()
print("All headings:")
for b in all_blocks:
    t = b["type"]
    if t in ("heading_1", "heading_2", "heading_3"):
        text = "".join(rt["text"]["content"] for rt in b[t]["rich_text"])
        prefix = {"heading_1": "## ", "heading_2": "### ", "heading_3": "    #### "}[t]
        print(f"{prefix}{text}")

print()
print("Tables found:")
for b in all_blocks:
    if b["type"] == "table":
        print(f"  Table: id={b['id']}, width={b['table'].get('table_width')}")
        # List table children (rows)
        rows = client.blocks.children.list(block_id=b["id"])
        for r in rows["results"]:
            if r["type"] == "table_row":
                cells = ["".join(rt["text"]["content"] for rt in c) for c in r["table_row"]["cells"]]
                print(f"    Row: {cells}")

print()
print(f"Callouts: {sum(1 for b in all_blocks if b['type'] == 'callout')}")
print(f"Code blocks: {sum(1 for b in all_blocks if b['type'] == 'code')}")
print(f"Dividers: {sum(1 for b in all_blocks if b['type'] == 'divider')}")
print(f"Paragraphs: {sum(1 for b in all_blocks if b['type'] == 'paragraph')}")
print(f"Bulleted items: {sum(1 for b in all_blocks if b['type'] == 'bulleted_list_item')}")
print(f"Numbered items: {sum(1 for b in all_blocks if b['type'] == 'numbered_list_item')}")
