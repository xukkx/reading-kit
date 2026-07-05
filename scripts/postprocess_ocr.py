# -*- coding: utf-8 -*-
r"""postprocess_ocr — strip spurious LaTeX from PaddleOCR-VL output of humanities texts.

PaddleOCR-VL wraps footnote markers, emphasis dots (着重号), underlines and the odd
diagram in $...$ / $$...$$ math spans. In humanities corpora there is no real math,
so every span can be converted back to plain text / minimal HTML:

    $ ^{①} $            -> ①
    $ ^{[3]} $ / $ ^{12} $ -> <sup>[3]</sup> / <sup>12</sup>
    \textcircled{3}      -> ③
    \underset{\cdot}{字}  -> 字          (着重号 dropped)
    \underline{\text{x}} / \uwave{...} -> <u>x</u>
    $ 33.9 \times 49.4 $ -> 33.9 × 49.4
    $$ \begin{array}...  -> plain-text lines

Usage:
    python postprocess_ocr.py <file-or-glob> [...] [--dry-run]

Files are rewritten in place (UTF-8). --dry-run only reports what would change.
Do NOT run this on STEM books — their formulas are real.
"""
import argparse, glob, io, re, sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# circled numbers ①-⑳ ㉑-㉟ ㊱-㊿ and dingbat ❶-➓
_CIRCLED = r"①-⑳㉑-㉟㊱-㊿❶-➓⓪⓫-⓿"

_UNKNOWN = set()   # unrecognized \commands seen (reported at the end)


def _convert_commands(s: str) -> str:
    """Convert the LaTeX commands PaddleOCR actually emits to text/HTML."""
    # \textcircled{3} -> ③ (footnote mark in yet another encoding)
    s = re.sub(r"\\textcircled\s*\{(\d{1,2})\}",
               lambda m: chr(0x2460 + int(m.group(1)) - 1) if 1 <= int(m.group(1)) <= 20
               else f"({m.group(1)})", s)
    # emphasis dots under chars (着重号): \underset{\cdot}{字} -> 字
    s = re.sub(r"\\underset\{\\cdot\}\{([^{}]*)\}", r"\1", s)
    # underline / wavy underline (专名号/着重线): keep an <u> so the mark survives
    s = re.sub(r"\\u(?:nderline|wave)\{\\text\{([^{}]*)\}\}", r"<u>\1</u>", s)
    s = re.sub(r"\\u(?:nderline|wave)\{([^{}]*)\}", r"<u>\1</u>", s)
    s = re.sub(r"\\text\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", s)
    replacements = {
        r"\times": "×", r"\triangle": "△", r"\cdot": "·", r"\cdots": "…",
        r"\ldots": "…", r"\quad": " ", r"\qquad": "  ", r"\sim": "~",
        r"\%": "%", r"\&": "&", r"\$": "$", r"\{": "{", r"\}": "}",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    # structural noise from array/aligned blocks
    s = re.sub(r"\\begin\{[a-z*]+\}(?:\{[a-zclr|@ ]*\})?", "", s)
    s = re.sub(r"\\end\{[a-z*]+\}", "", s)
    s = re.sub(r"\\left\.?|\\right\.?", "", s)
    s = s.replace(r"\\", "\n").replace("&", " ")
    # report anything we did not anticipate, then drop the bare command word
    for cmd in re.findall(r"\\[A-Za-z]+", s):
        _UNKNOWN.add(cmd)
        s = s.replace(cmd, "")
    return s


def _inline(m: re.Match) -> str:
    inner = m.group(1).strip()
    if not inner:
        return ""
    # footnote superscript: ^{①} ^{[3]} ^{12} ^{{*}}
    sup = re.match(r"^\^\s*\{(.+)\}$", inner) or re.match(r"^\^\s*(\S+)$", inner)
    if sup:
        mark = sup.group(1).strip().strip("{}").strip()
        if re.fullmatch(f"[{_CIRCLED}]+", mark):
            return mark                     # ① is self-evidently a footnote mark
        return f"<sup>{mark}</sup>"        # [3] / 12 / * need the superscript
    return _convert_commands(inner).strip()


def _block(m: re.Match) -> str:
    body = _convert_commands(m.group(1)).strip()
    body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
    return ("\n" + body + "\n") if body else ""


def demath(text: str) -> str:
    text = re.sub(r"\$\$(.*?)\$\$", _block, text, flags=re.S)
    # inline spans stay on one line; bound length so a stray $ can't swallow a page
    text = re.sub(r"\$([^$\n]{0,200})\$", _inline, text)
    # orphan superscript whose closing $ the OCR dropped: $ ^{⑥}
    text = re.sub(r"\$\s*\^\s*\{([^{}]{1,8})\}\s*",
                  lambda m: m.group(1) if re.fullmatch(f"[{_CIRCLED}]+", m.group(1))
                  else f"<sup>{m.group(1)}</sup>", text)
    # collapse spaces introduced around removed spans (CJK needs no space)
    text = re.sub(r"(?<=[一-鿿。，、；：）】」』”])[ \t]+(?=[一-鿿（【「『“])", "", text)
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="markdown files or globs")
    ap.add_argument("--dry-run", action="store_true", help="report only, do not rewrite")
    args = ap.parse_args()

    files = []
    for p in args.paths:
        hits = glob.glob(p, recursive=True)
        files += [Path(h) for h in hits] if hits else [Path(p)]

    changed = 0
    for f in files:
        if not f.is_file():
            print(f"skip (not a file): {f}")
            continue
        before = f.read_text(encoding="utf-8")
        after = demath(before)
        n = before.count("$") - after.count("$")
        if after != before:
            changed += 1
            if not args.dry_run:
                f.write_text(after, encoding="utf-8")
            print(f"{'would clean' if args.dry_run else 'cleaned'}: {f.name}  (-{n} $)")
    print(f"\n{changed}/{len(files)} file(s) changed")
    if _UNKNOWN:
        print("unrecognized latex commands (content kept, command dropped):", sorted(_UNKNOWN))


if __name__ == "__main__":
    main()
