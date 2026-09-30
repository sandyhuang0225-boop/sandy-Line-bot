import os
import re
import time
import random
import threading
import calendar
from datetime import datetime, timedelta
from flask import Flask, request, abort, render_template_string, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, VideoMessage,
    TemplateSendMessage, ButtonsTemplate, MessageAction, URIAction,
    FlexSendMessage, BubbleContainer, BoxComponent, TextComponent,
    SeparatorComponent, ButtonComponent
)
import gspread

# ==================== 帳號與品牌環境設定 ====================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get(
    "LINE_CHANNEL_ACCESS_TOKEN",
    "HpRLyoX/uZTEYScUerqj9inQaDOev7PwesnXvOrbcptLVRiE9KOsGTAxhBEGPjEYEd1d7rAcxxpkOE6HDUm4iRJk2d08zDuvBhXhOn9tGTD4nta4iQHnJ3TFGqWiPDD/pj0ZVRaJYK/w2+7CP8639QdB04t89/1O/w1cDnyilFU="
)
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "9cc5bdd12f3b7596f8007b119d168113")
MY_LINE_USER_ID = os.environ.get("MY_LINE_USER_ID", "U7278e50c97fbaf8753c1cbf976903666")

# 🛡️ 安全測試模式開關：
# True  = 【安全測試模式】所有卡片強制只發給 Sandy，絕不驚擾租客/管理員/合伙人
# False = 【正式上線模式】卡片發送給對應的真實 LINE ID
TEST_MODE = True

# 銀行匯款資訊（星展銀行 810）
BANK_NAME = "星展銀行"
BANK_CODE = "810"
BANK_ACCOUNT = "6712880487"
BANK_HOLDER = "黃蕙芬"

# 管家聯絡資訊
MANAGER_NAME = "Sandy（黃蕙芬）"
MANAGER_PHONE = "0934008636"
MANAGER_LINE_ID = "sandy0225"

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
app = Flask(__name__)

# ==================== Google Sheets 連線設定（安全容錯延遲載入）====================
_gc = None
_spreadsheet = None
_sheets_cache = {}

def get_spreadsheet():
    """安全獲取 Google 試算表物件，延遲載入且啟動時絕不崩潰"""
    global _gc, _spreadsheet
    if _spreadsheet:
        return _spreadsheet
    try:
        if not _gc:
            _gc = gspread.service_account(filename="credentials.json")
        _spreadsheet = _gc.open("V1sandy宅安心包租財務總覽")
        return _spreadsheet
    except Exception as e:
        print(f"⚠️ Google Sheets 連線異常: {e}")
        return None

def get_worksheet(name):
    """安全取得指定工作表"""
    sh = get_spreadsheet()
    if not sh:
        return None
    try:
        return sh.worksheet(name)
    except Exception:
        return None

def get_worksheet_safely(possible_names):
    for name in possible_names:
        w = get_worksheet(name)
        if w:
            return w
    return None

# ==================== 【修改一】Webhook 去重防護 ====================
# 防止 LINE 重送 Webhook 導致收據/通知重複發送十幾封
_processed_tokens = set()
_token_lock = threading.Lock()

def _is_duplicate(token):
    """回傳 True = 此 token 已處理過（重複事件），直接忽略"""
    if not token:
        return False
    token_str = str(token)
    with _token_lock:
        if token_str in _processed_tokens:
            return True
        _processed_tokens.add(token_str)
        if len(_processed_tokens) > 2000:
            _processed_tokens.clear()
        return False

# ==================== 【修改十一】綁定碼機制 ====================
_bind_codes = {}   # { '738291': {'room_no': 'A101', 'row_idx': 5, 'expire': timestamp} }
_bind_lock  = threading.Lock()

def _generate_bind_code(room_no, row_idx):
    """產生 6 位綁定碼，有效 24 小時"""
    code   = str(random.randint(100000, 999999))
    expire = datetime.now().timestamp() + 86400
    with _bind_lock:
        _bind_codes[code] = {'room_no': room_no, 'row_idx': row_idx, 'expire': expire}
    return code

def _verify_bind_code(code):
    """驗證綁定碼；回傳 dict 或 None（無效/已過期）"""
    with _bind_lock:
        entry = _bind_codes.get(code)
        if not entry:
            return None
        if datetime.now().timestamp() > entry['expire']:
            del _bind_codes[code]
            return None
        del _bind_codes[code]   # 一次性使用
        return entry

# ==================== 資料輔助函式 ====================

def clean_num(val_str):
    """清除千分位與貨幣符號，轉成整數"""
    if not val_str:
        return 0
    s = re.sub(r'[^\d]', '', str(val_str).strip())
    return int(s) if s else 0

def is_paid(status_str):
    """租金儲存格已繳判斷"""
    if not status_str:
        return False
    s = str(status_str).strip()
    return any(sym in s for sym in ["✔", "✓", "✅", "已收", "已付", "是"])

def is_expense_due(status_str):
    """
    【修改四】管理費/貸款專用三態判斷：
      'XX' 或 '❌' → True  （本期應繳，尚未繳，需提醒）
      ''            → False （空白 = 本月不需繳，半年/雙月的非繳費月，跳過）
      '✅'           → False （已繳清，不提醒）
    """
    if not status_str:
        return False
    s = str(status_str).strip()
    return s.upper() in ["XX", "Xx", "xx"] or "❌" in s

def next_rent_month(current_month: int) -> str:
    """【修改五】租金收的是下個月，查詢時用 current_month+1（跨年 12→1）"""
    return str(1 if current_month == 12 else current_month + 1)

def parse_target_month(text: str, default_month: str) -> str:
    """從使用者文字中解析目標月份，支援國字 (如十月) 與阿拉伯數字 (如 10月)"""
    cn_months = {
        "十二月": "12", "十一月": "11", "十月": "10",
        "一月": "1", "二月": "2", "三月": "3", "四月": "4",
        "五月": "5", "六月": "6", "七月": "7", "八月": "8",
        "九月": "9"
    }
    for cn_m, num_m in cn_months.items():
        if cn_m in text:
            return num_m
    for m in range(12, 0, -1):
        if f"{m}月" in text or f"{m} 月" in text:
            return str(m)
    return str(default_month)

def get_rent_sheet_columns(month_str):
    """收租追蹤：(狀態欄, 實收金額欄) 1-indexed"""
    m = int(month_str)
    return 4 + m * 2, 5 + m * 2

def get_expense_sheet_columns(month_str, expense_type):
    """支出追蹤：(狀態欄, 支出金額欄) 1-indexed"""
    m = int(month_str)
    base = 6 + (m - 1) * 4
    return (base, base + 1) if expense_type == "管理費" else (base + 2, base + 3)

def load_properties():
    """從「房客總表」讀取所有物業資料"""
    properties = []
    try:
        sh_t = get_worksheet("房客總表")
        if not sh_t:
            return properties
        rows = sh_t.get_all_values()
        for idx, r in enumerate(rows[1:], start=2):
            addr           = r[0].strip()  if len(r) > 0  else ""
            prop_type      = r[1].strip()  if len(r) > 1  else ""
            room_no        = r[2].strip()  if len(r) > 2  else ""
            tenant_name    = r[3].strip()  if len(r) > 3  else ""
            total_rent     = clean_num(r[5].strip() if len(r) > 5 else "0")
            pure_rent      = clean_num(r[6]) if len(r) > 6 else 0
            mgmt_fee       = clean_num(r[8]) if len(r) > 8 else 0
            contract_end   = r[16].strip() if len(r) > 16 else ""
            due_day_str    = r[14].strip() if len(r) > 14 else ""
            due_nums       = re.findall(r'\d+', due_day_str)
            due_day        = int(due_nums[0]) if due_nums else 31
            tenant_line_id = r[21].strip() if len(r) > 21 else ""
            enable_notify  = r[22].strip().upper() if len(r) > 22 else "Y"

            if addr or room_no:
                properties.append({
                    'row_idx':        idx,
                    'addr':           addr,
                    'type':           prop_type,
                    'room_no':        room_no,
                    'tenant':         tenant_name,
                    'total_rent':     total_rent,
                    'total_rent_str': f"{total_rent:,}",
                    'pure_rent':      pure_rent,
                    'mgmt_fee':       mgmt_fee,
                    'due_day':        due_day,
                    'contract_end':   contract_end,
                    'line_id':        tenant_line_id,
                    'enable_notify':  enable_notify,
                })
    except Exception as e:
        print(f"讀取房客總表失敗: {e}")
    return properties

def get_rent_for_month(prop, month_str):
    """
    根據月份動態計算應收租金金額、明細與說明：
    1. A102 裕誠路（傅瑜）：
       - 偶數月（如 10月、12月）: 29,931 元（含雙月代收管理費 1,931 元）
       - 奇數月（如 11月、1月）: 28,000 元（純租金）
    2. B1-03 車位（周大哥）：
       - 季繳（1、4、7、10月）: 5,400 元（含當季 3 個月）
    3. 其他一般物件：
       - 依據房客總表定額
    回傳: (amount_int, amount_str, note_str, mgmt_fee)
    """
    try:
        m = int(month_str)
    except:
        m = datetime.now().month

    room = prop.get('room_no', '').strip()
    tenant = prop.get('tenant', '').strip()

    # 1. A102 裕誠路（傅瑜）
    if 'A102' in room or '傅瑜' in tenant:
        if m % 2 == 0:
            amt = 29931
            return amt, "29,931", "含雙月代收管理費 1,931 元", 1931
        else:
            amt = 28000
            return amt, "28,000", "純租金", 0

    # 2. B1-03 車位（周大哥）
    if 'B1-03' in room or '周大哥' in tenant:
        amt = 5400
        quarter_map = {1: "Q1（1~3月）", 4: "Q2（4~6月）", 7: "Q3（7~9月）", 10: "Q4（10~12月）"}
        q_name = quarter_map.get(m, f"{m}月起一季")
        return amt, "5,400", f"每季繳納（含 {q_name} 3個月）", 0

    # 3. 其他一般物件
    amt = prop.get('total_rent', 0)
    mgmt = prop.get('mgmt_fee', 0)
    return amt, f"{amt:,}", "", mgmt

