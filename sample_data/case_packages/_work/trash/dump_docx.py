# -*- coding: utf-8 -*-
import docx, os, glob
from docx.table import Table
from docx.text.paragraph import Paragraph

def dump_docx(path):
    d = docx.Document(path)
    out = []
    out.append(f"===== {path} =====")
    for si, sec in enumerate(d.sections):
        out.append(f"[Section {si}] page={sec.page_width}x{sec.page_height} margins T{sec.top_margin} B{sec.bottom_margin} L{sec.left_margin} R{sec.right_margin}")
    body = d.element.body
    for child in body.iterchildren():
        if child.tag.endswith('}p'):
            p = Paragraph(child, d)
            runs_info = []
            for r in p.runs:
                fname = r.font.name
                ea = None
                rPr = r._element.rPr
                if rPr is not None and rPr.rFonts is not None:
                    ea = rPr.rFonts.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia')
                sz = r.font.size.pt if r.font.size else None
                runs_info.append(f"[{fname}|ea:{ea}|sz:{sz}|b:{r.font.bold}]{(r.text[:50])}")
            align = p.alignment
            ind = p.paragraph_format
            ls = ind.line_spacing
            fld = ind.first_line_indent
            left = ind.left_indent
            if runs_info:
                out.append(f"P st={p.style.name} al={align} fi={fld} li={left} ls={ls} | " + " || ".join(runs_info))
            else:
                out.append(f"P st={p.style.name} al={align} (noruns) text='{p.text[:40]}'")
        elif child.tag.endswith('}tbl'):
            t = Table(child, d)
            out.append(f"[TABLE {len(t.rows)}x{len(t.columns)}]")
            for row in t.rows[:4]:
                cells = [c.text[:18].replace('\n', ' ') for c in row.cells]
                out.append("   | " + " | ".join(cells))
            if len(t.rows) > 4:
                out.append(f"   ... ({len(t.rows)} rows total)")
    return "\n".join(out)

root = "C:/Users/youxi/Documents/shujvfa/sample_data/case_packages"
out = []
for f in sorted(glob.glob(os.path.join(root, "GOLD_CASE_00?", "**", "*.docx"), recursive=True)):
    out.append(dump_docx(f))
    out.append("")
text = "\n".join(out)
with open(os.path.join(root, "_work", "docx_dump.txt"), "w", encoding="utf-8") as fh:
    fh.write(text)
print("written", len(text), "chars")
