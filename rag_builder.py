import os
import re
import math
import hashlib
import chromadb
from chromadb.utils import embedding_functions
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass

# ── 分词 ────────────────────────────────────────────────────
# 中文没有空格，早先那句 re.split(r"[^a-zA-Z0-9一-鿿]+") 会把整句话切成一个 token，
# 「计算机硬件系统五大部件」永远匹配不上原文的「计算机硬件系统（五大部件）」，中文检索等于失效。
# 现在中文切二元组（计算/算机/机硬/硬件…），英文数字仍按词切，两边才有交集。
_CJK = "一-鿿㐀-䶿"
_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[" + _CJK + "]+")
_DOTTED_NUMBER_RE = re.compile(r"(?<![a-zA-Z0-9_])(?:v)?\d+(?:\.\d+)+(?![a-zA-Z0-9_]|\.\d)", re.IGNORECASE)
DOCUMENT_CHUNK = "document_chunk"


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    text: str
    source: str
    doc_id: str | None
    page: int | None


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    confidence: float
    keyword_only: bool = False


def tokenize(text) -> list:
    """中英混排分词：英文/数字按词切，中文切成相邻二元组。"""
    tokens = []
    for chunk in _TOKEN_RE.findall(str(text).lower()):
        if chunk[0].isascii():
            tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return tokens


def _bucket(token: str, dim: int) -> int:
    """把 token 映射到 0..dim-1 的桶。

    这里必须用固定摘要：内置 hash() 对字符串是每个进程带随机种子的，
    同一个词在两次启动里会落到不同桶，存进去的向量和查询时的向量根本不在同一个空间，
    检索结果全是噪声（实测 hash("计算机") 连开三个进程得到三个不同值）。
    """
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big") % dim


class HashEmbeddingFunction:
    """离线嵌入：分词之后做哈希词袋（hashing trick），再 L2 归一化。

    dim 从 64 提到 512：二元组比整句 token 多得多，64 个桶撞得太厉害。
    注意——改了分词或维度就必须重建索引（旧向量是按老规则算的），
    重建脚本见 README。
    """

    def __init__(self, dim: int = 512):
        self.dim = dim

    def name(self):
        return "hash-embedding"

    def __call__(self, input):
        vectors = []
        for text in input:
            vec = [0.0] * self.dim
            for token in tokenize(text):
                vec[_bucket(token, self.dim)] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors

    def embed_query(self, input):
        """查库走的是这个方法，不是 __call__。

        chromadb 1.x 在 query 时调 embed_query、在 add 时调 __call__。原来只有
        __call__，于是每次 query 都抛 AttributeError 被 except 吞掉，静默降级成
        关键词搜索——中文那会儿又切不出 token，检索等于完全失效。存进来的向量
        因此从来没被查过一次。这里补上，向量召回才真正生效。
        """
        return self(input)

