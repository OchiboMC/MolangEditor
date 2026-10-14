#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect MoLang identifiers used by Minecraft: Bedrock Edition animations.

Default source: the PUBLIC Mojang/bedrock-samples GitHub repository.
Local folders, .zip, .mcpack, and .mcaddon sources are also supported.
Only Python's standard library is required (Python 3.9+).

Examples:
  python collect_bedrock_molang.py
  python collect_bedrock_molang.py --out molang_variables.json
  python collect_bedrock_molang.py --source ./resource_pack
  python collect_bedrock_molang.py --source pack.mcpack --source ./another_pack
  python collect_bedrock_molang.py --github --source ./my_pack --no-controllers
  python collect_bedrock_molang.py --include-expressions
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile


NAMESPACES = {
    "query": "query", "q": "query",
    "variable": "variable", "v": "variable",
    "temp": "temp", "t": "temp",
    "context": "context", "c": "context",
    "math": "math", "m": "math",
    "array": "array", "a": "array",
}
CATEGORIES = ("query", "variable", "temp", "context", "math", "array", "this")
# A whole MoLang identifier (and optional nested fields), not arbitrary substrings.
# Full namespace canonicalization is applied after matching.
SYMBOL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"(?P<ns>query|variable|temp|context|math|array|q|v|t|c|m|a)"
    r"\s*\.\s*"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\s*\.\s*[A-Za-z_][A-Za-z0-9_]*)*)",
    re.IGNORECASE,
)
IDENT_PART_RE = re.compile(r"\s*\.\s*")
THIS_RE = re.compile(r"(?<![A-Za-z0-9_.])this(?![A-Za-z0-9_])", re.IGNORECASE)
MAX_JSON_BYTES = 10 * 1024 * 1024


