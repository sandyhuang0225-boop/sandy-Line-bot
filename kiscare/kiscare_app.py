"""
擊癌利病患照護小管家 (Kisqali Care Bot) - 伺服器主程式
專屬責任醫院：基隆長庚醫院、三軍總醫院、汐止國泰醫院
採用與 line_bot 100% 一模一樣且最穩定的 linebot v2 架構
"""

import os
import json
import logging
import traceback
import urllib.parse
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, abort, jsonify, send_file, send_from_directory
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, PostbackEvent,
    FlexSendMessage, FollowEvent
)

# 台灣時區 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

def get_taiwan_today():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")

def get_taiwan_now_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
kiscare_bp = Blueprint('kiscare', __name__, static_folder=BASE_DIR)
logger = logging.getLogger('kiscare')
logging.basicConfig(level=logging.INFO)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get(
    "KISCARE_LINE_CHANNEL_ACCESS_TOKEN",
    "Upmod4oov1d4uGawXN7T8ueE+vH09eU80aQQzdGbF93yy8qOn9d3yEnfvSBvq0iU2PLVT+M8YQnzKA46c5yR3ocKBlcxtOn6veim2KJnzDzWcNRYF7qB50URl9+pD8B0JdkaFAPWwMBcCgDG2/BQXQdB04t89/1O/w1cDnyilFU="
)
LINE_CHANNEL_SECRET = os.environ.get(
    "KISCARE_LINE_CHANNEL_SECRET",
    "a3926ce21639f4d64e293eb822d9c807"
)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

HOSPITALS = {
    "三總": {
        "name": "三軍總醫院",
        "pharmacy": "躍獅寶湖藥局",
        "address": "114台北市內湖區寶湖里民權東路6段123巷28號",
        "phone": "02-87928335"
    },
    "基隆長庚": {
        "name": "基隆長庚醫院",
        "pharmacy": "宏仁藥局",
        "address": "基隆市安樂區安樂路2段130號",
        "phone": "02-24321739"
    },
    "汐止國泰": {
        "name": "汐止國泰醫院",
        "pharmacy": "躍獅寶湖藥局（由內湖支援）",
        "address": "114台北市內湖區寶湖里民權東路6段123巷28號",
        "phone": "02-87928335"
    }
}

DATA_FILE = os.path.join(BASE_DIR, "user_data.json")

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("profiles", {}), data.get("counter", 0)
        except Exception as e:
            logger.error(f"Error loading {DATA_FILE}: {e}")
    return {}, 0

def save_data(profiles, counter):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"profiles": profiles, "counter": counter}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving {DATA_FILE}: {e}")

user_profiles, patient_counter = load_data()

def normalize_patient_code(raw_text):
    """
    智慧模糊辨識病友代號：
    支援：0001, 1, 01, 001, ksq0001, KSQ0001, ksq-0001, KSQ-0001, ksq 0001, KSQ 1, #0001 等
    一律精準正規化為標準 KSQ-XXXX
    """
    if not raw_text:
        return None
    raw = raw_text.strip().upper()
    cleaned = raw.replace("-", "").replace(" ", "").replace("#", "").replace("NO.", "").replace("NO", "")
    if cleaned.startswith("KSQ"):
        digits_part = cleaned[3:]
    else:
        digits_part = cleaned
    if digits_part.isdigit():
        num = int(digits_part)
        if num > 0:
            return f"KSQ-{num:04d}"
    return None

def get_effective_user(user_id):
    """
    若使用者為家屬照護者身分，取得所綁定之病友真實資料與病友 ID。
    回傳 (target_user_dict, effective_user_id, is_caregiver)
    """
    if not user_id:
        return {}, None, False
    user = get_or_create_user(user_id)
    if user.get("role") == "caregiver" and user.get("bound_patient_id"):
        patient_id = user["bound_patient_id"]
        if patient_id in user_profiles:
            return user_profiles[patient_id], patient_id, True
    return user, user_id, False

def get_or_create_user(user_id):
    """
    確保每個 LINE 使用者永遠綁定唯一的專屬病友代號 (KSQ-XXXX)。
    不論跨院就醫或資料更新，ID 永遠鎖定不跳號，存摺永遠只有一本。
    """
    global patient_counter
    if user_id not in user_profiles:
        # 若是第一位真實進入小管家的管理者/測試者，自動綁定為預設已建檔之 KSQ-0001
        if "U7278e50c97fbaf8753c1cbf976903666" in user_profiles:
            user_profiles[user_id] = user_profiles.pop("U7278e50c97fbaf8753c1cbf976903666")
            save_data(user_profiles, patient_counter)
            return user_profiles[user_id]

        patient_counter += 1
        code_str = f"KSQ-{patient_counter:04d}"
        user_profiles[user_id] = {
            "hosp": "三軍總醫院",
            "cancer": "早期乳癌",
            "dose": 2,
            "stock": 0,
            "code": code_str,
            "pharmacy": "躍獅寶湖藥局",
            "is_registered": False,
            "role": "patient",
            "state": None
        }
        save_data(user_profiles, patient_counter)
    return user_profiles[user_id]

# ----------------- 開戶與身分選擇入口卡片 -----------------
def get_onboarding_entry_flex():
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0F172A",
            "contents": [
                {"type": "text", "text": "✨ 擊癌利專屬存摺開戶與綁定", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": "請選擇您的身分開始使用", "color": "#94A3B8", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "請依您的身分點選下方按鈕：", "size": "xs", "color": "#64748B"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FEF2F2", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": "🌸【我是病友本人】（初次開戶）", "size": "xs", "weight": "bold", "color": "#991B1B"},
                        {"type": "text", "text": "適合首次使用，建立您的專屬病友代號、鎖定就診醫院與個人存摺。", "size": "xxs", "color": "#64748B", "wrap": True}
                    ]
                },
                {
                    "type": "button", "style": "primary", "color": "#991B1B", "height": "sm",
                    "action": {"type": "postback", "label": "🌸 我是病友本人（新開戶）", "data": "action=start_patient_onboard", "displayText": "我是病友本人"}
                },
                {"type": "separator"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#EEF2FF", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": "👨‍👩‍👧【我是家屬／照護者】（代號綁定）", "size": "xs", "weight": "bold", "color": "#4338CA"},
                        {"type": "text", "text": "適合協助家人追蹤存藥，只需輸入家人的病友代號即可同步存摺！", "size": "xxs", "color": "#64748B", "wrap": True}
                    ]
                },
                {
                    "type": "button", "style": "primary", "color": "#4F46E5", "height": "sm",
                    "action": {"type": "postback", "label": "👨‍👩‍👧 我是家屬（綁定家人存摺）", "data": "action=caregiver_bind", "displayText": "我是家屬，協助綁定"}
                }
            ]
        }
    }

# ----------------- 家屬輸入代號引導卡片 -----------------
def get_caregiver_input_prompt_flex():
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#4338CA",
            "contents": [
                {"type": "text", "text": "👨‍👩‍👧 家屬／照護者存摺綁定", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": "【步驟 1/2】請輸入病友專屬代號", "color": "#E0E7FF", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "請直接在下方聊天室輸入病友的「專屬代號」：", "size": "xs", "weight": "bold", "color": "#1E293B"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": "💡【貼心防呆模糊支援】：", "size": "xs", "weight": "bold", "color": "#047857"},
                        {"type": "text", "text": "• 打純數字：「0001」或「1」\n• 英文連打：「KSQ0001」或「ksq0001」\n• 標準格式：「KSQ-0001」或「ksq-0001」", "size": "xxs", "color": "#475569", "wrap": True},
                        {"type": "text", "text": "👉 以上任何一種格式，系統都會自動識別為 KSQ-0001！", "size": "xxs", "color": "#D97706", "weight": "bold", "wrap": True}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "postback", "label": "🌸 改為病友本人開戶", "data": "action=start_patient_onboard", "displayText": "改為病友本人開戶"}
                }
            ]
        }
    }

# ----------------- 家屬驗證就診醫院卡片（隱私安全） -----------------
def get_caregiver_verify_hosp_flex(target_code):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#1E293B",
            "contents": [
                {"type": "text", "text": "🔐【步驟 2/2】責任醫院安全驗證", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"已辨識病友代號：{target_code}", "color": "#CBD5E1", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": f"系統已成功辨識代號【{target_code}】！", "size": "xs", "weight": "bold", "color": "#047857"},
                {"type": "text", "text": "為保障病友用藥安全與個人隱私，請點選病友目前就診的責任醫院完成驗證：", "size": "xxs", "color": "#64748B", "wrap": True},
                {"type": "button", "style": "primary", "color": "#991B1B", "height": "sm", "action": {"type": "postback", "label": "🏥 三軍總醫院（內湖/汀州/松山）", "data": f"verify_caregiver_hosp={target_code}|三總", "displayText": "三軍總醫院"}},
                {"type": "button", "style": "primary", "color": "#B91C1C", "height": "sm", "action": {"type": "postback", "label": "🏥 基隆長庚醫院（基隆/情人湖）", "data": f"verify_caregiver_hosp={target_code}|基隆長庚", "displayText": "基隆長庚醫院"}},
                {"type": "button", "style": "primary", "color": "#DC2626", "height": "sm", "action": {"type": "postback", "label": "🏥 汐止國泰醫院", "data": f"verify_caregiver_hosp={target_code}|汐止國泰", "displayText": "汐止國泰醫院"}}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "postback", "label": "🔄 代號按錯，重新輸入", "data": "action=caregiver_bind", "displayText": "重新輸入代號"}
                }
            ]
        }
    }

