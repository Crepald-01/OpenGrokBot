"""Reading PDFs and spreadsheets, and writing new spreadsheets.

Everything here treats a file as untrusted: sizes, pages, sheets, rows and cells are capped, spreadsheets are opened read-only
with their saved values (formulas are never calculated and macros never run), and a cell that would start a formula in
Excel is written with a leading apostrophe so a Bot cannot plant one. pypdf and openpyxl are imported only when needed, with
a clear message if they are missing."""
from __future__ import annotations

import csv
import io
from pathlib import Path

MAX_PDF_BYTES = 40_000_000
MAX_PDF_PAGES = 400
MAX_XLSX_BYTES = 30_000_000
MAX_SHEETS = 20
MAX_ROWS = 5000
MAX_COLS = 60
MAX_CELL_CHARS = 500
MAX_WRITE_ROWS = 50_000
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class DocumentError(ValueError):
    pass


def _need(module: str, pip: str):
    try:
        return __import__(module)
    except ImportError as e:   # pragma: no cover  (only when the package is missing)
        raise DocumentError(f"Reading this file needs the '{pip}' package, which is not installed. Reinstall OpenGrokBot or run: pip install {pip}") from e


# ----------------------------------------------------------------------------- PDF
def read_pdf(data: bytes, first: int = 1, last: int | None = None) -> tuple[str, dict]:
    """Text of pages first..last (1-based) as '[Page N]' blocks, plus {'pages': total, 'from': a, 'to': b}."""
    if len(data) > MAX_PDF_BYTES:
        raise DocumentError("That PDF is too large (the limit is 40 MB).")
    pypdf = _need("pypdf", "pypdf")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"That does not look like a readable PDF ({type(e).__name__}).") from e
    if reader.is_encrypted:
        try:
            ok = reader.decrypt("")        # many PDFs are 'encrypted' with an empty password only to restrict printing
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            raise DocumentError("That PDF is password protected. Remove the password and add it again.")
    total = len(reader.pages)
    a = max(1, int(first or 1))
    b = min(total, int(last or total), a + MAX_PDF_PAGES - 1)
    blocks, chars = [], 0
    for n in range(a, b + 1):
        try:
            t = (reader.pages[n - 1].extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            t = ""
        chars += len(t)
        blocks.append(f"[Page {n}]\n{t}")
    if chars < 20 * max(1, b - a + 1) and chars < 200:
        raise DocumentError("That PDF has almost no text: it is probably scanned images. Run it through OCR first, then add the result.")
    return "\n\n".join(blocks), {"pages": total, "from": a, "to": b, "truncated": b < total and b - a + 1 >= MAX_PDF_PAGES}


# ----------------------------------------------------------------------------- spreadsheets
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        v = int(v)
    s = str(v).replace("\r\n", " ").replace("\n", " ").replace("\t", " ")
    return s[:MAX_CELL_CHARS] + ("…" if len(s) > MAX_CELL_CHARS else "")


def read_xlsx(data: bytes, sheet: str = "", max_rows: int = 200) -> tuple[str, dict]:
    """The sheets of a workbook as tab-separated tables. `sheet` picks one by name; `max_rows` caps rows per sheet."""
    if len(data) > MAX_XLSX_BYTES:
        raise DocumentError("That workbook is too large (the limit is 30 MB).")
    openpyxl = _need("openpyxl", "openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise DocumentError(f"That does not look like a readable Excel workbook ({type(e).__name__}). Only .xlsx is supported.") from e
    max_rows = max(1, min(MAX_ROWS, int(max_rows or 200)))
    names = wb.sheetnames
    if sheet and sheet not in names:
        wb.close()
        raise DocumentError(f"No sheet called '{sheet}'. The sheets are: {', '.join(names)}.")
    out, info = [], {"sheets": names[:MAX_SHEETS], "rows": {}}
    for name in ([sheet] if sheet else names[:MAX_SHEETS]):
        ws = wb[name]
        rows, total = [], 0
        for row in ws.iter_rows(values_only=True):
            total += 1
            if total <= max_rows:
                rows.append("\t".join(_cell(c) for c in row[:MAX_COLS]).rstrip("\t"))
            if total >= MAX_ROWS * 4:
                break
        while rows and not rows[-1].strip():
            rows.pop()
        info["rows"][name] = total
        more = f"\n[... {total - max_rows} more rows not shown]" if total > max_rows else ""
        out.append(f"[Sheet: {name}]  ({total} rows)\n" + "\n".join(rows) + more)
    wb.close()
    return "\n\n".join(out), info


def read_csv_text(raw: str, max_rows: int = 200) -> str:
    rows = []
    for i, row in enumerate(csv.reader(io.StringIO(raw), dialect=_sniff(raw))):
        if i >= max_rows:
            rows.append("[... more rows not shown]")
            break
        rows.append("\t".join(_cell(c) for c in row[:MAX_COLS]))
    return "\n".join(rows)


def _sniff(raw: str):
    try:
        return csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def safe_cell(v):
    """Numbers stay numbers; text that Excel would treat as a formula gets a leading apostrophe."""
    if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
        return v
    s = str(v)
    if s.startswith(FORMULA_START):
        return "'" + s
    return s[:32_000]


def write_xlsx(sheets: dict[str, list[list]]) -> bytes:
    """A new workbook from {sheet name: rows}. Raises DocumentError for anything out of bounds."""
    openpyxl = _need("openpyxl", "openpyxl")
    if not sheets or not isinstance(sheets, dict):
        raise DocumentError("Give at least one sheet: {\"Sheet1\": [[\"A1\", \"B1\"], [\"A2\", \"B2\"]]}.")
    if len(sheets) > MAX_SHEETS:
        raise DocumentError(f"At most {MAX_SHEETS} sheets.")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        if not isinstance(rows, list):
            raise DocumentError(f"Sheet '{name}' must be a list of rows.")
        if len(rows) > MAX_WRITE_ROWS:
            raise DocumentError(f"Sheet '{name}' has more than {MAX_WRITE_ROWS} rows.")
        title = "".join(ch for ch in str(name) if ch not in "[]:*?/\\")[:31] or "Sheet"
        ws = wb.create_sheet(title)
        for r in rows:
            if not isinstance(r, list):
                raise DocumentError(f"Every row in '{name}' must be a list of cells.")
            ws.append([safe_cell(c) for c in r[:MAX_COLS * 4]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def kind_of(name: str) -> str:
    return {".pdf": "pdf", ".xlsx": "xlsx", ".csv": "csv", ".tsv": "csv"}.get(Path(name).suffix.lower(), "")
