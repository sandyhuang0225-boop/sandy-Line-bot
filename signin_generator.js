// 🌟 台灣諾華 RTD 醫學會議 100% 制式簽到表通用生成引擎 (A4 多頁含候補備用頁) 🌟
// 嚴格遵守諾華合規標準：
// 1. 欄位順序 1:1：No. -> 單位所在縣市 -> 服務醫院/機構 -> 科別/單位 -> 職稱(含核選方框) -> 姓名 -> 簽名
// 2. 頁面標準 A4 直向 (210 x 297 mm)，10mm 安全列印邊距，7-11 ibon 實測防裁切
// 3. 每頁必備：官方諾華 Logo、活動資訊表頭、官方個資保護告知暨同意聲明條款
// 4. 動態分頁：已報名貴賓在第 1～N 頁 (每頁 20 列)，並【永遠自動額外加附 2 頁完全空白簽到表】(項次接續編號)，以備現場臨時出席醫師加簽！

const NOVARTIS_LOGO_BASE64 = "data:image/png;base64,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";

const SIGNIN_CSS_STYLES = `
  @page {
    size: A4 portrait;
    margin: 6mm 8mm 6mm 8mm;
  }
  * {
    box-sizing: border-box;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft JhengHei", "Noto Sans TC", sans-serif;
  }
  .signin-a4-page {
    width: 210mm;
    min-height: 297mm;
    max-height: 297mm;
    page-break-after: always;
    break-after: page;
    padding: 6mm 8mm;
    box-sizing: border-box;
    background: #ffffff;
    color: #000000;
    position: relative;
    overflow: hidden;
  }
  .signin-a4-page:last-child {
    page-break-after: avoid !important;
    break-after: avoid !important;
  }
  .top-banner {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 2px;
  }
  .top-banner td {
    padding: 1px 0;
    vertical-align: middle;
  }
  .header-info-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
    margin-bottom: 2px;
  }
  .header-info-table td {
    padding: 1px 2px;
  }
  .legal-box {
    margin: 2px 0 3px 0;
    padding: 2.5px 5px;
    font-size: 7.5px;
    line-height: 1.25;
    background: #ffffff;
    border: 1px solid #64748b;
    color: #0f172a;
  }
  .signin-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 10px;
  }
  .signin-table th, .signin-table td {
    border: 1px solid #000000;
    padding: 2px 4px;
    vertical-align: middle;
  }
  .signin-table th {
    background-color: #f1f5f9;
    text-align: center;
    font-weight: bold;
    font-size: 11px;
    padding: 2.5px 4px;
  }
  .text-center { text-align: center !important; }
  .sig-col {
    width: 155px;
    background: #ffffff;
  }
`;