# ----------------- 查無代號提示卡片 -----------------
def get_caregiver_not_found_flex(target_code):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#B45309",
            "contents": [
                {"type": "text", "text": "⚠️ 查無此病友開戶紀錄", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"輸入代號：{target_code}", "color": "#FEF3C7", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"系統目前尚未查到代號【{target_code}】的開戶紀錄。", "size": "xs", "color": "#0F172A", "weight": "bold"},
                {"type": "text", "text": "可能原因：\n1. 家人尚未在醫院或本管家完成首次建檔開戶。\n2. 代號數字有誤（請向家人確認）。", "size": "xxs", "color": "#64748B", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#4F46E5", "height": "sm",
                    "action": {"type": "postback", "label": "🔄 重新輸入代號", "data": "action=caregiver_bind", "displayText": "重新輸入代號"}
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "postback", "label": "🌸 我是病友本人（建立新開戶）", "data": "action=start_patient_onboard", "displayText": "我是病友本人"}
                }
            ]
        }
    }

# ----------------- 未建檔病友防呆導引卡片 -----------------
def get_unregistered_prompt_flex(feature_name="功能"):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "⚠️ 尚未完成病友開戶建檔", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"使用【{feature_name}】前請先開戶或綁定", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "親愛的病友與家屬您好：", "size": "sm", "weight": "bold", "color": "#0F172A"},
                {"type": "text", "text": f"您目前尚未建立或綁定病友資料。請選擇您的身分以利管理「{feature_name}」與試算用藥安全天數：", "size": "xs", "color": "#64748B", "wrap": True},
                {"type": "separator", "margin": "md"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FEF2F2", "cornerRadius": "md", "paddingAll": "sm",
                    "contents": [
                        {"type": "text", "text": "🌸 病友本人：首次開戶建檔（只需 30 秒）", "size": "xs", "weight": "bold", "color": "#991B1B"},
                        {"type": "text", "text": "👨‍👩‍👧 家屬／照護者：輸入病友代號直接同步", "size": "xs", "weight": "bold", "color": "#4338CA", "margin": "xs"}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#991B1B", "height": "sm",
                    "action": {"type": "postback", "label": "🌸 我是病友本人開戶", "data": "action=start_patient_onboard"}
                },
                {
                    "type": "button", "style": "primary", "color": "#4338CA", "height": "sm",
                    "action": {"type": "postback", "label": "👨‍👩‍👧 我是家屬協助綁定", "data": "action=caregiver_bind"}
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "postback", "label": "📬 查看領藥流程說明", "data": "action=sop"}
                }
            ]
        }
    }

# ----------------- 已建檔病友／家屬防呆卡片 -----------------
def get_already_registered_flex(user, user_id=None):
    eff_user, eff_user_id, is_caregiver = get_effective_user(user_id) if user_id else (user, None, user.get("role") == "caregiver")
    dose = eff_user.get("dose", 2)
    stock = eff_user.get("stock", 0)
    code = eff_user.get("code", "KSQ-0001")
    days = stock // dose if dose > 0 else 999

    if is_caregiver:
        return {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "backgroundColor": "#4338CA",
                "contents": [
                    {"type": "text", "text": "👨‍👩‍👧 您已綁定為照護家屬！", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                    {"type": "text", "text": f"病友代號：{code} ｜ 共同照護模式", "color": "#E0E7FF", "size": "xs", "margin": "xs"}
                ]
            },
            "body": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {"type": "text", "text": "您已成功與家人存摺連線同步，目前病友存摺狀態：", "size": "xs", "color": "#64748B"},
                    {
                        "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                        "contents": [
                            {"type": "text", "text": f"• 就診醫院：{eff_user.get('hosp', '三軍總醫院')}（{eff_user.get('pharmacy', '躍獅寶湖藥局')}）", "size": "xs", "color": "#0F172A", "weight": "bold"},
                            {"type": "text", "text": f"• 治療類型：{eff_user.get('cancer', '早期乳癌')}", "size": "xs", "color": "#0F172A"},
                            {"type": "text", "text": f"• 目前處方：每日 {dose} 顆 ({dose*200}mg)", "size": "xs", "color": "#0F172A"},
                            {"type": "text", "text": f"• 手邊存藥：{stock} 顆（預估服 {days} 天）", "size": "xs", "color": "#991B1B" if days <= 14 else "#047857", "weight": "bold"}
                        ]
                    },
                    {"type": "separator", "margin": "xs"},
                    {"type": "text", "text": "💡 您可在下方協助登記購藥或查看最新存摺與門診速報。", "size": "xxs", "color": "#64748B", "wrap": True}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical", "spacing": "sm",
                "contents": [
                    {
                        "type": "button", "style": "primary", "color": "#047857", "height": "sm",
                        "action": {"type": "postback", "label": "🧾 代為購藥登記", "data": "action=receipt"}
                    },
                    {
                        "type": "button", "style": "primary", "color": "#1E293B", "height": "sm",
                        "action": {"type": "postback", "label": "📕 查看病友存摺", "data": "action=passbook"}
                    },
                    {
                        "type": "button", "style": "secondary", "height": "sm",
                        "action": {"type": "postback", "label": "🏥 門診速報", "data": "action=doctor"}
                    },
                    {
                        "type": "button", "style": "secondary", "height": "sm",
                        "action": {"type": "postback", "label": "🔄 解除家屬綁定", "data": "action=unbind_caregiver"}
                    }
                ]
            }
        }

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#B45309",
            "contents": [
                {"type": "text", "text": "⚠️ 您已完成過開戶建檔！", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"專屬代號：{code} ｜ 唯一綁定防呆", "color": "#FEF3C7", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "系統已有您的建檔紀錄，目前存摺資料如下：", "size": "xs", "color": "#64748B"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": f"• 就診醫院：{user.get('hosp', '三軍總醫院')}（{user.get('pharmacy', '躍獅寶湖藥局')}）", "size": "xs", "color": "#0F172A", "weight": "bold"},
                        {"type": "text", "text": f"• 治療類型：{user.get('cancer', '早期乳癌')}", "size": "xs", "color": "#0F172A"},
                        {"type": "text", "text": f"• 目前劑量：每日 {dose} 顆 ({dose*200}mg)", "size": "xs", "color": "#0F172A"},
                        {"type": "text", "text": f"• 手邊存藥：{stock} 顆（預估服 {days} 天）", "size": "xs", "color": "#991B1B", "weight": "bold"}
                    ]
                },
                {"type": "separator", "margin": "xs"},
                {"type": "text", "text": "💡【跨院就醫與存藥防呆】：", "size": "xs", "weight": "bold", "color": "#1E293B"},
                {"type": "text", "text": "您的病友代號為唯一鎖定綁定。若至其他指定責任醫院就醫或轉院，藥物存量會自動鎖定延續，無需重複開戶！", "size": "xxs", "color": "#64748B", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#047857", "height": "sm",
                    "action": {"type": "postback", "label": "🧾 購藥登記（增加新買顆數）", "data": "action=receipt"}
                },
                {
                    "type": "button", "style": "primary", "color": "#0D9488", "height": "sm",
                    "action": {"type": "postback", "label": "🏥 變更就診醫院（跨院移轉）", "data": "action=switch_hosp"}
                },
                {
                    "type": "button", "style": "primary", "color": "#1E293B", "height": "sm",
                    "action": {"type": "postback", "label": "📕 查看我的病友存摺", "data": "action=passbook"}
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {"type": "postback", "label": "⚠️ 重新完整開戶（清除重設）", "data": "action=force_onboard"}
                }
            ]
        }
    }

# ----------------- 變更就診醫院選擇卡片 -----------------
def get_switch_hospital_flex(user):
    stock = user.get("stock", 0)
    hosp = user.get("hosp", "三軍總醫院")
    code = user.get("code", "KSQ-0001")
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0D9488",
            "contents": [
                {"type": "text", "text": "🏥 變更責任醫院（跨院移轉）", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"病友代號：{code} ｜ 存藥鎖定延續", "color": "#CCFBF1", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"您目前在【{hosp}】就診，手邊存藥【{stock} 顆】將完整鎖定保留！請點選您欲變更的新就診醫院：", "size": "xs", "color": "#334155", "wrap": True},
                {"type": "separator", "margin": "sm"},
                {"type": "button", "style": "primary", "color": "#991B1B", "height": "sm", "action": {"type": "postback", "label": "🏥 三軍總醫院（寶湖藥局）", "data": "confirm_switch_hosp=三總", "displayText": "三軍總醫院"}},
                {"type": "button", "style": "primary", "color": "#B91C1C", "height": "sm", "action": {"type": "postback", "label": "🏥 基隆長庚醫院（宏仁藥局）", "data": "confirm_switch_hosp=基隆長庚", "displayText": "基隆長庚醫院"}},
                {"type": "button", "style": "primary", "color": "#DC2626", "height": "sm", "action": {"type": "postback", "label": "🏥 汐止國泰醫院（寶湖支援）", "data": "confirm_switch_hosp=汐止國泰", "displayText": "汐止國泰醫院"}}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "contents": [
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "❌ 取消變更，留在原醫院", "data": "cancel_purchase", "displayText": "取消變更"}}
            ]
        }
    }

