# -*- coding: utf-8 -*-
import docx
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

def set_cell_margins(cell, top=100, bottom=100, left=150, right=150):
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    tcMar = OxmlElement('w:tcMar')
    for m, val in [('top', top), ('bottom', bottom), ('left', left), ('right', right)]:
        node = OxmlElement(f'w:{m}')
        node.set(qn('w:w'), str(val))
        node.set(qn('w:type'), 'dxa')
        tcMar.append(node)
    tcPr.append(tcMar)

def create_nhi_form(filepath, hospital_name, hosp_code, doctor_name, patient_name, patient_id, dob,
                    diag_code, reason_text, items, is_ebc=True):
    doc = Document()
    
    # Set narrow margins (0.5 inch / 36 pt)
    sections = doc.sections
    for s in sections:
        s.top_margin = Inches(0.5)
        s.bottom_margin = Inches(0.5)
        s.left_margin = Inches(0.6)
        s.right_margin = Inches(0.6)
        
    # Title Header
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run("附件一 (第一聯：保險人審查聯 / 第二聯：核定通知聯)")
    run.font.size = Pt(8.5)
    run.font.color.rgb = RGBColor(100, 100, 100)
    
    p_title = doc.add_paragraph()
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_title = p_title.add_run("全民健康保險藥品及特殊材料事前審查申請書")
    r_title.font.bold = True
    r_title.font.size = Pt(15)
    
    p_sub = doc.add_paragraph()
    p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_sub.paragraph_format.space_after = Pt(6)
    r_sub = p_sub.add_run("☑ 藥品    ☑ 事前審查申請書    ☑ 送核 (自 115 年 12 月 1 日生效新制)")
    r_sub.font.bold = True
    r_sub.font.size = Pt(10)
    r_sub.font.color.rgb = RGBColor(180, 40, 40)
    
    # Main Table
    table = doc.add_table(rows=6, cols=6)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    
    # Row 0: Hosp & Doctor info
    # Col 0: 醫事機構 (width 1.2 in), Col 1-2: value (width 2.3 in), Col 3: 主治醫師 (width 1.0 in), Col 4-5: value (width 2.5 in)
    row0 = table.rows[0]
    row0.cells[0].text = "醫事服務機構\n名稱及代號"
    row0.cells[1].text = f"{hospital_name}\n(代號：{hosp_code})"
    # Merge col 1 & 2
    row0.cells[1].merge(row0.cells[2])
    row0.cells[3].text = "主治醫師\n姓名"
    row0.cells[4].text = f"{doctor_name} 醫師\n科別：一般外科 / 乳房外科"
    row0.cells[4].merge(row0.cells[5])
    
    # Row 1: Patient info
    row1 = table.rows[1]
    row1.cells[0].text = "保險對象\n姓名"
    row1.cells[1].text = patient_name
    row1.cells[2].text = "身分證號"
    row1.cells[3].text = patient_id
    row1.cells[4].text = "出生年月日"
    row1.cells[5].text = dob
    
    # Row 2: Diagnosis & ICD
    row2 = table.rows[2]
    row2.cells[0].text = "國際疾病\n分類碼"
    row2.cells[1].text = diag_code
    row2.cells[2].text = "預定實施\n日期"
    row2.cells[3].text = "115 年 12 月 01 日"
    row2.cells[4].text = "原受理編號"
    row2.cells[5].text = "(初次送核免填)"
    
    # Row 3: Reason & Justification (Merged cols 1-5)
    row3 = table.rows[3]
    row3.cells[0].text = "傷病名稱\n傷病情況\n及使用理由"
    row3.cells[1].text = reason_text
    # Merge cells across 1 to 5
    for c in range(2, 6):
        row3.cells[1].merge(row3.cells[c])
        
    # Row 4: Items header
    row4 = table.rows[4]
    headers = ["項次", "項目編號 (事審代碼)", "項目名稱 (藥品名)", "規格、劑型及用法", "廠牌", "數量 (24週)"]
    for i, h in enumerate(headers):
        row4.cells[i].text = h
        
    # Row 5: Item 1
    row5 = table.rows[5]
    it1 = items[0]
    row5.cells[0].text = "1"
    row5.cells[1].text = it1["code"]
    row5.cells[2].text = it1["name"]
    row5.cells[3].text = it1["dosage"]
    row5.cells[4].text = it1["brand"]
    row5.cells[5].text = it1["qty"]
    
    # If items > 1, add extra row
    if len(items) > 1:
        it2 = items[1]
        row_extra = table.add_row()
        row_extra.cells[0].text = "2"
        row_extra.cells[1].text = it2["code"]
        row_extra.cells[2].text = it2["name"]
        row_extra.cells[3].text = it2["dosage"]
        row_extra.cells[4].text = it2["brand"]
        row_extra.cells[5].text = it2["qty"]
        
    # Format Table cells styling
    for row in table.rows:
        for cell in row.cells:
            set_cell_margins(cell, top=80, bottom=80, left=100, right=100)
            for paragraph in cell.paragraphs:
                for r in paragraph.runs:
                    r.font.size = Pt(9)
                    r.font.name = 'Microsoft JhengHei'
                    
    # Bottom Attachments & Signatures
    p_att = doc.add_paragraph()
    p_att.paragraph_format.space_before = Pt(8)
    p_att.paragraph_format.space_after = Pt(4)
    r_att = p_att.add_run("檢送附件：☑ 1.病歷影本  ☑ 5.病歷摘要  ☑ 7.治療計劃  ☑ 11.其他：病理組織切片報告、IHC 免疫染色報告")
    r_att.font.size = Pt(8.5)
    r_att.font.bold = True
    
    p_sign = doc.add_paragraph()
    p_sign.paragraph_format.space_before = Pt(12)
    r_sign = p_sign.add_run(f"醫事服務機構負責醫師印章：____________      主治醫師簽章：【 {doctor_name} 】     申請日期：115 年 12 月 01 日")
    r_sign.font.size = Pt(9.5)
    r_sign.font.bold = True
    
    doc.save(filepath)
    print(f"Generated: {filepath}")

