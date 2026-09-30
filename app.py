import os
import re
import time
import random
import threading
import calendar
from datetime import datetime, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
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
    with _token_lock:
        if token in _processed_tokens:
            return True
        _processed_tokens.add(token)
        if len(_processed_tokens) > 500:
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

def get_expiring_contracts(days_threshold=60):
    """
    找出即將在指定天數內（預設 60 天 = 約 2 個月）到期或已過期的合約
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
                ButtonComponent(action=MessageAction(label="💬 留下委託諮詢", text="我想諮詢包租代管"), style='secondary', height='sm'),
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
                ButtonComponent(action=MessageAction(label="💬 預約 Sandy 評估", text="我想諮詢包租代管稅費優惠"), style='secondary', height='sm'),
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
                    TextComponent(text="🌟 託管核心服務內容", weight='bold', size='sm', color='#333333'),
                    create_row("💰 租金代收", "每月系統自動對帳、催收與匯報"),
                    create_row("🔧 修繕對接", "配合專業工班隨時排除水電設備故障"),
                    create_row("🏠 定期巡視", "退租點交、設備檢驗拍照存證"),
                    create_row("🤝 協調溝通", "管家即時排解租客日常問題"),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=URIAction(label="📞 預約託管規劃專線", uri=f"tel:{MANAGER_PHONE}"), style='primary', color='#0d6efd', height='sm'),
                    ButtonComponent(action=MessageAction(label="💬 留下託管需求", text="我想諮詢房屋託管"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 3. 我要租屋 ──
def create_service_renting_flex():
    return FlexSendMessage(
        alt_text="【Sandy宅安心】優質租屋尋找",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#fd7e14', contents=[
                    TextComponent(text="🔍 Sandy 宅安心 ｜ 我要租屋", weight='bold', size='md', color='#ffffff'),
                    TextComponent(text="嚴選安心好宅，給您舒適溫馨的家！", size='xs', color='#fff3cd', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="🏡 我們的房源特色", weight='bold', size='sm', color='#333333'),
                    create_row("✨ 拎包入住", "冷氣、冰箱、洗衣機、熱水器一應俱全"),
                    create_row("✨ 生活便利", "鄰近捷運與商圈，交通採買超方便"),
                    create_row("✨ 專人管理", "專業管家服務，居住問題迅速處理"),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="請告訴管家您的「預算、期望區域、起租日期」，我們將為您推薦最適合的房源！", size='xs', color='#666666', margin='md', wrap=True),
                    ButtonComponent(action=MessageAction(label="📝 填寫找屋需求", text="我有租屋需求"), style='primary', color='#fd7e14', height='sm'),
                    ButtonComponent(action=URIAction(label="📞 撥打看屋專線", uri=f"tel:{MANAGER_PHONE}"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 4. 修繕報修 ──
def create_repair_flex(room_no="", tenant=""):
    room_info = f"房號：{room_no}（{tenant} 您好）" if room_no else "請提供您的房號與地址"
    return FlexSendMessage(
        alt_text="【Sandy宅安心】設備修繕報修中心",
        contents=BubbleContainer(
            body=BoxComponent(layout='vertical', padding_all='none', contents=[
                BoxComponent(layout='vertical', padding_all='lg', background_color='#ffc107', contents=[
                    TextComponent(text="🛠️ Sandy 宅安心 ｜ 修繕報修中心", weight='bold', size='md', color='#333333'),
                    TextComponent(text="設備有故障？管家儘速為您安排工班！", size='xs', color='#555555', margin='xs'),
                ]),
                BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text=f"📍 {room_info}", weight='bold', size='sm', color='#0d6efd'),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="請在此對話框直接傳送報修訊息：", weight='bold', size='xs', color='#333333', margin='sm'),
                    TextComponent(text="1. 損壞設備名稱（如冷氣不冷、水管漏水）\n2. 附上損壞狀況之【照片或短影片】\n3. 方便師傅到府維修的時段", size='xs', color='#666666', wrap=True),
                    SeparatorComponent(margin='md'),
                    ButtonComponent(action=MessageAction(label="✏️ 我要送出報修資訊", text=f"我要報修|{room_no}"), style='primary', color='#0d6efd', height='sm'),
                    ButtonComponent(action=URIAction(label="📞 緊急修繕專線", uri=f"tel:{MANAGER_PHONE}"), style='secondary', height='sm'),
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
                    ButtonComponent(action=URIAction(label="📞 直接聯絡 Sandy 管家", uri=f"tel:{MANAGER_PHONE}"), style='secondary', height='sm'),
                    SeparatorComponent(margin='md'),
                    TextComponent(text="若您是本管理系統用戶，請洽管家 Sandy 協助進行房號綁定。", size='xs', color='#aaaaaa', margin='sm', wrap=True),
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
                    ButtonComponent(action=MessageAction(label="⏳ 查即將到期合約（2個月內）", text="查即將到期合約"), style='secondary', height='sm'),
                    ButtonComponent(action=MessageAction(label="🔄 切換測試/正式模式", text="切換模式"), style='secondary', height='sm'),
                ]),
            ])
        )
    )

# ── 即將到期合約卡片 ──
def create_expiring_contracts_flex(expiring_list):
    if not expiring_list:
        return FlexSendMessage(
            alt_text="【合約到期檢查】未來 60 天內無即將到期合約",
            contents=BubbleContainer(
                body=BoxComponent(layout='vertical', padding_all='lg', spacing='sm', contents=[
                    TextComponent(text="📋 合約到期追蹤", weight='bold', size='md', color='#2c3e50'),
                    SeparatorComponent(margin='sm'),
                    TextComponent(text="🎉 太棒了！未來 60 天內沒有即將到期的房客合約。", size='sm', color='#198754', margin='md', wrap=True),
                ])
            )
        )
    
    rows_components = []
    for item in expiring_list[:8]:
        days_str = f"剩 {item['days_left']} 天" if item['days_left'] >= 0 else f"已逾期 {abs(item['days_left'])} 天"
        color = "#dc3545" if item['days_left'] <= 30 else "#fd7e14"
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
                    TextComponent(text="⏳ 房客合約即將到期提醒（60天內）", weight='bold', size='md', color='#ffffff'),
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

# ==================== Webhook 路由 ====================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

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

        # 檢查 60 天內（約 2 個月）即將到期合約
        expiring = get_expiring_contracts(days_threshold=60)
        if expiring:
            summary += "\n\n⏳【合約即將到期提醒（60天內）】\n" + "\n".join(
                f"• {c['room_no']} {c['tenant']}: {c['end_date']} (剩 {c['days_left']} 天)"
                for c in expiring
            )

        # 1. 發送每日文字彙總
        line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(text=summary))

        # 2. 若有 60 天內到期合約，自動加推圖文 Flex 卡片給 Sandy，不必手動點選
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

def _daily_scheduler_loop():
    global _last_auto_check_date
    while True:
        try:
            # 台灣時間 UTC+8
            now_tw = datetime.utcnow() + timedelta(hours=8)
            today_tw = now_tw.date()
            if now_tw.hour == 9 and _last_auto_check_date != today_tw:
                print(f"[{now_tw}] 觸發每日 09:00 自動推播檢查...")
                _last_auto_check_date = today_tw
                perform_auto_check_and_push()
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

    # 紀錄新朋友加入
    if not is_admin:
        try:
            sh_log = get_worksheet_safely(["LINE加入紀錄", "LINE 加入紀錄"])
            if sh_log:
                try:
                    profile   = line_bot_api.get_profile(user_id)
                    user_name = profile.display_name
                except Exception:
                    user_name = "訪客"
                now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")
                sh_log.append_row([now_str, user_id, user_name, text, "否", ""])
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

        # ── 舊式地址綁定（保留相容）──
        if "綁定地址" in text or "我要綁定" in text:
            input_str = text.replace("綁定地址", "").replace("我要綁定", "").replace("：", "").replace(":", "").strip()
            props = load_properties()
            matched_prop = next(
                (p for p in props if input_str and (input_str in p['addr'] or p['addr'] in input_str or input_str in p['room_no'])),
                None
            )
            if matched_prop:
                try:
                    sheet_tenant.update_cell(matched_prop['row_idx'], 22, user_id)
                    sheet_tenant.update_cell(matched_prop['row_idx'], 23, "Y")
                    reply_msg = f"✅ 綁定成功！\n物件：{matched_prop['room_no']}（{matched_prop['tenant']} 您好）\n未來收租確認與繳費通知將會發送給您。"
                except Exception as e:
                    reply_msg = f"⚠️ 綁定過程發生錯誤：{str(e)}"
            else:
                reply_msg = "❌ 找不到符合的物件或地址，請確認輸入是否正確，或洽詢 Sandy 管家協助綁定。"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            return

        # ==================== 六宮格按鍵（所有人可互動）====================

        # ── 按鈕 1：包租代管 ──
        if any(kw in text for kw in ["包租代管", "包租", "1", "我要包租", "包租貸款", "社會住宅"]) and "諮詢" not in text and "優惠" not in text:
            line_bot_api.reply_message(event.reply_token, create_service_baozu_flex())
            return

        # ── 客戶諮詢需求（包含包租代管、稅費優惠、房屋託管、租屋、叫修），自動即時轉知 Sandy ──
        if any(kw in text for kw in ["我想諮詢包租代管", "我想諮詢包租代管稅費優惠", "我想諮詢房屋託管", "我有租屋需求", "我想諮詢管家", "稅費優惠", "諮詢"]) or text.startswith("我要報修"):
            try:
                line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                    text=f"📢 【客戶即時需求通知】\n來自 LINE 用戶：{user_name} ({user_id})\n需求內容：{text}\n請管家抽空聯繫處理！"
                ))
            except Exception as err:
                print(f"轉發通知房東失敗: {err}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"✅ 已為您通知 Sandy 管家！管家將儘速與您聯繫。\n如情況緊急，歡迎直接撥打專線：{MANAGER_PHONE}，謝謝！"
            ))
            return

        # ── 按鈕 2：房屋託管 ──
        if any(kw in text for kw in ["房屋託管", "託管", "2", "我要託管"]) and "諮詢" not in text:
            line_bot_api.reply_message(event.reply_token, create_service_tuoguan_flex())
            return

        # ── 按鈕 3：我要租屋 ──
        if any(kw in text for kw in ["我要租屋", "找房子", "租屋", "3"]) and "諮詢" not in text and "需求" not in text:
            line_bot_api.reply_message(event.reply_token, create_service_renting_flex())
            return

        # ── 按鈕 4：修繕報修 ──
        if any(kw in text for kw in ["修繕報修", "報修", "4", "設備維修", "叫修"]) and not text.startswith("我要報修"):
            props     = load_properties()
            user_prop = next((p for p in props if p['line_id'] == user_id), None)
            line_bot_api.reply_message(event.reply_token, create_repair_flex(
                room_no=user_prop['room_no'] if user_prop else "",
                tenant =user_prop['tenant']  if user_prop else ""
            ))
            return

        # ── 按鈕 6：聯絡管家 ──
        if any(kw in text for kw in ["聯絡管家", "聯繫管家", "管家名片", "專屬管家", "聯絡sandy", "聯絡Sandy", "管家", "客服", "聯絡客服", "找管家", "6"]) and "諮詢" not in text and "委託" not in text:
            line_bot_api.reply_message(event.reply_token, create_contact_manager_flex())
            return

        # 客戶其他諮詢或叫修，自動轉知 Sandy
        if any(kw in text for kw in ["我想諮詢房屋託管", "我有租屋需求", "我想諮詢管家", "我要報修"]) or text.startswith("我要報修"):
            try:
                line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(
                    text=f"📢 【客戶即時需求通知】\n來自 LINE 用戶：{user_name} ({user_id})\n需求內容：{text}\n請管家抽空聯繫處理！"
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

        # ── 查詢即將到期合約（2個月內 / 60天） ──
        if any(kw in text for kw in ["查合約", "合約到期", "即將到期", "到期合約", "合約"]):
            expiring = get_expiring_contracts(days_threshold=60)
            line_bot_api.reply_message(event.reply_token, create_expiring_contracts_flex(expiring))
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
                status_col_idx, _ = get_expense_sheet_columns(target_m, "管理費")
                sh_e = get_worksheet("支出追蹤")
                if not sh_e:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 試算表連線失敗，請稍候再試。"))
                    return
                exp_rows  = sh_e.get_all_values()
                amount_val = "0"
                for idx, r in enumerate(exp_rows[1:], start=2):
                    if len(r) > 0 and target_addr in r[0].strip():
                        amount_val = r[3].strip() if len(r) > 3 else "0"
                        sh_e.update_cell(idx, status_col_idx, "✅")
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
                status_col_idx, _ = get_expense_sheet_columns(target_m, "貸款")
                sh_e = get_worksheet("支出追蹤")
                if not sh_e:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 試算表連線失敗，請稍候再試。"))
                    return
                exp_rows  = sh_e.get_all_values()
                amount_val = "0"
                for idx, r in enumerate(exp_rows[1:], start=2):
                    if len(r) > 0 and target_addr in r[0].strip():
                        amount_val = r[4].strip() if len(r) > 4 else "0"
                        sh_e.update_cell(idx, status_col_idx, "✅")
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
            target_m     = str(today.month)
            for m in range(12, 0, -1):
                if f"{m}月" in text:
                    target_m = str(m)
                    break
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
                status_col_idx, _ = get_rent_sheet_columns(target_m)
                sh_r = get_worksheet("收租追蹤")
                if not sh_r:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 試算表連線失敗，請稍候再試。"))
                    return
                rent_rows = sh_r.get_all_values()
                for idx, r in enumerate(rent_rows[1:], start=2):
                    if len(r) > 1 and r[1].strip() == target_room:
                        sh_r.update_cell(idx, status_col_idx, "✅")
                        # 若為周大哥季繳（1、4、7、10月），將該季 3 個月份一併標記已收
                        if ('B1-03' in target_room or '周大哥' in (r[2] if len(r)>2 else '')) and target_m in ['1', '4', '7', '10']:
                            q_start = int(target_m)
                            for offset in [1, 2]:
                                qm = q_start + offset
                                if qm <= 12:
                                    q_col, _ = get_rent_sheet_columns(str(qm))
                                    sh_r.update_cell(idx, q_col, "✅")
                        break

                props = load_properties()
                prop = next((p for p in props if p['room_no'] == target_room), None)
                if prop:
                    amt, amt_str, note_str, mgmt_fee_val = get_rent_for_month(prop, target_m)
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
            target_m = next_rent_month(today.month)
            for m in range(12, 0, -1):
                if f"{m}月" in text:
                    target_m = str(m)
                    break
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


# 相容不同 gunicorn 啟動語法 (gunicorn app:app 或 gunicorn app.app)
app.app = app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