# ----------------- 轉院與藥局變更確認卡片 -----------------
def get_transfer_confirm_flex(user, new_hosp_key, new_hosp_info):
    stock = user.get("stock", 0)
    code = user.get("code", "KSQ-0001")
    old_hosp = user.get("hosp", "三軍總醫院")
    old_ph = user.get("pharmacy", "躍獅寶湖藥局")
    new_hosp = new_hosp_info["name"]
    new_ph = new_hosp_info["pharmacy"]
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0D9488",
            "contents": [
                {"type": "text", "text": "🔄 跨院轉移與藥局切換確認", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"代號：{code} ｜ 藥物存量鎖定", "color": "#CCFBF1", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F0FDFA", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": f"• 原主治醫院：{old_hosp}（{old_ph}）", "size": "xxs", "color": "#64748B"},
                        {"type": "text", "text": f"• 新主治醫院：{new_hosp}（{new_ph}）", "size": "xs", "weight": "bold", "color": "#0F766E"},
                        {"type": "text", "text": f"• 目前存藥庫存：{stock} 顆（完整保留延續）", "size": "xs", "weight": "bold", "color": "#B91C1C"}
                    ]
                },
                {"type": "text", "text": "跨院移轉後，您的代號與存藥完全保留，領藥藥局將自動切換為新醫院指定藥局：", "size": "xxs", "color": "#64748B", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#0D9488", "height": "sm", "action": {"type": "postback", "label": f"✅ 保留 {stock} 顆存藥並轉院", "data": f"apply_switch={new_hosp_key}&keep=1", "displayText": "確認轉院並保留存藥"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "📝 轉院並重新盤點顆數", "data": f"apply_switch={new_hosp_key}&keep=0", "displayText": "轉院並重盤顆數"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "❌ 取消變更", "data": "cancel_purchase", "displayText": "取消變更"}}
            ]
        }
    }

# ----------------- 重新開戶二次防呆警示卡片 -----------------
def get_force_onboard_confirm_flex(user):
    stock = user.get("stock", 0)
    code = user.get("code", "KSQ-0001")
    hosp = user.get("hosp", "三軍總醫院")
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "⚠️ 確定要重新開戶建檔嗎？", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"專屬代號：{code} ｜ 資料重設提醒", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "系統偵測到您目前已有完整建檔紀錄：", "size": "xs", "color": "#64748B"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FEF2F2", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": f"• 就診醫院：{hosp}", "size": "xs", "color": "#0F172A"},
                        {"type": "text", "text": f"• 手邊存藥：{stock} 顆", "size": "xs", "weight": "bold", "color": "#991B1B"}
                    ]
                },
                {"type": "text", "text": "💡 若您只是跨院就診或換了醫院，請點選【變更就診醫院】，存藥顆數會自動鎖定保留！", "size": "xxs", "color": "#334155", "wrap": True}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "button", "style": "primary", "color": "#0D9488", "height": "sm", "action": {"type": "postback", "label": "🏥 僅變更就診醫院（保留存藥）", "data": "action=switch_hosp", "displayText": "變更就診醫院"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "🔄 堅持重新完整建檔", "data": "do_force_onboard", "displayText": "確定重新建檔"}},
                {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "❌ 取消，返回我的存摺", "data": "action=passbook", "displayText": "返回存摺"}}
            ]
        }
    }

# ----------------- 輸入數字意圖確認卡片（校正庫存 vs 購藥加買） -----------------
def get_stock_confirm_flex(user, num):
    curr_stock = user.get("stock", 0)
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#0284C7",
            "contents": [
                {"type": "text", "text": "📋 存藥確認與校正", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"病友代號：{user.get('code', 'KSQ-0001')} ｜ 意圖確認", "color": "#E0F2FE", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": "偵測到您輸入了顆數數字，請問您的操作是：", "size": "xs", "color": "#64748B"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F0F9FF", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": f"• 目前存摺登記庫存：{curr_stock} 顆", "size": "xs", "weight": "bold", "color": "#0369A1"},
                        {"type": "text", "text": f"• 剛才輸入的數字：{num} 顆", "size": "xs", "weight": "bold", "color": "#B91C1C"}
                    ]
                },
                {"type": "separator", "margin": "xs"},
                {"type": "text", "text": "請依您的實際狀況點選下方處理方式：", "size": "xs", "color": "#334155"}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#D97706", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": f"🔄 校正庫存為 {num} 顆",
                        "data": f"calibrate_stock={num}",
                        "displayText": f"校正庫存為 {num} 顆"
                    }
                },
                {
                    "type": "button", "style": "primary", "color": "#047857", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": f"➕ 新買加累 {num} 顆",
                        "data": f"confirm_add_purchase={num}",
                        "displayText": f"新買加累 {num} 顆"
                    }
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": f"❌ 維持原 {curr_stock} 顆不變",
                        "data": "cancel_purchase",
                        "displayText": "維持不變"
                    }
                }
            ]
        }
    }

# ----------------- 今日重複購藥防呆卡片 -----------------
def get_duplicate_purchase_flex(last_pills, num, today_str, last_time):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#B45309",
            "contents": [
                {"type": "text", "text": "⚠️ 發現今日已有購藥登記！", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"防呆檢查 ｜ 日期：{today_str}", "color": "#FEF3C7", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"您今天稍早（{last_time}）已經登記過一次：", "size": "xs", "color": "#64748B"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FEF3C7", "cornerRadius": "md", "paddingAll": "sm",
                    "contents": [
                        {"type": "text", "text": f"• 今日已登錄：{last_pills} 顆", "size": "xs", "weight": "bold", "color": "#92400E"},
                        {"type": "text", "text": f"• 剛才又輸入：{num} 顆", "size": "xs", "weight": "bold", "color": "#B91C1C"}
                    ]
                },
                {"type": "separator", "margin": "sm"},
                {"type": "text", "text": "為避免您重複累計或打錯，請選擇以下處理方式：", "size": "xs", "color": "#334155"}
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": f"❌ 維持原 {last_pills} 顆不變",
                        "data": "cancel_purchase",
                        "displayText": "維持不變"
                    }
                },
                {
                    "type": "button", "style": "primary", "color": "#C2410C", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": f"🔄 更正為 {num} 顆",
                        "data": f"overwrite_purchase={num}",
                        "displayText": f"更正為 {num} 顆"
                    }
                },
                {
                    "type": "button", "style": "primary", "color": "#047857", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": f"➕ 加買累計 {num} 顆",
                        "data": f"confirm_add_purchase={num}",
                        "displayText": f"加買累計 {num} 顆"
                    }
                }
            ]
        }
    }

# ----------------- 建檔步驟 1~4 -----------------
def get_onboarding_step1_flex():
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "✨ 新病友開戶建檔", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": "【步驟 1/4】請點選您的主治就診醫院", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "請直接點選下方任一家責任醫院：", "size": "xs", "color": "#64748B"},
                {"type": "button", "style": "primary", "color": "#991B1B", "height": "sm", "action": {"type": "postback", "label": "🏥 三軍總醫院（內湖/汀州/松山）", "data": "set_hosp=三總", "displayText": "三軍總醫院"}},
                {"type": "button", "style": "primary", "color": "#B91C1C", "height": "sm", "action": {"type": "postback", "label": "🏥 基隆長庚醫院（基隆/情人湖）", "data": "set_hosp=基隆長庚", "displayText": "基隆長庚醫院"}},
                {"type": "button", "style": "primary", "color": "#DC2626", "height": "sm", "action": {"type": "postback", "label": "🏥 汐止國泰醫院", "data": "set_hosp=汐止國泰", "displayText": "汐止國泰醫院"}}
            ]
        }
    }

def get_onboarding_step2_flex(hospital_name):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "✨ 新病友開戶建檔", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"已選醫院：{hospital_name} ｜ 【步驟 2/4】治療類型", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "請依主治醫師診斷點選您的治療階段：", "size": "xs", "color": "#64748B"},
                {"type": "button", "style": "primary", "color": "#059669", "height": "sm", "action": {"type": "postback", "label": "🌸 早期乳癌（EBC・3年方案）", "data": "set_cancer=早期乳癌", "displayText": "早期乳癌"}},
                {"type": "button", "style": "primary", "color": "#D97706", "height": "sm", "action": {"type": "postback", "label": "🎗️ 轉移晚期乳癌（MBC・5年方案）", "data": "set_cancer=晚期乳癌", "displayText": "晚期乳癌"}}
            ]
        }
    }