# ==================== Generate 3 Concrete Demos ====================

# 1. Keelung Chang Gung - Early Breast Cancer T2N1
reason_1 = (
    "【病歷摘要與使用理由】：\n"
    "1. 診斷：侵襲性乳癌 (Invasive Ductal Carcinoma, T2N1M0, Stage IIB)。\n"
    "2. 病理報告數值：\n"
    "   • 腫瘤最大徑：2.4 公分 (符合 T2 > 2cm)\n"
    "   • 淋巴結轉移：2 / 16 顆陽性 (符合 pALN 1-3 顆)\n"
    "   • 荷爾蒙受體：ER 85% 陽性、PR 70% 陽性 (符合 ER 或 PR > 30%)\n"
    "   • HER-2 檢測：IHC 1+ (陰性)\n"
    "   • 細胞分化：Histologic Grade 2\n"
    "3. 健保給付合規比對：\n"
    "   本案完全符合 115 年 12 月 1 日健保修訂規定 9.72 第 4 點之早期乳癌高復發風險條件 II「pALN 為 1-3 顆，且腫瘤大小 > 2cm」。\n"
    "4. 病患已完成標準化學輔助治療，手術後未滿 16 個月，內分泌治療未滿 12 週，特此申請 ribociclib (事審代碼：C50P1) 併用芳香環轉化酶抑制劑 (AI) 進行輔助治療。"
)
item_1 = [{
    "code": "C50P1 / KC01004100",
    "name": "擊癌利膜衣錠 200毫克\n(Kisqali film-coated tabs)",
    "dosage": "200mg/錠，口服每日 2 粒 (400mg)\n服 21 天休 7 天，28 天為一週期",
    "brand": "台灣諾華 (Novartis)",
    "qty": "252 粒\n(24週/6週期)"
}]
create_nhi_form(
    r"C:\Users\Sandy Huang\OneDrive\Desktop\範例1_基隆長庚_李春輝醫師_早期乳癌C50P1事前審查申請書.docx",
    "長庚醫療財團法人基隆長庚紀念醫院及其情人湖院區",
    "1111060015",
    "李春輝",
    "王○蘭 (範例病友)",
    "A22019**** (去識別化)",
    "052 年 08 月 15 日",
    "C50.919",
    reason_1,
    item_1,
    is_ebc=True
)

