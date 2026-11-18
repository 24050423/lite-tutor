import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import fitz
import requests
from docx import Document
from fastapi.testclient import TestClient

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def pdf_bytes(*texts: str) -> bytes:
    with fitz.open() as pdf:
        for text in texts:
            pdf.new_page().insert_text((72, 72), text)
        return pdf.tobytes()


class LocalAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with ExitStack() as stack:
            scratch = stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))
            stack.callback(os.chdir, Path.cwd())
            os.chdir(scratch)
            stack.enter_context(patch.dict(os.environ, {"TUTOR_OFFLINE": "1"}))
            original = sys.modules.get("local_secrets")
            sys.modules["local_secrets"] = SimpleNamespace(DEEPSEEK_API_KEY="")
            try:
                import server
            finally:
                if original is None:
                    sys.modules.pop("local_secrets", None)
                else:
                    sys.modules["local_secrets"] = original

        cls.server = server

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        scratch = stack.enter_context(tempfile.TemporaryDirectory(ignore_cleanup_errors=True))
        stack.callback(os.chdir, Path.cwd())
        os.chdir(scratch)
        stack.enter_context(patch.dict(os.environ, {"TUTOR_OFFLINE": "1", "RAG_CONF_THRESHOLD": "0.35"}))
        self.kb = self.server.LocalRAGKnowledgeBase()
        stack.enter_context(patch.object(self.server, "rag_db", self.kb))
        stack.enter_context(patch.object(self.server, "chat_db", {}))
        stack.enter_context(patch.object(self.server, "learning_db", {}))
        self.http = stack.enter_context(TestClient(self.server.app))

    def upload(self, name, contents):
        return self.http.post("/upload_doc", files={"file": (name, contents)}).json()

    def search(self, query, **options):
        return self.http.post("/search", json={"query": query, **options}).json()