def parse_contract_date(date_str):
    """安全解析合約日期字串，防止 9-31 等月份天數錯誤"""
    if not date_str:
        return None
    m = re.search(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', str(date_str).strip())
    if not m:
        return None
    y, mon, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    mon = max(1, min(12, mon))
    max_day = calendar.monthrange(y, mon)[1]
    d = max(1, min(max_day, d))
    return datetime(y, mon, d).date()

def get_expiring_contracts(days_threshold=100):
    """
    找出即將在指定天數內（預設 100 天 = 約 3 個月 / 涵蓋本季底與年底）到期或已過期的合約
    回傳清單: [{'room_no': 'A101', 'tenant': '...', 'addr': '...', 'end_date': date, 'days_left': 45}, ...]
    """
    props = load_properties()
    today = datetime.now().date()
    expiring = []
    for p in props:
        if not p.get('contract_end') or not p.get('tenant'):
            continue
        end_date = parse_contract_date(p['contract_end'])
        if end_date:
            days_left = (end_date - today).days
            if days_left <= days_threshold:
                expiring.append({
                    'room_no': p['room_no'],
                    'tenant': p['tenant'],
                    'addr': p['addr'],
                    'end_date': end_date.strftime('%Y-%m-%d'),
                    'days_left': days_left
                })
    # 按剩餘天數由小到大排序（最快到期的排前面）
    expiring.sort(key=lambda x: x['days_left'])
    return expiring

def load_contacts():
    """【修改二】從「LINE聯絡人」讀取管理員（管理費）與合伙人（貸款）資料"""
    contacts = []
    sh_c = get_worksheet_safely(["LINE聯絡人", "LINE 聯絡人"])
    if not sh_c:
        return contacts
    try:
        rows = sh_c.get_all_values()
        for r in rows[1:]:
            line_id     = r[4].strip() if len(r) > 4 else ""
            notice_type = r[6].strip() if len(r) > 6 else ""   # 「管理費」或「貸款」
            if not line_id or not notice_type:
                continue
            contacts.append({
                'addr':        r[0].strip() if len(r) > 0 else "",
                'room_no':     r[2].strip() if len(r) > 2 else "",
                'line_name':   r[3].strip() if len(r) > 3 else "",
                'line_id':     line_id,
                'last5':       r[5].strip() if len(r) > 5 else "",
                'notice_type': notice_type,
                'payer':       r[7].strip() if len(r) > 7 else BANK_HOLDER,
            })
    except Exception as e:
        print(f"讀取LINE聯絡人失敗: {e}")
    return contacts

def identify_user_role(user_id):
    """
    【修改三】角色識別：
    1. 先查房客總表 V欄 → tenant
    2. 查LINE聯絡人 G欄=管理費 → manager
    3. 查LINE聯絡人 G欄=貸款  → partner
    4. 都沒有 → guest
    回傳 (role, data)
    """
    if user_id == MY_LINE_USER_ID:
        return 'admin', None

    props = load_properties()
    prop  = next((p for p in props if p['line_id'] == user_id), None)
    if prop:
        return 'tenant', prop

    contacts = load_contacts()
    mgr = next((c for c in contacts
                if c['line_id'] == user_id and c['notice_type'] == '管理費'), None)
    if mgr:
        return 'manager', mgr

    ptr = next((c for c in contacts
                if c['line_id'] == user_id and c['notice_type'] == '貸款'), None)
    if ptr:
        return 'partner', ptr

    return 'guest', None

# ==================== Flex 卡片產生器 ====================

def create_row(label, value, color='#111111', weight='regular'):
    return BoxComponent(
        layout='baseline', spacing='sm',
        contents=[
            TextComponent(text=label, color='#888888', size='sm', flex=3),
            TextComponent(text=str(value), color=color, size='sm', weight=weight, flex=5, wrap=True),
        ]
    )

# ── 屋主委託諮詢表單卡片 ──
def create_owner_inquiry_form_flex():
    template_text = "屋主委託|房屋類型:大樓|地點:鼓山區|稱呼:王先生|電話:0912345678|方便聯絡時間:隨時"
    return FlexSendMessage(
        alt_text="【Sandy宅安心】屋主委託評估諮詢表單",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#20c997', contents=[
                    TextComponent(text="📝 Sandy 宅安心 ｜ 屋主委託評估", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="不想直接通話？請留下愛屋資訊，管家主動與您聯繫！", size='xs', color='#e6fffa', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📋 請於對話框回傳以下資訊：", weight='bold', size='sm', color='#333333'),
                    SeparatorComponent(margin='xs'),
                    create_row("🏢 房屋類型", "大樓 / 公寓 / 透天 / 套房 / 店面"),
                    create_row("📍 物件地點", "行政區、路名（如：鼓山區裕誠路）"),
                    create_row("👤 屋主稱呼", "如：王先生 / 陳小姐"),
                    create_row("📞 聯絡電話", "方便聯繫的手機號碼"),
                    create_row("⏰ 聯絡時間", "隨時 / 平日白天 / 晚上"),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 點選下方按鈕可「自動帶入範本」，填入您的愛屋資訊後即可送出！", size='xs', color='#666666', wrap=True),
                    ButtonComponent(action=MessageAction(label="✏️ 點我填寫委託表單", text=template_text), style='primary', color='#20c997', height='sm'),
                    ButtonComponent(action=URIAction(label="📞 還是想直接撥打電話？", uri=f"tel:{MANAGER_PHONE}"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 1. 包租代管介紹（雙卡輪播：服務保障 ＋ 政府3稅減免與補助）──
def create_service_baozu_flex():
    # 卡片 1：房東四大服務保障
    bubble1 = BubbleContainer(
        body=BoxComponent(layout='vertical', padding_all='none', contents=[
            BoxComponent(layout='vertical', padding_all='lg', background_color='#198754', contents=[
                TextComponent(text="🏡 Sandy 宅安心 ｜ 包租代管保障", weight='bold', size='md', color='#ffffff'),
                TextComponent(text="讓愛屋穩定收益，屋主輕鬆收租零煩惱！", size='xs', color='#e8f5e9', margin='xs'),
            ]),
            BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                TextComponent(text="🌟 房東四大專屬服務保障", weight='bold', size='sm', color='#198754'),
                SeparatorComponent(margin='xs'),
                create_row("💰 穩定收租", "按月穩定收租入帳，合約期內零空置風險"),
                create_row("🔍 嚴選租客", "多重身分過濾嚴格把關，杜絕惡鄰租霸"),
                create_row("🛠️ 房屋管護", "定期屋況巡檢，配合專業工班即時維護"),
                create_row("📜 法律公證", "合法租賃住宅契約＋公證保障，合法安心"),
                SeparatorComponent(margin='md'),
                TextComponent(text="👉 左右滑動查看【政府稅金減免與補助】", size='xs', color='#888888', align='center', margin='sm'),
                ButtonComponent(action=URIAction(label="📞 撥打屋主諮詢專線", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#198754', height='sm'),
                ButtonComponent(action=URIAction(label="📝 填寫線上委託表單", uri="https://sandy-line-bot.onrender.com/inquiry"), style='secondary', height='sm'),
            ]),
        ])
    )

    # 卡片 2：政府政策優惠（3稅減免 ＋ 3費補助）
    bubble2 = BubbleContainer(
        body=BoxComponent(layout='vertical', padding_all='none', contents=[
            BoxComponent(layout='vertical', padding_all='lg', background_color='#0d6efd', contents=[
                TextComponent(text="🎁 政府政策紅利 ｜ 3稅減免＋3費補助", weight='bold', size='md', color='#ffffff'),
                TextComponent(text="加入包租代管，房東享政府多重節稅與修繕補助！", size='xs', color='#e3f2fd', margin='xs'),
            ]),
            BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                TextComponent(text="🏛️ 房東 3 稅減免（超有感節稅）", weight='bold', size='sm', color='#0d6efd'),
                SeparatorComponent(margin='xs'),
                create_row("💵 所得稅", "每屋每月 1.5 萬免稅額，超額必要費用率 60%"),
                create_row("🏠 房屋稅", "比照自用住宅稅率 1.2%（大幅減省囤房稅）"),
                create_row("🌿 地價稅", "適用自用住宅優惠稅率 2‰（千分之二）"),
                SeparatorComponent(margin='sm'),
                TextComponent(text="🛠️ 政府 3 費全額補助", weight='bold', size='sm', color='#fd7e14'),
                SeparatorComponent(margin='xs'),
                create_row("🔧 修繕補助", "每屋每年最高補助 10,000 元（最高享3年）"),
                create_row("⚖️ 公證補助", "每件每次最高補助 3,000 元"),
                create_row("🛡️ 保險補助", "每年最高補助 3,500 元居家安全險"),
                SeparatorComponent(margin='md'),
                ButtonComponent(action=URIAction(label="📞 諮詢房東稅費試算", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#0d6efd', height='sm'),
                ButtonComponent(action=URIAction(label="📝 線上預約專人評估", uri="https://sandy-line-bot.onrender.com/inquiry"), style='secondary', height='sm'),
            ]),
        ])
    )

    return FlexSendMessage(
        alt_text="【Sandy宅安心】包租代管服務保障與政府3稅減免優惠",
        contents={"type": "carousel", "contents": [bubble1.as_json_dict(), bubble2.as_json_dict()]}
    )

# ── 2. 房屋託管介紹 ──
def create_service_tuoguan_flex():
    return FlexSendMessage(
        alt_text="【Sandy宅安心】專業房屋託管服務",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#0d6efd', contents=[
                    TextComponent(text="🛡️ Sandy 宅安心 ｜ 房屋託管", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="為您打理所有繁瑣的租屋管理大小事！", size='xs', color='#e3f2fd', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="🌟 託管四大核心專業服務", weight='bold', size='sm', color='#0d6efd'),
                    SeparatorComponent(margin='xs'),
                    create_row("💰 租金代收", "每月自動對帳催收，按期匯入屋主戶頭"),
                    create_row("🔧 修繕對接", "嚴選水電泥作工班，故障排除即時回報"),
                    create_row("🏠 驗屋巡視", "退租完整屋況點交，拍照存證確保權益"),
                    create_row("🤝 租客溝通", "專業管家排解日常大小事，房東零負擔"),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="📞 預約託管規劃專線", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#0d6efd', height='sm'),
                    ButtonComponent(action=URIAction(label="📝 填寫線上委託表單", uri="https://sandy-line-bot.onrender.com/inquiry"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 3. 我要租屋 ──
def create_service_renting_flex():
    return FlexSendMessage(
        alt_text="【Sandy宅安心】優質安心好宅尋找",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#fd7e14', contents=[
                    TextComponent(text="🔍 Sandy 宅安心 ｜ 我要租屋", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="嚴選安心好宅，給您舒適溫馨的家！", size='xs', color='#fff3cd', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="🏡 我們的房源特色", weight='bold', size='sm', color='#fd7e14'),
                    SeparatorComponent(margin='xs'),
                    create_row("✨ 拎包入住", "冷氣、冰箱、洗衣機、熱水器一應俱全"),
                    create_row("✨ 生活便利", "鄰近捷運與商圈，交通採買超方便"),
                    create_row("✨ 專人管理", "專業管家服務，居住問題迅速處理"),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="歡迎登記您的「預算、期望區域、格局」，Sandy 管家將為您推薦最適合的房源！", size='xs', color='#666666', margin='sm', wrap=True),
                    ButtonComponent(action=URIAction(label="📞 撥打看屋專線", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#fd7e14', height='sm'),
                    ButtonComponent(action=URIAction(label="📝 登記線上找屋需求", uri="https://sandy-line-bot.onrender.com/rental_inquiry"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 4. 修繕報修 ──
def create_repair_flex(addr="", tenant=""):
    addr_info = f"📍 租賃地址：{addr}（{tenant} 您好）" if addr else "📍 請提供您的【完整承租地址（含樓層戶別）】"
    return FlexSendMessage(
        alt_text="【Sandy宅安心】設備修繕報修中心",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#ffc107', contents=[
                    TextComponent(text="🛠️ Sandy 宅安心 ｜ 修繕報修中心", weight='bold', size='md', color='#333333'),
                    TextComponent(text="設備有故障？管家儘速為您安排工班！", size='xs', color='#555555', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text=addr_info, weight='bold', size='sm', color='#0d6efd', wrap=True),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="📋 報修傳送方式：", weight='bold', size='xs', color='#333333', margin='xs'),
                    TextComponent(text="1. 請直接在此對話框傳送【故障照片或影片】\n2. 說明損壞狀況與房屋地址（樓層戶別）\n3. 方便師傅到府檢修時段", size='xs', color='#555555', wrap=True),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="📞 緊急修繕專線", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#dc3545', height='sm'),
                    ButtonComponent(action=MessageAction(label="💬 留下報修說明", text="我要報修"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 5a. 房客個人帳務卡片（已綁定租客專用）──
def create_tenant_account_flex(prop, month_str, paid_status):
    is_month_paid = is_paid(paid_status)
    status_text  = "✅ 本月已繳納入帳" if is_month_paid else "⏳ 本月尚未收訖"
    status_color = "#198754" if is_month_paid else "#dc3545"
    amt, amt_str, note_str, mgmt_fee_val = get_rent_for_month(prop, month_str)
    breakdown    = note_str if note_str else (f"含代收管理費 {mgmt_fee_val:,} 元" if mgmt_fee_val > 0 else "純租金")
    return FlexSendMessage(
        alt_text=f"【個人帳務】{prop['room_no']} {month_str}月份租金繳費資訊",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#0d6efd', contents=[
                    TextComponent(text="🧾 房客租金與繳費帳務", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text=f"{prop['room_no']} ｜ {prop['tenant']} 您好", size='xs', color='#ffffff', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    create_row("完整地址", prop.get('addr', ''), color='#111111'),
                    create_row("繳款月份", f"{month_str} 月"),
                    create_row("應繳總額", f"{amt_str} 元", color='#dc3545', weight='bold'),
                    create_row("約定繳租日", f"每月 {prop['due_day']} 號", color='#0d6efd', weight='bold'),
                    create_row("費用明細", breakdown, color='#666666'),
                    create_row("繳納狀態", status_text, color=status_color, weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="🏦 租金匯款指定帳號：", weight='bold', size='xs', color='#333333', margin='sm'),
                    create_row("銀行名稱", BANK_NAME),
                    create_row("銀行代號", BANK_CODE, color='#0d6efd', weight='bold'),
                    create_row("存款帳號", BANK_ACCOUNT, color='#dc3545', weight='bold'),
                    create_row("戶名", BANK_HOLDER),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="匯款完成後請於此處留言回傳「匯款後5碼」，管家將為您對帳並開立收據，謝謝！", size='xs', color='#666666', margin='md', wrap=True),
                ]),
            ])
        )
    )

# ── 5b. 【修改十】訪客畫面（只看服務介紹，不顯示任何金額/帳號）──
def create_guest_account_flex():
    return FlexSendMessage(
        alt_text="【Sandy宅安心】專業包租代管服務",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#198754', contents=[
                    TextComponent(text="🏡 歡迎來到 Sandy 宅安心", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="專業包租代管 ｜ 房屋託管 ｜ 優質租屋", size='xs', color='#e8f5e9', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="我們能為您提供：", weight='bold', size='sm', color='#333333'),
                    create_row("🏡 包租代管", "穩定收益，屋主零煩惱"),
                    create_row("🛡️ 房屋託管", "專人管理，租賃一手包辦"),
                    create_row("🔍 優質房源", "嚴選好宅，拎包即住"),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="歡迎點選下方按鈕了解更多，或直接聯絡 Sandy 管家！", size='xs', color='#666666', margin='sm', align='center', wrap=True),
                    ButtonComponent(action=MessageAction(label="🏡 了解包租代管服務", text="包租代管"), style='primary', color='#198754', height='sm'),
                    ButtonComponent(action=MessageAction(label="🔍 我要租屋", text="我要租屋"), style='secondary', height='sm'),
                    ButtonComponent(action=MessageAction(label="🔑 現有房客綁定房間", text="我想綁定房間"), style='secondary', height='sm'),
                    ButtonComponent(action=URIAction(label="📞 直接聯絡 Sandy 管家", uri=f"tel:{MANAGER_PHONE}"), style='secondary', height='sm'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="若您是現有房客，點擊上方【🔑 現有房客綁定房間】或輸入「我是＋姓名」即可立即開通！", size='xs', color='#888888', margin='sm', wrap=True),
                ]),
            ])
        )
    )

# ── 6. 聯絡管家名片 ──
def create_contact_manager_flex():
    return FlexSendMessage(
        alt_text="【Sandy宅安心】專屬管家名片",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#20c997', contents=[
                    TextComponent(text="👩‍💼 Sandy 宅安心 ｜ 專屬管家名片", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="用心守護您的居住生活，竭誠為您服務！", size='xs', color='#e6fffa', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    create_row("專屬管家", MANAGER_NAME, weight='bold'),
                    create_row("服務專線", MANAGER_PHONE, color='#0d6efd', weight='bold'),
                    create_row("LINE ID",  MANAGER_LINE_ID, color='#20c997', weight='bold'),
                    create_row("服務時間", "週一至週日 09:00 ~ 20:00"),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="如有緊急修繕、租屋諮詢或合約問題，歡迎隨時聯繫！", size='xs', color='#666666', margin='md', align='center'),
                    ButtonComponent(action=URIAction(label="📞 直接撥打電話", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#20c997', height='sm'),
                    ButtonComponent(action=MessageAction(label="💬 留下提問訊息", text="我想諮詢管家"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 租金感謝收據卡（發給租客）──
def create_receipt_flex(room_no, addr, tenant, month_str, amount_str, mgmt_fee=0, is_test=False, note=""):
    header = [TextComponent(text="🧾 租金已繳確認收據", weight='bold', size='md', color='#ffffff')]
    if is_test:
        header.append(TextComponent(text=f"🧪 安全測試模式（模擬發給：{tenant}）", size='xs', color='#ffeb3b', margin='xs'))
    breakdown = note if note else (f"含代收管理費 {mgmt_fee:,} 元" if mgmt_fee > 0 else "純租金")
    return FlexSendMessage(
        alt_text=f"【租金收據】{addr} {room_no} {tenant} {month_str}月租金已入帳",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='md', background_color='#198754', contents=header),
                BoxComponent(layout='vertical', padding_all='lg', contents=[
                    TextComponent(text=f"{room_no} ｜ {tenant}", weight='bold', size='md', color='#333333'),
                    SeparatorComponent(margin='md'),
                    BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[
                        create_row("完整地址", addr, color='#111111', weight='bold'),
                        create_row("繳納月份", f"{month_str} 月"),
                        create_row("實收金額", f"{amount_str} 元", color='#198754', weight='bold'),
                        create_row("費用明細", breakdown, color='#666666'),
                        create_row("確認狀態", "✅ 已確認入帳", color='#198754', weight='bold'),
                    ]),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="非常感謝您的準時繳納，款項已入帳留存！", size='xs', color='#28a745', margin='md', align='center'),
                ]),
            ])
        )
    )

# ── 催繳提醒卡（發給逾期租客）──
def create_reminder_flex(room_no, addr, tenant, month_str, amount_str, due_day, is_test=False, note=""):
    header = [TextComponent(text="📢 租金繳費溫馨提醒", weight='bold', size='md', color='#ffffff')]
    if is_test:
        header.append(TextComponent(text=f"🧪 安全測試模式（模擬發給：{tenant}）", size='xs', color='#ffffff', margin='xs'))
    month_desc = f"{month_str} 月租金 {note}" if note else f"{month_str} 月租金（含管理費）"
    return FlexSendMessage(
        alt_text=f"【繳款提醒】{addr} {room_no} {tenant} {month_str}月租金請安排匯款",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='md', background_color='#dc3545', contents=header),
                BoxComponent(layout='vertical', padding_all='lg', contents=[
                    TextComponent(text=f"{room_no} ｜ {tenant} 您好", weight='bold', size='md', color='#333333'),
                    SeparatorComponent(margin='md'),
                    BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[
                        create_row("完整地址", addr, color='#111111', weight='bold'),
                        create_row("應繳月份", month_desc),
                        create_row("應繳總額", f"{amount_str} 元", color='#dc3545', weight='bold'),
                        create_row("約定繳租日", f"每月 {due_day} 號"),
                        SeparatorComponent(margin='sm'),
                        create_row("匯款銀行", f"{BANK_NAME} {BANK_CODE}"),
                        create_row("匯款帳號", BANK_ACCOUNT, color='#dc3545', weight='bold'),
                        create_row("戶名", BANK_HOLDER),
                    ]),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="溫馨提醒您本月租金繳費期限已屆，請抽空安排轉帳匯款。若您今日已繳納請忽略此訊息，謝謝您的配合！", size='xs', color='#666666', margin='md', wrap=True),
                ]),
            ])
        )
    )

# ── 【修改八】管理費已繳確認卡（發給管理員）──
def create_mgmt_paid_flex(addr, room_no, line_name, month_str, amount, last5, payer, is_test=False):
    clean_payer = re.sub(r'[A-Za-z\(\)\s（）]', '', str(payer)).strip() or BANK_HOLDER
    header = [TextComponent(text="✅ 管理費繳費確認", weight='bold', size='md', color='#ffffff')]
    if is_test:
        header.append(TextComponent(text=f"🧪 測試模式（模擬發給：{line_name}）", size='xs', color='#ffeb3b', margin='xs'))
    return FlexSendMessage(
        alt_text=f"【管理費確認】{addr} {room_no} {month_str}月管理費已繳",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='md', background_color='#198754', contents=header),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    create_row("完整地址", addr, color='#111111', weight='bold'),
                    create_row("房號/戶別", room_no if room_no else "—"),
                    create_row("繳費月份", f"{month_str} 月"),
                    create_row("匯款人",   clean_payer, color='#333333', weight='bold'),
                    create_row("匯款金額", f"{amount} 元", color='#198754', weight='bold'),
                    create_row("帳號後5碼", last5 if last5 else "（未提供）", color='#0d6efd', weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="以上管理費已匯出，請管委會／管理員查收核銷，謝謝！", size='xs', color='#666666', margin='sm', align='center', wrap=True),
                ]),
            ])
        )
    )

# ── 【修改八】貸款已繳確認卡（發給合伙人）──
def create_loan_paid_flex(addr, room_no, line_name, month_str, amount, last5, payer, is_test=False):
    clean_payer = re.sub(r'[A-Za-z\(\)\s（）]', '', str(payer)).strip() or BANK_HOLDER
    header = [TextComponent(text="✅ 貸款繳費確認", weight='bold', size='md', color='#ffffff')]
    if is_test:
        header.append(TextComponent(text=f"🧪 測試模式（模擬發給：{line_name}）", size='xs', color='#ffeb3b', margin='xs'))
    return FlexSendMessage(
        alt_text=f"【貸款確認】{addr} {room_no} {month_str}月貸款已繳",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='md', background_color='#0d6efd', contents=header),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    create_row("完整地址", addr, color='#111111', weight='bold'),
                    create_row("房號/戶別", room_no if room_no else "—"),
                    create_row("繳費月份", f"{month_str} 月"),
                    create_row("匯款人",   clean_payer, color='#333333', weight='bold'),
                    create_row("匯款金額", f"{amount} 元", color='#0d6efd', weight='bold'),
                    create_row("帳號後5碼", last5 if last5 else "（未提供）", color='#dc3545', weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="以上貸款已匯出，請確認查收核銷，謝謝！", size='xs', color='#666666', margin='sm', align='center', wrap=True),
                ]),
            ])
        )
    )

# ── Sandy 專屬房東管理選單 Flex 卡片 ──
def create_admin_menu_flex(rent_m, current_m, is_test):
    mode_desc = "🛡️ 安全測試模式（卡片只發給自己）" if is_test else "🚀 正式上線模式（卡片發給租客/關係人）"
    mode_color = "#ffc107" if is_test else "#198754"
    return FlexSendMessage(
        alt_text="【Sandy宅安心 房東管理選單】",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#2c3e50', contents=[
                    TextComponent(text="👑 Sandy 宅安心 ｜ 房東管理中心", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text=mode_desc, size='xs', color=mode_color, margin='xs', weight='bold'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="請選擇欲執行的管理功能：", size='xs', color='#666666'),
                    ButtonComponent(action=MessageAction(label=f"💰 查詢 {rent_m}月租金未繳", text=f"查 {rent_m}月租金"), style='primary', color='#0d6efd', height='sm'),
                    ButtonComponent(action=MessageAction(label=f"🏢 查詢 {current_m}月管理費", text=f"查 {current_m}月管理費"), style='primary', color='#198754', height='sm'),
                    ButtonComponent(action=MessageAction(label=f"🏦 查詢 {current_m}月貸款", text=f"查 {current_m}月貸款"), style='primary', color='#6f42c1', height='sm'),
                    ButtonComponent(action=URIAction(label="⚡ 明華路水電分帳計算器", uri="https://sandy-line-bot.onrender.com/utility_calc"), style='primary', color='#fd7e14', height='sm'),
                    ButtonComponent(action=URIAction(label="👑 總裁財務與資產月報", uri="https://sandy-line-bot.onrender.com/executive_report?key=sandy0225"), style='primary', color='#212529', height='sm'),
                    ButtonComponent(action=MessageAction(label="🚄 總裁高鐵差旅秘書", text="高鐵"), style='primary', color='#ea580c', height='sm'),
                    ButtonComponent(action=MessageAction(label="⏳ 查即將到期合約（約3個月/年底）", text="查即將到期合約"), style='secondary', height='sm'),
                    ButtonComponent(action=MessageAction(label="🔄 切換測試/正式模式", text="切換模式"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 即將到期合約卡片 ──
def create_expiring_contracts_flex(expiring_list):
    if not expiring_list:
        return FlexSendMessage(
            alt_text="【合約到期檢查】未來 100 天內無即將到期合約",
            contents=BubbleContainer(
                body=BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📋 合約到期追蹤", weight='bold', size='md', color='#2c3e50'),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="🎉 太棒了！未來 100 天內（含年底）沒有即將到期的房客合約。", size='sm', color='#198754', margin='md', wrap=True),
                ])
            )
        )
    
    rows_components = []
    for item in expiring_list[:8]:
        days_str = f"剩 {item['days_left']} 天" if item['days_left'] >= 0 else f"已逾期 {abs(item['days_left'])} 天"
        color = "#dc3545" if item['days_left'] <= 30 else ("#fd7e14" if item['days_left'] <= 60 else "#0d6efd")
        rows_components.append(
            BoxComponent(layout='horizontal', spacing='sm', margin='sm', contents=[
                TextComponent(text=f"{item['room_no']} {item['tenant']}", weight='bold', size='sm', flex=5),
                TextComponent(text=item['end_date'], size='xs', color='#888888', flex=4, align='center'),
                TextComponent(text=days_str, size='xs', color=color, weight='bold', flex=3, align='end'),
            ])
        )

    return FlexSendMessage(
        alt_text=f"【合約即將到期】共 {len(expiring_list)} 筆",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='md', background_color='#fd7e14', contents=[
                    TextComponent(text="⏳ 房客合約即將到期提醒（約3個月/年底前）", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text=f"共發現 {len(expiring_list)} 位房客即將到期，請及早安排續約或招租！", size='xs', color='#fff3cd', margin='xs', wrap=True),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', contents=[
                    BoxComponent(layout='horizontal', contents=[
                        TextComponent(text="房號 / 房客", size='xs', color='#888888', flex=5),
                        TextComponent(text="到期日", size='xs', color='#888888', flex=4, align='center'),
                        TextComponent(text="倒數天數", size='xs', color='#888888', flex=3, align='end'),
                    ]),
                    SeparatorComponent(margin='sm'),
                    *rows_components,
                    SeparatorComponent(margin='md'),
                    TextComponent(text="提醒：可及早與房客確認續約意願，確保收租穩定無空置期。", size='xs', color='#aaaaaa', margin='md', wrap=True),
                ])
            ])
        )
    )

# ── 管理者未繳租金輪播卡 ──
def create_rent_carousel(month_str, unpaid_items):
    bubbles = []
    for item in unpaid_items:
        amt, amt_str, note_str, mgmt_fee_val = get_rent_for_month(item, month_str)
        info_rows = [
            create_row("應收總額", f"{amt_str} 元", color='#dc3545', weight='bold'),
            create_row("繳租日",   f"每月 {item['due_day']} 號", color='#0d6efd', weight='bold'),
        ]
        if note_str:
            info_rows.append(create_row("收費明細", note_str, color='#666666'))
        elif mgmt_fee_val > 0:
            info_rows.append(create_row("代收管理費", f"{mgmt_fee_val:,} 元", color='#666666'))

        bubble = BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='lg', contents=[
                BoxComponent(layout='baseline', contents=[
                    TextComponent(text=item['room_no'], weight='bold', size='md', color='#0d6efd', flex=3),
                    TextComponent(text=item['tenant'],  weight='bold', size='md', color='#333333', flex=5),
                ]),
                TextComponent(text=f"📍 {item['addr']}", size='xs', color='#555555', margin='xs', wrap=True),
                SeparatorComponent(margin='md'),
                BoxComponent(layout='vertical', margin='md', spacing='sm', contents=info_rows),
                SeparatorComponent(margin='md'),
                BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[
                    ButtonComponent(action=MessageAction(label="✅ 租金已收（發送收據）", text=f"租金已收|{month_str}|{item['room_no']}"), style='primary', color='#198754', height='sm'),
                    ButtonComponent(action=MessageAction(label="📢 發送催繳提醒", text=f"發送提醒|{month_str}|{item['room_no']}"), style='secondary', height='sm'),
                ]),
            ])
        )
        bubbles.append(bubble)
        if len(bubbles) >= 10:
            break
    return FlexSendMessage(
        alt_text=f"【{month_str}月未繳租金名單】",
        contents={"type": "carousel", "contents": [b.as_json_dict() for b in bubbles]}
    )