// 生成多頁 A4 簽到表 HTML 結構
function generateUniversalSignInHtml(eventInfo, attendeeList) {
  const info = eventInfo || {};
  const title = info.title || '醫學會議研討會 (RTD)';
  const date = info.date || '2026年';
  const time = info.time || '18:30 - 20:30';
  const venue = info.venue || info.location || '待定';

  const ROWS_PER_PAGE = 20;
  const list = Array.isArray(attendeeList) ? attendeeList : [];

  // 計算已報名者所需頁數 (至少 1 頁)
  const attendeePagesCount = Math.max(1, Math.ceil(list.length / ROWS_PER_PAGE));
  // 總頁數 = 已報名頁數 + 額外 2 頁完全空白現場候補/加簽備用頁
  const totalPages = attendeePagesCount + 2;

  let pagesHtml = '';

  // 1. 渲染已報名貴賓頁次 (1 ~ attendeePagesCount)
  for (let p = 1; p <= attendeePagesCount; p++) {
    const startIdx = (p - 1) * ROWS_PER_PAGE;
    const pageAttendees = list.slice(startIdx, startIdx + ROWS_PER_PAGE);

    let rowsHtml = '';
    for (let r = 0; r < ROWS_PER_PAGE; r++) {
      const globalIdx = startIdx + r;
      const a = pageAttendees[r];

      if (a) {
        const titleStr = a.title ? String(a.title) : '';
        const hospStr = a.hospital ? String(a.hospital) : '';
        const deptStr = a.dept ? String(a.dept) : '';
        const nameStr = a.name ? String(a.name) : '';
        const cityStr = a.city ? String(a.city) : '台北市';

        const isDoc = titleStr === '醫師';
        const isNurse = titleStr === '護理師';
        const isPharm = titleStr === '藥師';
        const isNovartis = titleStr === '台灣諾華' || hospStr.includes('諾華');
        const isOther = !isDoc && !isNurse && !isPharm;
        const otherText = isNovartis ? '台灣諾華' : (isOther ? (titleStr || '其他') : '____________');

        rowsHtml += `
          <tr>
            <td class="text-center">${globalIdx + 1}</td>
            <td class="text-center">${cityStr}</td>
            <td>${hospStr}</td>
            <td>${deptStr}</td>
            <td><div style="font-size: 8.5px; line-height: 1.25;">
              ${isDoc ? '☑' : '□'} 醫師 &nbsp;${isNurse ? '☑' : '□'} 護理師 &nbsp;${isPharm ? '☑' : '□'} 藥師<br>
              ${isOther ? '☑' : '□'} 其他HCP: ${otherText}
            </div></td>
            <td class="text-center" style="font-weight: bold; font-size: 11px;">${nameStr}</td>
            <td class="sig-col"></td>
          </tr>
        `;
      } else {
        // 第一頁報名未滿 20 人時，補足至 20 列空白
        rowsHtml += `
          <tr>
            <td class="text-center text-muted">${globalIdx + 1}</td>
            <td class="text-center"></td>
            <td></td>
            <td></td>
            <td><div style="font-size: 8.5px; line-height: 1.25;">□ 醫師 &nbsp;□ 護理師 &nbsp;□ 藥師<br>□ 其他HCP: ____________</div></td>
            <td class="text-center"></td>
            <td class="sig-col"></td>
          </tr>
        `;
      }
    }

    pagesHtml += buildSinglePageMarkup(p, totalPages, title, date, time, venue, rowsHtml, false);
  }

  // 2. 🌟 額外追加 2 頁完全空白的現場候補/臨時加簽備用頁 (項次連續編號) 🌟
  for (let b = 1; b <= 2; b++) {
    const pageNum = attendeePagesCount + b;
    const startIdx = (pageNum - 1) * ROWS_PER_PAGE;

    let rowsHtml = '';
    for (let r = 0; r < ROWS_PER_PAGE; r++) {
      const globalIdx = startIdx + r;
      rowsHtml += `
        <tr>
          <td class="text-center text-muted">${globalIdx + 1}</td>
          <td class="text-center"></td>
          <td></td>
          <td></td>
          <td><div style="font-size: 8.5px; line-height: 1.25;">□ 醫師 &nbsp;□ 護理師 &nbsp;□ 藥師<br>□ 其他HCP: ____________</div></td>
          <td class="text-center"></td>
          <td class="sig-col"></td>
        </tr>
      `;
    }

    pagesHtml += buildSinglePageMarkup(pageNum, totalPages, title, date, time, venue, rowsHtml, true);
  }

  return {
    html: pagesHtml,
    totalPages: totalPages,
    attendeePagesCount: attendeePagesCount
  };
}

