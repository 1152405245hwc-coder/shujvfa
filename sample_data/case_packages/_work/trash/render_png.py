# -*- coding: utf-8 -*-
import pymupdf, os, glob

ROOT = "C:/Users/youxi/Documents/shujvfa/sample_data/case_packages"
PDF = os.path.join(ROOT, "_work", "pdf")
PNG = os.path.join(ROOT, "_work", "png")
os.makedirs(PNG, exist_ok=True)

want = ["01_起诉书", "03_证人证言", "05_被害人陈述", "01_indictment", "06_witness_statements", "08_chat_records", "04_被告人供述与辩解", "09_project_materials"]
for f in sorted(glob.glob(os.path.join(PDF, "*.pdf"))):
    base = os.path.splitext(os.path.basename(f))[0]
    if not any(w in base for w in want):
        continue
    doc = pymupdf.open(f)
    print(base, "pages:", len(doc))
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=100)
        out = os.path.join(PNG, f"{base}_p{i+1}.png")
        pix.save(out)
    doc.close()
print("rendered")
