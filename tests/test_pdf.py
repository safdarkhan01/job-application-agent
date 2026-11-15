from pathlib import Path
import tempfile
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from job_agent.resume import read_resume


class PDFTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "synthetic.pdf"

    def writer(self, text=True):
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
            })
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 50 700 Td (Synthetic Software Engineer with Python and SQL experience.) Tj ET")
            page[NameObject("/Contents")] = stream
        return writer

    def test_text_pdf_extracts_only_evidenced_skills_and_title(self):
        self.writer().write(self.path)
        imported = read_resume(self.path)
        self.assertIn("Software Engineer", imported.titles)
        self.assertEqual(["Python", "SQL"], imported.skills)

    def test_scanned_or_blank_pdf_has_actionable_ocr_error(self):
        self.writer(text=False).write(self.path)
        with self.assertRaisesRegex(ValueError, "OCR"):
            read_resume(self.path)

    def test_password_pdf_is_explicitly_unsupported(self):
        writer = self.writer()
        writer.encrypt("synthetic-test-password")
        writer.write(self.path)
        with self.assertRaisesRegex(ValueError, "Password-protected"):
            read_resume(self.path)

    def test_invalid_pdf_has_explicit_failure(self):
        self.path.write_bytes(b"synthetic invalid PDF")
        with self.assertRaisesRegex(ValueError, "PDF parsing failed"):
            read_resume(self.path)