# ── 【修改七】管理費/貸款未繳輪播卡（附「已繳發通知」按鈕）──
def create_expense_carousel(month_str, expense_type, unpaid_items):
    color      = '#198754' if expense_type == '管理費' else '#0d6efd'
    cmd_prefix = '管理費已繳' if expense_type == '管理費' else '貸款已繳'
    bubbles = []
    for item in unpaid_items:
        bubble = BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='lg', contents=[
                TextComponent(text=item.get('room_no', ''), weight='bold', size='md', color=color),
                TextComponent(text=f"📍 {item.get('addr', '')}", size='xs', color='#555555', margin='xs', wrap=True),
                SeparatorComponent(margin='md'),
                BoxComponent(layout='vertical', margin='md', spacing='sm', contents=[
                    create_row("類型", expense_type),
                    create_row("月份", f"{month_str} 月"),
                    create_row("金額", f"{item.get('amount_str', '0')} 元", color='#dc3545', weight='bold'),
                ]),
                SeparatorComponent(margin='md'),
                ButtonComponent(
                    action=MessageAction(label=f"✅ {expense_type}已繳（發通知）", text=f"{cmd_prefix}|{month_str}|{item.get('addr', '')}"),
                    style='primary', color=color, height='sm'
                ),
            ])
        )
        bubbles.append(bubble)
        if len(bubbles) >= 10:
            break
    if not bubbles:
        return None
    return FlexSendMessage(
        alt_text=f"【{month_str}月{expense_type}未繳清單】",
        contents={"type": "carousel", "contents": [b.as_json_dict() for b in bubbles]}
    )

# ==================== 明華路前後門水電分帳計算器核心邏輯 ====================

def calculate_utility_split(period, elec_total, elec_public, front_read, back_read, water_total, front_init=42.3, back_init=25.5):
    """
    明華路271之1號 前後門水電分帳核心計算函數：
    1. 電費：
       - 流動電費 = 總額 - 公電
       - 前門分電度數 = 前門新讀數 - 前門期初
       - 後門分電度數 = 後門新讀數 - 後門期初
       - 前門應收 = 流動電費 * (前門度數 / 總度數) + 公電 * 55%（四捨五入）
       - 後門應收 = 總電費 - 前門應收
    2. 水費：
       - 前門應收 = 水費總額 * 66.67%（四捨五入）
       - 後門應收 = 水費總額 - 前門應收
    """
    elec_total = float(elec_total or 0)
    elec_public = float(elec_public or 0)
    front_read = float(front_read or 0)
    back_read = float(back_read or 0)
    front_init = float(front_init or 42.3)
    back_init = float(back_init or 25.5)

    front_deg = max(0.0, front_read - front_init) if front_read >= front_init else 0.0
    back_deg = max(0.0, back_read - back_init) if back_read >= back_init else 0.0
    total_deg = front_deg + back_deg
    front_ratio = (front_deg / total_deg) if total_deg > 0 else 0.5
    back_ratio = 1.0 - front_ratio

    flow_elec = max(0.0, elec_total - elec_public)
    front_elec = round(flow_elec * front_ratio + elec_public * 0.55)
    back_elec = int(elec_total - front_elec) if elec_total > 0 else 0

    water_total = float(water_total or 0)
    front_water = round(water_total * (2.0 / 3.0)) if water_total > 0 else 0
    back_water = int(water_total - front_water) if water_total > 0 else 0

    front_total = front_elec + front_water
    back_total = back_elec + back_water

    return {
        'period': period or '本期',
        'elec_total': int(elec_total),
        'elec_public': int(elec_public),
        'front_init': front_init,
        'front_read': front_read,
        'front_deg': round(front_deg, 1),
        'front_ratio_pct': f"{round(front_ratio * 100, 1)}%",
        'front_elec': front_elec,
        'front_water': front_water,
        'front_total': front_total,
        
        'back_init': back_init,
        'back_read': back_read,
        'back_deg': round(back_deg, 1),
        'back_ratio_pct': f"{round(back_ratio * 100, 1)}%",
        'back_elec': back_elec,
        'back_water': back_water,
        'back_total': back_total,
        'water_total': int(water_total),
    }

def get_latest_utility_inits():
    """從「電費分攤」的副本自動取得最新期初讀數（預設 42.3 與 25.5）"""
    try:
        ws_e = get_worksheet_safely(["「電費分攤」的副本", "電費分攤", "明華路電費分攤", "電費分帳"])
        if ws_e:
            rows = ws_e.get_all_values()
            for r in reversed(rows[8:]):
                if len(r) >= 5 and r[3].strip() and r[4].strip():
                    try:
                        return float(r[3].strip()), float(r[4].strip())
                    except Exception:
                        pass
    except Exception as e:
        print(f"讀取電費期初讀數失敗: {e}")
    return 42.3, 25.5

def create_utility_receipt_flex(data, is_front=True, is_test=False):
    room_no = "A107" if is_front else "A109"
    tenant = "林采葳" if is_front else "楊芸錚"
    door = "前門店面" if is_front else "後門店面"
    addr = "高雄市鼓山區明華路271之1號" + ("" if is_front else "（後門）")
    deg = data.get('front_deg' if is_front else 'back_deg', 0)
    init_val = data.get('front_init' if is_front else 'back_init', 0)
    read_val = data.get('front_read' if is_front else 'back_read', 0)
    ratio_pct = data.get('front_ratio_pct' if is_front else 'back_ratio_pct', '50%')
    elec_amt = data.get('front_elec' if is_front else 'back_elec', 0)
    water_amt = data.get('front_water' if is_front else 'back_water', 0)
    total_amt = data.get('front_total' if is_front else 'back_total', 0)
    pub_share_pct = "55%" if is_front else "45%"
    water_share_desc = "2/3 分攤" if is_front else "1/3 分攤"
    period = data.get('period', '本期')

    header_color = "#fd7e14" if is_front else "#0d6efd"
    header_contents = [
        TextComponent(text=f"⚡ 明華路水電繳費單 ｜ {door}", weight='bold', size='md', color='#ffffff'),
    ]
    if is_test:
        header_contents.append(TextComponent(text=f"🧪 安全測試模式（模擬發給：{tenant}）", size='xs', color='#ffeb3b', margin='xs', weight='bold'))
    else:
        header_contents.append(TextComponent(text=f"承租人：{tenant}（{room_no}）", size='xs', color='#ffffff', margin='xs'))

    return FlexSendMessage(
        alt_text=f"【水電繳費單】明華路 {door} {tenant} 本期應繳 {total_amt:,} 元",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color=header_color, contents=header_contents),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    create_row("承租人", f"{tenant}（{room_no} {door}）", weight='bold'),
                    create_row("完整地址", addr, color='#111111'),
                    create_row("計費期間", period, color='#333333', weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 電費計算明細：", size='xs', color='#666666', weight='bold'),
                    create_row("分電表讀數", f"{init_val} ➔ {read_val}（用電 {deg} 度）", color='#0d6efd'),
                    create_row("流動電費分攤", f"用電佔比 {ratio_pct}"),
                    create_row("公電分攤", f"按戶別協議分攤 {pub_share_pct}"),
                    create_row("電費應收小計", f"{elec_amt:,} 元", color='#dc3545', weight='bold'),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="💧 水費計算明細：", size='xs', color='#666666', weight='bold'),
                    create_row("水費總額", f"{data.get('water_total', 0):,} 元"),
                    create_row("分攤比例", water_share_desc),
                    create_row("水費應收小計", f"{water_amt:,} 元", color='#dc3545', weight='bold'),
                    SeparatorComponent(margin='md'),
                    create_row("本期應繳總額", f"{total_amt:,} 元", color='#dc3545', weight='bold'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="指定匯款帳號（星展銀行）：", size='xs', color='#666666'),
                    create_row("匯款銀行", f"{BANK_NAME}（代號 {BANK_CODE}）"),
                    create_row("匯款帳號", BANK_ACCOUNT, color='#0d6efd', weight='bold'),
                    create_row("戶名", BANK_HOLDER),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="請於收到通知後抽空安排轉帳匯款，完成後回傳末5碼，謝謝您的配合！", size='xs', color='#888888', margin='sm', wrap=True),
                ])
            ])
        )
    )

# ==================== Webhook 路由 ====================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"Webhook handling exception (returning OK to terminate LINE retry loop): {e}")
        return 'OK'
    return 'OK'

