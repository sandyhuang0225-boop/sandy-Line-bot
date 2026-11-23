// 🌟 台灣諾華 RTD 會議 Concur 報帳專用名冊一鍵加速工具 🌟
// 功能特色：
// 1. 智慧中英文姓名拆分 (支援單姓、複姓、英文名字)
// 2. 自動對應 Concur 系統官方標準英文城市 (Taipei, New Taipei, Keelung, Taoyuan, etc.)
// 3. 自動串接諾華官方 4,387 位 HCP 資料庫 (speaker_db.js) 自動帶出 HCP Pool No (ND...) 與 Oncore ID
// 4. 單欄一鍵秒複製 (姓、名、機構、城市代碼)
// 5. 整批複製 Excel / Concur TSV 格式
// 6. 一鍵發送 Concur 報帳名冊至公務信箱 (mailto)

// 台灣常見複姓列表
const COMPOUND_SURNAMES = [
  "歐陽", "司徒", "上官", "諸葛", "司馬", "皇甫", "尉遲", "申屠",
  "公孫", "令狐", "軒轅", "鍾離", "宇文", "長孫", "慕容", "司空",
  "夏侯", "諸比", "公羊", "微生", "公西", "淳于", "單于", "樂正",
  "端木", "拓跋", "東郭", "南門", "呼延", "百里", "東方", "南宮"
];

// 縣市對應 Concur 官方標準英文城市名稱
const CITY_TO_CONCUR_MAP = {
  "台北市": "Taipei",
  "臺北市": "Taipei",
  "台北": "Taipei",
  "臺北": "Taipei",
  "新北市": "New Taipei",
  "新北": "New Taipei",
  "基隆市": "Keelung",
  "基隆": "Keelung",
  "桃園市": "Taoyuan",
  "桃園縣": "Taoyuan",
  "桃園": "Taoyuan",
  "新竹市": "Hsinchu",
  "新竹縣": "Hsinchu",
  "新竹": "Hsinchu",
  "苗栗縣": "Miaoli",
  "苗栗市": "Miaoli",
  "苗栗": "Miaoli",
  "台中市": "Taichung",
  "臺中市": "Taichung",
  "台中": "Taichung",
  "臺中": "Taichung",
  "彰化縣": "Changhua",
  "彰化市": "Changhua",
  "彰化": "Changhua",
  "南投縣": "Nantou",
  "南投市": "Nantou",
  "南投": "Nantou",
  "雲林縣": "Yunlin",
  "雲林": "Yunlin",
  "嘉義市": "Chiayi",
  "嘉義縣": "Chiayi",
  "嘉義": "Chiayi",
  "台南市": "Tainan",
  "臺南市": "Tainan",
  "台南": "Tainan",
  "臺南": "Tainan",
  "高雄市": "Kaohsiung",
  "高雄": "Kaohsiung",
  "屏東縣": "Pingtung",
  "屏東市": "Pingtung",
  "屏東": "Pingtung",
  "宜蘭縣": "Yilan",
  "宜蘭市": "Yilan",
  "宜蘭": "Yilan",
  "花蓮縣": "Hualien",
  "花蓮市": "Hualien",
  "花蓮": "Hualien",
  "台東縣": "Taitung",
  "臺東縣": "Taitung",
  "台東": "Taitung",
  "臺東": "Taitung",
  "澎湖縣": "Penghu",
  "澎湖": "Penghu",
  "金門縣": "Kinmen",
  "金門": "Kinmen",
  "連江縣": "Matsu",
  "馬祖": "Matsu"
};

// 智慧姓名拆分
function splitNameForConcur(rawName) {
  if (!rawName) return { lastName: '', firstName: '' };
  const clean = rawName.trim();

  // 若含英文字母 (例如 Sandy Huang 或 Dr. John Doe)
  if (/[a-zA-Z]/.test(clean)) {
    const parts = clean.split(/\s+/);
    if (parts.length === 1) {
      return { lastName: parts[0], firstName: '' };
    }
    const lastName = parts[parts.length - 1];
    const firstName = parts.slice(0, -1).join(' ');
    return { lastName, firstName };
  }

  // 中文姓名處理
  // 檢查是否為複姓
  if (clean.length >= 3) {
    const firstTwo = clean.slice(0, 2);
    if (COMPOUND_SURNAMES.includes(firstTwo)) {
      return {
        lastName: firstTwo,
        firstName: clean.slice(2)
      };
    }
  }

  // 一般單姓 (例如 廖國秀 -> 姓: 廖, 名: 國秀)
  if (clean.length >= 2) {
    return {
      lastName: clean.slice(0, 1),
      firstName: clean.slice(1)
    };
  }

  return { lastName: clean, firstName: '' };
}