def strip_jsonc(text: str) -> str:
    """Convert JSON with // and /* */ comments and trailing commas into JSON.

    Preserves newlines and most offsets for easier troubleshooting. Comments in
    actual JSON string values (e.g. URLs) are never removed.
    """
    chars = list(text)
    n = len(chars)
    i, in_string, escaped = 0, False, False
    while i < n:
        ch = chars[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == '/' and i + 1 < n and chars[i + 1] == '/':
            j = i + 2
            while j < n and chars[j] not in '\r\n':
                j += 1
            for k in range(i, j):
                chars[k] = ' '
            i = j
            continue
        if ch == '/' and i + 1 < n and chars[i + 1] == '*':
            j = i + 2
            while j + 1 < n and not (chars[j] == '*' and chars[j + 1] == '/'):
                j += 1
            if j + 1 >= n:
                raise ValueError('Unterminated JSON block comment')
            j += 2
            for k in range(i, j):
                if chars[k] not in '\r\n':
                    chars[k] = ' '
            i = j
            continue
        i += 1

    # Remove only commas followed by ] or } outside quoted text.
    in_string, escaped = False, False
    for i, ch in enumerate(chars):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == ',':
            j = i + 1
            while j < n and chars[j].isspace():
                j += 1
            if j < n and chars[j] in ']}':
                chars[i] = ' '
    return ''.join(chars)


def molang_code_mask(value: str) -> str:
    """Blank comments and MoLang string literals, keeping offset alignment.

    The input is an already-decoded JSON string. A `variable.x` inside a quoted
    string or a comment is data, not a variable reference.
    """
    result = list(value)
    i, n = 0, len(value)
    while i < n:
        c = value[i]
        if c in "\"'":
            delim = c
            start = i
            i += 1
            while i < n:
                if value[i] == '\\':
                    i += 2
                elif value[i] == delim:
                    i += 1
                    break
                else:
                    i += 1
            for p in range(start, min(i, n)):
                if value[p] not in '\r\n':
                    result[p] = ' '
            continue
        if value.startswith('//', i):
            start = i
            while i < n and value[i] not in '\r\n':
                i += 1
            for p in range(start, i):
                result[p] = ' '
            continue
        if value.startswith('/*', i):
            start = i
            end = value.find('*/', i + 2)
            i = n if end < 0 else end + 2
            for p in range(start, i):
                if value[p] not in '\r\n':
                    result[p] = ' '
            continue
        i += 1
    return ''.join(result)


def collect_from_expression(expression: str) -> list[tuple[str, str, int]]:
    masked = molang_code_mask(expression)
    matches = []
    for m in SYMBOL_RE.finditer(masked):
        kind = NAMESPACES[m.group('ns').lower()]
        member = IDENT_PART_RE.sub('.', m.group('name')).lower()
        matches.append((kind, f'{kind}.{member}', m.start()))
    for match in THIS_RE.finditer(masked):
        matches.append(('this', 'this', match.start()))
    matches.sort(key=lambda item: item[2])
    return matches


def pointer_escape(segment: Any) -> str:
    return str(segment).replace('~', '~0').replace('/', '~1')


def walk_strings(obj: Any, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from walk_strings(value, path + '/' + pointer_escape(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from walk_strings(value, path + '/' + str(i))


ENTITY_DIRS = ('entity', 'entities', 'attachables')
NOT_ENTITY_PARENTS = ('metadata', 'json_schemas', 'models', 'textures', 'sounds', 'texts',
                      'loot_tables', 'spawn_rules', 'trading', 'biomes', 'recipes')
# A complete animation / animation-controller ID as written in entity files.
ANIMATION_ID_RE = re.compile(r'^(?:controller\.)?animation\.[A-Za-z0-9_.\-]+$')


def file_type(path: str) -> str | None:
    """Accept nested subpacks, zipped folders, and standalone RP roots.

    Returns 'animations' / 'animation_controllers' for definition files, and
    'entity_references' for client/server entity and attachable files, which name
    the animation IDs they use (including IDs whose definition is built into the
    game and therefore exists in no JSON file).
    """
    if not path.lower().endswith('.json'):
        return None
    chunks = path.replace('\\', '/').lower().split('/')
    # Pick the nearest recognized directory, including nested resource packs.
    for component in reversed(chunks[:-1]):
        if component in ('animations', 'animation_controllers'):
            return component
    # `entity` also names unrelated folders (models/entity geometry, JSON schemas, ...).
    if any(component in NOT_ENTITY_PARENTS for component in chunks[:-1]):
        return None
    for component in reversed(chunks[:-1]):
        if component in ENTITY_DIRS:
            return 'entity_references'
    return None


def load_document(content: bytes, label: str) -> dict:
    if len(content) > MAX_JSON_BYTES:
        raise ValueError(f'JSON file larger than {MAX_JSON_BYTES} bytes')
    txt = content.decode('utf-8-sig')
    try:
        data = json.loads(strip_jsonc(txt))
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f'JSON/JSONC parse failed: {e}') from e
    if not isinstance(data, dict):
        raise ValueError('Top-level JSON must be an object')
    return data


def read_source(path: Path) -> Iterator[tuple[str, bytes]]:
    """Yield matching JSON files from a directory or archive (including mcaddon)."""
    if path.is_dir():
        for file in sorted(path.rglob('*.json')):
            relative = file.relative_to(path).as_posix()
            # Classify by the path inside the source plus the source folder's own name
            # (so `--source ./animations` works), never by folders above the source:
            # /home/me/entity/... must not turn every file into an entity file.
            virtual = f'{path.resolve().name}/{relative}'
            if file_type(virtual):
                yield f'local:{path.resolve()}!/{virtual}', file.read_bytes()
        return
    if path.suffix.lower() not in ('.zip', '.mcpack', '.mcaddon'):
        raise ValueError(f'Expected a directory, .zip, .mcpack or .mcaddon: {path}')
    with zipfile.ZipFile(path) as archive:
        for info in sorted(archive.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            if file_type(info.filename):
                if info.file_size > MAX_JSON_BYTES:
                    raise ValueError(f'JSON too big: {path}!/{info.filename}')
                yield f'archive:{path.resolve()}!/{info.filename}', archive.read(info)
            elif path.suffix.lower() == '.mcaddon' and info.filename.lower().endswith('.mcpack'):
                # .mcaddon often wraps one or more .mcpack ZIPs.
                if info.file_size > 200 * 1024 * 1024:
                    raise ValueError(f'Nested mcpack too big: {info.filename}')
                with zipfile.ZipFile(io.BytesIO(archive.read(info))) as nested:
                    for inner in nested.infolist():
                        if not inner.is_dir() and file_type(inner.filename):
                            if inner.file_size > MAX_JSON_BYTES:
                                raise ValueError(f'JSON too big: {inner.filename}')
                            yield (f'archive:{path.resolve()}!/{info.filename}!/{inner.filename}',
                                   nested.read(inner))


def http_read(url: str, timeout: float = 35.0, retries: int = 3) -> bytes:
    last_exception: Exception | None = None
    for attempt in range(retries):
        try:
            headers = {
                'User-Agent': 'Bedrock-MoLang-Variable-Collector/1.0',
                'Accept': 'application/vnd.github+json' if 'api.github.com' in url else '*/*',
            }
            # Unauthenticated api.github.com is limited to 60 requests/hour per IP, which
            # shared CI runners exhaust easily. The token is sent to the API host only.
            token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
            if token and url.startswith('https://api.github.com/'):
                headers['Authorization'] = f'Bearer {token}'
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as resp:
                return resp.read()
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            last_exception = error
            # Fail fast on request errors that won't improve when retried.
            if isinstance(error, HTTPError) and error.code not in (403, 408, 429, 500, 502, 503, 504):
                break
            if attempt < retries - 1:
                time.sleep(min(2**attempt, 4))
    raise RuntimeError(f'HTTP request failed {url}: {last_exception}')


def github_files(repo: str, ref: str, cache: Path, workers: int,
                 refresh: bool, timeout: float) -> tuple[list[tuple[str, bytes]], list[str], str]:
    """Download only animation/controller JSON files, NOT huge textures/archives."""
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('--repo must be an owner/repository name')
    if not ref or '/' in ref or '\\' in ref or ref in ('.', '..'):
        raise ValueError('--ref must be a branch or tag without slashes')
    cache.mkdir(parents=True, exist_ok=True)
    tree_url = f'https://api.github.com/repos/{repo}/git/trees/{quote(ref, safe="")}?recursive=1'
    tree = json.loads(http_read(tree_url, timeout=timeout))
    if tree.get('truncated'):
        raise RuntimeError('GitHub file tree was truncated; use a local clone/ZIP with --source')
    if not isinstance(tree.get('tree'), list):
        raise RuntimeError(f'GitHub API error: {tree.get("message", "missing tree")}')
    items = sorted({x['path'] for x in tree['tree']
                    if x.get('type') == 'blob' and file_type(x.get('path', ''))})
    if not items:
        raise RuntimeError(f'No animation JSON files found in GitHub repository {repo}@{ref}')
    errors = []
    results = []
    def download_one(name: str) -> tuple[str, bytes]:
        identity = f'{repo}@{ref}/{name}'
        key = hashlib.sha256(identity.encode()).hexdigest()
        target = cache / key[:2] / (key + '.json')
        if not refresh and target.is_file():
            return f'github:{identity}', target.read_bytes()
        url = f'https://raw.githubusercontent.com/{repo}/{quote(ref, safe="")}/{quote(name, safe="/")}'
        data = http_read(url, timeout=timeout)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError(f'File too large: {name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + '.part')
        temp.write_bytes(data)
        temp.replace(target)
        return f'github:{identity}', data
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_to_name = {pool.submit(download_one, name): name for name in items}
        for fut in as_completed(future_to_name):
            name = future_to_name[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                errors.append(f'github:{repo}@{ref}/{name}: {e}')
    results.sort(key=lambda item: item[0])
    return results, errors, tree.get('sha', ref)


GITHUB_LABEL_RE = re.compile(r'^github:[^@]+@[^/]+/')


def label_path(label: str) -> str:
    """The path of a source label inside its pack (what file_type() should look at)."""
    if '!/' in label:
        return label.rsplit('!/', 1)[-1]
    return GITHUB_LABEL_RE.sub('', label)


def analyze(sources: list[tuple[str, bytes]], include_controllers: bool,
            include_expressions: bool, include_references: bool = True) -> dict:
    """Parse every definition independently, then merge same-name definitions.

    Entity / attachable files are scanned only for the animation and controller IDs
    they name. An ID that is used there but defined in no JSON file (typically a
    definition built into the game, e.g. animation.ghast.scale) is still listed, with
    `defined: false`, so it can be completed even though its MoLang is unknown.
    """
    definitions: dict[str, dict[str, dict]] = {'animations': {}, 'animation_controllers': {}}
    errors: list[str] = []
    warnings: list[str] = []
    references: dict[str, set[str]] = {}
    stats = {'input_files': 0, 'files_with_definitions': 0, 'definitions': 0,
             'occurrences': 0, 'entity_files': 0}
    for source, data in sorted(sources, key=lambda pair: pair[0]):
        source_kind = file_type(label_path(source))
        if source_kind is None:
            continue
        if source_kind == 'animation_controllers' and not include_controllers:
            continue
        if source_kind == 'entity_references':
            if not include_references:
                continue
            stats['entity_files'] += 1
            try:
                root = load_document(data, source)
            except (ValueError, UnicodeError) as err:
                # Supplementary data: report it, but do not discard the whole dataset.
                warnings.append(f'{source}: {err}')
                continue
            for _, text in walk_strings(root, ''):
                if ANIMATION_ID_RE.match(text):
                    references.setdefault(text, set()).add(label_path(source))
            continue
        stats['input_files'] += 1
        try:
            root = load_document(data, source)
        except (ValueError, UnicodeError) as err:
            errors.append(f'{source}: {err}')
            continue
        defs = root.get(source_kind, {})
        if not isinstance(defs, dict):
            errors.append(f'{source}: {source_kind} is not an object')
            continue
        if defs:
            stats['files_with_definitions'] += 1
        for name, spec in sorted(defs.items()):
            if not isinstance(spec, dict):
                errors.append(f'{source}: {name} is not an object')
                continue
            stats['definitions'] += 1
            per_expression: dict[tuple[str, str, str], list[int]] = defaultdict(list)
            values: dict[str, str] = {}
            for path, expr in walk_strings(spec, '/' + pointer_escape(source_kind) + '/' + pointer_escape(name)):
                for category, symbol, offset in collect_from_expression(expr):
                    per_expression[(path, category, symbol)].append(offset)
                    if include_expressions:
                        values[path] = expr
            occurrences = []
            for (path, category, symbol), offsets in sorted(per_expression.items()):
                occurrence = {'symbol': symbol, 'category': category,
                              'json_pointer': path, 'offsets': offsets}
                if include_expressions:
                    occurrence['expression'] = values[path]
                occurrences.append(occurrence)
                stats['occurrences'] += len(offsets)
            bucket = definitions[source_kind].setdefault(name, {'defined': True,
                                                                 'symbols': {k: [] for k in CATEGORIES},
                                                                 'definitions': []})
            bucket['definitions'].append({'source': source, 'occurrences': occurrences})
    referenced_only = {'animations': 0, 'animation_controllers': 0}
    for ref_id, files in sorted(references.items()):
        group = 'animation_controllers' if ref_id.lower().startswith('controller.') else 'animations'
        if group == 'animation_controllers' and not include_controllers:
            continue
        known = {existing.lower() for existing in definitions[group]}
        if ref_id.lower() in known:
            continue
        definitions[group][ref_id] = {'defined': False,
                                      'symbols': {k: [] for k in CATEGORIES},
                                      'definitions': [],
                                      'referenced_by': sorted(files)}
        referenced_only[group] += 1
    index: dict[str, dict[str, dict]] = {k: {} for k in CATEGORIES}
    for group, by_name in definitions.items():
        for name, item in sorted(by_name.items()):
            symbols = {k: set() for k in CATEGORIES}
            for definition in item['definitions']:
                for occurrence in definition['occurrences']:
                    symbol, kind = occurrence['symbol'], occurrence['category']
                    symbols[kind].add(symbol)
                    index_entry = index[kind].setdefault(symbol, {
                        'animations': set(), 'animation_controllers': set(),
                        'references': 0})
                    index_entry[group].add(name)
                    index_entry['references'] += len(occurrence['offsets'])
            item['symbols'] = {k: sorted(symbols[k]) for k in CATEGORIES}
    for category in CATEGORIES:
        index[category] = {
            name: {'animations': sorted(entry['animations']),
                   'animation_controllers': sorted(entry['animation_controllers']),
                   'references': entry['references']}
            for name, entry in sorted(index[category].items())
        }
    for group in definitions:
        definitions[group] = dict(sorted(definitions[group].items()))
    return {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'scope_note': ('Direct MoLang references in publicly available animation and animation-controller '
                       'definitions, plus animation / controller IDs named by entity and attachable files '
                       '(entries with defined=false have no JSON definition, e.g. engine built-ins); '
                       'not all private or third-party animations.'),
        'canonical_namespaces': list(CATEGORIES),
        'statistics': {**stats, 'animation_count': len(definitions['animations']),
                       'animation_controller_count': len(definitions['animation_controllers']),
                       'referenced_only_animation_count': referenced_only['animations'],
                       'referenced_only_animation_controller_count': referenced_only['animation_controllers'],
                       'distinct_symbols': {k: len(index[k]) for k in CATEGORIES}},
        'animations': definitions['animations'],
        'animation_controllers': definitions['animation_controllers'],
        'index': index,
        'errors': errors,
        'warnings': warnings,
    }


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Collect MoLang identifiers from Bedrock animations into one JSON file.')
    parser.add_argument('--source', '-s', type=Path, action='append', default=[],
                        help='Local resource-pack folder or ZIP/MCPACK/MCADDON (repeatable)')
    parser.add_argument('--github', action='store_true',
                        help='Use GitHub public sources in addition to --source (automatic if no sources)')
    parser.add_argument('--repo', default='Mojang/bedrock-samples', help='GitHub owner/repo (default Mojang/bedrock-samples)')
    parser.add_argument('--ref', default='main', help='GitHub branch/tag (default main)')
    parser.add_argument('--out', '-o', type=Path, default=Path('molang_variables.json'))
    parser.add_argument('--cache', type=Path, default=Path.home() / '.cache' / 'bedrock-molang-collector')
    parser.add_argument('--workers', type=int, default=8, help='Concurrent raw-file requests; default 8')
    parser.add_argument('--timeout', type=float, default=35.0, help='Per HTTP request timeout (seconds)')
    parser.add_argument('--refresh', action='store_true', help='Redownload files instead of using cache')
    parser.add_argument('--no-controllers', action='store_true', help='Scan animations only')
    parser.add_argument('--no-entity-refs', action='store_true',
                        help='Do not scan entity/attachable files for IDs that have no JSON definition')
    parser.add_argument('--include-expressions', action='store_true',
                        help='Include full MoLang source strings in occurrences (larger JSON)')
    parser.add_argument('--allow-partial', action='store_true',
                        help='Write output even if some source files fail to download or parse')
    args = parser.parse_args(argv)
    if args.workers < 1 or args.timeout <= 0:
        parser.error('--workers and --timeout must be greater than zero')

    sources: list[tuple[str, bytes]] = []
    errors: list[str] = []
    github_commit: str | None = None
    should_github = args.github or not args.source
    if should_github:
        print(f'[1/3] Listing and downloading {args.repo}@{args.ref}...', file=sys.stderr)
        try:
            got, errs, github_commit = github_files(args.repo, args.ref, args.cache, args.workers,
                                                     args.refresh, args.timeout)
            sources.extend(got)
            errors.extend(errs)
        except Exception as err:
            errors.append(f'GitHub: {err}')
    for path in args.source:
        print(f'[1/3] Reading {path}...', file=sys.stderr)
        try:
            if not path.exists():
                raise FileNotFoundError(path)
            sources.extend(read_source(path))
        except Exception as err:
            errors.append(f'{path}: {err}')
    if not sources:
        for error in errors:
            print('ERROR:', error, file=sys.stderr)
        print('No input files were found; output not created.', file=sys.stderr)
        return 1

    print(f'[2/3] Parsing {len(sources)} JSON files...', file=sys.stderr)
    output = analyze(sources, not args.no_controllers, args.include_expressions,
                     include_references=not args.no_entity_refs)
    output['sources'] = {'github': {'repository': args.repo, 'ref': args.ref, 'tree_sha': github_commit}
                         if should_github else None,
                         'local': [str(path.resolve()) for path in args.source]}
    output['errors'] = errors + output['errors']
    if output['errors'] and not args.allow_partial:
        for err in output['errors'][:30]:
            print('ERROR:', err, file=sys.stderr)
        print(f'{len(output["errors"])} errors; no JSON written. '
              'Use --allow-partial if you accept an incomplete dataset.', file=sys.stderr)
        return 1
    stats_out = output['statistics']
    defined_count = (stats_out['animation_count'] - stats_out['referenced_only_animation_count']
                     + stats_out['animation_controller_count']
                     - stats_out['referenced_only_animation_controller_count'])
    if defined_count == 0:
        print('No animation definitions found; output not written.', file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(args.out.name + '.tmp')
    try:
        tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        tmp.replace(args.out)
    finally:
        if tmp.exists():
            tmp.unlink()
    counts = output['statistics']
    print(f'[3/3] Saved {args.out.resolve()}', file=sys.stderr)
    print(f'  animations: {counts["animation_count"]}, controllers: {counts["animation_controller_count"]}'
          f' (of which referenced only: {counts["referenced_only_animation_count"]}'
          f' / {counts["referenced_only_animation_controller_count"]}; entity files scanned: {counts["entity_files"]})',
          file=sys.stderr)
    print(f'  matches: {counts["occurrences"]}, unique symbols: {counts["distinct_symbols"]}', file=sys.stderr)
    for warning in output['warnings'][:20]:
        print('  WARNING (entity file skipped):', warning, file=sys.stderr)
    if output['errors']:
        print(f'  WARNING: {len(output["errors"])} source errors. Output is incomplete.', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(cli())
