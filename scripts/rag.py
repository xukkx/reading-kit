#!/usr/bin/env python3
"""
rag.py — local-first RAG knowledge base over the Obsidian vault.

Portability contract (runs on ANY user's machine):
  embeddings backend auto-detection, in order:
    1. local Ollama server (localhost:11434) with EMBED_MODEL — GPU or CPU, free & private
    2. cloud embeddings provider from .rag/providers.json ("embeddings" entry)
    3. clear error telling the user what to set up
  chat backend: any OpenAI-compatible endpoint from providers.json (deepseek / mimo /
  ollama-cloud / ...), switchable with --provider.

Collections: vault content partitions into independent indices so retrieval never
crosses into unrelated material. Collection names are DISCOVERED from Books/ only:
a Books/<X>/ that itself contains subfolders (Books/<X>/<书名>/file.md) means X is
a real collection; a Books/<X>/ holding files directly means X is just a book.
That discovered set of names is then applied to 10-Notes/<X>/ and 20-Literature/<X>/
too. This asymmetry is deliberate — 10-Notes/20-Literature subfolders often exist
for unrelated reasons (e.g. a "full text vs stub" split) and must NOT be mistaken
for a collection boundary. Anything not matching a real collection name — including
a single-collection vault with no Books/ nesting at all — falls into "default", so
existing vaults work unchanged. Each collection gets its own
.rag/<collection>/{index.json,vectors.npy} — rebuilding one never touches another.

Usage:
  python rag.py build [--collection X]        # (re)index; omit --collection to (re)build all
  python rag.py ask "问题" [--collection X] [--provider deepseek] [-k 6]
  python rag.py serve [--port 8766]           # LAN HTTP endpoint; collection comes per-request
"""
import argparse, hashlib, json, os, re, sys
from pathlib import Path
from urllib import request as urlreq

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "vault"
RAG = ROOT / ".rag"
EMBED_MODEL = "bge-m3"
OLLAMA_LOCAL = "http://localhost:11434"

# ---------- providers ----------

def load_providers():
    f = RAG / "providers.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

def get_key(spec):
    """Resolve a key spec: 'env:NAME' or 'dpapi:file.dat' (Windows) or literal."""
    if spec.startswith("env:"):
        return os.environ.get(spec[4:], "")
    if spec.startswith("dpapi:"):
        import subprocess
        cmd = (f"$sec = Get-Content \"$HOME\\.secrets\\{spec[6:]}\" | ConvertTo-SecureString; "
               f"[pscredential]::new('x',$sec).GetNetworkCredential().Password")
        r = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True)
        return r.stdout.strip()
    return spec

def post_json(url, payload, key=None, timeout=180):
    req = urlreq.Request(url, data=json.dumps(payload).encode(),
                         headers={"Content-Type": "application/json",
                                  **({"Authorization": f"Bearer {key}"} if key else {})})
    with urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# ---------- embeddings (auto-detecting) ----------

def detect_embedder():
    """Return (name, embed_fn). Local Ollama first, cloud fallback second."""
    try:
        urlreq.urlopen(f"{OLLAMA_LOCAL}/api/version", timeout=2)
        def local_embed(texts):
            out = post_json(f"{OLLAMA_LOCAL}/api/embed",
                            {"model": EMBED_MODEL, "input": texts})
            return np.array(out["embeddings"], dtype=np.float32)
        return f"ollama-local/{EMBED_MODEL}", local_embed
    except Exception:
        pass
    emb = load_providers().get("embeddings")
    if emb:
        key = get_key(emb.get("key", ""))
        def cloud_embed(texts):
            out = post_json(f"{emb['base_url'].rstrip('/')}/embeddings",
                            {"model": emb.get("model", EMBED_MODEL), "input": texts}, key)
            return np.array([d["embedding"] for d in out["data"]], dtype=np.float32)
        return f"cloud/{emb.get('model', EMBED_MODEL)}", cloud_embed
    sys.exit("No embedding backend: start Ollama (ollama pull bge-m3) or add an "
             "'embeddings' provider to .rag/providers.json")