def get_onboarding_step3_flex(cancer_type):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "✨ 新病友開戶建檔", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"已選類型：{cancer_type} ｜ 【步驟 3/4】每日劑量", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {"type": "text", "text": "請依醫師處方點選每日服用顆數：", "size": "xs", "color": "#64748B"},
                {"type": "button", "style": "primary", "color": "#1E293B", "height": "sm", "action": {"type": "postback", "label": "💊 每日 3 顆（600 mg 起始劑量）", "data": "set_dose=3", "displayText": "每日 3 顆"}},
                {"type": "button", "style": "primary", "color": "#334155", "height": "sm", "action": {"type": "postback", "label": "💊 每日 2 顆（400 mg 標準/一級減量）", "data": "set_dose=2", "displayText": "每日 2 顆"}},
                {"type": "button", "style": "primary", "color": "#475569", "height": "sm", "action": {"type": "postback", "label": "💊 每日 1 顆（200 mg 二級減量）", "data": "set_dose=1", "displayText": "每日 1 顆"}}
            ]
        }
    }

def get_onboarding_step4_flex(dose_str):
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "✨ 新病友開戶建檔", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": f"已選劑量：{dose_str} ｜ 【步驟 4/4】現有存藥", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {
                    "type": "text",
                    "text": "請直接在下方聊天室輸入您手邊「目前剩餘的確切顆數」：",
                    "size": "sm",
                    "weight": "bold",
                    "color": "#0F172A",
                    "wrap": True
                },
                {
                    "type": "box",
                    "layout": "vertical",
                    "backgroundColor": "#F8FAFC",
                    "cornerRadius": "md",
                    "paddingAll": "md",
                    "borderWidth": "light",
                    "borderColor": "#CBD5E1",
                    "contents": [
                        {"type": "text", "text": "👉 請直接打數字並送出，例如：", "size": "xs", "color": "#64748B"},
                        {"type": "text", "text": "「21」 或 「53」 或 「70」", "size": "lg", "weight": "bold", "color": "#991B1B", "margin": "sm"}
                    ]
                },
                {
                    "type": "text",
                    "text": "💡 每個病人購買的顆數都不一樣，系統會依您輸入的數字為您精準試算剩餘天數並啟動安全警報！",
                    "size": "xs",
                    "color": "#64748B",
                    "wrap": True
                }
            ]
        }
    }

# ----------------- 病患同意書範例與教學卡片 -----------------
def get_consent_form_flex():
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "backgroundColor": "#991B1B",
            "contents": [
                {"type": "text", "text": "📋 擊癌利病患同意書範例與教學", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                {"type": "text", "text": "贈藥申請審核必備文件 ｜ 填寫注意事項", "color": "#FEE2E2", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#FEF2F2",
                    "cornerRadius": "md", "paddingAll": "sm", "borderWidth": "light", "borderColor": "#FECACA",
                    "contents": [
                        {"type": "text", "text": "📝 同意書填寫三大量點（缺一不可）：", "size": "xs", "weight": "bold", "color": "#991B1B"},
                        {"type": "text", "text": "1. 🟨【醫師黃框區】：醫師親筆簽名 ＋ 門診日期（務必手寫年月日）", "size": "xxs", "color": "#334155", "margin": "xs", "wrap": True},
                        {"type": "text", "text": "2. 🟥【病患紅框區】：正楷填寫病友姓名、身分證字號、聯絡手機", "size": "xxs", "color": "#334155", "margin": "xs", "wrap": True},
                        {"type": "text", "text": "3. 🧾【黏貼收據影本】：翻至指定頁面，平整浮貼滿 63 顆之自費收據影本", "size": "xxs", "color": "#334155", "margin": "xs", "wrap": True}
                    ]
                },
                {
                    "type": "box", "layout": "vertical", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": "📄 兩聯單分開處理：", "size": "xs", "weight": "bold", "color": "#0F172A"},
                        {"type": "text", "text": "• 第一聯（白色）：寄回永欣生技顧問審核", "size": "xs", "color": "#475569"},
                        {"type": "text", "text": "• 第二聯（粉紅色）：撕下由病友自行妥善留存備查", "size": "xs", "color": "#DC2626", "weight": "bold"}
                    ]
                },
                {"type": "separator", "margin": "xs"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC",
                    "cornerRadius": "md", "paddingAll": "sm",
                    "contents": [
                        {"type": "text", "text": "✉️ 寄件免貼郵票：", "size": "xs", "weight": "bold", "color": "#0F172A"},
                        {"type": "text", "text": "封底已印妥回郵地址，撕開雙面膠對摺黏牢，直接投入郵筒即可寄達永欣生技！", "size": "xxs", "color": "#64748B", "wrap": True}
                    ]
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "xs",
            "contents": [
                {
                    "type": "button", "style": "primary", "color": "#991B1B", "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "📕 查看我的病友存摺",
                        "data": "action=passbook"
                    }
                },
                {
                    "type": "button", "style": "secondary", "height": "sm",
                    "action": {
                        "type": "uri",
                        "label": "📞 致電永欣審核 (02-8780-3236)",
                        "uri": "tel:0287803236"
                    }
                }
            ]
        }
    }

# ----------------- 擊癌利專屬病友存摺（全收斂回 LINE，零跳轉） -----------------
def get_profile_summary_flex(user_id, is_caregiver=False):
    p = user_profiles.get(user_id, {
        "hosp": "三軍總醫院", "cancer": "早期乳癌", "dose": 2, "stock": 53, "code": "KSQ-0001", "pharmacy": "躍獅寶湖藥局"
    })
    dose = p.get("dose", 2)
    stock = p.get("stock", 53)
    days = stock // dose if dose > 0 else 999
    is_alert = days <= 14 and dose > 0
    code = p.get("code", "KSQ-0001")
    cancer = p.get("cancer", "早期乳癌")
    hosp = p.get("hosp", "三軍總醫院")
    pharmacy = p.get("pharmacy", "躍獅寶湖藥局")

    # 盒數與零存整付試算 (每盒 63 顆)
    cur_box = p.get("boxes", max(1, (stock + 62) // 63) if stock > 0 else 0)
    rem_to_box = 63 - (stock % 63) if (stock % 63) != 0 else 0
    left_boxes = max(0, 6 - cur_box)

    # 全期自費達成率計算 (EBC 3年目標 11 盒 / MBC 5年目標 15 盒 - 選項 A 心理激勵模型)
    is_ebc = "早期" in cancer or "EBC" in cancer
    target_boxes = 11 if is_ebc else 15
    year_label = "EBC 3年期" if is_ebc else "MBC 5年期"
    year_goal = "3 年" if is_ebc else "5 年"
    rate_pct = min(100.0, round((cur_box / target_boxes) * 100, 1))
    boxes_remain = max(0, target_boxes - cur_box)

    if rate_pct >= 100:
        enc_txt = f"🏆 恭喜圓滿完成全期 {year_goal}支持計畫！您真的非常勇敢、非常了不起！"
    elif rate_pct >= 75:
        enc_txt = f"🌟 邁入最後衝刺！達成率高達 {rate_pct}%，勝利就在眼前！"
    elif rate_pct >= 50:
        enc_txt = f"🎉 恭喜突破一半大關（{rate_pct}%）！最辛苦的第一階段已挺過，接下來藥物補助愈來愈多囉！"
    elif rate_pct >= 30:
        enc_txt = f"💖 太棒了！已完成超過 1/3 自購里程（{rate_pct}%）！離買1送3愈來愈近囉！"
    else:
        enc_txt = f"🌱 萬事起頭難，每一步都是為健康扎根！最辛苦的第一階段，小管家陪您一起堅持！"

    header_title = "📕 擊癌利存摺（家屬端）" if is_caregiver else "📕 擊癌利專屬存摺"
    badge_color = "#4338CA" if is_caregiver else ("#047857" if not is_alert else "#92400E")
    badge_label = "家屬共同照護" if is_caregiver else "買1送1"
    sub_title = f"病友代號：{code} ｜ 家屬共同照護" if is_caregiver else f"病友代號：{code} ｜ 專屬綁定存摺"

    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#065F46" if not is_alert else "#B45309",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "alignItems": "center",
                    "contents": [
                        {"type": "text", "text": header_title, "weight": "bold", "color": "#FFFFFF", "size": "md", "flex": 5},
                        {
                            "type": "box", "layout": "vertical",
                            "backgroundColor": badge_color,
                            "cornerRadius": "md", "paddingAll": "xs", "alignItems": "center", "flex": 2,
                            "contents": [
                                {"type": "text", "text": badge_label, "size": "xxs", "color": "#FFFFFF", "weight": "bold", "align": "center"}
                            ]
                        }
                    ]
                },
                {"type": "text", "text": sub_title, "color": "#E6FFFA" if not is_alert else "#FEF3C7", "size": "xs", "margin": "xs"}
            ]
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "vertical",
                    "backgroundColor": "#FFFBEB", "cornerRadius": "md",
                    "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {
                            "type": "box", "layout": "horizontal", "alignItems": "center",
                            "contents": [
                                {"type": "text", "text": f"🎯 全期自費達成率（{year_label}）", "size": "xs", "weight": "bold", "color": "#92400E", "flex": 4},
                                {"type": "text", "text": f"{rate_pct}%", "size": "md", "weight": "bold", "color": "#B45309", "align": "end", "flex": 2}
                            ]
                        },
                        {
                            "type": "text",
                            "text": f"已自費 {cur_box} / {target_boxes} 盒" + (f"（再自購 {boxes_remain} 盒圓滿完成）" if boxes_remain > 0 else "（已全數自購達標！）"),
                            "size": "xxs", "color": "#78350F", "weight": "bold"
                        },
                        {
                            "type": "text",
                            "text": enc_txt,
                            "size": "xxs", "color": "#92400E", "wrap": True
                        }
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "就診醫院", "size": "xs", "color": "#64748B", "flex": 2},
                        {"type": "text", "text": f"{hosp}", "size": "xs", "color": "#0F172A", "weight": "bold", "flex": 4}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "治療類型", "size": "xs", "color": "#64748B", "flex": 2},
                        {"type": "text", "text": f"{cancer} (第1年 1:1補助)", "size": "xs", "color": "#0F172A", "flex": 4}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "每日處方", "size": "xs", "color": "#64748B", "flex": 2},
                        {"type": "text", "text": f"每日 {dose} 顆 ({dose*200}mg)", "size": "xs", "color": "#0F172A", "weight": "bold", "flex": 4}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "手邊存藥", "size": "xs", "color": "#64748B", "flex": 2},
                        {"type": "text", "text": f"{stock} 顆（預估服 {days} 天）", "size": "xs", "color": "#B91C1C" if is_alert else "#0F172A", "weight": "bold", "flex": 4}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal",
                    "contents": [
                        {"type": "text", "text": "領藥藥局", "size": "xs", "color": "#64748B", "flex": 2},
                        {"type": "text", "text": f"{pharmacy}", "size": "xs", "color": "#047857", "weight": "bold", "flex": 4}
                    ]
                },
                {"type": "separator", "margin": "sm"},
                {
                    "type": "box", "layout": "vertical", "backgroundColor": "#F8FAFC", "cornerRadius": "md", "paddingAll": "sm", "spacing": "xs",
                    "contents": [
                        {"type": "text", "text": f"📊 第一年自費晉級進度：第 {cur_box} / 6 盒", "size": "xs", "weight": "bold", "color": "#1E293B"},
                        {"type": "text", "text": f"🪙 零存整付：再自費 {rem_to_box} 顆達標滿盒贈藥" if rem_to_box > 0 else "🪙 零存整付：已達標滿盒贈藥資格！", "size": "xxs", "color": "#D97706" if rem_to_box > 0 else "#059669", "weight": "bold"},
                        {"type": "text", "text": f"👉 再自費 {left_boxes} 盒即可晉級第2年「買1送3」方案！" if left_boxes > 0 else "🎉 已達成 6 盒！即將晉級「買1送3」！", "size": "xxs", "color": "#64748B"}
                    ]
                },
                {
                    "type": "text",
                    "text": "⚠️【安全提醒】目前存藥不足兩週！回診請記得請醫師補開處方！" if is_alert else "✅ 存藥充足（大於兩週），請依醫囑安心服藥！",
                    "size": "xs", "color": "#B45309" if is_alert else "#059669", "wrap": True
                }
            ]
        },
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        {"type": "button", "style": "primary", "color": "#047857", "height": "sm", "action": {"type": "postback", "label": "🧾 購藥登記", "data": "action=receipt"}},
                        {"type": "button", "style": "primary", "color": "#C2410C", "height": "sm", "action": {"type": "postback", "label": "⚙️ 劑量調整", "data": "action=dose"}}
                    ]
                },
                {
                    "type": "box", "layout": "horizontal", "spacing": "sm",
                    "contents": [
                        {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "🏥 門診速報", "data": "action=doctor"}},
                        {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "📬 領藥流程", "data": "action=sop"}}
                    ]
                }
            ]
        }
    }

