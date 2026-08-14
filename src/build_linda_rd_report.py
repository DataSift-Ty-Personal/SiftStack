"""Build the combined Deep Prospecting PDF report for 8207 Linda Rd / Jarboe estate.

Pulls together: verified 2019 quit-claim deed finding (with embedded scans),
the title-defect analysis, signer decision tree, Julia Jarboe heir map, and the
full live Tracerfy + Trestle skip-trace number breakdown for Alicia Decker and
Krystina Edlin.

Output: 8207_Linda_Rd_Deep_Prospecting_Report.pdf in the project root.
No em/en dashes in any content (plain hyphens only).
"""

import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image, ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "8207_Linda_Rd_Deep_Prospecting_Report.pdf"

NAVY = colors.HexColor("#1f3a5f")
STEEL = colors.HexColor("#2e5a88")
LIGHT = colors.HexColor("#eef3f8")
GREEN = colors.HexColor("#1b7a43")
RED = colors.HexColor("#9c2a2a")
AMBER = colors.HexColor("#8a5a00")
GREY = colors.HexColor("#555555")

styles = getSampleStyleSheet()


def S(name, **kw):
    base = kw.pop("parent", styles["Normal"])
    return ParagraphStyle(name, parent=base, **kw)


body = S("body", fontSize=9.5, leading=13, spaceAfter=6)
small = S("small", fontSize=8, leading=10, textColor=GREY)
h1 = S("h1", parent=styles["Heading1"], fontSize=15, textColor=NAVY, spaceBefore=10, spaceAfter=6)
h2 = S("h2", parent=styles["Heading2"], fontSize=12, textColor=STEEL, spaceBefore=8, spaceAfter=4)
title = S("title", fontSize=22, leading=26, textColor=NAVY, alignment=TA_CENTER, spaceAfter=4)
sub = S("sub", fontSize=12, leading=15, textColor=STEEL, alignment=TA_CENTER, spaceAfter=2)
cap = S("cap", fontSize=8, leading=10, textColor=GREY, alignment=TA_CENTER, spaceAfter=10)
cell = S("cell", fontSize=8.5, leading=11)
cellb = S("cellb", fontSize=8.5, leading=11, fontName="Helvetica-Bold")
white_h = S("white_h", fontSize=8.5, leading=11, fontName="Helvetica-Bold", textColor=colors.white)


def bullet(items, style=body):
    return ListFlowable(
        [ListItem(Paragraph(t, style), leftIndent=10, value="•") for t in items],
        bulletType="bullet", start="•", leftIndent=12,
    )


