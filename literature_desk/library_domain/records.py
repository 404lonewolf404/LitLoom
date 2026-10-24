"""Compatibility facade for library records and maintenance workflows."""

from .memory import (
    _research_memory,
)
from .queries import (
    paper_connections,
    paper_dict,
    _hydrate_retrieval_metadata,
    paper_detail,
    refresh_search_content,
    rebuild_search_index,
    search_library,
)
from .maintenance import (
    maintenance_status,
    get_backup_policy,
    save_backup_policy,
    prune_backup_archives,
    create_backup_archive,
    scheduled_backup_tick,
)
from .paper_records import (
    paper_history,
    reading_card_dict,
    record_paper_revision,
    save_reading_card,
    batch_update_papers,
    delete_papers,
    attach_source,
    find_existing_paper,
    upsert_paper,
)

__all__ = ['_research_memory', 'paper_connections', 'paper_dict', '_hydrate_retrieval_metadata', 'paper_detail', 'refresh_search_content', 'rebuild_search_index', 'search_library', 'maintenance_status', 'get_backup_policy', 'save_backup_policy', 'prune_backup_archives', 'create_backup_archive', 'scheduled_backup_tick', 'paper_history', 'reading_card_dict', 'record_paper_revision', 'save_reading_card', 'batch_update_papers', 'delete_papers', 'attach_source', 'find_existing_paper', 'upsert_paper']
