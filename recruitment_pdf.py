"""Shared print layout for recruitment documents."""
from io import BytesIO
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Table, TableStyle


def layout(title, ernasa_logo, company_logo):
    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, title=title,
                                 rightMargin=42, leftMargin=42, topMargin=38, bottomMargin=42)
    font = "ErnasaUnicode"
    pdfmetrics.registerFont(TTFont(font, os.getenv(
        "ERNASA_IK_PDF_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")))
    styles = getSampleStyleSheet()
    for name in ("Title", "Heading2", "BodyText", "Normal"):
        styles[name].fontName = font
        styles[name].textColor = colors.HexColor("#142d4d")
    styles["Title"].alignment = 0
    styles["BodyText"].leading = 15

    def image(source):
        if not source:
            return ""
        width, height = ImageReader(source).getSize()
        scale = min(150 / width, 58 / height)
        return Image(source, width * scale, height * scale)

    header = Table([[image(str(ernasa_logo)) if ernasa_logo.is_file() else "",
                    image(BytesIO(company_logo)) if company_logo else ""]],
                   colWidths=[document.width / 2] * 2)
    header.setStyle(TableStyle([
        ("ALIGN", (0, 0), (0, 0), "LEFT"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LINEBELOW", (0, 0), (-1, -1), 0.75, colors.HexColor("#b7c9dd")),
    ]))
    return buffer, document, styles, [header, Paragraph(escape(title), styles["Title"])]


def paragraph(value, style):
    return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)
