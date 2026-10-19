"""HTML rendering for the local read-only dashboard (batch 3a).

Every string that came from the database (guestbook text, display names,
node names) MUST be passed through `esc()` before landing in a template --
nothing here trusts its input. No JavaScript; the only non-text visuals are
tiny inline SVG bar charts. No external assets: all CSS is inlined in
`_HEAD_STYLE` below, shared by every page via `layout()`.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable

_NAV_ITEMS: tuple[tuple[str, str], ...] = (
    ("/", "Right now"),
    ("/health", "Health"),
    ("/traffic", "Traffic"),
    ("/guestbook", "Guestbook"),
    ("/records", "Records"),
    ("/digest", "Digest"),
    ("/nodes", "Nodes"),
    ("/map", "Map"),
    ("/topology", "Topology"),
    ("/propagation", "Propagation"),
)

# Brand-neutral palette: readable in both `prefers-color-scheme` states.
# Light is the base (`:root`); dark values only override under the media
# query, per Meshgarden's own light-first convention elsewhere (README).
_HEAD_STYLE = """
:root {
  --bg: #f7f7f5; --fg: #1a1a1a; --muted: #5f6368; --card: #ffffff;
  --border: #dfe1e5; --accent: #2a6f4b; --accent-fg: #ffffff;
  --bar: #2a6f4b; --bar2: #9db8ab; --warn: #a15c00; --crit: #b3261e;
  --link: #1a5cad;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14161a; --fg: #e8e8e6; --muted: #a0a4ab; --card: #1d2025;
    --border: #33373d; --accent: #6fbf94; --accent-fg: #0b1210;
    --bar: #6fbf94; --bar2: #3d5c4d; --warn: #e0a53a; --crit: #ff6b60;
    --link: #7ab2f0;
  }
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--fg); margin: 0; padding: 0;
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
  Helvetica, Arial, sans-serif; }
a { color: var(--link); }
header.top { border-bottom: 1px solid var(--border); padding: 10px 16px;
  display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline; }
header.top .title { font-weight: 600; margin-right: 8px; }
nav.top a { text-decoration: none; color: var(--muted); padding: 2px 0; }
nav.top a.active, nav.top a:hover { color: var(--fg); border-bottom: 2px solid var(--accent); }
main { padding: 16px; max-width: 960px; margin: 0 auto; }
h1 { font-size: 1.25rem; margin: 0 0 12px; }
h2 { font-size: 1.05rem; margin: 20px 0 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 10px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; }
.card .label { color: var(--muted); font-size: 0.78rem; text-transform: uppercase;
  letter-spacing: .03em; }
.card .value { font-size: 1.3rem; font-weight: 600; margin-top: 2px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 16px; }
th, td { text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border);
  font-variant-numeric: tabular-nums; }
th { color: var(--muted); font-weight: 600; font-size: 0.82rem; }
.muted { color: var(--muted); }
.pill { display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 0.78rem; border: 1px solid var(--border); }
.pill.ok { color: var(--accent); border-color: var(--accent); }
.pill.warn { color: var(--warn); border-color: var(--warn); }
.pill.crit { color: var(--crit); border-color: var(--crit); }
.banner { background: var(--card); border: 1px solid var(--warn); color: var(--warn);
  border-radius: 8px; padding: 8px 12px; margin-bottom: 14px; }
.bars { display: block; }
.bars text { font-size: 8px; fill: var(--muted); }
footer { color: var(--muted); padding: 20px 16px 30px; font-size: 0.8rem; }
.pagination { margin: 10px 0; }
.pagination a { margin-right: 10px; }
@media (max-width: 480px) {
  main { padding: 12px; }
  table, thead, tbody, th, td, tr { display: block; }
  thead tr { display: none; }
  td { border: none; padding: 2px 8px; }
  tr { border-bottom: 1px solid var(--border); padding: 6px 0; }
}
""".strip()


def esc(value: object) -> str:
    """HTML-escapes anything that might have come from the database.
    None becomes "" (never the literal string "None")."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def pressure_pill(pressure: str | None) -> str:
    cls = {"ok": "ok", "warn": "warn", "critical": "crit"}.get(pressure or "", "")
    label = esc(pressure) if pressure else "unknown"
    return f'<span class="pill {cls}">{label}</span>'


