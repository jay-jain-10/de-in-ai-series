"""Shared offline fixtures: handmade PDFs, fake Anthropic, fake S3, fake Snowflake."""

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.config import PipelineConfig


def make_pdf(pages):
    """Build a minimal valid PDF. Each page is a str (text layer) or None (blank = 'scanned')."""
    objs = []  # list of bytes bodies, 1-indexed
    n = len(pages)
    page_ids = [3 + 2 * i for i in range(n)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = page_ids[i] + 1
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        if text:
            esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = f"BT /F1 10 Tf 50 700 Td ({esc}) Tj ET".encode()
        else:
            stream = b""
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


LONG = "This Agreement is made between the Party and the Vendor and shall be governed by the terms herein. "
PAY_TEXT = "Payment. Client shall pay all undisputed invoices within 30 days of receipt. " + LONG
TERM_TEXT = "Termination. Either party may terminate on 60 days written notice. " + LONG
LIAB_TEXT = "Liability. Liability is capped at fees paid in the prior 12 months. " + LONG
LAW_TEXT = "Governing Law. This Agreement is governed by the laws of Delaware. " + LONG


@pytest.fixture
def digital_pdf():
    return make_pdf([PAY_TEXT, TERM_TEXT, LIAB_TEXT + LAW_TEXT])


@pytest.fixture
def config():
    cfg = PipelineConfig()
    cfg.s3.bucket = "test-bucket"
    cfg.extraction_version = 2.1
    return cfg


def fake_response(text, inp=100, out=50):
    return SimpleNamespace(content=[SimpleNamespace(text=text)],
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out))


class FakeAnthropic:
    """Mock Anthropic client. `responder(model, prompt)` returns reply text (or raises)."""

    def __init__(self, responder):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._responder = responder

    def _create(self, model, max_tokens, messages):
        prompt = messages[0]["content"]
        self.calls.append({"model": model, "prompt": prompt})
        return fake_response(self._responder(model, prompt))


def clause_json(value, conf, page, span):
    return json.dumps({"value": value, "confidence": conf, "page_number": page, "source_span": span})


@pytest.fixture
def fake_s3():
    s3 = MagicMock()
    s3.store = {}
    s3.put_object.side_effect = lambda Bucket, Key, Body, **kw: s3.store.__setitem__(Key, Body)
    return s3


class FakeCursor:
    def __init__(self, log, columns=()):
        self.log, self.columns = log, columns
        self._rows = []

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
        if "information_schema.columns" in sql:
            self._rows = [(c,) for c in self.columns]

    def executemany(self, sql, rows):
        self.log.append(("executemany", sql, rows))

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, columns=()):
        self.log, self.columns = [], columns
        self.committed = self.rolled_back = False

    def cursor(self):
        return FakeCursor(self.log, self.columns)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass
