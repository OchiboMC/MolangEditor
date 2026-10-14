#!/usr/bin/env python3
"""Embed a collect_bedrock_molang.py JSON index in the offline MoLang Editor.

Example:
    python embed_molang_catalog.py molang_variables.json MoLang_Editor.html -o MoLang_Editor_Full.html
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re

TAG_RE = re.compile(r'(<script\s+id="builtInCollectorData"\s+type="application/json">)(.*?)(</script>)', re.S)
CATEGORIES = ("query", "variable", "temp", "context", "math", "array", "this")


def embed(json_path: Path, editor_path: Path, output_path: Path) -> int:
    data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not isinstance(data.get("index"), dict):
        raise ValueError("collector JSON must contain an 'index' object")
    index = data['index']
    if any(k in index and not isinstance(index[k], dict) for k in CATEGORIES):
        raise ValueError("index category must be an object")
    count = sum(len(index.get(k, {})) for k in CATEGORIES)
    if not count:
        raise ValueError("collector index contains no identifiers")
    html = editor_path.read_text(encoding="utf-8")
    if len(TAG_RE.findall(html)) != 1:
        raise ValueError("Editor needs one builtInCollectorData script marker")
    payload = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    payload = payload.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    replaced = TAG_RE.sub(lambda m: m.group(1) + payload + m.group(3), html, count=1)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(replaced, encoding="utf-8")
    print(f"Created: {output_path} ({count} indexed identifiers)")
    return count


def cli() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('catalog', type=Path, help='collector JSON output')
    p.add_argument('editor', type=Path, help='MoLang Editor HTML file')
    p.add_argument('-o', '--out', type=Path, default=Path('MoLang_Editor_with_Catalog.html'))
    args = p.parse_args()
    embed(args.catalog, args.editor, args.out)


if __name__ == '__main__':
    cli()
