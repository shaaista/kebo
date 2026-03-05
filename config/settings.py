from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_name: str = "KePSLA Bot v2"
    app_env: str = "development"
    debug: bool = True
    secret_key: str = "change-me-in-production"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # API Gateway
    api_gateway_auth_enabled: bool = False
    api_gateway_api_key: str = ""
    api_gateway_rate_limit_enabled: bool = True
    api_gateway_rate_limit_requests: int = 80
    api_gateway_rate_limit_window_seconds: int = 60

    # Observability / Evaluation
    observability_enabled: bool = True
    observability_log_file: str = "./logs/observability.log"
    evaluation_metrics_enabled: bool = True
    conversation_audit_enabled: bool = True
    conversation_audit_log_file: str = "./logs/conversation_audit.jsonl"

    # Database (SQLite for dev, PostgreSQL for production)
    database_url: str = "sqlite+aiosqlite:///./kepsla_bot.db"
    database_echo: bool = False
    admin_db_fast_fallback_timeout_seconds: float = 1.5
    admin_db_unavailable_backoff_seconds: float = 15.0

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_embedding_model: str = "text-embedding-3-small"

    # LLM Settings
    llm_max_tokens: int = 2000
    llm_temperature: float = 0.7
    llm_timeout: int = 30

    # RAG Settings
    rag_backend: str = "local"  # local | qdrant
    rag_chunk_size: int = 220
    rag_chunk_overlap: int = 40
    rag_top_k: int = 4
    rag_min_retrieval_score: float = 0.18
    rag_enable_rerank: bool = True
    rag_enable_mmr: bool = True
    rag_mmr_lambda: float = 0.7
    rag_candidate_pool_min: int = 20
    rag_candidate_pool_max: int = 40
    rag_enable_llm_query_rewrite: bool = True
    rag_llm_query_rewrite_max_tokens: int = 64
    rag_enable_llm_rerank: bool = False
    rag_step_logs_enabled: bool = True
    rag_step_log_file: str = "./logs/detailedsteps.log"
    rag_step_log_preview_chars: int = 260
    rag_local_index_file: str = "./data/rag/local_index.json"
    kb_direct_lookup_enabled: bool = True
    kb_direct_lookup_min_score: float = 0.34
    kb_direct_lookup_max_answer_chars: int = 600
    kb_direct_lookup_step_logs_enabled: bool = True
    kb_direct_lookup_enable_llm_rewrite: bool = True
    kb_direct_lookup_llm_rewrite_max_tokens: int = 64
    kb_direct_disable_rag_fallback: bool = True
    agent_plugin_runtime_enabled: bool = False
    chat_full_kb_llm_mode: bool = False
    full_kb_llm_max_kb_chars: int = 180000
    full_kb_llm_max_history_messages: int = 10
    full_kb_llm_memory_summary_chars: int = 2200
    full_kb_llm_force_summary_refresh: bool = True
    full_kb_llm_step_logs_enabled: bool = True
    full_kb_llm_temperature: float = 0.1
    full_kb_llm_passthrough_mode: bool = False
    full_kb_llm_pre_shortcuts_enabled: bool = False
    chat_require_strict_confirmation_phrase: bool = True
    chat_confirmation_phrase: str = "yes confirm"
    chat_kb_only_mode: bool = False
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_collection: str = "kepsla_kb_chunks"
    qdrant_vector_size: int = 1536

    # Context Settings
    max_conversation_history: int = 20
    context_window_tokens: int = 8000
    session_ttl_hours: int = 24

    # Confidence Thresholds
    intent_confidence_threshold: float = 0.7
    response_confidence_threshold: float = 0.6
    escalation_threshold: float = 0.4

    # WhatsApp
    whatsapp_api_url: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""

    # External ticketing/handoff integrations (Lumira-compatible)
    ticketing_base_url: str = ""
    ticketing_create_path: str = "/insert/ticket.htm"
    ticketing_update_path_template: str = "/insert/ticket/{ticket_id}.htm"
    ticketing_timeout_seconds: float = 10.0
    ticketing_local_mode: bool = False
    ticketing_local_store_file: str = "./data/ticketing/local_tickets.json"
    ticketing_local_csv_file: str = ""
    agent_handoff_api_url: str = ""
    ticketing_smart_routing_enabled: bool = True
    ticketing_smart_routing_use_llm: bool = True
    ticketing_router_model: str = "gpt-4o-mini"
    ticketing_router_ack_similarity: float = 0.88
    ticketing_router_update_similarity: float = 0.55
    ticketing_enrichment_enabled: bool = True
    ticketing_auto_create_on_actionable: bool = True
    ticketing_plugin_enabled: bool = True
    ticketing_plugin_takeover_mode: bool = False
    ticketing_case_match_use_llm: bool = True
    ticketing_case_match_model: str = "gpt-4o-mini"
    ticketing_case_match_fallback_enabled: bool = True
    ticketing_subcategory_llm_enabled: bool = False
    ticketing_subcategory_model: str = "gpt-4o-mini"
    ticketing_stale_reconfirm_enabled: bool = False
    ticketing_stale_reconfirm_minutes: int = 30
    ticketing_guest_preferences_enabled: bool = False
    ticketing_guest_preferences_use_llm: bool = False
    ticketing_guest_preferences_model: str = "gpt-4o-mini"
    ticketing_identity_gate_enabled: bool = False
    ticketing_identity_gate_prebooking_only: bool = True
    ticketing_identity_require_name: bool = True
    ticketing_identity_require_phone: bool = True

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()
