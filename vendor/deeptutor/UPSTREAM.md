# DeepTutor source snapshot

Source: [HKUDS/DeepTutor at `1284116f`](https://github.com/HKUDS/DeepTutor/tree/1284116f92368480aef3a47f91b8df9e9b105062). Licensed under Apache 2.0. See [LICENSE](LICENSE). Files preserve their upstream paths relative to the `deeptutor` package.

- `services/rag/pipelines/llamaindex/` provides the upstream BM25 and reciprocal rank fusion implementation. Lite-Tutor supplies scoped ChromaDB candidates and existing chunk IDs through an adapter.
- `reading/quiz.py` provides source-grounded multiple-choice generation and exact-phrase evidence validation. Its completion callable is injected by Lite-Tutor instead of using DeepTutor's model selection service.
- `learning/semantic_assessment.py` provides the upstream grading prompt, verdict validation and confidence gating. An injected text-only transport uses the existing Lite-Tutor model settings. `learning/grading.py` checks multiple-choice submissions against the stored key.
- `services/practice/scheduler.py` is copied unchanged. Lite-Tutor replays existing course attempts through it and applies the user’s manual completion or snooze separately.

Absolute package imports in the copied modules point at `vendor.deeptutor`. The snapshot contains only the modules and entry points used by the adapter. DeepTutor's application server, agent runtime, visual grading transport, and SQLite storage are outside this integration. The upstream BM25 tests in `tests/upstream/` change the package import to the vendored path.
