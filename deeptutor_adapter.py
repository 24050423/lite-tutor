import json
from pathlib import Path
from types import SimpleNamespace

from vendor.deeptutor.learning.semantic_assessment import judge_answers
from vendor.deeptutor.reading.extensions import ReadingContext
from vendor.deeptutor.reading.quiz import ReadingQuizExtension
from vendor.deeptutor.utils.json_parser import parse_json_response


class UncertainGrade(Exception):
    pass


def rank_chunks(query: str, vector: list[tuple[str, float]], lexical: dict[str, str],
                limit: int, storage_dir: Path) -> list[str]:
    if not lexical:
        return [key for key, _ in vector[:limit]]

    from llama_index.core.retrievers import BaseRetriever
    from llama_index.core.schema import NodeWithScore, TextNode

    from vendor.deeptutor.services.rag.pipelines.llamaindex.retrievers import (
        RetrievalConfig,
        bm25_available,
        build_retriever,
    )

    if not bm25_available():
        return list(dict.fromkeys([*(key for key, _ in vector), *lexical]))[:limit]

    def node(key: str) -> TextNode:
        return TextNode(id_=key, text=lexical.get(key, ""), metadata={"chunk_id": key},
                        excluded_embed_metadata_keys=["chunk_id"])

    class ChromaCandidates(BaseRetriever):
        def _retrieve(self, query_bundle):
            return [NodeWithScore(node=node(key), score=score) for key, score in vector[:self.top_k]]

    class ChromaScope:
        docstore = SimpleNamespace(docs={key: node(key) for key in lexical})

        def as_retriever(self, *, similarity_top_k):
            candidates = ChromaCandidates()
            candidates.top_k = similarity_top_k
            return candidates

    retriever = build_retriever(ChromaScope(), storage_dir, top_k=limit, config=RetrievalConfig())
    return [hit.node.node_id for hit in retriever.retrieve(query)]


async def reading_questions(topic: str, passage: str, avoid_question: str, complete) -> list[dict]:
    async def request(**options):
        system = options["system_prompt"] + "\n优先考查 topic 指定的知识点。previous_question 仅用于去重。"
        user = json.loads(options["prompt"])
        user.update(topic=topic, previous_question=avoid_question)
        return complete([{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
                        max_tokens=options["max_tokens"], temperature=options["temperature"], timeout=12)

    context = ReadingContext(material_id="lite-tutor", locator=1, locale="zh", visible_text=passage)
    result = await ReadingQuizExtension(request).run_action("start", context)
    return result.payload["questions"]


async def assess_rubric(question: str, answer: str, rubric: list[dict], complete) -> list[dict]:
    async def request(config, instruction, payload, **kwargs):
        return parse_json_response(complete(
            [{"role": "system", "content": instruction},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            max_tokens=1200, temperature=0, timeout=20,
        ), fallback=None)

    rows = []
    statuses = {"correct": "met", "partial": "partial", "incorrect": "missed"}
    for start in range(0, len(rubric), 4):
        part = rubric[start:start + 4]
        verdicts = await judge_answers(None, [
            {"question_id": item["id"], "question": question, "expected_answer": item["expected"],
             "answer": answer} for item in part
        ], request=request)
        for item in part:
            receipt = verdicts[item["id"]].receipt()
            if not receipt["final"]:
                raise UncertainGrade(receipt["feedback"])
            rows.append({**item, "status": statuses[receipt["result"]], "explanation": receipt["feedback"]})
    return rows
