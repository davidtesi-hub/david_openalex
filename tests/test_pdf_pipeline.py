"""Pure routing, source selection and score checks for PDF evaluation."""
import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# The candidate and parser checks do not require downloading or loading a model.
sys.modules.setdefault("fitz", types.SimpleNamespace(open=None))
sys.modules.setdefault("llama_cpp", types.SimpleNamespace(Llama=object))
sys.modules.setdefault("huggingface_hub", types.SimpleNamespace(hf_hub_download=None))

from pdf_weekday import group_for_timestamp, qwen_pdf_db
from phase3_policy import parse_response
from review_openalex_pdfs import pdf_locations, first_readable_pdf, split_answers
from unittest.mock import patch


class PdfPipelineTests(unittest.TestCase):
    def test_weekday_routes_publication_date(self):
        self.assertEqual(group_for_timestamp("2026-11-09"), "monday")
        self.assertEqual(group_for_timestamp("2026-11-10"), "tuesday")
        self.assertEqual(group_for_timestamp("2026-11-15"), "weekend")

    def test_pdf_prefers_primary_then_best_then_other(self):
        published = {"pdf_url": "https://publisher/paper.pdf", "is_oa": True,
                     "version": "publishedVersion", "source": {"display_name": "Journal"}}
        preprint = {"pdf_url": "https://arxiv.org/pdf/1234.5678", "is_oa": True,
                    "version": "submittedVersion"}
        self.assertEqual(pdf_locations({"primary_location": published, "locations": [preprint]})[0][1],
                         "publishedVersion")
        self.assertEqual(pdf_locations({"primary_location": {"pdf_url": None},
                                        "best_oa_location": preprint})[0][1], "submittedVersion")
        self.assertEqual(pdf_locations({"locations": [{"pdf_url": "https://closed/paper.pdf",
                                                        "is_oa": False}]}), [])

    def test_unreadable_primary_falls_back_to_preprint(self):
        locations = [("https://publisher/paper.pdf", "publishedVersion", "Journal"),
                     ("https://arxiv.org/pdf/1234.5678", "submittedVersion", "arXiv")]
        def fake_fetch(url):
            if "publisher" in url:
                raise ValueError("Downloaded content is not a PDF")
            return b"%PDF-test"
        with patch("review_openalex_pdfs.fetch_pdf", side_effect=fake_fetch):
            chosen = first_readable_pdf(locations)
        self.assertEqual(chosen[1], "submittedVersion")
        self.assertEqual(chosen[3], b"%PDF-test")

    def test_six_answers_and_phase3_score(self):
        raw = "\n".join(f"{i}. Answer {i}" for i in range(1, 7))
        self.assertEqual(len(split_answers(raw)), 6)
        choices, score = parse_response("Q1=C\nQ2=C\nQ3=C\nQ4=C\nQ5=C\nQ6=C", "q1_no_u")
        self.assertEqual(choices, tuple("CCCCCC"))
        self.assertEqual(score, 667)


if __name__ == "__main__":
    unittest.main()