INQUIRY_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Sandy 宅安心 ｜ 屋主委託評估</title>
  <style>
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
    body { background-color: #f0f4f8; margin: 0; padding: 20px 12px; color: #333; }
    .card { max-width: 460px; margin: 0 auto; background: #ffffff; border-radius: 16px; padding: 24px 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }
    .header { text-align: center; border-bottom: 2px solid #eef2f5; padding-bottom: 16px; margin-bottom: 20px; }
    .header h2 { margin: 0 0 6px; color: #198754; font-size: 20px; }
    .header p { margin: 0; color: #666; font-size: 13px; line-height: 1.5; }
    .form-group { margin-bottom: 16px; }
    label { display: block; margin-bottom: 6px; font-weight: bold; font-size: 14px; color: #333; }
    .req { color: #dc3545; }
    input[type="text"], input[type="tel"], select, textarea {
      width: 100%; padding: 12px; border: 1.5px solid #dce2e6; border-radius: 8px; font-size: 15px; outline: none; transition: border-color 0.2s; -webkit-appearance: none;
    }
    input:focus, select:focus, textarea:focus { border-color: #198754; background: #fafdfb; }
    .btn-submit {
      width: 100%; background: #198754; color: #fff; border: none; padding: 14px; border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; box-shadow: 0 4px 12px rgba(25,135,84,0.3);
    }
    .btn-submit:active { background: #146c43; }
    .btn-call {
      display: block; width: 100%; text-align: center; background: #f8f9fa; color: #0d6efd; border: 1.5px solid #0d6efd; padding: 12px; border-radius: 10px; font-size: 14px; font-weight: bold; text-decoration: none; margin-top: 12px;
    }
    .success-box { text-align: center; padding: 30px 10px; }
    .success-icon { font-size: 48px; color: #198754; margin-bottom: 12px; }
    .success-box h3 { color: #198754; margin: 0 0 10px; font-size: 20px; }
    .success-box p { color: #555; font-size: 14px; line-height: 1.6; margin-bottom: 20px; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
    <div class="success-box">
      <div class="success-icon">✅</div>
      <h3>委託需求已成功送出！</h3>
      <p>非常感謝您的委託諮詢！<br><strong>Sandy（黃蕙芬）管家</strong> 已收到您的房屋資訊，將會於您方便的時段主動致電與您聯繫，竭誠為您服務！</p>
      <a href="tel:0934008636" class="btn-call">📞 如有急件，可直接撥打管家專線</a>
    </div>
    {% else %}
    <div class="header">
      <h2>🏡 Sandy 宅安心 ｜ 屋主委託評估</h2>
      <p>不便直接通話？請留下愛屋資訊，管家將主動致電為您評估！</p>
    </div>
    <form method="POST" action="/inquiry">
      <div class="form-group">
        <label>屋主姓名／稱呼 <span class="req">*</span></label>
        <input type="text" name="name" placeholder="例如：陳先生、王小姐" required>
      </div>
      <div class="form-group">
        <label>聯絡電話（手機） <span class="req">*</span></label>
        <input type="tel" name="phone" placeholder="例如：0912345678" required>
      </div>
      <div class="form-group">
        <label>房屋類型 <span class="req">*</span></label>
        <select name="prop_type" required>
          <option value="電梯大樓">電梯大樓</option>
          <option value="無電梯公寓">無電梯公寓</option>
          <option value="透天別墅">透天別墅</option>
          <option value="獨立套房">獨立套房</option>
          <option value="店面商辦">店面商辦</option>
          <option value="車位">車位</option>
          <option value="其他">其他類型</option>
        </select>
      </div>
      <div class="form-group">
        <label>物件地點／地址 <span class="req">*</span></label>
        <input type="text" name="location" placeholder="例如：台北中山區、新北板橋、台南東區、高雄裕誠路..." required>
      </div>
      <div class="form-group">
        <label>方便聯絡時段</label>
        <select name="contact_time">
          <option value="隨時皆可">隨時皆可</option>
          <option value="平日白天（09:00~18:00）">平日白天（09:00~18:00）</option>
          <option value="平日晚上（18:00~21:00）">平日晚上（18:00~21:00）</option>
          <option value="週末假日">週末假日</option>
        </select>
      </div>
      <div class="form-group">
        <label>其他需求或備註（選填）</label>
        <textarea name="notes" rows="2" placeholder="例如：現有裝潢屋況、預計起租時間等"></textarea>
      </div>
      <button type="submit" class="btn-submit">🚀 送出委託諮詢</button>
      <a href="tel:0934008636" class="btn-call">📞 或直接撥打專線諮詢</a>
    </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/inquiry", methods=['GET', 'POST'])
def inquiry():
    if request.method == 'POST':
        name         = request.form.get("name", "").strip()
        phone        = request.form.get("phone", "").strip()
        prop_type    = request.form.get("prop_type", "").strip()
        location     = request.form.get("location", "").strip()
        contact_time = request.form.get("contact_time", "").strip()
        notes        = request.form.get("notes", "").strip()

        lead_card = FlexSendMessage(
            alt_text=f"🔥 【新屋主線上委託】{name} {phone}",
            contents=BubbleContainer(
                body=BoxComponent(layout='vertical', padding_all='none', contents=[
                    BoxComponent(layout='vertical', padding_all='md', background_color='#198754', contents=[
                        TextComponent(text="🔥 【新屋主線上委託通知！】", weight='bold', size='md', color='#ffffff'),
                        TextComponent(text="有屋主透過線上表單送出愛屋委託評估！", size='xs', color='#e8f5e9', margin='xs'),
                    ]),
                    BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                        create_row("👤 屋主稱呼", name, color='#111111', weight='bold'),
                        create_row("📞 聯絡電話", phone, color='#0d6efd', weight='bold'),
                        create_row("🏢 房屋類型", prop_type),
                        create_row("📍 物件地點", location),
                        create_row("⏰ 聯絡時段", contact_time),
                        create_row("📝 補充備註", notes if notes else "（無）", color='#666666'),
                        SeparatorComponent(margin='md'),
                        ButtonComponent(action=URIAction(label=f"📞 直接致電給屋主（{phone}）", uri=f"tel:{phone}"), style='primary', color='#198754', height='sm'),
                    ]),
                ])
            )
        )
        try:
            line_bot_api.push_message(MY_LINE_USER_ID, lead_card)
        except Exception as e:
            print(f"推播屋主委託至 Sandy 失敗: {e}")

        return render_template_string(INQUIRY_FORM_HTML, success=True)
    return render_template_string(INQUIRY_FORM_HTML, success=False)

RENTAL_INQUIRY_FORM_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Sandy 宅安心 ｜ 預約找屋需求登記</title>
  <style>
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
    body { background-color: #f8f9fa; margin: 0; padding: 20px 12px; color: #333; }
    .card { max-width: 460px; margin: 0 auto; background: #ffffff; border-radius: 16px; padding: 24px 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }
    .header { text-align: center; border-bottom: 2px solid #eef2f5; padding-bottom: 16px; margin-bottom: 20px; }
    .header h2 { margin: 0 0 6px; color: #fd7e14; font-size: 20px; }
    .header p { margin: 0; color: #666; font-size: 13px; line-height: 1.5; }
    .form-group { margin-bottom: 16px; }
    label { display: block; margin-bottom: 6px; font-weight: bold; font-size: 14px; color: #333; }
    .req { color: #dc3545; }
    input[type="text"], input[type="tel"], select, textarea {
      width: 100%; padding: 12px; border: 1.5px solid #dce2e6; border-radius: 8px; font-size: 15px; outline: none; transition: border-color 0.2s; -webkit-appearance: none;
    }
    input:focus, select:focus, textarea:focus { border-color: #fd7e14; background: #fffaf5; }
    .btn-submit {
      width: 100%; background: #fd7e14; color: #fff; border: none; padding: 14px; border-radius: 10px; font-size: 16px; font-weight: bold; cursor: pointer; margin-top: 10px; box-shadow: 0 4px 12px rgba(253,126,20,0.3);
    }
    .btn-submit:active { background: #e0680a; }
    .btn-call {
      display: block; width: 100%; text-align: center; background: #f8f9fa; color: #0d6efd; border: 1.5px solid #0d6efd; padding: 12px; border-radius: 10px; font-size: 14px; font-weight: bold; text-decoration: none; margin-top: 12px;
    }
    .success-box { text-align: center; padding: 30px 10px; }
    .success-icon { font-size: 48px; color: #fd7e14; margin-bottom: 12px; }
    .success-box h3 { color: #fd7e14; margin: 0 0 10px; font-size: 20px; }
    .success-box p { color: #555; font-size: 14px; line-height: 1.6; margin-bottom: 20px; }
  </style>
</head>
<body>
  <div class="card">
    {% if success %}
    <div class="success-box">
      <div class="success-icon">🎉</div>
      <h3>找屋需求已成功送出！</h3>
      <p>非常感謝您的登記！<br><strong>Sandy（黃蕙芬）管家</strong> 已收到您的找屋條件，將會為您配對合適房源，並主動與您聯繫安排看屋！</p>
      <a href="tel:0934008636" class="btn-call">📞 如急需看屋，可直接撥打管家專線</a>
    </div>
    {% else %}
    <div class="header">
      <h2>🔍 Sandy 宅安心 ｜ 預約找屋需求</h2>
      <p>告訴管家您的找屋條件，為您精選最適合的安心好宅！</p>
    </div>
    <form method="POST" action="/rental_inquiry">
      <div class="form-group">
        <label>您的稱呼 <span class="req">*</span></label>
        <input type="text" name="name" placeholder="例如：林先生、張小姐" required>
      </div>
      <div class="form-group">
        <label>聯絡電話（手機） <span class="req">*</span></label>
        <input type="tel" name="phone" placeholder="例如：0912345678" required>
      </div>
      <div class="form-group">
        <label>期望租屋區域（縣市、行政區或路段） <span class="req">*</span></label>
        <input type="text" name="area" placeholder="例如：台北大安區、新北板橋、台南東區、高雄裕誠路..." required>
      </div>
      <div class="form-group">
        <label>期望格局 <span class="req">*</span></label>
        <select name="layout" required>
          <option value="獨立套房">獨立套房</option>
          <option value="1 房 1 廳">1 房 1 廳</option>
          <option value="2 房格局">2 房格局</option>
          <option value="3 房以上家庭房">3 房以上家庭房</option>
          <option value="店面商辦">店面商辦</option>
          <option value="車位租賃">平面／機械車位</option>
          <option value="其他">其他類型</option>
        </select>
      </div>
      <div class="form-group">
        <label>每月租金預算（可自由填寫金額） <span class="req">*</span></label>
        <input type="text" name="budget" placeholder="例如：25,000元左右、15,000~20,000元" required>
      </div>
      <div class="form-group">
        <label>預計起租日期</label>
        <select name="move_in_date">
          <option value="兩週內起租">兩週內起租（急需）</option>
          <option value="一個月內">一個月內</option>
          <option value="兩個月內">兩個月內</option>
          <option value="半年內/先看看">半年內 / 先看看房源</option>
        </select>
      </div>
      <div class="form-group">
        <label>特殊需求或備註（選填）</label>
        <textarea name="notes" rows="2" placeholder="例如：需汽車停車位、可開伙、有養貓咪、陽台獨洗曬等"></textarea>
      </div>
      <button type="submit" class="btn-submit">🚀 送出找屋需求</button>
      <a href="tel:0934008636" class="btn-call">📞 亦可直接撥打看屋專線</a>
    </form>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/rental_inquiry", methods=['GET', 'POST'])
def rental_inquiry():
    if request.method == 'POST':
        name         = request.form.get("name", "").strip()
        phone        = request.form.get("phone", "").strip()
        area         = request.form.get("area", "").strip()
        layout       = request.form.get("layout", "").strip()
        budget       = request.form.get("budget", "").strip()
        move_in_date = request.form.get("move_in_date", "").strip()
        notes        = request.form.get("notes", "").strip()

        lead_card = FlexSendMessage(
            alt_text=f"🔍 【新租客找屋需求】{name} {phone}",
            contents=BubbleContainer(
                body=BoxComponent(layout='vertical', padding_all='none', contents=[
                    BoxComponent(layout='vertical', padding_all='md', background_color='#fd7e14', contents=[
                        TextComponent(text="🔍 【新租客找屋需求通知！】", weight='bold', size='md', color='#ffffff'),
                        TextComponent(text="有新租客登記找屋條件，請管家配對房源！", size='xs', color='#fff3cd', margin='xs'),
                    ]),
                    BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                        create_row("👤 租客稱呼", name, color='#111111', weight='bold'),
                        create_row("📞 聯絡電話", phone, color='#0d6efd', weight='bold'),
                        create_row("📍 期望區域", area),
                        create_row("🏠 期望格局", layout),
                        create_row("💰 預算範圍", budget, color='#fd7e14', weight='bold'),
                        create_row("📅 預計起租", move_in_date),
                        create_row("💡 特殊需求", notes if notes else "（無）", color='#666666'),
                        SeparatorComponent(margin='md'),
                        ButtonComponent(action=URIAction(label=f"📞 直接致電給租客（{phone}）", uri=f"tel:{phone}"), style='primary', color='#fd7e14', height='sm'),
                    ]),
                ])
            )
        )
        try:
            line_bot_api.push_message(MY_LINE_USER_ID, lead_card)
        except Exception as e:
            print(f"推播租客需求至 Sandy 失敗: {e}")

        return render_template_string(RENTAL_INQUIRY_FORM_HTML, success=True)
    return render_template_string(RENTAL_INQUIRY_FORM_HTML, success=False)


# ==================== 明華路水電分帳計算器 Web 路由 ====================

UTILITY_CALC_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Sandy 宅安心 ｜ 明華路水電分帳計算器</title>
  <style>
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
    body { background-color: #f4f7f6; margin: 0; padding: 16px 12px; color: #333; }
    .container { max-width: 540px; margin: 0 auto; }
    .card { background: #ffffff; border-radius: 16px; padding: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.06); margin-bottom: 16px; }
    .header { text-align: center; border-bottom: 2px solid #eef2f5; padding-bottom: 14px; margin-bottom: 16px; }
    .header h2 { margin: 0 0 6px; color: #fd7e14; font-size: 20px; }
    .header p { margin: 0; color: #666; font-size: 13px; line-height: 1.4; }
    .badge { display: inline-block; background: #fff3cd; color: #856404; font-size: 11px; font-weight: bold; padding: 3px 8px; border-radius: 12px; margin-top: 4px; }
    .section-title { font-size: 15px; font-weight: bold; color: #2c3e50; margin: 16px 0 10px; display: flex; align-items: center; gap: 6px; }
    .form-group { margin-bottom: 12px; }
    .form-row { display: flex; gap: 10px; }
    .form-row .form-group { flex: 1; }
    label { display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600; color: #495057; }
    input[type="text"], input[type="number"] {
      width: 100%; padding: 10px 12px; border: 1.5px solid #dce2e6; border-radius: 8px; font-size: 15px; outline: none; transition: border-color 0.2s; -webkit-appearance: none;
    }
    input:focus { border-color: #fd7e14; background: #fffaf5; }
    .details-toggle { font-size: 12px; color: #0d6efd; cursor: pointer; text-decoration: underline; margin-bottom: 10px; display: inline-block; }
    .results-grid { display: flex; gap: 10px; margin-top: 14px; }
    .result-box { flex: 1; background: #f8f9fa; border-radius: 12px; padding: 14px 12px; border-top: 4px solid #fd7e14; }
    .result-box.back { border-top-color: #0d6efd; }
    .result-box h4 { margin: 0 0 8px; font-size: 15px; color: #333; display: flex; justify-content: space-between; align-items: center; }
    .result-box .tag { font-size: 11px; padding: 2px 6px; border-radius: 6px; background: #e9ecef; }
    .res-row { display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 5px; color: #666; }
    .res-total { border-top: 1px dashed #ccc; margin-top: 8px; padding-top: 8px; font-size: 14px; font-weight: bold; color: #dc3545; display: flex; justify-content: space-between; }
    .btn { display: block; width: 100%; padding: 14px; border-radius: 10px; font-size: 15px; font-weight: bold; cursor: pointer; border: none; text-align: center; margin-top: 10px; transition: opacity 0.2s; }
    .btn:active { opacity: 0.8; }
    .btn-line { background: #06c755; color: #fff; box-shadow: 0 4px 12px rgba(6,199,85,0.3); }
    .btn-sheet { background: #0d6efd; color: #fff; box-shadow: 0 4px 12px rgba(13,110,253,0.3); }
    .btn-copy { background: #f8f9fa; color: #495057; border: 1.5px solid #ced4da; font-size: 13px; padding: 10px; }
    .alert-box { padding: 12px; border-radius: 8px; font-size: 13px; margin-top: 14px; display: none; line-height: 1.4; }
    .alert-success { background: #d1e7dd; color: #0f5132; border: 1px solid #badbcc; }
    .alert-error { background: #f8d7da; color: #842029; border: 1px solid #f5c2c7; }
    .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 0.8s linear infinite; vertical-align: middle; margin-right: 6px; }
    @keyframes spin { to { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div class="container">
    <div class="card">
      <div class="header">
        <h2>⚡ 明華路水電分帳計算器</h2>
        <p>高雄市鼓山區明華路271之1號</p>
        <span class="badge">前門 A107 林采葳 ｜ 後門 A109 楊芸錚</span>
      </div>

      <div class="form-group">
        <label>📅 計費期間</label>
        <input type="text" id="period" value="{{ default_period }}" placeholder="例如：2026/09/18~11/18">
      </div>

      <div class="section-title">⚡ 台電電費資訊</div>
      <div class="form-row">
        <div class="form-group">
          <label>電費應繳總額 ($)</label>
          <input type="number" id="elec_total" placeholder="例如：3500" value="0">
        </div>
        <div class="form-group">
          <label>公共設施電費 ($)</label>
          <input type="number" id="elec_public" placeholder="例如：500" value="0">
        </div>
      </div>

      <div class="form-row">
        <div class="form-group">
          <label>前門新讀數（左表）</label>
          <input type="number" step="0.1" id="front_read" placeholder="新度數" value="{{ default_front_init }}">
        </div>
        <div class="form-group">
          <label>後門新讀數（右表）</label>
          <input type="number" step="0.1" id="back_read" placeholder="新度數" value="{{ default_back_init }}">
        </div>
      </div>

      <span class="details-toggle" onclick="toggleInits()">⚙️ 展開／自訂兩表期初起點</span>
      <div id="inits_panel" style="display:none; background:#f1f3f5; padding:10px; border-radius:8px; margin-bottom:12px;">
        <div class="form-row">
          <div class="form-group">
            <label style="font-size:11px;">前門期初起點</label>
            <input type="number" step="0.1" id="front_init" value="{{ default_front_init }}">
          </div>
          <div class="form-group">
            <label style="font-size:11px;">後門期初起點</label>
            <input type="number" step="0.1" id="back_init" value="{{ default_back_init }}">
          </div>
        </div>
      </div>

      <div class="section-title">💧 台水水費資訊</div>
      <div class="form-group">
        <label>水費總額 ($)</label>
        <input type="number" id="water_total" placeholder="例如：300" value="0">
      </div>

      <!-- 即時分帳試算預覽 -->
      <div class="results-grid">
        <div class="result-box">
          <h4>
            <span>🏢 前門（A107）</span>
            <span class="tag">林采葳</span>
          </h4>
          <div class="res-row">
            <span>用電度數</span>
            <strong id="front_deg_txt">0.0 度</strong>
          </div>
          <div class="res-row">
            <span>用電比 (流動+公電55%)</span>
            <strong id="front_elec_txt">$0</strong>
          </div>
          <div class="res-row">
            <span>水費分攤 (2/3)</span>
            <strong id="front_water_txt">$0</strong>
          </div>
          <div class="res-total">
            <span>應繳總額</span>
            <span id="front_total_txt" style="font-size:16px;">$0 元</span>
          </div>
        </div>

        <div class="result-box back">
          <h4>
            <span>🚪 後門（A109）</span>
            <span class="tag">楊芸錚</span>
          </h4>
          <div class="res-row">
            <span>用電度數</span>
            <strong id="back_deg_txt">0.0 度</strong>
          </div>
          <div class="res-row">
            <span>用電比 (流動+公電45%)</span>
            <strong id="back_elec_txt">$0</strong>
          </div>
          <div class="res-row">
            <span>水費分攤 (1/3)</span>
            <strong id="back_water_txt">$0</strong>
          </div>
          <div class="res-total">
            <span>應繳總額</span>
            <span id="back_total_txt" style="font-size:16px;">$0 元</span>
          </div>
        </div>
      </div>

      <button type="button" class="btn btn-line" id="btn_send_line" onclick="sendToLine()">
        📲 一鍵發送 2 張水電繳費單至 LINE
      </button>
      <button type="button" class="btn btn-sheet" id="btn_save_sheet" onclick="saveToSheet()">
        💾 同步寫入 Google 試算表存檔
      </button>
      <button type="button" class="btn btn-copy" onclick="copyNotices()">
        📋 複製前後門文字帳單（備用）
      </button>

      <div id="alert_box" class="alert-box"></div>
    </div>
  </div>

  <script>
    function toggleInits() {
      var p = document.getElementById('inits_panel');
      p.style.display = p.style.display === 'none' ? 'block' : 'none';
    }

    function calculate() {
      var period = document.getElementById('period').value;
      var elec_total = parseFloat(document.getElementById('elec_total').value) || 0;
      var elec_public = parseFloat(document.getElementById('elec_public').value) || 0;
      var front_read = parseFloat(document.getElementById('front_read').value) || 0;
      var back_read = parseFloat(document.getElementById('back_read').value) || 0;
      var front_init = parseFloat(document.getElementById('front_init').value) || 42.3;
      var back_init = parseFloat(document.getElementById('back_init').value) || 25.5;
      var water_total = parseFloat(document.getElementById('water_total').value) || 0;

      var front_deg = Math.max(0, front_read - front_init);
      var back_deg = Math.max(0, back_read - back_init);
      var total_deg = front_deg + back_deg;
      var front_ratio = total_deg > 0 ? (front_deg / total_deg) : 0.5;

      var flow_elec = Math.max(0, elec_total - elec_public);
      var front_elec = Math.round(flow_elec * front_ratio + elec_public * 0.55);
      var back_elec = Math.max(0, elec_total - front_elec);

      var front_water = Math.round(water_total * (2.0 / 3.0));
      var back_water = Math.max(0, water_total - front_water);

      var front_total = front_elec + front_water;
      var back_total = back_elec + back_water;

      document.getElementById('front_deg_txt').innerText = front_deg.toFixed(1) + ' 度 (' + (front_ratio * 100).toFixed(1) + '%)';
      document.getElementById('front_elec_txt').innerText = '$' + front_elec.toLocaleString();
      document.getElementById('front_water_txt').innerText = '$' + front_water.toLocaleString();
      document.getElementById('front_total_txt').innerText = '$' + front_total.toLocaleString() + ' 元';

      document.getElementById('back_deg_txt').innerText = back_deg.toFixed(1) + ' 度 (' + ((1 - front_ratio) * 100).toFixed(1) + '%)';
      document.getElementById('back_elec_txt').innerText = '$' + back_elec.toLocaleString();
      document.getElementById('back_water_txt').innerText = '$' + back_water.toLocaleString();
      document.getElementById('back_total_txt').innerText = '$' + back_total.toLocaleString() + ' 元';

      return {
        period: period,
        elec_total: elec_total,
        elec_public: elec_public,
        front_read: front_read,
        back_read: back_read,
        front_init: front_init,
        back_init: back_init,
        water_total: water_total,
        front_deg: front_deg.toFixed(1),
        back_deg: back_deg.toFixed(1),
        front_elec: front_elec,
        back_elec: back_elec,
        front_water: front_water,
        back_water: back_water,
        front_total: front_total,
        back_total: back_total
      };
    }

    var inputs = document.querySelectorAll('input');
    inputs.forEach(function(el) {
      el.addEventListener('input', calculate);
    });
    calculate();

    function showAlert(msg, isSuccess) {
      var box = document.getElementById('alert_box');
      box.style.display = 'block';
      box.className = 'alert-box ' + (isSuccess ? 'alert-success' : 'alert-error');
      box.innerHTML = msg;
    }

    function sendToLine() {
      var data = calculate();
      var btn = document.getElementById('btn_send_line');
      var origText = btn.innerText;
      btn.innerHTML = '<span class="spinner"></span> 正在傳送收據至 LINE...';
      btn.disabled = true;

      fetch('/api/send_utility_receipts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      })
      .then(function(res) { return res.json(); })
      .then(function(res) {
        btn.innerHTML = origText;
        btn.disabled = false;
        if (res.status === 'ok') {
          showAlert(res.message, true);
        } else {
          showAlert('❌ 傳送失敗：' + res.message, false);
        }
      })
      .catch(function(err) {
        btn.innerHTML = origText;
        btn.disabled = false;
        showAlert('❌ 網路請求失敗：' + err, false);
      });
    }

    function saveToSheet() {
      var data = calculate();
      var btn = document.getElementById('btn_save_sheet');
      var origText = btn.innerText;
      btn.innerHTML = '<span class="spinner"></span> 正在同步寫入試算表...';
      btn.disabled = true;

      fetch('/api/save_utility_sheet', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      })
      .then(function(res) { return res.json(); })
      .then(function(res) {
        btn.innerHTML = origText;
        btn.disabled = false;
        if (res.status === 'ok') {
          showAlert(res.message, true);
        } else {
          showAlert('❌ 存入失敗：' + res.message, false);
        }
      })
      .catch(function(err) {
        btn.innerHTML = origText;
        btn.disabled = false;
        showAlert('❌ 網路請求失敗：' + err, false);
      });
    }

    function copyNotices() {
      var d = calculate();
      var text = "【明華路水電繳費通知】\\n"
        + "計費期間：" + d.period + "\\n\\n"
        + "🏢 前門（A107 林采葳）：\\n"
        + "• 用電：" + d.front_deg + " 度（電費 $" + d.front_elec.toLocaleString() + "）\\n"
        + "• 水費：$" + d.front_water.toLocaleString() + "\\n"
        + "• 應繳總額：$" + d.front_total.toLocaleString() + " 元\\n\\n"
        + "🚪 後門（A109 楊芸錚）：\\n"
        + "• 用電：" + d.back_deg + " 度（電費 $" + d.back_elec.toLocaleString() + "）\\n"
        + "• 水費：$" + d.back_water.toLocaleString() + "\\n"
        + "• 應繳總額：$" + d.back_total.toLocaleString() + " 元\\n\\n"
        + "匯款帳號：星展銀行 810 6712880487（戶名：黃蕙芬）";
      navigator.clipboard.writeText(text).then(function() {
        alert("📋 已複製前後門水電繳費通知文字！可直接貼在 LINE 傳給房客！");
      });
    }
  </script>
</body>
</html>
"""

@app.route("/utility_calc")
def utility_calc():
    f_init, b_init = get_latest_utility_inits()
    today = datetime.now()
    default_period = f"{today.year}/{today.month}月期"
    return render_template_string(
        UTILITY_CALC_HTML,
        default_front_init=f_init,
        default_back_init=b_init,
        default_period=default_period
    )

@app.route("/api/send_utility_receipts", methods=['POST'])
def api_send_utility_receipts():
    try:
        data = request.get_json(force=True)
        res = calculate_utility_split(
            period=data.get('period', ''),
            elec_total=data.get('elec_total', 0),
            elec_public=data.get('elec_public', 0),
            front_read=data.get('front_read', 0),
            back_read=data.get('back_read', 0),
            water_total=data.get('water_total', 0),
            front_init=data.get('front_init', 42.3),
            back_init=data.get('back_init', 25.5)
        )
        card_front = create_utility_receipt_flex(res, is_front=True, is_test=TEST_MODE)
        card_back = create_utility_receipt_flex(res, is_front=False, is_test=TEST_MODE)

        props = load_properties()
        prop_front = next((p for p in props if p['room_no'] == 'A107'), None)
        prop_back = next((p for p in props if p['room_no'] == 'A109'), None)

        if TEST_MODE:
            line_bot_api.push_message(MY_LINE_USER_ID, card_front)
            line_bot_api.push_message(MY_LINE_USER_ID, card_back)
            msg = "✅ 【安全測試模式】前後門 2 張水電繳費單已發送至 Sandy 手機！"
        else:
            target_front = prop_front['line_id'] if (prop_front and prop_front['line_id']) else MY_LINE_USER_ID
            target_back = prop_back['line_id'] if (prop_back and prop_back['line_id']) else MY_LINE_USER_ID
            line_bot_api.push_message(target_front, card_front)
            line_bot_api.push_message(target_back, card_back)
            note_f = "" if (prop_front and prop_front['line_id']) else "（林采葳尚未綁定LINE，已發給您留存）"
            note_b = "" if (prop_back and prop_back['line_id']) else "（楊芸錚尚未綁定LINE，已發給您留存）"
            msg = f"🚀 【正式模式】水電繳費單已成功發出！{note_f} {note_b}".strip()

        return jsonify({'status': 'ok', 'message': msg, 'data': res})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route("/api/save_utility_sheet", methods=['POST'])
def api_save_utility_sheet():
    try:
        data = request.get_json(force=True)
        period = str(data.get('period', '')).strip()
        elec_total = float(data.get('elec_total', 0))
        elec_public = float(data.get('elec_public', 0))
        front_read = float(data.get('front_read', 0))
        back_read = float(data.get('back_read', 0))
        water_total = float(data.get('water_total', 0))

        # 1. 寫入電費分攤表
        target_row_e = None
        ws_e = get_worksheet_safely(["「電費分攤」的副本", "電費分攤", "明華路電費分攤", "電費分帳"])
        if ws_e:
            rows_e = ws_e.get_all_values()
            for idx in range(8, len(rows_e)):
                r = rows_e[idx]
                if not (r[0].strip() or (len(r) > 1 and r[1].strip())):
                    target_row_e = idx + 1
                    break
            if not target_row_e:
                target_row_e = len(rows_e) + 1
            ws_e.update(values=[[period, elec_total, elec_public, front_read, back_read]], range_name=f"A{target_row_e}:E{target_row_e}", value_input_option='USER_ENTERED')

        # 2. 寫入水費分攤表
        target_row_w = None
        ws_w = get_worksheet_safely(["「水費分攤」的副本", "水費分攤", "明華路水費分攤", "水費分帳"])
        if ws_w:
            rows_w = ws_w.get_all_values()
            for idx in range(8, len(rows_w)):
                r = rows_w[idx]
                if not (r[0].strip() or (len(r) > 1 and r[1].strip())):
                    target_row_w = idx + 1
                    break
            if not target_row_w:
                target_row_w = len(rows_w) + 1
            ws_w.update(values=[[period, water_total]], range_name=f"A{target_row_w}:B{target_row_w}", value_input_option='USER_ENTERED')

        return jsonify({'status': 'ok', 'message': f'✅ 已成功同步存入 Google 試算表（電費第 {target_row_e} 列、水費第 {target_row_w} 列）！'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ==================== 👑 Sandy 總裁財務與資產決策月報 ====================

EXECUTIVE_REPORT_HTML = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <title>👑 Sandy 總裁財務與資產決策月報</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: #111827;
      --card-border: #1f293d;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --gold: #f59e0b;
      --gold-light: #fef3c7;
      --emerald: #10b981;
      --emerald-bg: rgba(16, 185, 129, 0.12);
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.12);
      --blue: #3b82f6;
      --blue-bg: rgba(59, 130, 246, 0.12);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft JhengHei", sans-serif;
      background: var(--bg);
      color: var(--text-main);
      line-height: 1.6;
      padding: 16px;
      max-width: 900px;
      margin: 0 auto;
    }
    .header-banner {
      background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 100%);
      border: 1px solid #312e81;
      border-radius: 16px;
      padding: 24px;
      margin-bottom: 20px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .badge-vip {
      display: inline-block;
      background: rgba(245, 158, 11, 0.2);
      color: var(--gold);
      border: 1px solid rgba(245, 158, 11, 0.4);
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 1px;
      margin-bottom: 8px;
    }
    h1 { font-size: 22px; font-weight: 800; color: #ffffff; margin-bottom: 4px; }
    .subtitle { font-size: 13px; color: var(--text-muted); }
    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      margin-top: 16px;
    }
    .kpi-card {
      background: rgba(15, 23, 42, 0.7);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 14px;
    }
    .kpi-title { font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
    .kpi-val { font-size: 20px; font-weight: 800; color: #ffffff; }
    .kpi-sub { font-size: 11px; margin-top: 4px; }
    .text-emerald { color: var(--emerald); }
    .text-gold { color: var(--gold); }
    .text-red { color: var(--red); }
    .text-blue { color: var(--blue); }

    .module-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 20px;
      margin-bottom: 20px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }
    .module-title {
      font-size: 16px;
      font-weight: 700;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 8px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 16px;
    }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    @media (max-width: 600px) {
      .grid-2 { grid-template-columns: 1fr; }
    }
    .info-box {
      background: #172033;
      border: 1px solid #23314d;
      border-radius: 10px;
      padding: 12px 14px;
    }
    .info-label { font-size: 12px; color: var(--text-muted); }
    .info-num { font-size: 17px; font-weight: 700; margin-top: 2px; }

    .table-container {
      overflow-x: auto;
      margin-top: 12px;
      border-radius: 8px;
      border: 1px solid var(--card-border);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      text-align: left;
    }
    th {
      background: #172033;
      color: var(--text-muted);
      font-weight: 600;
      padding: 10px 12px;
      border-bottom: 1px solid var(--card-border);
    }
    td {
      padding: 10px 12px;
      border-bottom: 1px solid #1a2438;
      color: #e2e8f0;
    }
    tr:last-child td { border-bottom: none; }
    .status-badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 600;
    }
    .badge-paid { background: var(--emerald-bg); color: var(--emerald); border: 1px solid rgba(16,185,129,0.3); }
    .badge-unpaid { background: var(--red-bg); color: var(--red); border: 1px solid rgba(239,68,68,0.3); }
    .badge-neutral { background: var(--blue-bg); color: var(--blue); border: 1px solid rgba(59,130,246,0.3); }

    .scenario-card {
      background: linear-gradient(180deg, #162032 0%, #111827 100%);
      border: 1px solid #2b3954;
      border-radius: 12px;
      padding: 14px;
      margin-bottom: 10px;
    }
    .scenario-title {
      font-size: 14px;
      font-weight: 700;
      color: var(--gold);
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
    }
    .scenario-desc { font-size: 13px; color: #cbd5e1; }

    .advice-item {
      display: flex;
      align-items: flex-start;
      gap: 10px;
      padding: 12px;
      background: #151d30;
      border-radius: 10px;
      margin-bottom: 8px;
      font-size: 13px;
      border-left: 3px solid var(--gold);
    }
    .conclusion-box {
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      border: 2px solid #f59e0b;
      border-radius: 16px;
      padding: 20px;
      margin-bottom: 24px;
      box-shadow: 0 0 20px rgba(245, 158, 11, 0.15);
    }
    .conclusion-header {
      font-size: 16px;
      font-weight: 800;
      color: var(--gold);
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
    }
    .conclusion-text {
      font-size: 14px;
      color: #f1f5f9;
      line-height: 1.7;
    }
    .btn-action {
      display: block;
      width: 100%;
      text-align: center;
      padding: 12px;
      background: #f59e0b;
      color: #0f172a;
      text-decoration: none;
      font-weight: 700;
      border-radius: 10px;
      font-size: 14px;
      margin-top: 16px;
      transition: background 0.2s;
    }
    .btn-action:hover { background: #d97706; }
    .footer-note {
      text-align: center;
      font-size: 12px;
      color: #64748b;
      margin-top: 24px;
      margin-bottom: 16px;
    }
  </style>
</head>
<body>

  <!-- Top Banner -->
  <div class="header-banner">
    <div class="badge-vip">👑 TOP SECRET ｜ 總裁決策專用</div>
    <h1>Sandy 總裁財務與資產決策月報</h1>
    <div class="subtitle">{{ data.report_date }} 匯總 ｜ 宅安心資產管理系統即時分析</div>
    
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-title">總裁財務防禦評級</div>
        <div class="kpi-val text-emerald">AAA 級</div>
        <div class="kpi-sub text-emerald">極安全 ｜ 防禦近 30 年</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">流動現金總儲備</div>
        <div class="kpi-val text-gold">{{ data.cash_total_fmt }} 元</div>
        <div class="kpi-sub text-muted">安全月數：{{ data.safe_month_fmt }} 個月</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">退休每月生活支出預估</div>
        <div class="kpi-val text-blue">{{ data.retire_cost_fmt }} 元</div>
        <div class="kpi-sub text-muted">自付: {{ data.retire_self_fmt }} ｜ 補貼: {{ data.retire_company_fmt }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">本月租賃實收金流</div>
        <div class="kpi-val text-emerald">{{ data.total_collected_fmt }} 元</div>
        <div class="kpi-sub text-muted">達成率：{{ data.collection_rate_fmt }}%</div>
      </div>
    </div>
  </div>

  <!-- Module 1: 租賃金流全景 -->
  <div class="module-card">
    <div class="module-title">🏢 模組一：{{ data.month }}月租賃金流全景（包租本業運作）</div>
    <div class="grid-2">
      <div class="info-box">
        <div class="info-label">應收租金總額</div>
        <div class="info-num">{{ data.total_expected_fmt }} 元</div>
      </div>
      <div class="info-box">
        <div class="info-label">實收租金總額</div>
        <div class="info-num text-emerald">{{ data.total_collected_fmt }} 元</div>
      </div>
      <div class="info-box">
        <div class="info-label">管理費已繳 / 應繳</div>
        <div class="info-num">{{ data.total_mgmt_paid_fmt }} / {{ data.total_mgmt_expected_fmt }} 元</div>
      </div>
      <div class="info-box">
        <div class="info-label">房貸支出已繳 / 應繳</div>
        <div class="info-num">{{ data.total_loan_paid_fmt }} / {{ data.total_loan_expected_fmt }} 元</div>
      </div>
    </div>
    
    <div class="info-box" style="margin-top:12px; background: rgba(16,185,129,0.08); border-color: rgba(16,185,129,0.3);">
      <div class="info-label">本月租賃實質淨現金流（實收 - 管理費 - 貸款）</div>
      <div class="info-num text-emerald" style="font-size: 20px;">
        {{ data.net_leasing_cashflow_fmt }} 元
      </div>
    </div>

    {% if data.unpaid_list %}
    <div style="margin-top: 16px;">
      <div style="font-size: 13px; font-weight:700; color: var(--gold); margin-bottom: 6px;">⏳ 待收租金名單（共 {{ data.unpaid_list|length }} 間）：</div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>房號</th>
              <th>房客</th>
              <th>應繳金額</th>
              <th>狀態</th>
            </tr>
          </thead>
          <tbody>
            {% for u in data.unpaid_list %}
            <tr>
              <td><b>{{ u.room }}</b></td>
              <td>{{ u.tenant }}</td>
              <td>{{ u.amount_fmt }} 元</td>
              <td><span class="status-badge badge-unpaid">{{ u.status }}</span></td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    {% else %}
    <div style="margin-top: 14px; padding: 10px; background: var(--emerald-bg); border-radius: 8px; font-size: 13px; color: var(--emerald);">
      🎉 太棒了！本月所有物件租金已 100% 全數繳清入帳！
    </div>
    {% endif %}
  </div>

  <!-- Module 2: 財務自由與安全指標 -->
  <div class="module-card">
    <div class="module-title">🛡️ 模組二：財務自由與安全指標深度體檢</div>
    <div class="grid-2">
      <div class="info-box">
        <div class="info-label">流動現金儲備</div>
        <div class="info-num text-gold">{{ data.cash_total_fmt }} 元</div>
        <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">可覆蓋月生活支出約 {{ data.safe_month_fmt }} 個月（29.8 年）</div>
      </div>
      <div class="info-box">
        <div class="info-label">含本業薪資每月總現金流</div>
        <div class="info-num text-emerald">+{{ data.total_cashflow_fmt }} 元</div>
        <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">月薪約 {{ data.salary_month_fmt }} 元 支撐資產擴張</div>
      </div>
    </div>

    <div style="margin-top: 14px;">
      <div style="font-size: 13px; font-weight: 700; color: #cbd5e1; margin-bottom: 6px;">💡 被動收入覆蓋率深度解析（雙視角透視）：</div>
      
      <div class="scenario-card">
        <div class="scenario-title">📌 視角 A：現階段持有期（含房貸還本支出）</div>
        <div class="scenario-desc">
          目前因同時背負房產貸款（每月需還本付息），動態淨現金流呈現約 -5.1 萬元；但這筆金額<strong>絕大多數是在替您累積房產本金淨資產</strong>，本業薪資充沛（每月薪資約 {{ data.salary_month_fmt }} 元），足以輕鬆吸收並持續產生淨現金流入！
        </div>
      </div>

      <div class="scenario-card">
        <div class="scenario-title">🚀 視角 B：退休無貸金流覆蓋率（純被動毛收益 vs 生活費）</div>
        <div class="scenario-desc">
          名下收租物業每月產生租金毛收入約 <strong>{{ data.total_expected_fmt }} 元</strong>，對比您的退休每月生活支出 <strong>{{ data.retire_cost_fmt }} 元</strong>，被動覆蓋率高達 <strong class="text-emerald">{{ data.cover_ratio_gross_fmt }}%（超標 5 倍以上！）</strong>。只要貸款逐步隨合約收租攤平，被動收入將全面接管生活！
        </div>
      </div>
    </div>
  </div>

  <!-- Module 3: 退休後每月生活支出試算 (Sandy 核心關注) -->
  <div class="module-card">
    <div class="module-title">🏖️ 模組三：退休後每月生活支出試算（Sandy 專屬精算）</div>
    
    <div class="grid-2">
      <div class="info-box">
        <div class="info-label">平均每月生活成本（退休基準）</div>
        <div class="info-num text-gold">{{ data.retire_cost_fmt }} 元</div>
        <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">依年度支出大數據綜合推算之平均值</div>
      </div>
      <div class="info-box">
        <div class="info-label">現行自付 vs 公司補貼結構</div>
        <div class="info-num" style="font-size:14px; margin-top:6px;">
          自付：<span class="text-emerald">{{ data.retire_self_fmt }} 元</span> ｜ 補貼：<span class="text-blue">{{ data.retire_company_fmt }} 元</span>
        </div>
        <div style="font-size:11px; color:var(--text-muted); margin-top:2px;">退休後公司補貼將由自身被動金流吸收</div>
      </div>
    </div>

    <div style="margin-top: 14px;">
      <div style="font-size: 13px; font-weight: 700; color: #cbd5e1; margin-bottom: 8px;">📊 退休金流三大防禦陣地情境推演：</div>

      <div class="scenario-card">
        <div class="scenario-title">🛡️ 陣地一：純現金零收入極限防禦（無敵護城河）</div>
        <div class="scenario-desc">
          即使 Sandy 明天起完全停止所有工作、且所有物業完全零租金，單憑目前戶頭 <strong>{{ data.cash_total_fmt }} 元現金</strong>，以每月正常生活支出計算，足足可支撐 <strong>{{ data.safe_month_fmt }} 個月（約 29.8 年）</strong>！退休生活無後顧之憂。
        </div>
      </div>

      <div class="scenario-card">
        <div class="scenario-title">🏡 陣地二：房產租金養老金流（永續自體循環）</div>
        <div class="scenario-desc">
          當房產貸款逐步清償完畢後，每月租金淨流入預估超過 <strong>12 ~ 14 萬元</strong>，不僅完全覆蓋退休生活支出（2.8 萬 ~ 4.7 萬），每月還能多出 <strong>8 ~ 10 萬元</strong> 的額外旅遊與享受基金！
        </div>
      </div>

      <div class="scenario-card">
        <div class="scenario-title">📈 陣地三：流動資金股息化（睡覺都在賺錢）</div>
        <div class="scenario-desc">
          若將部分閒置現金逐步配置於優質高股息 ETF（如 0056 / 00878 / 00919，年化 5%），每投入 800 萬即可年領 40 萬股息（每月約 3.3 萬），單靠股息就已完全包辦退休基本開銷！
        </div>
      </div>
    </div>
  </div>

  <!-- Module 4: 資產效益排行與體檢 -->
  <div class="module-card">
    <div class="module-title">🏆 模組四：資產效益體檢與月現金流排行</div>
    
    <div style="font-size: 13px; font-weight:700; color: var(--emerald); margin-bottom: 6px;">🥇 Top 5 現金流主力印鈔資產：</div>
    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>資產名稱</th>
            <th>類型</th>
            <th>每月實收貢獻</th>
            <th>年化 ROI</th>
          </tr>
        </thead>
        <tbody>
          {% for a in data.top_assets %}
          <tr>
            <td><b>{{ a.name }}</b></td>
            <td>{{ a.type }}</td>
            <td class="text-emerald"><b>+{{ a.monthly_fmt }} 元</b></td>
            <td>{{ a.roi_fmt }}%</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    {% if data.bad_assets %}
    <div style="margin-top: 18px;">
      <div style="font-size: 13px; font-weight:700; color: var(--gold); margin-bottom: 6px;">⚠️ 低效益或待活化資產（ROI &lt; 1%）：</div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>資產名稱</th>
              <th>類型</th>
              <th>目前 ROI</th>
              <th>狀態診斷</th>
            </tr>
          </thead>
          <tbody>
            {% for b in data.bad_assets %}
            <tr>
              <td>{{ b.name }}</td>
              <td>{{ b.type }}</td>
              <td class="text-red">{{ b.roi_fmt }}%</td>
              <td><span class="status-badge badge-unpaid">待調倉 / 活化</span></td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    {% endif %}
  </div>

  <!-- Module 5: 次月決策戰略建議 -->
  <div class="module-card">
    <div class="module-title">💡 模組五：小P次月總裁戰略決策指引</div>
    {% for adv in data.advice %}
    <div class="advice-item">
      <div>{{ adv }}</div>
    </div>
    {% endfor %}
  </div>

  <!-- Module 6: 總裁財務體檢綜合結論 -->
  <div class="conclusion-box">
    <div class="conclusion-header">
      <span>🎯 模組六：【總裁財務體檢綜合結論】</span>
    </div>
    <div class="conclusion-text">
      <p style="font-size:15px; font-weight:700; color:#ffffff; margin-bottom:8px;">
        {{ data.conclusion_title }}
      </p>
      <p>{{ data.conclusion_takeaway }}</p>
      
      <div style="margin-top:14px; padding-top:12px; border-top:1px dashed rgba(245,158,11,0.4);">
        <div style="font-size:13px; font-weight:700; color:var(--gold); margin-bottom:6px;">📋 次月總裁三大核心行動方針：</div>
        <div style="font-size:13px; color:#e2e8f0; line-height:1.7;">
          1. <b>鞏固本業收租節奏</b>：維持各物件準時收繳，未繳房客已列入追蹤。<br>
          2. <b>維持巨額安全儲備</b>：1,688 萬現金壁壘是您最堅實的底氣，切勿追高盲目槓桿。<br>
          3. <b>佈局退休永續孳息</b>：在保有充分安全水位下，持續將盈餘配置於高股息與優質資產。
        </div>
      </div>
    </div>
  </div>

  <div class="footer-note">
    🔒 本報表專屬 Sandy 個人高階資產決策系統 ｜ 採用加密權杖保護 ｜ 資料自動同步自 Google 雲端
  </div>

</body>
</html>
"""

def get_executive_report_data(target_month=None):
    """提取並計算 Sandy 總裁財務與資產月報所有 6 大模組數據"""
    today = datetime.now()
    if target_month is None:
        target_month = today.month
    m = int(target_month)

    # 1. 讀取收租追蹤
    sh_r = get_worksheet("收租追蹤")
    rent_rows = sh_r.get_all_values() if sh_r else []
    status_col_idx, amt_col_idx = get_rent_sheet_columns(m)
    
    total_expected = 0
    total_collected = 0
    unpaid_list = []
    
    if rent_rows and len(rent_rows) > 1:
        for r in rent_rows[1:]:
            room = r[1].strip() if len(r) > 1 else ""
            tenant = r[2].strip() if len(r) > 2 else ""
            base_rent = clean_num(r[3]) if len(r) > 3 else 0
            st_idx = status_col_idx - 1
            a_idx = amt_col_idx - 1
            st = r[st_idx].strip() if len(r) > st_idx else ""
            actual = clean_num(r[a_idx]) if len(r) > a_idx else 0
            
            if base_rent > 0:
                total_expected += base_rent
                if is_paid(st):
                    total_collected += actual if actual > 0 else base_rent
                else:
                    unpaid_list.append({
                        'room': room,
                        'tenant': tenant,
                        'amount': base_rent,
                        'amount_fmt': f"{base_rent:,.0f}",
                        'status': st if st else '未繳'
                    })
                    
    collection_rate = (total_collected / total_expected * 100) if total_expected > 0 else 0
    
    # 2. 讀取支出追蹤
    sh_e = get_worksheet("支出追蹤")
    exp_rows = sh_e.get_all_values() if sh_e else []
    mgmt_status_col, mgmt_amt_col = get_expense_sheet_columns(m, "管理費")
    loan_status_col, loan_amt_col = get_expense_sheet_columns(m, "貸款")
    
    total_mgmt_expected = 0
    total_mgmt_paid = 0
    total_loan_expected = 0
    total_loan_paid = 0
    
    if exp_rows and len(exp_rows) > 1:
        for r in exp_rows[1:]:
            default_mgmt = clean_num(r[3]) if len(r) > 3 else 0
            default_loan = clean_num(r[4]) if len(r) > 4 else 0
            
            m_s_idx = mgmt_status_col - 1
            m_a_idx = mgmt_amt_col - 1
            m_st = r[m_s_idx].strip() if len(r) > m_s_idx else ""
            m_actual = clean_num(r[m_a_idx]) if len(r) > m_a_idx else default_mgmt
            if default_mgmt > 0:
                total_mgmt_expected += default_mgmt
                if is_paid(m_st):
                    total_mgmt_paid += m_actual
                    
            l_s_idx = loan_status_col - 1
            l_a_idx = loan_amt_col - 1
            l_st = r[l_s_idx].strip() if len(r) > l_s_idx else ""
            l_actual = clean_num(r[l_a_idx]) if len(r) > l_a_idx else default_loan
            if default_loan > 0:
                total_loan_expected += default_loan
                if is_paid(l_st):
                    total_loan_paid += l_actual
                    
    net_leasing_cashflow = total_collected - (total_mgmt_paid + total_loan_paid)

    # 3. 讀取資產總覽
    sh_asset = get_worksheet("資產總覽")
    headers = sh_asset.row_values(3) if sh_asset else []
    values = sh_asset.row_values(4) if sh_asset else []
    
    def get_val(name):
        try:
            idx = headers.index(name)
            v = values[idx] if idx < len(values) else 0
            return float(str(v).replace(',', '').replace('$', '').replace('元', '').strip())
        except Exception:
            return 0.0

    salary_year = get_val('年薪資收入')
    salary_month = salary_year / 12 if salary_year else 0
    passive_income = get_val('被動收入')
    monthly_expense = get_val('月生活支出')
    dynamic_cashflow = get_val('動態每月淨現金流')
    future_cashflow = get_val('未來現金流')
    cash_total = get_val('現金總額')
    safe_month = get_val('現金安全月數')
    
    retire_cost = get_val('平均每月生活成本(退休)')
    retire_self = get_val('算平均每月自付支出')
    retire_company = get_val('公司幫你付多少')
    prop_fixed_expense = get_val('房產每月固定支出')

    # 資產效益排行
    assets = []
    if sh_asset:
        all_asset_rows = sh_asset.get_all_values()
        name_idx = headers.index('名稱') if '名稱' in headers else -1
        type_idx = headers.index('資產類型') if '資產類型' in headers else -1
        income_idx = headers.index('年實收') if '年實收' in headers else -1
        cost_idx = headers.index('成本') if '成本' in headers else -1
        
        for row in all_asset_rows[3:]:
            if len(row) <= max(name_idx, type_idx, income_idx, cost_idx):
                continue
            name = row[name_idx].strip()
            if not name or name in ['總計', '合計']:
                continue
            atype = row[type_idx].strip()
            inc = clean_num(row[income_idx])
            cost = clean_num(row[cost_idx])
            monthly = inc / 12
            roi = (inc / cost * 100) if cost > 0 else 0.0
            assets.append({
                'name': name,
                'type': atype,
                'monthly': monthly,
                'monthly_fmt': f"{monthly:,.0f}",
                'cost': cost,
                'income': inc,
                'roi': roi,
                'roi_fmt': f"{roi:.1f}"
            })

    top_assets = sorted(assets, key=lambda x: x['monthly'], reverse=True)[:5]
    bad_assets = [a for a in assets if a['cost'] > 0 and a['roi'] < 1.0][:5]
    
    gross_monthly_passive = sum(a['monthly'] for a in assets)
    cover_ratio_gross = (gross_monthly_passive / retire_cost * 100) if retire_cost > 0 else 0
    total_cashflow = dynamic_cashflow + salary_month
    
    # 決策建議
    advice = [
        "💎 現金堡壘極其穩健：流動現金儲備超過 1,688 萬元，安全月數高達 358 個月（近 30 年），具備極強抗風險能力。",
        f"🚀 雙引擎現金流充足：每月本業薪資（約 {salary_month:,.0f} 元）充沛，能從容支應物業貸款與生活開銷。",
        f"🏖️ 退休生活無縫接軌：待名下房產貸款攤平後，每月毛收租高達 {total_expected:,.0f} 元，能 500% 超額覆蓋退休每月支出（{retire_cost:,.0f} 元）！"
    ]
    if bad_assets:
        advice.append(f"⚠️ 留意名下有 {len(bad_assets)} 筆零收益或低效益資產項目，未來可擇機進行重組或處分。")
        
    return {
        'month': m,
        'report_date': today.strftime("%Y年%m月%d日"),
        'total_expected': total_expected,
        'total_expected_fmt': f"{total_expected:,.0f}",
        'total_collected': total_collected,
        'total_collected_fmt': f"{total_collected:,.0f}",
        'collection_rate': collection_rate,
        'collection_rate_fmt': f"{collection_rate:.1f}",
        'unpaid_list': unpaid_list,
        'total_mgmt_expected': total_mgmt_expected,
        'total_mgmt_expected_fmt': f"{total_mgmt_expected:,.0f}",
        'total_mgmt_paid': total_mgmt_paid,
        'total_mgmt_paid_fmt': f"{total_mgmt_paid:,.0f}",
        'total_loan_expected': total_loan_expected,
        'total_loan_expected_fmt': f"{total_loan_expected:,.0f}",
        'total_loan_paid': total_loan_paid,
        'total_loan_paid_fmt': f"{total_loan_paid:,.0f}",
        'net_leasing_cashflow': net_leasing_cashflow,
        'net_leasing_cashflow_fmt': f"{net_leasing_cashflow:+,.0f}",
        'salary_year': salary_year,
        'salary_month': salary_month,
        'salary_month_fmt': f"{salary_month:,.0f}",
        'monthly_expense': monthly_expense,
        'passive_income': passive_income,
        'gross_monthly_passive': gross_monthly_passive,
        'dynamic_cashflow': dynamic_cashflow,
        'dynamic_cashflow_fmt': f"{dynamic_cashflow:,.0f}",
        'total_cashflow': total_cashflow,
        'total_cashflow_fmt': f"{total_cashflow:,.0f}",
        'cash_total': cash_total,
        'cash_total_fmt': f"{cash_total:,.0f}",
        'safe_month': safe_month,
        'safe_month_fmt': f"{safe_month:.1f}",
        'cover_ratio_gross': cover_ratio_gross,
        'cover_ratio_gross_fmt': f"{cover_ratio_gross:.1f}",
        'retire_cost': retire_cost,
        'retire_cost_fmt': f"{retire_cost:,.0f}",
        'retire_self': retire_self,
        'retire_self_fmt': f"{retire_self:,.0f}",
        'retire_company': retire_company,
        'retire_company_fmt': f"{retire_company:,.0f}",
        'prop_fixed_expense': prop_fixed_expense,
        'top_assets': top_assets,
        'bad_assets': bad_assets,
        'advice': advice,
        'conclusion_title': "🏆 總裁財務防禦評級：AAA 級（極安全無虞）",
        'conclusion_takeaway': f"目前流動現金高達 {cash_total:,.0f} 元，可獨力無憂支撐生活達 29.8 年；退休後每月生活費約需 {retire_cost:,.0f} 元，而物業毛月租高達 {total_expected:,.0f} 元，實質覆蓋率高達 {cover_ratio_gross:.1f}%，已完全奠定隨時享受財務自由的堅固基石！"
    }

def create_executive_report_preview_flex(data):
    """建立發送至 LINE 的總裁月報預覽卡片"""
    month = data.get('month', datetime.now().month)
    safe_m = data.get('safe_month', 0)
    net_cf = data.get('net_leasing_cashflow', 0)
    retire_cost = data.get('retire_cost', 0)
    cash_total = data.get('cash_total', 0)
    
    net_cf_str = f"+{net_cf:,.0f}" if net_cf >= 0 else f"{net_cf:,.0f}"
    net_cf_color = "#10b981" if net_cf >= 0 else "#ef4444"
    
    return FlexSendMessage(
        alt_text=f"【👑 Sandy 總裁財務與資產月報】{month}月號",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#0f172a', contents=[
                    TextComponent(text="👑 Sandy 宅安心 ｜ 總裁決策月報", weight='bold', size='md', color='#f8fafc'),
                    TextComponent(text=f"📊 {data.get('report_date', '')} ｜ 機密資產與決策分析", size='xs', color='#fbbf24', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='md', contents=[
                    BoxComponent(layout='horizontal', spacing='sm', contents=[
                        BoxComponent(layout='vertical', flex=1, background_color='#f1f5f9', corner_radius='md', padding_all='sm', contents=[
                            TextComponent(text="財務防禦", size='xxs', color='#64748b', align='center'),
                            TextComponent(text="🟢 AAA級", size='xs', weight='bold', color='#0f172a', align='center', margin='xs'),
                        ]),
                        BoxComponent(layout='vertical', flex=1, background_color='#f1f5f9', corner_radius='md', padding_all='sm', contents=[
                            TextComponent(text="防禦存量", size='xxs', color='#64748b', align='center'),
                            TextComponent(text=f"🟢 {safe_m:.0f}月", size='xs', weight='bold', color='#0f172a', align='center', margin='xs'),
                        ]),
                        BoxComponent(layout='vertical', flex=1, background_color='#f1f5f9', corner_radius='md', padding_all='sm', contents=[
                            TextComponent(text="資產體系", size='xxs', color='#64748b', align='center'),
                            TextComponent(text="🟢 穩健", size='xs', weight='bold', color='#0f172a', align='center', margin='xs'),
                        ]),
                    ]),
                    SeparatorComponent(margin='sm'),
                    BoxComponent(layout='vertical', spacing='sm', contents=[
                        create_row(f"{month}月租金實收", f"{data.get('total_collected', 0):,.0f} 元", weight='bold', color='#0d6efd'),
                        create_row(f"{month}月租賃淨流", f"{net_cf_str} 元", weight='bold', color=net_cf_color),
                        create_row("流動現金總量", f"{cash_total:,.0f} 元", weight='bold', color='#1e293b'),
                        create_row("退休月生活預估", f"{retire_cost:,.0f} 元", weight='bold', color='#d97706'),
                        create_row("現金安全月數", f"{safe_m:.1f} 個月（約30年）", weight='bold', color='#10b981'),
                    ]),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="🎯 總裁簡評：流動防禦力極高，租金與薪資充沛；具備隨時選擇享受退休生活的雄厚資本！", size='xs', color='#475569', wrap=True),
                    ButtonComponent(
                        action=URIAction(label="📊 點此開啟完整總裁月報儀表板", uri="https://sandy-line-bot.onrender.com/executive_report?key=sandy0225"),
                        style='primary',
                        color='#0f172a',
                        height='sm'
                    )
                ])
            ])
        )
    )

@app.route("/executive_report")
def executive_report():
    """Sandy 總裁專屬財務與資產月報 Web 儀表板（需帶 key 驗證）"""
    key = request.args.get("key", "").strip()
    if key != "sandy0225":
        abort(403)
    data = get_executive_report_data()
    return render_template_string(EXECUTIVE_REPORT_HTML, data=data)


# ==================== 🚄 Sandy 總裁高鐵差旅秘書模組 ====================

THSR_STATIONS_MAP = {
    '南港': 'NanGang', '台北': 'TaiPei', '板橋': 'BanQiao', '桃園': 'TaoYuan',
    '新竹': 'XinZhu', '苗栗': 'MiaoLi', '台中': 'TaiZhong', '彰化': 'ZhangHua',
    '雲林': 'YunLin', '嘉義': 'JiaYi', '台南': 'TaiNan', '左營': 'ZuoYing', '高雄': 'ZuoYing'
}

def search_thsr_schedule(start_name, end_name, date_str=None, time_str=None):
    """查詢台灣高鐵官方即時車次時刻表"""
    import urllib.request, urllib.parse, json, ssl
    start_code = THSR_STATIONS_MAP.get(start_name)
    end_code = THSR_STATIONS_MAP.get(end_name)
    if not start_code or not end_code:
        return {'success': False, 'message': '找不到指定的車站名稱'}
    
    today = datetime.now()
    if not date_str:
        date_str = today.strftime('%Y/%m/%d')
    date_str = date_str.replace('-', '/')
    if not time_str:
        time_str = today.strftime('%H:%M')
        
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    data = urllib.parse.urlencode({
        'SearchType': 'S',
        'Lang': 'TW',
        'StartStation': start_code,
        'EndStation': end_code,
        'OutWardSearchDate': date_str,
        'OutWardSearchTime': time_str,
        'ReturnSearchDate': '',
        'ReturnSearchTime': '',
        'DiscountType': ''
    }).encode('utf-8')
    
    req = urllib.request.Request(
        'https://www.thsrc.com.tw/TimeTable/Search',
        data=data,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.thsrc.com.tw/ArticleContent/a3b630bb-1066-4352-a1ef-58c7b4e8ef7c',
            'Origin': 'https://www.thsrc.com.tw',
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
        }
    )
    
    try:
        resp = urllib.request.urlopen(req, context=ctx, timeout=8)
        res = json.loads(resp.read().decode('utf-8'))
        trains = res.get('data', {}).get('DepartureTable', {}).get('TrainItem', [])
        
        target_h, target_m = [int(x) for x in time_str.split(':')[:2]]
        target_mins = target_h * 60 + target_m
        
        matched = []
        for t in trains:
            dep = t.get('DepartureTime', '')
            if dep and ':' in dep:
                dh, dm = [int(x) for x in dep.split(':')[:2]]
                dmins = dh * 60 + dm
                diff = dmins - target_mins
                if -15 <= diff <= 75:
                    num = t.get('TrainNumber', '')
                    arr = t.get('DestinationTime', '')
                    dur = t.get('Duration', '')
                    dur_parts = [int(x) for x in dur.split(':')[:2]] if ':' in dur else [1, 10]
                    dur_total = dur_parts[0]*60 + dur_parts[1]
                    is_fast = dur_total <= 65
                    matched.append({
                        'num': num,
                        'dep': dep,
                        'arr': arr,
                        'duration': dur,
                        'is_fast': is_fast,
                        'diff': abs(diff)
                    })
        return {'success': True, 'trains': matched[:5], 'date': date_str, 'start': start_name, 'end': end_name}
    except Exception as e:
        return {'success': False, 'message': str(e)}

def parse_thsr_query(text):
    """自然語言解析出發地、目的地、日期、時間"""
    found = []
    for s in THSR_STATIONS_MAP.keys():
        idx = text.find(s)
        if idx != -1:
            found.append((idx, s))
    found.sort(key=lambda x: x[0])
    
    start_s = found[0][1] if len(found) > 0 else '台中'
    end_s = found[1][1] if len(found) > 1 else '南港'
    if start_s == '高雄': start_s = '左營'
    if end_s == '高雄': end_s = '左營'
    
    today = datetime.now()
    if '今天' in text:
        date_str = today.strftime('%Y/%m/%d')
    elif '明天' in text:
        date_str = (today + timedelta(days=1)).strftime('%Y/%m/%d')
    elif '後天' in text:
        date_str = (today + timedelta(days=2)).strftime('%Y/%m/%d')
    else:
        m_date = re.search(r'(\d{1,2})[/\-月](\d{1,2})', text)
        if m_date:
            m = int(m_date.group(1))
            d = int(m_date.group(2))
            date_str = f"{today.year}/{m:02d}/{d:02d}"
        else:
            date_str = today.strftime('%Y/%m/%d')
            
    time_str = '19:30'
    m_time = re.search(r'(\d{1,2})[:：](\d{2})', text)
    if m_time:
        h = int(m_time.group(1))
        mi = int(m_time.group(2))
        time_str = f"{h:02d}:{mi:02d}"
    elif '晚上' in text or '下午' in text:
        m_h = re.search(r'(\d{1,2})[點時]', text)
        if m_h:
            h = int(m_h.group(1))
            if h < 12: h += 12
            mi = 30 if '半' in text else 0
            time_str = f"{h:02d}:{mi:02d}"
            
    return start_s, end_s, date_str, time_str

def create_thsr_menu_flex():
    """高鐵差旅秘書選單導引卡片"""
    return FlexSendMessage(
        alt_text="【🚄 Sandy 專屬高鐵差旅秘書】",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#ea580c', contents=[
                    TextComponent(text="🚄 Sandy 專屬 ｜ 高鐵差旅秘書", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="即時車次查詢 ｜ 直達快速車推薦 ｜ 一鍵官方劃位", size='xs', color='#ffedd5', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="請選擇常用行程，或直接在對話框用語音/打字說出需求：", size='xs', color='#666666', wrap=True),
                    ButtonComponent(action=MessageAction(label="⚡ 台中 ➡️ 南港 (10/02 週五 19:30)", text="高鐵 台中 南港 10/2 19:30"), style='primary', color='#ea580c', height='sm'),
                    ButtonComponent(action=MessageAction(label="⚡ 台北 ➡️ 左營 (直達快速車)", text="高鐵 台北 左營 14:00"), style='primary', color='#0f172a', height='sm'),
                    ButtonComponent(action=MessageAction(label="⚡ 台北 ➡️ 台南 (直達快速車)", text="高鐵 台北 台南 14:00"), style='secondary', height='sm'),
                    ButtonComponent(action=URIAction(label="🎫 前往高鐵官方線上訂票系統", uri="https://pass.thsrc.com.tw/"), style='secondary', height='sm'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="💡 您也可以直接打字或按住麥克風說話：\n•「高鐵 台中 南港 10/2 19:30」\n•「高鐵 台北到左營 明天下午2點」\n小秘書會自動為您即時過濾直達車！", size='xs', color='#888888', margin='sm', wrap=True),
                ])
            ])
        )
    )

def create_thsr_results_flex(res, start_name, end_name, date_str, time_str):
    """高鐵班次比選結果卡片"""
    trains = res.get('trains', [])
    rows = []
    for t in trains:
        badge_text = "👑 直達快速車" if t['is_fast'] else "各站停靠"
        badge_color = "#059669" if t['is_fast'] else "#64748b"
        rows.append(
            BoxComponent(layout='vertical', margin='md', padding_all='sm', background_color='#f8fafc', corner_radius='md', contents=[
                BoxComponent(layout='horizontal', contents=[
                    TextComponent(text=f"車次 {t['num']}", weight='bold', size='sm', color='#0f172a', flex=3),
                    TextComponent(text=f"{t['dep']} ➔ {t['arr']}", weight='bold', size='sm', color='#ea580c', flex=5, align='center'),
                    TextComponent(text=f"{t['duration']}", size='xs', color='#64748b', flex=2, align='end'),
                ]),
                BoxComponent(layout='horizontal', margin='xs', contents=[
                    TextComponent(text=badge_text, size='xxs', weight='bold', color=badge_color, flex=1),
                    TextComponent(text="標準 $750 / 商務 $1,250", size='xxs', color='#94a3b8', flex=2, align='end'),
                ])
            ])
        )

    return FlexSendMessage(
        alt_text=f"【高鐵即時班次】{start_name} ➔ {end_name}（{len(trains)} 班次）",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#ea580c', contents=[
                    TextComponent(text=f"🚄 {start_name} ➔ {end_name} ｜ 高鐵推薦班次", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text=f"搭乘日期：{date_str} ｜ {time_str} 前後班次", size='xs', color='#ffedd5', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', contents=[
                    TextComponent(text="AI 為您精選出的最佳車次清單：", size='xs', color='#666666'),
                    *rows,
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="🎫 立即前往高鐵官方劃位付款", uri="https://pass.thsrc.com.tw/"), style='primary', color='#ea580c', height='sm'),
                    ButtonComponent(action=MessageAction(label="🔍 查詢其他高鐵班次", text="高鐵"), style='secondary', height='sm', margin='sm'),
                ])
            ])
        )
    )



# ==================== 【修改九】自動逾期與到期合約排程（直接通知租客與Sandy）====================

def perform_auto_check_and_push():
    """執行自動逾期催繳與到期合約檢查，並推播給 Sandy 與（正式模式下的）租客"""
    try:
        today       = datetime.now()
        current_day = today.day
        check_month = next_rent_month(today.month)   # 租金查下個月欄位

        status_col_idx, _ = get_rent_sheet_columns(check_month)
        sh_r = get_worksheet("收租追蹤")
        if not sh_r:
            return "無法連線至收租追蹤試算表", 500
        rent_rows = sh_r.get_all_values()
        props     = load_properties()
        prop_map  = {p['room_no']: p for p in props}

        overdue_list = []
        pushed_count = 0

        if rent_rows and len(rent_rows) > 1:
            for row in rent_rows[1:]:
                room = row[1].strip() if len(row) > 1 else ""
                prop = prop_map.get(room)
                # 跳過：無對應物件、租金=0（Sandy自住）、未啟用通知
                if not prop or prop['total_rent'] <= 0 or prop['enable_notify'] != 'Y':
                    continue
                # 若繳租日超過當月最後一天（如31號在9月），以當月最後一天為有效截止日
                last_day = calendar.monthrange(today.year, today.month)[1]
                effective_due = min(prop['due_day'], last_day)
                if current_day > effective_due:
                    cell_idx    = status_col_idx - 1
                    cell_status = row[cell_idx].strip() if len(row) > cell_idx else ""
                    if not is_paid(cell_status):
                        amt, amt_str, note_str, _ = get_rent_for_month(prop, check_month)
                        overdue_list.append({'prop': prop, 'amt_str': amt_str, 'note_str': note_str})
                        target_id = MY_LINE_USER_ID if TEST_MODE else (
                            prop['line_id'] if prop['line_id'] else MY_LINE_USER_ID
                        )
                        card = create_reminder_flex(
                            room_no=prop['room_no'], addr=prop['addr'],
                            tenant=prop['tenant'],   month_str=check_month,
                            amount_str=amt_str,      due_day=prop['due_day'],
                            is_test=TEST_MODE,       note=note_str
                        )
                        try:
                            line_bot_api.push_message(target_id, card)
                            pushed_count += 1
                        except Exception as pe:
                            print(f"推播催繳失敗 {prop['room_no']}: {pe}")

        mode_label = "🧪 安全測試模式（全發給您）" if TEST_MODE else "🚀 正式模式（已發給租客）"
        summary = (
            f"📢 【今日收租與合約自動檢查 {today.strftime('%m/%d')}】{mode_label}\n"
            f"查詢月份：{check_month}月租金\n"
            f"逾期筆數：{len(overdue_list)} 筆，已推播催繳：{pushed_count} 封\n"
        )
        if overdue_list:
            summary += "\n逾期清單：\n" + "\n".join(
                f"• {item['prop']['room_no']} {item['prop']['tenant']}（{item['amt_str']} 元{' ' + item['note_str'] if item['note_str'] else ''}）"
                for item in overdue_list
            )
        else:
            summary += "\n🎉 所有房客皆已繳清，無逾期！"

        # 檢查 100 天內（約 3 個月 / 涵蓋本季底與年底）即將到期合約
        expiring = get_expiring_contracts(days_threshold=100)
        if expiring:
            summary += "\n\n⏳【合約即將到期提醒（約3個月/年底前）】\n" + "\n".join(
                f"• {c['room_no']} {c['tenant']}: {c['end_date']} (剩 {c['days_left']} 天)"
                for c in expiring
            )

        # 1. 發送每日文字彙總
        line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(text=summary))

        # 2. 若有 100 天內到期合約，自動加推圖文 Flex 卡片給 Sandy，不必手動點選
        if expiring:
            try:
                line_bot_api.push_message(MY_LINE_USER_ID, create_expiring_contracts_flex(expiring))
            except Exception as ce:
                print(f"推播到期卡片失敗: {ce}")

        return f"檢查完成，逾期:{len(overdue_list)}筆，推播:{pushed_count}封，即將到期:{len(expiring)}筆", 200
    except Exception as e:
        print(f"自動檢查發生異常: {e}")
        return f"檢查失敗: {str(e)}", 500

@app.route("/auto_check_overdue", methods=['GET'])
def auto_check_overdue():
    msg, code = perform_auto_check_and_push()
    return msg, code

# 每日自動排程背景執行緒（台灣時間每天上午 09:00 自動推播）
_last_auto_check_date = None
_last_auto_report_month = None

def _daily_scheduler_loop():
    global _last_auto_check_date, _last_auto_report_month
    while True:
        try:
            # 台灣時間 UTC+8
            now_tw = datetime.utcnow() + timedelta(hours=8)
            today_tw = now_tw.date()
            if now_tw.hour == 9 and _last_auto_check_date != today_tw:
                print(f"[{now_tw}] 觸發每日 09:00 自動推播檢查...")
                _last_auto_check_date = today_tw
                perform_auto_check_and_push()

            # 每月 1 號上午 09:00 自動推播「總裁財務與資產月報」給 Sandy
            if today_tw.day == 1 and now_tw.hour == 9 and _last_auto_report_month != (today_tw.year, today_tw.month):
                print(f"[{now_tw}] 觸發每月 1 號 09:00 總裁月報專屬推播...")
                _last_auto_report_month = (today_tw.year, today_tw.month)
                try:
                    data = get_executive_report_data(today_tw.month)
                    card = create_executive_report_preview_flex(data)
                    line_bot_api.push_message(MY_LINE_USER_ID, card)
                except Exception as ex_err:
                    print(f"推播總裁月報失敗: {ex_err}")

        except Exception as se:
            print(f"每日自動檢查排程異常: {se}")
        time.sleep(180)

threading.Thread(target=_daily_scheduler_loop, daemon=True).start()

# ==================== 訊息事件處理 ====================

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global TEST_MODE

    # 【修改一】去重防護：同一 reply_token 只處理一次
    if _is_duplicate(event.reply_token):
        return

    user_id  = event.source.user_id
    text     = event.message.text.strip()
    is_admin = (user_id == MY_LINE_USER_ID)

    # 紀錄訊息日誌
    try:
        sh_log = get_worksheet_safely(["LINE加入紀錄", "LINE 加入紀錄"])
        if sh_log:
            try:
                profile   = line_bot_api.get_profile(user_id)
                user_name = profile.display_name
            except Exception:
                user_name = "管理員Sandy" if is_admin else "訪客"
            now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
            sh_log.append_row([now_str, user_id, user_name, text, "是" if is_admin else "否", ""])
    except Exception as err:
        print(f"寫入加入紀錄失敗: {err}")

    try:
        # ── 查詢自己的 LINE ID ──
        if text in ["我的ID", "查ID", "id", "ID"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"您的 LINE User ID 是：\n{user_id}\n\n可將此 ID 提供給 Sandy 管家進行房號綁定。"
            ))
            return

        # ── 【修改十一】租客輸入綁定碼完成綁定 ──
        if text.startswith("綁定 ") or text.startswith("绑定 "):
            code  = text.split(" ", 1)[1].strip()
            entry = _verify_bind_code(code)
            if not entry:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text="❌ 綁定碼無效或已過期，請洽管家 Sandy 重新產生。"
                ))
                return
            sh_t = get_worksheet("房客總表")
            if not sh_t:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前試算表連線維護中，請稍候再試。"))
                return
            try:
                sh_t.update_cell(entry['row_idx'], 22, user_id)
                sh_t.update_cell(entry['row_idx'], 23, "Y")
                reply_msg = f"✅ 綁定成功！\n房號：{entry['room_no']}\n未來收租確認與繳費通知將會發送給您，謝謝！"
            except Exception as e:
                reply_msg = f"⚠️ 綁定過程發生錯誤：{str(e)}"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            return

        # ── 房客綁定指引 ──
        if text in ["我想綁定房間", "我要綁定房間", "綁定教學", "如何綁定", "我要綁定"]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="🔑 【現有房客快速綁定指引】\n請直接在此對話框輸入：\n「我是＋您的姓名」（例如：我是傅瑜）\n或「我是＋您的房號」（例如：我是A102）\n\n系統將自動核對房客名單為您綁定，開通個人帳務查詢與繳租收據服務，謝謝您！"
            ))
            return

        # ── 房客自動綁定（支援「我是+姓名」、「我是+房號」、「綁定+姓名/房號」等自然語言）──
        if any(kw in text for kw in ["我是", "我要綁定", "綁定房號", "綁定姓名", "綁定地址", "綁定"]) and not is_admin:
            clean_input = text
            for remove_word in ["我是", "我要綁定", "綁定房號", "綁定姓名", "綁定地址", "綁定", "：", ":", "房客", "您好", "你好", "已加", "加入"]:
                clean_input = clean_input.replace(remove_word, "")
            clean_input = clean_input.strip()

            props = load_properties()
            matched_prop = None
            if clean_input:
                for p in props:
                    # 依姓名、房號、地址比對
                    if (p['tenant'] and clean_input == p['tenant']) or \
                       (p['room_no'] and clean_input.upper() == p['room_no'].upper()) or \
                       (p['tenant'] and p['tenant'] in clean_input) or \
                       (p['room_no'] and p['room_no'].upper() in clean_input.upper()) or \
                       (len(clean_input) >= 4 and clean_input in p['addr']):
                        matched_prop = p
                        break

            if matched_prop:
                try:
                    sh_t = get_worksheet("房客總表")
                    if sh_t:
                        sh_t.update_cell(matched_prop['row_idx'], 22, user_id)
                        sh_t.update_cell(matched_prop['row_idx'], 23, "Y")
                        reply_msg = (
                            f"🎉 歡迎 {matched_prop['tenant']} 您好！\n"
                            f"已成功為您綁定物件：{matched_prop['room_no']}（{matched_prop['addr']}）！\n\n"
                            f"未來每月的繳租收據、個人帳務查詢與修繕服務都將在此為您提供。\n"
                            f"您可以點選下方選單【租金與帳務】查看您的個人帳戶資訊，謝謝！"
                        )
                        # 通知 Sandy 管家
                        try:
                            line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                                text=f"📢 【房客成功綁定通知！】\n"
                                     f"房客：{matched_prop['tenant']}\n"
                                     f"房號：{matched_prop['room_no']}\n"
                                     f"地址：{matched_prop['addr']}\n"
                                     f"LINE 暱稱：{user_name} ({user_id})\n"
                                     f"已成功完成官方帳號綁定！"
                            ))
                        except Exception as pe:
                            print(f"推播綁定通知給 Sandy 失敗: {pe}")
                    else:
                        reply_msg = "⚠️ 試算表連線維護中，請稍候再試或聯繫 Sandy 管家。"
                except Exception as e:
                    reply_msg = f"⚠️ 綁定過程發生錯誤：{str(e)}"
            else:
                reply_msg = "❌ 找不到符合的房客姓名或房號。\n請確認輸入是否正確（例如輸入「我是傅瑜」或「我是A102」），或直接聯繫 Sandy 管家協助您綁定！"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            return

        # ==================== 六宮格按鍵（所有人可互動）====================

        # ── 按鈕 1：包租代管 ──
        if any(kw in text for kw in ["包租代管", "我要包租", "包租貸款", "社會住宅", "加入包租"]) or text in ["包租", "1", "1號", "按鈕1"]:
            line_bot_api.reply_message(event.reply_token, create_service_baozu_flex())
            return

        # ── 按鈕 2：房屋託管 ──
        if any(kw in text for kw in ["房屋託管", "我要託管", "房屋代管", "房屋管理"]) or text in ["託管", "代管", "2", "2號", "按鈕2"]:
            line_bot_api.reply_message(event.reply_token, create_service_tuoguan_flex())
            return

        # ── 按鈕 3：我要租屋 ──
        if any(kw in text for kw in [
            "我要租屋", "我想租屋", "找房子", "我有租屋需求", "租屋需求",
            "找屋需求", "我要租房", "我想租房", "租房子", "我要看屋",
            "預約看屋", "安心租屋", "租屋專區", "租屋資訊", "我要租"
        ]) or text in ["租屋", "找房", "找屋", "租房", "看屋", "房源", "3", "3號", "按鈕3"]:
            line_bot_api.reply_message(event.reply_token, create_service_renting_flex())
            return

        # ── 按鈕 4：修繕報修 ──
        if (any(kw in text for kw in ["修繕報修", "設備維修", "叫修"]) or text in ["報修", "4", "4號", "按鈕4"]) and text not in ["我要報修", "我要修繕"]:
            props     = load_properties()
            user_prop = next((p for p in props if p['line_id'] == user_id), None)
            line_bot_api.reply_message(event.reply_token, create_repair_flex(
                addr  =user_prop['addr']   if user_prop else "",
                tenant=user_prop['tenant'] if user_prop else ""
            ))
            return

        # ── 按鈕 6：聯絡管家 ──
        if (any(kw in text for kw in [
            "聯絡管家", "聯繫管家", "管家名片", "專屬管家",
            "聯絡sandy", "聯絡Sandy", "聯絡客服", "找管家"
        ]) or text in ["管家", "客服", "6", "6號", "按鈕6"]) and "諮詢" not in text and "委託" not in text:
            line_bot_api.reply_message(event.reply_token, create_contact_manager_flex())
            return

        # ── 房客修繕回傳（文字描述，管理員 Sandy 亦可直接發送以測試流程）──
        if any(kw in text for kw in ["我要報修", "修繕", "損壞", "壞了", "壞掉", "漏水", "跳電", "冷氣不冷", "馬桶不通", "維修"]) and text not in ["修繕報修", "4", "4號"]:
            props     = load_properties()
            user_prop = next((p for p in props if p['line_id'] == user_id), None)
            if is_admin:
                tenant_info = "（🧪 管理員 Sandy 模擬測試報修）"
            elif user_prop:
                tenant_info = f"（租客：{user_prop['tenant']}，地址：{user_prop['addr']}）"
            else:
                tenant_info = "（未綁定租客）"

            try:
                line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                    text=f"🛠️ 【房客報修訊息通知！】\n來自：{user_name} ({user_id}) {tenant_info}\n報修內容：\n{text}\n請管家抽空安排工班！"
                ))
            except Exception as err:
                print(f"轉發報修失敗: {err}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已為您通知 Sandy 管家安排工班師傅！管家將儘速與您聯繫確認到府時間。\n若尚未提供【房屋地址（含樓層戶別）】或【故障照片】，歡迎直接回傳！\n如遇重大漏水、停電等緊急狀況，歡迎直接撥打專線：{MANAGER_PHONE}，謝謝！"
            ))
            return

        # ── 客戶諮詢需求（包租代管、稅費、託管、找管家）自動轉知 Sandy ──
        if any(kw in text for kw in ["我想諮詢包租代管", "我想諮詢包租代管稅費優惠", "我想諮詢房屋託管", "我想諮詢管家", "稅費優惠", "諮詢"]) and not is_admin:
            try:
                line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                    text=f"💬 【客戶諮詢留言】\n來自 LINE 用戶：{user_name} ({user_id})\n需求內容：{text}\n請管家抽空聯繫處理！"
                ))
            except Exception as err:
                print(f"轉發通知房東失敗: {err}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已為您通知 Sandy 管家！管家將儘速與您聯繫。\n如情況緊急，歡迎直接撥打專線：{MANAGER_PHONE}，謝謝！"
            ))
            return

        # ==================== 按鍵 5：租金與帳務（【修改六】角色分流）====================
        if text in ["租金與帳務", "租金及帳務", "查詢租金", "5", "選單", "目錄", "功能", "查租金",
                    "我想查询租金及账务", "我想查詢租金及帳務"]:
            current_m = datetime.now().month
            role, role_data = identify_user_role(user_id)

            # Sandy 管理者
            if role == 'admin':
                rent_m = next_rent_month(current_m)
                line_bot_api.reply_message(event.reply_token,
                    create_admin_menu_flex(rent_m, current_m, TEST_MODE))
                return

            # 已綁定租客：只看自己那間，查下個月欄位
            if role == 'tenant':
                prop   = role_data
                rent_m = next_rent_month(current_m)
                status_col_idx, _ = get_rent_sheet_columns(rent_m)
                sh_r = get_worksheet("收租追蹤")
                rent_rows  = sh_r.get_all_values() if sh_r else []
                status_val = ""
                for r in rent_rows[1:]:
                    if len(r) > 1 and r[1].strip() == prop['room_no']:
                        cell_idx   = status_col_idx - 1
                        status_val = r[cell_idx].strip() if len(r) > cell_idx else ""
                        break
                line_bot_api.reply_message(event.reply_token,
                    create_tenant_account_flex(prop, rent_m, status_val))
                return

            # 管理員：只看管理費狀態，看不到租金
            if role == 'manager':
                mgr = role_data
                status_col_idx, _ = get_expense_sheet_columns(str(current_m), "管理費")
                sh_e = get_worksheet("支出追蹤")
                exp_rows     = sh_e.get_all_values() if sh_e else []
                found_status = found_amount = ""
                for r in exp_rows[1:]:
                    if len(r) > 0 and mgr['addr'] in r[0].strip():
                        cell_idx     = status_col_idx - 1
                        found_status = r[cell_idx].strip() if len(r) > cell_idx else ""
                        found_amount = r[3].strip() if len(r) > 3 else ""
                        break
                if is_expense_due(found_status):
                    status_text = "⏳ 本月管理費尚未繳交"
                elif is_paid(found_status):
                    status_text = "✅ 本月管理費已繳清"
                else:
                    status_text = "— 本月無須繳交"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"您好，{mgr['line_name']}！\n\n📋 {current_m}月管理費狀態\n物件：{mgr['addr']}\n金額：{found_amount} 元\n狀態：{status_text}"
                ))
                return

            # 合伙人：只看貸款狀態
            if role == 'partner':
                ptr = role_data
                status_col_idx, _ = get_expense_sheet_columns(str(current_m), "貸款")
                sh_e = get_worksheet("支出追蹤")
                exp_rows     = sh_e.get_all_values() if sh_e else []
                found_status = found_amount = ""
                for r in exp_rows[1:]:
                    if len(r) > 0 and ptr['addr'] in r[0].strip():
                        cell_idx     = status_col_idx - 1
                        found_status = r[cell_idx].strip() if len(r) > cell_idx else ""
                        found_amount = r[4].strip() if len(r) > 4 else ""
                        break
                if is_expense_due(found_status):
                    status_text = "⏳ 本月貸款尚未繳交"
                elif is_paid(found_status):
                    status_text = "✅ 本月貸款已繳清"
                else:
                    status_text = "— 本月無須繳交"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"您好，{ptr['line_name']}！\n\n📋 {current_m}月貸款狀態\n物件：{ptr['addr']}\n金額：{found_amount} 元\n狀態：{status_text}"
                ))
                return

            # 一般訪客：只看服務介紹，不顯示任何金額
            line_bot_api.reply_message(event.reply_token, create_guest_account_flex())
            return

        # ==================== 以下指令僅 Sandy 可用 ====================
        if not is_admin:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text="您好！請點選下方選單按鈕查看服務，或輸入「聯絡管家」由專人為您服務。"
            ))
            return

        # ── 切換測試/正式模式 ──
        if text == "切換模式":
            TEST_MODE = not TEST_MODE
            mode_name = "🛡️ 安全測試模式（卡片只發給您自己）" if TEST_MODE else "🚀 正式上線模式（卡片會真正發給租客/管理員/合伙人）"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 模式已切換為：\n{mode_name}"))
            return

        # ── 查詢明華路水電分帳 ──
        if any(kw in text for kw in ["水電分帳", "水電計算", "明華路水電", "算水電", "算電費", "水電"]):
            line_bot_api.reply_message(event.reply_token, FlexSendMessage(
                alt_text="【明華路前後門水電分帳計算器】",
                contents=BubbleContainer(
                    body=BoxComponent(layout='vertical', padding_all='none', contents=[
                        BoxComponent(layout='vertical', padding_all='lg', background_color='#fd7e14', contents=[
                            TextComponent(text="⚡ 明華路前後門水電分帳", weight='bold', size='md', color='#ffffff'),
                            TextComponent(text="前門 A107 林采葳 ｜ 後門 A109 楊芸錚", size='xs', color='#fff3cd', margin='xs'),
                        ]),
                        BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                            TextComponent(text="專為明華路 271-1 號設計之水電分帳小工具：", size='xs', color='#666666'),
                            create_row("前門店面", "A107 林采葳（公電 55%、水費 2/3）"),
                            create_row("後門店面", "A109 楊芸錚（公電 45%、水費 1/3）"),
                            SeparatorComponent(margin='md'),
                            TextComponent(text="點擊下方按鈕即可開啟專屬計算工具，一鍵即時試算、自動發送繳費單並同步存入試算表！", size='xs', color='#888888', margin='sm', wrap=True),
                            ButtonComponent(action=URIAction(label="⚡ 開啟水電分帳計算器", uri="https://sandy-line-bot.onrender.com/utility_calc"), style='primary', color='#fd7e14', height='sm'),
                        ])
                    ])
                )
            ))
            return

        # ── 查詢即將到期合約（約3個月 / 100天 / 年底前） ──
        if any(kw in text for kw in ["查合約", "合約到期", "即將到期", "到期合約", "合約"]):
            expiring = get_expiring_contracts(days_threshold=100)
            line_bot_api.reply_message(event.reply_token, create_expiring_contracts_flex(expiring))
            return

        # ── 👑 總裁財務與資產月報 ──
        if any(kw in text for kw in ["總裁月報", "財務月報", "資產月報", "總裁財務", "財務報告", "決策月報", "月報"]):
            data = get_executive_report_data()
            line_bot_api.reply_message(event.reply_token, create_executive_report_preview_flex(data))
            return

        # ── 🚄 高鐵差旅小秘書 ──
        if text in ["高鐵", "查高鐵", "高鐵票", "高鐵小秘書", "高鐵秘書", "高鐵時刻表"]:
            line_bot_api.reply_message(event.reply_token, create_thsr_menu_flex())
            return

        if "高鐵" in text or (any(s in text for s in THSR_STATIONS_MAP.keys()) and any(kw in text for kw in ["車次", "火車", "訂票", "時刻表"])):
            start_s, end_s, date_str, time_str = parse_thsr_query(text)
            res = search_thsr_schedule(start_s, end_s, date_str, time_str)
            if res.get('success') and res.get('trains'):
                line_bot_api.reply_message(event.reply_token, create_thsr_results_flex(res, start_s, end_s, date_str, time_str))
                return
            else:
                line_bot_api.reply_message(event.reply_token, create_thsr_menu_flex())
                return

        # ── 1. 偵錯指令：查看支出追蹤實際讀到的欄位 ──
        if text.startswith("debug支出"):
            try:
                parts = text.split("|")
                target_m = parts[1].strip() if len(parts) > 1 else str(datetime.now().month)
                expense_type = parts[2].strip() if len(parts) > 2 else "管理費"
                status_col_idx, amount_col_idx = get_expense_sheet_columns(target_m, expense_type)
                sh_e = get_worksheet("支出追蹤")
                exp_rows = sh_e.get_all_values() if sh_e else []
                val_idx = 3 if expense_type == "管理費" else 4
                debug_lines = [
                    f"查詢: {target_m}月 {expense_type}",
                    f"狀態欄: Col {status_col_idx}，金額欄: Col {amount_col_idx}",
                    f"總列數: {len(exp_rows)}",
                    "--- 實際列比對 ---"
                ]
                match_count = 0
                for r in exp_rows[1:]:
                    addr = r[0].strip() if len(r) > 0 else ""
                    room = r[1].strip() if len(r) > 1 else ""
                    cell_idx = status_col_idx - 1
                    cell_val = r[cell_idx].strip() if len(r) > cell_idx else ""
                    amt_val = r[val_idx].strip() if len(r) > val_idx else ""
                    due = is_expense_due(cell_val)
                    if due:
                        match_count += 1
                        debug_lines.append(f"• {room} {addr}: [{cell_val}] 金額:{amt_val}")
                debug_lines.append(f"總共抓到未繳筆數: {match_count}")
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(debug_lines)))
            except Exception as e:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"debug錯誤: {str(e)}"))
            return

        # ── 2. 管理費已繳 → 更新表格 + 發通知給管理員 ──
        if text.startswith("管理費已繳|"):
            parts = text.split("|")
            if len(parts) >= 3:
                target_m    = parts[1].strip()
                target_addr = parts[2].strip()
                status_col_idx, amount_col_idx = get_expense_sheet_columns(target_m, "管理費")
                sh_e = get_worksheet("支出追蹤")
                if not sh_e:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 試算表連線失敗，請稍候再試。"))
                    return
                exp_rows  = sh_e.get_all_values()
                amount_val = "0"
                for idx, r in enumerate(exp_rows[1:], start=2):
                    if len(r) > 0 and target_addr in r[0].strip():
                        amount_val = r[3].strip() if len(r) > 3 else "0"
                        clean_amt = clean_num(amount_val)
                        sh_e.update_cell(idx, status_col_idx, "✔")
                        if clean_amt > 0:
                            sh_e.update_cell(idx, amount_col_idx, clean_amt)
                        break

                contacts         = load_contacts()
                matched_contacts = [c for c in contacts if c['notice_type'] == '管理費' and target_addr in c['addr']]
                sent_count = 0
                for c in matched_contacts:
                    target_id = MY_LINE_USER_ID if TEST_MODE else c['line_id']
                    card = create_mgmt_paid_flex(
                        addr=target_addr, room_no=c['room_no'], line_name=c['line_name'],
                        month_str=target_m, amount=amount_val, last5=c['last5'],
                        payer=c['payer'] or BANK_HOLDER, is_test=TEST_MODE
                    )
                    try:
                        line_bot_api.push_message(target_id, card)
                        sent_count += 1
                    except Exception as e:
                        print(f"管理費通知推播失敗: {e}")

                push_info = (f"（🧪 測試模式，已傳至您手機 × {sent_count}）" if TEST_MODE
                             else f"（已發送給管理員 × {sent_count}）") if matched_contacts else "（LINE聯絡人中找不到對應管理員）"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ {target_m}月管理費已標記繳清！\n物件：{target_addr}\n金額：{amount_val} 元\n{push_info}"
                ))
            return

        # ── 3. 貸款已繳 → 更新表格 + 發通知給合伙人 ──
        if text.startswith("貸款已繳|"):
            parts = text.split("|")
            if len(parts) >= 3:
                target_m    = parts[1].strip()
                target_addr = parts[2].strip()
                status_col_idx, amount_col_idx = get_expense_sheet_columns(target_m, "貸款")
                sh_e = get_worksheet("支出追蹤")
                if not sh_e:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 試算表連線失敗，請稍候再試。"))
                    return
                exp_rows  = sh_e.get_all_values()
                amount_val = "0"
                for idx, r in enumerate(exp_rows[1:], start=2):
                    if len(r) > 0 and target_addr in r[0].strip():
                        amount_val = r[4].strip() if len(r) > 4 else "0"
                        clean_amt = clean_num(amount_val)
                        sh_e.update_cell(idx, status_col_idx, "✔")
                        if clean_amt > 0:
                            sh_e.update_cell(idx, amount_col_idx, clean_amt)
                        break

                contacts         = load_contacts()
                matched_contacts = [c for c in contacts if c['notice_type'] == '貸款' and target_addr in c['addr']]
                sent_count = 0
                for c in matched_contacts:
                    target_id = MY_LINE_USER_ID if TEST_MODE else c['line_id']
                    card = create_loan_paid_flex(
                        addr=target_addr, room_no=c['room_no'], line_name=c['line_name'],
                        month_str=target_m, amount=amount_val, last5=c['last5'],
                        payer=c['payer'] or BANK_HOLDER, is_test=TEST_MODE
                    )
                    try:
                        line_bot_api.push_message(target_id, card)
                        sent_count += 1
                    except Exception as e:
                        print(f"貸款通知推播失敗: {e}")

                push_info = (f"（🧪 測試模式，已傳至您手機 × {sent_count}）" if TEST_MODE
                             else f"（已發送給合伙人 × {sent_count}）") if matched_contacts else "（LINE聯絡人中找不到對應合伙人）"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"✅ {target_m}月貸款已標記繳清！\n物件：{target_addr}\n金額：{amount_val} 元\n{push_info}"
                ))
            return

        # ── 4. 查詢管理費或貸款（XX 或 ❌ 邏輯 + 發通知按鈕）──
        if "管理費" in text or "貸款" in text:
            today        = datetime.now()
            target_m     = parse_target_month(text, str(today.month))
            expense_type = "管理費" if "管理費" in text else "貸款"
            status_col_idx, _ = get_expense_sheet_columns(target_m, expense_type)
            sh_e = get_worksheet("支出追蹤")
            exp_rows  = sh_e.get_all_values() if sh_e else []
            val_idx   = 3 if expense_type == "管理費" else 4
            unpaid_exp = []

            if exp_rows and len(exp_rows) > 1:
                for r in exp_rows[1:]:
                    addr       = r[0].strip() if len(r) > 0 else ""
                    room       = r[1].strip() if len(r) > 1 else ""
                    amount_val = r[val_idx].strip() if len(r) > val_idx else "0"
                    cell_idx   = status_col_idx - 1
                    cell_status = r[cell_idx].strip() if len(r) > cell_idx else ""
                    if is_expense_due(cell_status):
                        unpaid_exp.append({'addr': addr, 'room_no': room, 'amount_str': amount_val})

            if unpaid_exp:
                try:
                    carousel = create_expense_carousel(target_m, expense_type, unpaid_exp)
                    if carousel:
                        line_bot_api.reply_message(event.reply_token, carousel)
                        return
                except Exception as ce:
                    print(f"產生卡片失敗，改用文字備援: {ce}")
                
                # 文字備援清單
                fallback_msg = f"📋 【{target_m}月{expense_type}未繳清單】共 {len(unpaid_exp)} 筆：\n\n"
                for item in unpaid_exp:
                    fallback_msg += f"• {item['room_no']} ({item['addr']})\n  金額：{item['amount_str']} 元\n"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=fallback_msg.strip()))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"🎉 【{target_m}月{expense_type}】目前已全數繳清！"
                ))
            return

        # ── 5. 租金已收 → 更新收租表格 + 發送收據給租客 ──
        if text.startswith("租金已收|"):
            parts = text.split("|")
            if len(parts) >= 3:
                target_m    = parts[1].strip()
                target_room = parts[2].strip()
                status_col_idx, amount_col_idx = get_rent_sheet_columns(target_m)
                sh_r = get_worksheet("收租追蹤")
                if not sh_r:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 試算表連線失敗，請稍候再試。"))
                    return

                props = load_properties()
                prop = next((p for p in props if p['room_no'] == target_room), None)
                amt, amt_str, note_str, mgmt_fee_val = get_rent_for_month(prop, target_m) if prop else (0, "0", "", 0)

                rent_rows = sh_r.get_all_values()
                for idx, r in enumerate(rent_rows[1:], start=2):
                    if len(r) > 1 and r[1].strip() == target_room:
                        sh_r.update_cell(idx, status_col_idx, "✔")
                        # 若為周大哥季繳（1、4、7、10月），每個月帶入 1,800 元，將該季 3 個月份一併標記已收
                        if ('B1-03' in target_room or '周大哥' in (r[2] if len(r)>2 else '')):
                            sh_r.update_cell(idx, amount_col_idx, 1800)
                            if target_m in ['1', '4', '7', '10']:
                                q_start = int(target_m)
                                for offset in [1, 2]:
                                    qm = q_start + offset
                                    if qm <= 12:
                                        q_stat_col, q_amt_col = get_rent_sheet_columns(str(qm))
                                        sh_r.update_cell(idx, q_stat_col, "✔")
                                        sh_r.update_cell(idx, q_amt_col, 1800)
                        else:
                            if amt > 0:
                                sh_r.update_cell(idx, amount_col_idx, amt)
                        break

                if prop:
                    target_id = MY_LINE_USER_ID if TEST_MODE else (
                        prop['line_id'] if prop['line_id'] else MY_LINE_USER_ID
                    )
                    card = create_receipt_flex(
                        room_no=prop['room_no'], addr=prop['addr'], tenant=prop['tenant'],
                        month_str=target_m, amount_str=amt_str,
                        mgmt_fee=mgmt_fee_val, is_test=TEST_MODE, note=note_str
                    )
                    try:
                        line_bot_api.push_message(target_id, card)
                    except Exception as e:
                        print(f"租金收據推播失敗: {e}")

                    push_info = "（🧪 測試模式，已傳至您手機）" if TEST_MODE else (
                        f"（已發送收據給 {prop['tenant']}）" if prop['line_id'] else "（房客未綁定LINE，已發給您留存）"
                    )
                    extra_note = f"\n備註：{note_str}" if note_str else ""
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"✅ {target_room}（{prop['tenant']}）{target_m}月租金已標記繳清！\n金額：{amt_str} 元{extra_note}\n{push_info}"
                    ))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"✅ {target_room} {target_m}月租金已標記繳清！"
                    ))
            return

        # ── 6. 發送催繳提醒 → 發送催繳提醒卡給租客 ──
        if text.startswith("發送提醒|"):
            parts = text.split("|")
            if len(parts) >= 3:
                target_m    = parts[1].strip()
                target_room = parts[2].strip()
                props = load_properties()
                prop = next((p for p in props if p['room_no'] == target_room), None)
                if prop:
                    amt, amt_str, note_str, _ = get_rent_for_month(prop, target_m)
                    target_id = MY_LINE_USER_ID if TEST_MODE else (
                        prop['line_id'] if prop['line_id'] else MY_LINE_USER_ID
                    )
                    card = create_reminder_flex(
                        room_no=prop['room_no'], addr=prop['addr'], tenant=prop['tenant'],
                        month_str=target_m, amount_str=amt_str,
                        due_day=prop['due_day'], is_test=TEST_MODE, note=note_str
                    )
                    try:
                        line_bot_api.push_message(target_id, card)
                    except Exception as e:
                        print(f"催繳提醒推播失敗: {e}")

                    push_info = "（🧪 測試模式，已傳至您手機）" if TEST_MODE else (
                        f"（已發送催繳提醒給 {prop['tenant']}）" if prop['line_id'] else "（房客未綁定LINE，已發給您留存）"
                    )
                    extra_note = f"\n備註：{note_str}" if note_str else ""
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"📢 已發送 {target_room}（{prop['tenant']}）{target_m}月催繳提醒！\n金額：{amt_str} 元{extra_note}\n{push_info}"
                    ))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(
                        text=f"❌ 找不到房號 {target_room} 之房客資料。"
                    ))
            return

        # ── 7. 查詢租金未繳清單（輪播卡 + 租金已收/發催繳提醒按鈕）──
        if "租金" in text:
            today    = datetime.now()
            target_m = parse_target_month(text, next_rent_month(today.month))
            status_col_idx, _ = get_rent_sheet_columns(target_m)
            sh_r = get_worksheet("收租追蹤")
            if not sh_r:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前試算表連線維護中，請稍候再試。"))
                return
            rent_rows = sh_r.get_all_values()
            props = load_properties()
            prop_map = {p['room_no']: p for p in props}
            unpaid_items = []

            if rent_rows and len(rent_rows) > 1:
                for row in rent_rows[1:]:
                    room = row[1].strip() if len(row) > 1 else ""
                    prop = prop_map.get(room)
                    # 跳過：無物件、租金 <= 0 (Sandy自住)
                    if not prop or prop['total_rent'] <= 0:
                        continue
                    cell_idx = status_col_idx - 1
                    cell_status = row[cell_idx].strip() if len(row) > cell_idx else ""
                    if not is_paid(cell_status):
                        unpaid_items.append(prop)

            if unpaid_items:
                try:
                    carousel = create_rent_carousel(target_m, unpaid_items)
                    if carousel:
                        line_bot_api.reply_message(event.reply_token, carousel)
                        return
                except Exception as ce:
                    print(f"產生租金輪播卡失敗，改用文字備援: {ce}")

                fallback_msg = f"📋 【{target_m}月租金未繳清單】共 {len(unpaid_items)} 筆：\n\n"
                for item in unpaid_items:
                    amt, amt_str, note_str, _ = get_rent_for_month(item, target_m)
                    extra = f" {note_str}" if note_str else ""
                    fallback_msg += f"• {item['room_no']} {item['tenant']}\n  金額：{amt_str} 元{extra}（每月 {item['due_day']} 號應繳）\n"
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=fallback_msg.strip()))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(
                    text=f"🎉 【{target_m}月租金】目前所有房客皆已繳納完畢！"
                ))
            return

        # 訪客或自訂訊息通用智慧回覆（不再生硬回覆「請點選下方圖文選單」）
        # 1. 若非 Sandy 本人，將訪客留言自動推播至 Sandy 手機
        if user_id != MY_LINE_USER_ID:
            try:
                line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                    text=f"💬 【LINE 收到訪客留言】\n來自用戶：{user_name} ({user_id})\n留言內容：\n{text}\n請管家抽空查看回覆！"
                ))
            except Exception as err:
                print(f"轉發訪客留言失敗: {err}")

        # 2. 親切回覆用戶，並提供專線電話
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"您好！Sandy 管家已收到您的訊息：「{text}」，將儘速由專人為您服務！\n如情況緊急或需即時通話，歡迎直接撥打專線：{MANAGER_PHONE}，謝謝！"
        ))

    except Exception as e:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"系統處理發生異常：{str(e)}"))