def _can_reach(url: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False

class LocalRAGKnowledgeBase:
    """
    Offline RAG Knowledge Base using ChromaDB.
    Ingests professional course materials (e.g., Big Data, Data Structures) 
    to eliminate LLM hallucinations.
    """
    # 注意：collection_name 是已持久化数据的标识，改名会连到空集合导致知识库"消失"。
    # 项目已更名为「智能学习平台」，但此处保持原名以兼容既有 chroma_db 数据。
    def __init__(self, db_path="./chroma_db", collection_name="lite_tutor_kb"):
        print("[SYSTEM] Initializing Local Vector Database...")
        self.client = chromadb.PersistentClient(path=db_path)
        mode = (os.getenv("TUTOR_OFFLINE") or os.getenv("LITETUTOR_OFFLINE") or "auto").strip().lower()
        force_offline = mode in {"1", "true", "yes", "offline"}
        force_online = mode in {"0", "false", "no", "online"}
        self.embedding_error = ""
        try:
            existing = self.client.get_collection(collection_name, embedding_function=None)
        except chromadb.errors.NotFoundError:
            existing = None

        if existing is None:
            self.embedding_fn = HashEmbeddingFunction()
            if not force_offline and (force_online or _can_reach("https://huggingface.co")):
                try:
                    self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction()
                except Exception as exc:
                    print(f"[RAG] 新库使用哈希嵌入，句向量加载失败：{type(exc).__name__}")
            self.collection = self.client.get_or_create_collection(
                name=collection_name, embedding_function=self.embedding_fn,
            )
        else:
            self.embedding_fn = self._existing_embedding(existing, force_offline)
            self.collection = (self.client.get_collection(collection_name, embedding_function=self.embedding_fn)
                               if self.embedding_fn is not None else existing)
        if self.embedding_error:
            print(f"[RAG] {self.embedding_error}，仅关键词检索")
        else:
            print(f"[RAG] 知识库已连接，嵌入：{self.embedding_fn.name()}")

    def _existing_embedding(self, collection, force_offline: bool):
        config = collection.configuration_json.get("embedding_function") or {}
        name = config.get("name")
        if name == "sentence_transformer":
            if force_offline:
                self.embedding_error = "现有知识库使用句向量，离线设置下仅可读取正文"
                return None
            try:
                factory = embedding_functions.SentenceTransformerEmbeddingFunction
                return factory.build_from_config(config["config"]) if config.get("config") else factory()
            except Exception as exc:
                self.embedding_error = f"现有知识库的句向量模型加载失败（{type(exc).__name__}）"
                return None
        if name in (None, "hash-embedding"):
            candidate = HashEmbeddingFunction()
            sample = collection.get(limit=1, include=["embeddings"])["embeddings"]
            if sample is None or not len(sample) or len(sample[0]) == candidate.dim:
                return candidate
        self.embedding_error = "现有知识库的嵌入配置与当前版本不兼容"
        return None

    def ingest_text_file(self, file_path: str, chunk_size: int = 500):
        """Reads a text file, chunks it, and upserts to the vector database."""
        if not os.path.exists(file_path):
            print(f"[ERROR] File not found: {file_path}")
            return
            
        print(f"\n[PROCESS] Reading and chunking {file_path}...")
        with open(file_path, 'r', encoding='utf-8') as f:
            text = f.read()
            
        # Simple chunking strategy (splitting by paragraphs/length)
        # In a real scenario, you'd use LangChain's RecursiveCharacterTextSplitter
        chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
        
        ids = [f"{os.path.basename(file_path)}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": file_path} for _ in range(len(chunks))]
        
        print(f"[PROCESS] Upserting {len(chunks)} chunks into ChromaDB...")
        self.collection.add(
            documents=chunks,
            metadatas=metadatas,
            ids=ids
        )
        print("[SUCCESS] Ingestion complete!")

    @staticmethod
    def _keyword_tokens(text: str) -> set[str]:
        # 题号 2.6.8 和 2.6.9 要区分，同时保留已有向量索引的分词规则。
        identifiers = _DOTTED_NUMBER_RE.findall(text.lower())
        return set(tokenize(_DOTTED_NUMBER_RE.sub(" ", text))) | set(identifiers)

    @staticmethod
    def _search_where(source=None, published: dict[str, str] | None = None) -> dict:
        clauses = [{"type": {"$ne": "fingerprint"}}]
        if source:
            clauses.append({"source": source})
        if published is not None:
            legacy = {"type": {"$ne": DOCUMENT_CHUNK}}
            if published:
                legacy = {"$and": [legacy, {"source": {"$nin": list(published)}}]}
                clauses.append({"$or": [legacy, {"doc_id": {"$in": list(published.values())}}]})
            else:
                clauses.append(legacy)
        return {"$and": clauses} if len(clauses) > 1 else clauses[0]

    @staticmethod
    def _hits(ids, documents, metadatas):
        for chunk_id, text, meta in zip(ids, documents, metadatas):
            if not text or text.startswith("__fingerprint__"):
                continue
            meta = meta or {}
            yield SearchHit(chunk_id, text, meta.get("source") or "未知资料",
                            meta.get("doc_id"), meta.get("page"))

    def _keyword_hits(self, query_text: str, where: dict):
        query_tokens = self._keyword_tokens(query_text)
        if not query_tokens:
            return []
        rows = self.collection.get(include=["documents", "metadatas"], where=where)
        scores = []
        for hit in self._hits(rows["ids"], rows["documents"], rows["metadatas"]):
            matches = query_tokens & self._keyword_tokens(hit.text)
            if matches:
                scores.append((hit, len(matches) / len(query_tokens)))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    def _vector_hits(self, query_text: str, n_results: int, where: dict):
        if self.embedding_fn is None:
            return None
        try:
            results = self.collection.query(
                query_texts=[query_text], n_results=n_results, where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            print(f"[SEARCH] 向量检索失败：{type(exc).__name__}")
            return None
        ids = results["ids"][0]
        distances = dict(zip(ids, results["distances"][0]))
        return [(hit, distances[hit.chunk_id]) for hit in
                self._hits(ids, results["documents"][0], results["metadatas"][0])]

    def query_knowledge_chunks(self, query_text: str, n_results: int = 2, source=None):
        where = self._search_where(source)
        candidates = self._vector_hits(query_text, n_results, where)
        if candidates is None:
            candidates = self._keyword_hits(query_text, where)[:n_results]
        return [hit.text for hit, _ in candidates]

    def query_knowledge_chunks_with_scores(self, query_text: str, n_results: int = 2, source=None):
        where = self._search_where(source)
        candidates = self._vector_hits(query_text, n_results, where)
        if candidates is None:
            candidates = [(hit, 1.0 / score - 1.0)
                          for hit, score in self._keyword_hits(query_text, where)[:n_results]]
        return [hit.text for hit, _ in candidates], [distance for _, distance in candidates]

    def query_knowledge(self, query_text: str, n_results: int = 2, source=None) -> str:
        chunks = self.query_knowledge_chunks(query_text, n_results, source)
        if not chunks:
            return "No relevant context found in the local knowledge base."
        return "\n---\n".join(chunks)

    def search_hits(self, query_text: str, n_results: int = 2, source=None, *, mode: str = "hybrid",
                    published: dict[str, str] | None = None) -> SearchResult:
        """独立召回向量和关键词候选，再按名次融合已过门槛的片段。"""
        if n_results < 1 or not query_text.strip():
            return SearchResult([], 0.0)

        threshold = float(os.getenv("RAG_CONF_THRESHOLD", "0.35"))
        hybrid = mode.lower() != "vector"
        limit = n_results * 2 if hybrid else n_results
        where = self._search_where(source, published)
        distances = self._vector_hits(query_text, limit, where)
        vector = [(hit, 1.0 / (1.0 + distance)) for hit, distance in distances or []]
        keywords = self._keyword_hits(query_text, where)[:limit] if hybrid or distances is None else []
        confidence = max((score for _, score in keywords + vector), default=0.0)

        # 两路分数的含义不同。用倒数排名融合，同一片段每路只计一次。
        combined: dict[SearchHit, float] = {}
        for candidates in (keywords, vector):
            qualified = dict.fromkeys(hit for hit, score in candidates if score >= threshold)
            for rank, hit in enumerate(qualified, start=1):
                combined[hit] = combined.get(hit, 0.0) + 1.0 / (60 + rank)
        ranked = sorted(combined, key=combined.__getitem__, reverse=True)
        return SearchResult(ranked[:n_results], confidence, keyword_only=distances is None)

    def search_chunks(self, query_text: str, n_results: int = 2, source=None,
                      *, mode: str = "hybrid") -> tuple[list[str], float]:
        result = self.search_hits(query_text, n_results, source, mode=mode)
        return [hit.text for hit in result.hits], result.confidence

    def has_content(self, published: dict[str, str]) -> bool:
        rows = self.collection.get(limit=1, include=[], where=self._search_where(published=published))
        return bool(rows["ids"])

    def query_knowledge_hybrid(self, query_text: str, n_results: int = 2, source=None) -> str:
        chunks, _ = self.search_chunks(query_text, n_results, source)
        if not chunks:
            return "No relevant context found in the local knowledge base."
        return "\n---\n".join(chunks)

    def add_document_version(self, doc_id: str, source: str, sections: list[tuple[int | None, str]]) -> int:
        if self.embedding_fn is None:
            raise RuntimeError(self.embedding_error)
        chunks = []
        metadatas = []
        for page, text in sections:
            # 保留页边界，同一页按固定窗口切分，并留少量重叠。
            text = text.strip()
            start = 0
            while start < len(text):
                end = min(start + 500, len(text))
                chunks.append(text[start:end])
                metadata = {"type": DOCUMENT_CHUNK, "source": source, "doc_id": doc_id}
                if page is not None:
                    metadata["page"] = page
                metadatas.append(metadata)
                if end == len(text):
                    break
                start = end - 50
        if chunks:
            batch_size = self.client.get_max_batch_size()
            for start in range(0, len(chunks), batch_size):
                end = min(start + batch_size, len(chunks))
                self.collection.add(ids=[f"{doc_id}:{i}" for i in range(start, end)],
                                    documents=chunks[start:end], metadatas=metadatas[start:end])
        return len(chunks)

    def delete_document_version(self, doc_id: str) -> None:
        self.collection.delete(where={"doc_id": doc_id})

    def add_document(self, text: str, source: str = "uploaded") -> bool:
        """动态添加文档到知识库，返回False表示已存在"""
        if self.embedding_fn is None:
            raise RuntimeError(self.embedding_error)
        # 计算文档指纹
        fingerprint = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
        fp_id = f"__fingerprint__{source}"
        # 检查是否已存在
        try:
            existing = self.collection.get(ids=[fp_id])
            if existing and existing.get("ids"):
                print(f"[RAG] 文档已存在，跳过：{source}")
                return False
        except Exception:
            pass
        chunks = []
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 20]
        current_chunk = ""
        for para in paragraphs:
            if len(current_chunk) + len(para) < 300:
                current_chunk += para + "\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = para + "\n"
        if current_chunk:
            chunks.append(current_chunk.strip())
        if not chunks:
            return
        ids = [f"{source}_{i}_{uuid.uuid4().hex[:6]}" for i in range(len(chunks))]
        self.collection.add(
            documents=chunks,
            ids=ids,
            metadatas=[{"source": source} for _ in chunks]
        )
        # 存入指纹标记
        try:
            self.collection.add(
                documents=[f"__fingerprint__{fingerprint}"],
                ids=[fp_id],
                metadatas=[{"source": source, "type": "fingerprint"}]
            )
        except Exception:
            pass
        print(f"[RAG] 已添加 {len(chunks)} 个chunk，来源：{source}")
        return True


    def delete_source(self, source: str, *, preserve_doc_id: str | None = None) -> int:
        """删掉某个来源的全部 chunk，返回删除条数。

        指纹标记那条也要一起删——它记的是「这份文件已经导过了」，
        留着的话下次重新上传同名文件会被当成重复直接跳过。
        """
        where = {"source": source}
        if preserve_doc_id:
            where = {"$and": [where, {"doc_id": {"$ne": preserve_doc_id}}]}
        ids = self.collection.get(include=[], where=where)["ids"]
        fp_id = f"__fingerprint__{source}"
        if fp_id not in ids and self.collection.get(ids=[fp_id], include=[])["ids"]:
            ids.append(fp_id)
        if not ids:
            return 0
        self.collection.delete(ids=ids)
        return len(ids)

    def list_sources(self, published: dict[str, str] | None = None) -> list:
        """列出知识库中所有文档来源"""
        try:
            results = self.collection.get(include=["metadatas"], where=self._search_where(published=published))
            sources = list({m.get("source", "unknown") for m in results["metadatas"] if m})
            return sources
        except Exception:
            return []

# Quick Local Test Block
if __name__ == "__main__":
    # 1. Initialize the RAG engine
    rag = LocalRAGKnowledgeBase()
    
    # 2. Let's create a dummy textbook file for testing
    dummy_file = "data_structure_notes.txt"
    with open(dummy_file, "w", encoding="utf-8") as f:
        f.write("KMP Algorithm Core Concept: KMP avoids redundant comparisons by utilizing a partial match table (next array) to skip characters safely during string matching.\n")
        f.write("Depth-First Search (DFS): A graph traversal algorithm that explores as far as possible along each branch before backtracking. It commonly uses a Stack data structure.\n")
        f.write("Big Data Principles: Hadoop uses HDFS for distributed storage and MapReduce for distributed processing of massive datasets.\n")
    
    # 3. Feed the textbook to the database
    rag.ingest_text_file(dummy_file)
    
    # 4. Pull the trigger (Test a search)
    question = "What data structure does DFS use?"
    retrieved_info = rag.query_knowledge(question)
    
    print("\n[RESULT] Retrieved Context:")
    print("========================================")
    print(retrieved_info)
    print("========================================")
