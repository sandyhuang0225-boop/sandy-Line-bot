# -*- coding: utf-8 -*-
import sys
import os
sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('./kiscare'))
import json
from unittest.mock import MagicMock

# Mock line_bot_api
import kiscare.kiscare_app as app
app.line_bot_api = MagicMock()

def test_option_b_patient_flow():
    print("=== Testing Option B: Patient Flow ===")
    test_user_id = "test_user_option_b_patient"
    if test_user_id in app.user_profiles:
        del app.user_profiles[test_user_id]
    if "KSQ-0002" in app.user_profiles:
        del app.user_profiles["KSQ-0002"]

    # 1. Patient types "0002"
    mock_event = MagicMock()
    mock_event.source.user_id = test_user_id
    mock_event.message.text = "0002"
    mock_event.reply_token = "reply_token_1"
    
    app.handle_text_message(mock_event)
    user = app.user_profiles[test_user_id]
    assert user.get("code") == "KSQ-0002"
    assert "基隆長庚" in user.get("hosp")
    assert user.get("pharmacy") == "宏仁藥局"
    assert user.get("doctor") == "李春輝"
    assert user.get("is_registered") is False
    assert user.get("state") == "onboarding_step_2"
    print("Step 1 (Preloaded code input -> Locked hosp/pharmacy/doctor -> Step 1 flex): PASSED!")

    # 2. Patient clicks "set_cancer=早期乳癌"
    mock_event_pb1 = MagicMock()
    mock_event_pb1.source.user_id = test_user_id
    mock_event_pb1.postback.data = "set_cancer=早期乳癌"
    mock_event_pb1.reply_token = "reply_token_2"

    app.handle_postback(mock_event_pb1)
    user = app.user_profiles[test_user_id]
    assert user.get("cancer") == "早期乳癌"
    assert user.get("state") == "onboarding_step_3"
    print("Step 2 (Cancer type selection -> Saved -> Step 2 dose flex): PASSED!")

    # 3. Patient clicks "set_dose=2"
    mock_event_pb2 = MagicMock()
    mock_event_pb2.source.user_id = test_user_id
    mock_event_pb2.postback.data = "set_dose=2"
    mock_event_pb2.reply_token = "reply_token_3"

    app.handle_postback(mock_event_pb2)
    user = app.user_profiles[test_user_id]
    assert user.get("dose") == 2
    assert user.get("state") == "awaiting_initial_stock"
    print("Step 3 (Dose selection -> Saved -> Step 3 stock prompt): PASSED!")

    # 4. Patient types "42"
    mock_event_stock = MagicMock()
    mock_event_stock.source.user_id = test_user_id
    mock_event_stock.message.text = "42"
    mock_event_stock.reply_token = "reply_token_4"

    app.handle_text_message(mock_event_stock)
    user = app.user_profiles[test_user_id]
    assert user.get("stock") == 42
    assert user.get("is_registered") is True
    assert user.get("state") is None
    
    # Check master record KSQ-0002 is also registered
    master = app.user_profiles.get("KSQ-0002")
    assert master is not None
    assert master.get("stock") == 42
    assert master.get("is_registered") is True
    print("Step 4 (Pill count input -> Activated passbook -> Days calculated): PASSED!")

    # Test summary flex generates without error
    flex = app.get_profile_summary_flex(test_user_id)
    assert flex["type"] == "bubble"
    print("Summary Flex generation: PASSED!")

def test_option_b_caregiver_flow():
    print("\n=== Testing Option B: Caregiver Flow (Already Registered) ===")
    cg_user_id = "test_user_option_b_caregiver"
    if cg_user_id in app.user_profiles:
        del app.user_profiles[cg_user_id]

    mock_event = MagicMock()
    mock_event.source.user_id = cg_user_id
    mock_event.message.text = "我是家屬，代號 0002"
    mock_event.reply_token = "reply_token_cg"

    app.handle_text_message(mock_event)
    cg = app.user_profiles[cg_user_id]
    assert cg.get("role") == "caregiver"
    assert cg.get("bound_patient_id") == "KSQ-0002"
    assert cg.get("is_registered") is True
    
    eff_user, eff_id, is_cg = app.get_effective_user(cg_user_id)
    assert is_cg is True
    assert eff_user.get("stock") == 42
    assert eff_user.get("dose") == 2
    print("Caregiver binding to already-registered patient: PASSED!")

def test_option_b_caregiver_onboard_on_behalf():
    print("\n=== Testing Option B: Caregiver Onboarding on Behalf (Unregistered Patient) ===")
    cg_user_id = "test_user_cg_onbehalf"
    patient_code = "KSQ-0003"
    if cg_user_id in app.user_profiles:
        del app.user_profiles[cg_user_id]
    if patient_code in app.user_profiles:
        del app.user_profiles[patient_code]

    # Caregiver initiates binding for KSQ-0003
    mock_event = MagicMock()
    mock_event.source.user_id = cg_user_id
    mock_event.message.text = "我是家屬，代號 0003"
    mock_event.reply_token = "reply_token_cg_new"

    app.handle_text_message(mock_event)
    cg = app.user_profiles[cg_user_id]
    assert cg.get("role") == "caregiver"
    assert cg.get("bound_patient_id") == patient_code
    assert cg.get("state") == "onboarding_step_2"
    assert cg.get("is_registered") is False
    print("Caregiver initial setup: PASSED!")

    # Caregiver selects cancer
    mock_pb1 = MagicMock()
    mock_pb1.source.user_id = cg_user_id
    mock_pb1.postback.data = "set_cancer=晚期乳癌"
    mock_pb1.reply_token = "token_cg_pb1"
    app.handle_postback(mock_pb1)

    cg = app.user_profiles[cg_user_id]
    assert cg.get("cancer") == "晚期乳癌"
    assert cg.get("state") == "onboarding_step_3"

    # Caregiver selects dose
    mock_pb2 = MagicMock()
    mock_pb2.source.user_id = cg_user_id
    mock_pb2.postback.data = "set_dose=3"
    mock_pb2.reply_token = "token_cg_pb2"
    app.handle_postback(mock_pb2)

    cg = app.user_profiles[cg_user_id]
    assert cg.get("dose") == 3
    assert cg.get("state") == "awaiting_initial_stock"

    # Caregiver enters pills
    mock_stock = MagicMock()
    mock_stock.source.user_id = cg_user_id
    mock_stock.message.text = "63"
    mock_stock.reply_token = "token_cg_stock"
    app.handle_text_message(mock_stock)

    cg = app.user_profiles[cg_user_id]
    assert cg.get("stock") == 63
    assert cg.get("is_registered") is True
    
    # Master patient record should be active
    master = app.user_profiles.get(patient_code)
    assert master is not None
    assert master.get("cancer") == "晚期乳癌"
    assert master.get("dose") == 3
    assert master.get("stock") == 63
    assert master.get("is_registered") is True
    print("Caregiver completing all 3 questions on behalf: PASSED!")

if __name__ == "__main__":
    test_option_b_patient_flow()
    test_option_b_caregiver_flow()
    test_option_b_caregiver_onboard_on_behalf()
    print("\nALL OPTION B INTEGRATION TESTS PASSED WITH 100% SUCCESS!")