// 內部輔助：組裝單一 A4 頁面完整架構
function buildSinglePageMarkup(pageNum, totalPages, title, date, time, venue, rowsHtml, isBlankBackup) {
  return `
    <div class="signin-a4-page" style="page-break-after: ${pageNum === totalPages ? 'avoid' : 'always'}; break-after: ${pageNum === totalPages ? 'avoid' : 'page'};">
      <!-- 頂部 Header：諾華官方 Logo 與 頁次 -->
      <table class="top-banner">
        <tr>
          <td style="width: 50%;">
            <img src="${NOVARTIS_LOGO_BASE64}" alt="Novartis" style="max-height: 36px; object-fit: contain;">
          </td>
          <td style="width: 50%; text-align: right; font-size: 10.5px; color: #475569;">
            ${isBlankBackup ? '<span style="color: #0284c7; font-weight: bold; margin-right: 6px;">[現場加簽備用頁]</span>' : ''}
            頁次：${pageNum} / ${totalPages}
          </td>
        </tr>
      </table>

      <!-- 活動基本資訊 (依諾華範本) -->
      <table class="header-info-table">
        <tr>
          <td style="width: 65px; font-weight: bold;">活動名稱</td>
          <td colspan="2">${title}</td>
        </tr>
        <tr>
          <td style="font-weight: bold;">日期</td>
          <td>${date}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<strong>時間：</strong>${time}</td>
          <td></td>
        </tr>
        <tr>
          <td style="font-weight: bold;">地點</td>
          <td colspan="2">${venue}</td>
        </tr>
      </table>

      <!-- 諾華制式個人資料告知事項及同意條款 (100% 公司官方 CSV 條款逐字一致) -->
      <div class="legal-box">
        與會者於此簽到表簽名，即表示已閱讀並同意下列【個人資料告知事項】及【同意】條款；如本活動提供住宿/交通或其他出席贊助（如適用），亦同意並確認下列贊助相關事項。<br>
        <strong>【個人資料告知事項】</strong><br>
        (1) 蒐集者：台灣諾華股份有限公司（以下稱『諾華』）。<br>
        (2) 蒐集目的：辦理本活動之出席管理、內外部法規遵循/稽核及相關行政作業（如有贊助/費用核銷等需求亦屬之）。<br>
        (3) 個人資料類別：姓名、服務機構、科別/單位、職稱、（如有提供）聯絡方式、簽名及出席紀錄等。<br>
        (4) 利用期間/地區/對象/方式：於活動期間及結束後5年，在中華民國境內，得以紙本或電子方式於前述目的範圍內利用，並得提供予諾華之關係企業及受託之第三方服務供應商；另得依法律或主管機關要求揭露。<br>
        (5) 權利：本人得依法行使查詢或請求閱覽、請求製給複製本、請求補充或更正、請求停止蒐集/處理/利用、請求刪除等權利；如需聯繫，請洽：taiwan.dp@novartis.com。<br>
        (6) 不提供之影響：本人得自由選擇是否提供個人資料；惟如不提供必要資料，可能影響本活動之參與或相關服務提供（例如出席證明/贊助核銷等）。<br>
        <strong>【同意】</strong>本人已閱讀並了解上述告知事項，並同意諾華於前述目的範圍內蒐集、處理及利用本人個人資料。<br>
        <strong>【若本活動提供住宿/交通或其他出席贊助（如適用）】</strong><br>
        (a)&nbsp;遵守所有相關法規命令，及所屬醫院、大學或機關之相關規章；<br>
        (b) 若有接受住宿之贊助，其住宿係由台灣諾華統籌辦理；<br>
        (c) 會提供相關單據、出席紀錄及個人資料予諾華及其指定之第三人以進行贊助查核付款作業；<br>
        (d)&nbsp;如因法規要求，諾華得公開揭露贊助醫師之項目內容；<br>
        (e) 上述補助並非用以換取採購或處方諾華產品，亦非成為推薦、處方、採購或施用諾華產品之誘因。
      </div>

      <!-- 制式簽到表格 (100% 依公司 CSV 格式與順序完全一致) -->
      <table class="signin-table">
        <thead>
          <tr>
            <th style="width: 38px;">No.</th>
            <th style="width: 85px;">單位所在縣市</th>
            <th style="width: 155px;">服務醫院/機構</th>
            <th style="width: 105px;">科別/單位</th>
            <th style="width: 160px;">職稱</th>
            <th style="width: 95px;">姓名</th>
            <th class="sig-col">簽名</th>
          </tr>
        </thead>
        <tbody>
          ${rowsHtml}
        </tbody>
      </table>
    </div>
  `;
}

