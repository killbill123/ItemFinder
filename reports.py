"""Item-list reading (CSV / Excel) and Excel report export."""
from __future__ import annotations

import csv
import datetime as _dt
import os
import re
from dataclasses import dataclass, field

import engine

CODE_HEADER = re.compile(r"code|sku|item\s*(no|#|num)|article|part\s*(no|#)|barcode|ref", re.I)
DESC_HEADER = re.compile(r"name|desc|title|product|item", re.I)


# =========================================================================== reading
def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))  # Excel stores 1234 as 1234.0
    return str(v).strip()


def read_table(path: str) -> tuple[list[str], list[list[str]]]:
    """Return (headers, rows) from a .csv/.txt or .xlsx/.xlsm file (first sheet)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        raw = [[_cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
        wb.close()
    elif ext == ".xls":
        raise ValueError("Old .xls files aren't supported - in Excel use File > Save As > .xlsx or .csv")
    else:
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                with open(path, "r", encoding=enc, newline="") as f:
                    text = f.read()
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        raw = [[c.strip() for c in r] for r in csv.reader(text.splitlines(), dialect)]
    raw = [r for r in raw if any(c for c in r)]
    if not raw:
        raise ValueError("The file is empty")
    width = max(len(r) for r in raw)
    raw = [r + [""] * (width - len(r)) for r in raw]
    first = raw[0]
    # header row if most cells are non-numeric text
    looks_header = sum(1 for c in first if c and not re.fullmatch(r"[\d.\-]+", c)) >= max(1, len(first) // 2)
    if looks_header:
        headers = [c or f"Column {i + 1}" for i, c in enumerate(first)]
        rows = raw[1:]
    else:
        headers = [f"Column {i + 1}" for i in range(width)]
        rows = raw
    return headers, rows


def guess_columns(headers: list[str], rows: list[list[str]]) -> tuple[int, int | None]:
    """(code column, description column or None)."""
    code_col = next((i for i, h in enumerate(headers) if re.search(r"\bcode\b|sku", h, re.I)), None)
    if code_col is None:
        code_col = next((i for i, h in enumerate(headers) if CODE_HEADER.search(h)), None)
    if code_col is None:  # column whose values most often look like item codes
        sample = rows[:500]
        scores = [sum(1 for r in sample if engine.item_code(r[i] + ".x")) for i in range(len(headers))]
        code_col = max(range(len(headers)), key=lambda i: scores[i]) if headers else 0
    desc_col = next((i for i, h in enumerate(headers) if i != code_col and DESC_HEADER.search(h)), None)
    return code_col, desc_col


# =========================================================================== matching
@dataclass
class ListCheck:
    no_photo: list[list[str]] = field(default_factory=list)      # list rows whose code has no photo
    not_in_list: list[str] = field(default_factory=list)         # photo paths whose code isn't listed
    matched_codes: int = 0
    list_codes: int = 0
    photos_with_code: int = 0
    photos_without_code: int = 0


def check_list(rows: list[list[str]], code_col: int, photo_paths: list[str]) -> ListCheck:
    res = ListCheck()
    list_keys: dict[str, list[str]] = {}
    for r in rows:
        k = engine.code_key(r[code_col]) if code_col < len(r) else None
        if k:
            list_keys.setdefault(k, r)
    res.list_codes = len(list_keys)
    photo_keys: set[str] = set()
    for p in photo_paths:
        k = engine.code_key(engine.item_code(p))
        if not k:
            res.photos_without_code += 1
            continue
        res.photos_with_code += 1
        photo_keys.add(k)
        if k not in list_keys:
            res.not_in_list.append(p)
    res.matched_codes = len(photo_keys & set(list_keys))
    res.no_photo = [r for k, r in list_keys.items() if k not in photo_keys]
    res.not_in_list.sort(key=lambda p: os.path.basename(p).lower())
    return res


# =========================================================================== Excel export
WARN_STATUSES = {"Extra copy", "Looks different", "Not in list", "No photo"}


def file_url(path: str) -> str:
    """Clickable link for a local (C:\\...) or network (\\\\SERVER\\share\\...) file."""
    p = path.replace("\\", "/")
    if p.startswith("//"):
        return "file:" + p            # file://SERVER/share/...
    return "file:///" + p.lstrip("/")  # file:///C:/...


def write_excel(path: str, sheets: list[tuple[str, list[str], list[list]]], path_header: str = "Full path"):
    """sheets: [(title, headers, rows)]. Full-path cells become clickable links."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    head_fill = PatternFill("solid", fgColor="1F6FEB")
    warn_fill = PatternFill("solid", fgColor="FFE3C2")
    band_fill = PatternFill("solid", fgColor="F1F5FB")
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title[:31])
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head_fill
            c.alignment = Alignment(vertical="center")
        status_col = headers.index("Status") if "Status" in headers else None
        group_col = headers.index("Group") if "Group" in headers else None
        link_col = headers.index(path_header) if path_header in headers else None
        for r in rows:
            ws.append(r)
            row = ws.max_row
            if link_col is not None and r[link_col]:
                cell = ws.cell(row=row, column=link_col + 1)
                cell.hyperlink = file_url(str(r[link_col]))
                cell.font = Font(color="1F6FEB", underline="single")
            if status_col is not None and r[status_col] in WARN_STATUSES:
                for c in ws[row]:
                    if c.column != (link_col or -1) + 1:
                        c.fill = warn_fill
            elif group_col is not None and isinstance(r[group_col], int) and r[group_col] % 2 == 0:
                for c in ws[row]:
                    c.fill = band_fill
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:2000] if i - 1 < len(r)])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(8, longest + 2))
    wb.save(path)


def default_report_name(kind: str) -> str:
    return f"ItemFinder {kind} {_dt.datetime.now():%Y-%m-%d %H%M}.xlsx"
