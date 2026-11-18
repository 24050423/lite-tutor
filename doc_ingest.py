# -*- coding: utf-8 -*-
"""资料库：把上传的课件落盘、转成可在线查看的 PDF、并抽出正文喂给知识库。

支持的格式：pdf / docx / doc / pptx / ppt

在线查看走「一律转 PDF」这条路——浏览器能原生渲染 PDF，但打不开 Office 格式。
转换用本机已装的 Word / PowerPoint（COM 自动化），转出来的 PDF 保真度最高；
没装 Office 或转换失败时自动降级成「只抽文字」，页面上会说明原因。
"""
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

DOCS_DIR = Path("documents")
DOCS_DB_PATH = Path("documents_db.json")
DOCS_LOCK = threading.RLock()

# 扩展名 -> (分类, 中文名)
SUPPORTED_EXTS = {
    ".pdf": ("pdf", "PDF"),
    ".docx": ("word", "Word"),
    ".doc": ("word", "Word（旧版）"),
    ".pptx": ("ppt", "PPT"),
    ".ppt": ("ppt", "PPT（旧版）"),
}

CONVERT_TIMEOUT = 180  # 秒。大课件转起来慢，给足时间；超时就放弃转换、只留文字

try:
    import pythoncom
    import win32com.client
    HAS_OFFICE_COM = True
except Exception:  # 非 Windows 或没装 pywin32
    HAS_OFFICE_COM = False


# ── 文档元数据 ──────────────────────────────────────────────

def load_docs_db() -> dict:
    try:
        return json.loads(DOCS_DB_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_docs_db(db: dict) -> None:
    temporary = DOCS_DB_PATH.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DOCS_DB_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def published_documents(db: dict) -> dict[str, str]:
    return {meta["name"]: doc_id for doc_id, meta in db.items() if meta.get("versioned_index")}


def doc_paths(doc_id: str, ext: str) -> Tuple[Path, Path]:
    """返回（原件路径, 预览用 PDF 路径）。"""
    return DOCS_DIR / f"{doc_id}{ext}", DOCS_DIR / f"{doc_id}.pdf"


# ── Office COM 自动化 ───────────────────────────────────────
#
# 每次转换都新起一个 Word 进程要十几秒（DLL 冷启动），所以复用同一个实例。
# COM 对象是线程亲和的，而 FastAPI 会在不同线程里跑请求，于是把转换全部丢进
# 一条固定线程的线程池，实例缓存在该线程的 thread-local 上。

_office_lock = threading.Lock()
_office_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="office")
_office_local = threading.local()


def _drop_app(kind: str) -> None:
    apps = getattr(_office_local, "apps", None)
    if apps and kind in apps:
        try:
            apps[kind].Quit()
        except Exception:
            pass
        apps.pop(kind, None)


def _get_app(kind: str):
    apps = getattr(_office_local, "apps", None)
    if apps is None:
        pythoncom.CoInitialize()
        apps = _office_local.apps = {}
    if kind not in apps:
        if kind == "word":
            app = win32com.client.DispatchEx("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0
        else:
            # PowerPoint 不接受 Visible=False，靠打开时 WithWindow=False 做到不弹窗
            app = win32com.client.DispatchEx("PowerPoint.Application")
        apps[kind] = app
    return apps[kind]


def _word_to_pdf(src: Path, dst: Path) -> None:
    app = _get_app("word")
    doc = app.Documents.Open(str(src.resolve()), ReadOnly=True,
                             ConfirmConversions=False, AddToRecentFiles=False)
    try:
        doc.ExportAsFixedFormat(str(dst.resolve()), 17)  # 17 = wdExportFormatPDF
    finally:
        doc.Close(False)


def _ppt_to_pdf(src: Path, dst: Path) -> None:
    app = _get_app("ppt")
    pres = app.Presentations.Open(str(src.resolve()), WithWindow=False)
    try:
        pres.SaveAs(str(dst.resolve()), 32)  # 32 = ppSaveAsPDF
    finally:
        pres.Close()


def _convert_once(src: Path, dst: Path, kind: str) -> None:
    try:
        (_word_to_pdf if kind == "word" else _ppt_to_pdf)(src, dst)
    except Exception:
        # 缓存的实例可能已经被关掉或崩了，丢掉重来一次
        _drop_app(kind)
        (_word_to_pdf if kind == "word" else _ppt_to_pdf)(src, dst)


def convert_to_pdf(src: Path, dst: Path, kind: str) -> Tuple[bool, str]:
    """把 Office 文档转成 PDF。返回 (是否成功, 失败原因)。"""
    if kind == "pdf":
        return True, ""
    if not HAS_OFFICE_COM:
        return False, "未安装 pywin32，无法调用 Office 转换（原件仍可下载）"

    global _office_pool
    with _office_lock:
        pool = _office_pool
    try:
        pool.submit(_convert_once, src, dst, kind).result(timeout=CONVERT_TIMEOUT)
        return True, ""
    except FutureTimeout:
        # COM 调用卡住是没法中断的，这条线程只能放弃，换一条新的继续服务
        with _office_lock:
            if _office_pool is pool:
                _office_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="office")
        return False, f"转换超时（超过 {CONVERT_TIMEOUT} 秒），已跳过转换"
    except Exception as e:
        return False, f"Office 转换失败：{e}"


