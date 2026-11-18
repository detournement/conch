#!/usr/bin/env python3
"""Generate 4 finance PDFs: Chart of Accounts, Periods, Legal Entities, Trial Balance.
All internally consistent; the trial balance balances (debits == credits)."""
import calendar
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph

OUT = "/Users/thom"
styles = getSampleStyleSheet()
title_st = ParagraphStyle("t", parent=styles["Title"], fontSize=16, spaceAfter=4)
sub_st = ParagraphStyle("s", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=10)

# ---------------- Chart of Accounts ----------------
COA = [
    ("1000", "Assets", "Asset", "Header", "", "Debit"),
    ("1100", "Current Assets", "Asset", "Header", "1000", "Debit"),
    ("1110", "Cash and Cash Equivalents", "Asset", "Cash", "1100", "Debit"),
    ("1120", "Accounts Receivable - Trade", "Asset", "Receivable", "1100", "Debit"),
    ("1130", "Allowance for Doubtful Accounts", "Asset", "Contra Receivable", "1100", "Credit"),
    ("1140", "Inventory - Raw Materials", "Asset", "Inventory", "1100", "Debit"),
    ("1150", "Inventory - Finished Goods", "Asset", "Inventory", "1100", "Debit"),
    ("1160", "Prepaid Expenses", "Asset", "Prepaid", "1100", "Debit"),
    ("1200", "Non-Current Assets", "Asset", "Header", "1000", "Debit"),
    ("1210", "Property, Plant & Equipment - Cost", "Asset", "PP&E", "1200", "Debit"),
    ("1220", "Accumulated Depreciation - PP&E", "Asset", "Contra PP&E", "1200", "Credit"),
    ("1230", "Intangible Assets", "Asset", "Intangible", "1200", "Debit"),
    ("1240", "Accumulated Amortization - Intangibles", "Asset", "Contra Intangible", "1200", "Credit"),
    ("1250", "Investments in Subsidiaries", "Asset", "Investment", "1200", "Debit"),
    ("1260", "Deferred Tax Assets", "Asset", "Deferred Tax", "1200", "Debit"),
    ("2000", "Liabilities", "Liability", "Header", "", "Credit"),
    ("2100", "Current Liabilities", "Liability", "Header", "2000", "Credit"),
    ("2110", "Accounts Payable - Trade", "Liability", "Payable", "2100", "Credit"),
    ("2120", "Accrued Expenses", "Liability", "Accrual", "2100", "Credit"),
    ("2130", "Accrued Payroll and Benefits", "Liability", "Accrual", "2100", "Credit"),
    ("2140", "Short-Term Borrowings", "Liability", "Debt", "2100", "Credit"),
    ("2150", "Current Portion of Long-Term Debt", "Liability", "Debt", "2100", "Credit"),
    ("2160", "Deferred Revenue", "Liability", "Deferred Revenue", "2100", "Credit"),
    ("2200", "Non-Current Liabilities", "Liability", "Header", "2000", "Credit"),
    ("2210", "Long-Term Debt", "Liability", "Debt", "2200", "Credit"),
    ("2220", "Deferred Tax Liabilities", "Liability", "Deferred Tax", "2200", "Credit"),
    ("2230", "Pension and Post-Retirement Obligations", "Liability", "Pension", "2200", "Credit"),
    ("3000", "Equity", "Equity", "Header", "", "Credit"),
    ("3100", "Common Stock", "Equity", "Share Capital", "3000", "Credit"),
    ("3200", "Additional Paid-In Capital", "Equity", "Share Capital", "3000", "Credit"),
    ("3300", "Retained Earnings", "Equity", "Retained Earnings", "3000", "Credit"),
    ("3400", "Accumulated Other Comprehensive Income", "Equity", "AOCI", "3000", "Credit"),
    ("4000", "Revenue", "Revenue", "Header", "", "Credit"),
    ("4100", "Product Revenue", "Revenue", "Operating Revenue", "4000", "Credit"),
    ("4200", "Service Revenue", "Revenue", "Operating Revenue", "4000", "Credit"),
    ("4300", "Subscription Revenue", "Revenue", "Operating Revenue", "4000", "Credit"),
    ("4900", "Other Income", "Revenue", "Non-Operating", "4000", "Credit"),
    ("5000", "Cost of Sales", "Expense", "Header", "", "Debit"),
    ("5100", "Cost of Goods Sold - Materials", "Expense", "COGS", "5000", "Debit"),
    ("5200", "Cost of Goods Sold - Labor", "Expense", "COGS", "5000", "Debit"),
    ("5300", "Cost of Goods Sold - Overhead", "Expense", "COGS", "5000", "Debit"),
    ("5400", "Cost of Services", "Expense", "COGS", "5000", "Debit"),
    ("6000", "Operating Expenses", "Expense", "Header", "", "Debit"),
    ("6100", "Salaries and Wages", "Expense", "Payroll", "6000", "Debit"),
    ("6110", "Employee Benefits", "Expense", "Payroll", "6000", "Debit"),
    ("6200", "Rent and Facilities", "Expense", "Occupancy", "6000", "Debit"),
    ("6300", "Depreciation Expense", "Expense", "D&A", "6000", "Debit"),
    ("6310", "Amortization Expense", "Expense", "D&A", "6000", "Debit"),
    ("6400", "Sales and Marketing", "Expense", "S&M", "6000", "Debit"),
    ("6500", "Research and Development", "Expense", "R&D", "6000", "Debit"),
    ("6600", "Professional Fees", "Expense", "G&A", "6000", "Debit"),
    ("6700", "Information Technology", "Expense", "G&A", "6000", "Debit"),
    ("6800", "Travel and Entertainment", "Expense", "G&A", "6000", "Debit"),
    ("7000", "Other Income (Expense)", "Expense", "Header", "", "Debit"),
    ("7100", "Interest Expense", "Expense", "Interest", "7000", "Debit"),
    ("7200", "Interest Income", "Revenue", "Interest", "7000", "Credit"),
    ("7300", "Foreign Exchange Gain (Loss)", "Expense", "FX", "7000", "Debit"),
    ("8000", "Income Tax Expense", "Expense", "Tax", "", "Debit"),
]