def banner(text, color, tcolor=colors.white):
    t = Table([[Paragraph(text, S("bn", fontSize=10.5, leading=13,
                                   fontName="Helvetica-Bold", textColor=tcolor))]],
              colWidths=[7.0 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def phone_table(rows):
    header = ["Phone", "Score", "Tier", "Line type", "Carrier", "DNC", "Note"]
    data = [[Paragraph(h, white_h) for h in header]]
    for r in rows:
        data.append([Paragraph(str(c), cell) for c in r])
    widths = [0.95, 0.45, 0.85, 0.85, 1.25, 0.45, 1.7]
    t = Table(data, colWidths=[w * inch for w in widths], repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d2dd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
    ]
    # Highlight the best row (first data row of each table is the best one)
    style.append(("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#dff3e6")))
    t.setStyle(TableStyle(style))
    return t


def kv_table(pairs, c0=1.7, c1=5.3):
    data = [[Paragraph(k, cellb), Paragraph(v, cell)] for k, v in pairs]
    t = Table(data, colWidths=[c0 * inch, c1 * inch])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d2dd")),
        ("BACKGROUND", (0, 0), (0, -1), LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def img_fit(path, max_w=3.3 * inch, max_h=4.3 * inch):
    from PIL import Image as PILImage
    w, h = PILImage.open(path).size
    ratio = min(max_w / w, max_h / h)
    return Image(str(path), width=w * ratio, height=h * ratio)


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(GREY)
    canvas.drawString(0.75 * inch, 0.5 * inch,
                      "CONFIDENTIAL - prepared for internal acquisition use. Not legal advice; confirm title with a KY attorney.")
    canvas.drawRightString(7.75 * inch, 0.5 * inch, "Page %d" % doc.page)
    canvas.restoreState()


def build():
    story = []

    # ── Cover ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("Deep Prospecting and Title Verification Report", title))
    story.append(Paragraph("8207 Linda Rd, Louisville, KY 40219", sub))
    story.append(Paragraph("Jefferson County  |  Parcel 23094501210000  |  Lot 121, Pebblebrook Subdivision, Section 1", cap))
    story.append(Paragraph("Prepared June 3, 2026  |  $50,000 assignment under review", cap))
    story.append(Spacer(1, 0.05 * inch))

    story.append(banner("BOTTOM LINE", NAVY))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Title to 8207 Linda Rd is currently vested in <b>Alicia Decker</b> by a recorded 2019 quit-claim deed "
        "from <b>Julia (Julie) K. Jarboe</b>. This was verified against the recorded instrument itself "
        "(read directly, plus an independent fetch of the Jefferson County Clerk index), not taken on assertion. "
        "Two findings reframe the deal:", body))
    story.append(bullet([
        "<b>Alicia Decker is Julia's granddaughter</b> (Julia wrote \"Grandmother\" on the deed in her own hand). "
        "She is family and likely a blood heir herself, not an outsider to remove.",
        "<b>Julia may never have held clean fee title.</b> The deed recites that 2007 fee went to "
        "TT &amp; S Building &amp; Construction, LLC, while Julia held only a Contract for Deed interest. "
        "Voiding the quit-claim and assembling the Jarboe heirs does NOT cure this upstream gap.",
    ]))
    story.append(Paragraph(
        "<b>Recommendation:</b> Treat the upstream fee-title gap (TT &amp; S), not the heir signatures, as the "
        "thing that can sink the close. Get a Kentucky title attorney on it. Alicia Decker is on title and "
        "reachable at (502) 640-1613 and is likely a cooperative signer.", body))

    story.append(Spacer(1, 6))
    story.append(banner("VERIFICATION CHAIN (how we know the deed is real)", GREEN))
    story.append(Spacer(1, 4))
    story.append(bullet([
        "Independently fetched the Jefferson County Clerk index page: Grantor JARBOE JULIE, Grantee DECKER ALICIA, "
        "DEED, Book 11545 Page 539, recorded 11/05/2019, \"PEBBLEBROOK SUB SEC 1 LOT 121.\"",
        "Pulled the recorded 4-page deed image from the clerk's host (instrument #2019255619).",
        "Read the scan directly: clerk recording cover, \"QUIT CLAIM DEED\" caption, both signatures, legal description.",
    ], small))

    story.append(PageBreak())

    # ── Section 1: The Deed ──────────────────────────────────────────────
    story.append(Paragraph("1. Verified Title Finding: the 2019 Quit-Claim Deed", h1))
    story.append(kv_table([
        ("Instrument #", "2019255619 (Batch 204447), Jefferson County Clerk (Bobbie Holsclaw)"),
        ("Recorded", "11/05/2019 09:58:59 AM  |  Deed Book D 11545, Pages 539-542  |  KY fee $17.00"),
        ("Document type", "QUIT CLAIM DEED"),
        ("Grantor", "Mrs. Julie (Julia K.) Jarboe, a widowed female, 8207 Linda Rd (deceased 09/04/2022)"),
        ("Grantee", "Ms. Alicia Decker, 8207 Linda Rd (granddaughter; born 08/29/1986)"),
        ("Consideration", "$10.00 (handwritten fair market value note: $125,150.00)"),
        ("Legal", "Lot #121, Pebblebrook Subdivision, Section 1 (Plat Book 21, Page 9)"),
        ("Witnesses / notary", "Alfred Elliott; Joseph Decker. Grantor ack 9/18/19; grantee signed 10/30/19."),
    ]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Because Julia executed and recorded this while alive (2019), roughly three years before her death, "
        "the parcel left her estate by inter vivos deed. That is why the assignment chain points at Decker and why "
        "aggregators still show 2008 as the last \"sale\" (a $10 quit-claim does not record as a sale).", body))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Recorded deed: clerk cover (Pg 539) and Quit Claim Deed body (Pg 540)", h2))
    imgrow = Table([[img_fit(ROOT / "deed_p1.png"), img_fit(ROOT / "deed_p2.png")]],
                   colWidths=[3.45 * inch, 3.45 * inch])
    imgrow.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(imgrow)

    story.append(PageBreak())

    # ── Section 2: Defects ───────────────────────────────────────────────
    story.append(Paragraph("2. The Two Title Problems That Actually Matter", h1))
    story.append(banner("Problem A - the quit-claim deed is facially defective", AMBER))
    story.append(Spacer(1, 4))
    story.append(bullet([
        "The granting clause leaves the Lot and Section blanks empty (\"Lot No. , with the Section No. ,\").",
        "The acknowledgment dates are internally inconsistent: grantor 9/18/19, grantee 10/30/19, "
        "consideration stated \"as of 09/05/2019.\"",
        "This is almost certainly the \"improperly executed quit claim deed\" you were told to void.",
    ]))
    story.append(Spacer(1, 4))
    story.append(banner("Problem B - Julia may never have held clean FEE title (the bigger risk)", RED))
    story.append(Spacer(1, 4))
    story.append(Paragraph("The deed's own legal-description recital states:", body))
    story.append(bullet([
        "2007 <b>fee</b> deed went to <b>TT &amp; S Building &amp; Construction, LLC</b> (Deed Book 9051, Page 23).",
        "Julia K. Jarboe received only a <b>Contract for Deed</b> interest (Deed Book 9051, Page 25).",
    ]))
    story.append(Paragraph(
        "A quitclaim conveys only what the grantor actually had. If that contract for deed was never performed and "
        "converted into a deed, then Julia held only an equitable/contract interest, and so does Decker. In that case "
        "<b>neither Decker nor the Jarboe heirs hold the fee</b>, and fee may still sit with TT &amp; S Building &amp; "
        "Construction, LLC or its successors. This is a marketability defect that a title attorney or quiet-title "
        "action must resolve. Pulling the TT &amp; S outbound deed is the single most important next step.", body))

    story.append(Spacer(1, 8))
    # ── Section 3: Who signs ─────────────────────────────────────────────
    story.append(Paragraph("3. Who Actually Needs to Sign", h1))
    dt_header = ["Scenario", "Who conveys / signs", "Catch"]
    dt_rows = [
        ["Decker's deed is accepted",
         "Alicia Decker alone (record owner)",
         "Does not fix the TT &amp; S fee gap; QCD defects may need a corrective deed"],
        ["QCD is voided (your current plan)",
         "Julia's 5 children / their representatives",
         "They inherit only the contract interest, not fee; Decker (grantee and likely heir) probably still signs a release"],
        ["The gap that overrides both",
         "Whoever controls TT &amp; S Building &amp; Construction, LLC",
         "Likely needs a title attorney or quiet-title action to cure"],
    ]
    data = [[Paragraph(h, white_h) for h in dt_header]]
    for r in dt_rows:
        data.append([Paragraph(r[0], cellb), Paragraph(r[1], cell), Paragraph(r[2], cell)])
    t = Table(data, colWidths=[1.8 * inch, 2.1 * inch, 3.1 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d2dd")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "<b>Key point for your last-minute scramble:</b> Krystina is only a required signer on the \"void it\" path. "
        "If Decker's deed is accepted, Krystina's signature may not be needed at all. The signature that is most "
        "certainly required is Alicia Decker's, and she is already on title and reachable.", body))

    story.append(PageBreak())

    # ── Section 4: Heir map ──────────────────────────────────────────────
    story.append(Paragraph("4. Julia Jarboe Heir List (verified against her obituary)", h1))
    story.append(Paragraph(
        "Julia Kathleen Jarboe (b. 04/29/1944, d. 09/04/2022). Owen Funeral Home obituary names exactly five "
        "children, with no predeceased child listed, cross-corroborated by Virginia Mattingly's obituary.", body))
    story.append(bullet([
        "<b>Pete Presley</b> (son)",
        "<b>Kimberly Schwaniger</b> (daughter)",
        "<b>Thomas Sowders</b> (son; spouse Jackie, in-law)",
        "<b>Alice Fay Otto</b> (daughter; spouse Jerry, in-law)",
        "<b>Virginia \"Jenny\" Mattingly</b> (daughter) - DECEASED 01/28/2023, survived Julia by ~5 months. "
        "Her share passes by representation to her children: <b>Krystina Edlin, Emilie Mattingly, John Edlin</b>.",
    ]))
    story.append(Paragraph(
        "<b>Alicia Decker</b> is a granddaughter (relative graph ties her to the Otto branch), not in Jenny's line. "
        "Her exact parent-branch is unconfirmed. Heirship here is obituary-grade evidence, not a probate decree; "
        "confirm via the estate file if you take the \"void it\" path.", small))

    story.append(Spacer(1, 10))
    # ── Section 5: Skip trace ────────────────────────────────────────────
    story.append(Paragraph("5. Skip Trace: Best Contacts (live Tracerfy + Trestle)", h1))
    story.append(Paragraph(
        "Numbers below are ranked by Trestle activity score (81-100 Dial First, 61-80 Dial Second, 41-60 Dial Third, "
        "21-40 Dial Fourth, 0-20 Drop). Litigator risk came back clear (False) on every number. Highlighted row = "
        "recommended number. Total skip-trace cost: $0.35.", small))

    story.append(Spacer(1, 6))
    story.append(Paragraph("Alicia Decker  -  the signer who matters most", h2))
    story.append(Paragraph(
        "DOB 08/29/1986. Mailing address: 8207 Linda Rd, Louisville, KY 40219 (the subject property). "
        "Emails: jwromance@aol.com, mralbert2002@yahoo.com, momma2three_tinkers@yahoo.com.", small))
    story.append(Spacer(1, 3))
    story.append(phone_table([
        ["(502) 640-1613", "100", "Dial First", "Mobile", "AT&T Wireless", "Yes", "Best - call first"],
        ["(502) 290-0319", "100", "Dial First", "Landline", "AT&T PSTN", "Yes", "High-activity backup"],
        ["(502) 384-1459", "30", "Dial Fourth", "FixedVOIP", "Charter", "No", "Only non-DNC line"],
        ["(502) 368-3650", "30", "Dial Fourth", "Landline", "BellSouth", "Yes", "Low"],
        ["(502) 384-4769", "30", "Dial Fourth", "FixedVOIP", "Charter", "Yes", "Low"],
    ]))

    story.append(Spacer(1, 10))
    story.append(Paragraph("Krystina Edlin (formerly Brenzel)  -  Jarboe heir via Jenny's line", h2))
    story.append(Paragraph(
        "Mailing address: 1118 Royal Gardens Ct, Louisville, KY 40214. Email: lettelady8790@gmail.com. "
        "Note: the (502) 295-3593 number originally given as her \"best number\" scores 10 (Drop) - a dead line.", small))
    story.append(Spacer(1, 3))
    story.append(phone_table([
        ["(502) 690-9634", "70", "Dial Second", "FixedVOIP", "Charter", "No", "Best - high activity, not on DNC"],
        ["(502) 742-0624", "70", "Dial Second", "FixedVOIP", "Charter", "Yes", "Strong backup"],
        ["(502) 995-4419", "30", "Dial Fourth", "Landline", "BellSouth", "Yes", "Low"],
        ["(936) 687-2102", "30", "Dial Fourth", "Landline", "Windstream TX", "Yes", "Low"],
        ["(502) 295-3593", "10", "Drop", "Mobile", "T-Mobile", "Yes", "The original \"best number\" - dead line"],
    ]))
    story.append(Spacer(1, 5))
    story.append(Paragraph(
        "<b>DNC note:</b> Do-Not-Call governs telemarketing solicitation. A transactional call to a known party about "
        "a specific deed matter she is already involved in is a different category, but confirm your own compliance "
        "posture. Krystina's (502) 690-9634 is the clean case: high activity and not on DNC.", small))

    story.append(PageBreak())

    # ── Section 6: Next docs ─────────────────────────────────────────────
    story.append(Paragraph("6. Exact Documents to Pull Next (priority order)", h1))
    story.append(bullet([
        "<b>TT &amp; S Building &amp; Construction, LLC outbound deed.</b> search.jeffersondeeds.com -> Search By "
        "Party Name, Grantor = \"TT &amp; S Building,\" 2007-2019. Decides whether fee ever legitimately reached "
        "Julia. Most important open item.",
        "<b>Any deed OUT of Alicia Decker, 2019-present.</b> Same site, Grantor = \"Decker Alicia.\" Confirms she is "
        "still the current owner of record.",
        "<b>Julia Kathleen Jarboe probate file.</b> Jefferson County Probate (502) 595-4434 or CourtNet 2.0. Needed "
        "only for the \"void it\" path, to judicially fix the heir list.",
    ]))

    story.append(Spacer(1, 8))
    story.append(Paragraph("7. What Remains Unverified (honesty box)", h1))
    story.append(bullet([
        "The TT &amp; S deed has not been pulled yet, so the fee-title gap is identified but not resolved. This is a "
        "title-attorney question, not something to assume away.",
        "No later deed OUT of Decker was affirmatively excluded (none surfaced, but the grantor search was not run to "
        "completion).",
        "No probate case for Julia was locatable (CourtNet is login-gated); the heir list rests on two corroborating "
        "obituaries, not a court order.",
        "Whether the quit-claim's facial defects actually void it is a legal call.",
        "Alicia Decker's exact heir branch is unconfirmed (granddaughter, but via which child).",
    ]))

    story.append(Spacer(1, 10))
    story.append(banner("SOURCES", STEEL))
    story.append(Spacer(1, 4))
    story.append(bullet([
        "Jefferson County Clerk recorded deed, instrument #2019255619, Book D 11545, Pages 539-542 "
        "(search.jeffersondeeds.com index detail + deed image, read directly).",
        "Owen Funeral Home obituary (Julia Kathleen Jarboe); Newcomer Kentuckiana obituaries (Virginia \"Jenny\" "
        "Mattingly, John Mattingly, Alessa Brenzel).",
        "Tracerfy instant trace and Trestle phone_intel (live API runs, June 3, 2026).",
        "Supporting: ThatsThem, NationalPublicData, Spokeo, Radaris (people-search corroboration).",
    ], small))

    story.append(PageBreak())
    # ── Appendix: full deed pages ────────────────────────────────────────
    story.append(Paragraph("Appendix: Recorded Deed, Pages 3 and 4", h1))
    story.append(Paragraph(
        "Page 541 (legal description + TT &amp; S / Contract-for-Deed recital + notary) and Page 542 (grantor "
        "acknowledgment).", small))
    story.append(Spacer(1, 6))
    imgrow2 = Table([[img_fit(ROOT / "deed_p3.png"), img_fit(ROOT / "deed_p4.png")]],
                    colWidths=[3.45 * inch, 3.45 * inch])
    imgrow2.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                 ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(imgrow2)

    doc = SimpleDocTemplate(
        str(OUT), pagesize=letter,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="8207 Linda Rd Deep Prospecting Report",
        author="SiftStack",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(f"Report written: {OUT}  ({os.path.getsize(OUT):,} bytes)")


if __name__ == "__main__":
    build()
