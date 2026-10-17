"""2.4: PDFs, Excel and CSV (reading and writing), and the opt-in meaning-based knowledge search."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from core import documents as D  # noqa: E402
from core import knowledge as K  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.tooling import ToolContext  # noqa: E402
from service.server import create_app  # noqa: E402

TOKEN = "t24d"
_N = __import__("itertools").count(1)


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small real PDF: one text object per line, uncompressed, so no extra library is needed to build it."""
    objs: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, lines in enumerate(pages):
        stream = "BT /F1 12 Tf 14 TL 50 740 Td " + " T* ".join(f"({ln.replace('(', '[').replace(')', ']')}) Tj" for ln in lines) + " ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R /Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode())
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offsets:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


class Pdf(unittest.TestCase):
    def test_reads_pages_and_ranges(self):
        pdf = make_pdf([["Quarterly report", "Revenue grew by twelve percent over the previous quarter."], ["Second page about hiring plans and the budget for next year."]])
        text, info = D.read_pdf(pdf)
        self.assertIn("[Page 1]", text)
        self.assertIn("Revenue grew", text)
        self.assertIn("[Page 2]", text)
        self.assertEqual((info["pages"], info["from"], info["to"]), (2, 1, 2))
        text, info = D.read_pdf(pdf, 2, 2)
        self.assertNotIn("Revenue", text)
        self.assertIn("hiring plans", text)

    def test_scanned_encrypted_and_broken_files_say_so(self):
        import pypdf
        w = pypdf.PdfWriter()
        w.add_blank_page(612, 792)
        buf = io.BytesIO()
        w.write(buf)
        with self.assertRaisesRegex(D.DocumentError, "scanned"):
            D.read_pdf(buf.getvalue())
        good = pypdf.PdfReader(io.BytesIO(make_pdf([["A real page with enough words in it to count as text for sure."]])))
        w2 = pypdf.PdfWriter()
        w2.append(good)
        w2.encrypt("secret")
        enc = io.BytesIO()
        w2.write(enc)
        with self.assertRaisesRegex(D.DocumentError, "password"):
            D.read_pdf(enc.getvalue())
        with self.assertRaisesRegex(D.DocumentError, "readable PDF"):
            D.read_pdf(b"this is not a pdf at all")
        with self.assertRaisesRegex(D.DocumentError, "too large"):
            D.read_pdf(b"x" * (D.MAX_PDF_BYTES + 1))

    def test_knowledge_accepts_pdf_and_xlsx(self):
        self.assertIn("Hiring plan", K.extract_text("plan.pdf", make_pdf([["Hiring plan for the next year, with budget and headcount per team."]])))
        wb = D.write_xlsx({"Costs": [["item", "cost"], ["rent", 1200]]})
        self.assertIn("rent\t1200", K.extract_text("costs.xlsx", wb))
        with self.assertRaises(K.KnowledgeError):
            K.extract_text("bad.pdf", b"nope")


class Sheets(unittest.TestCase):
    def test_round_trip_and_formula_text_is_neutralised(self):
        data = D.write_xlsx({"Sales": [["name", "total"], ["Ada", 10], ["=HYPERLINK(\"http://evil\",\"x\")", "+1+1"], ["ok", 2.5]], "Notes": [["hello"]]})
        text, info = D.read_xlsx(data)
        self.assertEqual(info["sheets"], ["Sales", "Notes"])
        self.assertIn("[Sheet: Sales]  (4 rows)", text)
        self.assertIn("Ada\t10", text)
        self.assertIn("ok\t2.5", text)
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data))["Sales"]
        self.assertTrue(str(ws["A3"].value).startswith("'="), "a formula-looking cell is saved as text")
        self.assertNotEqual(ws["A3"].data_type, "f")
        self.assertEqual(ws["B2"].value, 10)

    def test_limits_and_errors(self):
        data = D.write_xlsx({"Big": [[i, i * 2] for i in range(500)]})
        text, info = D.read_xlsx(data, max_rows=10)
        self.assertIn("490 more rows not shown", text)
        self.assertEqual(info["rows"]["Big"], 500)
        with self.assertRaisesRegex(D.DocumentError, "No sheet called"):
            D.read_xlsx(data, sheet="Nope")
        with self.assertRaisesRegex(D.DocumentError, "readable Excel"):
            D.read_xlsx(b"PK not really")
        with self.assertRaises(D.DocumentError):
            D.write_xlsx({})
        with self.assertRaises(D.DocumentError):
            D.write_xlsx({"S": "not rows"})
        with self.assertRaises(D.DocumentError):
            D.write_xlsx({f"s{i}": [] for i in range(D.MAX_SHEETS + 1)})

    def test_csv(self):
        out = D.read_csv_text("a;b;c\n1;2;3\n4;5;6\n", max_rows=2)
        self.assertEqual(out.splitlines()[0], "a\tb\tc")
        self.assertIn("more rows not shown", out)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OPENGROKBOT_HOME"] = tempfile.mkdtemp(prefix="gbtest-d24-")
        cls.eng = Engine()
        cls.eng.settings.set("notifications.toast", False)
        cls.eng.start()
        cls.c = TestClient(create_app(cls.eng, TOKEN))
        cls.h = {"Authorization": f"Bearer {TOKEN}"}

    @classmethod
    def tearDownClass(cls):
        cls.eng.stop()


