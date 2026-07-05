"""
Tiny Markdown -> PDF renderer for the Sentinel learning guide.
Pure-Python (fpdf2) + embedded DejaVu fonts so the ASCII/box-drawing
diagrams render in real monospace. Run:

    uv run --with fpdf2 python _render_pdf.py
"""
import re
import sys
from fpdf import FPDF
from fpdf.enums import XPos, YPos

SRC = "/opt/sentinel/sentinel-learning-guide.md"
OUT = "/opt/sentinel/sentinel-learning-guide.pdf"
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

LMARGIN = TMARGIN = 15
RMARGIN = 15
PAGE_W, PAGE_H = 210, 297
CONTENT_W = PAGE_W - LMARGIN - RMARGIN
BOTTOM_LIMIT = PAGE_H - 15

BODY = 10.0          # pt magnitude used as size
LINE_H = 5.0         # mm line height for body
CODE_BASE = 8.3
ACCENT = (15, 60, 110)      # headings
CODE_BG = (244, 244, 246)
CODE_FG = (45, 45, 45)
INLINE_CODE = (150, 40, 40)
RULE = (200, 200, 205)

INLINE_RE = re.compile(r'(\*\*.+?\*\*|`[^`]+`)')


class PDF(FPDF):
    def footer(self):
        self.set_y(-12)
        self.set_font("Sans", "", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 8, f"HomelabSentinel — Learning Guide   ·   {self.page_no()}",
                  align="C")


def ensure(pdf, h):
    if pdf.get_y() + h > BOTTOM_LIMIT:
        pdf.add_page()


def inline_runs(text):
    """Split a line into (kind, text) runs: kind in normal|bold|code."""
    runs = []
    for piece in INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**"):
            runs.append(("bold", piece[2:-2]))
        elif piece.startswith("`") and piece.endswith("`"):
            runs.append(("code", piece[1:-1]))
        else:
            runs.append(("normal", piece))
    return runs


def write_inline(pdf, text, size=BODY, lh=LINE_H):
    """Flow inline-formatted text, wrapping at the right margin. No trailing ln."""
    for kind, chunk in inline_runs(text):
        if kind == "bold":
            pdf.set_font("Sans", "B", size)
            pdf.set_text_color(20, 20, 20)
        elif kind == "code":
            pdf.set_font("Mono", "", size - 0.7)
            pdf.set_text_color(*INLINE_CODE)
        else:
            pdf.set_font("Sans", "", size)
            pdf.set_text_color(25, 25, 25)
        pdf.write(lh, chunk)
    pdf.set_text_color(0, 0, 0)


