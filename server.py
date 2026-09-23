import os
import asyncio
import subprocess
import re
import uuid
from collections import Counter
from typing import Optional, Dict, Any, List
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import uvicorn
import json
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, UploadFile, File
import fitz  # pymupdf


import doc_ingest
from rag_builder import LocalRAGKnowledgeBase
from edge_tool import get_tool_schemas

class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

app = FastAPI(title="智能学习平台 Edge Node", default_response_class=UTF8JSONResponse)

def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-zA-Z0-9\u4e00-\u9fff]+", text.lower()) if len(t) > 1]

def _extract_keywords(text: str, limit: int = 4) -> List[str]:
    tokens = _tokenize(text)
    if not tokens:
        return []
    counts = Counter(tokens)
    return [w for w, _ in counts.most_common(limit)]

def _build_quiz(question: str, context: Optional[str] = None, n_results: int = 2,
                source: Optional[str] = None):
    working_context = context.strip() if context else ""
    if not working_context:
        working_context = rag_db.query_knowledge_hybrid(question, n_results=n_results,
                                                        source=source)

    # 用LLM生成高质量题目
    try:
        prompt = f"""你是一名出题老师，请根据以下知识点内容，针对主题「{question}」出一道简答题。

知识点内容：
{working_context[:3000] if working_context else "无背景知识，请根据主题直接出题"}

要求：
1. 题目清晰、有具体考察点
2. 难度适中，适合大学生
3. 同时给出3-5个评分关键词

请用JSON格式回复：
{{
  "quiz": "题目内容",
  "keywords": ["关键词1", "关键词2", "关键词3"]
}}"""

        resp = httpx.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 300,
                "response_format": {"type": "json_object"}
            },
            timeout=20
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        quiz = parsed.get("quiz", "")
        keywords = parsed.get("keywords", [])
        if quiz and keywords:
            return working_context, keywords, quiz
    except Exception as e:
        print(f"[QUIZ] LLM出题失败，降级到词频：{e}")
    
    # 降级到原来的词频方案
    keywords = _extract_keywords(working_context)
    if keywords:
        quiz = "请用一句话解释以下关键词并至少覆盖其中两个：" + "、".join(keywords)
    else:
        quiz = f"请简述你对「{question}」的理解。"
    return working_context, keywords, quiz

import httpx

# 与 app.py 同一套取法：环境变量 > local_secrets.py（本机私有，不进版本库）。
try:
    from local_secrets import DEEPSEEK_API_KEY as _LOCAL_API_KEY
except ImportError:
    _LOCAL_API_KEY = ""

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") or _LOCAL_API_KEY
DEEPSEEK_BASE = "https://api.deepseek.com/v1"

def _grade_answer(answer: str, keywords: list, min_hit: int = 1):
    """保留原接口兼容性"""
    hits = [k for k in (keywords or []) if k in answer]
    result = "校验通过" if len(hits) >= min_hit else "校验未通过"
    return result, hits

# ✅ LLM语义评分（主力）
def _grade_answer_llm(question: str, answer: str, keywords: list) -> tuple:
    kw_str = "、".join(keywords) if keywords else "无"
    prompt = f"""你是一名严格但公正的老师，请判断学生的回答是否正确。

题目：{question}
参考知识点关键词：{kw_str}
学生回答：{answer}

请用JSON格式回复，不要有其他内容：
{{
  "passed": true或false,
  "matched_concepts": ["命中的概念1", "命中的概念2"],
  "feedback": "一句话点评，指出对的地方和缺少的地方"
}}"""
    try:
        resp = httpx.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 300,
                "response_format": {"type": "json_object"}
            },
            timeout=20
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        passed = parsed.get("passed", False)
        hits = parsed.get("matched_concepts", [])
        feedback = parsed.get("feedback", "")
        result = "校验通过" if passed else "校验未通过"
        return result, hits, feedback
    except Exception as e:
        # 降级到关键词匹配
        hits = [k for k in (keywords or []) if k in answer.lower()]
        result = "校验通过" if hits else "校验未通过"
        return result, hits, f"（AI评分暂时不可用，已降级到关键词匹配）"


