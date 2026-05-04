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


/**
 * Master Ledger upsert — preserves Mike's manual edits across re-pushes.
 *
 * Difference from _appendRowsToSheet: instead of blindly appending, we look
 * up each row by address and:
 *   - If the address exists already, UPDATE the system-managed columns
 *     (run_date, dedup_status, datasift_uploaded, datasift_tagged) and
 *     LEAVE Mike's columns alone (mike_status, last_touched, mike_notes)
 *   - If the address is new, append it with default mike_status = "New"
 *
 * Columns preserved across runs (Mike's working columns):
 *   - mike_status, last_touched, mike_notes
 */
function _upsertLedgerRows(ss, rows) {
  let sheet = ss.getSheetByName(LEDGER_TAB);
  if (!sheet) sheet = ss.insertSheet(LEDGER_TAB);
  if (!rows || rows.length === 0) return { appended: 0, updated: 0 };

  // First-time setup
  if (sheet.getLastRow() === 0) {
    const headers = Object.keys(rows[0]);
    sheet.appendRow(headers);
    sheet.getRange(1, 1, 1, headers.length)
      .setFontWeight('bold')
      .setBackground('#222222')
      .setFontColor('#ffffff');
    sheet.setFrozenRows(1);
  }

  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const addrCol = headers.indexOf('address');
  const cityCol = headers.indexOf('city');
  if (addrCol < 0 || cityCol < 0) {
    // Fallback to plain append if expected columns missing
    return { appended: _appendRowsToSheet(ss, LEDGER_TAB, rows), updated: 0 };
  }

  // Build address-key index of existing rows (case-insensitive, trimmed)
  const lastRow = sheet.getLastRow();
  const existingIndex = {};
  if (lastRow > 1) {
    const addrCityRange = sheet.getRange(2, 1, lastRow - 1, headers.length).getValues();
    for (let i = 0; i < addrCityRange.length; i++) {
      const addr = String(addrCityRange[i][addrCol] || '').trim().toLowerCase();
      const city = String(addrCityRange[i][cityCol] || '').trim().toLowerCase();
      if (addr && city) existingIndex[`${addr}|${city}`] = i + 2; // sheet row #
    }
  }

  // Columns Mike owns — never overwrite during upsert
  const protectedCols = ['mike_status', 'last_touched', 'mike_notes'];

  const newRows = [];
  let updated = 0;

  for (const row of rows) {
    const addr = String(row.address || '').trim().toLowerCase();
    const city = String(row.city || '').trim().toLowerCase();
    const key = `${addr}|${city}`;

    if (existingIndex[key]) {
      // UPDATE: write only system-managed columns, preserve Mike's
      const sheetRow = existingIndex[key];
      const existingValues = sheet.getRange(sheetRow, 1, 1, headers.length).getValues()[0];
      const newValues = headers.map((h, i) => {
        if (protectedCols.includes(h)) return existingValues[i];
        if (h === 'mike_status' || h === 'last_touched' || h === 'mike_notes') {
          return existingValues[i];  // belt + suspenders
        }
        return (row[h] !== undefined && row[h] !== null) ? row[h] : existingValues[i];
      });
      sheet.getRange(sheetRow, 1, 1, headers.length).setValues([newValues]);
      updated++;
    } else {
      // APPEND
      newRows.push(headers.map(h => (row[h] !== undefined && row[h] !== null) ? row[h] : ''));
    }
  }

  if (newRows.length > 0) {
    const startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, newRows.length, headers.length).setValues(newRows);
  }

  return { appended: newRows.length, updated };
}


/**
 * onEdit installable trigger — auto-stamp last_touched when Mike updates a row.
 *
 * Setup (one time):
 *   Apps Script editor → Triggers (clock icon, left sidebar) → Add Trigger
 *     - Function: onEditTrigger
 *     - Event source: From spreadsheet
 *     - Event type: On edit
 *     - Save (authorize when prompted)
 */
function onEditTrigger(e) {
  if (!e || !e.range) return;
  const sheet = e.range.getSheet();
  if (sheet.getName() !== LEDGER_TAB) return;
  if (e.range.getRow() === 1) return;  // header row — skip

  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const editedCol = headers[e.range.getColumn() - 1];

  // Only stamp when Mike's columns change
  const watchedCols = ['mike_status', 'mike_notes'];
  if (!watchedCols.includes(editedCol)) return;

  const lastTouchedCol = headers.indexOf('last_touched') + 1;
  if (lastTouchedCol < 1) return;

  const stamp = Utilities.formatDate(new Date(),
    Session.getScriptTimeZone() || 'America/New_York',
    'yyyy-MM-dd HH:mm');
  sheet.getRange(e.range.getRow(), lastTouchedCol).setValue(stamp);
}


/**
 * Weekly status summary — produces a small table on the "Weekly" tab so
 * Aaron + Mike can see at-a-glance how leads are progressing.
 *
 * Setup (one time):
 *   Apps Script editor → Triggers → Add Trigger
 *     - Function: weeklyStatusSummary
 *     - Event source: Time-driven
 *     - Type of time-based trigger: Week timer
 *     - Day of the week: Friday
 *     - Time: 5pm to 6pm
 */
function weeklyStatusSummary() {
  const ss = SpreadsheetApp.openById(SHEET_ID);
  const ledger = ss.getSheetByName(LEDGER_TAB);
  if (!ledger || ledger.getLastRow() < 2) return;

  const headers = ledger.getRange(1, 1, 1, ledger.getLastColumn()).getValues()[0];
  const data = ledger.getRange(2, 1, ledger.getLastRow() - 1, ledger.getLastColumn()).getValues();
  const statusCol = headers.indexOf('mike_status');
  const typeCol = headers.indexOf('notice_type');
  const countyCol = headers.indexOf('county');
  if (statusCol < 0) return;

  // Tally by status + (type, county)
  const tally = {};
  for (const row of data) {
    const status = String(row[statusCol] || 'New').trim();
    const type = String(row[typeCol] || '?').trim();
    const county = String(row[countyCol] || '?').trim();
    const k1 = `status:${status}`;
    const k2 = `type:${type}`;
    const k3 = `county:${county}`;
    tally[k1] = (tally[k1] || 0) + 1;
    tally[k2] = (tally[k2] || 0) + 1;
    tally[k3] = (tally[k3] || 0) + 1;
  }

  let weekly = ss.getSheetByName('Weekly');
  if (!weekly) weekly = ss.insertSheet('Weekly');
  weekly.clear();
  weekly.appendRow(['Snapshot', Utilities.formatDate(new Date(),
    Session.getScriptTimeZone() || 'America/New_York', 'yyyy-MM-dd HH:mm')]);
  weekly.appendRow([]);
  weekly.appendRow(['Bucket', 'Count']);
  for (const [k, v] of Object.entries(tally)) {
    weekly.appendRow([k, v]);
  }
  weekly.getRange(3, 1, 1, 2).setFontWeight('bold');
}


function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.openById(SHEET_ID);

    // Records payload: upsert into Master Ledger (preserves Mike's columns)
    if (body.type === 'records' && Array.isArray(body.records)) {
      const result = _upsertLedgerRows(ss, body.records);
      const sheet = ss.getSheetByName(LEDGER_TAB);
      return ContentService
        .createTextOutput(JSON.stringify({
          status: 'ok',
          tab: LEDGER_TAB,
          appended_rows: result.appended,
          updated_rows: result.updated,
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
