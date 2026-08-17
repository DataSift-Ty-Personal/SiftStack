#!/usr/bin/env node
/**
 * build_docx.js — Converts a playbook/SOP markdown file into a polished Word document.
 *
 * Extracts Mermaid code blocks, renders them as PNG images via mmdc (Mermaid CLI),
 * then builds a formatted .docx with the images embedded inline using docx-js.
 *
 * Usage:
 *   node build_docx.js input.md output.docx [--title "My Playbook"]
 */

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const os = require("os");

// Resolve docx from multiple possible locations
let docxPath;
const tryPaths = [
  path.join(process.cwd(), "node_modules", "docx"),
  path.join("/sessions/confident-gallant-pasteur", "node_modules", "docx"),
  "docx", // global fallback
];
for (const p of tryPaths) {
  try { require.resolve(p); docxPath = p; break; } catch {}
}
if (!docxPath) { console.error("Error: 'docx' npm package not found. Install with: npm install docx"); process.exit(1); }

const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, ImageRun,
  Header, Footer, AlignmentType, LevelFormat, HeadingLevel, BorderStyle,
  WidthType, ShadingType, PageBreak, PageNumber, TabStopType, TabStopPosition
} = require(docxPath);

// ---------------------------------------------------------------------------
// Args
// ---------------------------------------------------------------------------
const args = process.argv.slice(2);
let inputFile, outputFile, title;
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--title" && args[i + 1]) { title = args[++i]; continue; }
  if (!inputFile) { inputFile = args[i]; continue; }
  if (!outputFile) { outputFile = args[i]; continue; }
}
if (!inputFile || !outputFile) {
  console.error("Usage: node build_docx.js input.md output.docx [--title 'Title']");
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Mermaid rendering
// ---------------------------------------------------------------------------
function findMmdc() {
  const candidates = [
    path.join(process.cwd(), "node_modules", ".bin", "mmdc"),
    path.join("/sessions/confident-gallant-pasteur", "node_modules", ".bin", "mmdc"),
    path.join(os.homedir(), ".npm-global", "bin", "mmdc"),
  ];
  for (const c of candidates) {
    if (fs.existsSync(c)) return c;
  }
  try {
    const which = execSync("which mmdc", { encoding: "utf8" }).trim();
    if (which) return which;
  } catch {}
  return null;
}

function renderMermaid(code, outputPath, mmdc) {
  const tmpFile = path.join(os.tmpdir(), `mermaid_${Date.now()}_${Math.random().toString(36).slice(2)}.mmd`);
  fs.writeFileSync(tmpFile, code);
  try {
    execSync(`${mmdc} -i "${tmpFile}" -o "${outputPath}" -b white -w 1200 -s 2`, {
      timeout: 30000, stdio: "pipe"
    });
    return fs.existsSync(outputPath) && fs.statSync(outputPath).size > 0;
  } catch (e) {
    console.error(`  Warning: Mermaid render failed: ${e.message}`);
    return false;
  } finally {
    try { fs.unlinkSync(tmpFile); } catch {}
  }
}

// ---------------------------------------------------------------------------
// Colors
// ---------------------------------------------------------------------------
const COLORS = {
  darkNavy: "1A1A2E",
  darkGray: "2D3436",
  medGray: "636E72",
  lightGray: "DFE6E9",
  veryLightGray: "F8F9FA",
  white: "FFFFFF",
  proTipBg: "E8F5E9",
  proTipText: "2D6A4F",
  screenshotBg: "FFF8E1",
  screenshotText: "555555",
  quoteBg: "F0F7FF",
  quoteText: "2D3436",
  headerBg: "D5E8F0",
};

// ---------------------------------------------------------------------------
// Markdown parser
// ---------------------------------------------------------------------------
function parseMarkdown(md) {
  const blocks = [];
  const lines = md.split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Mermaid code block
    if (line.trim().startsWith("```mermaid")) {
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      blocks.push({ type: "mermaid", code: codeLines.join("\n") });
      i++;
      continue;
    }

    // Generic code block
    if (line.trim().startsWith("```")) {
      const lang = line.trim().replace(/^`+/, "").trim();
      const codeLines = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      blocks.push({ type: "code", lang, code: codeLines.join("\n") });
      i++;
      continue;
    }

    // Horizontal rule
    if (/^(---|\*\*\*|___)$/.test(line.trim())) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    // Headers
    const headerMatch = line.match(/^(#{1,4})\s+(.*)/);
    if (headerMatch) {
      blocks.push({ type: "heading", level: headerMatch[1].length, text: headerMatch[2].trim() });
      i++;
      continue;
    }

    // Table
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|[\s\-:|]+\|\s*$/.test(lines[i + 1])) {
      const tableRows = [];
      while (i < lines.length && lines[i].includes("|")) {
        const stripped = lines[i].trim();
        if (/^\|[\s\-:|]+\|$/.test(stripped)) { i++; continue; }
        const cells = stripped.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        tableRows.push(cells);
        i++;
      }
      blocks.push({ type: "table", rows: tableRows });
      continue;
    }

    // Blockquote
    if (line.trim().startsWith(">")) {
      const quoteLines = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        quoteLines.push(lines[i].trim().replace(/^>\s*/, ""));
        i++;
      }
      blocks.push({ type: "blockquote", text: quoteLines.join("\n") });
      continue;
    }

    // Checklist
    if (/^\s*[-*]\s*\[[ x]\]/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s*\[[ x]\]/.test(lines[i])) {
        const checked = /\[x\]/i.test(lines[i]);
        const text = lines[i].replace(/^\s*[-*]\s*\[[ x]\]\s*/, "");
        items.push({ checked, text });
        i++;
      }
      blocks.push({ type: "checklist", items });
      continue;
    }

    // Numbered list
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ type: "numbered_list", items });
      continue;
    }

    // Bullet list
    if (/^\s*[-*]\s+/.test(line) && !/^\s*[-*]\s*\[/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      blocks.push({ type: "bullet_list", items });
      continue;
    }

    // Paragraph
    if (line.trim()) {
      const paraLines = [];
      while (i < lines.length && lines[i].trim() &&
        !lines[i].trim().startsWith("#") &&
        !lines[i].trim().startsWith("```") &&
        !/^(---|\*\*\*|___)$/.test(lines[i].trim()) &&
        !lines[i].trim().startsWith(">") &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !/^\s*[-*]\s+/.test(lines[i]) &&
        !(lines[i].includes("|") && i + 1 < lines.length && lines[i + 1] && lines[i + 1].includes("|"))
      ) {
        paraLines.push(lines[i].trim());
        i++;
      }
      blocks.push({ type: "paragraph", text: paraLines.join(" ") });
      continue;
    }

    i++;
  }
  return blocks;
}

