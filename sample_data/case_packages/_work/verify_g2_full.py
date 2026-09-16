# -*- coding: utf-8 -*-
import docx, re
BUILD = r'C:\Users\youxi\Documents\shujvfa\sample_data\case_packages\_work\build2'
BACK = r'C:\Users\youxi\Documents\shujvfa\sample_data\case_packages\_work\backup\GOLD_CASE_002\visible\documents'


def doc_paras(path):
    d = docx.Document(path)
    return [p.text for p in d.paragraphs if p.text.strip()]


def norm(s):
    return re.sub(r'\s+', '', s)


def fuzzy_in(frag, text):
    f = norm(frag)
    t = norm(text)
    if f in t:
        return True
    chars = [c for c in f if c not in '，。；、“”（）']
    if len(chars) <= 6:
        return ''.join(chars) in t
    nums = re.findall(r'[\d]+万[元]?|2025年\d月\d日|[34567]月\d日', f)
    if nums and all(n in t for n in nums):
        return True
    quotes = re.findall(r'“[^”]+”', f)
    if quotes and all(q in t for q in quotes):
        return True
    return False


pairs = [
    ('01_起诉书.docx', '01_indictment.docx'),
    ('03_刘某陈述.docx', '03_victim_liu_statement.docx'),
    ('04_周某陈述.docx', '04_victim_zhou_statement.docx'),
    ('05_郑某陈述.docx', '05_victim_zheng_statement.docx'),
    ('06_证人证言.docx', '06_witness_statements.docx'),
    ('07_被告人供述与辩解.docx', '07_defendant_statements.docx'),
]
SKIP = ['完全虚构', '询问笔录节选', '三次讯问笔录节选', '审查提示', '案件编号',
        '涉嫌罪名', '被害人：', '陈述人', '关联主张', 'GOLD_CASE', '（虚构）',
        '被害人刘某陈述', '被害人周某陈述', '被害人郑某陈述', '被告人陈某供述与辩解', '证人证言汇编', '起诉书']

KEY = {
    '01_起诉书.docx': ['陈某', '刘某', '周某', '郑某', '240万元', '180万元', '160万元', '120万元',
                      '700万元', '孙某', '马某', '马某甲', '韩某', '赵某', '高某', '许某',
                      '3月20日', '4月12日', '6月5日', '5月1日', '诈骗罪', '第二百六十六条',
                      '第一百七十六条', '京某检刑诉〔2026〕87号'],
    '03_刘某陈述.docx': ['240万元', '80万元', '3月20日', '4月6日', '4月25日', '120万元', '5月2日',
                        '60万元', '5月12日', '马会计', '马某', '30万元', '收益', '15万元',
                        '退款', '25万元', '周转', '8月3日'],
    '04_周某陈述.docx': ['180万元', '4月12日', '4月22日', '4月16日', '孙某', '25万元', '本金', '8月4日'],
    '05_郑某陈述.docx': ['160万元', '6月5日', '80万元', '6月12日', '马某', '马会计', '20万元', '项目分红', '8月5日'],
    '06_证人证言.docx': ['60万元', 'A105', '140万元', '15万元', '95万元', '70万元', '30万元',
                        '40万元', '155万元', '马师傅', '5月9日', '5月18日', '5月21日', '内部投资份额'],
    '07_被告人供述与辩解.docx': ['360万元', '180万元', '160万元', '马会计', '210万元', '证券账户',
                              '设备采购', '完全虚构', '退回本金', '收益安排', '非法占有'],
}

allok = True
for new, old in pairs:
    new_text = ' '.join(doc_paras(BUILD + '\\' + new))
    old_paras = doc_paras(BACK + '\\' + old)
    fails = []
    for p in old_paras:
        if any(s in p for s in SKIP):
            continue
        for sent in re.split(r'(?<=[。；])', p):
            sent = sent.strip()
            if len(norm(sent)) < 8:
                continue
            if not fuzzy_in(sent, new_text):
                fails.append(sent)
    missk = [k for k in KEY[new] if k not in new_text]
    if fails or missk:
        allok = False
        print(new, 'FAIL')
        for f in fails:
            print('   FACT?', f[:60])
        if missk:
            print('   MISSING KEY:', missk)
    else:
        print(new, 'OK')
print('ALL', 'OK' if allok else 'FAIL')