class ReferenceTests(LocalAPITest):
    def test_distinct_page_references_and_history(self):
        first = self.upload("first.pdf", pdf_bytes("Intro A", "Shared identifier 2.6.8"))
        second = self.upload("second.pdf", pdf_bytes("Intro B", "Shared identifier 2.6.8"))
        docs = [result["document"]["doc_id"] for result in (first, second)]
        result = self.search("2.6.8", n_results=4)
        refs = [ref for ref in result["references"] if ref["page"] == 2]
        self.assertEqual({ref["doc_id"] for ref in refs}, set(docs))
        self.assertEqual(len({ref["chunk_id"] for ref in refs}), 2)
        scoped = self.search("2.6.8", source="second.pdf")
        self.assertTrue(scoped["references"])
        self.assertEqual({ref["doc_id"] for ref in scoped["references"]}, {docs[1]})
        self.assertEqual(self.search("2.6.8", source="absent.pdf")["retrieval_status"], "no_match")

        for ref in refs:
            response = self.http.get(f"/document/{ref['doc_id']}/preview?page=2")
            self.assertEqual(response.status_code, 200)
            with fitz.open(stream=response.content, filetype="pdf") as preview:
                self.assertEqual(preview.page_count, 1)
                self.assertIn("2.6.8", preview[0].get_text())
        self.assertEqual(self.http.get(f"/document/{docs[0]}/preview?page=3").status_code, 404)
        self.assertEqual(self.http.get(f"/document/{docs[0]}/preview?page=0").status_code, 422)

        reply = {"role": "assistant", "content": "[出处1]", "citations": refs,
                 "retrieval_note": "test", "tool_calls": ["do not store"]}
        legacy = {"role": "user", "content": "Original message"}
        response = self.http.post("/chat_history", json={"session_id": "history", "messages": [legacy, reply]})
        self.assertEqual(response.json()["count"], 2)
        with patch.object(self.server, "chat_db", self.server._load_chat_db()):
            stored = self.http.get("/chat_history", params={"session_id": "history"}).json()["messages"]
        self.assertEqual(stored, [legacy, {key: value for key, value in reply.items() if key != "tool_calls"}])

    def test_failed_replacement_keeps_published_version(self):
        filename = "lecture.pdf"
        original = self.upload(filename, pdf_bytes("originalidentifier 2.6.8"))["document"]
        original_id = original["doc_id"]
        self.assertEqual(self.upload(filename, b"bad pdf")["status"], "error")

        write_chunks = self.kb.add_document_version

        def fail_after_write(*args):
            write_chunks(*args)
            raise RuntimeError("interrupt indexing")

        # Even if cleanup fails, filters prevent an unpublished version from being retrieved.
        with patch.object(self.kb, "add_document_version", side_effect=fail_after_write), \
             patch.object(self.kb, "delete_document_version", side_effect=RuntimeError):
            failed = self.upload(filename, pdf_bytes("temporaryidentifier 6.8.2"))
        self.assertEqual(failed["status"], "error")
        self.assertEqual(self.http.get(f"/document/{original_id}/preview").status_code, 200)
        result = self.search("originalidentifier", source=filename, n_results=10)
        self.assertEqual({ref["doc_id"] for ref in result["references"]}, {original_id})
        self.assertNotIn("temporaryidentifier", result["context"])

        replacement = self.upload(filename, pdf_bytes("replacementidentifier 6.8.2"))["document"]
        self.assertNotEqual(replacement["doc_id"], original_id)
        self.assertEqual(self.http.get(f"/document/{original_id}/preview").status_code, 404)
        result = self.search("replacementidentifier", source=filename, n_results=10)
        self.assertEqual({ref["doc_id"] for ref in result["references"]}, {replacement["doc_id"]})
        self.assertNotIn("originalidentifier", result["context"])
        self.assertEqual(self.http.delete(f"/document/{replacement['doc_id']}").json()["status"], "success")
        self.assertEqual(self.search("replacementidentifier")["retrieval_status"], "empty")

    def test_legacy_source_scope_and_keyword_only(self):
        self.kb.collection.add(ids=["legacy"], documents=["【第5页】\nLegacy recursion material."],
                               metadatas=[{"source": "legacy.txt"}])
        encoded = self.search("recursion", source="legacy.txt")["references"][0]
        self.assertIsNone(encoded["doc_id"])
        self.assertIsNone(encoded["page"])
        self.assertEqual(encoded["source"], "legacy.txt")

        with patch.object(self.kb.collection, "query", side_effect=RuntimeError):
            result = self.search("recursion", source="legacy.txt", mode="vector")
            self.assertTrue(result["keyword_only"])
            self.assertFalse(result["fallback_required"])
            self.assertEqual(self.search("xyzunrelated")["retrieval_status"], "no_match")

    def test_unpaged_word_and_scoped_quiz(self):
        from io import BytesIO

        stream = BytesIO()
        word = Document()
        word.add_paragraph("Linked list contains nodes connected by pointers.")
        word.save(stream)
        with patch.object(self.server.doc_ingest, "convert_to_pdf", return_value=(False, "Office absent")):
            doc = self.upload("notes.docx", stream.getvalue())["document"]
        with patch.object(self.server.httpx, "post") as model:
            answer = json.dumps({"quiz": "Explain pointers.", "keywords": ["nodes"]})
            model.return_value.json.return_value = {"choices": [{"message": {"content": answer}}]}
            found = self.http.post("/quiz", json={"question": "pointers", "source": "notes.docx"}).json()
            self.assertEqual(found["status"], "success")
            self.assertIn("Linked list", model.call_args.kwargs["json"]["messages"][0]["content"])
            self.assertEqual(found["references"][0]["doc_id"], doc["doc_id"])
            self.assertIsNone(found["references"][0]["page"])
            missing = self.http.post("/quiz", json={"question": "pointers", "source": "absent.pdf"}).json()
            self.assertEqual(missing["references"], [])
            self.assertEqual(missing["context"], "")

    def test_existing_embedding_with_wrong_dimensions_stays_readable(self):
        from rag_builder import HashEmbeddingFunction

        old = self.kb.client.create_collection("legacy_vectors", embedding_function=HashEmbeddingFunction(64))
        old.add(ids=["one"], documents=["recursive call uses the stack"], metadatas=[{"source": "old"}])
        recovered = self.server.LocalRAGKnowledgeBase(collection_name="legacy_vectors")
        self.assertIsNone(recovered.embedding_fn)
        result = recovered.search_hits("recursive")
        self.assertEqual([hit.chunk_id for hit in result.hits], ["one"])
        self.assertTrue(result.keyword_only)

    def test_chat_scope_citation_and_replacement_ui(self):
        from streamlit.testing.v1 import AppTest

        self.upload("first.pdf", pdf_bytes("Shared topic 2.6.8 uses the first definition."))
        doc = self.upload("second.pdf", pdf_bytes("Shared topic 2.6.8 uses the second definition."))["document"]
        sent = []

        def get(url, **options):
            response = self.http.get(urlsplit(url).path, params=options.get("params"))
            return SimpleNamespace(ok=response.is_success, json=response.json)

        def post(url, *, json, **options):
            path = urlsplit(url).path
            if path.endswith("/chat/completions"):
                sent.append(json["messages"])
                model = {"choices": [{"message": {"content": "The definition is here.【出处1】【出处99】"}}]}
                return SimpleNamespace(ok=True, json=lambda: model)
            response = self.http.post(path, json=json)
            return SimpleNamespace(ok=response.is_success, json=response.json)

        # Use an app copy so the test has independent settings and no private configuration.
        path = shutil.copyfile(APP_PATH, Path.cwd() / "app.py")
        Path("local_secrets.py").write_text('DEEPSEEK_API_KEY = ""\n', encoding="utf-8")
        with patch.object(requests, "get", side_effect=get), patch.object(requests, "post", side_effect=post), \
             patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-key-for-tests"}):
            app = AppTest.from_file(str(path)).run()
            self.assertFalse(app.exception)
            app.selectbox(key="chat_scope_input").select("second.pdf").run()
            app.chat_input[0].set_value("2.6.8").run()
            self.assertFalse(app.exception)
            reply = app.session_state.messages[-1]
            self.assertEqual(reply["citations"][0]["doc_id"], doc["doc_id"])
            self.assertNotIn("出处99", reply["content"])
            self.assertIn("second definition", str(sent[-1]))
            self.assertNotIn("first definition", str(sent[-1]))
            self.assertTrue(any(f"/{doc['doc_id']}/preview?page=1" in link.proto.url
                                for link in app.get("link_button")))

            self.upload("second.pdf", pdf_bytes("The second definition has been revised."))
            app.run()
            self.assertFalse(app.exception)
            self.assertTrue(any("已被替换或删除" in caption.value for caption in app.caption))
            self.assertFalse(any(f"/{doc['doc_id']}/" in link.proto.url for link in app.get("link_button")))


if __name__ == "__main__":
    unittest.main()
