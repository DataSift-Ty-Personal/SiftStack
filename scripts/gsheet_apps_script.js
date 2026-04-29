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
const SHEET_TAB_NAME = 'Daily Summary';  // tab name within the Sheet


function doGet(e) {
  // Browser ping — friendly status response.
  return ContentService
    .createTextOutput(JSON.stringify({
      status: 'ok',
      message: 'SiftStack webhook endpoint live. POST JSON to append a row.',
    }))
    .setMimeType(ContentService.MimeType.JSON);
}


function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.openById(SHEET_ID);

    // Get or create the Daily Summary tab
    let sheet = ss.getSheetByName(SHEET_TAB_NAME);
    if (!sheet) {
      sheet = ss.insertSheet(SHEET_TAB_NAME);
    }

    const keys = Object.keys(body);
    const values = Object.values(body);

    // First-time setup: write headers if sheet is empty
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(keys);
      // Style header row: bold + frozen
      sheet.getRange(1, 1, 1, keys.length)
        .setFontWeight('bold')
        .setBackground('#222222')
        .setFontColor('#ffffff');
      sheet.setFrozenRows(1);
    } else {
      // Existing sheet — make sure column order matches; if not, append
      // values in the order matching the existing headers.
      const existingHeaders = sheet.getRange(1, 1, 1, sheet.getLastColumn())
        .getValues()[0];
      const orderedRow = existingHeaders.map(h => body[h] !== undefined ? body[h] : '');
      sheet.appendRow(orderedRow);
      // Note: any new keys in `body` not present in existing headers are
      // dropped silently. To add new columns, edit the header row manually
      // OR delete the sheet and let the next run recreate it.
    }

    return ContentService
      .createTextOutput(JSON.stringify({
        status: 'ok',
        row: sheet.getLastRow(),
        appended: keys.length + ' fields',
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
