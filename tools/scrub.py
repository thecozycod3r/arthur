"""Replace personal names with fictional stand-ins in a copy of the repo before publishing.

The name map lives in tools/scrub_map.json (gitignored, never published). Shape:
  {"replace": [["Real Name", "Stand-in"], ...],         # plain substring swaps, applied in order
   "quoted":  [["realname", "standin"], ...],            # swapped only inside '...' or "..." literals
   "words":   [["\\bACME\\b", "the firm"], ...]}         # regex swaps
See tools/scrub_map.example.json.
"""
import json, pathlib, re, sys

HERE = pathlib.Path(__file__).parent
cfg = json.loads((HERE / "scrub_map.json").read_text())
root = pathlib.Path(sys.argv[1])
for p in root.rglob("*"):
    if p.is_file() and p.suffix in (".py", ".html", ".mjs", ".sh", ".md", ".json") and ".git" not in p.parts:
        if p.name == "scrub_map.example.json":
            continue
        s = o = p.read_text()
        for a, b in cfg.get("replace", []): s = s.replace(a, b)
        for a, b in cfg.get("quoted", []): s = s.replace(f'"{a}"', f'"{b}"').replace(f"'{a}'", f"'{b}'")
        for a, b in cfg.get("words", []): s = re.sub(a, b, s)
        if s != o: p.write_text(s)
