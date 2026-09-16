# -*- coding: utf-8 -*-
import docx, os
BUILD = r'C:\Users\youxi\Documents\shujvfa\sample_data\case_packages\_work\build2'
EA = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia'
for name in sorted(os.listdir(BUILD)):
    d = docx.Document(os.path.join(BUILD, name))
    sec = d.sections[0]
    pw = round(sec.page_width.cm, 2)
    ph = round(sec.page_height.cm, 2)
    m = tuple(round(x.cm, 2) for x in [sec.top_margin, sec.bottom_margin, sec.left_margin, sec.right_margin])
    print('====', name, '| page', pw, 'x', ph, '| margins', m)
    cnt = 0
    for p in d.paragraphs:
        if not p.text.strip():
            continue
        r = p.runs[0]
        rPr = r._element.rPr
        ea = None
        if rPr is not None and rPr.rFonts is not None:
            ea = rPr.rFonts.get(EA)
        sz = r.font.size.pt if r.font.size else None
        ls = p.paragraph_format.line_spacing
        lspt = ls.pt if hasattr(ls, 'pt') and ls else None
        ind = None
        pPr = p._p.pPr
        if pPr is not None:
            i = pPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ind')
            if i is not None:
                ind = i.get(EA.replace('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia', 'w:firstLineChars'))
        print('  %-16s | %-16s | %s | %spt | ls=%s | fl=%s' % (name[:6], p.text[:16], ea, sz, lspt, ind))
        cnt += 1
        if cnt > 14:
            break