# ✅ 保留原版作为降级备用（改名加下划线前缀）
def _grade_answer(answer: str, keywords: Optional[List[str]] = None, min_hit: int = 1):
    clean_answer = answer.strip()
    lowered = clean_answer.lower()
    keyword_list = keywords or []
    hits = [k for k in keyword_list if k in lowered]
    threshold = max(1, min_hit)
    if keyword_list and len(hits) >= threshold:
        result = "校验通过"
    elif not keyword_list and len(clean_answer) >= 6:
        result = "校验通过"
    else:
        result = "校验未通过"
    return result, hits


LEARNING_DB_PATH = Path("learning_db.json")
tutor_sessions: Dict[str, Dict[str, Any]] = {}

def _load_learning_db() -> dict:
    if LEARNING_DB_PATH.exists():
        try:
            return json.loads(LEARNING_DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_learning_db(db: dict):
    LEARNING_DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8"
    )

learning_db = _load_learning_db()

# 聊天记录独立存放，避免破坏 learning_db.json 既有的 {会话: [答题记录]} 结构
CHAT_DB_PATH = Path("chat_db.json")

def _load_chat_db() -> dict:
    if CHAT_DB_PATH.exists():
        try:
            return json.loads(CHAT_DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_chat_db(db: dict):
    CHAT_DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8"
    )

chat_db = _load_chat_db()

print("Waking up Right Brain (ChromaDB)...")
rag_db = LocalRAGKnowledgeBase()

# 启动时自动导入本地知识文件
import pathlib
_auto_files = ["data_structure_notes.txt", "math.md"]
for _f in _auto_files:
    _path = pathlib.Path(_f)
    if _path.exists():
        try:
            # 尝试utf-8，失败则用gbk
            try:
                _text = _path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                _text = _path.read_text(encoding="gbk")
            rag_db.add_document(_text, source=_f)
            print(f"[AUTO-IMPORT] {_f} 已导入知识库")
        except Exception as _e:
            print(f"[AUTO-IMPORT] {_f} 导入失败：{_e}")

class TaskRequest(BaseModel):
    task_instruction: Optional[str] = None
    code: Optional[str] = None
    language: str = "python"
    timeout: int = 20