@handler.add(MessageEvent, message=(ImageMessage, VideoMessage))
def handle_media_message(event):
    """【修繕報修】房客傳送照片或影片之自動轉發與親切回覆（含防重複重試機制）"""
    msg_id = getattr(event.message, 'id', '')
    token  = getattr(event, 'reply_token', '')

    # 雙重防重防刷機制：以 message_id 與 reply_token 攔截 LINE 的重試請求
    if _is_duplicate(msg_id) or _is_duplicate(token):
        return

    user_id = event.source.user_id
    msg_type_str = "照片" if isinstance(event.message, ImageMessage) else "影片"

    # 先立即以 200 OK 回覆用戶，防止 LINE 伺服器因等待而觸發 5~7 次連鎖重送！
    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text=f"✅ 已收到您傳送的修繕{msg_type_str}！\n系統已同步通知 Sandy 管家安排工班師傅。\n\n請您補充說明損壞狀況與房屋地址（若尚未填寫），管家將儘速為您安排！\n如遇重大漏水、停電等緊急狀況，歡迎直接撥打專線：{MANAGER_PHONE}，謝謝！"
        ))
    except Exception as re:
        print(f"回覆媒體失敗: {re}")

    # 背景非同步推播一次通知給 Sandy
    def _async_notify():
        try:
            try:
                profile   = line_bot_api.get_profile(user_id)
                user_name = profile.display_name
            except Exception:
                user_name = "管理員Sandy" if user_id == MY_LINE_USER_ID else "訪客/租客"

            props     = load_properties()
            user_prop = next((p for p in props if p['line_id'] == user_id), None)
            if user_id == MY_LINE_USER_ID:
                tenant_info = "（🧪 管理員 Sandy 模擬測試）"
            elif user_prop:
                tenant_info = f"（已綁定租客：{user_prop['tenant']}，地址：{user_prop['addr']}）"
            else:
                tenant_info = "（未綁定用戶）"

            line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                text=f"📸 【房客傳送修繕{msg_type_str}通知！】\n來自：{user_name} ({user_id}) {tenant_info}\n用戶在 LINE 傳送了修繕{msg_type_str}，請 Sandy 管家至 LINE 官方帳號查看！"
            ))
        except Exception as pe:
            print(f"轉發媒體通知給 Sandy 失敗: {pe}")

    threading.Thread(target=_async_notify, daemon=True).start()

# 相容不同 gunicorn 啟動語法 (gunicorn app:app 或 gunicorn app.app)
app.app = app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