# ----------------- 專屬問候語（已建檔 vs 未建檔） -----------------
def get_welcome_registered_messages(user, user_id):
    eff_user, eff_user_id, is_caregiver = get_effective_user(user_id)
    code = eff_user.get("code", "KSQ-0001")
    hosp = eff_user.get("hosp", "三軍總醫院")
    stock = eff_user.get("stock", 0)
    dose = eff_user.get("dose", 2)
    days = stock // dose if dose > 0 else 999
    
    if is_caregiver:
        greeting_text = (
            f"🌸【{code} 家屬照護端】您好！\n"
            f"您已成功綁定為病友【{code}】的照護家屬。\n\n"
            f"📋 病友存摺即時摘要：\n"
            f"• 就診醫院：{hosp}\n"
            f"• 每日處方：每日 {dose} 顆 ({dose*200}mg)\n"
            f"• 手邊存藥：{stock} 顆（預估可服 {days} 天）\n\n"
            f"💡 您可代為點選下方【購藥登記】更新庫存，或點選【吉立存摺】查看完整補助進度！"
        )
    else:
        greeting_text = (
            f"🌸【{code}】病友您好！\n"
            f"您已做過病友建檔。\n\n"
            f"📋 目前存摺摘要：\n"
            f"• 就診醫院：{hosp}\n"
            f"• 每日劑量：{dose} 顆 ({dose*200}mg)\n"
            f"• 手邊存藥：{stock} 顆（預估可服 {days} 天）\n\n"
            f"💡 若有新購藥物請點選下方【購藥登記】；若需查詢完整補助與服藥進度請點選【我的存摺】！"
        )
    flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
    return [
        TextSendMessage(text=greeting_text),
        FlexSendMessage(alt_text=f"📕 {code} 擊癌利存摺", contents=flex)
    ]

def get_welcome_unregistered_messages():
    greeting_text = (
        "👋 您好！歡迎使用擊癌利病患照護小管家。\n\n"
        "⚠️ 您目前【尚未開戶或綁定病友資料】！\n"
        "• 若您是病友本人：請點選【病友開戶】建立基本資料。\n"
        "• 若您是家屬/照顧者：請點選【家屬綁定】輸入病友代號即可同步存摺！"
    )
    flex = get_unregistered_prompt_flex("開戶建檔")
    return [
        TextSendMessage(text=greeting_text),
        FlexSendMessage(alt_text="⚠️ 請先完成病友開戶或家屬綁定", contents=flex)
    ]

@kiscare_bp.route("/", methods=["GET"])
def index():
    p = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(p):
        return send_file(p)
    return jsonify({"status": "online", "system": "擊癌利病患照護小管家"}), 200

@kiscare_bp.route("/health", methods=["GET"])
def health():
    return "OK", 200

@kiscare_bp.route("/api/users", methods=["GET"])
def api_users():
    return jsonify({"profiles": user_profiles, "counter": patient_counter}), 200

@kiscare_bp.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature")
        abort(400)
    except Exception as e:
        logger.error(f"Error handling webhook: {traceback.format_exc()}")

    return "OK", 200

