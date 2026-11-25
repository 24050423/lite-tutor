import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from vendor.deeptutor.services.practice.scheduler import schedule

PASS_SCORE = 75
POINTS = {"met": 1.0, "partial": 0.5, "missed": 0.0}


def read_study(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"courses": {}, "quizzes": {}, "review": {}}


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def unique_names(names: list[str]) -> list[str]:
    unique = {}
    for name in names:
        if name.strip():
            unique.setdefault(normalized(name), name.strip())
    return list(unique.values())


def adhoc_topic_id(topic: str, source: str | None) -> str:
    key = json.dumps([source, normalized(topic)], ensure_ascii=False).encode("utf-8")
    return "adhoc-" + hashlib.sha256(key).hexdigest()[:24]


def course_topics(names: list[str], previous: list[dict]) -> list[dict]:
    old = {normalized(topic["name"]): topic["id"] for topic in previous}
    return [{"id": old.get(normalized(name)) or uuid.uuid4().hex, "name": name} for name in unique_names(names)]


def quiz_rubric(reply: dict) -> list[dict]:
    items = reply.get("rubric")
    if not isinstance(items, list) or not items:
        keywords = reply.get("keywords")
        if not isinstance(keywords, list):
            return []
        items = [{"keyword": word, "expected": word} for word in keywords if isinstance(word, str)]
    valid = []
    seen = set()
    for item in items[:8]:
        if not isinstance(item, dict):
            continue
        keyword, expected = item.get("keyword"), item.get("expected")
        if not isinstance(keyword, str) or not isinstance(expected, str):
            continue
        keyword, expected = keyword.strip()[:80], expected.strip()[:400]
        key = normalized(keyword)
        if not key or not expected or key in seen:
            continue
        seen.add(key)
        valid.append({"id": f"r{len(valid) + 1}", "keyword": keyword, "expected": expected})
    return valid


def keyword_rows(answer: str, rubric: list[dict]) -> list[dict]:
    text = normalized(answer)
    rows = []
    for item in rubric:
        term = normalized(item["keyword"])
        if term.isascii():
            found = bool(re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text))
        else:
            found = term in text
        rows.append({**item, "status": "met" if found else "missed",
                     "explanation": "答案中找到了关键词。" if found else "答案中未找到该关键词。"})
    return rows


def summarize(records: list[dict]) -> list[dict]:
    grouped = {}
    for record in records:
        name, topic_id = record.get("topic"), record.get("topic_id")
        if not name or not topic_id:
            continue
        topic = grouped.setdefault(topic_id, {"id": topic_id, "name": name, "total": 0, "passed": 0})
        topic["total"] += 1
        topic["passed"] += record.get("result") == "校验通过"
    return list(grouped.values())


def review_queue(course: dict, attempts: list[dict], progress: dict, now: datetime) -> list[dict]:
    by_topic: dict[str, list[dict]] = {}
    for record in sorted(attempts, key=lambda item: item.get("timestamp", "")):
        by_topic.setdefault(record.get("topic_id"), []).append(record)
    summary = {item["id"]: item for item in summarize(attempts)}
    queue = []
    for topic in course["topics"]:
        topic_id = topic["id"]
        history = by_topic.get(topic_id, [])
        latest = history[-1] if history else {}
        quiz_id = latest.get("quiz_id")
        if not latest:
            due, reason, priority = None, "还没有练习记录", 1
        else:
            state = {}
            for record in history:
                rating = "good" if record["result"] == "校验通过" else "again"
                state = schedule(state, rating, datetime.fromisoformat(record["timestamp"]).timestamp())
            due = datetime.fromtimestamp(state["due_at"])
            if latest["result"] != "校验通过":
                reason, priority = "上次练习未通过，安排短间隔复习", 0
            else:
                reason, priority = f"上次练习通过，间隔 {state['interval_days']:g} 天复习", 2
        marker = progress.get(topic_id) or {}
        if marker.get("at") and marker.get("quiz_id") == quiz_id:
            at = datetime.fromisoformat(marker["at"])
            due = at + timedelta(days=1 if marker["action"] == "snooze" else 3)
            reason = "已稍后复习" if marker["action"] == "snooze" else "已手动完成本次复习"
        queue.append({
            **topic, "due": due.isoformat() if due else None, "due_now": due is None or due <= now,
            "reason": reason, "total": summary.get(topic_id, {}).get("total", 0),
            "passed": summary.get(topic_id, {}).get("passed", 0), "last_quiz_id": quiz_id,
            "last_attempt_at": latest.get("timestamp"), "priority": priority,
        })
    return sorted(queue, key=lambda task: (not task["due_now"], task["priority"], task["due"] or ""))
