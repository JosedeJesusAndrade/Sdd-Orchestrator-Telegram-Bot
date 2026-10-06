"""Verify the created Notion page."""
import os
import re
import sys
from notion_client import Client

# Force UTF-8 output
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
if not NOTION_TOKEN:
    with open(r"C:\Users\marie\.config\opencode\opencode.json", encoding="utf-8") as f:
        m = re.search(r'NOTION_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)', f.read())
        NOTION_TOKEN = m.group(1)

client = Client(auth=NOTION_TOKEN)
page_id = "3917c219-93af-8145-9a08-f8205885a8f5"

page = client.pages.retrieve(page_id)
print("Title:", page["properties"]["title"]["title"][0]["text"]["content"])
print("Icon:", page.get("icon"))
print("Parent:", page.get("parent"))
print("URL:", page.get("url"))
print()

children = client.blocks.children.list(block_id=page_id, page_size=100)
blocks = children["results"]
print(f"Total blocks in page: {len(blocks)}")
print()
print("Headings:")
h1_count = h2_count = h3_count = 0
for b in blocks:
    t = b["type"]
    if t in ("heading_1", "heading_2", "heading_3"):
        text = "".join(rt["text"]["content"] for rt in b[t]["rich_text"])
        if t == "heading_1":
            h1_count += 1
            prefix = "#"
        elif t == "heading_2":
            h2_count += 1
            prefix = "##"
        else:
            h3_count += 1
            prefix = "###"
        print(f"  {prefix} {text}")

print()
print(f"H1 count: {h1_count}")
print(f"H2 count: {h2_count}")
print(f"H3 count: {h3_count}")

# Block type breakdown
from collections import Counter
type_counts = Counter(b["type"] for b in blocks)
print()
print("Block type breakdown:")
for t, c in type_counts.most_common():
    print(f"  {t}: {c}")
