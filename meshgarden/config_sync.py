"""Text-preserving TOML config sync (Task 16).

`sync_missing_keys` adds to an existing config the keys that are present in
a template but missing from it -- each with the template's default value
and its comment -- without changing, reordering, or reformatting any line
that already exists. Detection is TOML-aware (both texts are parsed with
`tomllib` to diff key sets); the merge itself is pure text splicing, so
every existing line survives byte-for-byte.

Only top-level tables are diffed. A template key whose value is itself a
nested table (an inline table or a dotted sub-table) is unsupported by
this simple line-splicing approach -- current Meshgarden config tables are
all flat, so this is a defensive no-op rather than something real configs
hit today. Such keys are reported back via the `skipped` return value
instead of being silently dropped, so a caller can print a note.

Known limitations of the line-based scanner (acceptable for this repo's
real config files, which never contain multi-line strings and always put
a key's own comment directly above it): a line that merely *looks* like a
`[section]` header (matching `_HEADER_RE`) but actually appears inside a
multi-line (triple-quoted) TOML string would be misidentified as a table
boundary; and a comment block that documents a whole section rather than
the key directly below it gets attributed to that first key, since the
scanner only ever looks at the contiguous `#` run immediately above a
key's line.
"""

from __future__ import annotations

import re
import tomllib

_HEADER_RE = re.compile(r"^\[\s*([A-Za-z_][A-Za-z0-9_]*)\s*\]\s*(#.*)?$")


def _split_lines(text: str) -> tuple[list[str], bool]:
    """Split `text` into real content lines (no line separators), plus
    whether it ended with a trailing newline. Round-trips exactly via
    `_join_lines` -- this is what lets untouched text come back
    byte-identical."""
    if text == "":
        return [], False
    lines = text.split("\n")
    trailing_newline = lines[-1] == ""
    if trailing_newline:
        lines = lines[:-1]
    return lines, trailing_newline


def _join_lines(lines: list[str], trailing_newline: bool) -> str:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    return text


def _table_spans(lines: list[str]) -> dict[str, tuple[int, int, int]]:
    """Map table name -> (header_index, content_start, content_end) for
    every top-level `[name]` header line in `lines`. `content_end` is
    exclusive: the index of the next header line, or len(lines)."""
    headers = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _HEADER_RE.match(line))]
    spans: dict[str, tuple[int, int, int]] = {}
    for idx, (header_idx, name) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        spans[name] = (header_idx, header_idx + 1, end)
    return spans


def _trailing_blank_count(lines: list[str], start: int, end: int) -> int:
    count = 0
    i = end - 1
    while i >= start and lines[i] == "":
        count += 1
        i -= 1
    return count


def _extract_key_chunk(lines: list[str], start: int, end: int, key: str) -> list[str] | None:
    """Find `key`'s `key = value` line within lines[start:end], plus the
    contiguous run of '#' comment lines immediately above it (stopping at
    the first non-comment line, e.g. the table header or a blank line, or
    the previous key). Returned in file order (comments, then the key
    line); None if the key's line can't be found."""
    key_re = re.compile(rf"^{re.escape(key)}\s*=")
    for i in range(start, end):
        if key_re.match(lines[i]):
            comment_start = i
            j = i - 1
            while j >= start and lines[j].startswith("#"):
                comment_start = j
                j -= 1
            return lines[comment_start : i + 1]
    return None


def sync_missing_keys(
    existing_text: str, template_text: str
) -> tuple[str, list[str], list[str]]:
    """Return (new_text, additions, skipped): `new_text` is `existing_text`
    with every template key missing from it appended (with its template
    default value and comment) to the end of the right table's block --
    or, if the whole table is missing, the template's entire block for it
    appended at EOF. `additions` lists each addition as "table.key", in
    template order. `skipped` lists "table.key" for a template key that is
    missing from the existing config but whose value is itself a nested
    table -- unsupported by this line-splicing merge -- so the caller can
    print a note instead of the key silently never being added. Returns
    `(existing_text, [], skipped)` unchanged (byte-identical) when nothing
    can be added.
    """
    existing_data = tomllib.loads(existing_text)
    template_data = tomllib.loads(template_text)

    per_table_missing: dict[str, list[str]] = {}
    missing_tables: list[str] = []
    skipped: list[str] = []

    for table_name, template_table in template_data.items():
        if not isinstance(template_table, dict):
            continue  # not a table; nothing in this schema hits this
        if table_name not in existing_data:
            missing_tables.append(table_name)
            continue
        existing_table = existing_data[table_name]
        if not isinstance(existing_table, dict):
            continue
        missing_keys = []
        for key, value in template_table.items():
            if key in existing_table:
                continue
            if isinstance(value, dict):
                skipped.append(f"{table_name}.{key}")
                continue
            missing_keys.append(key)
        if missing_keys:
            per_table_missing[table_name] = missing_keys

    if not per_table_missing and not missing_tables:
        return existing_text, [], skipped

    existing_lines, trailing_newline = _split_lines(existing_text)
    template_lines, _ = _split_lines(template_text)
    template_spans = _table_spans(template_lines)
    existing_spans = _table_spans(existing_lines)

    additions: list[str] = []

    # Add missing keys to tables that already exist, bottom of the file
    # first: an insertion only shifts line indices for content *after* it,
    # so working from the last table up keeps every not-yet-processed
    # table's already-recorded span valid.
    for table_name in sorted(
        per_table_missing, key=lambda t: existing_spans[t][0], reverse=True
    ):
        _, t_start, t_end = template_spans[table_name]
        chunk: list[str] = []
        for key in per_table_missing[table_name]:
            found = _extract_key_chunk(template_lines, t_start, t_end, key)
            if found is None:
                continue  # template couldn't be matched textually; skip defensively
            chunk.extend(found)
            additions.append(f"{table_name}.{key}")
        if not chunk:
            continue
        _, e_start, e_end = existing_spans[table_name]
        blanks = _trailing_blank_count(existing_lines, e_start, e_end)
        insert_at = e_end - blanks
        existing_lines[insert_at:insert_at] = chunk

    # Append whole tables missing from the existing file at EOF, in
    # template order, each preceded by exactly one blank-line separator
    # (added only if the file doesn't already end with one -- no existing
    # line is ever removed to make room for it).
    for table_name in missing_tables:
        template_table = template_data[table_name]
        h_idx, t_start, t_end = template_spans[table_name]
        content = template_lines[t_start:t_end]
        trailing = _trailing_blank_count(template_lines, t_start, t_end)
        if trailing:
            content = content[: len(content) - trailing]
        block = [template_lines[h_idx], *content]

        if existing_lines and existing_lines[-1] != "":
            existing_lines.append("")
        existing_lines.extend(block)

        for key, value in template_table.items():
            if isinstance(value, dict):
                skipped.append(f"{table_name}.{key}")
                continue
            additions.append(f"{table_name}.{key}")

    new_text = _join_lines(existing_lines, True if missing_tables else trailing_newline)
    return new_text, additions, skipped