def render_code_block(pdf, lines):
    # drop trailing blank lines
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return
    pdf.set_font("Mono", "", CODE_BASE)
    maxw = max((pdf.get_string_width("  " + ln) for ln in lines), default=1) or 1
    size = CODE_BASE
    if maxw > CONTENT_W:
        size = max(6.0, CODE_BASE * CONTENT_W / maxw)
    lh = max(3.2, size * 0.47)
    pdf.set_font("Mono", "", size)
    pdf.ln(1.5)
    pdf.set_fill_color(*CODE_BG)
    pdf.set_text_color(*CODE_FG)
    for ln in lines:
        ensure(pdf, lh)
        pdf.set_x(LMARGIN)
        pdf.cell(CONTENT_W, lh, "  " + ln.rstrip(), border=0, fill=True,
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def render_bullet(pdf, text, nested=False):
    marker_x = LMARGIN + (8 if nested else 2)
    text_x = LMARGIN + (13 if nested else 6)
    ensure(pdf, LINE_H)
    pdf.set_left_margin(text_x)
    pdf.set_xy(marker_x, pdf.get_y())
    pdf.set_font("Sans", "", BODY)
    pdf.set_text_color(*ACCENT)
    pdf.write(LINE_H, "•  ")
    pdf.set_x(text_x)
    write_inline(pdf, text)
    pdf.ln(LINE_H)
    pdf.set_left_margin(LMARGIN)
    pdf.set_x(LMARGIN)


def render_numbered(pdf, num, text):
    text_x = LMARGIN + 8
    ensure(pdf, LINE_H)
    pdf.set_left_margin(text_x)
    pdf.set_xy(LMARGIN + 2, pdf.get_y())
    pdf.set_font("Sans", "B", BODY)
    pdf.set_text_color(*ACCENT)
    pdf.write(LINE_H, f"{num}.")
    pdf.set_x(text_x)
    write_inline(pdf, text)
    pdf.ln(LINE_H)
    pdf.set_left_margin(LMARGIN)
    pdf.set_x(LMARGIN)


def render_heading(pdf, level, text):
    sizes = {1: 21, 2: 15.5, 3: 12.5, 4: 11}
    gap_before = {1: 4, 2: 8, 3: 5, 4: 3.5}
    size = sizes.get(level, 11)
    ensure(pdf, size * 0.6 + 8)
    pdf.ln(gap_before.get(level, 4))
    if level == 2:
        # part divider rule above
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.3)
        y = pdf.get_y()
        pdf.line(LMARGIN, y, PAGE_W - RMARGIN, y)
        pdf.ln(3)
    pdf.set_font("Sans", "B", size)
    pdf.set_text_color(*ACCENT)
    pdf.set_x(LMARGIN)
    pdf.multi_cell(CONTENT_W, size * 0.52, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2 if level >= 3 else 3)


def render_paragraph(pdf, text):
    ensure(pdf, LINE_H)
    pdf.set_x(LMARGIN)
    write_inline(pdf, text)
    pdf.ln(LINE_H)
    pdf.ln(1.6)


def render_rule(pdf):
    pdf.ln(2)
    pdf.set_draw_color(*RULE)
    pdf.set_line_width(0.3)
    y = pdf.get_y()
    pdf.line(LMARGIN, y, PAGE_W - RMARGIN, y)
    pdf.ln(3)


def main():
    with open(SRC, encoding="utf-8") as f:
        raw = f.read().split("\n")

    pdf = PDF(format="A4", unit="mm")
    pdf.set_margins(LMARGIN, TMARGIN, RMARGIN)
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_font("Sans", "", f"{FONT_DIR}/DejaVuSans.ttf")
    pdf.add_font("Sans", "B", f"{FONT_DIR}/DejaVuSans-Bold.ttf")
    pdf.add_font("Mono", "", f"{FONT_DIR}/DejaVuSansMono.ttf")
    pdf.add_font("Mono", "B", f"{FONT_DIR}/DejaVuSansMono-Bold.ttf")
    pdf.add_page()

    i = 0
    para = []

    def flush_para():
        if para:
            render_paragraph(pdf, " ".join(para).strip())
            para.clear()

    while i < len(raw):
        line = raw[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_para()
            i += 1
            block = []
            while i < len(raw) and not raw[i].strip().startswith("```"):
                block.append(raw[i])
                i += 1
            render_code_block(pdf, block)
            i += 1
            continue

        if stripped == "":
            flush_para()
            i += 1
            continue

        if stripped == "---":
            flush_para()
            render_rule(pdf)
            i += 1
            continue

        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            flush_para()
            render_heading(pdf, len(m.group(1)), m.group(2).strip())
            i += 1
            continue

        mnum = re.match(r'^(\d+)\.\s+(.*)$', line)
        if mnum:
            flush_para()
            render_numbered(pdf, mnum.group(1), mnum.group(2).strip())
            i += 1
            continue

        mb = re.match(r'^(\s*)-\s+(.*)$', line)
        if mb:
            flush_para()
            nested = len(mb.group(1)) >= 2
            render_bullet(pdf, mb.group(2).strip(), nested=nested)
            i += 1
            continue

        para.append(stripped)
        i += 1

    flush_para()
    pdf.output(OUT)
    print(f"wrote {OUT}  ({pdf.page_no()} pages)")


if __name__ == "__main__":
    sys.exit(main())