def bool_pill(value: bool | None, *, true_label: str, false_label: str) -> str:
    if value is None:
        return '<span class="pill">unknown</span>'
    cls = "ok" if value else "crit"
    label = true_label if value else false_label
    return f'<span class="pill {cls}">{esc(label)}</span>'


def bool_pill_yn(value: bool | None) -> str:
    """Shorthand for the common `bool_pill(..., true_label="yes",
    false_label="no")` case."""
    return bool_pill(value, true_label="yes", false_label="no")


def stat_card(label: str, value: str) -> str:
    return (
        f'<div class="card"><div class="label">{esc(label)}</div>'
        f'<div class="value">{value}</div></div>'
    )


def na(value: object, fmt: str = "{}") -> str:
    """Renders `value` with `fmt`, or "n/a" if it is None."""
    return esc(fmt.format(value)) if value is not None else "n/a"


def svg_bar_chart(
    labels: Iterable[str],
    series: Iterable[tuple[str, list[float], str]],
    *,
    width: int = 720,
    height: int = 110,
) -> str:
    """Tiny inline SVG grouped-bar chart -- no JavaScript, no external
    assets. `series` is (name, values, css-color) tuples, all the same
    length as `labels`. Purely decorative/summary; exact pixel values are
    not asserted by tests."""
    labels = list(labels)
    series = list(series)
    n = len(labels)
    if n == 0 or not series:
        return '<div class="muted">no data</div>'
    max_value = max((v for _, values, _ in series for v in values), default=0.0) or 1.0
    plot_h = height - 18
    group_w = width / n
    bar_w = max(1.0, (group_w - 4) / max(1, len(series)))

    parts = [
        f'<svg class="bars" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="24 hour activity">'
    ]
    for i, label in enumerate(labels):
        gx = i * group_w
        for j, (_, values, color) in enumerate(series):
            v = values[i] if i < len(values) else 0.0
            bar_h = (v / max_value) * plot_h
            x = gx + 2 + j * bar_w
            y = plot_h - bar_h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 1:.1f}" '
                f'height="{max(0.0, bar_h):.1f}" fill="{esc(color)}"><title>'
                f"{esc(label)}: {esc(round(v, 1))}</title></rect>"
            )
        if n <= 24 and i % 3 == 0:
            parts.append(f'<text x="{gx + 2:.1f}" y="{height - 4}">{esc(label)}</text>')
    parts.append("</svg>")
    legend = " &nbsp; ".join(
        f'<span style="color:{esc(color)}">&#9632;</span> {esc(name)}' for name, _, color in series
    )
    parts.append(f'<div class="muted">{legend}</div>')
    return "".join(parts)


def svg_sparkline(
    labels: Iterable[str],
    values: Iterable[float | None],
    *,
    color: str,
    width: int = 720,
    height: int = 50,
    unit_fmt: str = "{}",
) -> str:
    """Tiny inline SVG line sparkline for a 30-day trend (batch 3b node
    profile: packets/day, best SNR/day). `values[i]` may be None (no data
    that day); such days are simply skipped when building the polyline --
    no interpolation guess across a gap. No JavaScript, no external
    assets -- same convention as `svg_bar_chart`."""
    labels = list(labels)
    values = list(values)
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if not pts:
        return '<div class="muted">no data</div>'
    n = max(1, len(values) - 1)
    vmin = min(v for _, v in pts)
    vmax = max(v for _, v in pts)
    vrange = (vmax - vmin) or 1.0
    plot_h = height - 4

    def _xy(i: int, v: float) -> tuple[float, float]:
        x = (i / n) * width
        y = plot_h - ((v - vmin) / vrange) * plot_h + 2
        return x, y

    coords = [(i, *_xy(i, v)) for i, v in pts]
    poly = " ".join(f"{x:.1f},{y:.1f}" for _, x, y in coords)
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.6" fill="{esc(color)}"><title>'
        f"{esc(labels[i])}: {esc(unit_fmt.format(values[i]))}</title></circle>"
        for i, x, y in coords
    )
    return (
        f'<svg class="bars" viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="30 day trend"><polyline points="{poly}" fill="none" '
        f'stroke="{esc(color)}" stroke-width="1.5"/>{circles}</svg>'
    )


