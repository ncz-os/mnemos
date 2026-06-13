# Cross-backend persistence coverage matrix

## MemoryRepository (23 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| assert_memory_readable | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_log | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_diff_commit_pair | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_checkout_commit | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_export | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_referenced_memory_allowlist | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_by_id | IMPL | IMPL | IMPL | IMPL | IMPL |
| set_suppress_version_snapshot | STUB | IMPL | STUB | STUB | IMPL |
| fetch_versioned_memory_ids | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_head_checks | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_context | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_memories | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| update_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| find_active_duplicate_by_content_hash | IMPL | IMPL | IMPL | IMPL | IMPL |
| bump_recall_and_get_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| find_duplicate_content_groups | IMPL | IMPL | IMPL | IMPL | IMPL |
| consolidate_duplicate_memories | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| semantic_search | IMPL | IMPL | IMPL | IMPL | IMPL |
| fts_search | IMPL | IMPL | IMPL | IMPL | IMPL |
| gather_stats | IMPL | IMPL | IMPL | IMPL | IMPL |

## KGRepository (3 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| fetch_kg_triples_for_export | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_kg_triple | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_kg_triple_by_id | IMPL | IMPL | IMPL | IMPL | IMPL |

## VersionRepository (4 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| fetch_memory_versions_for_export | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_versions_by_ids | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_memory_version | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_version_by_id | IMPL | IMPL | IMPL | IMPL | IMPL |

## BranchRepository (4 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| create_memory_branch | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete_memory_branches_for_memories | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_memory_branch_heads | IMPL | IMPL | IMPL | IMPL | IMPL |
| upsert_memory_branch_head | IMPL | IMPL | IMPL | IMPL | IMPL |

## CompressionRepository (5 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| fetch_compressed_variants_for_export | IMPL | IMPL | IMPL | IMPL | IMPL |
| compression_candidate_exists | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_compressed_variant | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_compressed_variant_by_memory_id | IMPL | IMPL | IMPL | IMPL | IMPL |
| gather_stats | IMPL | IMPL | IMPL | IMPL | IMPL |

## WebhookRepository (1 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| dispatch_event | IMPL | IMPL | IMPL | IMPL | IMPL |

## ConsultationAuditRepository (5 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| fetch_recommended_model | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_model_recommendation | IMPL | IMPL | IMPL | IMPL | IMPL |
| lookup_provider_for_model | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_available_models | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_model_provider | IMPL | IMPL | IMPL | IMPL | IMPL |

## OAuthRepository (7 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| list_enabled_providers | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_provider | IMPL | IMPL | IMPL | IMPL | IMPL |
| provision_or_link_user | IMPL | IMPL | IMPL | IMPL | IMPL |
| create_session | IMPL | IMPL | IMPL | IMPL | IMPL |
| revoke_session | IMPL | IMPL | IMPL | IMPL | IMPL |
| revoke_all_sessions | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_identity_for_session | IMPL | IMPL | IMPL | IMPL | IMPL |

## SessionsRepository (9 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| create_session | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_session | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_injected_memory_ids | IMPL | IMPL | IMPL | IMPL | IMPL |
| add_message | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_provider_history | IMPL | IMPL | IMPL | IMPL | IMPL |
| add_memory_injections | IMPL | IMPL | IMPL | IMPL | IMPL |
| update_metrics | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_history | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete_session | IMPL | IMPL | IMPL | IMPL | IMPL |

## ConsultationsRepository (7 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| resolve_tier_lineup | IMPL | IMPL | IMPL | IMPL | IMPL |
| resolve_models | IMPL | IMPL | IMPL | IMPL | IMPL |
| create_consultation_with_audit | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_audit_log | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_audit_chain | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_consultation | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_consultation_artifacts | IMPL | IMPL | IMPL | IMPL | IMPL |

## FederationRepository (23 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| fetch_memory_page | IMPL | IMPL | IMPL | IMPL | IMPL |
| create_peer | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_peers | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_peer | IMPL | IMPL | IMPL | IMPL | IMPL |
| update_peer | IMPL | IMPL | IMPL | IMPL | IMPL |
| upsert_peer | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete_peer | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_sync_log | IMPL | IMPL | IMPL | IMPL | IMPL |
| feed_query | IMPL | IMPL | IMPL | IMPL | RAISE |
| get_feed_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_sync_peer | IMPL | IMPL | IMPL | IMPL | IMPL |
| update_peer_schema_check | IMPL | IMPL | IMPL | IMPL | IMPL |
| record_schema_abort | IMPL | IMPL | IMPL | IMPL | IMPL |
| create_sync_log | IMPL | IMPL | IMPL | IMPL | IMPL |
| finish_sync_log | IMPL | IMPL | IMPL | IMPL | IMPL |
| record_sync_error | IMPL | IMPL | IMPL | IMPL | IMPL |
| record_sync_success | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_due_peers | IMPL | IMPL | IMPL | IMPL | IMPL |
| fetch_federated_memory_marker | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_federated_memory | IMPL | IMPL | IMPL | IMPL | IMPL |
| update_federated_memory_if_newer | IMPL | IMPL | IMPL | IMPL | IMPL |
| apply_consolidation_tombstone | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete_federated_memory | IMPL | IMPL | IMPL | IMPL | IMPL |

## StateRepository (5 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| get | IMPL | IMPL | IMPL | IMPL | IMPL |
| set | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_namespace | IMPL | IMPL | IMPL | IMPL | IMPL |
| delete_namespace | IMPL | IMPL | IMPL | IMPL | IMPL |

## AuditChainRepository (9 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| get_latest_audit_entry | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_audit_entry | IMPL | IMPL | IMPL | IMPL | IMPL |
| claim_unsealed_window | IMPL | IMPL | IMPL | IMPL | IMPL |
| stamp_window_with_root | IMPL | IMPL | IMPL | IMPL | IMPL |
| insert_audit_root | IMPL | IMPL | IMPL | IMPL | IMPL |
| list_window_entries | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_audit_entry_by_id | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_chain_stats | IMPL | IMPL | IMPL | IMPL | IMPL |
| get_latest_audit_entries_batch | IMPL | IMPL | IMPL | IMPL | IMPL |

## CompressionQueueRepository (6 methods)
| method | oracle | postgres | db2 | mysql | sqlite |
|---|---|---|---|---|---|
| enqueue_compression | IMPL | IMPL | IMPL | IMPL | IMPL |
| enqueue_all_compression | IMPL | IMPL | IMPL | IMPL | IMPL |
| dequeue_compression | IMPL | IMPL | IMPL | IMPL | IMPL |
| mark_compression_done | IMPL | IMPL | IMPL | IMPL | IMPL |
| mark_compression_failed | IMPL | IMPL | IMPL | IMPL | IMPL |
| sweep_stale_compression | IMPL | IMPL | IMPL | IMPL | IMPL |

## TOTALS (across all ABC repo methods)
| backend | IMPL | STUB | RAISE | absent(--) |
|---|---|---|---|---|
| oracle | 110 | 1 | 0 | 0 |
| postgres | 111 | 0 | 0 | 0 |
| db2 | 110 | 1 | 0 | 0 |
| mysql | 110 | 1 | 0 | 0 |
| sqlite | 110 | 0 | 1 | 0 |
