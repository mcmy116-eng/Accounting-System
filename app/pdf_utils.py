import io
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from app.models import CompanySettings, money


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="DocTitle", fontSize=20, leading=24, textColor=colors.HexColor("#1f2937")))
    styles.add(ParagraphStyle(name="Small", fontSize=9, textColor=colors.HexColor("#6b7280")))
    return styles


def invoice_pdf(invoice):
    settings = CompanySettings.query.first()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = _styles()
    story = []

    story.append(Paragraph(escape(settings.company_name) if settings else "Company", styles["DocTitle"]))
    if settings and settings.legal_address:
        story.append(Paragraph(escape(settings.legal_address).replace("\n", "<br/>"), styles["Small"]))
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph(escape(f"INVOICE {invoice.number}"), styles["Heading1"]))
    story.append(Paragraph(escape(f"Bill to: {invoice.contact.name}"), styles["Normal"]))
    if invoice.contact.address:
        story.append(Paragraph(escape(invoice.contact.address).replace("\n", "<br/>"), styles["Small"]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        f"Issue date: {invoice.issue_date.isoformat()} &nbsp;&nbsp; Due date: {invoice.due_date.isoformat()} "
        f"&nbsp;&nbsp; Status: {invoice.status.upper()}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 6 * mm))

    data = [["Description", "Qty", "Unit Price", "Amount"]]
    for l in invoice.lines:
        data.append([
            l.description, f"{l.quantity:g}",
            f"{invoice.currency_code} {money(l.unit_price_cents)}",
            f"{invoice.currency_code} {money(l.amount_cents)}",
        ])
    table = Table(data, colWidths=[80 * mm, 20 * mm, 35 * mm, 35 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111827")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e7eb")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.append(Spacer(1, 6 * mm))

    totals = [
        ["Subtotal", f"{invoice.currency_code} {money(invoice.subtotal_cents)}"],
        ["Tax", f"{invoice.currency_code} {money(invoice.tax_cents)}"],
        ["Total", f"{invoice.currency_code} {money(invoice.total_cents)}"],
        ["Paid", f"{invoice.currency_code} {money(invoice.amount_paid_cents)}"],
        ["Balance due", f"{invoice.currency_code} {money(invoice.balance_due_cents())}"],
    ]
    ttable = Table(totals, colWidths=[135 * mm, 35 * mm])
    ttable.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#111827")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(ttable)

    if invoice.notes:
        story.append(Spacer(1, 8 * mm))
        story.append(Paragraph("Notes", styles["Heading3"]))
        story.append(Paragraph(escape(invoice.notes).replace("\n", "<br/>"), styles["Normal"]))

    doc.build(story)
    buf.seek(0)
    return buf