class Tools(Base):
    def spec(self, name):
        return next(t for t in self.eng.builtin if t.name == name)

    def ctx(self):
        bot = self.eng.bots.create(f"DocBot{next(_N)}", job="x")
        return ToolContext(self.eng, bot, "", "", mock.Mock(stop=threading.Event(), vision=False))

    def test_write_read_overwrite_and_outside_rules(self):
        ctx = self.ctx()
        w, r = self.spec("spreadsheet_write"), self.spec("spreadsheet_read")
        args = {"path": "reports/q1.xlsx", "sheets": {"Q1": [["a", "b"], ["=1+1", 2]]}}
        self.assertIsNone(w.risk(ctx, args), "a new file needs no approval")
        res = w.handler(ctx, args)
        self.assertFalse(res.is_error, res.text)
        self.assertTrue((self.eng.computer.workspace / "reports" / "q1.xlsx").is_file())
        risk = w.risk(ctx, args)
        self.assertEqual(risk.category, "overwrite")                     # replacing a file asks first
        out = w.risk(ctx, {"path": str(Path(tempfile.gettempdir()) / "x.xlsx"), "sheets": {"S": [[1]]}})
        self.assertEqual(out.category, "outside_workspace")
        self.assertTrue(out.never_auto)
        got = r.handler(ctx, {"path": "reports/q1.xlsx"})
        self.assertIn("'=1+1", got.data)                                  # the formula was neutralised on the way in
        self.assertTrue(got.untrusted)
        self.assertTrue(r.read_only)
        bad = w.handler(ctx, {"path": "reports/bad.xlsx", "sheets": {}})
        self.assertTrue(bad.is_error)
        self.assertTrue(self.spec("spreadsheet_read").handler(ctx, {"path": "reports/q1.xlsx", "sheet": "Nope"}).is_error)

    def test_an_overwritten_workbook_is_kept_in_file_history(self):
        ctx = self.ctx()
        w = self.spec("spreadsheet_write")
        w.handler(ctx, {"path": "hist.xlsx", "sheets": {"S": [["v1"]]}})
        w.handler(ctx, {"path": "hist.xlsx", "sheets": {"S": [["v2"]]}})
        vers = self.eng.computer.history.versions("hist.xlsx")
        self.assertEqual(len(vers), 1, "the first workbook was snapshotted before it was replaced")
        self.assertIn("v1", D.read_xlsx(self.eng.computer.history.read(vers[0]["id"]))[0])

    def test_pdf_read_tool(self):
        ctx = self.ctx()
        (self.eng.computer.workspace / "doc.pdf").write_bytes(make_pdf([["Contract terms apply from the first of March, renewing yearly unless cancelled."]]))
        res = self.spec("pdf_read").handler(ctx, {"path": "doc.pdf"})
        self.assertIn("Contract terms", res.data)
        self.assertTrue(res.untrusted)
        (self.eng.computer.workspace / "scan.pdf").write_bytes(b"%PDF-1.4 garbage")
        self.assertTrue(self.spec("pdf_read").handler(ctx, {"path": "scan.pdf"}).is_error)


class EmbedHandler(BaseHTTPRequestHandler):
    calls: list = []
    fail = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        EmbedHandler.calls.append(body)
        if EmbedHandler.fail:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        data = []
        for i, t in enumerate(body["input"]):
            t = t.lower()
            # a tiny fake "meaning" space: pets, money, weather
            vec = [float(any(w in t for w in ("cat", "dog", "kitten", "puppy", "pet"))), float(any(w in t for w in ("invoice", "payment", "money", "price"))),
                   float(any(w in t for w in ("rain", "storm", "weather"))), 0.01]
            data.append({"index": i, "embedding": vec})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": data}).encode())

    def log_message(self, *a):
        pass


