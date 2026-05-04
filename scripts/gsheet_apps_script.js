// SiftStack daily summary → Google Sheet bridge.
//
// Paste this code into the Apps Script editor of your Sheet:
//   1. Open https://docs.google.com/spreadsheets/d/1TbKDwHiZ7iyNIhMsl7jqPAEAWcZ_igvRMPGbcyKOrN0/
//   2. Top menu: Extensions → Apps Script
//   3. Replace any default code with this entire file's contents
//   4. Click 💾 Save (Cmd+S)
//   5. Click "Deploy" (top-right blue button) → "New deployment"
//   6. Click the gear icon next to "Select type" → choose "Web app"
//   7. Description: "SiftStack daily summary endpoint"
//      Execute as: Me (your_email@gmail.com)
//      Who has access: Anyone (this is a webhook, no UI)
//   8. Click "Deploy"
//   9. Authorize when prompted (Apps Script needs permission to write to your Sheet)
//  10. Copy the Web app URL it gives you — looks like:
//      https://script.google.com/macros/s/AKfycb.../exec
//  11. Paste that URL into .env as GSHEET_WEBHOOK_URL=...
//
// After this, every daily SiftStack run will append one row to this Sheet
// at the same time the Slack ping fires.
//
// To test: copy the Web app URL into your browser. You should see:
//   {"status":"ok","message":"SiftStack webhook endpoint live. POST JSON to append a row."}
// That confirms it's reachable.

const SHEET_ID = '1TbKDwHiZ7iyNIhMsl7jqPAEAWcZ_igvRMPGbcyKOrN0';
const SUMMARY_TAB = 'Daily Summary';     // one row per daily run
const LEDGER_TAB = 'Master Ledger';      // one row per scraped record (rolling)


function doGet(e) {
  return ContentService
    .createTextOutput(JSON.stringify({
      status: 'ok',
      message: 'SiftStack webhook endpoint live. POST JSON to append a row.',
      endpoints: {
        summary: 'POST {object with summary fields} → Daily Summary tab',
        records: 'POST {type: "records", records: [array]} → Master Ledger tab (one row per record)',
      },
    }))
    .setMimeType(ContentService.MimeType.JSON);
}


function _appendRowsToSheet(ss, tabName, rows) {
  // rows: array of objects, all sharing the same keys (caller's responsibility)
  let sheet = ss.getSheetByName(tabName);
  if (!sheet) sheet = ss.insertSheet(tabName);

  if (!rows || rows.length === 0) return 0;

  const firstKeys = Object.keys(rows[0]);

  // If sheet empty, write header row
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(firstKeys);
    sheet.getRange(1, 1, 1, firstKeys.length)
      .setFontWeight('bold')
      .setBackground('#222222')
      .setFontColor('#ffffff');
    sheet.setFrozenRows(1);
  }

  // Use existing header order so new fields are dropped vs misalign columns
  const existingHeaders = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const data = rows.map(r => existingHeaders.map(h => (r[h] !== undefined && r[h] !== null) ? r[h] : ''));

  // Bulk append — far faster than per-row appendRow for large batches
  const startRow = sheet.getLastRow() + 1;
  sheet.getRange(startRow, 1, data.length, existingHeaders.length).setValues(data);
  return data.length;
}


function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.openById(SHEET_ID);

    // Records payload: bulk-append to Master Ledger tab
    if (body.type === 'records' && Array.isArray(body.records)) {
      const appended = _appendRowsToSheet(ss, LEDGER_TAB, body.records);
      const sheet = ss.getSheetByName(LEDGER_TAB);
      return ContentService
        .createTextOutput(JSON.stringify({
          status: 'ok',
          tab: LEDGER_TAB,
          appended_rows: appended,
          total_rows: sheet.getLastRow() - 1,
        }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // Default: summary payload (single-row daily summary)
    const appended = _appendRowsToSheet(ss, SUMMARY_TAB, [body]);
    const sheet = ss.getSheetByName(SUMMARY_TAB);
    return ContentService
      .createTextOutput(JSON.stringify({
        status: 'ok',
        tab: SUMMARY_TAB,
        row: sheet.getLastRow(),
        appended: Object.keys(body).length + ' fields',
      }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({
        status: 'error',
        message: err.toString(),
      }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