@app.post("/solve")
async def receive_task(request: TaskRequest):
    if request.code and request.code.strip():
        if request.language.lower() != "python":
            return {"status": "error", "message": "Only python is supported for code execution."}
        try:
            result = subprocess.run(
                ["python", "-c", request.code],
                capture_output=True,
                text=True,
                timeout=request.timeout
            )
            return {
                "status": "success" if result.returncode == 0 else "failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    if request.task_instruction and request.task_instruction.strip():
        instruction = request.task_instruction
        print("\n" + "="*60)
        print(f"[TASK RECEIVED] Instruction: {instruction}")
        try:
            print("Waking up OpenCode Native UI (Fire-and-Forget Mode)...")
            full_command = f'opencode --prompt "{instruction}"'
            subprocess.Popen(full_command, shell=True)
            return {
                "status": "success", 
                "solution": "[Task dispatched successfully. Execution output is rendering natively on the Edge Node physical screen.]"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return {"status": "error", "message": "Either task_instruction or code must be provided."}

class SearchRequest(BaseModel):
    query: str
    mode: str = "hybrid"
    n_results: int = 2

@app.post("/search")
async def search_knowledge(req: SearchRequest):
    print("\n" + "="*60)
    print(f"[SEARCH RECEIVED] Query: {req.query}")
    try:
        documents, distances = rag_db.query_knowledge_chunks_with_scores(req.query, n_results=req.n_results)
        confidence = 0.0
        if distances:
            confidence = 1.0 / (1.0 + distances[0])
        threshold = float(os.getenv("RAG_CONF_THRESHOLD", "0.35"))
        fallback_required = (not documents) or (confidence < threshold)
        if fallback_required:
            return {
                "status": "success",
                "context": "",
                "fallback_required": True,
                "confidence": confidence
            }
        if req.mode.lower() == "vector":
            retrieved_context = "\n---\n".join(documents)
        else:
            retrieved_context = rag_db.query_knowledge_hybrid(req.query, n_results=req.n_results)
        print(f"[SEARCH RESULT] Found {len(retrieved_context)} characters of context.")
        return {
            "status": "success", 
            "context": retrieved_context,
            "fallback_required": False,
            "confidence": confidence
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

class QuizRequest(BaseModel):
    question: str
    context: Optional[str] = None
    n_results: int = 2
    source: Optional[str] = None   # 只从这份资料里出题；不给就搜整个知识库

@app.post("/quiz")
async def generate_quiz(req: QuizRequest):
    try:
        context, keywords, quiz = _build_quiz(req.question, req.context, req.n_results,
                                              req.source)
        return {
            "status": "success",
            "quiz": quiz,
            "keywords": keywords,
            "context": context
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

class GradeRequest(BaseModel):
    answer: str
    keywords: Optional[List[str]] = None
    min_hit: int = 1
    session_id: Optional[str] = "default"
    question: Optional[str] = ""

@app.post("/grade")
async def grade_answer(req: GradeRequest):
    try:
        result, hits, feedback = _grade_answer_llm(
            question=req.question or "",
            answer=req.answer,
            keywords=req.keywords or []
        )
        record = {
            "timestamp": datetime.now().isoformat(),
            "question": req.question or "",
            "keywords": req.keywords or [],
            "answer": req.answer,
            "result": result,
            "hits": hits,
            "feedback": feedback
        }
        sid = req.session_id or "default"
        if sid not in learning_db:
            learning_db[sid] = []
        learning_db[sid].append(record)
        _save_learning_db(learning_db)
        return {"status": "success", "result": result, "matched_keywords": hits, "feedback": feedback}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/tools")
async def list_tools():
    public_url = os.getenv("EDGE_PUBLIC_URL", "http://127.0.0.1:8000").strip()
    tools = get_tool_schemas(public_url)
    return {"status": "success", "tools": tools}

class TutorRequest(BaseModel):
    session_id: Optional[str] = None
    user_input: str

@app.post("/tutor")
async def tutor_fsm(req: TutorRequest):
    session_id = req.session_id or str(uuid.uuid4())
    state = tutor_sessions.get(session_id)
    if not state:
        state = {
            "stage": "diagnose",
            "question": req.user_input.strip(),
            "context": "",
            "keywords": []
        }
        tutor_sessions[session_id] = state

    stage = state["stage"]
    if stage == "diagnose":
        response = (
            f"我先做诊断：你正在处理的问题是「{state['question']}」。"
            "请补充你目前的解题进度或卡住点。"
        )
        state["stage"] = "explain"
        return {"status": "success", "session_id": session_id, "stage": "diagnose", "response": response}

    if stage == "explain":
        context, keywords, _ = _build_quiz(state["question"], None, 2)
        state["context"] = context
        state["keywords"] = keywords
        response = (
            "启发讲解如下：\n\n"
            f"{context}\n\n"
            "如果理解了，请回答“继续测验”。"
        )
        state["stage"] = "quiz"
        return {"status": "success", "session_id": session_id, "stage": "explain", "response": response}

    if stage == "quiz":
        _, keywords, prompt = _build_quiz(state["question"], state.get("context", ""), 2)
        state["keywords"] = keywords
        state["stage"] = "validate"
        return {"status": "success", "session_id": session_id, "stage": "quiz", "response": prompt}

    if stage == "validate":
        keywords = state.get("keywords", [])
        answer = req.user_input.strip()
        result, hits, feedback = _grade_answer_llm(
            question=state.get("question", ""),
            answer=answer,
            keywords=keywords
        )
        # 记录到学习数据库
        record = {
            "timestamp": datetime.now().isoformat(),
            "question": state.get("question", ""),
            "keywords": keywords,
            "answer": answer,
            "result": result,
            "hits": hits,
            "feedback": feedback
        }
        sid = session_id
        if sid not in learning_db:
            learning_db[sid] = []
        learning_db[sid].append(record)
        _save_learning_db(learning_db)
        response = f"{result}。{feedback}\n\n如果需要，我可以继续补充讲解或出新题。"
        state["stage"] = "complete"
        return {"status": "success", "session_id": session_id, "stage": "validate", "response": response, "matched_keywords": hits}

    response = "本轮已完成。如需继续，请提交新问题。"
    return {"status": "success", "session_id": session_id, "stage": "complete", "response": response}

@app.get("/learning_stats")
async def get_learning_stats(session_id: str = "default"):
    records = learning_db.get(session_id, [])
    return {"status": "success", "records": records}

@app.get("/all_sessions")
async def get_all_sessions():
    return {"status": "success", "sessions": list(learning_db.keys())}


class ChatHistoryRequest(BaseModel):
    session_id: str
    messages: List[Dict[str, Any]]

@app.post("/chat_history")
async def save_chat_history(req: ChatHistoryRequest):
    """整段覆盖式保存某个会话的聊天记录。"""
    sid = (req.session_id or "").strip()
    if not sid:
        return {"status": "error", "message": "session_id 不能为空"}
    # 只落盘 role / content，避免把工具调用等内部结构写进磁盘
    cleaned = [
        {"role": str(m.get("role", "")), "content": m.get("content", "")}
        for m in req.messages
        if m.get("role") and isinstance(m.get("content"), str)
    ]
    title = ""
    for m in cleaned:
        if m["role"] == "user":
            title = m["content"].strip().replace("\n", " ")[:40]
            break
    chat_db[sid] = {
        "title": title,
        "updated_at": datetime.now().isoformat(),
        "messages": cleaned,
    }
    _save_chat_db(chat_db)
    return {"status": "success", "count": len(cleaned)}


@app.get("/chat_history")
async def get_chat_history(session_id: str = ""):
    entry = chat_db.get(session_id)
    if not entry:
        return {"status": "success", "messages": [], "title": "", "updated_at": ""}
    return {
        "status": "success",
        "messages": entry.get("messages", []),
        "title": entry.get("title", ""),
        "updated_at": entry.get("updated_at", ""),
    }


@app.get("/sessions")
async def list_sessions():
    """合并聊天记录与答题记录，按最近活动时间倒序返回。"""
    merged: Dict[str, Dict[str, Any]] = {}

    for sid, entry in chat_db.items():
        merged[sid] = {
            "session_id": sid,
            "title": entry.get("title") or "",
            "updated_at": entry.get("updated_at", ""),
            "message_count": len(entry.get("messages", [])),
            "record_count": len(learning_db.get(sid, [])),
        }

    for sid, records in learning_db.items():
        if sid in merged:
            continue
        title = ""
        last_ts = ""
        for r in records:
            if not title and r.get("question"):
                title = str(r["question"])[:40]
            if r.get("timestamp"):
                last_ts = r["timestamp"]
        merged[sid] = {
            "session_id": sid,
            "title": title,
            "updated_at": last_ts,
            "message_count": 0,
            "record_count": len(records),
        }

    sessions = [
        s for s in merged.values()
        if s["message_count"] or s["record_count"]
    ]
    sessions.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return {"status": "success", "sessions": sessions}


@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    """删除指定会话——对话记录和答题记录一并清掉，其他会话不受影响。"""
    sid = (session_id or "").strip()
    if not sid:
        return {"status": "error", "message": "session_id 不能为空"}

    removed_chat = chat_db.pop(sid, None)
    message_count = len(removed_chat.get("messages", [])) if removed_chat else 0
    removed_records = learning_db.pop(sid, None)
    record_count = len(removed_records) if removed_records else 0

    if not message_count and not record_count:
        return {"status": "error", "message": f"未找到会话 {sid}"}

    if removed_chat is not None:
        _save_chat_db(chat_db)
    if removed_records is not None:
        _save_learning_db(learning_db)
    return {
        "status": "success",
        "message": f"已删除会话 {sid}（对话 {message_count} 条、答题 {record_count} 条）",
        "messages_removed": message_count,
        "records_removed": record_count,
    }

@app.post("/upload_doc")
async def upload_doc(file: UploadFile = File(...)):
    """上传资料（PDF / Word / PPT）：落盘、转 PDF 供在线查看、抽正文入知识库。

    转换会调本机 Office，可能是本进程里最慢的一步，所以丢到线程里跑，别卡住事件循环。
    """
    filename = (file.filename or "").strip()
    if not filename:
        return {"status": "error", "message": "文件名为空"}
    try:
        contents = await file.read()
        if not contents:
            return {"status": "error", "message": "文件内容为空"}
        result = await asyncio.to_thread(doc_ingest.ingest, filename, contents, rag_db)
        if result.get("status") == "success":
            d = result["document"]
            bits = []
            if d.get("pages"):
                bits.append(f"{d['pages']} 页")
            bits.append(f"{d['chars']} 字")
            if d.get("indexed"):
                bits.append("已入知识库")
            else:
                bits.append("未入库（没抽到正文）")
            result["message"] = f"《{filename}》导入成功：" + "、".join(bits)
            if not d.get("previewable"):
                result["message"] += f"；在线查看不可用——{d.get('preview_note')}"
        return result
    except Exception as e:
        return {"status": "error", "message": f"导入失败：{e}"}


@app.get("/documents")
async def list_documents():
    """资料库列表，含每份资料能否在线查看。"""
    try:
        return {"status": "success", "documents": doc_ingest.list_all(rag_db)}
    except Exception as e:
        return {"status": "error", "message": str(e), "documents": []}


@app.get("/document/{doc_id}/preview")
async def preview_document(doc_id: str):
    """在线查看用的 PDF。inline 让浏览器直接渲染，而不是下载。"""
    db = doc_ingest.load_docs_db()
    meta = db.get(doc_id)
    if not meta:
        return JSONResponse({"status": "error", "message": "资料不存在"}, status_code=404)
    _, pdf_path = doc_ingest.doc_paths(doc_id, meta.get("ext", ""))
    if not pdf_path.exists():
        return JSONResponse(
            {"status": "error",
             "message": meta.get("preview_note") or "这份资料没有可预览的 PDF"},
            status_code=404,
        )
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{Path(meta.get('name', doc_id)).stem}.pdf",
        content_disposition_type="inline",
    )


@app.get("/document/{doc_id}/download")
async def download_document(doc_id: str):
    """下载原件。"""
    db = doc_ingest.load_docs_db()
    meta = db.get(doc_id)
    if not meta:
        return JSONResponse({"status": "error", "message": "资料不存在"}, status_code=404)
    src_path, _ = doc_ingest.doc_paths(doc_id, meta.get("ext", ""))
    if not src_path.exists():
        return JSONResponse({"status": "error", "message": "原件已不在磁盘上"}, status_code=404)
    return FileResponse(src_path, filename=meta.get("name", doc_id))


@app.get("/document/{doc_id}/text")
async def document_text(doc_id: str):
    """把抽好的正文读回来，给「无法在线查看」的降级阅读用。"""
    db = doc_ingest.load_docs_db()
    meta = db.get(doc_id)
    if not meta:
        return JSONResponse({"status": "error", "message": "资料不存在"}, status_code=404)
    src_path, pdf_path = doc_ingest.doc_paths(doc_id, meta.get("ext", ""))
    text, pages, note = doc_ingest.extract_text(
        src_path, meta.get("kind", ""), pdf_path if pdf_path.exists() else None
    )
    return {"status": "success", "name": meta.get("name", ""), "text": text,
            "pages": pages, "note": note}


@app.delete("/document/{doc_id}")
async def delete_document(doc_id: str):
    ok, message = await asyncio.to_thread(doc_ingest.remove, doc_id, rag_db)
    return {"status": "success" if ok else "error", "message": message}


@app.get("/knowledge_sources")
async def list_knowledge_sources():
    """列出当前知识库里有哪些文档"""
    try:
        sources = rag_db.list_sources()
        return {"status": "success", "sources": sources}
    except Exception:
        return {"status": "success", "sources": ["（知识库不支持来源列表）"]}

@app.delete("/knowledge_source/{source_name}")
async def delete_knowledge_source(source_name: str):
    """按来源名删除知识库切片（兼容没有原件记录的旧条目）"""
    ok, message = await asyncio.to_thread(doc_ingest.remove_by_source, source_name, rag_db)
    return {"status": "success" if ok else "error", "message": message}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
