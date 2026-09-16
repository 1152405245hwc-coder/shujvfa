# -*- coding: utf-8 -*-
import win32com.client as w
import os, glob, pymupdf

BUILD = r'C:\Users\youxi\Documents\shujvfa\sample_data\case_packages\_work\build2'
PDF = r'C:\Users\youxi\Documents\shujvfa\sample_data\case_packages\_work\pdf_g2_full'
os.makedirs(PDF, exist_ok=True)

app = w.DispatchEx('Word.Application')
app.Visible = False
app.DisplayAlerts = 0
try:
    for f in sorted(glob.glob(os.path.join(BUILD, '*.docx'))):
        base = os.path.splitext(os.path.basename(f))[0]
        pdf = os.path.join(PDF, base + '.pdf')
        doc = app.Documents.Open(f, ReadOnly=True)
        doc.SaveAs2(pdf, FileFormat=17)
        doc.Close(False)
        d = pymupdf.open(pdf)
        print(base, 'pages:', len(d))
        for i, page in enumerate(d):
            pix = page.get_pixmap(dpi=100)
            pix.save(os.path.join(PDF, base + '_p%d.png' % (i + 1)))
        d.close()
finally:
    app.Quit()
print('done')
