# Changelog

## 0.15.0 - 2026-10-12

- AI 阅读卡生成默认自动应用到结构化阅读卡的空白字段，并保留人工字段、历史快照和 `apply:false` 草稿模式。
- OpenAI 兼容配置继续支持官方 API 与 HTTPS 中转服务；连接测试报告具体的 `/models` 地址、HTTP 状态和服务端错误片段，API Key 仍不写入数据库或审计日志。
- 同步更新 OpenAPI、README、配置界面和版本信息。

## 0.14.0 - 2026-10-12

- Upgraded full-text reading-card and deep-read analysis to a multimodal PDF pipeline: rendered page images are sent with page-anchored text so formulas, plots, tables, diagrams and screenshots can be inspected.
- Added LaTeX formula transcription, table/figure relationships, problem formulation, algorithm steps, equation analysis and visual evidence sections to deep-read results.
- Added cached JPEG page rendering with `/api/papers/{id}/visual-pages` and `/visual-pages/{file}` inspection endpoints, bounded visual context, provider rejection fallback, and explicit visual uncertainty records.
- Scanned PDFs with no text layer can proceed through visual-only page analysis when a vision model is configured; text citations remain subject to local verification.

## 0.13.0 - 2026-10-12

- Added page-aware full-text AI reading-card drafts and graduate-level deep reads, using bounded chunks and a synthesis pass for long PDFs.
- Added local verification for model-cited page numbers and quotations, analysis history with page-text input hashes, explicit full-text consent, and a safe apply action that fills only empty reading-card fields by default.
- Added paper-detail controls for generating and reopening full-text analyses, plus API contract and regression coverage. Results remain separate from user notes and are not silently applied.
- Included versioned AI analyses in JSON library migration with paper-ID remapping and duplicate-safe import.

## 0.12.0 - 2026-10-12

- Added scheduled local backups with a configurable interval, retention count, retry state, and maintenance-page visibility.
- Separated manually exported archives from rotating automatic archives so long-term snapshots are preserved.
- Added `/api/settings/backup-policy` and system-settings controls for backup policy management.
- Added `MULTIUSER_PLATFORM_DESIGN.md` defining the target `src/` architecture, multi-user identity and workspace isolation, PostgreSQL migration, evidence-grounded paper analysis, agent workflows, and rollout gates. This remains a design baseline; version 0.12.0 was a local single-user application.

## 0.11.0 - 2026-10-12

- Added schema 9 durable search and maintenance foundations: SQLite FTS5 over metadata, notes, structured reading cards and extracted PDF text, with a safe field-search fallback and rebuild endpoint.
- Added server-side paginated library search, sort and filters so the browser no longer loads the whole collection at startup.
- Added note and reading-card revision history, restore actions, JSON export/import of history and audit coverage.
- Added durable synchronization job state with stages, attempts, retry time and interrupted-on-restart detection.
- Added maintenance status for index completeness, database/WAL size, document counts and orphan attachments/pages.

## 0.10.2 - 2026-10-12

- Added schema 8 structured reading cards with evidence, reproducibility, relevance and next-step fields; cards are included in JSON export/import and analytics coverage metrics.
- Added transactional batch triage for up to 500 papers, with safe audit events and library controls for selection, status, topic and favorites.
- Added `/api/runs/{runId}` source and query-variant run details, with an automation detail modal and historical-detail quality marker.

## 0.10.1 - 2026-10-12

- Added schema 7 migration with persisted per-source run details, so retrieval funnel, source health, filtering and acceptance metrics are backed by the actual run rather than the source-name list.
- Added explicit analytics data-quality status for historical runs whose detailed metrics cannot be reconstructed.
- Updated export/backup schema metadata, release diagnostics, UI quality notice and regression coverage.
- Added protocol-level regression coverage for the OpenAI-compatible `/models` connection check and Bearer header handling.

## 0.10.0 - 2026-10-12