class Meaning(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = HTTPServer(("127.0.0.1", 0), EmbedHandler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.eng.settings.set("providers", {"localemb": {"label": "Local embeddings", "kind": "openai", "base_url": f"http://127.0.0.1:{cls.srv.server_port}/v1",
                                                         "model": "x", "needs_key": False}})
        cls.eng.knowledge.add_text("pets", "Our kitten and the puppy love the garden. A cat sleeps all day.")
        cls.eng.knowledge.add_text("finance", "The invoice payment is due in May. The price list changed.")
        cls.eng.knowledge.add_text("weather", "Heavy rain and a storm are forecast for the weekend.")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        super().tearDownClass()

    def enable(self, **kw):
        return self.c.post("/api/knowledge/embeddings", headers=self.h, json={"enabled": True, "profile": "localemb", "model": "m1", "confirm": True, **kw})

    def test_a_off_by_default_and_needs_confirmation(self):
        st = self.c.get("/api/knowledge/embeddings", headers=self.h).json()
        self.assertFalse(st["enabled"])
        self.assertEqual(self.eng.knowledge.search("kitten")[0]["why"], "keyword")
        r = self.c.post("/api/knowledge/embeddings", headers=self.h, json={"enabled": True, "profile": "localemb"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Confirm", r.json()["error"])
        self.assertFalse(self.eng.settings.get("knowledge.embeddings"))

    def test_b_anthropic_and_admin_policy_are_refused(self):
        r = self.c.post("/api/knowledge/embeddings", headers=self.h, json={"enabled": True, "profile": "anthropic", "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no embeddings service", r.json()["error"])
        with mock.patch.object(type(self.eng.admin), "embeddings_allowed", lambda self: False):
            r = self.enable()
            self.assertEqual(r.status_code, 400)
            self.assertIn("administrator", r.json()["error"])
        self.assertFalse(self.eng.settings.get("knowledge.embeddings"))

    def test_c_meaning_finds_what_keywords_miss_and_says_why(self):
        r = self.enable()
        self.assertEqual(r.status_code, 200, r.text)
        self.eng.knowledge.index_pending()
        self.assertEqual(self.eng.knowledge.stats()["vectors"], 3)
        self.assertEqual(self.eng.knowledge.keyword_search("pet"), [], "no passage contains the word 'pet'... ")
        hits = self.eng.knowledge.search("pet animals")
        self.assertEqual(hits[0]["name"], "pets")
        self.assertEqual(hits[0]["why"], "meaning")
        both = self.eng.knowledge.search("kitten")
        self.assertEqual(both[0]["why"], "keyword + meaning")
        self.assertTrue(all(c["model"] == "m1" for c in EmbedHandler.calls))
        self.assertTrue(self.eng.knowledge.search("weather rain")[0]["name"] == "weather")

    def test_d_a_failing_service_falls_back_to_keywords(self):
        self.enable()
        self.eng.knowledge.index_pending()
        EmbedHandler.fail = True
        try:
            hits = self.eng.knowledge.search("kitten")
            self.assertEqual(hits[0]["name"], "pets")
            self.assertEqual(hits[0]["why"], "keyword")
        finally:
            EmbedHandler.fail = False

    def test_e_changing_the_model_reindexes_and_turning_off_deletes_vectors(self):
        self.enable()
        self.eng.knowledge.index_pending()
        self.assertEqual(self.eng.knowledge.stats()["vectors"], 3)
        self.enable(model="m2")
        self.eng.knowledge.index_pending()
        models = {r["model"] for r in self.eng.db.query("SELECT model FROM knowledge_vectors")}
        self.assertEqual(models, {"m2"})
        off = self.c.post("/api/knowledge/embeddings", headers=self.h, json={"enabled": False})
        self.assertEqual(off.status_code, 200)
        self.assertEqual(self.eng.knowledge.stats()["vectors"], 0, "off means nothing derived from the documents is kept")

    def test_f_removing_a_source_removes_its_vectors_and_new_text_is_indexed(self):
        self.enable()
        self.eng.knowledge.index_pending()
        src = next(s for s in self.eng.knowledge.sources() if s["name"] == "weather")
        self.eng.knowledge.remove(src["id"])
        self.assertEqual(self.eng.knowledge.stats()["vectors"], 2)
        self.eng.knowledge.add_text("later", "A new note about the dog.")
        self.assertEqual(self.eng.knowledge.index_pending(), 1)

    def test_g_tokens_cannot_change_the_setting(self):
        from core.apitokens import allowed
        self.assertFalse(allowed("full", "POST", "/api/knowledge/embeddings"))


if __name__ == "__main__":
    unittest.main()