// 城市轉 Concur 英文
function mapCityToConcur(chineseCity) {
  if (!chineseCity) return 'Taipei';
  const clean = chineseCity.trim();
  for (const [zh, en] of Object.entries(CITY_TO_CONCUR_MAP)) {
    if (clean.includes(zh) || zh.includes(clean)) {
      return en;
    }
  }
  return 'Taipei';
}

// 查詢諾華官方 HCP Speaker Pool 資料庫
function lookupHcpSpeakerDb(name, hospital) {
  if (typeof window === 'undefined' || !window.NOVARTIS_SPEAKER_DATABASE) {
    return null;
  }
  const db = window.NOVARTIS_SPEAKER_DATABASE;
  const cleanName = (name || '').trim();
  if (!cleanName) return null;

  // 1. 完全比對姓名 + 醫院包含
  if (hospital) {
    const cleanHosp = hospital.trim();
    const match = db.find(d => d.n === cleanName && (d.h.includes(cleanHosp) || cleanHosp.includes(d.h) || (d.h.includes('三總') && cleanHosp.includes('三軍總醫院')) || (d.h.includes('長庚') && cleanHosp.includes('長庚'))));
    if (match) return match;
  }

  // 2. 僅比對姓名
  const nameMatches = db.filter(d => d.n === cleanName);
  if (nameMatches.length === 1) {
    return nameMatches[0];
  }

  return null;
}

// 處理出席名單為 Concur 專用資料列
function processAttendeesForConcur(attendees) {
  const list = Array.isArray(attendees) ? attendees : [];
  return list.map((a, i) => {
    const { lastName, firstName } = splitNameForConcur(a.name);
    const cityEn = mapCityToConcur(a.city);
    const isNovartis = (a.title === '台灣諾華') || (a.hospital && a.hospital.includes('諾華')) || (a.name && a.name.includes('Sandy'));
    const attendeeType = isNovartis ? 'Internal (Novartis)' : 'HCP';
    
    // 嘗試比對諾華官方資料庫
    const hcpMatch = isNovartis ? null : lookupHcpSpeakerDb(a.name, a.hospital);

    return {
      index: i + 1,
      id: a.id || i,
      rawName: a.name || '',
      lastName: lastName,
      firstName: firstName,
      hospital: a.hospital || '',
      dept: a.dept || '',
      title: a.title || '醫師',
      cityZh: a.city || '台北市',
      cityEn: cityEn,
      attendeeType: attendeeType,
      hcpPoolNo: hcpMatch ? hcpMatch.p : '',
      oncoreId: (hcpMatch && hcpMatch.i !== '申請中' && hcpMatch.i !== 'na') ? hcpMatch.i : ''
    };
  });
}

// 複製文字並提供回饋視覺
function copyFieldVal(text, btnEl, label) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    if (btnEl) {
      const origHtml = btnEl.innerHTML;
      btnEl.innerHTML = '<i class="fa-solid fa-check text-success"></i> 已複製';
      btnEl.classList.add('btn-success', 'text-white');
      btnEl.classList.remove('btn-outline-secondary', 'btn-outline-primary', 'btn-outline-dark');
      setTimeout(() => {
        btnEl.innerHTML = origHtml;
        btnEl.classList.remove('btn-success', 'text-white');
      }, 2000);
    }
  }).catch(err => {
    console.error('複製失敗:', err);
  });
}

// 批次複製 Excel / Concur TSV 格式 (可直接貼入 Excel 試算表)
function copyAllConcurTsv(concurList, btnEl) {
  if (!concurList || concurList.length === 0) {
    alert('目前尚無與會人員資料！');
    return;
  }

  let tsv = "項次\tAttendee Type\tLast Name (姓)\tFirst Name (名)\tCompany (服務機構)\tCity (Concur英文城市)\t職稱/科別\tNovartis HCP Pool No\tOncore ID\n";
  concurList.forEach(item => {
    tsv += `${item.index}\t${item.attendeeType}\t${item.lastName}\t${item.firstName}\t${item.hospital}\t${item.cityEn}\t${item.dept} ${item.title}\t${item.hcpPoolNo || '-'}\t${item.oncoreId || '-'}\n`;
  });

  navigator.clipboard.writeText(tsv).then(() => {
    if (btnEl) {
      const orig = btnEl.innerHTML;
      btnEl.innerHTML = '<i class="fa-solid fa-circle-check text-success me-1"></i>已複製 TSV 格式！可直接貼上 Excel';
      setTimeout(() => { btnEl.innerHTML = orig; }, 3500);
    }
    alert(`✅ 已成功複製全體 ${concurList.length} 位貴賓之 Concur 報帳資料！\n\n格式已設定為 Tab 分隔 (TSV)，您可以直接開啟 Excel 或 Concur 試算表貼上 (Ctrl+V)，所有欄位將完美對齊！`);
  });
}