# ---------- indexing ----------

SKIP = re.compile(r"[\\/](\.obsidian|_templates)[\\/]")
COLLECTION_ROOTS = ("Books", "10-Notes", "20-Literature")
DEFAULT_COLLECTION = "default"

def discover_collections():
    """Collections are named by Books/ subfolders that themselves contain further
    subfolders (Books/<Collection>/<Book>/...). A Books/<X>/ holding files directly
    is a book, not a collection -- X doesn't count. This intentionally does NOT
    infer collections from 10-Notes/20-Literature's own nesting: those folders can
    be subdivided for unrelated reasons (e.g. a "full text vs stub" split) that
    have nothing to do with topic grouping, and would otherwise cause false splits."""
    books_dir = VAULT / "Books"
    names = set()
    if books_dir.exists():
        for entry in books_dir.iterdir():
            if entry.is_dir() and any(child.is_dir() for child in entry.iterdir()):
                names.add(entry.name)
    return names

def file_collection(rel_path, collections):
    """rel_path's collection: a Books/10-Notes/20-Literature subfolder name, but
    only if that name is a real collection (see discover_collections). Everything
    else -- including same-named coincidences that aren't collections -- is default."""
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] in COLLECTION_ROOTS and parts[1] in collections:
        return parts[1]
    return DEFAULT_COLLECTION

def chunk_file(path):
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)  # frontmatter off
    rel = str(path.relative_to(VAULT))
    heading, page, chunks, buf = "", "", [], ""
    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append({"file": rel, "heading": heading, "page": page,
                           "text": buf.strip()[:1200]})
        buf = ""
    for block in re.split(r"\n\s*\n", text):
        if m := re.match(r"^#{1,6}\s+(.*)", block.strip()):
            heading = m.group(1).strip()
        if m := re.search(r'data-p="(\d+)"|%%p\.(\d+)%%', block):
            page = m.group(1) or m.group(2)
        block = re.sub(r"<[^>]+>", "", block)  # keep markup out of embeddings
        if len(buf) + len(block) > 700:
            flush()
        buf += block + "\n\n"
    flush()
    return chunks

def cmd_build(collection=None):
    RAG.mkdir(exist_ok=True)
    name, embed = detect_embedder()
    collections = discover_collections()
    by_collection = {}
    for f in sorted(VAULT.rglob("*.md")):
        if SKIP.search(str(f)):
            continue
        rel = str(f.relative_to(VAULT))
        c = file_collection(rel, collections)
        if collection and c != collection:
            continue
        by_collection.setdefault(c, []).extend(chunk_file(f))

    if not by_collection:
        print(f"没有匹配的文件（collection={collection!r}），未写入任何索引")
        return

    for c, chunks in sorted(by_collection.items()):
        if not chunks:
            print(f"[{c}] 0 chunks，跳过（文件夹里都是空文件？）")
            continue
        cdir = RAG / c
        cdir.mkdir(exist_ok=True)
        print(f"[{c}] {len(chunks)} chunks, embedding with {name} ...")
        vecs = []
        for i in range(0, len(chunks), 32):
            vecs.append(embed([ck["text"] for ck in chunks[i:i+32]]))
            print(f"  [{c}] {min(i+32, len(chunks))}/{len(chunks)}", flush=True)
        mat = np.vstack(vecs)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
        np.save(cdir / "vectors.npy", mat)
        (cdir / "index.json").write_text(json.dumps(
            {"embed_model": EMBED_MODEL, "backend": name, "chunks": chunks},
            ensure_ascii=False), encoding="utf-8")
        print(f"[{c}] index saved: {mat.shape[0]} vectors x {mat.shape[1]} dims")

def list_collections():
    if not RAG.exists():
        return []
    return sorted(d.name for d in RAG.iterdir() if d.is_dir() and (d / "index.json").exists())

# ---------- ask ----------