# ----------------- POSTBACK 事件處理器 -----------------
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data
    user_id = event.source.user_id
    reply_token = event.reply_token

    user = get_or_create_user(user_id)
    eff_user, eff_user_id, is_caregiver = get_effective_user(user_id)

    try:
        # 六宮格按鍵 6: 病友建檔 (身分入口：病友開戶 vs 家屬綁定)
        if data == "action=onboard":
            if user.get("is_registered", False):
                already_flex = get_already_registered_flex(user, user_id=user_id)
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 您已建檔過", contents=already_flex))
                return

            entry_flex = get_onboarding_entry_flex()
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 擊癌利專屬存摺開戶與綁定", contents=entry_flex))

        # 病友本人開戶
        elif data == "action=start_patient_onboard":
            user["state"] = "onboarding_step_1"
            save_data(user_profiles, patient_counter)
            flex = get_onboarding_step1_flex()
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 新病友開戶建檔", contents=flex))

        # 家屬／照護者綁定
        elif data == "action=caregiver_bind":
            user["state"] = "awaiting_caregiver_code"
            save_data(user_profiles, patient_counter)
            flex = get_caregiver_input_prompt_flex()
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="👨‍👩‍👧 請輸入病友專屬代號", contents=flex))

        # 家屬驗證就診醫院
        elif data.startswith("verify_caregiver_hosp="):
            payload = data.replace("verify_caregiver_hosp=", "").strip()
            parts = payload.split("|")
            target_code = parts[0]
            selected_hosp_key = parts[1]
            
            target_pid = None
            target_prof = None
            for pid, prof in user_profiles.items():
                if prof.get("code") == target_code and prof.get("is_registered"):
                    target_pid = pid
                    target_prof = prof
                    break

            if not target_prof:
                line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 找不到該病友資料，請重新嘗試！"))
                return

            expected_hosp = HOSPITALS.get(selected_hosp_key, {}).get("name", "")
            if target_prof.get("hosp") == expected_hosp:
                user["role"] = "caregiver"
                user["is_registered"] = True
                user["bound_patient_id"] = target_pid
                user["bound_patient_code"] = target_code
                user["code"] = target_code
                user["hosp"] = target_prof.get("hosp")
                user["pharmacy"] = target_prof.get("pharmacy")
                user["state"] = None
                save_data(user_profiles, patient_counter)

                success_text = (
                    f"🎉【家屬照護綁定成功！】\n"
                    f"已成功驗證並綁定為病友【{target_code}】的照護家屬！\n\n"
                    f"即日起，您可隨時透過本小管家協助追蹤存藥天數、登記購藥入庫，並在門診與領藥時提供協助！"
                )
                flex = get_profile_summary_flex(target_pid, is_caregiver=True)
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(text=success_text),
                    FlexSendMessage(alt_text="🎉 家屬照護存摺已連線", contents=flex)
                ])
            else:
                fail_flex = {
                    "type": "bubble",
                    "header": {
                        "type": "box", "layout": "vertical", "backgroundColor": "#B45309",
                        "contents": [
                            {"type": "text", "text": "⚠️ 醫院身分驗證未通過", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                            {"type": "text", "text": f"代號：{target_code}", "color": "#FEF3C7", "size": "xs", "margin": "xs"}
                        ]
                    },
                    "body": {
                        "type": "box", "layout": "vertical", "spacing": "sm",
                        "contents": [
                            {"type": "text", "text": f"您選擇的責任醫院與【{target_code}】的實際開戶醫院不符。", "size": "xs", "color": "#0F172A", "wrap": True},
                            {"type": "text", "text": "為保障病友用藥安全與個人隱私，請確認病友目前的責任醫院後重新點選：", "size": "xxs", "color": "#64748B", "wrap": True}
                        ]
                    },
                    "footer": {
                        "type": "box", "layout": "vertical", "spacing": "sm",
                        "contents": [
                            {"type": "button", "style": "primary", "color": "#B45309", "height": "sm", "action": {"type": "postback", "label": "🔄 重新選擇醫院", "data": f"reverify_hosp={target_code}"}},
                            {"type": "button", "style": "secondary", "height": "sm", "action": {"type": "postback", "label": "❌ 重新輸入代號", "data": "action=caregiver_bind"}}
                        ]
                    }
                }
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 醫院驗證未通過", contents=fail_flex))

        # 重新選擇驗證醫院
        elif data.startswith("reverify_hosp="):
            target_code = data.replace("reverify_hosp=", "").strip()
            flex = get_caregiver_verify_hosp_flex(target_code)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🔐 請確認病友就診醫院", contents=flex))

        # 解除家屬綁定
        elif data == "action=unbind_caregiver":
            user["role"] = None
            user["is_registered"] = False
            user["bound_patient_id"] = None
            user["bound_patient_code"] = None
            user["state"] = None
            save_data(user_profiles, patient_counter)
            line_bot_api.reply_message(reply_token, TextSendMessage(
                text="👌 已成功解除家屬照護綁定！\n若日後需要重新綁定或建立新檔案，請隨時點選下方選單【病友建檔】。"
            ))

        # 變更就診醫院（跨院移轉藥局，防 Doctor shopping）
        elif data == "action=switch_hosp":
            if not eff_user.get("is_registered", False):
                flex = get_unregistered_prompt_flex("變更醫院")
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 請先完成病友開戶建檔", contents=flex))
                return
            switch_flex = get_switch_hospital_flex(eff_user)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🏥 變更責任醫院", contents=switch_flex))

        # 點選新醫院，觸發轉院防呆確認
        elif data.startswith("confirm_switch_hosp="):
            new_hosp_key = data.replace("confirm_switch_hosp=", "").strip()
            new_hosp_info = HOSPITALS.get(new_hosp_key, HOSPITALS["三總"])
            
            if eff_user.get("hosp") == new_hosp_info["name"]:
                line_bot_api.reply_message(reply_token, TextSendMessage(
                    text=f"🏥 目前已在【{eff_user['hosp']}】就診，指定藥局為【{eff_user.get('pharmacy', '指定藥局')}】，資料完全一致，無需重複變更！"
                ))
                return

            transfer_flex = get_transfer_confirm_flex(eff_user, new_hosp_key, new_hosp_info)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🔄 跨院轉移確認", contents=transfer_flex))

        # 確認執行轉院 (keep=1 保留存藥, keep=0 重新盤點)
        elif data.startswith("apply_switch="):
            query_str = data.replace("apply_switch=", "hosp=")
            params = dict(urllib.parse.parse_qsl(query_str))
            hosp_key = params.get("hosp", "三總")
            keep = params.get("keep", "1")
            new_hosp_info = HOSPITALS.get(hosp_key, HOSPITALS["三總"])

            eff_user["hosp"] = new_hosp_info["name"]
            eff_user["pharmacy"] = new_hosp_info["pharmacy"]
            if is_caregiver:
                user["hosp"] = new_hosp_info["name"]
                user["pharmacy"] = new_hosp_info["pharmacy"]

            if keep == "1":
                user["state"] = None
                save_data(user_profiles, patient_counter)
                role_msg = "（家屬代辦）" if is_caregiver else ""
                receipt_msg = (
                    f"🏥【跨院移轉完成{role_msg}】\n"
                    f"主治醫院已更新為：{new_hosp_info['name']}\n"
                    f"指定領藥藥局：{new_hosp_info['pharmacy']}\n\n"
                    f"💡 專屬病友代號【{eff_user['code']}】與手邊存藥【{eff_user['stock']} 顆】已完整保留鎖定，跨院延續不遺失！"
                )
                flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(text=receipt_msg),
                    FlexSendMessage(alt_text="📕 最新擊癌利存摺", contents=flex)
                ])
            else:
                user["state"] = "awaiting_initial_stock"
                save_data(user_profiles, patient_counter)
                hint = (
                    f"🏥 主治醫院已更新為：{new_hosp_info['name']}\n"
                    f"指定領藥藥局：{new_hosp_info['pharmacy']}。\n\n"
                    f"👉 請直接在下方聊天室輸入目前在新醫院手邊剩餘的確切存藥顆數（例如 53）："
                )
                line_bot_api.reply_message(reply_token, TextSendMessage(text=hint))

        # 要求重新建檔（若已建檔，跳出二次防呆警示，保護現有存藥）
        elif data == "action=force_onboard":
            if user.get("is_registered", False):
                confirm_flex = get_force_onboard_confirm_flex(user)
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 確定重新開戶建檔？", contents=confirm_flex))
                return

            user["is_registered"] = False
            user["state"] = "onboarding_step_1"
            save_data(user_profiles, patient_counter)
            flex = get_onboarding_step1_flex()
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 重新開戶建檔", contents=flex))

        # 病友堅持重新開戶建檔 (保留專屬代號 code 絕對鎖定！)
        elif data == "do_force_onboard":
            user["is_registered"] = False
            user["state"] = "onboarding_step_1"
            save_data(user_profiles, patient_counter)
            flex = get_onboarding_step1_flex()
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 重新開戶建檔", contents=flex))

        # 步驟 1 點選醫院
        elif data.startswith("set_hosp="):
            hosp_key = data.replace("set_hosp=", "").strip()
            hosp_info = HOSPITALS.get(hosp_key, HOSPITALS["三總"])

            # 防呆：若病友其實已建過檔卻觸發 set_hosp，導引至轉院確認
            if user.get("is_registered", False):
                transfer_flex = get_transfer_confirm_flex(user, hosp_key, hosp_info)
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🔄 跨院轉移確認", contents=transfer_flex))
                return

            user["hosp"] = hosp_info["name"]
            user["pharmacy"] = hosp_info["pharmacy"]
            user["state"] = "onboarding_step_2"
            save_data(user_profiles, patient_counter)

            flex = get_onboarding_step2_flex(hosp_info["name"])
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 請選擇治療類型", contents=flex))

        # 步驟 2 點選治療類型
        elif data.startswith("set_cancer="):
            c_type = data.replace("set_cancer=", "").strip()
            user["cancer"] = c_type
            user["state"] = "onboarding_step_3"
            save_data(user_profiles, patient_counter)

            flex = get_onboarding_step3_flex(c_type)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 請選擇每日劑量", contents=flex))

        # 劑量選擇 (步驟 3 或日常調整劑量)
        elif data.startswith("set_dose="):
            dose_val = int(data.replace("set_dose=", "").strip())
            eff_user["dose"] = dose_val
            if is_caregiver:
                user["dose"] = dose_val

            if eff_user.get("is_registered", False):
                user["state"] = None
                save_data(user_profiles, patient_counter)
                flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
                dose_note = f"每日 {dose_val} 顆 ({dose_val*200}mg)" if dose_val > 0 else "暫停服藥"
                role_note = "（家屬代為回報）" if is_caregiver else ""
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(text=f"✅ 處方劑量已更新為：{dose_note}{role_note}\n系統已為您重新精準試算安全庫存！"),
                    FlexSendMessage(alt_text="📕 最新擊癌利存摺", contents=flex)
                ])
            else:
                user["state"] = "awaiting_initial_stock"
                save_data(user_profiles, patient_counter)
                flex = get_onboarding_step4_flex(f"每日 {dose_val} 顆")
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="✨ 請輸入現有存藥顆數", contents=flex))

        # 六宮格按鍵 1: 我的存摺
        elif data == "action=passbook":
            if not eff_user.get("is_registered", False):
                flex = get_unregistered_prompt_flex("我的存摺")
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 請先完成病友開戶建檔", contents=flex))
                return
            flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="📕 您的擊癌利存摺", contents=flex))

        # 六宮格按鍵 4: 劑量調整
        elif data == "action=dose":
            if not eff_user.get("is_registered", False):
                flex = get_unregistered_prompt_flex("劑量調整")
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 請先完成病友開戶建檔", contents=flex))
                return
            dose_flex = {
                "type": "bubble",
                "header": {
                    "type": "box", "layout": "vertical", "backgroundColor": "#C2410C",
                    "contents": [
                        {"type": "text", "text": "⚙️ 醫師調整劑量", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                        {"type": "text", "text": "回報副作用不適・依醫囑調整每日顆數", "color": "#FFEDD5", "size": "xs", "margin": "xs"}
                    ]
                },
                "body": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "請直接點選本次門診醫師開立之最新劑量：", "size": "xs", "color": "#64748B"},
                        {"type": "button", "style": "primary", "color": "#1E293B", "height": "sm", "action": {"type": "postback", "label": "3 顆 / 天 (600mg 起始劑量)", "data": "set_dose=3", "displayText": "改為 3 顆"}},
                        {"type": "button", "style": "primary", "color": "#334155", "height": "sm", "action": {"type": "postback", "label": "2 顆 / 天 (400mg 標準/一級減量)", "data": "set_dose=2", "displayText": "改為 2 顆"}},
                        {"type": "button", "style": "primary", "color": "#475569", "height": "sm", "action": {"type": "postback", "label": "1 顆 / 天 (200mg 二級減量)", "data": "set_dose=1", "displayText": "改為 1 顆"}},
                        {"type": "button", "style": "primary", "color": "#D97706", "height": "sm", "action": {"type": "postback", "label": "暫停服藥 (副作用休養，不扣庫存)", "data": "set_dose=0", "displayText": "暫停服藥"}}
                    ]
                }
            }
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚙️ 醫師調整劑量", contents=dose_flex))

        # 六宮格按鍵 3: 門診速報
        elif data == "action=doctor":
            if not eff_user.get("is_registered", False):
                flex = get_unregistered_prompt_flex("門診速報")
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 請先完成病友開戶建檔", contents=flex))
                return
            dose = eff_user.get("dose", 2)
            stock = eff_user.get("stock", 0)
            code = eff_user.get("code", "KSQ-0001")
            hosp = eff_user.get("hosp", "三軍總醫院")
            cancer = eff_user.get("cancer", "早期乳癌")
            days = stock // dose if dose > 0 else 999

            cur_box = eff_user.get("boxes", max(1, (stock + 62) // 63) if stock > 0 else 0)
            is_ebc = "早期" in cancer or "EBC" in cancer
            target_boxes = 11 if is_ebc else 15
            year_label = "EBC 3年期" if is_ebc else "MBC 5年期"
            rate_pct = min(100.0, round((cur_box / target_boxes) * 100, 1))

            doc_flex = {
                "type": "bubble",
                "header": {
                    "type": "box", "layout": "vertical", "backgroundColor": "#1E293B",
                    "contents": [
                        {"type": "text", "text": "🏥 擊癌利門診速報卡", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                        {"type": "text", "text": "出示供主治醫師快速評估療程" if not is_caregiver else "家屬代出示供主治醫師快速評估", "color": "#94A3B8", "size": "xs", "margin": "xs"}
                    ]
                },
                "body": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": f"病友代號：{code} ｜ 醫院：{hosp}", "size": "xs", "weight": "bold", "color": "#991B1B"},
                        {
                            "type": "box", "layout": "horizontal", "alignItems": "center", "backgroundColor": "#FEF3C7", "cornerRadius": "md", "paddingAll": "sm",
                            "contents": [
                                {"type": "text", "text": f"🎯 全期自費達成率 ({year_label})", "size": "xs", "weight": "bold", "color": "#92400E", "flex": 4},
                                {"type": "text", "text": f"{rate_pct}%", "size": "lg", "weight": "bold", "color": "#B45309", "align": "end", "flex": 2}
                            ]
                        },
                        {"type": "text", "text": f"自費進度：已自購 {cur_box} / {target_boxes} 盒（全程目標 {target_boxes} 盒）", "size": "xxs", "color": "#78350F", "weight": "bold"},
                        {"type": "text", "text": f"治療類型：{cancer} (第 1 階段 買 1 送 1)", "size": "xs", "color": "#334155"},
                        {"type": "text", "text": f"目前劑量：每日 {dose} 顆 ({dose*200}mg)", "size": "sm", "weight": "bold", "color": "#0F172A"},
                        {"type": "text", "text": f"手邊存藥：{stock} 顆（預估剩餘 {days} 天）", "size": "sm", "weight": "bold", "color": "#B91C1C" if days <= 14 else "#0F172A"},
                        {"type": "separator", "margin": "md"},
                        {"type": "text", "text": "⚠️ 建議醫師本次門診開立自費處方補藥！" if days <= 14 else "✅ 存藥充足（大於兩週）", "size": "xs", "weight": "bold", "color": "#B45309" if days <= 14 else "#059669"}
                    ]
                }
            }
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🏥 擊癌利門診速報卡", contents=doc_flex))

        # 六宮格按鍵 5: 領藥流程
        elif data == "action=sop":
            hosp_name = eff_user.get("hosp", "三軍總醫院")
            hosp_key = "基隆長庚" if "長庚" in hosp_name else ("汐止國泰" if "國泰" in hosp_name else "三總")
            ph_info = HOSPITALS.get(hosp_key, HOSPITALS["三總"])
            sop_flex = {
                "type": "bubble",
                "header": {
                    "type": "box", "layout": "vertical", "backgroundColor": "#854D0E",
                    "contents": [
                        {"type": "text", "text": "📬 贈藥申請 ＆ 領藥流程", "weight": "bold", "color": "#FFFFFF", "size": "md"},
                        {"type": "text", "text": f"您指定的領藥藥局：{ph_info['pharmacy']}", "color": "#FEF3C7", "size": "xs", "margin": "xs"}
                    ]
                },
                "body": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "1️⃣ 填寫同意書：醫師黃框(日期必手寫) + 病患紅框 + 粉紅聯撕下自留", "size": "xs", "wrap": True, "color": "#334155"},
                        {"type": "text", "text": "2️⃣ 摺疊免郵信封：免貼郵票投郵筒，寄至【永欣生技顧問】(02-8780-3236)", "size": "xs", "wrap": True, "color": "#334155"},
                        {"type": "text", "text": "3️⃣ 接獲 SMS 簡訊：攜帶簡訊與健保卡至指定院外藥局領藥 (3個月內有效)", "size": "xs", "wrap": True, "color": "#334155"},
                        {"type": "separator", "margin": "sm"},
                        {"type": "text", "text": f"📍 藥局地址：{ph_info['address']}", "size": "xs", "color": "#0F172A", "weight": "bold", "wrap": True},
                        {"type": "text", "text": f"☎️ 藥局電話：{ph_info['phone']}", "size": "xs", "color": "#991B1B", "weight": "bold"}
                    ]
                },
                "footer": {
                    "type": "box", "layout": "vertical", "spacing": "sm",
                    "contents": [
                        {
                            "type": "button", "style": "primary", "color": "#991B1B", "height": "sm",
                            "action": {"type": "postback", "label": "📄 查看病患同意書範例", "data": "action=consent"}
                        },
                        {
                            "type": "button", "style": "secondary", "height": "sm",
                            "action": {"type": "uri", "label": f"📞 致電藥局 ({ph_info['pharmacy'][:4]})", "uri": f"tel:{ph_info['phone'].replace('-', '')}"}
                        }
                    ]
                }
            }
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="📬 贈藥申請與領藥流程", contents=sop_flex))

        # 查看病患同意書範例與教學
        elif data == "action=consent":
            consent_flex = get_consent_form_flex()
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="📄 擊癌利病患同意書範例與教學", contents=consent_flex))

        # 六宮格按鍵 2: 購藥登記
        elif data == "action=receipt":
            if not eff_user.get("is_registered", False):
                flex = get_unregistered_prompt_flex("購藥登記")
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 請先完成病友開戶建檔", contents=flex))
                return

            today_str = get_taiwan_today()
            last_date = eff_user.get("last_purchase_date")
            last_pills = eff_user.get("last_purchase_pills", 0)

            user["state"] = "awaiting_receipt_pills"
            save_data(user_profiles, patient_counter)

            role_prefix = "（家屬代辦）" if is_caregiver else ""
            hint = f"🧾【購藥收據登記{role_prefix}】\n病友【{eff_user.get('code')}】目前存摺庫存為：{eff_user.get('stock', 0)} 顆。\n請直接在聊天室輸入本次購買顆數（例如打「21」或「42」），系統將自動為您累加入庫並同步更新！"
            if last_date == today_str and last_pills > 0:
                hint += f"\n\n💡 提示：今日稍早已有一筆登記 {last_pills} 顆的紀錄。"
            line_bot_api.reply_message(reply_token, TextSendMessage(text=hint))

        # 手邊存藥庫存校正 (直接覆蓋更新總庫存)
        elif data.startswith("calibrate_stock="):
            num = int(data.replace("calibrate_stock=", "").strip())
            today_str = get_taiwan_today()
            now_time_str = get_taiwan_now_str()

            eff_user["stock"] = num
            eff_user["last_purchase_date"] = today_str
            eff_user["last_purchase_time"] = now_time_str
            eff_user["last_purchase_pills"] = num
            user["state"] = None
            save_data(user_profiles, patient_counter)

            role_msg = "（家屬代為校正）" if is_caregiver else ""
            receipt_msg = f"🔄【手邊存藥庫存校正完成{role_msg}】\n已將病友【{eff_user.get('code')}】存摺手邊庫存校正為：{num} 顆！\n系統已為您重新試算安全服藥天數。"
            flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
            line_bot_api.reply_message(reply_token, [
                TextSendMessage(text=receipt_msg),
                FlexSendMessage(alt_text="📕 最新擊癌利存摺", contents=flex)
            ])

        # 確認加買 (增加累加顆數)
        elif data.startswith("confirm_add_purchase="):
            num = int(data.replace("confirm_add_purchase=", "").strip())
            today_str = get_taiwan_today()
            now_time_str = get_taiwan_now_str()

            eff_user["stock"] = eff_user.get("stock", 0) + num
            eff_user["last_purchase_date"] = today_str
            eff_user["last_purchase_time"] = now_time_str
            eff_user["last_purchase_pills"] = num
            user["state"] = None
            save_data(user_profiles, patient_counter)

            role_msg = "（家屬代辦）" if is_caregiver else ""
            receipt_msg = f"✅【購藥加累登記成功{role_msg}】\n已為病友【{eff_user.get('code')}】額外入庫 {num} 顆！\n目前手邊總存藥為：{eff_user['stock']} 顆。"
            flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
            line_bot_api.reply_message(reply_token, [
                TextSendMessage(text=receipt_msg),
                FlexSendMessage(alt_text="📕 最新擊癌利存摺", contents=flex)
            ])

        # 更正今日購藥登記 (更正今日購藥顆數)
        elif data.startswith("overwrite_purchase="):
            new_num = int(data.replace("overwrite_purchase=", "").strip())
            old_num = eff_user.get("last_purchase_pills", 0)
            today_str = get_taiwan_today()
            now_time_str = get_taiwan_now_str()

            eff_user["stock"] = max(0, eff_user.get("stock", 0) - old_num + new_num)
            eff_user["last_purchase_date"] = today_str
            eff_user["last_purchase_time"] = now_time_str
            eff_user["last_purchase_pills"] = new_num
            user["state"] = None
            save_data(user_profiles, patient_counter)

            role_msg = "（家屬代辦）" if is_caregiver else ""
            receipt_msg = f"🔄【更正今日購藥顆數{role_msg}】\n已將今日登記由 {old_num} 顆更正為 {new_num} 顆！\n目前手邊總存藥為：{eff_user['stock']} 顆。"
            flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
            line_bot_api.reply_message(reply_token, [
                TextSendMessage(text=receipt_msg),
                FlexSendMessage(alt_text="📕 最新擊癌利存摺", contents=flex)
            ])

        # 放棄/維持原樣
        elif data == "cancel_purchase":
            user["state"] = None
            save_data(user_profiles, patient_counter)
            flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
            line_bot_api.reply_message(reply_token, [
                TextSendMessage(text="👌 已保留原存摺庫存紀錄，未進行變更！"),
                FlexSendMessage(alt_text="📕 您的擊癌利存摺", contents=flex)
            ])

    except Exception as e:
        logger.error(f"Error in handle_postback: {traceback.format_exc()}")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 系統處理中發生問題，請稍候重試或點擊選單！"))
        except Exception:
            pass

