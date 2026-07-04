#!/usr/bin/env python3
"""
paddle_ocr.py — batch-OCR a document with PaddleOCR-VL via the AI Studio job API.

Submits the WHOLE file as one job (PDF or image), polls until done, then writes
one markdown file per page: <out>/pg-0001.paddle.md, pg-0002.paddle.md, ...

Role in the pipeline: dedicated OCR engine (model A). An independent vision LLM
still produces the ==highlight==-annotated transcript (model B); consensus logic
lives in ingest.py. This script does OCR only.

Usage: python paddle_ocr.py --file book.pdf --out staging/slug/paddle [--first-page 1]
Env:   PADDLEOCR_TOKEN (injected by paddle.ps1 from the DPAPI store)
"""
import argparse, json, os, sys, time
from pathlib import Path
import requests

sys.stdout.reconfigure(encoding="utf-8")

JOB_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
TOKEN = os.environ["PADDLEOCR_TOKEN"]
MODEL = "PaddleOCR-VL-1.6"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--first-page", type=int, default=1,
                   help="PDF page number of the file's first page (for labeling)")
    p.add_argument("--poll", type=int, default=10)
    p.add_argument("--timeout", type=int, default=3600)
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"bearer {TOKEN}"}
    optional = {"useDocOrientationClassify": False, "useDocUnwarping": False,
                "useChartRecognition": False}

    with open(args.file, "rb") as f:
        r = requests.post(JOB_URL, headers=headers,
                          data={"model": MODEL, "optionalPayload": json.dumps(optional)},
                          files={"file": f}, timeout=300)
    if r.status_code != 200:
        sys.exit(f"submit failed {r.status_code}: {r.text[:300]}")
    job_id = r.json()["data"]["jobId"]
    print(f"job {job_id} submitted")

    deadline = time.time() + args.timeout
    json_url = None
    while time.time() < deadline:
        jr = requests.get(f"{JOB_URL}/{job_id}", headers=headers, timeout=60).json()["data"]
        state = jr["state"]
        if state == "done":
            prog = jr.get("extractProgress", {})
            print(f"done: {prog.get('extractedPages')} pages")
            json_url = jr["resultUrl"]["jsonUrl"]
            break
        if state == "failed":
            sys.exit(f"job failed: {jr.get('errorMsg')}")
        prog = jr.get("extractProgress") or {}
        print(f"{state} {prog.get('extractedPages','?')}/{prog.get('totalPages','?')}", flush=True)
        time.sleep(args.poll)
    if not json_url:
        sys.exit("timed out")

    jl = requests.get(json_url, timeout=300)
    jl.raise_for_status()
    n = 0
    page = args.first_page
    for line in jl.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        for res in json.loads(line)["result"]["layoutParsingResults"]:
            (out / f"pg-{page:04d}.paddle.md").write_text(
                res["markdown"]["text"], encoding="utf-8")
            n += 1
            page += 1
    print(f"wrote {n} page files to {out}")

if __name__ == "__main__":
    main()
