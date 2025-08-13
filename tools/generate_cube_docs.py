#!/usr/bin/env python3
"""
Generate end-user documentation for all cubes in a TM1 Git repo checkout.

Features:
- Output formats: Markdown, Confluence wiki markup, or HTML
- Auto-maps upstream TI processes that write to each cube
- Includes Business Rules: link and optional embedded rules content
- Optional per-cube pages

Usage examples:
  python3 generate_cube_docs.py --format md --embed-rules --per-cube
  python3 generate_cube_docs.py --format confluence
  python3 generate_cube_docs.py --format html --max-rule-lines 300
"""

import argparse
import json
import re
import html
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any

# ---------- Configuration & CLI ----------

def get_args():
    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", default=".", help="Path to repo root containing cubes/, processes/, dimensions/")
    p.add_argument("--format", choices=["md", "confluence", "html"], default="md", help="Output format")
    p.add_argument("--embed-rules", action="store_true", help="Embed rules code under each cube")
    p.add_argument("--max-rule-lines", type=int, default=200, help="Max number of rule lines to embed (0 = no limit)")
    p.add_argument("--per-cube", action="store_true", help="Also write one file per cube under docs/cubes/")
    p.add_argument("--dev-branch-name", default="Dev", help="Branch label to print in the doc header")
    return p.parse_args()

# ---------- IO helpers ----------

def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

def read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(read_text(path) or "{}")
    except Exception:
        return {}

def walk_for_code_links(obj: Any) -> List[str]:
    """Find @Code.link or @Code.links entries recursively (TM1 Git convention)."""
    found = []
    def rec(x):
        if isinstance(x, dict):
            if "@Code.link" in x and isinstance(x["@Code.link"], str):
                found.append(x["@Code.link"])
            if "@Code.links" in x and isinstance(x["@Code.links"], list):
                for it in x["@Code.links"]:
                    if isinstance(it, dict) and "path" in it:
                        found.append(it["path"])
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
    rec(obj)
    return found

def extract_strings(obj: Any) -> List[str]:
    """Collect all string values (useful when TI code is embedded in JSON fields)."""
    out = []
    def rec(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
    rec(obj)
    return out

# ---------- TM1 parsing ----------

# Functions indicating writes to cubes (2nd arg is cube name)
TI_WRITE_FUNCS = [
    "CellPutN", "CellPutS", "CellIncrementN", "CellPutProportionalSpread",
    "CellPutDirectN", "CellPutDirectS"
]
# Regex to capture: FuncName( value , "CubeName" , ...
WRITE_REGEX = re.compile(
    r'\b(?:' + "|".join(map(re.escape, TI_WRITE_FUNCS)) + r')\s*\(\s*[^,]+,\s*"([^"]+)"',
    re.IGNORECASE
)

def find_cube_writes_in_code(code: str) -> List[str]:
    return [m.group(1) for m in WRITE_REGEX.finditer(code or "")]

def load_process_code(repo_root: Path, proc_json_path: Path) -> List[str]:
    """Return list of TI code blobs for a process (linked file(s) + embedded strings)."""
    blobs = []
    obj = read_json(proc_json_path)
    # Linked code files
    for rel in walk_for_code_links(obj):
        p = (repo_root / rel).resolve()
        if p.exists():
            t = read_text(p)
            if t:
                blobs.append(t)
    # Embedded snippets (PrologProcedure, MetadataProcedure, etc) as strings
    for s in extract_strings(obj):
        if any(fn.lower() in s.lower() for fn in ["CellPut", "ProportionalSpread", "ViewConstruct", "ExecuteProcess("]):
            blobs.append(s)
    return blobs

def try_rules_link(obj: Dict[str, Any]) -> Optional[str]:
    links = walk_for_code_links(obj)
    # Prefer .rules files
    rules = [l for l in links if l.lower().endswith(".rules")]
    return rules[0] if rules else (links[0] if links else None)

def extract_cube_core(cube_json: Dict[str, Any], fallback_name: str) -> Tuple[str, Optional[str], List[str]]:
    """Return (name, description, dimensions[])"""
    name = None
    desc = None
    dims: List[str] = []

    def rec(d):
        nonlocal name, desc, dims
        if not isinstance(d, dict):
            return
        if name is None:
            for k in ("Name", "name"):
                if k in d and isinstance(d[k], str):
                    name = d[k]
                    break
        if desc is None:
            for k in ("Description", "description", "Caption", "caption", "Label", "label"):
                if k in d and isinstance(d[k], str) and d[k].strip():
                    desc = d[k].strip()
                    break
        for k in ("Dimensions", "dimensions", "Dimension", "dimension", "OrderedDimensions"):
            if k in d and isinstance(d[k], list) and not dims:
                out = []
                for it in d[k]:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict):
                        out.append(it.get("Name") or it.get("name") or it.get("Id") or it.get("id") or json.dumps(it))
                if out:
                    dims = out
        for v in d.values():
            if isinstance(v, dict):
                rec(v)
            elif isinstance(v, list):
                for i in v:
                    if isinstance(i, dict):
                        rec(i)

    if isinstance(cube_json.get("value"), list):
        for it in cube_json["value"]:
            if isinstance(it, dict):
                rec(it)
    else:
        rec(cube_json)

    return (name or fallback_name, desc, dims)

