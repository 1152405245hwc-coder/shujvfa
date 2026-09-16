# -*- coding: utf-8 -*-
"""Convert rebuilt docx files to PDF via Word COM (late binding)."""
import os, glob, win32com.client as w

ROOT = "C:/Users/youxi/Documents/shujvfa/sample_data/case_packages"
BUILD = os.path.join(ROOT, "_work", "build")
PDF = os.path.join(ROOT, "_work", "pdf")
os.makedirs(PDF, exist_ok=True)

app = w.DispatchEx("Word.Application")
app.Visible = False
app.DisplayAlerts = 0

files = sorted(glob.glob(os.path.join(BUILD, "*.docx")))
for f in files:
    doc = app.Documents.Open(f, ReadOnly=True)
    out = os.path.join(PDF, os.path.splitext(os.path.basename(f))[0] + ".pdf")
    doc.SaveAs2(out, FileFormat=17)  # wdFormatPDF
    doc.Close(False)
    print("pdf:", os.path.basename(out))
app.Quit()
print("done")