# ── 正文抽取 ────────────────────────────────────────────────

def _extract_pdf_pages(path: Path) -> tuple[list[tuple[int, str]], int]:
    import fitz
    parts = []
    with fitz.open(str(path)) as doc:
        for number, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                parts.append((number, text))
        return parts, doc.page_count


def _extract_pdf(path: Path) -> Tuple[str, int]:
    parts, total = _extract_pdf_pages(path)
    return "\n\n".join(f"【第{page}页】\n{text}" for page, text in parts), total


def _extract_docx(path: Path) -> Tuple[str, Optional[int]]:
    import docx
    d = docx.Document(str(path))
    parts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts), None


def _extract_pptx(path: Path, *, notes_only: bool = False) -> Tuple[str, int]:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides):
        buf = []
        for shape in ([] if notes_only else slide.shapes):
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs).strip()
                    if line:
                        buf.append(line)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        buf.append(" | ".join(cells))
        try:
            if slide.has_notes_slide:
                note = slide.notes_slide.notes_text_frame.text.strip()
                if note:
                    buf.append(f"（备注）{note}")
        except Exception:
            pass
        if buf:
            parts.append("\n".join(buf) if notes_only else f"【第{i + 1}页】\n" + "\n".join(buf))
    return "\n\n".join(parts), len(prs.slides)


def extract_text(path: Path, kind: str, pdf_path: Optional[Path] = None) -> Tuple[str, Optional[int], str]:
    """抽出正文。返回 (正文, 页数, 说明)。

    .doc/.ppt 这些旧格式没有纯 Python 解析库，只能拿转出来的 PDF 顶一下——
    Word 转出来的 PDF 文字层是准的，PowerPoint 的有时候会乱码，所以给个说明。
    """
    try:
        if kind == "pdf":
            text, pages = _extract_pdf(path)
            return text, pages, ""
        if kind == "word":
            if path.suffix.lower() == ".docx":
                text, pages = _extract_docx(path)
                return text, pages, ""
            if pdf_path and pdf_path.exists():
                text, pages = _extract_pdf(pdf_path)
                return text, pages, "旧版 .doc：正文从转换后的 PDF 提取"
            return "", None, "旧版 .doc 需要转成 PDF 才能提取正文，但转换未成功"
        if kind == "ppt":
            if path.suffix.lower() == ".pptx":
                text, pages = _extract_pptx(path)
                return text, pages, ""
            if pdf_path and pdf_path.exists():
                text, pages = _extract_pdf(pdf_path)
                return text, pages, "旧版 .ppt：正文从转换后的 PDF 提取，可能有乱码"
            return "", None, "旧版 .ppt 需要转成 PDF 才能提取正文，但转换未成功"
    except Exception as e:
        return "", None, f"正文提取失败：{e}"
    return "", None, "未知格式"


# ── 对外主流程 ──────────────────────────────────────────────

def _remove_files(*paths: Path) -> None:
    for path in set(paths):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[DOCS] 清理资料文件失败：{type(exc).__name__}")