// ---------------------------------------------------------------------------
// Inline markdown to TextRun array
// ---------------------------------------------------------------------------
function inlineToRuns(text, baseStyle = {}) {
  const runs = [];
  // Split on bold (**...**), italic (*...*), and inline code (`...`)
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`|([^*`]+))/g;
  let match;
  while ((match = regex.exec(text)) !== null) {
    if (match[2]) {
      // Bold
      runs.push(new TextRun({ text: match[2], bold: true, ...baseStyle }));
    } else if (match[3]) {
      // Italic
      runs.push(new TextRun({ text: match[3], italics: true, ...baseStyle }));
    } else if (match[4]) {
      // Code
      runs.push(new TextRun({ text: match[4], font: "Courier New", size: 18, ...baseStyle }));
    } else if (match[5]) {
      // Plain text — replace → with ->
      runs.push(new TextRun({ text: match[5].replace(/→/g, "->"), ...baseStyle }));
    }
  }
  if (runs.length === 0) {
    runs.push(new TextRun({ text: text.replace(/→/g, "->"), ...baseStyle }));
  }
  return runs;
}

// ---------------------------------------------------------------------------
// Build the document
// ---------------------------------------------------------------------------
async function buildDocx(mdText, outputPath, docTitle) {
  const mmdc = findMmdc();
  if (!mmdc) console.error("Warning: mmdc not found. Mermaid diagrams will be placeholders.");

  const blocks = parseMarkdown(mdText);
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "playbook-"));
  let mermaidCount = 0;
  const children = [];

  const thinBorder = { style: BorderStyle.SINGLE, size: 1, color: COLORS.lightGray };
  const cellBorders = { top: thinBorder, bottom: thinBorder, left: thinBorder, right: thinBorder };
  const cellMargins = { top: 80, bottom: 80, left: 120, right: 120 };

  // Page dimensions
  const PAGE_WIDTH = 12240;
  const MARGIN = 1440;
  const CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN; // 9360

  for (const block of blocks) {
    switch (block.type) {

      case "heading": {
        const level = block.level;
        const headingLevel = level === 1 ? HeadingLevel.HEADING_1
          : level === 2 ? HeadingLevel.HEADING_2
          : level === 3 ? HeadingLevel.HEADING_3
          : HeadingLevel.HEADING_4;
        children.push(new Paragraph({
          heading: headingLevel,
          children: inlineToRuns(block.text),
          spacing: level <= 2 ? { before: 240, after: 120 } : { before: 180, after: 80 },
          ...(level <= 2 ? {
            border: { bottom: { style: BorderStyle.SINGLE, size: 1, color: COLORS.lightGray, space: 4 } }
          } : {}),
        }));
        break;
      }

      case "paragraph": {
        // Detect motto (italic wrapped)
        if (block.text.startsWith("*") && block.text.endsWith("*") && block.text.length < 200) {
          const clean = block.text.replace(/^\*+|\*+$/g, "");
          children.push(new Paragraph({
            alignment: AlignmentType.CENTER,
            spacing: { after: 200 },
            children: [new TextRun({ text: clean, italics: true, color: COLORS.medGray, size: 24, font: "Arial" })],
          }));
        } else {
          children.push(new Paragraph({
            spacing: { after: 120 },
            children: inlineToRuns(block.text, { font: "Arial", size: 20 }),
          }));
        }
        break;
      }

      case "blockquote": {
        const text = block.text;
        const isScreenshot = text.includes("SCREENSHOT:");
        const isProTip = text.includes("Pro Tip:");

        const bgColor = isScreenshot ? COLORS.screenshotBg
          : isProTip ? COLORS.proTipBg
          : COLORS.quoteBg;
        const textColor = isScreenshot ? COLORS.screenshotText
          : isProTip ? COLORS.proTipText
          : COLORS.quoteText;

        // Build as a single-cell table for background color
        const quoteLines = text.split("\n");
        const quoteParagraphs = quoteLines.map(l =>
          new Paragraph({
            spacing: { after: 40 },
            children: inlineToRuns(l, { font: "Arial", size: 18, color: textColor, italics: isProTip || isScreenshot }),
          })
        );

        children.push(new Table({
          width: { size: CONTENT_WIDTH, type: WidthType.DXA },
          columnWidths: [CONTENT_WIDTH],
          rows: [new TableRow({
            children: [new TableCell({
              width: { size: CONTENT_WIDTH, type: WidthType.DXA },
              shading: { fill: bgColor, type: ShadingType.CLEAR },
              borders: {
                top: { style: BorderStyle.NONE, size: 0 },
                bottom: { style: BorderStyle.NONE, size: 0 },
                right: { style: BorderStyle.NONE, size: 0 },
                left: { style: BorderStyle.SINGLE, size: 6, color: isProTip ? "2D6A4F" : isScreenshot ? "F9A825" : "4A90D9" },
              },
              margins: { top: 100, bottom: 100, left: 200, right: 200 },
              children: quoteParagraphs,
            })]
          })],
        }));
        children.push(new Paragraph({ spacing: { after: 80 } })); // spacer
        break;
      }

      case "mermaid": {
        mermaidCount++;
        const pngPath = path.join(tmpDir, `mermaid_${mermaidCount}.png`);

        // Count nodes to warn about oversized charts
        const nodeCount = (block.code.match(/\w+[\[{(]/g) || []).length;
        if (nodeCount > 7) {
          console.warn(`  Warning: Mermaid chart #${mermaidCount} has ~${nodeCount} nodes (max recommended: 7). Consider splitting into smaller charts.`);
        }

        if (mmdc && renderMermaid(block.code, pngPath, mmdc)) {
          try {
            const imgData = fs.readFileSync(pngPath);
            // PNG header: width at offset 16 (4 bytes BE), height at offset 20 (4 bytes BE)
            const width = imgData.readUInt32BE(16);
            const height = imgData.readUInt32BE(20);

            // Scale to fit content width with minimum readability enforcement
            // Use full content width (6.5 inches = 624px at 96dpi) for better readability
            const maxW = 624;
            const maxH = 700;  // Allow taller charts since segmented charts are narrower
            const minW = 400;  // Minimum width — never shrink below this
            const minH = 200;  // Minimum height

            let ratio = Math.min(maxW / width, maxH / height, 1);
            let finalW = Math.round(width * ratio);
            let finalH = Math.round(height * ratio);

            // Enforce minimum dimensions — if the chart would be too small, scale up
            if (finalW < minW && width > 0) {
              const upRatio = minW / width;
              finalW = minW;
              finalH = Math.round(height * upRatio);
            }
            // Cap again in case upscaling made it too tall
            if (finalH > maxH) {
              const downRatio = maxH / finalH;
              finalW = Math.round(finalW * downRatio);
              finalH = maxH;
            }

            children.push(new Paragraph({ spacing: { before: 120, after: 120 } }));
            children.push(new Paragraph({
              alignment: AlignmentType.CENTER,
              children: [new ImageRun({
                type: "png",
                data: imgData,
                transformation: { width: finalW, height: finalH },
                altText: { title: "Process Map", description: "Flowchart diagram", name: `mermaid_${mermaidCount}` },
              })],
            }));
            children.push(new Paragraph({ spacing: { after: 120 } }));
          } catch (e) {
            console.error(`  Warning: Could not embed image: ${e.message}`);
            children.push(new Paragraph({
              children: [new TextRun({ text: `[Flowchart — see source .md file]`, italics: true, color: "999999", font: "Arial", size: 18 })],
            }));
          }
        } else {
          children.push(new Paragraph({
            shading: { fill: "F5F5F5", type: ShadingType.CLEAR },
            children: [new TextRun({ text: `[Flowchart — install @mermaid-js/mermaid-cli to render]`, italics: true, color: "999999", font: "Courier New", size: 16 })],
          }));
        }
        break;
      }

      case "code": {
        const codeLines = block.code.split("\n");
        for (const cl of codeLines) {
          children.push(new Paragraph({
            shading: { fill: "F5F5F5", type: ShadingType.CLEAR },
            indent: { left: 360 },
            spacing: { after: 0 },
            children: [new TextRun({ text: cl || " ", font: "Courier New", size: 16 })],
          }));
        }
        children.push(new Paragraph({ spacing: { after: 120 } }));
        break;
      }

      case "table": {
        if (!block.rows.length) break;
        const colCount = block.rows[0].length;
        const colWidth = Math.floor(CONTENT_WIDTH / colCount);
        const columnWidths = Array(colCount).fill(colWidth);
        // Adjust last column to account for rounding
        columnWidths[colCount - 1] = CONTENT_WIDTH - colWidth * (colCount - 1);

        const rows = block.rows.map((row, rowIdx) => {
          const isHeader = rowIdx === 0;
          return new TableRow({
            children: row.map((cell, colIdx) => new TableCell({
              width: { size: columnWidths[colIdx], type: WidthType.DXA },
              borders: cellBorders,
              margins: cellMargins,
              shading: {
                fill: isHeader ? COLORS.darkNavy : (rowIdx % 2 === 0 ? COLORS.white : COLORS.veryLightGray),
                type: ShadingType.CLEAR,
              },
              children: [new Paragraph({
                children: inlineToRuns(cell, {
                  font: "Arial",
                  size: 18,
                  bold: isHeader,
                  color: isHeader ? COLORS.white : COLORS.darkGray,
                }),
              })],
            })),
          });
        });

        children.push(new Paragraph({ spacing: { before: 80 } }));
        children.push(new Table({
          width: { size: CONTENT_WIDTH, type: WidthType.DXA },
          columnWidths,
          rows,
        }));
        children.push(new Paragraph({ spacing: { after: 80 } }));
        break;
      }

      case "numbered_list": {
        for (const [idx, item] of block.items.entries()) {
          children.push(new Paragraph({
            indent: { left: 720, hanging: 360 },
            spacing: { after: 60 },
            children: [
              new TextRun({ text: `${idx + 1}. `, bold: true, font: "Arial", size: 20 }),
              ...inlineToRuns(item, { font: "Arial", size: 20 }),
            ],
          }));
        }
        children.push(new Paragraph({ spacing: { after: 80 } }));
        break;
      }

      case "bullet_list": {
        for (const item of block.items) {
          children.push(new Paragraph({
            indent: { left: 720, hanging: 360 },
            spacing: { after: 60 },
            children: [
              new TextRun({ text: "\u2022 ", font: "Arial", size: 20 }),
              ...inlineToRuns(item, { font: "Arial", size: 20 }),
            ],
          }));
        }
        children.push(new Paragraph({ spacing: { after: 80 } }));
        break;
      }

      case "checklist": {
        for (const item of block.items) {
          const check = item.checked ? "\u2611" : "\u2610";
          children.push(new Paragraph({
            indent: { left: 720, hanging: 360 },
            spacing: { after: 60 },
            children: [
              new TextRun({ text: `${check} `, font: "Arial", size: 20 }),
              ...inlineToRuns(item.text, { font: "Arial", size: 20 }),
            ],
          }));
        }
        children.push(new Paragraph({ spacing: { after: 80 } }));
        break;
      }

      case "hr": {
        children.push(new Paragraph({
          spacing: { before: 120, after: 120 },
          border: { bottom: { style: BorderStyle.SINGLE, size: 1, color: COLORS.lightGray, space: 4 } },
        }));
        break;
      }
    }
  }

  // Build document
  const doc = new Document({
    styles: {
      default: {
        document: { run: { font: "Arial", size: 20 } },
      },
      paragraphStyles: [
        {
          id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 40, bold: true, font: "Arial", color: COLORS.darkNavy },
          paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 },
        },
        {
          id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 30, bold: true, font: "Arial", color: COLORS.darkNavy },
          paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 },
        },
        {
          id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 24, bold: true, font: "Arial", color: COLORS.darkGray },
          paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 2 },
        },
        {
          id: "Heading4", name: "Heading 4", basedOn: "Normal", next: "Normal", quickFormat: true,
          run: { size: 22, bold: true, italics: true, font: "Arial", color: COLORS.darkGray },
          paragraph: { spacing: { before: 160, after: 80 }, outlineLevel: 3 },
        },
      ],
    },
    sections: [{
      properties: {
        page: {
          size: { width: 12240, height: 15840 },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
        },
      },
      headers: {
        default: new Header({
          children: [new Paragraph({
            alignment: AlignmentType.RIGHT,
            children: [new TextRun({
              text: docTitle || "Playbook",
              font: "Arial", size: 16, color: COLORS.medGray, italics: true,
            })],
          })],
        }),
      },
      footers: {
        default: new Footer({
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [
              new TextRun({ text: "Page ", font: "Arial", size: 16, color: COLORS.medGray }),
              new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: COLORS.medGray }),
            ],
          })],
        }),
      },
      children,
    }],
  });

  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(outputPath, buffer);
  console.log(`DOCX created: ${outputPath}`);

  // Cleanup
  try { fs.rmSync(tmpDir, { recursive: true }); } catch {}
}

// ---------------------------------------------------------------------------
// Run
// ---------------------------------------------------------------------------
const mdText = fs.readFileSync(inputFile, "utf8");
buildDocx(mdText, outputFile, title).catch(e => {
  console.error("Error:", e);
  process.exit(1);
});