def retrieve(q, k, collection=DEFAULT_COLLECTION):
    cdir = RAG / collection
    idx_path = cdir / "index.json"
    if not idx_path.exists():
        raise FileNotFoundError(
            f"collection {collection!r} 还没有索引 —— 先跑 python scripts\\rag.py build --collection {collection}")
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    mat = np.load(cdir / "vectors.npy")
    _, embed = detect_embedder()
    qv = embed([q])[0]
    qv /= np.linalg.norm(qv) + 1e-9
    top = np.argsort(mat @ qv)[::-1][:k]
    return [idx["chunks"][i] for i in top]

def cmd_ask(q, provider=None, k=6, collection=DEFAULT_COLLECTION):
    cfg = load_providers()
    pname = provider or cfg.get("default")
    p = cfg["providers"][pname]
    hits = retrieve(q, k, collection)
    ctx = "\n\n".join(
        f"[{i+1}] 《{h['file']}》{('#' + h['heading']) if h['heading'] else ''}"
        f"{(' p.' + h['page']) if h['page'] else ''}\n{h['text']}"
        for i, h in enumerate(hits))
    out = post_json(f"{p['base_url'].rstrip('/')}/chat/completions", {
        "model": p["model"],
        "messages": [
            {"role": "system", "content":
             "你是一个私人知识库助手。只依据提供的笔记片段回答问题，引用时标注来源编号如[1]。"
             "片段里没有的信息就明说不知道，不要编造。用户用什么语言问就用什么语言答。"},
            {"role": "user", "content": f"笔记片段：\n\n{ctx}\n\n问题：{q}"},
        ],
        "stream": False,
    }, get_key(p.get("key", "")))
    answer = out["choices"][0]["message"]["content"]
    return answer, hits, pname

# ---------- serve (LAN endpoint for the Obsidian plugin) ----------

def cmd_serve(port):
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            cols = list_collections()
            counts = {}
            for c in cols:
                try:
                    counts[c] = len(json.loads((RAG / c / "index.json").read_text(encoding="utf-8"))["chunks"])
                except Exception:
                    counts[c] = 0
            data = json.dumps({"ok": True, "chunks": sum(counts.values()), "collections": counts}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            try:
                if body.get("cmd") == "rebuild":
                    # collection omitted or "*" -> rebuild everything; else scoped to just that one
                    target = body.get("collection")
                    cmd_build(None if (not target or target == "*") else target)
                    counts = {c: len(json.loads((RAG / c / "index.json").read_text(encoding="utf-8"))["chunks"])
                              for c in list_collections()}
                    data = json.dumps({"ok": True, "chunks": sum(counts.values()), "collections": counts}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                ans, hits, pname = cmd_ask(body["q"], body.get("provider"), body.get("k", 6),
                                            body.get("collection", DEFAULT_COLLECTION))
                data = json.dumps({"answer": ans, "provider": pname,
                                   "sources": hits}, ensure_ascii=False).encode()
                self.send_response(200)
            except Exception as e:
                data = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    print(f"RAG endpoint: POST http://0.0.0.0:{port}  body: {{\"q\": \"问题\"}}")
    # ThreadingHTTPServer: a single wedged client connection must not block
    # every other request (with the plain HTTPServer even /health hangs).
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--collection", default=None)
    a = sub.add_parser("ask"); a.add_argument("q"); a.add_argument("--provider"); a.add_argument("-k", type=int, default=6)
    a.add_argument("--collection", default=DEFAULT_COLLECTION)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    if args.cmd == "build":
        cmd_build(args.collection)
    elif args.cmd == "ask":
        ans, hits, pname = cmd_ask(args.q, args.provider, args.k, args.collection)
        print(f"—— 回答（{pname}）——\n{ans}\n\n—— 来源 ——")
        for i, h in enumerate(hits):
            print(f"[{i+1}] {h['file']}" + (f" p.{h['page']}" if h['page'] else ""))
    elif args.cmd == "serve":
        cmd_serve(args.port)