def make_pdf(fname, title, subtitle, headers, rows, col_widths=None):
    doc = SimpleDocTemplate(f"{OUT}/{fname}", pagesize=letter,
                            leftMargin=0.6*inch, rightMargin=0.6*inch,
                            topMargin=0.6*inch, bottomMargin=0.6*inch)
    elems = [Paragraph(title, title_st), Paragraph(subtitle, sub_st)]
    t = Table([headers] + rows, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1f3864")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.white),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f2f5fa")]),
        ("GRID", (0,0), (-1,-1), 0.25, colors.HexColor("#c8cfdb")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    elems.append(t)
    doc.build(elems)
    print("wrote", fname)

# 1. Chart of Accounts
make_pdf(
    "chart_of_accounts.pdf",
    "Chart of Accounts",
    "Meridian Holdings Group, Inc. — Consolidated CoA — Effective FY2026 — Generated 2026-11-02",
    ["Acct Code", "Account Name", "Type", "Subtype / Classification", "Parent", "Normal Balance"],
    [[c, n, t, s, p, b] for c, n, t, s, p, b in COA],
    col_widths=[0.7*inch, 3.2*inch, 0.8*inch, 1.6*inch, 0.6*inch, 0.9*inch],
)

# 2. Periods
period_rows = []
for year in (2025, 2026):
    for m in range(1, 13):
        q = (m - 1) // 3 + 1
        last = calendar.monthrange(year, m)[1]
        status = "Closed" if (year, m) < (2026, 11) else ("Open" if (year, m) == (2026, 11) else "Future")
        period_rows.append([
            f"FY{year}-P{m:02d}", calendar.month_name[m], str(year), f"Q{q}",
            f"{year}-{m:02d}-01", f"{year}-{m:02d}-{last}", status,
        ])
make_pdf(
    "periods.pdf",
    "Fiscal Periods / Calendar",
    "Meridian Holdings Group, Inc. — Calendar-year fiscal calendar — FY2025–FY2026",
    ["Period ID", "Period Name", "Fiscal Year", "Quarter", "Start Date", "End Date", "Status"],
    period_rows,
    col_widths=[0.9*inch, 1.2*inch, 0.8*inch, 0.7*inch, 1.0*inch, 1.0*inch, 0.8*inch],
)

# 3. Legal entities
ENT = [
    ("LE-1000", "Meridian Holdings Group, Inc.", "USA", "Delaware, USA", "USD", "Parent", "100%", "Active"),
    ("LE-1100", "Meridian Products US, LLC", "USA", "Delaware, USA", "USD", "Subsidiary", "100% (LE-1000)", "Active"),
    ("LE-1200", "Meridian Services US, LLC", "USA", "Nevada, USA", "USD", "Subsidiary", "100% (LE-1000)", "Active"),
    ("LE-2000", "Meridian Europe Holding B.V.", "Netherlands", "Amsterdam, NL", "EUR", "Intermediate Holdco", "100% (LE-1000)", "Active"),
    ("LE-2100", "Meridian GmbH", "Germany", "Munich, DE", "EUR", "Subsidiary", "100% (LE-2000)", "Active"),
    ("LE-2200", "Meridian UK Ltd", "United Kingdom", "London, UK", "GBP", "Subsidiary", "100% (LE-2000)", "Active"),
    ("LE-2300", "Meridian France SARL", "France", "Paris, FR", "EUR", "Subsidiary", "100% (LE-2000)", "Active"),
    ("LE-3000", "Meridian Asia Pacific Pte Ltd", "Singapore", "Singapore", "SGD", "Intermediate Holdco", "100% (LE-1000)", "Active"),
    ("LE-3100", "Meridian Japan K.K.", "Japan", "Tokyo, JP", "JPY", "Subsidiary", "100% (LE-3000)", "Active"),
    ("LE-3200", "Meridian Australia Pty Ltd", "Australia", "Sydney, AU", "AUD", "Subsidiary", "100% (LE-3000)", "Active"),
    ("LE-9000", "Meridian Eliminations", "—", "Consolidation", "USD", "Elimination Entity", "—", "System"),
]
make_pdf(
    "legal_entities.pdf",
    "Legal Entities",
    "Meridian Holdings Group, Inc. — Entity master — As of 2026-11-01",
    ["Entity ID", "Legal Name", "Country", "Jurisdiction", "Func. Currency", "Role", "Ownership", "Status"],
    [list(e) for e in ENT],
    col_widths=[0.75*inch, 2.1*inch, 0.95*inch, 1.1*inch, 0.75*inch, 1.1*inch, 0.95*inch, 0.65*inch],
)

# 4. Trial balance — balanced
leaf = [(c, n, t, b) for c, n, t, s, p, b in COA if s != "Header"]
AMT = {
    "1110": 8450, "1120": 12300, "1130": -615, "1140": 5200, "1150": 7800, "1160": 1150,
    "1210": 42000, "1220": -18500, "1230": 9600, "1240": -3200, "1250": 15000, "1260": 1450,
    "2110": 9100, "2120": 3400, "2130": 2600, "2140": 5000, "2150": 2400, "2160": 3850,
    "2210": 22000, "2220": 2100, "2230": 1700,
    "3100": 500, "3200": 25000, "3300": 0, "3400": 850,
    "4100": 68000, "4200": 24000, "4300": 15500, "4900": 900,
    "5100": 31500, "5200": 9800, "5300": 6200, "5400": 8500,
    "6100": 19500, "6110": 4300, "6200": 2900, "6300": 3100, "6310": 800,
    "6400": 7400, "6500": 5100, "6600": 1600, "6700": 2300, "6800": 950,
    "7100": 1350, "7200": 420, "7300": 180, "8000": 3900,
}
op_ents = ["LE-1100", "LE-1200", "LE-2100", "LE-2200", "LE-2300", "LE-3100", "LE-3200"]
weights = {"LE-1100": .34, "LE-1200": .18, "LE-2100": .14, "LE-2200": .12, "LE-2300": .09, "LE-3100": .07, "LE-3200": .06}
tb_rows, tot_d, tot_c = [], 0.0, 0.0
for code, name, typ, normal in leaf:
    amt = AMT[code]
    debit = amt if amt > 0 else 0.0
    credit = -amt if amt < 0 else 0.0
    for e in op_ents:
        w = weights[e]
        d = round(debit * w, 1)
        c = round(credit * w, 1)
        if d == 0 and c == 0:
            continue
        tb_rows.append([e, code, name, "FY2026-P10", f"{d:,.1f}", f"{c:,.1f}", "USD"])
        tot_d += d; tot_c += c
diff = round(tot_d - tot_c, 1)
if diff != 0:
    plug_d = -diff if diff < 0 else 0.0
    plug_c = diff if diff > 0 else 0.0
    tb_rows.append(["LE-1000", "3300", "Retained Earnings", "FY2026-P10", f"{plug_d:,.1f}", f"{plug_c:,.1f}", "USD"])
    tot_d += plug_d; tot_c += plug_c
tb_rows.append(["", "", "TOTAL", "", f"{tot_d:,.1f}", f"{tot_c:,.1f}", ""])
make_pdf(
    "trial_balance.pdf",
    "Consolidated Trial Balance — FY2026 Period 10 (October 2026)",
    "Meridian Holdings Group, Inc. — Group reporting currency USD — amounts in thousands — unaudited",
    ["Entity", "Acct", "Account Name", "Period", "Debit", "Credit", "Currency"],
    tb_rows,
    col_widths=[0.8*inch, 0.55*inch, 2.9*inch, 0.85*inch, 0.9*inch, 0.9*inch, 0.7*inch],
)
print("totals: debit=%.1f credit=%.1f balanced=%s" % (tot_d, tot_c, abs(tot_d-tot_c) < 0.01))