# ---------- Renderers ----------

def safe(text: str) -> str:
    return text or ""

def render_md_header(branch_label: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"# BrevillePlay – Cube User Documentation ({branch_label})\n\nGenerated: {now}\n\nThis document summarizes all cubes in the branch, with dimensionality, Business Rules, and upstream TI dependencies (cell-writes).\n\n---\n\n"

def render_md_cube(ci, deps: List[str], rules_link: Optional[str], rules_preview: Optional[str]) -> str:
    dims_md = "\n".join(f"- {d}" for d in (ci['dimensions'] or [])) or "_(not specified in source)_"
    rules_line = f"[{rules_link}]({rules_link})" if rules_link else "_(none found)_"
    rules_block = ""
    if rules_preview:
        rules_block = f"\n<details>\n<summary>Business Rules (inline preview)</summary>\n\n```rules\n{rules_preview}\n```\n</details>\n"
    dep_md = "\n".join(f"- {p}" for p in deps) or "_(no upstream TI writers detected)_"
    perma = re.sub(r'[^a-z0-9]+','-', ci['name'].lower()).strip('-')
    return (
f"## {ci['name']} <a id=\"{perma}\"></a>\n\n"
f"**What is this cube?**\n\n{safe(ci['description']) or '_(no description in source)_'}\n\n"
f"**Dimensionality (order matters):**\n{dims_md}\n\n"
f"**Business Rules:**  \nRules file: {rules_line}\n{rules_block}\n"
f"**Upstream TI processes writing to this cube:**\n{dep_md}\n\n"
f"**Guidance for business users:**\n"
f"- Who uses it: _(FP&A / Supply / …)_\n"
f"- Primary reports/dashboards: _(list)_\n"
f"- Standard views: `Default`, `Monthly`, `YTD` _(adjust)_\n"
f"- Writeback/planning: _(Yes/No; note security groups)_\n\n"
"---\n\n"
)

def render_conf_header(branch_label: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"h1. BrevillePlay – Cube User Documentation ({branch_label})\n\n*Generated:* {now}\n\nThis page summarizes all cubes with dimensionality, *Business Rules*, and upstream TI dependencies (cell-writes).\n\n----\n\n"

def render_conf_cube(ci, deps: List[str], rules_link: Optional[str], rules_preview: Optional[str]) -> str:
    dims = "\n".join(f"* {d}" for d in (ci['dimensions'] or [])) or "_(not specified in source)_"
    rules_line = f"[Rules file|{rules_link}]" if rules_link else "_(none found)_"
    rules_block = ""
    if rules_preview:
        # Confluence code block macro
        rules_block = f"\n{{expand:Business Rules (inline preview)}}\n{{code:language=none|collapse=false}}\n{rules_preview}\n{{code}}\n{{expand}}\n"
    deps_conf = "\n".join(f"* {p}" for p in deps) or "_(no upstream TI writers detected)_"
    return (
f"h2. {ci['name']}\n\n"
f"*What is this cube?*\n\n{safe(ci['description']) or '_(no description in source)_'}\n\n"
f"*Dimensionality (order matters):*\n{dims}\n\n"
f"*Business Rules:* {rules_line}\n{rules_block}"
f"*Upstream TI processes writing to this cube:*\n{deps_conf}\n\n"
f"*Guidance for business users:*\n"
f"* Who uses it: _(FP&A / Supply / …)_\n"
f"* Primary reports/dashboards: _(list)_\n"
f"* Standard views: `Default`, `Monthly`, `YTD` _(adjust)_\n"
f"* Writeback/planning: _(Yes/No; note security groups)_\n\n"
"----\n\n"
)

def render_html_header(branch_label: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<title>BrevillePlay – Cube Docs ({html.escape(branch_label)})</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5;margin:2rem;}}
h1{{font-size:1.8rem;margin:0 0 1rem}}
h2{{font-size:1.4rem;margin:2rem 0 0.5rem}}
code, pre{{font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;}}
.card{{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:1rem;margin:1rem 0;box-shadow:0 1px 2px rgba(0,0,0,.05)}}
.small{{color:#6b7280}}
summary{{cursor:pointer;font-weight:600}}
ul{{margin:0.25rem 0 0.75rem 1.25rem}}
hr{{border:0;border-top:1px solid #e5e7eb;margin:2rem 0}}
.badge{{display:inline-block;border:1px solid #e5e7eb;border-radius:999px;padding:.15rem .6rem;font-size:.8rem}}
</style>
</head>
<body>
<h1>BrevillePlay – Cube User Documentation ({html.escape(branch_label)})</h1>
<p class="small">Generated: {html.escape(now)}</p>
<p>This document summarizes all cubes with dimensionality, <strong>Business Rules</strong>, and upstream TI dependencies (cell-writes).</p>
<hr/>
"""

def render_html_footer() -> str:
    return "</body></html>"

def render_html_cube(ci, deps: List[str], rules_link: Optional[str], rules_preview: Optional[str]) -> str:
    dims = "".join(f"<li>{html.escape(d)}</li>" for d in (ci['dimensions'] or [])) or "<i>(not specified in source)</i>"
    deps_li = "".join(f"<li>{html.escape(p)}</li>" for p in deps) or "<i>(no upstream TI writers detected)</i>"
    rules_line = f'<a href="{html.escape(rules_link)}">{html.escape(rules_link)}</a>' if rules_link else "<i>(none found)</i>"
    rules_block = ""
    if rules_preview:
        rules_block = (
            "<details><summary>Business Rules (inline preview)</summary>"
            "<pre><code>" + html.escape(rules_preview) + "</code></pre>"
            "</details>"
        )
    return f"""
<div class="card">
  <h2>{html.escape(ci['name'])}</h2>
  <p><span class="badge">What is this cube?</span></p>
  <p>{html.escape(ci['description'] or "(no description in source)")}</p>

  <p><span class="badge">Dimensionality (order matters)</span></p>
  <ul>{dims}</ul>

  <p><span class="badge">Business Rules</span> — Rules file: {rules_line}</p>
  {rules_block}

  <p><span class="badge">Upstream TI processes writing to this cube</span></p>
  <ul>{deps_li}</ul>

  <p><span class="badge">Guidance for business users</span></p>
  <ul>
    <li>Who uses it: (FP&amp;A / Supply / …)</li>
    <li>Primary reports/dashboards: (list)</li>
    <li>Standard views: <code>Default</code>, <code>Monthly</code>, <code>YTD</code> (adjust)</li>
    <li>Writeback/planning: (Yes/No; note security groups)</li>
  </ul>
</div>
"""

# ---------- Pipeline ----------

def main():
    args = get_args()
    root = Path(args.repo_root).resolve()
    cubes_dir = root / "cubes"
    procs_dir = root / "processes"
    out_basename = {
        "md": "Cubes_User_Documentation.md",
        "confluence": "Cubes_User_Documentation.confluence.txt",
        "html": "Cubes_User_Documentation.html",
    }[args.format]
    out_master = root / out_basename
    per_cube_dir = root / "docs" / "cubes"
    if args.per_cube:
        per_cube_dir.mkdir(parents=True, exist_ok=True)

    # 1) Parse cubes
    cube_files = sorted(cubes_dir.glob("*.json"))
    cubes = []
    for f in cube_files:
        cj = read_json(f)
        name, desc, dims = extract_cube_core(cj, f.stem)
        rules_rel = try_rules_link(cj)
        rules_abs = (root / rules_rel).resolve() if rules_rel else None
        rules_exists = rules_abs.exists() if rules_rel else False
        cubes.append({
            "name": name,
            "description": desc,
            "dimensions": dims,
            "rules_rel": rules_rel if rules_exists else None
        })

    # 2) Build dependency map: cube -> [processes that write to it]
    cube_deps: Dict[str, List[str]] = {c["name"]: [] for c in cubes}
    proc_jsons = sorted(procs_dir.glob("*.json"))
    for pj in proc_jsons:
        code_blobs = load_process_code(root, pj)
        writers = set()
        for blob in code_blobs:
            for cube_name in find_cube_writes_in_code(blob):
                writers.add(cube_name)
        if writers:
            proc_name = pj.stem
            for c in cube_deps.keys():
                if c in writers:
                    cube_deps[c].append(proc_name)

    # 3) Prepare output
    if args.format == "md":
        buffer = [render_md_header(args.dev_branch_name)]
        # TOC
        buffer.append("## Cubes\n")
        for c in cubes:
            anchor = re.sub(r'[^a-z0-9]+','-', c["name"].lower()).strip('-')
            buffer.append(f"- [{c['name']}](#{anchor})")
        buffer.append("\n---\n\n")
    elif args.format == "confluence":
        buffer = [render_conf_header(args.dev_branch_name)]
        # Confluence doesn’t need explicit anchors; skip TOC or let Confluence generate one
        buffer.append("*Cubes listed below in alphabetical order.*\n\n----\n\n")
    else:  # html
        buffer = [render_html_header(args.dev_branch_name)]
        # simple index
        buffer.append("<h2>Index</h2><ul>")
        for c in cubes:
            anchor = re.sub(r'[^a-z0-9]+','-', c["name"].lower()).strip('-')
            buffer.append(f'<li><a href="#{anchor}">{html.escape(c["name"])}</a></li>')
        buffer.append("</ul><hr/>\n")

    # 4) Render cubes
    for c in cubes:
        deps = sorted(set(cube_deps.get(c["name"], [])))
        rules_preview = None
        if args.embed_rules and c["rules_rel"]:
            rp = (root / c["rules_rel"]).resolve()
            txt = read_text(rp) or ""
            if args.max_rule_lines > 0:
                txt_lines = txt.splitlines()
                if len(txt_lines) > args.max_rule_lines:
                    txt = "\n".join(txt_lines[:args.max_rule_lines] + ["...", f"(truncated to {args.max_rule_lines} lines)"])
            rules_preview = txt

        if args.format == "md":
            buffer.append(render_md_cube(
                {"name": c["name"], "description": c["description"], "dimensions": c["dimensions"]},
                deps,
                c["rules_rel"],
                rules_preview
            ))
        elif args.format == "confluence":
            buffer.append(render_conf_cube(
                {"name": c["name"], "description": c["description"], "dimensions": c["dimensions"]},
                deps,
                c["rules_rel"],
                rules_preview
            ))
        else:  # html
            # Add id anchors to each section
            anchor = re.sub(r'[^a-z0-9]+','-', c["name"].lower()).strip('-')
            buffer.append(f'<a id="{anchor}"></a>')
            buffer.append(render_html_cube(
                {"name": c["name"], "description": c["description"], "dimensions": c["dimensions"]},
                deps,
                c["rules_rel"],
                rules_preview
            ))

        # Per-cube file
        if args.per_cube:
            if args.format == "md":
                per = render_md_cube(c, deps, c["rules_rel"], rules_preview)
                (per_cube_dir / f"{c['name']}.md").write_text(per, encoding="utf-8")
            elif args.format == "confluence":
                per = render_conf_cube(c, deps, c["rules_rel"], rules_preview)
                (per_cube_dir / f"{c['name']}.confluence.txt").write_text(per, encoding="utf-8")
            else:
                per = f'<a id="{re.sub(r"[^a-z0-9]+","-", c["name"].lower()).strip("-")}"></a>' + \
                      render_html_cube(c, deps, c["rules_rel"], rules_preview)
                (per_cube_dir / f"{c['name']}.html").write_text(render_html_header(args.dev_branch_name)+per+render_html_footer(), encoding="utf-8")

    # 5) Write master
    if args.format == "html":
        buffer.append(render_html_footer())
    out_master.write_text("".join(buffer), encoding="utf-8")

    print(f"Wrote {out_master}")
    if args.per_cube:
        print(f"Per-cube pages in {per_cube_dir}")

if __name__ == "__main__":
    main()

