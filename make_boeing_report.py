#!/usr/bin/env python3
"""A terrible (parody) Boeing annual report. Super short. Deliberately bad."""
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

OUT = "/Users/thom/boeing_annual_report_terrible.pdf"
styles = getSampleStyleSheet()

big = ParagraphStyle("big", parent=styles["Title"], fontSize=22, spaceAfter=2)
h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=13, textColor=colors.HexColor("#0b3d91"), spaceBefore=10)
body = ParagraphStyle("body", parent=styles["Normal"], fontSize=9.5, leading=12)
small = ParagraphStyle("small", parent=styles["Normal"], fontSize=7, textColor=colors.grey, leading=8.5)
center = ParagraphStyle("center", parent=body, alignment=1)

doc = SimpleDocTemplate(OUT, pagesize=letter, leftMargin=0.9*inch, rightMargin=0.9*inch, topMargin=0.8*inch, bottomMargin=0.8*inch)
E = []

E.append(Paragraph("BOEING-ish", big))
E.append(Paragraph("Annual Report 2026", ParagraphStyle("sub", parent=styles["Normal"], fontSize=12, textColor=colors.grey)))
E.append(Paragraph("<i>“Moving Forward.”</i> — motto, pending legal review", small))
E.append(Spacer(1, 14))

E.append(Paragraph("A Letter From Our CEO (Draft 7, FINAL_v2_USE_THIS_ONE)", h1))
E.append(Paragraph(
    "Dear Shareholder,<br/><br/>"
    "This year was a year. We built airplanes. Some of them are still airplanes. "
    "Our strategy remains clear: synergy, altitude, and checking if the doors are attached. "
    "I am confident that next year we will exceed expectations, assuming expectations are lowered first.",
    body))
E.append(Spacer(1, 8))

E.append(Paragraph("Key Financial Highlights (unaudited, unreviewed, unbothered)", h1))
fin = [
    ["Metric", "2026", "2025", "Trend"],
    ["Revenue", "$66B*", "$66B", "flat-ish"],
    ["Net Income", "($4B)", "($2B)", "✈ down, but like, in a bad way"],
    ["Airplanes Delivered", "some", "more than some", "vibes"],
    ["Door Plugs Retained", "99.7%", "N/A", "improving!"],
    ["Free Cash Flow", "not free", "also not free", "consistent"],
]
t = Table(fin, colWidths=[2.0*inch, 1.1*inch, 1.1*inch, 1.9*inch])
t.setStyle(TableStyle([
    ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0b3d91")),
    ("TEXTCOLOR", (0,0), (-1,0), colors.white),
    ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
    ("FONTSIZE", (0,0), (-1,-1), 8.5),
    ("GRID", (0,0), (-1,-1), 0.25, colors.grey),
    ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#eef2f9")]),
    ("TOPPADDING", (0,0), (-1,-1), 3), ("BOTTOMPADDING", (0,0), (-1,-1), 3),
]))
E.append(t)
E.append(Paragraph("* includes $4B of revenue we recognized for morale purposes", small))
E.append(Spacer(1, 8))

E.append(Paragraph("Safety & Quality Update", h1))
E.append(Paragraph(
    "Safety is our #1 priority, narrowly edging out priorities #1a through #1f (stock buybacks, "
    "executive bonuses, moving HQ again, stock buybacks). This year we launched "
    "<i>Quality Palooza '26</i>, a mandatory 45-minute webinar. Attendance: 61%. Completion: unknown. "
    "Bolts: mostly present.", body))
E.append(Spacer(1, 8))

E.append(Paragraph("737 MAX: The Return: Part IV: This Time For Real", h1))
E.append(Paragraph(
    "Certification is expected any quarter now. We have said this 11 consecutive quarters, "
    "which technically makes it a tradition.", body))
E.append(Spacer(1, 8))

E.append(Paragraph("Forward-Looking Statements", h1))
E.append(Paragraph(
    "This report contains forward-looking statements. They are looking forward because "
    "they dare not look back. Actual results may differ materially, spiritually, and aerodynamically.",
    small))
E.append(Spacer(1, 10))
E.append(Paragraph("— End of Report. That is all we are legally required to say. —", center))

doc.build(E)
print("wrote", OUT)
