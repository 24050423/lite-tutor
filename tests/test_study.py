import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

from test_references import APP_PATH, LocalAPITest, pdf_bytes


RUBRIC = [
    {"keyword": "stack", "expected": "The newest entry leaves first."},
    {"keyword": "recursion", "expected": "Each pending call stores its own state."},
]


def model_reply(value):
    body = {"choices": [{"message": {"content": json.dumps(value)}}]}
    return SimpleNamespace(json=lambda: body)


class StudyTests(LocalAPITest):
    def create_course(self, name, sources=()):
        result = self.http.post("/courses", json={"name": name, "goal": "Understand stack-based calls",
                                                  "topics": ["stack", "pointers"], "sources": list(sources)}).json()
        self.assertEqual(result["status"], "success", result)
        return result["course"]

    def quiz(self, **payload):
        response = self.http.post("/quiz", json=payload).json()
        self.assertEqual(response["status"], "success", response)
        return response

    def grade(self, quiz, answer, session_id="session"):
        result = self.http.post("/grade", json={"quiz_id": quiz["quiz_id"], "answer": answer,
                                               "session_id": session_id}).json()
        self.assertEqual(result["status"], "success", result)
        return result

    def test_course_references_variant_and_rubric(self):
        for name, marker in (("a.pdf", "Alpha"), ("b.pdf", "Beta"), ("outside.pdf", "Outside")):
            self.upload(name, pdf_bytes(f"{marker} stack and call frames."))
        course = self.create_course("Data structures", ["a.pdf", "b.pdf"])
        other = self.create_course("Systems", ["outside.pdf"])
        topic = course["topics"][0]
        self.assertNotEqual(topic["id"], other["topics"][0]["id"])
        options = {"course_id": course["id"], "topic_id": topic["id"], "n_results": 4}
        forbidden = self.http.post("/quiz", json={**options, "source": "outside.pdf"}).json()
        self.assertEqual(forbidden["status"], "error")

        generated = {"quiz": "Explain call stack entries.", "rubric": RUBRIC, "reference_numbers": [1, 999, True]}
        with patch.object(self.server.httpx, "post", return_value=model_reply(generated)):
            first = self.quiz(**options)
            self.assertEqual({ref["source"] for ref in first["references"]}, {"a.pdf", "b.pdf"})
            self.assertEqual([ref["number"] for ref in first["citations"]], [1])
            # A model that repeats the old question triggers a local alternative.
            variant = self.quiz(retry_quiz_id=first["quiz_id"])
        self.assertEqual(variant["retry_of"], first["quiz_id"])
        self.assertEqual(variant["topic_id"], topic["id"])
        self.assertNotEqual(variant["quiz"], first["quiz"])
        self.assertEqual(variant["generation_method"], "template")

        partial = {"items": [{"id": "r2", "status": "missed", "explanation": "No call state."},
                             {"id": "r1", "status": "partial", "explanation": "Order unspecified."}]}
        with patch.object(self.server.httpx, "post", return_value=model_reply(partial)):
            score = self.grade(first, "Entries are removed")
        self.assertEqual(score["score"], 25)
        self.assertEqual([item["status"] for item in score["items"]], ["partial", "missed"])
        self.assertEqual(score["grading_method"], "semantic")
        report = self.http.get("/learning_stats", params={"course_id": course["id"]}).json()
        self.assertEqual(report["topics"][0]["id"], topic["id"])
        self.assertEqual(report["records"][0]["citations"], first["citations"])
        self.assertEqual(self.http.get("/learning_stats", params={"course_id": other["id"]}).json()["records"], [])

    def test_keyword_timeout_and_duplicate_submission(self):
        with patch.object(self.server.httpx, "post", return_value=model_reply({"quiz": "Explain stack.", "rubric": RUBRIC})):
            quiz = self.quiz(question="stack")
        with patch.object(self.server.httpx, "post", side_effect=self.server.httpx.ReadTimeout("simulated")), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.grade(quiz, "STACK but not call state"), range(2)))
        self.assertEqual(sorted(item["duplicate"] for item in results), [False, True])
        self.assertTrue(all(item["score"] == 50 and item["grading_method"] == "keyword" for item in results))

        with patch.object(self.server, "learning_db", self.server._load_learning_db()), \
             patch.object(self.server.httpx, "post") as model:
            duplicate = self.grade(quiz, "different answer", "another-session")
            model.assert_not_called()
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["answer"], "STACK but not call state")
            self.assertEqual(len(self.server.learning_db["session"]), 1)

    def test_review_state_and_legacy_after_reload(self):
        course = self.create_course("Operating systems")
        primary, secondary = course["topics"]
        path = f"/courses/{course['id']}/review"
        self.assertEqual(sum(task["due_now"] for task in self.http.get(path).json()["tasks"]), 2)
        result = self.http.post(path, json={"topic_id": secondary["id"], "action": "snooze"}).json()
        self.assertEqual(result["status"], "success")
        answer = {"quiz": "Explain newest-first behavior.", "rubric": RUBRIC}
        with patch.object(self.server.httpx, "post", return_value=model_reply(answer)):
            first = self.quiz(course_id=course["id"], topic_id=primary["id"])
            self.assertEqual(first["source_type"], "general")
        with patch.object(self.server.httpx, "post", return_value=model_reply({"items": []})):
            self.assertEqual(self.grade(first, "unrelated")["score"], 0)
        tasks = self.http.get(path).json()["tasks"]
        self.assertEqual(tasks[0]["reason"], "上次练习未通过")
        self.assertFalse(tasks[1]["due_now"])
        self.assertEqual(tasks[1]["reason"], "已稍后复习")

        changed = self.http.post("/courses", json={"course_id": course["id"], "name": course["name"],
                                                   "topics": ["stack", "pointers", "queues"]}).json()["course"]
        self.assertEqual(changed["topics"][0]["id"], primary["id"])
        with patch.object(self.server.httpx, "post", return_value=model_reply({**answer, "quiz": "Give a stack example."})):
            retry = self.quiz(retry_quiz_id=first["quiz_id"])
        with patch.object(self.server.httpx, "post", return_value=model_reply({
                "items": [{"id": "r1", "status": "met", "explanation": "Newest first."},
                          {"id": "r2", "status": "met", "explanation": "Explains separate state."}]})):
            full = self.grade(retry, "The last entry is removed first and each call has its own saved state.")
        self.assertEqual(full["score"], 100)
        self.assertEqual(full["grading_method"], "semantic")
        natural = next(item for item in self.http.get(path).json()["tasks"] if item["id"] == primary["id"])
        self.assertFalse(natural["due_now"])
        self.assertEqual(natural["reason"], "近期有错题，间隔 1 天再练")
        self.assertEqual(self.http.post(path, json={"topic_id": primary["id"], "action": "done"}).json()["status"], "success")
        legacy = {"question": "Legacy question", "result": "校验未通过", "keywords": ["old"], "hits": []}
        self.server._save_learning_db({**self.server.learning_db, "legacy-session": [legacy]})
        with patch.object(self.server, "learning_db", self.server._load_learning_db()):
            tasks = {item["id"]: item for item in self.http.get(path).json()["tasks"]}
            self.assertEqual(tasks[primary["id"]]["reason"], "已手动完成本次复习")
            self.assertEqual(tasks[primary["id"]]["total"], 2)
            self.assertEqual(tasks[primary["id"]]["passed"], 1)
            report = self.http.get("/learning_stats", params={"session_id": "legacy-session"}).json()
            self.assertEqual(report["records"], [legacy])
            self.assertEqual(report["topics"], [])

    def test_review_ui_enters_quiz_and_wrong_answer_retries(self):
        from streamlit.testing.v1 import AppTest

        course = self.create_course("UI course")
        topic = course["topics"][0]

        def get(url, **options):
            response = self.http.get(urlsplit(url).path, params=options.get("params"))
            return SimpleNamespace(ok=response.is_success, json=response.json)

        def post(url, *, json, **options):
            response = self.http.post(urlsplit(url).path, json=json)
            return SimpleNamespace(ok=response.is_success, json=response.json)

        path = shutil.copyfile(APP_PATH, Path.cwd() / "app.py")
        Path("local_secrets.py").write_text('DEEPSEEK_API_KEY = ""\n', encoding="utf-8")
        responses = [model_reply({"quiz": "What happens first?", "rubric": RUBRIC}),
                     model_reply({"items": []}),
                     model_reply({"quiz": "How does the newest call return?", "rubric": RUBRIC})]
        with patch.object(requests, "get", side_effect=get), patch.object(requests, "post", side_effect=post), \
             patch.object(self.server.httpx, "post", side_effect=responses):
            app = AppTest.from_file(str(path)).run()
            app.selectbox(key="study_course").select(course["id"]).run()
            self.assertFalse(app.exception)
            app.button(key=f"plan_practice_{topic['id']}").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.study_tab, "🎯 测验")
            first = app.session_state.active_practice
            self.assertEqual(first["topic_id"], topic["id"])
            app.text_area(key=f"answer_{first['quiz_id']}").set_value("unknown")
            next(button for button in app.button if button.label == "✅ 提交答案").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state.practice_grade["score"], 0)
            app.button(key="retry_1").click().run()
            self.assertFalse(app.exception)
            retry = app.session_state.active_practice
            self.assertEqual(retry["topic_id"], topic["id"])
            self.assertEqual(retry["retry_of"], first["quiz_id"])
