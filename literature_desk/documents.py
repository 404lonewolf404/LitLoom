"""Compatibility facade for attachment, ingestion, and document search APIs.

Implementations are grouped under :mod:`literature_desk.document_domain`; this
module retains the historical ``literature_desk.documents`` import path.
"""

from .document_domain.attachments import (
    extract_attachment_pages,
    extract_paper_documents,
    extraction_summary,
    store_pdf_attachment,
    pdf_local_metadata,
    _fill_paper_metadata,
    crossref_metadata,
    complete_paper_metadata_draft,
)
from .document_domain.metadata_jobs import (
    _metadata_draft_input,
    metadata_draft_job_snapshot,
    get_metadata_draft_job,
    _update_metadata_draft_job,
    run_metadata_draft_job,
    _mark_metadata_draft_interrupted_for_shutdown,
    _enqueue_metadata_draft_job,
    submit_metadata_draft_job,
    retry_metadata_draft_job,
    resume_metadata_draft_jobs,
)
from .document_domain.ingest_jobs import (
    _ingest_pdf_item,
    _run_pdf_ingest_job,
    _mark_pdf_ingest_interrupted_for_shutdown,
    _enqueue_pdf_ingest_job,
    submit_pdf_ingest_job,
    resume_pdf_ingest_jobs,
    create_pdf_ingest_job,
    pdf_ingest_job_snapshot,
    list_pdf_ingest_jobs,
    register_paper_folder,
    retry_pdf_ingest_job,
    scan_paper_folder,
)
from .document_domain.document_search import (
    _document_search_terms,
    _document_fts_expression,
    _document_casefold_span,
    _document_search_snippet,
    search_document_pages,
    _embedding_index_model,
    request_embeddings,
    semantic_index_job_snapshot,
    get_semantic_index_job,
    get_semantic_index_status,
    _semantic_job_cancelled,
    _update_semantic_index_job,
    run_semantic_index_job,
    _enqueue_semantic_index_job,
    submit_semantic_index_job,
    cancel_semantic_index_job,
    resume_semantic_index_jobs,
    search_document_pages_detailed,
)

__all__ = ['extract_attachment_pages', 'extract_paper_documents', 'extraction_summary', 'store_pdf_attachment', 'pdf_local_metadata', '_fill_paper_metadata', 'crossref_metadata', 'complete_paper_metadata_draft', '_metadata_draft_input', 'metadata_draft_job_snapshot', 'get_metadata_draft_job', '_update_metadata_draft_job', 'run_metadata_draft_job', '_mark_metadata_draft_interrupted_for_shutdown', '_enqueue_metadata_draft_job', 'submit_metadata_draft_job', 'retry_metadata_draft_job', 'resume_metadata_draft_jobs', '_ingest_pdf_item', '_run_pdf_ingest_job', '_mark_pdf_ingest_interrupted_for_shutdown', '_enqueue_pdf_ingest_job', 'submit_pdf_ingest_job', 'resume_pdf_ingest_jobs', 'create_pdf_ingest_job', 'pdf_ingest_job_snapshot', 'list_pdf_ingest_jobs', 'register_paper_folder', 'retry_pdf_ingest_job', 'scan_paper_folder', '_document_search_terms', '_document_fts_expression', '_document_casefold_span', '_document_search_snippet', 'search_document_pages', '_embedding_index_model', 'request_embeddings', 'semantic_index_job_snapshot', 'get_semantic_index_job', 'get_semantic_index_status', '_semantic_job_cancelled', '_update_semantic_index_job', 'run_semantic_index_job', '_enqueue_semantic_index_job', 'submit_semantic_index_job', 'cancel_semantic_index_job', 'resume_semantic_index_jobs', 'search_document_pages_detailed']
