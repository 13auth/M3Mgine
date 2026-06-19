# Roadmap

This is a v0, early-access engine. Direction below; subject to change based on real usage.
Have an opinion? Open a [discussion](https://github.com/13auth/M3Mgine/discussions) or issue.

## Now — v0.1 (shipped)
- Correction → rule → pre-output **enforcement** (hard regex + soft LLM-judge, fail-closed)
- **Processed memory** (extract / dedup / bi-temporal) + temporal knowledge graph
- **Grounded answers** with `fact / prior / forecast` provenance + audit export
- Crypto-shred erasure (GDPR/KVKK Art. 17), multi-tenant
- SQLite + PostgreSQL/pgvector (conformance-tested parity), CLI / HTTP API / MCP / SDK

## Next — v0.2
- `pip install` from PyPI
- Published retrieval & enforcement benchmarks (reproducible)
- Cross-encoder reranker (premium retrieval path)
- Tighter docs site + more examples / cookbook

## Later
- TS/JS SDK
- Pluggable graph-DB backend for very large KGs
- Adaptive RRF weighting (feedback-driven)

## Non-goals
- Being "just another memory store" — enforcement + provenance is the point.
