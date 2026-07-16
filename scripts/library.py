#!/usr/bin/env python3
"""
library.py — cross-project library inventory ("书架总览").

Reads ~/.reading-kit/registry.json and scans EVERY registered project:
  * vault/Books/<collection>/<title>/正文.md  → books on the shelf (+ page count)
  * .rag/<collection>/index.json             → chunk counts per collection/book
  * vault/10-Notes/<collection>/             → annotation note counts
  * staging/<slug>/{transcripts,verified}    → pages pending human adjudication
  * staging/_jobs/<id>/job.json              → recent import jobs + gate results
  * TCP probe on ragPort / ragPort+100       → answer server / import console up?

Per this repo's convention the script never imports its siblings; the
"pending page" rule (transcript exists, verified file doesn't) is kept as a
duplicate of import_server.py's, not an import.

Usage:
  python scripts/library.py                    # markdown to stdout (chat-friendly)
  python scripts/library.py --json             # machine-readable
  python scripts/library.py --html out.html    # self-contained dashboard page
  python scripts/library.py --project <slug>   # limit to one project
  python scripts/library.py --no-probe         # skip server port probes
"""

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path

REGISTRY = Path.home() / ".reading-kit" / "registry.json"
CONSOLE_PORT_OFFSET = 100  # import_server.py convention: ragPort + 100
RECENT_JOBS = 8


# ---------- scanning ----------

def load_registry(path):
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("projects", []))


def port_open(port, timeout=0.3):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def count_pages(note_path):
    """Page anchors in a reflowed 正文.md (reflow.py emits data-p=\"N\" spans)."""
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return len(re.findall(r'data-p="\d+"', text))


def load_chunk_map(rag_dir):
    """Global file→chunk-count map across ALL .rag/*/index.json (collections may
    be per-book partitions or a legacy flat 'default' index — treat uniformly)."""
    counts = {}
    if not rag_dir.is_dir():
        return counts
    for index in rag_dir.glob("*/index.json"):
        try:
            chunks = json.loads(index.read_text(encoding="utf-8")).get("chunks", [])
        except (OSError, json.JSONDecodeError):
            continue
        for ch in chunks:
            f = str(ch.get("file", "")).replace("\\", "/")
            counts[f] = counts.get(f, 0) + 1
    return counts


def chunks_for_prefix(chunk_map, prefix):
    return sum(n for f, n in chunk_map.items() if f.startswith(prefix))


