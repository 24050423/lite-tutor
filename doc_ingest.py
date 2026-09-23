# -*- coding: utf-8 -*-
"""资料库：把上传的课件落盘、转成可在线查看的 PDF、并抽出正文喂给知识库。

支持的格式：pdf / docx / doc / pptx / ppt

在线查看走「一律转 PDF」这条路——浏览器能原生渲染 PDF，但打不开 Office 格式。
转换用本机已装的 Word / PowerPoint（COM 自动化），转出来的 PDF 保真度最高；
没装 Office 或转换失败时自动降级成「只抽文字」，页面上会说明原因。
"""
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

DOCS_DIR = Path("documents")
DOCS_DB_PATH = Path("documents_db.json")

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
    if DOCS_DB_PATH.exists():
        try:
            return json.loads(DOCS_DB_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_docs_db(db: dict) -> None:
    DOCS_DB_PATH.write_text(
        json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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

def _extract_pdf(path: Path) -> Tuple[str, int]:
    import fitz
    doc = fitz.open(str(path))
    parts = []
    try:
        for i, page in enumerate(doc):
            text = page.get_text().strip()
            if text:
                parts.append(f"【第{i + 1}页】\n{text}")
        total = doc.page_count
    finally:
        doc.close()
    return "\n\n".join(parts), total


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


def _extract_pptx(path: Path) -> Tuple[str, int]:
    from pptx import Presentation
    prs = Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides):
        buf = []
        for shape in slide.shapes:
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
            parts.append(f"【第{i + 1}页】\n" + "\n".join(buf))
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

def ingest(filename: str, contents: bytes, rag_db) -> dict:
    """保存 -> 转 PDF -> 抽正文 -> 入知识库。返回给前端的结果字典。"""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        return {"status": "error",
                "message": f"不支持的格式 {ext or '（无扩展名）'}，请上传 PDF / Word / PPT"}

    kind, type_label = SUPPORTED_EXTS[ext]
    DOCS_DIR.mkdir(exist_ok=True)
    db = load_docs_db()

    # 同名视为更新：清掉旧的记录、文件和知识库切片，避免越传越乱
    replaced = False
    for old_id, old in list(db.items()):
        if old.get("name") == filename:
            _purge(old_id, old, db, rag_db)
            replaced = True
    if not replaced and rag_db is not None:
        # 旧知识库里可能有同名来源却没有原件记录（历史遗留）。不先清掉的话，
        # 它的指纹还在，add_document 会把这次上传当成重复文件直接跳过，
        # 于是知识库里留着的还是旧正文。
        try:
            rag_db.delete_source(filename)
        except Exception:
            pass

    doc_id = uuid.uuid4().hex[:8]
    src_path, pdf_path = doc_paths(doc_id, ext)
    src_path.write_bytes(contents)

    # PDF 不用转，原件就是预览件（doc_paths 对 pdf 返回的是同一个路径）
    if kind == "pdf":
        if src_path.resolve() != pdf_path.resolve():
            shutil.copyfile(src_path, pdf_path)
        ok, reason = True, ""
    else:
        ok, reason = convert_to_pdf(src_path, pdf_path, kind)
        if not ok:
            pdf_path.unlink(missing_ok=True)

    text, pages, note = extract_text(src_path, kind, pdf_path if ok else None)

    indexed = False
    if text.strip():
        try:
            indexed = bool(rag_db.add_document(text, source=filename))
        except Exception as e:
            note = (note + "；" if note else "") + f"入知识库失败：{e}"

    db[doc_id] = {
        "doc_id": doc_id,
        "name": filename,
        "ext": ext,
        "kind": kind,
        "type_label": type_label,
        "size": len(contents),
        "uploaded_at": datetime.now().isoformat(),
        "pages": pages,
        "chars": len(text),
        "previewable": ok,
        "preview_note": reason,
        "indexed": indexed,
        "note": note,
    }
    save_docs_db(db)

    if not text.strip() and not ok:
        return {"status": "error",
                "message": f"既没能转换也没能提取正文：{reason}",
                "document": db[doc_id]}
    return {"status": "success", "document": db[doc_id]}


def _purge(doc_id: str, meta: dict, db: dict, rag_db) -> None:
    """删掉一份资料：文件、知识库切片、索引记录。调用方负责 save_docs_db。"""
    for path in doc_paths(doc_id, meta.get("ext", "")):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
    name = meta.get("name")
    if name and rag_db is not None:
        try:
            rag_db.delete_source(name)
        except Exception:
            pass
    db.pop(doc_id, None)


def remove(doc_id: str, rag_db) -> Tuple[bool, str]:
    db = load_docs_db()
    meta = db.get(doc_id)
    if not meta:
        return False, "资料不存在"
    name = meta.get("name", doc_id)
    _purge(doc_id, meta, db, rag_db)
    save_docs_db(db)
    return True, f"已删除《{name}》及其知识库切片"


def remove_by_source(source_name: str, rag_db) -> Tuple[bool, str]:
    """按文件名删除——兼容旧知识库里那些没有资料记录的来源。"""
    db = load_docs_db()
    hit = next((i for i, m in db.items() if m.get("name") == source_name), None)
    if hit:
        return remove(hit, rag_db)
    if rag_db is None:
        return False, "资料不存在"
    try:
        n = rag_db.delete_source(source_name)
    except Exception as e:
        return False, str(e)
    if not n:
        return False, f"知识库里没有《{source_name}》"
    return True, f"已从知识库删除《{source_name}》（{n} 个切片，无原件可清理）"


def list_all(rag_db) -> list:
    """资料库列表 = 有原件的资料 + 旧知识库里那些只能查到来源名的条目。"""
    db = load_docs_db()
    items = sorted(db.values(), key=lambda d: d.get("uploaded_at") or "", reverse=True)

    known = {d.get("name") for d in items}
    try:
        kb_sources = rag_db.list_sources() if rag_db else []
    except Exception:
        kb_sources = []
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
