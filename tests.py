import os
path = r"E:\tradingsoftware\hummingbot\docs\lifecycle_event_schema.md"
if os.path.exists(path):
    with open(path) as f:
        lines = f.readlines()
    print(f"✓ Schema docs exist: {len(lines)} lines")
    # Show section headers
    for l in lines:
        if l.startswith("## "):
            print(f"  {l.strip()}")
else:
    print("✗ docs/lifecycle_event_schema.md not found")

# Also check replay script
path2 = r"E:\tradingsoftware\hummingbot\scripts\replay_lifecycle_jsonl_to_db.py"
print(f"{'✓' if os.path.exists(path2) else '✗'} Replay script exists")