def ingest(filename: str, contents: bytes, rag_db) -> dict:
    """准备文件和索引，确认可用后发布新版本。"""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return {"status": "error",
                "message": f"不支持的格式 {ext or '（无扩展名）'}，请上传 PDF / Word / PPT"}

    kind, type_label = SUPPORTED_EXTS[ext]
    DOCS_DIR.mkdir(exist_ok=True)
    doc_id = uuid.uuid4().hex
    src_path, pdf_path = doc_paths(doc_id, ext)
    published = False
    try:
        src_path.write_bytes(contents)
        ok, reason = (True, "") if kind == "pdf" else convert_to_pdf(src_path, pdf_path, kind)
        sections: list[tuple[int | None, str]] = []
        pages, note = None, ""
        if ok:
            try:
                page_sections, pages = _extract_pdf_pages(pdf_path)
                sections.extend(page_sections)
            except Exception:
                ok, reason = False, "PDF 预览无法读取"
        if not ok and kind != "pdf":
            _remove_files(pdf_path)
        if sections and ext == ".pptx":
            try:
                notes, _ = _extract_pptx(src_path, notes_only=True)
                if notes:
                    sections.append((None, notes))
            except Exception:
                note = "未能提取幻灯片备注"
        if not sections and kind != "pdf":
            text, _, note = extract_text(src_path, kind)
            if text.strip():
                sections = [(None, text)]
        if not sections and not ok:
            return {"status": "error", "message": note or reason or "未提取到正文"}

        with DOCS_LOCK:
            db = load_docs_db()
            old = {key: meta for key, meta in db.items() if meta.get("name") == filename}
            if not sections:
                old_indexed = any(meta.get("indexed") for meta in old.values())
                if old_indexed or (rag_db is not None and filename in rag_db.list_sources(published_documents(db))):
                    return {"status": "error", "message": "新资料未提取到正文，已保留原有知识库"}

            document = {
                "doc_id": doc_id, "name": filename, "ext": ext, "kind": kind,
                "type_label": type_label, "size": len(contents),
                "uploaded_at": datetime.now().isoformat(), "pages": pages,
                "chars": sum(len(text) for _, text in sections), "previewable": ok,
                "preview_note": reason, "indexed": bool(sections), "note": note,
                "versioned_index": True,
            }
            try:
                if sections and (rag_db is None or not rag_db.add_document_version(doc_id, filename, sections)):
                    raise RuntimeError("知识库不可用")
                remaining = {key: meta for key, meta in db.items() if key not in old}
                remaining[doc_id] = document
                save_docs_db(remaining)
            except Exception as exc:
                if sections and rag_db is not None:
                    try:
                        rag_db.delete_document_version(doc_id)
                    except Exception:
                        pass  # 未发布的片段不会进入检索结果。
                message = getattr(rag_db, "embedding_error", "") or type(exc).__name__
                return {"status": "error", "message": f"资料发布失败，已保留原有数据（{message}）"}
            published = True

            if rag_db is not None:
                try:
                    rag_db.delete_source(filename, preserve_doc_id=doc_id)
                except Exception as exc:
                    print(f"[DOCS] 清理旧索引失败：{type(exc).__name__}")
            for old_id, meta in old.items():
                _remove_files(*doc_paths(old_id, meta.get("ext", "")))
        return {"status": "success", "document": document}
    finally:
        if not published:
            _remove_files(src_path, pdf_path)


def remove(doc_id: str, rag_db) -> Tuple[bool, str]:
    with DOCS_LOCK:
        db = load_docs_db()
        meta = db.get(doc_id)
        if not meta:
            return False, "资料不存在"
        name = meta.get("name", doc_id)
        if rag_db is not None:
            try:
                rag_db.delete_source(name)
            except Exception:
                return False, "知识库切片删除失败，资料仍保留"
        del db[doc_id]
        save_docs_db(db)
        _remove_files(*doc_paths(doc_id, meta.get("ext", "")))
    return True, f"已删除《{name}》及其知识库切片"


def remove_by_source(source_name: str, rag_db) -> Tuple[bool, str]:
    """按文件名删除——兼容旧知识库里那些没有资料记录的来源。"""
    with DOCS_LOCK:
        db = load_docs_db()
        hit = next((i for i, m in db.items() if m.get("name") == source_name), None)
        if hit:
            return remove(hit, rag_db)
        if rag_db is None:
            return False, "资料不存在"
        try:
            n = rag_db.delete_source(source_name)
        except Exception:
            return False, "知识库切片删除失败"
    if not n:
        return False, f"知识库里没有《{source_name}》"
    return True, f"已从知识库删除《{source_name}》（{n} 个切片，无原件可清理）"


def list_all(rag_db) -> list:
    """资料库列表 = 有原件的资料 + 旧知识库里那些只能查到来源名的条目。"""
    with DOCS_LOCK:
        db = load_docs_db()
        items = sorted(db.values(), key=lambda d: d.get("uploaded_at") or "", reverse=True)
        kb_sources = rag_db.list_sources(published_documents(db)) if rag_db else []

    known = {d.get("name") for d in items}
    for s in kb_sources:
        if s in known or s.startswith("__fingerprint__"):
            continue
        items.append({
            "doc_id": "",
            "name": s,
            "kind": "legacy",
            "type_label": "仅知识库",
            "size": 0,
            "uploaded_at": "",
            "pages": None,
            "previewable": False,
            "preview_note": "这份资料只有知识库切片，没有原件，因此无法在线查看",
            "indexed": True,
            "note": "",
        })
    return items