// 🌟 一鍵生成並下載 7-11 ibon 專用 PDF (高畫質 A4、防裁切、多頁) 🌟
async function downloadUniversalSignInPdf(eventInfo, attendeeList, triggerBtn) {
  const targetBtn = triggerBtn || (window.event ? window.event.target.closest('button') : null);
  const originalHtml = targetBtn ? targetBtn.innerHTML : '';

  if (targetBtn) {
    targetBtn.disabled = true;
    targetBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>正在生成 7-11 ibon A4 PDF (含 2 頁空白備用)...';
  }

  const { html, totalPages } = generateUniversalSignInHtml(eventInfo, attendeeList);

  // 建立乾淨的列印暫存容器
  const printContainer = document.createElement('div');
  printContainer.id = 'dynamicPrintContainer';
  printContainer.style.position = 'fixed';
  printContainer.style.left = '-9999px';
  printContainer.style.top = '0';
  printContainer.style.width = '210mm';
  printContainer.style.background = '#ffffff';
  printContainer.innerHTML = `<style>${SIGNIN_CSS_STYLES}</style>${html}`;
  document.body.appendChild(printContainer);

  const cleanTitle = (eventInfo.title || '諾華RTD研討會').replace(/[\\/:*?"<>|]/g, '_').slice(0, 30);
  const pdfFilename = `${cleanTitle}_簽到表(完整${totalPages}頁含空白備用).pdf`;

  try {
    if (typeof html2pdf !== 'undefined') {
      const opt = {
        margin:       [0, 0, 0, 0],
        filename:     pdfFilename,
        image:        { type: 'jpeg', quality: 0.98 },
        html2canvas:  { scale: 2, useCORS: true, logging: false },
        jsPDF:        { unit: 'mm', format: 'a4', orientation: 'portrait' },
        pagebreak:    { mode: ['css', 'legacy'] }
      };
      await html2pdf().set(opt).from(printContainer).save();

      if (targetBtn) {
        targetBtn.disabled = false;
        targetBtn.innerHTML = '<i class="fa-solid fa-circle-check text-success me-1"></i>PDF 已成功下載！請上傳 ibon';
        setTimeout(() => { targetBtn.innerHTML = originalHtml; }, 4000);
      }
    } else {
      // 若 html2pdf 尚未載入，開啟專屬 A4 獨立頁備案
      openPrintSignInWindow(eventInfo, attendeeList);
      if (targetBtn) { targetBtn.disabled = false; targetBtn.innerHTML = originalHtml; }
    }
  } catch (err) {
    console.error("生成 PDF 失敗，切換為預覽頁:", err);
    openPrintSignInWindow(eventInfo, attendeeList);
    if (targetBtn) { targetBtn.disabled = false; targetBtn.innerHTML = originalHtml; }
  } finally {
    if (printContainer && printContainer.parentNode) {
      printContainer.parentNode.removeChild(printContainer);
    }
  }
}

// 🌟 開啟獨立 A4 列印與預覽視窗 (可 Ctrl+P 完美列印) 🌟
function openPrintSignInWindow(eventInfo, attendeeList) {
  try {
    const payload = {
      eventInfo: eventInfo || {},
      attendees: Array.isArray(attendeeList) ? attendeeList : [],
      updatedAt: Date.now()
    };
    localStorage.setItem('current_print_event', JSON.stringify(payload));
  } catch (e) {
    console.warn("無法存入 localStorage:", e);
  }
  window.open('print_signin.html', '_blank');
}

// 導出供全站全域調用
if (typeof window !== 'undefined') {
  window.generateUniversalSignInHtml = generateUniversalSignInHtml;
  window.downloadUniversalSignInPdf = downloadUniversalSignInPdf;
  window.openPrintSignInWindow = openPrintSignInWindow;
}