def scan_staging(staging):
    """Book-shaped staging dirs (have transcripts/ or verified/ or paddle/) with
    pending-adjudication counts. jury_*/ocr/_jobs etc. don't match and are skipped."""
    out = []
    if not staging.is_dir():
        return out
    for d in sorted(staging.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        tdir, vdir, pdir = d / "transcripts", d / "verified", d / "paddle"
        if not (tdir.is_dir() or vdir.is_dir() or pdir.is_dir()):
            continue
        pages = set()
        if tdir.is_dir():
            for f in tdir.glob("pg-*.md"):
                m = re.match(r"(pg-\d+)\.", f.name)
                if m:
                    pages.add(m.group(1))
        verified = {f.stem for f in vdir.glob("pg-*.md")} if vdir.is_dir() else set()
        out.append({
            "slug": d.name,
            "transcribed": len(pages),
            "verified": len(verified),
            "pending": sorted(pages - verified),
        })
    return out


def scan_jobs(staging):
    jobs = []
    jobs_dir = staging / "_jobs"
    if not jobs_dir.is_dir():
        return jobs
    for jf in jobs_dir.glob("*/job.json"):
        try:
            j = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        gate = j.get("gate") or {}
        jobs.append({
            "id": j.get("id", jf.parent.name),
            "created": j.get("created", ""),
            "title": (j.get("params") or {}).get("title", ""),
            "collection": (j.get("params") or {}).get("collection", ""),
            "mode": (j.get("params") or {}).get("mode", ""),
            "state": j.get("state", "?"),
            "ratio": gate.get("ratio"),
            "verified": gate.get("verified"),
            "total": gate.get("total"),
        })
    jobs.sort(key=lambda x: x["created"], reverse=True)
    return jobs[:RECENT_JOBS]


def scan_project(proj, probe=True):
    root = Path(proj["path"])
    vault, rag, staging = root / "vault", root / ".rag", root / "staging"
    books_root = vault / "Books"
    notes_root = vault / "10-Notes"
    rag_port = proj.get("ragPort")

    chunk_map = load_chunk_map(rag)
    collections = []
    if books_root.is_dir():
        for cdir in sorted(books_root.iterdir()):
            if not cdir.is_dir():
                continue
            ndir = notes_root / cdir.name
            notes = len(list(ndir.rglob("*.md"))) if ndir.is_dir() else 0
            books = []
            subdirs = [d for d in sorted(cdir.iterdir()) if d.is_dir()]
            for bdir in subdirs:
                note = bdir / "正文.md"
                books.append({
                    "title": bdir.name,
                    "has_note": note.exists(),
                    "pages": count_pages(note) if note.exists() else 0,
                    "parts": None,
                    "chunks": chunks_for_prefix(chunk_map, f"Books/{cdir.name}/{bdir.name}/"),
                })
            if not subdirs:
                # legacy flat shape: Books/<X>/卷N.md — the folder IS one book
                parts = sorted(cdir.glob("*.md"))
                if parts:
                    books.append({
                        "title": cdir.name,
                        "has_note": True,
                        "pages": sum(count_pages(f) for f in parts),
                        "parts": len(parts),
                        "chunks": chunks_for_prefix(chunk_map, f"Books/{cdir.name}/"),
                    })
            collections.append({
                "name": cdir.name,
                "books": books,
                "chunks_total": chunks_for_prefix(chunk_map, f"Books/{cdir.name}/"),
                "notes": notes,
            })

    staging_items = scan_staging(staging)
    shelved = {b["title"] for c in collections for b in c["books"] if b["has_note"]}
    for s in staging_items:
        s["on_shelf"] = s["slug"] in shelved

    return {
        "slug": proj["slug"],
        "path": str(root),
        "exists": root.is_dir(),
        "notes_total": len(list(notes_root.rglob("*.md"))) if notes_root.is_dir() else 0,
        "ragPort": rag_port,
        "consolePort": (rag_port + CONSOLE_PORT_OFFSET) if rag_port else None,
        "server_up": port_open(rag_port) if (probe and rag_port) else None,
        "console_up": port_open(rag_port + CONSOLE_PORT_OFFSET) if (probe and rag_port) else None,
        "collections": collections,
        "staging": staging_items,
        "jobs": scan_jobs(staging),
    }


# ---------- rendering ----------

def status_of(project):
    """Attention items for a project, worst first."""
    issues = []
    for s in project["staging"]:
        if s["pending"] and not s["on_shelf"]:
            issues.append(f"{s['slug']}: {len(s['pending'])} 页待精校")
    for j in project["jobs"]:
        if j["state"] in ("failed", "escalate") or (j["ratio"] is not None and j["ratio"] < 1 and j["state"] not in ("passed",)):
            issues.append(f"任务 {j['id']}({j['title']}) 状态 {j['state']}")
            break
    return issues


def emit_markdown(projects):
    lines = ["# 书架总览", ""]
    total_books = sum(len([b for b in c["books"] if b["has_note"]])
                      for p in projects for c in p["collections"])
    lines.append(f"共 {len(projects)} 个项目，{total_books} 本在架。"
                 f"（{datetime.now().strftime('%Y-%m-%d %H:%M')}）")
    for p in projects:
        up = {True: "🟢", False: "⚫", None: "·"}
        lines += ["", f"## {p['slug']} — `{p['path']}`",
                  f"AI 服务 {up[p['server_up']]} :{p['ragPort']} · "
                  f"导入控制台 {up[p['console_up']]} :{p['consolePort']} · "
                  f"笔记 {p['notes_total']} 条"]
        if not p["exists"]:
            lines.append("⚠️ 项目目录不存在（注册表指向失效路径）")
            continue
        for c in p["collections"]:
            lines.append(f"\n### 合集：{c['name']}（{c['chunks_total']} chunks · {c['notes']} 条笔记）")
            lines.append("| 书 | 页 | chunks | 状态 |")
            lines.append("|---|---|---|---|")
            for b in c["books"]:
                st = "✅ 在架" if b["has_note"] else "🚧 未完成"
                size = f"{b['parts']} 篇" if b["parts"] and not b["pages"] else b["pages"]
                lines.append(f"| {b['title']} | {size} | {b['chunks']} | {st} |")
        pend = [s for s in p["staging"] if s["pending"] and not s["on_shelf"]]
        if pend:
            lines.append("\n**待处理（staging）**")
            for s in pend:
                lines.append(f"- 🟡 {s['slug']}：{s['verified']}/{s['transcribed']} 已核验，"
                             f"{len(s['pending'])} 页待精校")
        if p["jobs"]:
            j = p["jobs"][0]
            ratio = f"，质量门 {j['verified']}/{j['total']}" if j["total"] else ""
            lines.append(f"\n最近任务：`{j['id']}` {j['title']} [{j['mode']}] → {j['state']}{ratio}")
    return "\n".join(lines)


HTML_HEAD = """<meta charset="utf-8">
<title>书架总览</title>
<style>
:root{--bg:#faf9f6;--fg:#1e1e1c;--muted:#6f6b60;--card:#ffffff;--line:#e5e1d8;
--ok:#2e7d32;--warn:#b26a00;--bad:#b3261e;--accent:#4a5c46;}
@media (prefers-color-scheme: dark){:root{--bg:#191917;--fg:#e8e6df;--muted:#98948a;
--card:#232320;--line:#3a3a35;--ok:#7cb87f;--warn:#e0a94f;--bad:#e57373;--accent:#a3b899;}}
:root[data-theme="dark"]{--bg:#191917;--fg:#e8e6df;--muted:#98948a;--card:#232320;
--line:#3a3a35;--ok:#7cb87f;--warn:#e0a94f;--bad:#e57373;--accent:#a3b899;}
:root[data-theme="light"]{--bg:#faf9f6;--fg:#1e1e1c;--muted:#6f6b60;--card:#ffffff;
--line:#e5e1d8;--ok:#2e7d32;--warn:#b26a00;--bad:#b3261e;--accent:#4a5c46;}
body{background:var(--bg);color:var(--fg);font:15px/1.65 "Source Han Serif SC",
"Noto Serif CJK SC",Georgia,serif;margin:0;padding:2rem 1rem;}
main{max-width:56rem;margin:0 auto;}
h1{font-size:1.5rem;margin:.2rem 0 .3rem;} .sub{color:var(--muted);margin:0 0 1.4rem;}
.proj{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:1rem 1.2rem;margin:0 0 1.2rem;}
.proj h2{font-size:1.15rem;margin:.1rem 0 .2rem;}
.meta{color:var(--muted);font-size:.85rem;margin:0 0 .6rem;word-break:break-all;}
.dot{display:inline-block;width:.6em;height:.6em;border-radius:50%;margin-right:.3em;}
.up{background:var(--ok);} .down{background:var(--muted);}
h3{font-size:1rem;margin:1rem 0 .3rem;color:var(--accent);}
.tblwrap{overflow-x:auto;}
table{border-collapse:collapse;width:100%;font-size:.9rem;}
th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid var(--line);}
th{color:var(--muted);font-weight:normal;} td.num{text-align:right;font-variant-numeric:tabular-nums;}
.badge{font-size:.78rem;padding:.1rem .5rem;border-radius:99px;border:1px solid var(--line);}
.b-ok{color:var(--ok);} .b-warn{color:var(--warn);} .b-bad{color:var(--bad);}
.alert{border-left:3px solid var(--warn);padding:.3rem .8rem;margin:.6rem 0;
background:color-mix(in srgb,var(--warn) 8%,transparent);border-radius:0 6px 6px 0;font-size:.9rem;}
.jobs{color:var(--muted);font-size:.85rem;margin-top:.7rem;}
</style>
<main>
"""


def emit_html(projects):
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    total_books = sum(len([b for b in c["books"] if b["has_note"]])
                      for p in projects for c in p["collections"])
    out = [HTML_HEAD,
           "<h1>📚 书架总览</h1>",
           f"<p class='sub'>{len(projects)} 个项目 · {total_books} 本在架 · "
           f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>"]
    for p in projects:
        def dot(v):
            return f"<span class='dot {'up' if v else 'down'}'></span>"
        out.append("<section class='proj'>")
        out.append(f"<h2>{esc(p['slug'])}</h2>")
        out.append(f"<p class='meta'>{esc(p['path'])}<br>"
                   f"{dot(p['server_up'])}AI 服务 :{p['ragPort']} &nbsp; "
                   f"{dot(p['console_up'])}导入控制台 :{p['consolePort']} &nbsp; "
                   f"📝 笔记 {p['notes_total']} 条</p>")
        if not p["exists"]:
            out.append("<p class='alert'>⚠️ 项目目录不存在（注册表指向失效路径）</p></section>")
            continue
        for c in p["collections"]:
            out.append(f"<h3>{esc(c['name'])} <span class='badge'>{c['chunks_total']} chunks</span> "
                       f"<span class='badge'>{c['notes']} 条笔记</span></h3>")
            out.append("<div class='tblwrap'><table><tr><th>书</th><th>页</th>"
                       "<th>chunks</th><th>状态</th></tr>")
            for b in c["books"]:
                st = "<span class='badge b-ok'>✅ 在架</span>" if b["has_note"] \
                    else "<span class='badge b-warn'>🚧 未完成</span>"
                size = f"{b['parts']} 篇" if b["parts"] and not b["pages"] else b["pages"]
                out.append(f"<tr><td>{esc(b['title'])}</td><td class='num'>{size}</td>"
                           f"<td class='num'>{b['chunks']}</td><td>{st}</td></tr>")
            out.append("</table></div>")
        for s in p["staging"]:
            if s["pending"] and not s["on_shelf"]:
                out.append(f"<p class='alert'>🟡 <b>{esc(s['slug'])}</b>：{s['verified']}/"
                           f"{s['transcribed']} 已核验，<b>{len(s['pending'])} 页待精校</b>"
                           f"（导入控制台 → 人工精校）</p>")
        if p["jobs"]:
            rows = []
            for j in p["jobs"][:5]:
                cls = "b-ok" if j["state"] == "passed" else ("b-bad" if j["state"] in ("failed", "escalate") else "b-warn")
                ratio = f" {j['verified']}/{j['total']}" if j["total"] else ""
                rows.append(f"<code>{esc(j['id'])}</code> {esc(j['title'])} [{esc(j['mode'])}] "
                            f"<span class='badge {cls}'>{esc(j['state'])}{ratio}</span>")
            out.append("<p class='jobs'>最近任务：" + " · ".join(rows) + "</p>")
        out.append("</section>")
    out.append("</main>")
    return "\n".join(out)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description="cross-project library inventory")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--html", metavar="OUT", help="write self-contained dashboard page to OUT")
    ap.add_argument("--project", help="limit to one registry slug")
    ap.add_argument("--registry", default=str(REGISTRY))
    ap.add_argument("--no-probe", action="store_true", help="skip server port probes")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    projects = load_registry(Path(args.registry))
    if args.project:
        projects = [p for p in projects if p["slug"] == args.project]
        if not projects:
            print(f"[library] no project '{args.project}' in registry", file=sys.stderr)
            return 1
    if not projects:
        print("[library] registry empty or missing — run setup.ps1 first", file=sys.stderr)
        return 1

    scanned = [scan_project(p, probe=not args.no_probe) for p in projects]

    if args.json:
        print(json.dumps(scanned, ensure_ascii=False, indent=1))
    elif args.html:
        Path(args.html).write_text(emit_html(scanned), encoding="utf-8")
        print(f"[library] dashboard written: {args.html}")
    else:
        print(emit_markdown(scanned))
    return 0


if __name__ == "__main__":
    sys.exit(main())
