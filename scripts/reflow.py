#!/usr/bin/env python3
"""
reflow.py — stitch verified per-page transcripts into a reader-optimized
chapter markdown (the v0 "微信读书" reading build).

Transformations:
  - merge paragraphs split across page breaks (page ends mid-sentence)
  - strip standalone page-number lines (52 / —86— / 61)
  - keep citation anchors: each page contributes an invisible %%p.N%% comment
  - footnote lines (①②③…) collected into a quote block at the page's end
  - ==highlights== pass through (Obsidian renders them as real highlights)

Usage:
  python reflow.py --slug yuedu-heji --first 10 --last 32 \
      --title 比较文学论（梵·第根） --page-offset 40 --out "vault/Books/比较文学论"
"""
import argparse, re, sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

FOOT = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩]")
PAGENUM = re.compile(r"^[—\-–\s]*\d{1,4}[—\-–\s]*$")
SENT_END = tuple('。？！："」』）〕》＞>؟…—')
AUTHOR = re.compile(r"^[一-鿿]{1,4}(·[一-鿿]{1,4})+$")  # 保罗·梵·第根

def headingize(para, title):
    """Short standalone lines with no full sentence inside are section titles.
    Conservative on purpose: a missed heading is cosmetic, a false one breaks
    the text flow (page-seam continuation stubs look deceptively title-like)."""
    p = para.strip()
    if (len(p) <= 36 and "==" not in p and "<span" not in p
            and not re.search(r"[。？！…\d，、]", p)       # no sentence/clause inside, no digits
            and not p.endswith(("；", "：", "的", "有"))   # not a dangling clause
            and not re.fullmatch(r"[—–\-─]+", p)          # not a separator rule
            and not re.match(r"^一九|^二〇", p)            # not a date line
            and not AUTHOR.match(p)
            and not re.match(r"^[一-鿿]{2,4}\s*(译|著)$", p)
            and p not in title):
        return ("## " if re.match(r"^(第[一二三四五六七八九十]+部|导言|后记|附录)", p) else "### ") + p
    return None

def clean_page(text):
    """Return (paragraphs, footnotes) for one page."""
    paras, feet = [], []
    for block in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        keep = []
        for l in lines:
            if PAGENUM.match(l):
                continue
            if FOOT.match(l):
                feet.append(l)
            else:
                keep.append(l)
        if keep:
            # inside a block, hard-wrapped lines are one paragraph
            paras.append("".join(keep) if re.search(r"[一-鿿]", "".join(keep)) else " ".join(keep))
    return paras, feet

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument("--first", type=int, required=True)
    p.add_argument("--last", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--page-offset", type=int, default=0,
                   help="book page = pdf page + offset (for anchors)")
    p.add_argument("--out", required=True)
    args = p.parse_args()

    root = Path(__file__).resolve().parent.parent
    vdir = root / "staging" / args.slug / "verified"
    out_dir = root / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    body = []          # list of paragraphs (strings)
    pending_feet = []  # footnotes to flush after current page's paragraphs
    missing = []
    for n in range(args.first, args.last + 1):
        f = vdir / f"pg-{n:04d}.md"
        if not f.exists():
            missing.append(n)
            continue
        paras, feet = clean_page(f.read_text(encoding="utf-8"))
        paras = [headingize(p, args.title) or p for p in paras]
        anchor = f'<span class="pg" data-p="{n + args.page_offset}"></span>'
        if paras:
            # merge with previous paragraph if it ended mid-sentence
            if body and not body[-1].startswith("#") \
                    and not body[-1].rstrip("=").endswith(SENT_END) \
                    and not paras[0].startswith(("#", ">", "—")):
                # OCR models often insert □ for a "clipped" char at the top of a
                # page; when the seam joins mid-sentence, that □ is spurious.
                paras[0] = paras[0].lstrip("□")
                body[-1] = body[-1] + anchor + paras[0]
                paras = paras[1:]
            else:
                body.append(anchor)
        body.extend(paras)
        if feet:
            body.append("> " + "\n> ".join(feet))
    text = "\n\n".join(body)

    from datetime import date
    note = (f"---\ntags: [book, reader]\ncreated: {date.today().isoformat()}\n"
            f"---\n\n# {args.title}\n\n{text}\n")
    out = out_dir / "正文.md"
    out.write_text(note, encoding="utf-8")
    print(f"wrote {out} ({len(text)} chars, pages {args.first}-{args.last})")
    if missing:
        print(f"WARNING missing pages: {missing}")

if __name__ == "__main__":
    main()