# ----------------- 文字訊息事件處理器 -----------------
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id
    reply_token = event.reply_token

    user = get_or_create_user(user_id)
    eff_user, eff_user_id, is_caregiver = get_effective_user(user_id)
    today_str = get_taiwan_today()
    now_time_str = get_taiwan_now_str()

    try:
        # 情況 A：家屬正在綁定輸入病友代號 (state == awaiting_caregiver_code)
        if user.get("state") == "awaiting_caregiver_code":
            norm_code = normalize_patient_code(text)
            if not norm_code:
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(text="⚠️ 代號格式無法識別！\n請直接輸入數字代號（例如「0001」或「KSQ-0001」）唷！"),
                    FlexSendMessage(alt_text="👨‍👩‍👧 請輸入病友專屬代號", contents=get_caregiver_input_prompt_flex())
                ])
                return

            # 搜尋是否有此病友開戶紀錄
            target_pid = None
            target_prof = None
            for pid, prof in user_profiles.items():
                if prof.get("code") == norm_code and prof.get("is_registered"):
                    target_pid = pid
                    target_prof = prof
                    break

            if not target_prof:
                flex = get_caregiver_not_found_flex(norm_code)
                line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 查無此病友代號", contents=flex))
                return

            # 查到病友，進入責任醫院隱私安全驗證
            flex = get_caregiver_verify_hosp_flex(norm_code)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🔐 請確認病友就診醫院", contents=flex))
            return

        # 使用者輸入純數字
        if any(c.isdigit() for c in text):
            num = int("".join([c for c in text if c.isdigit()]))

            # 情況 1：尚未建檔
            if not eff_user.get("is_registered", False):
                if user.get("state") == "awaiting_initial_stock":
                    # 正確完成步驟 4，輸入初始顆數
                    user["stock"] = num
                    user["is_registered"] = True
                    user["state"] = None
                    user["last_purchase_date"] = today_str
                    user["last_purchase_time"] = now_time_str
                    user["last_purchase_pills"] = num
                    save_data(user_profiles, patient_counter)

                    flex = get_profile_summary_flex(user_id)
                    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="🎉 建檔完成！您的專屬擊癌利存摺", contents=flex))
                else:
                    # 使用者未走完步驟 1~3 就直接打數字，引導建檔
                    flex = get_unregistered_prompt_flex("開戶建檔")
                    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 請先完成病友開戶或家屬綁定", contents=flex))
                return

            # 情況 2：已建檔（病友本人或家屬）輸入數字
            # 若使用者先前點選過「購藥登記」（state 為 awaiting_receipt_pills）
            if user.get("state") == "awaiting_receipt_pills":
                last_date = eff_user.get("last_purchase_date")
                last_time = eff_user.get("last_purchase_time", today_str)
                last_pills = eff_user.get("last_purchase_pills", 0)

                # 今日已登記過 ➔ 觸發防呆提醒卡片
                if last_date == today_str and last_pills > 0:
                    dup_flex = get_duplicate_purchase_flex(last_pills, num, today_str, last_time)
                    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="⚠️ 今日已登記過購藥，請確認是否重複", contents=dup_flex))
                    return

                # 今日第一次登記購藥，直接入庫
                eff_user["stock"] = eff_user.get("stock", 0) + num
                eff_user["last_purchase_date"] = today_str
                eff_user["last_purchase_time"] = now_time_str
                eff_user["last_purchase_pills"] = num
                user["state"] = None
                save_data(user_profiles, patient_counter)

                role_str = "（家屬代辦）" if is_caregiver else ""
                receipt_msg = f"✅【購藥登記成功{role_str}】\n登記日期：{today_str}\n已為病友【{eff_user.get('code')}】入庫：{num} 顆！\n目前手邊總存藥為：{eff_user['stock']} 顆。"
                flex = get_profile_summary_flex(eff_user_id, is_caregiver=is_caregiver)
                line_bot_api.reply_message(reply_token, [
                    TextSendMessage(text=receipt_msg),
                    FlexSendMessage(alt_text="📕 最新擊癌利存摺", contents=flex)
                ])
                return

            # 若使用者未點選「購藥登記」，直接在聊天室打數字 ➔ 智慧意圖確認（校正庫存 vs 加買新藥）
            confirm_flex = get_stock_confirm_flex(eff_user, num)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="📋 存藥確認與校正", contents=confirm_flex))

        # 非純數字文字訊息（一般對話、問候語或指令）
        else:
            if eff_user.get("is_registered", False):
                msgs = get_welcome_registered_messages(user, user_id)
            else:
                msgs = get_welcome_unregistered_messages()
            line_bot_api.reply_message(reply_token, msgs)

    except Exception as e:
        logger.error(f"Error in handle_text_message: {traceback.format_exc()}")
        try:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="⚠️ 系統處理中發生問題，請稍候重試或點擊選單！"))
        except Exception:
            pass

# ----------------- 加入好友事件處理器 (FollowEvent) -----------------
@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    user = get_or_create_user(user_id)

    try:
        if user.get("is_registered", False):
            msgs = get_welcome_registered_messages(user, user_id)
        else:
            msgs = get_welcome_unregistered_messages()
        line_bot_api.reply_message(reply_token, msgs)
    except Exception as e:
        logger.error(f"Error in handle_follow: {traceback.format_exc()}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
