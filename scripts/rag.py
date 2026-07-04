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

Index: .rag/index.json (chunks+meta) + .rag/vectors.npy — records the embedding
model name; queries must use the same model (local or hosted — same model, same space).

Usage:
  python rag.py build                 # (re)index the vault
  python rag.py ask "问题" [--provider deepseek] [-k 6]
  python rag.py serve [--port 8766]   # LAN HTTP endpoint for the Obsidian plugin
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

def cmd_build():
    RAG.mkdir(exist_ok=True)
    name, embed = detect_embedder()
    chunks = []
    for f in sorted(VAULT.rglob("*.md")):
        if SKIP.search(str(f)):
            continue
        chunks.extend(chunk_file(f))
    print(f"{len(chunks)} chunks from vault, embedding with {name} ...")
    vecs = []
    for i in range(0, len(chunks), 32):
        vecs.append(embed([c["text"] for c in chunks[i:i+32]]))
        print(f"  {min(i+32, len(chunks))}/{len(chunks)}", flush=True)
    mat = np.vstack(vecs)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9
    np.save(RAG / "vectors.npy", mat)
    (RAG / "index.json").write_text(json.dumps(
        {"embed_model": EMBED_MODEL, "backend": name, "chunks": chunks},
        ensure_ascii=False), encoding="utf-8")
    print(f"index saved: {mat.shape[0]} vectors x {mat.shape[1]} dims")

# ---------- ask ----------

def retrieve(q, k):
    idx = json.loads((RAG / "index.json").read_text(encoding="utf-8"))
    mat = np.load(RAG / "vectors.npy")
    _, embed = detect_embedder()
    qv = embed([q])[0]
    qv /= np.linalg.norm(qv) + 1e-9
    top = np.argsort(mat @ qv)[::-1][:k]
    return [idx["chunks"][i] for i in top]

def cmd_ask(q, provider=None, k=6):
    cfg = load_providers()
    pname = provider or cfg.get("default")
    p = cfg["providers"][pname]
    hits = retrieve(q, k)
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
    from http.server import HTTPServer, BaseHTTPRequestHandler
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_GET(self):
            try:
                n = len(json.loads((RAG / "index.json").read_text(encoding="utf-8"))["chunks"])
            except Exception:
                n = 0
            data = json.dumps({"ok": True, "chunks": n}).encode()
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
                    cmd_build()
                    count = len(json.loads((RAG / "index.json").read_text(encoding="utf-8"))["chunks"])
                    data = json.dumps({"ok": True, "chunks": count}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                ans, hits, pname = cmd_ask(body["q"], body.get("provider"), body.get("k", 6))
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
    HTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    a = sub.add_parser("ask"); a.add_argument("q"); a.add_argument("--provider"); a.add_argument("-k", type=int, default=6)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    if args.cmd == "build":
        cmd_build()
    elif args.cmd == "ask":
        ans, hits, pname = cmd_ask(args.q, args.provider, args.k)
        print(f"—— 回答（{pname}）——\n{ans}\n\n—— 来源 ——")
        for i, h in enumerate(hits):
            print(f"[{i+1}] {h['file']}" + (f" p.{h['page']}" if h['page'] else ""))
    elif args.cmd == "serve":
        cmd_serve(args.port)
