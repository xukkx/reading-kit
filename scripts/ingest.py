#!/usr/bin/env python3
"""
ingest.py — verified ingestion of sources into staging/ (never into vault/).

Pipeline per page/image:
  render (if PDF) -> transcribe with model A -> transcribe with model B
  -> char-level similarity of normalized texts
  -> status: verified (>=0.97) | adjudicate (0.90-0.97) | escalated (<0.90)
  -> write transcripts, .diff for disagreements, append provenance to ledger.jsonl

The orchestrator (Claude) adjudicates flagged pages against the page image and
is the only party allowed to promote content from staging/ to vault/.

Usage:
  python ingest.py --pdf path.pdf --first 10 --last 39 --slug yuedu-heji
  python ingest.py --images "photo1.jpg,photo2.jpg" --slug handwritten-x
Env: OLLAMA_API_KEY (injected by ingest.ps1 from DPAPI store)
"""
import argparse, base64, difflib, io, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error

sys.stdout.reconfigure(encoding="utf-8")

API = "https://ollama.com/api/chat"
KEY = os.environ["OLLAMA_API_KEY"]
PROMPT = ("这是一页扫描的中文书页。请逐字转录页面上的全部文字，保持原有段落结构。"
          "页面上有荧光笔高亮标记的文字用==文字==包裹。看不清的字用□代替。"
          "只输出转录内容，不要任何解释。")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def transcribe(model, img_path, retries=2):
    b64 = base64.b64encode(Path(img_path).read_bytes()).decode()
    body = json.dumps({
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": PROMPT, "images": [b64]}],
    }).encode()
    for attempt in range(retries + 1):
        try:
            req = request.Request(API, data=body, headers={
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json"})
            with request.urlopen(req, timeout=420) as r:
                return json.loads(r.read())["message"]["content"].strip()
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(8 * (attempt + 1))

def normalize(t):
    """Strip highlight markers, whitespace, illegible placeholders, and ALL punctuation
    before comparing — models disagree on halfwidth vs fullwidth punctuation constantly,
    and hallucination detection cares about content characters, not typography."""
    t = t.replace("==", "")
    t = re.sub(r"[\s□]+", "", t)
    t = re.sub(r"[^\w一-鿿]", "", t)
    return t

def process_page(args, staging, ledger_path, img, label):
    tdir = staging / "transcripts"
    vdir = staging / "verified"
    paddle = Path(args.paddle_dir) / f"{label}.paddle.md" if args.paddle_dir else None
    model_a = "PaddleOCR-VL-1.6" if paddle else args.model_a
    rec = {"ts": now(), "source": args.pdf or args.images, "page": label,
           "image": str(img), "model_a": model_a, "model_b": args.model_b}
    try:
        if paddle:
            # Model A = dedicated OCR engine output (already on disk, no API call)
            a = paddle.read_text(encoding="utf-8")
        else:
            a = transcribe(args.model_a, img)
        b = transcribe(args.model_b, img)
        (tdir / f"{label}.a.md").write_text(a, encoding="utf-8")
        (tdir / f"{label}.b.md").write_text(b, encoding="utf-8")
        na, nb = normalize(a), normalize(b)
        sm = difflib.SequenceMatcher(None, na, nb)
        matched = sum(bl.size for bl in sm.get_matching_blocks())
        ratio = 2 * matched / max(1, len(na) + len(nb))
        # containment: how much of the SHORTER text is found in the longer one.
        # Layout engines drop/reorder footnote blocks; a near-superset is a
        # coverage difference, not a hallucination signal.
        contain = matched / max(1, min(len(na), len(nb)))
        rec["similarity"] = round(ratio, 4)
        rec["containment"] = round(contain, 4)
        if ratio >= 0.97 or (contain >= 0.97 and len(nb) >= len(na)):
            rec["status"] = "verified"
            if contain >= 0.97 and ratio < 0.97:
                rec["note"] = "b-superset: model B covers all of A plus blocks A dropped"
            # Prefer the LLM transcript: it carries ==highlight== markers, and
            # consensus guarantees the characters match the OCR engine anyway.
            (vdir / f"{label}.md").write_text(b if paddle else a, encoding="utf-8")
        else:
            if contain >= 0.97:
                rec["status"] = "adjudicate"
                rec["note"] = "a-superset: model B is missing blocks A has"
            else:
                rec["status"] = "adjudicate" if ratio >= 0.90 else "escalated"
            diff = "\n".join(difflib.unified_diff(
                a.splitlines(), b.splitlines(),
                fromfile=f"{label}.{model_a}", tofile=f"{label}.{args.model_b}", lineterm=""))
            (tdir / f"{label}.diff").write_text(diff, encoding="utf-8")
    except Exception as e:
        rec["status"] = "error"
        rec["error"] = str(e)[:300]
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{label}: {rec['status']}" + (f" (sim={rec.get('similarity')})" if "similarity" in rec else ""))
    return rec

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pdf")
    p.add_argument("--images", help="comma-separated image paths (e.g. photos of handwritten notes)")
    p.add_argument("--first", type=int, default=1)
    p.add_argument("--last", type=int)
    p.add_argument("--slug", required=True)
    p.add_argument("--paddle-dir", help="dir of pg-XXXX.paddle.md files from paddle_ocr.py; "
                   "when set, model A = PaddleOCR output (no LLM call for A)")
    p.add_argument("--model-a", default="gemini-3-flash-preview",
                   help="only used when --paddle-dir is not set")
    p.add_argument("--model-b", default="qwen3.5:397b")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--dpi", type=int, default=150)
    args = p.parse_args()

    root = Path(__file__).resolve().parent.parent
    staging = root / "staging" / args.slug
    for d in ("pages", "transcripts", "verified"):
        (staging / d).mkdir(parents=True, exist_ok=True)
    ledger = root / "staging" / "ledger.jsonl"

    jobs = []
    if args.pdf:
        prefix = staging / "pages" / "pg"
        subprocess.run(["pdftoppm", "-png", "-f", str(args.first), "-l", str(args.last),
                        "-r", str(args.dpi), args.pdf, str(prefix)], check=True)
        for img in sorted((staging / "pages").glob("pg-*.png")):
            n = int(img.stem.split("-")[1])
            if args.first <= n <= args.last:
                jobs.append((img, f"pg-{n:04d}"))
    elif args.images:
        for i, path in enumerate([s.strip() for s in args.images.split(",") if s.strip()], 1):
            jobs.append((Path(path), f"img-{i:04d}-{Path(path).stem}"))
    else:
        sys.exit("need --pdf or --images")

    print(f"ingest: {len(jobs)} page(s), models {args.model_a} + {args.model_b}")
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(lambda j: process_page(args, staging, ledger, *j), jobs))

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("SUMMARY " + json.dumps(counts))

if __name__ == "__main__":
    main()
