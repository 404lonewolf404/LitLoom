"""SQLite schema initialization and migrations."""

from __future__ import annotations

from .services import app_services as _services


def init_db() -> None:
    _services().STARTUP_INTERRUPTED_SYNC_IDS = []
    if _services().using_postgres():
        _initialize_postgres_db()
        return
    with _services().DB_LOCK, _services().db_session() as db:
        db.executescript(
            """
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_key TEXT NOT NULL,
            authors TEXT NOT NULL DEFAULT '',
            year INTEGER,
            abstract TEXT NOT NULL DEFAULT '',
            doi TEXT NOT NULL DEFAULT '',
            arxiv_id TEXT NOT NULL DEFAULT '',
            venue TEXT NOT NULL DEFAULT '',
            url TEXT NOT NULL DEFAULT '',
            pdf_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'inbox',
            topic TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            notes TEXT NOT NULL DEFAULT '',
            is_favorite INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS paper_title_key_idx ON papers(title_key);
        CREATE INDEX IF NOT EXISTS paper_status_idx ON papers(status);
        CREATE INDEX IF NOT EXISTS paper_year_idx ON papers(year);
        CREATE TABLE IF NOT EXISTS source_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '',
            query_id TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(paper_id, source, external_id, query_id)
        );
        CREATE INDEX IF NOT EXISTS source_records_paper_source_idx ON source_records(paper_id,source,fetched_at DESC);
        CREATE INDEX IF NOT EXISTS source_records_source_external_idx ON source_records(source,external_id);
        CREATE TABLE IF NOT EXISTS saved_queries (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            query TEXT NOT NULL,
            sources_json TEXT NOT NULL DEFAULT '["arxiv"]',
            search_options_json TEXT NOT NULL DEFAULT '{}',
            cadence_hours INTEGER NOT NULL DEFAULT 168,
            active INTEGER NOT NULL DEFAULT 1,
            last_run_at TEXT NOT NULL DEFAULT '',
            next_run_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            query_id TEXT NOT NULL DEFAULT '',
            query_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            new_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            sources_json TEXT NOT NULL DEFAULT '[]',
            source_details_json TEXT NOT NULL DEFAULT '[]',
            error TEXT NOT NULL DEFAULT '',
            is_scheduled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS run_snapshots (
            run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
            query TEXT NOT NULL DEFAULT '',
            plan_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            candidate_key TEXT NOT NULL DEFAULT '',
            record_json TEXT NOT NULL DEFAULT '{}',
            imported_paper_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS discovery_candidates_run_idx ON discovery_candidates(run_id);
        -- Candidate discovery is deliberately separate from the library.  A
        -- screening job only annotates this queue; it never imports or
        -- deletes a paper on the user's behalf.
        CREATE TABLE IF NOT EXISTS discovery_triage_jobs (
            run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'queued',
            model TEXT NOT NULL DEFAULT '',
            candidate_count INTEGER NOT NULL DEFAULT 0,
            processed_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS discovery_triage_jobs_status_idx ON discovery_triage_jobs(status,updated_at DESC);
        -- Feedback is keyed by a stable research intent and external paper
        -- identity, so a temporary candidate UUID cannot make an unwanted
        -- paper reappear as a completely new recommendation next time.
        CREATE TABLE IF NOT EXISTS discovery_candidate_feedback (
            intent_key TEXT NOT NULL,
            candidate_key TEXT NOT NULL,
            decision TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            candidate_title TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(intent_key,candidate_key)
        );
        CREATE INDEX IF NOT EXISTS discovery_candidate_feedback_intent_idx ON discovery_candidate_feedback(intent_key,updated_at DESC);
        CREATE TABLE IF NOT EXISTS discovery_search_memory (
            intent_key TEXT NOT NULL,
            identity_key TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            last_run_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(intent_key,identity_key)
        );
        CREATE INDEX IF NOT EXISTS discovery_search_memory_intent_seen_idx ON discovery_search_memory(intent_key,last_seen_at DESC);
        CREATE TABLE IF NOT EXISTS research_goals (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            objective TEXT NOT NULL,
            core_question TEXT NOT NULL DEFAULT '',
            scope_json TEXT NOT NULL DEFAULT '{}',
            time_range_json TEXT NOT NULL DEFAULT '{}',
            focus_json TEXT NOT NULL DEFAULT '[]',
            exclusions_json TEXT NOT NULL DEFAULT '[]',
            deliverables_json TEXT NOT NULL DEFAULT '[]',
            success_criteria_json TEXT NOT NULL DEFAULT '[]',
            stop_conditions_json TEXT NOT NULL DEFAULT '[]',
            sources_json TEXT NOT NULL DEFAULT '["arxiv","openalex","crossref"]',
            workflow TEXT NOT NULL DEFAULT 'research_map',
            status TEXT NOT NULL DEFAULT 'draft',
            current_stage TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT NOT NULL DEFAULT '',
            next_run_at TEXT NOT NULL DEFAULT '',
            cadence_hours INTEGER NOT NULL DEFAULT 168,
            auto_run INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS research_goals_status_idx ON research_goals(status,updated_at);
        CREATE TABLE IF NOT EXISTS research_goal_papers (
            goal_id TEXT NOT NULL REFERENCES research_goals(id) ON DELETE CASCADE,
            paper_key TEXT NOT NULL,
            local_paper_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            doi TEXT NOT NULL DEFAULT '',
            arxiv_id TEXT NOT NULL DEFAULT '',
            version_key TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            first_run_id TEXT NOT NULL DEFAULT '',
            last_run_id TEXT NOT NULL DEFAULT '',
            user_decision TEXT NOT NULL DEFAULT 'new',
            reading_status TEXT NOT NULL DEFAULT 'unread',
            change_summary TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(goal_id,paper_key)
        );
        CREATE INDEX IF NOT EXISTS research_goal_papers_seen_idx ON research_goal_papers(goal_id,last_seen_at);
        CREATE TABLE IF NOT EXISTS paper_version_links (
            canonical_key TEXT NOT NULL,
            paper_key TEXT NOT NULL,
            relation TEXT NOT NULL DEFAULT 'same_work',
            confidence REAL NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(canonical_key,paper_key)
        );
        CREATE INDEX IF NOT EXISTS paper_version_links_paper_idx ON paper_version_links(paper_key);
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES research_goals(id) ON DELETE CASCADE,
            workflow TEXT NOT NULL DEFAULT 'research_map',
            status TEXT NOT NULL DEFAULT 'queued',
            current_step_id TEXT NOT NULL DEFAULT '',
            trigger TEXT NOT NULL DEFAULT 'manual',
            plan_json TEXT NOT NULL DEFAULT '{}',
            budget_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            duration_ms REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS agent_runs_goal_idx ON agent_runs(goal_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS agent_runs_status_idx ON agent_runs(status,updated_at);
        CREATE TABLE IF NOT EXISTS agent_steps (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_key TEXT NOT NULL,
            agent_type TEXT NOT NULL,
            title TEXT NOT NULL,
            tool_name TEXT NOT NULL DEFAULT '',
            tool_class TEXT NOT NULL DEFAULT 'Read',
            ordinal INTEGER NOT NULL DEFAULT 0,
            dependencies_json TEXT NOT NULL DEFAULT '[]',
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            input_schema_json TEXT NOT NULL DEFAULT '{}',
            output_schema_json TEXT NOT NULL DEFAULT '{}',
            completion_json TEXT NOT NULL DEFAULT '{}',
            failure_policy_json TEXT NOT NULL DEFAULT '{}',
            budget_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            requires_confirmation INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 2,
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id,step_key)
        );
        CREATE INDEX IF NOT EXISTS agent_steps_run_idx ON agent_steps(run_id,ordinal);
        CREATE TABLE IF NOT EXISTS agent_tool_calls (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL REFERENCES agent_steps(id) ON DELETE CASCADE,
            tool_name TEXT NOT NULL,
            tool_class TEXT NOT NULL DEFAULT 'Read',
            status TEXT NOT NULL DEFAULT 'running',
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            duration_ms REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS agent_tool_calls_run_idx ON agent_tool_calls(run_id,started_at DESC);
        CREATE TABLE IF NOT EXISTS agent_artifacts (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES research_goals(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL REFERENCES agent_steps(id) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content_json TEXT NOT NULL DEFAULT '{}',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS agent_artifacts_goal_idx ON agent_artifacts(goal_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS research_claims (
            id TEXT PRIMARY KEY,
            goal_id TEXT NOT NULL REFERENCES research_goals(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            paper_id TEXT NOT NULL DEFAULT '',
            claim_text TEXT NOT NULL,
            claim_type TEXT NOT NULL DEFAULT 'model_synthesis',
            status TEXT NOT NULL DEFAULT 'needs_verification',
            confidence TEXT NOT NULL DEFAULT 'medium',
            evidence_scope TEXT NOT NULL DEFAULT 'unknown',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS research_claims_goal_idx ON research_claims(goal_id,status,updated_at);
        CREATE TABLE IF NOT EXISTS claim_critiques (
            id TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
            goal_id TEXT NOT NULL REFERENCES research_goals(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'medium',
            critique_text TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS claim_critiques_goal_idx ON claim_critiques(goal_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS evidence_spans (
            id TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL REFERENCES research_claims(id) ON DELETE CASCADE,
            paper_id TEXT NOT NULL DEFAULT '',
            attachment_id TEXT NOT NULL DEFAULT '',
            page_number INTEGER,
            section TEXT NOT NULL DEFAULT '',
            quote TEXT NOT NULL DEFAULT '',
            evidence_type TEXT NOT NULL DEFAULT 'text',
            verification_status TEXT NOT NULL DEFAULT 'unverified',
            source_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS evidence_spans_claim_idx ON evidence_spans(claim_id,page_number);
        CREATE TABLE IF NOT EXISTS run_papers (
            run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            search_rank INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(run_id,paper_id)
        );
        CREATE INDEX IF NOT EXISTS run_papers_rank_idx ON run_papers(run_id,search_rank);
        CREATE TABLE IF NOT EXISTS query_matches (
            query_id TEXT NOT NULL,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            first_run_id TEXT NOT NULL DEFAULT '',
            last_run_id TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            tier TEXT NOT NULL DEFAULT '低相关',
            explanation_json TEXT NOT NULL DEFAULT '[]',
            score_version TEXT NOT NULL DEFAULT 'triage-v1',
            user_decision TEXT NOT NULL DEFAULT 'new',
            viewed_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(query_id, paper_id)
        );
        CREATE INDEX IF NOT EXISTS query_matches_last_seen_idx ON query_matches(query_id,last_seen_at);
        CREATE TABLE IF NOT EXISTS citation_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            target_external_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source_paper_id,target_external_id,provider)
        );
        CREATE INDEX IF NOT EXISTS citation_edges_source_idx ON citation_edges(source_paper_id);
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_artifacts (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            artifact_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learning_feedback (
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            scope TEXT NOT NULL DEFAULT 'global',
            decision TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            PRIMARY KEY(paper_id, scope)
        );
        CREATE INDEX IF NOT EXISTS learning_feedback_decision_idx ON learning_feedback(decision);
        CREATE TABLE IF NOT EXISTS study_items (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            item_type TEXT NOT NULL,
            item_key TEXT NOT NULL,
            label TEXT NOT NULL,
            expansion TEXT NOT NULL DEFAULT '',
            explanation TEXT NOT NULL DEFAULT '',
            paper_role TEXT NOT NULL DEFAULT '',
            basis TEXT NOT NULL DEFAULT '',
            necessity TEXT NOT NULL DEFAULT '',
            page_number INTEGER,
            quote TEXT NOT NULL DEFAULT '',
            mastery TEXT NOT NULL DEFAULT 'new',
            review_due_at TEXT NOT NULL DEFAULT '',
            review_interval_days INTEGER NOT NULL DEFAULT 0,
            review_count INTEGER NOT NULL DEFAULT 0,
            review_lapses INTEGER NOT NULL DEFAULT 0,
            review_ease REAL NOT NULL DEFAULT 2.5,
            last_reviewed_at TEXT NOT NULL DEFAULT '',
            user_note TEXT NOT NULL DEFAULT '',
            source_artifact_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(paper_id,item_type,item_key)
        );
        CREATE INDEX IF NOT EXISTS study_items_mastery_idx ON study_items(mastery,item_type,updated_at DESC);
        CREATE INDEX IF NOT EXISTS study_items_paper_idx ON study_items(paper_id,item_type,label);
        CREATE TABLE IF NOT EXISTS reading_cards (
            paper_id TEXT PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
            research_question TEXT NOT NULL DEFAULT '',
            contribution TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT '',
            datasets TEXT NOT NULL DEFAULT '',
            baselines TEXT NOT NULL DEFAULT '',
            results TEXT NOT NULL DEFAULT '',
            limitations TEXT NOT NULL DEFAULT '',
            reproducibility TEXT NOT NULL DEFAULT '',
            evidence_notes TEXT NOT NULL DEFAULT '',
            relevance_to_me TEXT NOT NULL DEFAULT '',
            next_steps TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reading_cards_updated_idx ON reading_cards(updated_at);
        CREATE TABLE IF NOT EXISTS document_pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attachment_id TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            page_number INTEGER NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            char_count INTEGER NOT NULL DEFAULT 0,
            quality TEXT NOT NULL DEFAULT 'empty',
            extractor TEXT NOT NULL DEFAULT '',
            text_sha256 TEXT NOT NULL DEFAULT '',
            extracted_at TEXT NOT NULL,
            UNIQUE(attachment_id, page_number)
        );
        CREATE INDEX IF NOT EXISTS document_pages_paper_idx ON document_pages(paper_id,page_number);
        CREATE INDEX IF NOT EXISTS document_pages_search_idx ON document_pages(paper_id,quality);
        -- Stable, page-anchored reading units used by the side-by-side reader.
        -- The source text is kept with the annotation so an explanation can
        -- always be audited even after the PDF extraction is refreshed.
        CREATE TABLE IF NOT EXISTS paper_segment_annotations (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            segment_key TEXT NOT NULL,
            attachment_id TEXT NOT NULL DEFAULT '',
            page_number INTEGER NOT NULL,
            paragraph_index INTEGER NOT NULL DEFAULT 0,
            source_text TEXT NOT NULL DEFAULT '',
            analysis_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'completed',
            model TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(paper_id,segment_key)
        );
        CREATE INDEX IF NOT EXISTS paper_segment_annotations_paper_idx ON paper_segment_annotations(paper_id,page_number,paragraph_index);
        CREATE TABLE IF NOT EXISTS document_page_embeddings (
            page_id INTEGER NOT NULL REFERENCES document_pages(id) ON DELETE CASCADE,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            text_sha256 TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL,
            start_char INTEGER NOT NULL,
            end_char INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(page_id,chunk_index,model)
        );
        CREATE INDEX IF NOT EXISTS document_page_embeddings_paper_model_idx ON document_page_embeddings(paper_id,model,page_id);
        CREATE TABLE IF NOT EXISTS semantic_index_jobs (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            force_rebuild INTEGER NOT NULL DEFAULT 0,
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            failed_pages INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS semantic_index_jobs_paper_idx ON semantic_index_jobs(paper_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS semantic_index_jobs_status_idx ON semantic_index_jobs(status,updated_at);
        CREATE UNIQUE INDEX IF NOT EXISTS semantic_index_active_paper_idx ON semantic_index_jobs(paper_id) WHERE status IN ('queued','running');
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT '',
            entity_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS audit_events_created_idx ON audit_events(created_at);
        CREATE INDEX IF NOT EXISTS audit_events_type_idx ON audit_events(event_type);
        CREATE TABLE IF NOT EXISTS workspaces (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_memberships (
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('owner','admin','researcher','reviewer','viewer')),
            created_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id,user_id)
        );
        CREATE INDEX IF NOT EXISTS workspace_memberships_user_idx ON workspace_memberships(user_id,workspace_id);
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            csrf_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS auth_login_attempts (
            identity_hash TEXT PRIMARY KEY,
            window_started_at TEXT NOT NULL,
            failure_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS model_config (
            id INTEGER PRIMARY KEY CHECK(id=1),
            provider TEXT NOT NULL DEFAULT '',
            base_url TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT 'gpt-4.1-mini',
            fast_model TEXT NOT NULL DEFAULT '',
            analysis_model TEXT NOT NULL DEFAULT '',
            vision_model TEXT NOT NULL DEFAULT '',
            embedding_model TEXT NOT NULL DEFAULT '',
            embedding_api_url TEXT NOT NULL DEFAULT '',
            deepseek_search_enabled INTEGER NOT NULL DEFAULT 0,
            deepseek_search_api_url TEXT NOT NULL DEFAULT '',
            deepseek_fetch_api_url TEXT NOT NULL DEFAULT '',
            temperature REAL NOT NULL DEFAULT 0.2,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS model_usage_daily (
            day_utc TEXT NOT NULL,
            task TEXT NOT NULL,
            model TEXT NOT NULL,
            endpoint_host TEXT NOT NULL DEFAULT '',
            modality TEXT NOT NULL DEFAULT 'text',
            calls INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            token_calls INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            cost_calls INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            latency_sum_ms REAL NOT NULL DEFAULT 0,
            latency_max_ms REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(day_utc,task,model,endpoint_host,modality)
        );
        CREATE TABLE IF NOT EXISTS model_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            model TEXT NOT NULL,
            endpoint_host TEXT NOT NULL DEFAULT '',
            modality TEXT NOT NULL DEFAULT 'text',
            status TEXT NOT NULL,
            duration_ms REAL NOT NULL DEFAULT 0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            error_type TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS model_usage_events_created_idx ON model_usage_events(created_at DESC);
        CREATE INDEX IF NOT EXISTS model_usage_events_task_idx ON model_usage_events(task,created_at DESC);
        INSERT OR IGNORE INTO model_config(id,provider,base_url,model,temperature,enabled,updated_at)
            VALUES(1,'','','gpt-4.1-mini',0.2,0,'');
        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
        )
        db.executescript(
            """
        CREATE TABLE IF NOT EXISTS paper_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            revision_type TEXT NOT NULL,
            content_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS paper_revisions_history_idx ON paper_revisions(paper_id,created_at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS sync_jobs (
            id TEXT PRIMARY KEY,
            query_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sync_jobs_status_idx ON sync_jobs(status,next_retry_at);
        CREATE TABLE IF NOT EXISTS ai_jobs (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            artifact_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            options_json TEXT NOT NULL DEFAULT '{}',
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ai_jobs_paper_idx ON ai_jobs(paper_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS ai_jobs_status_idx ON ai_jobs(status,updated_at);
        CREATE TABLE IF NOT EXISTS metadata_draft_jobs (
            id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 2,
            message TEXT NOT NULL DEFAULT '',
            input_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS metadata_draft_jobs_hash_idx ON metadata_draft_jobs(request_hash,created_at DESC);
        CREATE INDEX IF NOT EXISTS metadata_draft_jobs_status_idx ON metadata_draft_jobs(status,updated_at DESC);
        -- PDF ingestion is durable and independent from browser state.  A
        -- batch can therefore continue after a refresh or a service restart.
        CREATE TABLE IF NOT EXISTS pdf_ingest_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            total INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            duplicates INTEGER NOT NULL DEFAULT 0,
            ai_completed INTEGER NOT NULL DEFAULT 0,
            ai_failed INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            options_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS pdf_ingest_jobs_status_idx ON pdf_ingest_jobs(status,updated_at DESC);
        CREATE TABLE IF NOT EXISTS pdf_ingest_items (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES pdf_ingest_jobs(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            source_path TEXT NOT NULL DEFAULT '',
            source_mtime REAL,
            source_size INTEGER,
            staged_path TEXT NOT NULL DEFAULT '',
            duplicate INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            paper_id TEXT NOT NULL DEFAULT '',
            attachment_id TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            page_count INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            metadata_source TEXT NOT NULL DEFAULT '',
            ai_status TEXT NOT NULL DEFAULT 'skipped',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pdf_ingest_items_job_idx ON pdf_ingest_items(job_id,created_at);
        CREATE INDEX IF NOT EXISTS pdf_ingest_items_source_idx ON pdf_ingest_items(source_path,source_mtime,source_size);
        CREATE TABLE IF NOT EXISTS paper_folder_sources (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            recursive INTEGER NOT NULL DEFAULT 1,
            auto_ai INTEGER NOT NULL DEFAULT 1,
            interval_minutes INTEGER NOT NULL DEFAULT 30,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_scan_at TEXT NOT NULL DEFAULT '',
            next_scan_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS paper_folder_sources_due_idx ON paper_folder_sources(enabled,next_scan_at);
        CREATE TABLE IF NOT EXISTS paper_search_content (
            rowid INTEGER PRIMARY KEY,
            paper_id TEXT NOT NULL UNIQUE REFERENCES papers(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT '', authors TEXT NOT NULL DEFAULT '', abstract TEXT NOT NULL DEFAULT '',
            topic TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            reading_card TEXT NOT NULL DEFAULT '', pdf_text TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS paper_page_search_content (
            rowid INTEGER PRIMARY KEY,
            page_id INTEGER NOT NULL UNIQUE REFERENCES document_pages(id) ON DELETE CASCADE,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            text TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS paper_page_search_paper_idx ON paper_page_search_content(paper_id);
        """
        )
        db.execute(
            "INSERT OR IGNORE INTO app_meta(key,value) VALUES (?,?)",
            ("schema_version", str(_services().DB_SCHEMA_VERSION)),
        )
        _services()._auth_workspace(db)
        _services()._ensure_auth_bootstrap_token(db)
        schema_row = db.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
        try:
            current_schema = int(schema_row[0]) if schema_row else 0
        except (TypeError, ValueError):
            current_schema = 0
        study_columns = {
            row[1] for row in db.execute("PRAGMA table_info(study_items)").fetchall()
        }
        study_review_columns = {
            "review_due_at": "TEXT NOT NULL DEFAULT ''",
            "review_interval_days": "INTEGER NOT NULL DEFAULT 0",
            "review_count": "INTEGER NOT NULL DEFAULT 0",
            "review_lapses": "INTEGER NOT NULL DEFAULT 0",
            "review_ease": "REAL NOT NULL DEFAULT 2.5",
            "last_reviewed_at": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in study_review_columns.items():
            if column not in study_columns:
                db.execute(f"ALTER TABLE study_items ADD COLUMN {column} {declaration}")
        db.execute(
            "CREATE INDEX IF NOT EXISTS study_items_review_due_idx ON study_items(review_due_at,mastery)"
        )
        # Older schema 5 databases were created before saved query filters
        # became persistent.  SQLite has no IF NOT EXISTS for ALTER TABLE.
        query_columns = {
            row[1] for row in db.execute("PRAGMA table_info(saved_queries)").fetchall()
        }
        if "search_options_json" not in query_columns:
            db.execute(
                "ALTER TABLE saved_queries ADD COLUMN search_options_json TEXT NOT NULL DEFAULT '{}'"
            )
        ingest_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(pdf_ingest_items)").fetchall()
        }
        if ingest_columns and "staged_path" not in ingest_columns:
            db.execute(
                "ALTER TABLE pdf_ingest_items ADD COLUMN staged_path TEXT NOT NULL DEFAULT ''"
            )
        if ingest_columns and "duplicate" not in ingest_columns:
            db.execute(
                "ALTER TABLE pdf_ingest_items ADD COLUMN duplicate INTEGER NOT NULL DEFAULT 0"
            )
        run_columns = {
            row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "source_details_json" not in run_columns:
            db.execute(
                "ALTER TABLE runs ADD COLUMN source_details_json TEXT NOT NULL DEFAULT '[]'"
            )
        goal_columns = {
            row[1] for row in db.execute("PRAGMA table_info(research_goals)").fetchall()
        }
        for name, definition in (
            ("cadence_hours", "INTEGER NOT NULL DEFAULT 168"),
            ("auto_run", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if goal_columns and name not in goal_columns:
                db.execute(f"ALTER TABLE research_goals ADD COLUMN {name} {definition}")
        model_columns = {
            row[1] for row in db.execute("PRAGMA table_info(model_config)").fetchall()
        }
        for name, definition in (
            ("website_url", "TEXT NOT NULL DEFAULT ''"),
            ("api_url", "TEXT NOT NULL DEFAULT ''"),
            ("protocol", "TEXT NOT NULL DEFAULT 'chat_completions'"),
            ("embedding_model", "TEXT NOT NULL DEFAULT ''"),
            ("embedding_api_url", "TEXT NOT NULL DEFAULT ''"),
            ("fast_model", "TEXT NOT NULL DEFAULT ''"),
            ("analysis_model", "TEXT NOT NULL DEFAULT ''"),
            ("vision_model", "TEXT NOT NULL DEFAULT ''"),
            ("deepseek_search_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("deepseek_search_api_url", "TEXT NOT NULL DEFAULT ''"),
            ("deepseek_fetch_api_url", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in model_columns:
                db.execute(f"ALTER TABLE model_config ADD COLUMN {name} {definition}")
        semantic_job_columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(semantic_index_jobs)").fetchall()
        }
        if semantic_job_columns and "force_rebuild" not in semantic_job_columns:
            db.execute(
                "ALTER TABLE semantic_index_jobs ADD COLUMN force_rebuild INTEGER NOT NULL DEFAULT 0"
            )
        ai_job_columns = {
            row[1] for row in db.execute("PRAGMA table_info(ai_jobs)").fetchall()
        }
        if ai_job_columns and "duration_ms" not in ai_job_columns:
            db.execute(
                "ALTER TABLE ai_jobs ADD COLUMN duration_ms REAL NOT NULL DEFAULT 0"
            )
        agent_step_columns = {
            row[1] for row in db.execute("PRAGMA table_info(agent_steps)").fetchall()
        }
        for name, definition in (
            ("progress_current", "INTEGER NOT NULL DEFAULT 0"),
            ("progress_total", "INTEGER NOT NULL DEFAULT 0"),
            ("message", "TEXT NOT NULL DEFAULT ''"),
            ("input_schema_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("output_schema_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("completion_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("failure_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("budget_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if agent_step_columns and name not in agent_step_columns:
                db.execute(f"ALTER TABLE agent_steps ADD COLUMN {name} {definition}")
        candidate_columns = {
            row[1] for row in db.execute("PRAGMA table_info(discovery_candidates)")
        }
        if "candidate_key" not in candidate_columns:
            db.execute(
                "ALTER TABLE discovery_candidates ADD COLUMN candidate_key TEXT NOT NULL DEFAULT ''"
            )
        db.execute(
            "CREATE INDEX IF NOT EXISTS discovery_candidates_identity_idx ON discovery_candidates(candidate_key,run_id)"
        )
        missing_keys = db.execute(
            "SELECT id,record_json FROM discovery_candidates WHERE candidate_key=''"
        ).fetchall()
        db.executemany(
            "UPDATE discovery_candidates SET candidate_key=? WHERE id=?",
            [
                (
                    _services().candidate_identity(
                        _services()._json_object(row["record_json"])
                    ),
                    row["id"],
                )
                for row in missing_keys
            ],
        )
        # Repair legacy candidate payloads once at startup.  Older searches
        # stored the entire candidate under ``raw``; history refill then
        # nested that payload again on every repeated search.  Keep the
        # durable row small and non-recursive so years of history remain fast.
        legacy_raw_rows = db.execute(
            "SELECT id,record_json FROM discovery_candidates WHERE record_json LIKE '%\"raw\"%' LIMIT 10000"
        ).fetchall()
        compact_updates = []
        for legacy_row in legacy_raw_rows:
            legacy_record = _services()._json_object(legacy_row["record_json"])
            if isinstance(legacy_record.get("raw"), dict):
                compact_record = _services().compact_candidate_record(legacy_record)
                if compact_record.get("raw") != legacy_record.get("raw"):
                    compact_updates.append(
                        (
                            _services().json.dumps(compact_record, ensure_ascii=False),
                            legacy_row["id"],
                        )
                    )
        if compact_updates:
            db.executemany(
                "UPDATE discovery_candidates SET record_json=? WHERE id=?",
                compact_updates,
            )
        if current_schema < 11:
            historic_candidates = db.execute(
                """SELECT dc.record_json,dc.run_id,dc.created_at,rs.query,rs.plan_json,rs.summary_json
                   FROM discovery_candidates dc LEFT JOIN run_snapshots rs ON rs.run_id=dc.run_id
                   ORDER BY dc.created_at"""
            ).fetchall()
            for historic in historic_candidates:
                record = _services()._json_object(historic["record_json"])
                plan = _services()._json_object(historic["plan_json"])
                summary = _services()._json_object(historic["summary_json"])
                intent_key = _services().clean_text(
                    record.get("intent_key")
                    or summary.get("intent_key")
                    or plan.get("discovery_intent_key"),
                    100,
                )
                if not intent_key and historic["query"]:
                    intent_key = _services().discovery_intent_key(
                        historic["query"],
                        plan.get("filters") or plan.get("search_options") or {},
                    )
                _services().record_discovery_search_memory(
                    db, intent_key, record, historic["run_id"], historic["created_at"]
                )
        # A triage worker is in-process and cannot survive a restart.  Keep
        # the already annotated candidates, but make the interrupted job
        # explicitly retryable instead of permanently displaying “running”.
        db.execute(
            "UPDATE discovery_triage_jobs SET status='interrupted', "
            "error=CASE WHEN error='' THEN '应用重启前的 AI 筛选未完成，可重新尝试。' ELSE error END, "
            "updated_at=?,completed_at=? WHERE status IN ('queued','running')",
            (_services().now_iso(), _services().now_iso()),
        )
        db.execute(
            "UPDATE semantic_index_jobs SET status='interrupted',stage='interrupted',message='应用重启前的索引任务未完成，可自动续跑',error='服务重启时任务未完成',updated_at=?,completed_at=? WHERE status IN ('queued','running')",
            (_services().now_iso(), _services().now_iso()),
        )
        fts5_available = True
        try:
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS paper_search USING fts5(title,authors,abstract,topic,tags,notes,reading_card,pdf_text,content='paper_search_content',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2')"
            )
        except _services().sqlite3.OperationalError:
            fts5_available = False
        if fts5_available:
            try:
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS paper_page_search USING fts5(text,content='paper_page_search_content',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2')"
                )
            except _services().sqlite3.OperationalError:
                fts5_available = False
        db.executescript(
            """
        CREATE TRIGGER IF NOT EXISTS papers_search_insert AFTER INSERT ON papers BEGIN
            INSERT INTO paper_search_content(paper_id,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
            VALUES(NEW.id,NEW.title,NEW.authors,NEW.abstract,NEW.topic,NEW.tags_json,NEW.notes,'', '');
        END;
        CREATE TRIGGER IF NOT EXISTS papers_search_update AFTER UPDATE OF title,authors,abstract,topic,tags_json,notes ON papers BEGIN
            UPDATE paper_search_content SET title=NEW.title,authors=NEW.authors,abstract=NEW.abstract,topic=NEW.topic,tags=NEW.tags_json,notes=NEW.notes WHERE paper_id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS papers_search_delete AFTER DELETE ON papers BEGIN
            DELETE FROM paper_search_content WHERE paper_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS reading_cards_search_insert AFTER INSERT ON reading_cards BEGIN
            UPDATE paper_search_content SET reading_card=NEW.research_question||' '||NEW.contribution||' '||NEW.method||' '||NEW.datasets||' '||NEW.baselines||' '||NEW.results||' '||NEW.limitations||' '||NEW.reproducibility||' '||NEW.evidence_notes||' '||NEW.relevance_to_me||' '||NEW.next_steps WHERE paper_id=NEW.paper_id;
        END;
        CREATE TRIGGER IF NOT EXISTS reading_cards_search_update AFTER UPDATE ON reading_cards BEGIN
            UPDATE paper_search_content SET reading_card=NEW.research_question||' '||NEW.contribution||' '||NEW.method||' '||NEW.datasets||' '||NEW.baselines||' '||NEW.results||' '||NEW.limitations||' '||NEW.reproducibility||' '||NEW.evidence_notes||' '||NEW.relevance_to_me||' '||NEW.next_steps WHERE paper_id=NEW.paper_id;
        END;
        CREATE TRIGGER IF NOT EXISTS reading_cards_search_delete AFTER DELETE ON reading_cards BEGIN
            UPDATE paper_search_content SET reading_card='' WHERE paper_id=OLD.paper_id;
        END;
        CREATE TRIGGER IF NOT EXISTS document_pages_search_insert AFTER INSERT ON document_pages BEGIN
            INSERT INTO paper_page_search_content(page_id,paper_id,text) VALUES(NEW.id,NEW.paper_id,NEW.text)
            ON CONFLICT(page_id) DO UPDATE SET paper_id=excluded.paper_id,text=excluded.text;
        END;
        CREATE TRIGGER IF NOT EXISTS document_pages_search_update AFTER UPDATE OF text,paper_id ON document_pages BEGIN
            UPDATE paper_page_search_content SET paper_id=NEW.paper_id,text=NEW.text WHERE page_id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS document_pages_search_delete AFTER DELETE ON document_pages BEGIN
            DELETE FROM paper_page_search_content WHERE page_id=OLD.id;
        END;
        """
        )
        interrupted_at = _services().now_iso()
        # Search workers are in-process threads. Neither a running search nor
        # a queued search can survive this startup, so reconcile both states
        # instead of leaving a stale run displayed as active forever.
        interrupted_jobs = db.execute(
            "SELECT id,query_id,run_id,status FROM sync_jobs WHERE status IN ('running','queued')"
        ).fetchall()
        _services().STARTUP_INTERRUPTED_SYNC_IDS = [
            _services().clean_text(row["id"], 120)
            for row in interrupted_jobs
            if _services().clean_text(row["id"], 120)
        ]
        for job in interrupted_jobs:
            error = (
                "服务重启时任务未完成"
                if job["status"] == "running"
                else "服务重启时任务尚未开始"
            )
            db.execute(
                "UPDATE sync_jobs SET status='interrupted',stage='interrupted',last_error=?,updated_at=? WHERE id=?",
                (error, interrupted_at, job["id"]),
            )
            db.execute(
                "UPDATE runs SET status='interrupted',completed_at=?,error=CASE WHEN error='' THEN ? ELSE error END WHERE id=? AND status=?",
                (interrupted_at, error, job["run_id"], job["status"]),
            )
            if job["query_id"]:
                db.execute(
                    "UPDATE saved_queries SET next_run_at=?,updated_at=? WHERE id=? AND active=1 AND (next_run_at='' OR next_run_at>?)",
                    (interrupted_at, interrupted_at, job["query_id"], interrupted_at),
                )
        db.execute(
            "UPDATE ai_jobs SET status='interrupted',stage='interrupted',"
            "error=CASE WHEN status='queued' THEN '服务重启时任务尚未开始' ELSE '服务重启时任务未完成' END,"
            "message=CASE WHEN status='queued' THEN '服务重启时任务尚未开始' ELSE '服务重启时任务未完成' END,"
            "updated_at=?,completed_at=? WHERE status IN ('queued','running')",
            (interrupted_at, interrupted_at),
        )
        # A nested ThreadPoolExecutor can race Python's global executor
        # shutdown hook if the process is terminated before our graceful
        # teardown runs. Older builds recorded this infrastructure event as a
        # permanent analysis failure, so recover only this precise error into
        # the normal durable restart path.
        db.execute(
            "UPDATE ai_jobs SET status='interrupted',stage='interrupted',"
            "error='服务重启时任务未完成',message='服务重启时任务未完成',"
            "updated_at=?,completed_at=? "
            "WHERE status IN ('failed','partial') "
            "AND lower(error) LIKE '%cannot schedule new futures after%shutdown%'",
            (interrupted_at, interrupted_at),
        )
        interrupted_agents = db.execute(
            "SELECT id FROM agent_runs WHERE status IN ('queued','running') AND error IN ('','服务重启时任务未完成')"
        ).fetchall()
        _services().STARTUP_INTERRUPTED_AGENT_IDS = [
            _services().clean_text(row["id"], 120)
            for row in interrupted_agents
            if _services().clean_text(row["id"], 120)
        ]
        db.execute(
            "UPDATE agent_runs SET status='interrupted',error='服务重启时任务未完成',updated_at=? WHERE status IN ('queued','running')",
            (interrupted_at,),
        )
        db.execute(
            "UPDATE agent_steps SET status='queued',error='服务重启时任务未完成',updated_at=? WHERE status='running' AND run_id IN (SELECT id FROM agent_runs WHERE status='interrupted')",
            (interrupted_at,),
        )
        if fts5_available:
            db.executescript(
                """
            CREATE TRIGGER IF NOT EXISTS paper_search_content_ai AFTER INSERT ON paper_search_content BEGIN
                INSERT INTO paper_search(rowid,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
                VALUES(NEW.rowid,NEW.title,NEW.authors,NEW.abstract,NEW.topic,NEW.tags,NEW.notes,NEW.reading_card,NEW.pdf_text);
            END;
            CREATE TRIGGER IF NOT EXISTS paper_search_content_ad AFTER DELETE ON paper_search_content BEGIN
                INSERT INTO paper_search(paper_search,rowid,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
                VALUES('delete',OLD.rowid,OLD.title,OLD.authors,OLD.abstract,OLD.topic,OLD.tags,OLD.notes,OLD.reading_card,OLD.pdf_text);
            END;
            CREATE TRIGGER IF NOT EXISTS paper_search_content_au AFTER UPDATE ON paper_search_content BEGIN
                INSERT INTO paper_search(paper_search,rowid,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
                VALUES('delete',OLD.rowid,OLD.title,OLD.authors,OLD.abstract,OLD.topic,OLD.tags,OLD.notes,OLD.reading_card,OLD.pdf_text);
                INSERT INTO paper_search(rowid,title,authors,abstract,topic,tags,notes,reading_card,pdf_text)
                VALUES(NEW.rowid,NEW.title,NEW.authors,NEW.abstract,NEW.topic,NEW.tags,NEW.notes,NEW.reading_card,NEW.pdf_text);
            END;
            CREATE TRIGGER IF NOT EXISTS paper_page_search_content_ai AFTER INSERT ON paper_page_search_content BEGIN
                INSERT INTO paper_page_search(rowid,text) VALUES(NEW.rowid,NEW.text);
            END;
            CREATE TRIGGER IF NOT EXISTS paper_page_search_content_ad AFTER DELETE ON paper_page_search_content BEGIN
                INSERT INTO paper_page_search(paper_page_search,rowid,text) VALUES('delete',OLD.rowid,OLD.text);
            END;
            CREATE TRIGGER IF NOT EXISTS paper_page_search_content_au AFTER UPDATE ON paper_page_search_content BEGIN
                INSERT INTO paper_page_search(paper_page_search,rowid,text) VALUES('delete',OLD.rowid,OLD.text);
                INSERT INTO paper_page_search(rowid,text) VALUES(NEW.rowid,NEW.text);
            END;
            """
            )
        for paper_row in db.execute(
            "SELECT id FROM papers WHERE id NOT IN (SELECT paper_id FROM paper_search_content)"
        ).fetchall():
            _services().refresh_search_content(db, paper_row["id"])
        if fts5_available and current_schema < _services().DB_SCHEMA_VERSION:
            db.execute("INSERT INTO paper_search(paper_search) VALUES('rebuild')")
            db.execute(
                "INSERT INTO paper_page_search(paper_page_search) VALUES('rebuild')"
            )
        for page_row in db.execute(
            "SELECT d.id,d.paper_id,d.text FROM document_pages d WHERE d.id NOT IN (SELECT page_id FROM paper_page_search_content)"
        ).fetchall():
            db.execute(
                "INSERT INTO paper_page_search_content(page_id,paper_id,text) VALUES(?,?,?)",
                (page_row["id"], page_row["paper_id"], page_row["text"] or ""),
            )
        if current_schema < _services().DB_SCHEMA_VERSION:
            for artifact in db.execute(
                """SELECT a.id,a.paper_id,a.answer FROM ai_artifacts a
                   WHERE a.artifact_type IN ('reading_card_draft','deep_read','prereading_guide')
                     AND a.id=(SELECT latest.id FROM ai_artifacts latest
                               WHERE latest.paper_id=a.paper_id
                                 AND latest.artifact_type IN ('reading_card_draft','deep_read','prereading_guide')
                               ORDER BY latest.created_at DESC LIMIT 1)"""
            ).fetchall():
                try:
                    saved = _services().json.loads(artifact["answer"] or "{}")
                    analysis = (
                        saved.get("analysis", {}) if isinstance(saved, dict) else {}
                    )
                    guide = (
                        analysis.get("prereading_guide", {})
                        if isinstance(analysis, dict)
                        else {}
                    )
                    _services().sync_prereading_study_items(
                        db,
                        artifact["paper_id"],
                        guide,
                        artifact["id"],
                        _services().now_iso(),
                    )
                except (TypeError, ValueError, _services().json.JSONDecodeError):
                    continue
            db.execute(
                "UPDATE app_meta SET value=? WHERE key='schema_version'",
                (str(_services().DB_SCHEMA_VERSION),),
            )
        # Rehydrate the in-memory circuit after every startup or verified
        # backup restore.  This prevents a just-rate-limited index from being
        # hammered again merely because the local service was restarted.
        _services()._load_persisted_public_cooldowns(db)

    # In isolated tenant mode, move the existing first workspace into its
    # private database once. The control database keeps only identities and
    # memberships; subsequent workspaces start with an empty domain store.
    if _services().tenant_isolation_enabled():
        with _services().DB_LOCK, _services().control_db_session() as control:
            workspace = control.execute(
                "SELECT id FROM workspaces ORDER BY created_at,id LIMIT 1"
            ).fetchone()
        if workspace:
            _services().ensure_workspace_database(workspace["id"], clone_current=True)


def _initialize_postgres_db() -> None:
    """Migrate an existing SQLite library once, then reconcile durable jobs."""
    database_url = _services().postgres_database_url()
    with _services().postgres_connect(database_url) as probe:
        table = _services().postgres_table_exists(probe, "app_meta")
    if not table:
        if _services().DB_PATH.is_file():
            source = _services().DB_PATH
        else:
            # A clean first run still uses the canonical SQLite schema builder
            # as an import format, then immediately converts it to PostgreSQL.
            source = _services().DATA_DIR / "postgres-bootstrap.sqlite3"
            previous_path = _services().DB_PATH
            previous_url = _services().os.environ.pop("LITLOOM_DATABASE_URL")
            try:
                _services().DB_PATH = source
                _services().init_db()
            finally:
                _services().DB_PATH = previous_path
                _services().os.environ["LITLOOM_DATABASE_URL"] = previous_url
        result = _services().postgres_migrate_source(source, database_url)
        _services().POSTGRES_MIGRATION_RESULT = result

    _services().postgres_apply_migrations(database_url)

    interrupted_at = _services().now_iso()
    with _services().DB_LOCK, _services().db_session() as db:
        schema = db.execute(
            "SELECT value FROM app_meta WHERE key='schema_version'"
        ).fetchone()
        if not schema or int(schema[0]) < _services().DB_SCHEMA_VERSION:
            raise RuntimeError(
                "PostgreSQL schema is older than this LitLoom version; apply database migrations before startup."
            )
        jobs = db.execute(
            "SELECT id,query_id,run_id,status FROM sync_jobs WHERE status IN ('running','queued')"
        ).fetchall()
        _services().STARTUP_INTERRUPTED_SYNC_IDS = [row["id"] for row in jobs]
        for job in jobs:
            error = "服务重启时任务未完成" if job["status"] == "running" else "服务重启时任务尚未开始"
            db.execute(
                "UPDATE sync_jobs SET status='interrupted',stage='interrupted',last_error=?,updated_at=? WHERE id=?",
                (error, interrupted_at, job["id"]),
            )
            db.execute(
                "UPDATE runs SET status='interrupted',completed_at=?,error=CASE WHEN error='' THEN ? ELSE error END WHERE id=? AND status=?",
                (interrupted_at, error, job["run_id"], job["status"]),
            )
            if job["query_id"]:
                db.execute(
                    "UPDATE saved_queries SET next_run_at=?,updated_at=? WHERE id=? AND active=1 AND (next_run_at='' OR next_run_at>?)",
                    (interrupted_at, interrupted_at, job["query_id"], interrupted_at),
                )
        db.execute(
            "UPDATE ai_jobs SET status='interrupted',stage='interrupted',error=CASE WHEN status='queued' THEN '服务重启时任务尚未开始' ELSE '服务重启时任务未完成' END,message=CASE WHEN status='queued' THEN '服务重启时任务尚未开始' ELSE '服务重启时任务未完成' END,updated_at=?,completed_at=? WHERE status IN ('queued','running')",
            (interrupted_at, interrupted_at),
        )
        agents = db.execute(
            "SELECT id FROM agent_runs WHERE status IN ('queued','running') AND error IN ('','服务重启时任务未完成')"
        ).fetchall()
        _services().STARTUP_INTERRUPTED_AGENT_IDS = [row["id"] for row in agents]
        db.execute(
            "UPDATE agent_runs SET status='interrupted',error='服务重启时任务未完成',updated_at=? WHERE status IN ('queued','running')",
            (interrupted_at,),
        )
        db.execute(
            "UPDATE agent_steps SET status='queued',error='服务重启时任务未完成',updated_at=? WHERE status='running' AND run_id IN (SELECT id FROM agent_runs WHERE status='interrupted')",
            (interrupted_at,),
        )


__all__ = ["init_db", "_initialize_postgres_db"]