// 複製 LINE / 備忘錄純文字總表
function copyAllConcurTextSummary(eventTitle, concurList, btnEl) {
  if (!concurList || concurList.length === 0) {
    alert('目前尚無與會人員資料！');
    return;
  }

  let text = `【Novartis RTD Concur 報帳速記清單】\n`;
  text += `活動名稱：${eventTitle || '醫學會議'}\n`;
  text += `總與會人數：${concurList.length} 位\n`;
  text += `產生時間：${new Date().toLocaleString('zh-TW')}\n\n`;

  concurList.forEach(item => {
    text += `【No.${item.index}】${item.rawName} (${item.attendeeType})\n`;
    text += `  • Last Name (姓): ${item.lastName}\n`;
    text += `  • First Name (名): ${item.firstName}\n`;
    text += `  • Company (醫院): ${item.hospital}\n`;
    text += `  • City (城市): ${item.cityEn}\n`;
    text += `  • Title/Dept: ${item.dept} ${item.title}\n`;
    if (item.hcpPoolNo) text += `  • HCP Pool No: ${item.hcpPoolNo}\n`;
    if (item.oncoreId) text += `  • Oncore ID: ${item.oncoreId}\n`;
    text += `------------------------------------\n`;
  });

  navigator.clipboard.writeText(text).then(() => {
    if (btnEl) {
      const orig = btnEl.innerHTML;
      btnEl.innerHTML = '<i class="fa-solid fa-circle-check text-success me-1"></i>已複製備忘清單！';
      setTimeout(() => { btnEl.innerHTML = orig; }, 3500);
    }
    alert(`✅ 已複製 Concur 填表備忘文字清單！\n您可直接貼入 LINE 筆記或隨身備忘錄。`);
  });
}

// 發送 Concur 報帳名冊至 Sandy 公務信箱
function sendConcurEmail(eventTitle, concurList) {
  if (!concurList || concurList.length === 0) {
    alert('目前尚無與會人員資料！');
    return;
  }

  const subject = encodeURIComponent(`【Concur 報帳名冊】${eventTitle || 'RTD 醫學會議'} (共 ${concurList.length} 位與會者)`);
  
  let bodyText = `Sandy 姐您好，\n\n此為【${eventTitle || 'RTD 醫學會議'}】之 SAP Concur 報帳專用與會者名冊資料：\n\n`;
  bodyText += `與會人數：共 ${concurList.length} 位\n\n`;

  concurList.forEach(item => {
    bodyText += `No.${item.index} | ${item.rawName} | 類型: ${item.attendeeType}\n`;
    bodyText += `  - Last Name: ${item.lastName}\n`;
    bodyText += `  - First Name: ${item.firstName}\n`;
    bodyText += `  - Company: ${item.hospital}\n`;
    bodyText += `  - City: ${item.cityEn}\n`;
    bodyText += `  - Title: ${item.dept} ${item.title}\n`;
    if (item.hcpPoolNo) bodyText += `  - HCP Pool No: ${item.hcpPoolNo}\n`;
    if (item.oncoreId) bodyText += `  - Oncore ID: ${item.oncoreId}\n`;
    bodyText += `\n`;
  });

  bodyText += `\n此信件由台灣諾華 RTD 智慧報名與報帳加速系統自動生成。`;

  const mailtoUrl = `mailto:sandy.huang@novartis.com?subject=${subject}&body=${encodeURIComponent(bodyText)}`;
  window.open(mailtoUrl, '_blank');
}

// 導出供全域調用
if (typeof window !== 'undefined') {
  window.splitNameForConcur = splitNameForConcur;
  window.mapCityToConcur = mapCityToConcur;
  window.lookupHcpSpeakerDb = lookupHcpSpeakerDb;
  window.processAttendeesForConcur = processAttendeesForConcur;
  window.copyFieldVal = copyFieldVal;
  window.copyAllConcurTsv = copyAllConcurTsv;
  window.copyAllConcurTextSummary = copyAllConcurTextSummary;
  window.sendConcurEmail = sendConcurEmail;
}