- Added optional OpenReview discovery for current conference and workshop submissions; a challenged or unavailable endpoint remains isolated from the other sources.
- Upgraded ranking to source-weighted Reciprocal Rank Fusion plus bounded MMR diversity re-ranking, with per-paper rank paths, source confirmation, library distance and discovery class exposed for review.
- Added near-title deduplication that removes provider punctuation/casing noise while preserving records with different strong identifiers as separate editions.
- Added Chinese phrase bi/tri-gram recall with lower scoring weight, DOI/arXiv direct lookup paths, deeper survey/benchmark/evaluation/limitations/frontier lanes, and local terminology learning from read/favorite papers.
- Added optional `/api/discover/agent-plan` model-assisted query expansion. The model sees only the query and extracted facets; suggestions are bounded, marked as requiring review, and never write to the library.
- Added an “AI 拓展检索” control and optional OpenReview source control to the discovery page. Regression coverage now includes OpenReview normalization, rank fusion, near-title deduplication and the model-free fallback.
- Added a data-driven development loop to `/api/analytics` and the analytics page: retrieval funnel, quality gates, observation window, and prioritized P1/P2/P3 optimization actions. Added ADR, development, demo, and environment-template artifacts for enterprise-style project packaging.

## 0.9.0 - 2026-10-12

- Strengthened multilingual triage so the same curated Chinese/English bridge vocabulary drives retrieval, relevance scoring, coverage, and local paper connections.
- Added keyless DBLP bibliography search for computer-science venues, comma-separated exclusion terms, cross-topic global feedback learning, and optional neighbourhood expansion seeded from the local library.
- Changed long-running topic novelty to use that topic's own query history, so a paper discovered under one topic can still be new and useful under another.
- Added `/api/diagnostics` and a settings-page release/data-quality panel for version, schema, connector boundaries, metadata completeness, feedback coverage, and SLO targets.
- Added a model-free `/api/briefing` and home-page research brief that turns recent additions, backlog, topic movement, source failures, and learned recommendations into a repeatable weekly workflow.
- Aligned package metadata and showcase evidence with the 0.9.0 runtime and current test count.
- Upgraded discovery planning with research facets, mixed Chinese/English bridges, configurable focused/balanced/deep search depth, and survey/benchmark/empirical lanes.
- Added novelty, foundations, latest, and exploration ranking modes; results now expose concept coverage, source quality, citation/open-access signals, recall lanes, and incremental summaries.
- Added explicit long-running topic controls for suppressing previously seen papers and a `/api/discover/plan` preview endpoint.
- Added regression coverage for deep query planning, citation-aware ranking, and non-repeating topic retrieval.

## 0.8.0 - 2026-10-12

- Added `/api/evaluation` and a data-analysis quality panel with explicit-feedback Precision@5/10 baselines.
- Added evaluation regression coverage and documented the controlled improvement loop for future rankers and connectors.
- Added request duration to structured local logs for latency diagnosis without logging request bodies or credentials.
- Added Chinese-to-English research concept bridges for common queries such as multimodal reasoning, visual question answering, RAG, LLM, and graph neural networks.

## 0.7.0 - 2026-10-12

- Added deterministic concept-alias query expansion for high-signal research terms, preserving the original query as the primary path.
- Merged duplicate hits across query variants before persistence and exposed per-paper discovery paths, variant-level source diagnostics, and duplicate counts.
- Included novelty in discovery ranking so recurring scheduled results lose priority while first-time candidates stay visible.
- Added regression coverage for multi-route recall and path-aware deduplication.
- Added a transparent evaluation loop with Precision@5/10 from explicit query feedback and a data-analysis panel for comparing future ranking changes.

## 0.6.0 - 2026-10-12

- Expanded discovery to Europe PMC and optional Semantic Scholar connectors.
- Added transparent query plans, persistent search filters, multi-source coverage metrics, recency/novelty/diversity ranking, and citation/related-work expansion.
- Added schema 6 migration for saved query search options and updated portable export/backup manifests.

## 0.5.0 - 2026-10-12

- Added optional page-level PDF text extraction with quality labels, hashes, and search endpoints.
- Added schema 5 migration and portable JSON export of derived page text.

## 0.4.0 - 2026-10-12

- Added OpenAlex as a broad scholarly search connector alongside arXiv and Crossref.
- Added local research-profile recommendations, cross-topic relationship explanations, and global feedback memory.
- Added schema 4 with secret-free audit events for paper, query, sync, feedback, import, attachment, model, and backup operations.
- Added `/api/health`, `/api/ready`, and `/api/audit` operator endpoints with request IDs and structured JSON request logs.
- Added operational metrics for feedback coverage, sync latency, audit volume, and database schema version.
- Added repository quality gates, Windows CI, project metadata, and migration-safe backup restore checks.

## 0.3.0

- Added OpenAI-compatible model configuration, analytics dashboard, OpenAlex citation hints, and persistent topic memory.

