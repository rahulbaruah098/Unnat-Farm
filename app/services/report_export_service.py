from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape as xml_escape


def _safe_text(value):
    return "" if value is None else str(value)


def _sheet_name(value, used):
    name = re.sub(r"[\\/*?:\[\]]", " ", _safe_text(value)).strip() or "Report"
    name = name[:31]
    base = name
    counter = 2
    while name in used:
        suffix = f" {counter}"
        name = (base[:31 - len(suffix)] + suffix).strip()
        counter += 1
    used.add(name)
    return name


def _cell_ref(col_index, row_index):
    letters = ""
    n = col_index
    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row_index}"


def _inline_cell(col, row, value, style=0):
    ref = _cell_ref(col, row)
    text = xml_escape(_safe_text(value))
    return f'<c r="{ref}" t="inlineStr" s="{style}"><is><t xml:space="preserve">{text}</t></is></c>'


def _worksheet_xml(rows, widths=None, freeze_row=None):
    max_cols = max([len(r[0]) for r in rows] or [1])
    widths = widths or [18] * max_cols
    col_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{max(10, min(width, 45))}" customWidth="1"/>'
        for idx, width in enumerate(widths, start=1)
    )
    sheet_rows = []
    for row_index, (values, style) in enumerate(rows, start=1):
        cells = "".join(_inline_cell(col_index, row_index, value, style) for col_index, value in enumerate(values, start=1))
        height = ' ht="24" customHeight="1"' if style in {1, 2, 3} else ""
        sheet_rows.append(f'<row r="{row_index}"{height}>{cells}</row>')
    pane = ""
    if freeze_row:
        pane = f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="{freeze_row}" topLeftCell="A{freeze_row + 1}" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
    else:
        pane = '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
{pane}<sheetFormatPr defaultRowHeight="18"/><cols>{col_xml}</cols><sheetData>{''.join(sheet_rows)}</sheetData>
</worksheet>'''


def build_xlsx(export_data):
    sheets = []
    used = set()

    summary_rows = [
        ([export_data.get("title") or "UnnatFarm Report"], 2),
        ([export_data.get("scope_label") or ""], 3),
        ([export_data.get("subtitle") or ""], 0),
        ([f"Generated: {export_data.get('generated_on') or ''}"], 0),
        ([""], 0),
        (["Applied Filters"], 1),
    ]
    for label, value in export_data.get("applied_filters") or []:
        summary_rows.append(([label, value], 0))
    summary_rows.extend([([""], 0), (["Summary"], 1)])
    for label, value in export_data.get("kpis") or []:
        summary_rows.append(([label, value], 0))
    if export_data.get("notice"):
        summary_rows.extend([([""], 0), (["Note", export_data["notice"]], 0)])
    sheets.append((_sheet_name("Summary", used), summary_rows, [24, 70], None))

    for table in export_data.get("tables") or []:
        rows = [([table.get("title") or "Details"], 2), (table.get("headers") or ["Details"], 1)]
        rows.extend([(list(row), 0) for row in table.get("rows") or []])
        headers = table.get("headers") or []
        widths = []
        for index, header in enumerate(headers):
            longest = len(_safe_text(header))
            for row in (table.get("rows") or [])[:250]:
                if index < len(row):
                    longest = max(longest, len(_safe_text(row[index])))
            widths.append(min(max(longest + 3, 12), 36))
        sheets.append((_sheet_name(table.get("title") or "Details", used), rows, widths or [18], 2))

    workbook_sheets = []
    workbook_rels = []
    content_types = []
    for idx, (name, _rows, _widths, _freeze) in enumerate(sheets, start=1):
        workbook_sheets.append(f'<sheet name="{xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>')
        workbook_rels.append(f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>')
        content_types.append(f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')

    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="4"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="16"/><color rgb="FF17252A"/><name val="Calibri"/></font><font><b/><sz val="12"/><color rgb="FF3B7D55"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF5FA878"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border/><border><left style="thin"><color rgb="FFDCEEE2"/></left><right style="thin"><color rgb="FFDCEEE2"/></right><top style="thin"><color rgb="FFDCEEE2"/></top><bottom style="thin"><color rgb="FFDCEEE2"/></bottom></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{''.join(content_types)}<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>''')
        zf.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>''')
        zf.writestr("xl/workbook.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{''.join(workbook_sheets)}</sheets></workbook>''')
        zf.writestr("xl/_rels/workbook.xml.rels", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{''.join(workbook_rels)}<Relationship Id="rId{len(sheets)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''')
        zf.writestr("xl/styles.xml", styles)
        for idx, (_name, rows, widths, freeze) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", _worksheet_xml(rows, widths, freeze))
        zf.writestr("docProps/core.xml", f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>{xml_escape(_safe_text(export_data.get('title')))}</dc:title><dc:creator>UnnatFarm MIS</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>''')
        zf.writestr("docProps/app.xml", '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>UnnatFarm MIS</Application></Properties>''')
    return buffer.getvalue()


def _pdf_ascii(value):
    text = _safe_text(value)
    replacements = {"₹": "Rs. ", "–": "-", "—": "-", "→": "->", "≤": "<=", "✓": "OK", "·": "-"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def _wrap(text, width=92):
    words = _pdf_ascii(text).split()
    lines, current = [], ""
    for word in words:
        candidate = word if not current else current + " " + word
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _build_pdf_fallback(export_data):
    lines = []
    lines.append((export_data.get("title") or "UnnatFarm Report", 16, True))
    lines.append((export_data.get("scope_label") or "", 12, True))
    if export_data.get("subtitle"):
        lines.append((export_data["subtitle"], 10, False))
    lines.append((f"Generated: {export_data.get('generated_on') or ''}", 9, False))
    lines.append(("", 8, False))
    lines.append(("FILTERS", 10, True))
    for label, value in export_data.get("applied_filters") or []:
        lines.append((f"{label}: {value}", 9, False))
    lines.append(("", 8, False))
    lines.append(("SUMMARY", 10, True))
    for label, value in export_data.get("kpis") or []:
        lines.append((f"{label}: {value}", 9, False))
    if export_data.get("notice"):
        lines.append(("", 8, False))
        for wrapped in _wrap("Note: " + export_data["notice"]):
            lines.append((wrapped, 8, False))
    for table in export_data.get("tables") or []:
        lines.append(("", 8, False))
        lines.append((table.get("title") or "Details", 11, True))
        headers = table.get("headers") or []
        for row in table.get("rows") or []:
            parts = [f"{headers[i]}: {row[i]}" for i in range(min(len(headers), len(row)))]
            for wrapped in _wrap(" | ".join(parts), 110):
                lines.append((wrapped, 7, False))

    pages = []
    current = []
    y = 800
    for text, size, bold in lines:
        needed = max(12, size + 4)
        if y - needed < 45:
            pages.append(current)
            current = []
            y = 800
        current.append((text, size, bold, y))
        y -= needed
    if current or not pages:
        pages.append(current)

    objects = []
    page_ids = []
    content_ids = []
    # object 1 catalog, 2 pages root, 3 regular font, 4 bold font
    objects.extend([None, None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"])
    next_id = 5
    for page in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        page_ids.append(page_id); content_ids.append(content_id)
        stream = ["BT"]
        for text, size, bold, y in page:
            safe = _pdf_ascii(text).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            font = "F2" if bold else "F1"
            stream.append(f"/{font} {size} Tf 45 {y} Td ({safe}) Tj {-45} {-y} Td")
        stream.append("ET")
        raw = "\n".join(stream).encode("latin-1", "replace")
        page_obj = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>".encode()
        content_obj = b"<< /Length " + str(len(raw)).encode() + b" >>\nstream\n" + raw + b"\nendstream"
        while len(objects) < page_id:
            objects.append(None)
        objects[page_id - 1] = page_obj
        while len(objects) < content_id:
            objects.append(None)
        objects[content_id - 1] = content_obj
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode()

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{idx} 0 obj\n".encode())
        output.write(obj or b"<<>>")
        output.write(b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects)+1}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return output.getvalue()


def build_pdf(export_data):
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=12*mm, leftMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="UFTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, textColor=colors.HexColor("#17252A"), alignment=TA_LEFT, spaceAfter=4))
        styles.add(ParagraphStyle(name="UFSub", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#64748B"), leading=12))
        story = [Paragraph(_pdf_ascii(export_data.get("title") or "UnnatFarm Report"), styles["UFTitle"]), Paragraph(_pdf_ascii(export_data.get("scope_label") or ""), styles["Heading3"]), Paragraph(_pdf_ascii(export_data.get("subtitle") or ""), styles["UFSub"]), Spacer(1, 5)]
        filters = [["Filter", "Value"]] + [[_pdf_ascii(a), _pdf_ascii(b)] for a, b in export_data.get("applied_filters") or []]
        if filters:
            table = Table(filters, colWidths=[40*mm, 95*mm], repeatRows=1)
            table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#5FA878")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("GRID", (0,0), (-1,-1), .35, colors.HexColor("#DCEEE2")), ("FONTSIZE", (0,0), (-1,-1), 8), ("VALIGN", (0,0), (-1,-1), "TOP")]))
            story.extend([table, Spacer(1, 8)])
        kpis = [["Summary", "Value"]] + [[_pdf_ascii(a), _pdf_ascii(b)] for a, b in export_data.get("kpis") or []]
        table = Table(kpis, colWidths=[55*mm, 50*mm], repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#5FA878")), ("TEXTCOLOR", (0,0), (-1,0), colors.white), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("GRID", (0,0), (-1,-1), .35, colors.HexColor("#DCEEE2")), ("FONTSIZE", (0,0), (-1,-1), 8)]))
        story.extend([table, Spacer(1, 8)])
        if export_data.get("notice"):
            story.extend([Paragraph(_pdf_ascii(export_data["notice"]), styles["UFSub"]), Spacer(1, 8)])
        for idx, report_table in enumerate(export_data.get("tables") or []):
            if idx:
                story.append(PageBreak())
            story.append(Paragraph(_pdf_ascii(report_table.get("title") or "Details"), styles["Heading2"]))
            headers = [_pdf_ascii(x) for x in report_table.get("headers") or []]
            rows = [[_pdf_ascii(cell) for cell in row] for row in report_table.get("rows") or []]
            data = [headers] + rows if headers else rows
            if not data:
                data = [["No records for the selected filters."]]
            col_count = max(len(data[0]), 1)
            available = 270*mm
            col_width = available / col_count
            t = Table(data, colWidths=[col_width]*col_count, repeatRows=1 if headers else 0)
            t.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E4F7EA")) if headers else ("BACKGROUND", (0,0), (-1,-1), colors.white), ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#17252A")), ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("GRID", (0,0), (-1,-1), .3, colors.HexColor("#DCEEE2")), ("FONTSIZE", (0,0), (-1,-1), 6.5), ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 3), ("RIGHTPADDING", (0,0), (-1,-1), 3)]))
            story.append(t)
        doc.build(story)
        return buffer.getvalue()
    except Exception:
        return _build_pdf_fallback(export_data)