def _heat_color(frac: float) -> str:
    """0.0 -> muted amber (weak reception), 1.0 -> accent green (strong);
    a fixed linear RGB interpolation between two light-mode palette
    endpoints, computed here in Python since no JavaScript is allowed."""
    frac = max(0.0, min(1.0, frac))
    lo = (0xA1, 0x5C, 0x00)  # --warn
    hi = (0x2A, 0x6F, 0x4B)  # --accent
    r = round(lo[0] + (hi[0] - lo[0]) * frac)
    g = round(lo[1] + (hi[1] - lo[1]) * frac)
    b = round(lo[2] + (hi[2] - lo[2]) * frac)
    return f"#{r:02x}{g:02x}{b:02x}"


def svg_heat_strip(
    labels: Iterable[str],
    values: Iterable[float | None],
    *,
    width: int = 720,
    height: int = 28,
) -> str:
    """A single row of small rects, one per day, coloured by `values[i]`
    (an SNR reading): greener for a higher/better value, muted grey for a
    missing day (batch 3b `/propagation`). No JavaScript, no external
    assets."""
    labels = list(labels)
    values = list(values)
    n = len(labels)
    if n == 0:
        return '<div class="muted">no data</div>'
    known = [v for v in values if v is not None]
    vmin = min(known) if known else 0.0
    vmax = max(known) if known else 1.0
    vrange = (vmax - vmin) or 1.0
    cell_w = width / n
    parts = [
        f'<svg class="bars" viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        'role="img" aria-label="SNR heat strip">'
    ]
    for i, (label, v) in enumerate(zip(labels, values, strict=True)):
        x = i * cell_w
        if v is None:
            fill = "var(--border)"
            title = f"{esc(label)}: no data"
        else:
            fill = _heat_color((v - vmin) / vrange)
            title = f"{esc(label)}: {esc(round(v, 1))} dB"
        parts.append(
            f'<rect x="{x:.1f}" y="0" width="{max(1.0, cell_w - 1):.1f}" height="{height}" '
            f'fill="{fill}"><title>{title}</title></rect>'
        )
    parts.append("</svg>")
    return "".join(parts)


def layout(*, title: str, active: str, body: str) -> bytes:
    nav_links = []
    for href, label in _NAV_ITEMS:
        cls = ' class="active"' if href == active else ""
        nav_links.append(f'<a href="{href}"{cls}>{label}</a>')
    nav = "".join(nav_links)
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — Meshgarden</title>
<style>{_HEAD_STYLE}</style>
</head>
<body>
<header class="top">
  <span class="title">Meshgarden</span>
  <nav class="top">{nav}</nav>
</header>
<main>
{body}
</main>
<footer>Local, read-only dashboard. No write actions are served here.</footer>
</body>
</html>
"""
    return html_doc.encode("utf-8")


_MAIN_RE = re.compile(r"(<main>\n)(.*)(\n</main>)", re.DOTALL)


def mesh_switcher(path: str, selected: str, options: tuple[tuple[str, str], ...]) -> str:
    """The `?mesh=` selector. Plain links to the same path (other query
    parameters are dropped: a page number or filter from one mesh means
    nothing on the other). `options` is (code, label) pairs."""
    bits = []
    for code, label in options:
        if code == selected:
            bits.append(f"<b>{esc(label)}</b>")
        else:
            bits.append(f'<a href="{esc(path)}?mesh={esc(code)}">{esc(label)}</a>')
    return f'<p class="muted">Mesh: {" &middot; ".join(bits)}</p>'


def main_of(page: bytes) -> str:
    """The <main> contents of a page `layout` rendered; "" if absent."""
    m = _MAIN_RE.search(page.decode("utf-8"))
    return m.group(2) if m else ""


def with_main(page: bytes, main: str) -> bytes:
    """`page` with its <main> contents replaced. The page-body functions
    return finished documents, so this lets the mesh switcher and the
    stacked "both" view wrap them without touching every page."""
    # A function replacement: `main` is page HTML, not a regex template.
    swapped = _MAIN_RE.sub(lambda m: m.group(1) + main + m.group(3), page.decode("utf-8"), count=1)
    return swapped.encode("utf-8")


def error_page(status: int, message: str) -> bytes:
    body = f"<h1>{status}</h1><p>{esc(message)}</p>"
    return layout(title=f"{status}", active="", body=body)