# 2. TSGH - Premenopausal MBC first line + Zoladex
reason_2 = (
    "【病歷摘要與使用理由】：\n"
    "1. 診斷：乳癌合併骨、肺遠端轉移 (Stage IV)。\n"
    "2. 臨床特徵與病理標記：\n"
    "   • 停經狀態：停經前女性 (年齡 42 歲)\n"
    "   • 荷爾蒙受體：ER 90%、PR 80% (符合 12/1 新制放寬之 ER 或 PR >= 1%)\n"
    "   • HER-2 檢測：IHC 0 (陰性)\n"
    "   • 轉移評估：無器官轉移危急症狀 (non-visceral crisis)，無中樞神經 (CNS) 轉移，非單純骨轉移。\n"
    "3. 健保給付合規比對：\n"
    "   符合 115 年 12 月 1 日健保修訂規定 9.72 第 2 點及 8.2.1 節，停經前轉移性乳癌須併用芳香環轉化酶抑制劑及 GnRH analogue。\n"
    "4. 特此提出事前審查申請：合併使用 ribociclib (事審代碼：C50R1，每日 3 粒) 及停經針 goserelin (Zoladex 3.6mg，每 28 天皮下注射 1 劑)。"
)
item_2 = [
    {
        "code": "C50R1 / KC01004100",
        "name": "擊癌利膜衣錠 200毫克\n(Kisqali film-coated tabs)",
        "dosage": "200mg/錠，口服每日 3 粒 (600mg)\n服 21 天休 7 天，28 天為一週期",
        "brand": "台灣諾華 (Novartis)",
        "qty": "378 粒\n(24週用量)"
    },
    {
        "code": "B018244243",
        "name": "諾曼德持續性注射劑 3.6毫克\n(Zoladex Depot 3.6mg)",
        "dosage": "3.6mg/針，每 28 天皮下注射 1 劑",
        "brand": "台灣阿斯特捷利康 (AstraZeneca)",
        "qty": "6 支\n(24週用量)"
    }
]
create_nhi_form(
    r"C:\Users\Sandy Huang\OneDrive\Desktop\範例2_三軍總醫院_晚期停經前Zoladex_C50R1事前審查申請書.docx",
    "三軍總醫院附設民眾診療服務處 (內湖總院/汀州院區)",
    "0501110514",
    "主治醫師",
    "陳○美 (範例病友)",
    "F22134**** (去識別化)",
    "073 年 03 月 20 日",
    "C50.919",
    reason_2,
    item_2,
    is_ebc=False
)

# 3. Keelung Chang Gung - 2nd line Fulvestrant (TTY Fustron)
reason_3 = (
    "【病歷摘要與使用理由】：\n"
    "1. 診斷：轉移性乳癌 (Stage IV)。\n"
    "2. 治療病史與病理受體：\n"
    "   • 荷爾蒙受體：ER 75%、PR 50% (符合 ER 或 PR > 30%)\n"
    "   • HER-2 檢測：IHC 1+ (陰性)\n"
    "   • 內分泌病史：先前曾接受第一線芳香環轉化酶抑制劑 (Letrozole) 治療，近期追蹤電腦斷層顯示疾病惡化 (PD)。\n"
    "   • 轉移評估：無器官危急症狀、無 CNS 轉移、非單純骨轉移。\n"
    "3. 健保給付合規比對：\n"
    "   完全符合 115 年 12 月 1 日健保最新解禁規定 9.72 第 3 點：ribociclib 與 fulvestrant 併用於曾接受過內分泌療法惡化之轉移性乳癌 (事審代碼：C50R2)。\n"
    "4. 特此提出事前審查申請：合併使用 ribociclib (每日 3 粒) 及 fulvestrant (院聘台灣東洋伏斯壯 Fustron，每次 500mg 肌注)。"
)
item_3 = [
    {
        "code": "C50R2 / KC01004100",
        "name": "擊癌利膜衣錠 200毫克\n(Kisqali film-coated tabs)",
        "dosage": "200mg/錠，口服每日 3 粒 (600mg)\n服 21 天休 7 天，28 天為一週期",
        "brand": "台灣諾華 (Novartis)",
        "qty": "378 粒\n(24週用量)"
    },
    {
        "code": "AC61825221",
        "name": "伏斯壯注射液 50毫克/毫升\n(Fustron 250mg/5mL)",
        "dosage": "每次 500mg (2支) 臀部肌肉注射\n(D1, D15, D29 及後續每月一次)",
        "brand": "台灣東洋藥品工業股份有限公司",
        "qty": "16 支\n(含第1個月負荷劑量)"
    }
]
create_nhi_form(
    r"C:\Users\Sandy Huang\OneDrive\Desktop\範例3_基隆長庚_二線東洋伏斯壯_C50R2事前審查申請書.docx",
    "長庚醫療財團法人基隆長庚紀念醫院及其情人湖院區",
    "1111060015",
    "吳宗翰",
    "林○君 (範例病友)",
    "C22098**** (去識別化)",
    "049 年 11 月 05 日",
    "C50.919",
    reason_3,
    item_3,
    is_ebc=False
)
