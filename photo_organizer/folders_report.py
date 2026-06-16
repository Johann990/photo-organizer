"""
folders_report.py — static HTML report for "no-event" source folders.

`plan` surfaces source folders whose name is just a date / serial / camera-dump
(e.g. ``20050814 蒙古\\0808``, ``100EOS5D``) — folders that were never given a
real event/location label. In a 15-year library these run into the hundreds, so
the terminal preview only shows the top 30. This module writes the COMPLETE list
to a self-contained HTML file the user can open anytime.

These dump folders are naturally hierarchical: many camera-dump subfolders
(``0807``, ``0808``, ``0809`` …) sit under one real event ancestor
(``20050814 蒙古``). The report groups by that immediate parent so a whole
event's dumps collapse into one expandable row.

Self-contained: inline CSS + vanilla JS, zero dependencies, no server. Native
``<details>``/``<summary>`` drive expand/collapse; a little JS adds sort and
expand-all / collapse-all. Read-only — it records nothing and touches no files.
"""

from __future__ import annotations

import html
from pathlib import Path


def _build_groups(
    no_event_src: dict[str, int],
) -> list[tuple[str, str, list[tuple[str, int]]]]:
    """Group dump folders by their immediate parent (the event ancestor).

    Returns a list of ``(group_label, group_path, children)`` where ``children``
    is a list of ``(child_name, file_count)``, sorted by total files desc.
    """
    groups: dict[str, list[tuple[str, int]]] = {}
    for folder, n in no_event_src.items():
        p = Path(folder)
        parent = str(p.parent)
        groups.setdefault(parent, []).append((p.name, n))

    out: list[tuple[str, str, list[tuple[str, int]]]] = []
    for parent, children in groups.items():
        children.sort(key=lambda kv: -kv[1])
        label = Path(parent).name or parent  # drive root → show full path
        out.append((label, parent, children))

    # Default order: groups with the most files first.
    out.sort(key=lambda g: -sum(n for _, n in g[2]))
    return out


def write_no_event_report(no_event_src: dict[str, int], out_path: Path) -> Path:
    """Render the no-event folder list to a self-contained HTML file.

    Returns the path written.
    """
    groups = _build_groups(no_event_src)
    n_folders = len(no_event_src)
    n_files = sum(no_event_src.values())
    n_groups = len(groups)

    rows: list[str] = []
    for gi, (label, gpath, children) in enumerate(groups):
        g_files = sum(n for _, n in children)
        g_subs = len(children)
        child_rows = "".join(
            f'<div class="child"><span class="cname">{html.escape(name)}</span>'
            f'<span class="cfiles">{n:,}</span></div>'
            for name, n in children
        )
        rows.append(
            f'<details class="group" data-files="{g_files}" '
            f'data-subs="{g_subs}" data-name="{html.escape(label, quote=True)}" '
            f'data-idx="{gi}">'
            f"<summary>"
            f'<span class="twisty"></span>'
            f'<span class="glabel">{html.escape(label)}</span>'
            f'<span class="gpath">{html.escape(gpath)}</span>'
            f'<span class="badge subs">{g_subs} 夾</span>'
            f'<span class="badge files">{g_files:,} files</span>'
            f"</summary>"
            f'<div class="children">{child_rows}</div>'
            f"</details>"
        )

    body = "\n".join(rows)

    doc = _TEMPLATE.format(
        n_folders=f"{n_folders:,}",
        n_files=f"{n_files:,}",
        n_groups=f"{n_groups:,}",
        body=body,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>No-event source folders</title>
<style>
  :root {{
    --bg: #1a1a1a; --fg: #e8e8e8; --dim: #9a9a9a; --accent: #f0c040;
    --row: #242424; --row2: #2c2c2c; --border: #383838; --child: #1e1e1e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif; font-size: 14px;
  }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  h1 .warn {{ color: var(--accent); }}
  .sub {{ color: var(--dim); margin-bottom: 16px; }}
  .controls {{ display: flex; gap: 8px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }}
  button, select {{
    background: var(--row2); color: var(--fg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer;
  }}
  button:hover {{ border-color: var(--accent); }}
  label {{ color: var(--dim); font-size: 13px; }}
  #tree {{ border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  details.group {{ border-bottom: 1px solid var(--border); }}
  details.group:last-child {{ border-bottom: none; }}
  summary {{
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    cursor: pointer; background: var(--row); list-style: none; user-select: none;
  }}
  summary::-webkit-details-marker {{ display: none; }}
  summary:hover {{ background: var(--row2); }}
  .twisty {{ width: 10px; color: var(--dim); transition: transform .12s; }}
  .twisty::before {{ content: "\\25B6"; }}
  details[open] .twisty {{ transform: rotate(90deg); }}
  .glabel {{ font-weight: 600; color: var(--accent); }}
  .gpath {{ color: var(--dim); font-size: 12px; flex: 1;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .badge {{ font-size: 12px; padding: 2px 8px; border-radius: 10px;
            background: var(--row2); color: var(--dim); white-space: nowrap; }}
  .badge.files {{ color: var(--fg); }}
  .children {{ background: var(--child); padding: 4px 0; }}
  .child {{ display: flex; padding: 5px 14px 5px 40px; gap: 10px; }}
  .child:hover {{ background: var(--row); }}
  .cname {{ flex: 1; }}
  .cfiles {{ color: var(--dim); }}
</style>
</head>
<body>
  <h1><span class="warn">&#9888;</span> No-event source folders
      <span style="color:var(--dim);font-weight:400">(date / serial / camera-dump names)</span></h1>
  <div class="sub">
    {n_folders} folders &middot; {n_files} files &middot; grouped into {n_groups} events.
    These source folders have no event/location label, so the organised target
    folder won&rsquo;t either &mdash; revisit and rename them later if you wish.
  </div>
  <div class="controls">
    <button id="expand">Expand all</button>
    <button id="collapse">Collapse all</button>
    <label for="sort">Sort by</label>
    <select id="sort">
      <option value="files">Files (most first)</option>
      <option value="subs">Subfolders (most first)</option>
      <option value="name">Name (A&rarr;Z)</option>
    </select>
  </div>
  <div id="tree">
{body}
  </div>
<script>
  const tree = document.getElementById('tree');
  const groups = () => Array.from(tree.querySelectorAll('details.group'));
  document.getElementById('expand').onclick = () => groups().forEach(g => g.open = true);
  document.getElementById('collapse').onclick = () => groups().forEach(g => g.open = false);
  document.getElementById('sort').onchange = (e) => {{
    const key = e.target.value;
    const sorted = groups().sort((a, b) => {{
      if (key === 'name')
        return a.dataset.name.localeCompare(b.dataset.name, 'zh-Hant');
      return (+b.dataset[key]) - (+a.dataset[key]);
    }});
    sorted.forEach(g => tree.appendChild(g));
  }};
</script>
</body>
</html>
"""
