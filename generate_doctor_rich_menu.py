# coding: utf-8
import sys, os
from PIL import Image, ImageDraw, ImageFont

# Canvas dimensions (LINE standard: 2500 x 1686)
W, H = 2500, 1686
img = Image.new("RGB", (W, H), "#F8FAFC")
draw = ImageDraw.Draw(img)

# Fonts
font_path_bold = "C:/Windows/Fonts/msjhbd.ttc"
font_path_reg = "C:/Windows/Fonts/msjh.ttc"

f_title_main = ImageFont.truetype(font_path_bold, 54)
f_tile_title = ImageFont.truetype(font_path_bold, 56)
f_tile_sub = ImageFont.truetype(font_path_reg, 32)
f_badge = ImageFont.truetype(font_path_bold, 28)
f_header_badge = ImageFont.truetype(font_path_bold, 28)

# Palette
DARK_PURPLE = "#2A1845" # Novartis Kisqali Royal Purple
ACCENT_GOLD = "#D4AF37" # Gold
CARD_BG = "#FFFFFF"
BORDER_COLOR = "#E2E8F0"
TEXT_DARK = "#0F172A"
TEXT_MUTED = "#64748B"

# Top Banner (height: 140px)
BANNER_H = 140
draw.rectangle([(0, 0), (W, BANNER_H)], fill=DARK_PURPLE)
# Gold bottom accent bar
draw.rectangle([(0, BANNER_H - 8), (W, BANNER_H)], fill=ACCENT_GOLD)

# Banner Title
draw.text((60, 42), "擊癌利 (Kisqali®) 健保事前審查智能小幫手", fill="#FFFFFF", font=f_title_main)
draw.text((W - 650, 52), "115/12/1 擴大給付・醫師專用版", fill=ACCENT_GOLD, font=f_header_badge)

# Grid layout: 2 rows x 3 columns
ROW_H = 740
PAD_X = 36
PAD_Y = 24
COL_W = (W - PAD_X * 4) // 3 # approx 785

tiles = [
    # Row 1
    {
        "row": 0, "col": 0,
        "badge_top": "【 核心入口 】",
        "title": "AI 語音審查判讀",
        "sub": "口述或打字輸入臨床條件\n3 秒即時判讀資格與代碼",
        "tag": "C50P1 / C50R1 語音快篩",
        "tag_color": "#10B981"
    },
    {
        "row": 0, "col": 1,
        "badge_top": "【 正式產出 】",
        "title": "事審表一鍵列印",
        "sub": "法定七欄標準事前申請書\n自動帶入醫師代碼與藥品",
        "tag": "A4 即印 ｜ 轉存 PDF",
        "tag_color": "#0284C7"
    },
    {
        "row": 0, "col": 2,
        "badge_top": "【 法規規章 】",
        "title": "六大給付情境速查",
        "sub": "115/12/1 擴大給付新制\n早期/晚期/二線條文對照",
        "tag": "ER 30% vs 1% 門檻速查",
        "tag_color": "#D97706"
    },
    # Row 2
    {
        "row": 1, "col": 0,
        "badge_top": "【 視覺辨識 】",
        "title": "病理報告拍照辨識",
        "sub": "手機拍照上傳英文報告\nAI 自動讀取 ER/PR/HER2",
        "tag": "英文切片單 Vision 辨識",
        "tag_color": "#059669"
    },
    {
        "row": 1, "col": 1,
        "badge_top": "【 雙藥配置 】",
        "title": "停經針／法洛德品項",
        "sub": "各院進藥品項一鍵切換\n東洋伏斯壯/法洛德/諾曼德",
        "tag": "東洋 AC61825221 預設",
        "tag_color": "#EA580C"
    },
    {
        "row": 1, "col": 2,
        "badge_top": "【 病友方案 】",
        "title": "擊癌利自費存摺專案",
        "sub": "未符健保時自費支持方案\n買一送一／買四送八補助",
        "tag": "防退件救濟 ｜ 專案表單",
        "tag_color": "#BE185D"
    }
]

for t in tiles:
    x0 = PAD_X + t["col"] * (COL_W + PAD_X)
    y0 = BANNER_H + PAD_Y + t["row"] * (ROW_H + PAD_Y)
    x1 = x0 + COL_W
    y1 = y0 + ROW_H

    # Draw card shadow & body
    draw.rounded_rectangle([(x0, y0), (x1, y1)], radius=36, fill=CARD_BG, outline=BORDER_COLOR, width=4)
    
    # Top header bar for card
    HEADER_BAR_H = 100
    draw.rounded_rectangle([(x0, y0), (x1, y0 + HEADER_BAR_H + 20)], radius=36, fill=DARK_PURPLE)
    draw.rectangle([(x0, y0 + HEADER_BAR_H - 10), (x1, y0 + HEADER_BAR_H + 20)], fill=DARK_PURPLE)
    
    # Top badge in card
    draw.text((x0 + 40, y0 + 32), t["badge_top"], fill="#FFFFFF", font=f_header_badge)
    
    # Gold accent line under card header
    draw.line([(x0, y0 + HEADER_BAR_H + 20), (x1, y0 + HEADER_BAR_H + 20)], fill=ACCENT_GOLD, width=3)

    # Card Title
    title_y = y0 + HEADER_BAR_H + 70
    draw.text((x0 + 40, title_y), t["title"], fill=TEXT_DARK, font=f_tile_title)

    # Subtitle lines
    sub_y = title_y + 90
    draw.text((x0 + 40, sub_y), t["sub"], fill=TEXT_MUTED, font=f_tile_sub, spacing=16)

    # Bottom Tag Badge Box
    tag_box_h = 70
    tag_box_y = y1 - tag_box_h - 45
    tag_box_w = COL_W - 80
    draw.rounded_rectangle([(x0 + 40, tag_box_y), (x0 + 40 + tag_box_w, tag_box_y + tag_box_h)], radius=18, fill="#F8FAFC", outline=t["tag_color"], width=3)
    draw.text((x0 + 65, tag_box_y + 18), t["tag"], fill=t["tag_color"], font=f_badge)

output_path = "kiscare/rich_menu_doctor_2500x1686.png"
img.save(output_path, "PNG")
print(f"Generated Rich Menu image: {output_path} ({W}x{H})")
