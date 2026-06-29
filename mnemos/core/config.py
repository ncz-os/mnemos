"""Typed MNEMOS runtime settings.

Environment reads are centralized here. Runtime code should import
``get_settings()`` and use typed fields instead of calling ``os.getenv`` or
``os.environ`` directly.

Allowed exceptions to the ban are:
  * ``mnemos/installer/*``: the install wizard runs before package config exists.
  * ``tests/*``: test-specific process environment setup is intentional.
"""

from __future__ import annotations

import os
import socket
import tomllib
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mnemos.core.extras import install_hint, is_extra_installed
from mnemos.core.services import ServiceResolution, parse_component_selection, resolve_profile_services


PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "backend": "postgres",
        "rate_limit_storage": "redis://localhost:6379/1",
        "workers": 1,
        "graeae_mode_default": "auto",
        "log_level": "INFO",
        "compression_workers": 4,
        "auth_enabled": True,
    },
    "edge": {
        "backend": "sqlite",
        "rate_limit_storage": "memory://",
        "workers": 1,
        "graeae_mode_default": "single",
        "log_level": "INFO",
        "compression_workers": 1,
        "auth_enabled": False,
    },
    "dev": {
        "backend": "sqlite",
        "rate_limit_storage": "memory://",
        "workers": 1,
        "graeae_mode_default": "auto",
        "log_level": "DEBUG",
        "compression_workers": 1,
        "loose_timeouts": True,
        "auth_enabled": False,
    },
}

PROFILE_ALIASES = {
    "personal": "edge",
}

_PROFILE_DEFAULT_TARGETS = {
    "backend": ("database", "backend"),
    "rate_limit_storage": ("rate_limit", "storage_uri"),
    "workers": ("server", "workers"),
    "graeae_mode_default": ("graeae", "mode_default"),
    "log_level": ("logging", "level"),
    "compression_workers": ("compression", "workers"),
    "loose_timeouts": ("runtime", "loose_timeouts"),
    "auth_enabled": ("auth", "enabled"),
}


def _config_model_config(*, env_prefix: str = "", extra: str = "ignore") -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=env_prefix,
        extra=extra,
        populate_by_name=True,
    )


class _DatabaseSettings(BaseSettings):
    model_config = _config_model_config(env_prefix="PG_")

    backend: str = Field(
        "auto",
        validation_alias=AliasChoices("MNEMOS_PERSISTENCE_BACKEND", "PERSISTENCE_BACKEND", "PG_BACKEND"),
    )
    dsn: str = Field(
        "",
        validation_alias=AliasChoices("MNEMOS_DATABASE_DSN", "DATABASE_DSN", "PG_DSN"),
    )
    url: str = Field(
        "",
        validation_alias=AliasChoices("MNEMOS_DATABASE_URL", "DATABASE_URL", "PG_URL"),
    )
    sqlite_path: Path = Field(
        default_factory=lambda: Path.home() / ".mnemos" / "mnemos.db",
        validation_alias=AliasChoices("MNEMOS_SQLITE_PATH", "SQLITE_DB_PATH", "PG_SQLITE_PATH"),
    )
    host: str = "localhost"
    port: int = 5432
    database: str = "mnemos"
    user: str = "mnemos_user"
    password: str = ""
    pool_min_size: int = Field(5, validation_alias="PG_POOL_MIN")
    pool_max_size: int = Field(20, validation_alias="PG_POOL_MAX")
    # Native-db2-port PR #11: runtime dialect selector.
    # "compat" = use Db2Backend (cursor-layer Oracle→Db2 translation, default)
    # "native" = use Db2BackendNative (pass-through, requires all repos emit
    #            Db2-native SQL with ? positional binds)
    db2_dialect: str = Field(
        "compat",
        validation_alias=AliasChoices("MNEMOS_DB2_DIALECT", "PG_DB2_DIALECT"),
    )
    embedding_dim: int = Field(
        768,
        validation_alias=AliasChoices("MNEMOS_EMBEDDING_DIM", "PG_EMBEDDING_DIM"),
        description=(
            "Vector dimension for the embedding column / sqlite-vec virtual table. "
            "Default 768 matches nomic-embed-text and the OpenAI text-embedding-3-small "
            "default. Override to match your embedding model: e.g. 512 for bge-small-zh-v1.5 "
            "(used on Cix Sky1 NPU substrate), 1536 for OpenAI text-embedding-3-small at "
            "full dim, 3072 for text-embedding-3-large. SQLite valid range is [1, 8192] "
            "per sqlite-vec SQLITE_VEC_VEC0_MAX_DIMENSIONS. Postgres backends are sized "
            "at schema-init time; switching dim on a populated DB requires a re-embed."
        ),
    )

    @field_validator("sqlite_path", mode="before")
    @classmethod
    def _expand_sqlite_path(cls, raw: Any) -> Path:
        return Path(raw).expanduser()

    @property
    def oracle_dsn(self) -> str:
        return oracle_dsn_env()

    @property
    def db2_dsn(self) -> str:
        return db2_dsn_env()

    @property
    def required_capabilities(self) -> str:
        return required_capabilities_env()

    @property
    def vector_dim_max(self) -> int:
        return vector_dim_max_env()

    @property
    def db2_vector_index(self) -> str:
        raw = db2_vector_index_override()
        return raw if raw is not None else "approx"

    @property
    def oracle_pdb(self) -> str:
        return oracle_pdb_env()

    @property
    def oracle_thick(self) -> str:
        return runtime_env_value_stripped("MNEMOS_ORACLE_THICK")

    @property
    def oracle_drcp(self) -> str:
        return runtime_env_value_stripped("MNEMOS_ORACLE_DRCP")

    @property
    def oracle_pool_min(self) -> int:
        return runtime_env_int("MNEMOS_ORACLE_POOL_MIN", 2)

    @property
    def oracle_pool_max(self) -> int:
        return runtime_env_int("MNEMOS_ORACLE_POOL_MAX", 10)

    @property
    def oracle_pool_increment(self) -> int:
        return runtime_env_int("MNEMOS_ORACLE_POOL_INCREMENT", 1)

    @property
    def oracle_stmt_cache_size(self) -> int:
        return runtime_env_int("MNEMOS_ORACLE_STMT_CACHE_SIZE", 20)

    @property
    def oracle_pool_acquire_timeout(self) -> float:
        return runtime_env_float("MNEMOS_ORACLE_POOL_ACQUIRE_TIMEOUT", 60.0)

    @property
    def mysql_pool_min(self) -> int:
        return runtime_env_int("MNEMOS_MYSQL_POOL_MIN", 2)

    @property
    def mysql_pool_max(self) -> int:
        return runtime_env_int("MNEMOS_MYSQL_POOL_MAX", 10)

    @property
    def mysql_connect_timeout(self) -> float:
        return runtime_env_float("MNEMOS_MYSQL_CONNECT_TIMEOUT", 10.0)


class _GraeaeSettings(BaseSettings):
    model_config = _config_model_config(extra="allow")

    providers: dict[str, Any] = Field(default_factory=dict)
    mode_default: str = Field("auto", validation_alias="GRAEAE_MODE_DEFAULT")
    nats_fanout: bool = Field(False, validation_alias="MNEMOS_GRAEAE_NATS_FANOUT")
    providers_enabled: str = Field("together,groq,openai,anthropic", validation_alias="GRAEAE_PROVIDERS")
    consensus_mode: bool = Field(True, validation_alias="GRAEAE_CONSENSUS_MODE")
    consensus_quorum_size: int = Field(3, validation_alias="GRAEAE_CONSENSUS_QUORUM_SIZE")
    cache_enabled: bool = Field(True, validation_alias="GRAEAE_CACHE_ENABLED")
    cache_ttl_seconds: int = Field(3600, validation_alias="GRAEAE_CACHE_TTL_SECONDS")
    elo_registry: Path = Field(
        Path("/var/lib/mnemos/graeae_elo_weights.json"),
        validation_alias="GRAEAE_ELO_REGISTRY",
    )


class _ServerSettings(BaseSettings):
    model_config = _config_model_config()

    bind: str = Field("127.0.0.1", validation_alias=AliasChoices("MNEMOS_BIND", "MNEMOS_HOST"))
    port: int = Field(5002, validation_alias="MNEMOS_PORT")
    workers: int = Field(1, validation_alias="MNEMOS_WORKERS")
    base: str = Field("http://localhost:5002", validation_alias="MNEMOS_BASE")
    base_configured: bool = False
    api_key: str = Field("", validation_alias="MNEMOS_API_KEY")
    # Round-3 residual #1 of #146: bridge-only credential for the
    # MCP audit ingestion endpoint. Bridges include
    # `X-Mnemos-Audit-Token: <value>` on POST /v1/internal/mcp_audit.
    # The route accepts ONLY this token (rejects normal API/session
    # bearer tokens) so any token holder can't append forged audit
    # rows. When unset, the endpoint operates in legacy mode (any
    # authenticated caller) and emits a warning at startup.
    internal_audit_token: str = Field("", validation_alias="MNEMOS_INTERNAL_AUDIT_TOKEN")
    profile: str = Field("personal", validation_alias="MNEMOS_PROFILE")
    max_body_bytes: int = Field(5 * 1024 * 1024, validation_alias="MAX_BODY_BYTES")
    cors_origins: str = Field(
        "http://localhost,http://127.0.0.1,http://127.0.0.1:5002,http://localhost:5002",
        validation_alias="CORS_ORIGINS",
    )
    session_secret: str = Field("", validation_alias="MNEMOS_SESSION_SECRET")
    session_https_only: bool = Field(False, validation_alias="MNEMOS_SESSION_HTTPS_ONLY")
    redis_url: str = Field(
        "redis://localhost:6379",
        validation_alias=AliasChoices("MNEMOS_REDIS_URL", "REDIS_URL"),
    )


class _ProfileServiceSettings(BaseSettings):
    model_config = _config_model_config()

    managed: bool = Field(False, validation_alias="MNEMOS_PROFILE_SERVICES_ENABLED")
    selected_components: str = Field("", validation_alias="MNEMOS_SELECTED_COMPONENTS")
    resolution: ServiceResolution = Field(
        default_factory=lambda: resolve_profile_services(
            profile=None,
            managed=False,
            selected_components=(),
            env=os.environ,
        )
    )


class _WebhookSettings(BaseSettings):
    model_config = _config_model_config()

    dns_timeout: float = Field(10.0, validation_alias="WEBHOOK_DNS_TIMEOUT")
    http_timeout: float = Field(10.0, validation_alias="WEBHOOK_HTTP_TIMEOUT")
    lease_seconds: int | None = Field(None, validation_alias="WEBHOOK_LEASE_SECONDS")
    shutdown_drain_seconds: float | None = Field(None, validation_alias="WEBHOOK_SHUTDOWN_DRAIN_SECONDS")
    finalize_buffer_seconds: float = Field(5.0, validation_alias="WEBHOOK_FINALIZE_BUFFER_SECONDS")
    response_body_max_bytes: int = Field(2048, validation_alias="WEBHOOK_RESPONSE_BODY_MAX_BYTES")
    post_header_cleanup_timeout_seconds: float = Field(
        5.0,
        validation_alias="WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS",
    )
    max_concurrent_sends: int = Field(64, validation_alias="WEBHOOK_MAX_CONCURRENT_SENDS")
    repair_burst_seconds: float = Field(60.0, validation_alias="WEBHOOK_REPAIR_BURST_SECONDS")
    repair_burst_interval: float = Field(5.0, validation_alias="WEBHOOK_REPAIR_BURST_INTERVAL")
    repair_periodic_interval: float = Field(300.0, validation_alias="WEBHOOK_REPAIR_PERIODIC_INTERVAL")
    allow_private_hosts: bool = Field(False, validation_alias="WEBHOOK_ALLOW_PRIVATE_HOSTS")

    @model_validator(mode="after")
    def _derive_lease_defaults(self) -> "_WebhookSettings":
        default_lease = max(90, int(self.dns_timeout + self.http_timeout + 30))
        if self.lease_seconds is None:
            self.lease_seconds = default_lease
        if self.shutdown_drain_seconds is None:
            self.shutdown_drain_seconds = float(self.lease_seconds)
        return self


class _ProviderSettings(BaseSettings):
    model_config = _config_model_config()

    openai_api_key: str = Field("", validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    gemini_api_key: str = Field("", validation_alias="GEMINI_API_KEY")
    xai_api_key: str = Field("", validation_alias="XAI_API_KEY")
    groq_api_key: str = Field("", validation_alias="GROQ_API_KEY")
    perplexity_api_key: str = Field("", validation_alias="PERPLEXITY_API_KEY")
    # KNEMON routing policy (deployment-configurable; no-op defaults).
    # Comma-separated provider ids/aliases excluded from KNEMON route
    # selection by default (merged with any per-request exclude_providers).
    # The Anthropic ban is THIS deployment's policy, not a universal rule —
    # other deployments leave this empty or choose differently.
    knemon_exclude_providers: str = Field("", validation_alias="KNEMON_DEFAULT_EXCLUDE_PROVIDERS")
    # Comma-separated ordered provider preference; candidates are bucketed by
    # this order first, then by graeae_weight within each bucket. Empty
    # preserves pure graeae_weight ordering.
    knemon_provider_preference: str = Field("", validation_alias="KNEMON_PROVIDER_PREFERENCE")
    together_api_key: str = Field("", validation_alias="TOGETHER_API_KEY")
    nvidia_api_key: str = Field("", validation_alias="NVIDIA_API_KEY")
    eih_api_key: str = Field("", validation_alias="EIH_API_KEY")
    deepseek_api_key: str = Field("", validation_alias="DEEPSEEK_API_KEY")
    keys_path: Path | None = Field(None, validation_alias="MNEMOS_KEYS_PATH")
    api_keys_file: Path = Field(
        default_factory=lambda: Path.home() / ".config" / "mnemos" / "api_keys.json",
        validation_alias="API_KEYS_FILE",
    )
    gpu_provider_host: str = Field("http://localhost", validation_alias="GPU_PROVIDER_HOST")
    gpu_provider_port: str = Field("8000", validation_alias="GPU_PROVIDER_PORT")
    gpu_provider_timeout: float = Field(30.0, validation_alias="GPU_PROVIDER_TIMEOUT")
    # Embedding generation is in-process — see mnemos/runtime/embedder.py.
    # Architectural decision mem_1779334716543_f8ebd4, operator-locked 2026-05-21.
    # Fields below are retained for installer compatibility only and are no
    # longer read by the runtime. New deployments should set MNEMOS_EMBED_*
    # (model path / threads / gpu_layers) instead.
    inference_embed_host: str = Field("", validation_alias="INFERENCE_EMBED_HOST")
    inference_embed_model: str = Field("", validation_alias="INFERENCE_EMBED_MODEL")
    inference_embed_timeout: float = Field(10.0, validation_alias="INFERENCE_EMBED_TIMEOUT")
    embed_model_path: str = Field(
        "/opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf",
        validation_alias="MNEMOS_EMBED_MODEL_PATH",
    )
    embed_n_ctx: int = Field(8192, validation_alias="MNEMOS_EMBED_N_CTX")
    embed_threads: int = Field(0, validation_alias="MNEMOS_EMBED_THREADS")  # 0 = auto
    embed_gpu_layers: int = Field(0, validation_alias="MNEMOS_EMBED_GPU_LAYERS")

    @property
    def embed_backend(self) -> str:
        return embed_backend_env()

    @property
    def embed_ov_model_id(self) -> str:
        return embed_ov_model_id_env()

    @property
    def embed_ov_device(self) -> str:
        return embed_ov_device_env()

    @property
    def embed_cix_model_path(self) -> str:
        return embed_cix_model_path_env()

    @property
    def embed_cix_tokenizer_id(self) -> str:
        return embed_cix_tokenizer_id_env()

    @property
    def embed_cix_max_seq_len(self) -> int:
        return embed_cix_max_seq_len_env()

    @property
    def embed_hybrid(self) -> str:
        return embed_hybrid_env()

    @property
    def embed_npu_threshold_chars(self) -> int:
        return embed_npu_threshold_chars_env()

    @property
    def embed_http_url(self) -> str:
        return embed_http_url_env()

    @property
    def embed_http_url_fallback(self) -> str:
        return embed_http_url_fallback_env()

    @property
    def embed_http_model(self) -> str:
        return embed_http_model_env()

    @property
    def embed_http_timeout(self) -> float:
        return embed_http_timeout_env()

    @property
    def embed_max_chars(self) -> int:
        return embed_max_chars_env()

    @property
    def reranker_url(self) -> str:
        return reranker_url_env()

    @property
    def reranker_model(self) -> str:
        return reranker_model_env()

    @property
    def reranker_timeout_secs(self) -> str:
        return reranker_timeout_secs_env() or ""

    def api_key_for(self, provider: str) -> str:
        keys = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google_gemini": self.gemini_api_key,
            "gemini": self.gemini_api_key,
            "xai": self.xai_api_key,
            "groq": self.groq_api_key,
            "perplexity": self.perplexity_api_key,
            "together_ai": self.together_api_key,
            "together": self.together_api_key,
            "nvidia": self.nvidia_api_key,
            "ngc": self.nvidia_api_key,
            "eih": self.eih_api_key,
            "deepseek": self.deepseek_api_key,
            "deepseek-direct": self.deepseek_api_key,
        }
        return keys.get(provider, "")


class _MCPSettings(BaseSettings):
    model_config = _config_model_config()

    token: str = Field("", validation_alias="MNEMOS_MCP_TOKEN")
    tokens: str = Field("", validation_alias="MNEMOS_MCP_TOKENS")
    bind: str = Field("127.0.0.1", validation_alias="MNEMOS_MCP_BIND")


class _RateLimitSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(True, validation_alias="RATE_LIMIT_ENABLED")
    default: str = Field("300/minute", validation_alias="RATE_LIMIT_DEFAULT")
    storage_uri: str = Field(
        "memory://",
        validation_alias=AliasChoices("RATE_LIMIT_STORAGE_URI", "RATE_LIMIT_STORAGE", "storage"),
    )
    trust_proxy: bool = Field(False, validation_alias="RATE_LIMIT_TRUST_PROXY")
    per_minute: int = Field(60, validation_alias=AliasChoices("MNEMOS_RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_PER_MINUTE"))
    pantheon_gateway: str = Field(
        "60/minute",
        validation_alias=AliasChoices(
            "MNEMOS_PANTHEON_GATEWAY_RATE_LIMIT",
            "PANTHEON_GATEWAY_RATE_LIMIT",
        ),
    )

    @property
    def storage(self) -> str:
        """Backward-compatible alias for older internal callers."""
        return self.storage_uri


class _ResilienceSettings(BaseSettings):
    model_config = _config_model_config()

    circuit_breaker_redis_prefix: str = Field(
        "mnemos:cb:",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_CIRCUIT_BREAKER_REDIS_PREFIX",
            "MNEMOS_CIRCUIT_BREAKER_REDIS_PREFIX",
        ),
    )
    circuit_breaker_nats_prefix: str = Field(
        "cb.",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_CIRCUIT_BREAKER_NATS_PREFIX",
            "MNEMOS_CIRCUIT_BREAKER_NATS_PREFIX",
        ),
    )
    rate_limiter_redis_prefix: str = Field(
        "mnemos:rl:",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_RATE_LIMITER_REDIS_PREFIX",
            "MNEMOS_RATE_LIMITER_REDIS_PREFIX",
        ),
    )
    concurrency_redis_prefix: str = Field(
        "mnemos:conc:",
        validation_alias=AliasChoices(
            "MNEMOS_RESILIENCE_CONCURRENCY_REDIS_PREFIX",
            "MNEMOS_CONCURRENCY_REDIS_PREFIX",
        ),
    )
    fallback_warning: bool = Field(True, validation_alias="MNEMOS_RESILIENCE_FALLBACK_WARNING")


class _ObservabilitySettings(BaseSettings):
    model_config = _config_model_config()

    structured_logs: bool = Field(False, validation_alias="MNEMOS_STRUCTURED_LOGS")
    tracing_enabled: bool = Field(True, validation_alias="MNEMOS_TRACING_ENABLED")
    metrics_enabled: bool = Field(True, validation_alias="MNEMOS_METRICS_ENABLED")
    # When True, /metrics requires the same Bearer token as the rest of
    # the API. Default False matches the Prometheus convention of
    # network-scoping the scrape endpoint via ingress / firewall rather
    # than per-request auth. Operators in environments where the
    # /metrics endpoint is reachable from less-trusted networks (shared
    # cloud Prometheus, public-internet-routed clusters) flip this on.
    metrics_require_auth: bool = Field(
        False,
        validation_alias="MNEMOS_METRICS_REQUIRE_AUTH",
    )
    otel_service_name: str = Field("mnemos", validation_alias="OTEL_SERVICE_NAME")
    otel_exporter_otlp_endpoint: str = Field("", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")


class _CompressionSettings(BaseSettings):
    model_config = _config_model_config()

    workers: int = Field(1, validation_alias="MNEMOS_COMPRESSION_WORKERS")
    contest_enabled: bool = Field(True, validation_alias="MNEMOS_CONTEST_ENABLED")
    contest_min_content_length: int = Field(0, validation_alias="MNEMOS_CONTEST_MIN_CONTENT_LENGTH")
    contest_stale_threshold_secs: int = Field(600, validation_alias="MNEMOS_CONTEST_STALE_THRESHOLD_SECS")
    apollo_enabled: bool = Field(True, validation_alias="MNEMOS_APOLLO_ENABLED")
    apollo_llm_fallback_enabled: bool = Field(True, validation_alias="MNEMOS_APOLLO_LLM_FALLBACK_ENABLED")
    judge_enabled: bool = Field(False, validation_alias="MNEMOS_JUDGE_ENABLED")
    judge_model: str = Field("judge-default", validation_alias="MNEMOS_JUDGE_MODEL")
    judge_mode: str = Field("llm", validation_alias="MNEMOS_JUDGE_MODE")
    cross_encoder_model: str = Field(
        "cross-encoder/ms-marco-MiniLM-L-12-v2",
        validation_alias="MNEMOS_CROSS_ENCODER_MODEL",
    )

    @field_validator("contest_stale_threshold_secs", mode="before")
    @classmethod
    def _non_negative_stale_threshold(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 0
        return value if value >= 0 else 0


class _ArtemisSettings(BaseSettings):
    model_config = _config_model_config()

    dedup_mode: str = Field("reject", validation_alias="MNEMOS_ARTEMIS_DEDUP_MODE")
    dedup_cross_namespace: bool = Field(
        False,
        validation_alias="MNEMOS_ARTEMIS_DEDUP_CROSS_NAMESPACE",
    )

    @field_validator("dedup_mode", mode="before")
    @classmethod
    def _normalize_dedup_mode(cls, raw: Any) -> str:
        value = str(raw or "reject").strip().lower()
        return value if value in {"reject", "merge", "warn", "off"} else "reject"


class _MorpheusSettings(BaseSettings):
    model_config = _config_model_config()

    cluster_threshold: float = Field(0.85, validation_alias="MNEMOS_MORPHEUS_CLUSTER_THRESHOLD")
    use_llm: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_USE_LLM")
    consolidate: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_CONSOLIDATE")
    extract: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_EXTRACT")
    extract_verify: bool = Field(False, validation_alias="MNEMOS_MORPHEUS_EXTRACT_VERIFY")
    extract_min_chars: int = Field(200, validation_alias="MNEMOS_MORPHEUS_EXTRACT_MIN_CHARS")
    extract_min_confidence: float = Field(0.6, validation_alias="MNEMOS_MORPHEUS_EXTRACT_MIN_CONFIDENCE")
    extract_muse: str = Field("qwen3-7b", validation_alias="MNEMOS_MORPHEUS_EXTRACT_MUSE")
    extract_verifier: str = Field("openai", validation_alias="MNEMOS_MORPHEUS_EXTRACT_VERIFIER")

    @property
    def orphan_timeout_hours(self) -> str | None:
        return morpheus_orphan_timeout_hours_env()


class _PersephoneSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_PERSEPHONE_ENABLED")
    archive_after_days: int = Field(180, validation_alias="MNEMOS_PERSEPHONE_ARCHIVE_AFTER_DAYS")
    batch_size: int = Field(100, validation_alias="MNEMOS_PERSEPHONE_BATCH_SIZE")
    check_interval_seconds: float = Field(
        3600.0,
        validation_alias="MNEMOS_PERSEPHONE_CHECK_INTERVAL_SECONDS",
    )
    namespace: str = Field("default", validation_alias="MNEMOS_PERSEPHONE_NAMESPACE")

    @field_validator("archive_after_days", "batch_size", mode="before")
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1

    @field_validator("check_interval_seconds", mode="before")
    @classmethod
    def _positive_interval(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 3600.0
        return value if value > 0 else 3600.0


class KronosSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_KRONOS_ENABLED")
    default_sensitivity: float = Field(2.5, validation_alias="MNEMOS_KRONOS_SENSITIVITY")
    default_lookback_hours: int = Field(168, validation_alias="MNEMOS_KRONOS_LOOKBACK_HOURS")
    default_baseline_days: int = Field(30, validation_alias="MNEMOS_KRONOS_BASELINE_DAYS")

    @property
    def backend(self) -> str:
        return kronos_backend_env()

    @field_validator("default_sensitivity", mode="before")
    @classmethod
    def _positive_sensitivity(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 2.5
        return value if value > 0 else 2.5

    @field_validator("default_lookback_hours", "default_baseline_days", mode="before")
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1


class PantheonSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_PANTHEON_ENABLED")
    cross_provider_fallback: bool = Field(
        False,
        validation_alias="MNEMOS_PANTHEON_CROSS_PROVIDER_FALLBACK",
    )
    consultation_cap: int = Field(
        50,
        validation_alias="MNEMOS_PANTHEON_CONSULTATION_CAP",
    )
    routing_window_minutes: int = Field(
        15,
        validation_alias="MNEMOS_PANTHEON_ROUTING_WINDOW_MINUTES",
    )
    policy_latency_weight: float = Field(
        0.40,
        validation_alias="MNEMOS_PANTHEON_POLICY_LATENCY_WEIGHT",
    )
    policy_error_weight: float = Field(
        0.40,
        validation_alias="MNEMOS_PANTHEON_POLICY_ERROR_WEIGHT",
    )
    policy_cost_weight: float = Field(
        0.20,
        validation_alias="MNEMOS_PANTHEON_POLICY_COST_WEIGHT",
    )
    default_quality_floor: float = Field(
        0.80,
        validation_alias="MNEMOS_PANTHEON_DEFAULT_QUALITY_FLOOR",
    )
    default_max_cost_usd_per_mtok: float = Field(
        10.0,
        validation_alias=AliasChoices(
            "MNEMOS_PANTHEON_DEFAULT_MAX_COST",
            "MNEMOS_PANTHEON_DEFAULT_MAX_COST_USD_PER_MTOK",
        ),
    )
    routing_log_queue_size: int = Field(
        2000,
        validation_alias="MNEMOS_PANTHEON_ROUTING_LOG_QUEUE_SIZE",
    )
    routing_log_drain_workers: int = Field(
        1,
        validation_alias="MNEMOS_PANTHEON_ROUTING_LOG_DRAIN_WORKERS",
    )
    reasoning_output_token_budget: int = Field(
        8000,
        validation_alias="MNEMOS_PANTHEON_REASONING_OUTPUT_TOKEN_BUDGET",
    )
    upstream_timeout_seconds: float = Field(
        60.0,
        validation_alias=AliasChoices(
            "MNEMOS_PANTHEON_UPSTREAM_TIMEOUT_SECONDS",
            "PANTHEON_UPSTREAM_TIMEOUT_SECONDS",
            "PANTHEON_UPSTREAM_TIMEOUT",
        ),
    )
    passthrough_enabled: bool = Field(
        True,
        validation_alias="MNEMOS_PANTHEON_PASSTHROUGH_ENABLED",
    )
    passthrough_provider: str = Field(
        "nvidia",
        validation_alias="MNEMOS_PANTHEON_PASSTHROUGH_PROVIDER",
    )
    passthrough_default_input_cost_per_mtok: float = Field(
        5.0,
        validation_alias="MNEMOS_PANTHEON_PASSTHROUGH_DEFAULT_INPUT_COST_PER_MTOK",
    )
    passthrough_default_output_cost_per_mtok: float = Field(
        30.0,
        validation_alias="MNEMOS_PANTHEON_PASSTHROUGH_DEFAULT_OUTPUT_COST_PER_MTOK",
    )
    passthrough_default_estimated_output_tokens: int = Field(
        4096,
        validation_alias="MNEMOS_PANTHEON_PASSTHROUGH_DEFAULT_ESTIMATED_OUTPUT_TOKENS",
    )
    shadow_port: int = Field(
        4101,
        validation_alias="MNEMOS_PANTHEON_SHADOW_PORT",
    )
    shadow_no_auth: bool = Field(
        True,
        validation_alias="MNEMOS_PANTHEON_SHADOW_NO_AUTH",
    )
    catalog_cache_path: str | None = Field(
        None,
        validation_alias="MNEMOS_PANTHEON_CATALOG_CACHE_PATH",
    )
    nats_key_secret: str = Field("", validation_alias="MNEMOS_PANTHEON_NATS_KEY_SECRET")

    @field_validator(
        "consultation_cap",
        "routing_window_minutes",
        "routing_log_queue_size",
        "routing_log_drain_workers",
        "reasoning_output_token_budget",
        "passthrough_default_estimated_output_tokens",
        "shadow_port",
        mode="before",
    )
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1

    @field_validator("policy_latency_weight", "policy_error_weight", "policy_cost_weight", mode="before")
    @classmethod
    def _non_negative_weight(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return value if value >= 0.0 else 0.0

    @field_validator("upstream_timeout_seconds", mode="before")
    @classmethod
    def _positive_timeout(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 60.0
        return value if value > 0.0 else 60.0

    @field_validator("passthrough_provider", mode="before")
    @classmethod
    def _non_empty_provider(cls, raw: Any) -> str:
        provider = str(raw or "").strip()
        return provider or "nvidia"

    @field_validator(
        "passthrough_default_input_cost_per_mtok",
        "passthrough_default_output_cost_per_mtok",
        mode="before",
    )
    @classmethod
    def _positive_cost(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0.0 else 1.0


class KnemonSettings(BaseSettings):
    model_config = _config_model_config(env_prefix="MNEMOS_KNEMON_")

    weekly_budget_cap_usd: float = 200.0
    # Universal per-provider weekly spend caps (2026-06-20). CSV of
    # "<provider>=<usd>" pairs, e.g. "openai=50,anthropic=30,minimax=20". Each
    # listed provider is capped INDEPENDENTLY of (and in addition to) the global
    # weekly_budget_cap_usd: a request is denied if EITHER the global cap or the
    # provider's own cap would be exceeded. Providers omitted here are governed
    # by the global cap only. Empty (default) preserves single-cap behaviour.
    # Env: MNEMOS_KNEMON_PROVIDER_BUDGET_CAPS_USD.
    provider_budget_caps_usd: str = ""
    session_burn_requests_per_hour: int = 10
    session_burn_window_seconds: int = 3600
    subscription_preferred_utilization_pct: float = 70.0
    subscription_near_cap_pct: float = 90.0
    low_priority_api_cost_ceiling_usd: float = 0.50
    g1_quality_floor: float = 0.85
    g2_quality_floor: float = 0.75

    @field_validator("session_burn_requests_per_hour", "session_burn_window_seconds", mode="before")
    @classmethod
    def _positive_int(cls, raw: Any) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1

    @field_validator("subscription_preferred_utilization_pct", "subscription_near_cap_pct", mode="before")
    @classmethod
    def _bounded_pct(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(100.0, value))

    @field_validator("weekly_budget_cap_usd", mode="before")
    @classmethod
    def _non_negative_budget(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 200.0
        return max(0.0, value)

    def parsed_provider_budget_caps_usd(self) -> dict[str, float]:
        """Parse ``provider_budget_caps_usd`` CSV into a ``{provider: cap_usd}`` map.

        Tolerant of whitespace and malformed entries: blank parts, entries
        without ``=``, non-numeric values, and non-positive caps are skipped so
        a typo can never silently grant unlimited spend or crash startup.
        """
        out: dict[str, float] = {}
        for part in (self.provider_budget_caps_usd or "").split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, raw = part.partition("=")
            name = name.strip().lower()
            try:
                value = float(raw.strip())
            except (TypeError, ValueError):
                continue
            if name and value > 0:
                out[name] = value
        return out

    @field_validator("g1_quality_floor", "g2_quality_floor", mode="before")
    @classmethod
    def _bounded_quality_floor(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, value))

    @field_validator("low_priority_api_cost_ceiling_usd", mode="before")
    @classmethod
    def _non_negative_cost(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return value if value >= 0.0 else 0.0


class _FederationNatsPeerSettings(BaseModel):
    name: str
    nats_url: str
    nats_token: str | None = None
    base_url: str | None = None
    auth_token: str | None = None
    namespace_filter: list[str] | None = None
    category_filter: list[str] | None = None
    subjects: list[str] = Field(default_factory=lambda: ["mnemos.memory.>"])


class _FederationSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_FEDERATION_ENABLED")
    peers: str = Field("", validation_alias="MNEMOS_FEDERATION_PEERS")
    nats_peers: list[_FederationNatsPeerSettings] = Field(
        default_factory=list,
        validation_alias="MNEMOS_FEDERATION_NATS_PEERS",
    )
    allow_insecure: bool = Field(False, validation_alias="FEDERATION_ALLOW_INSECURE")
    allow_private: bool = Field(False, validation_alias="FEDERATION_ALLOW_PRIVATE")
    # When set, federation NATS receivers join a JetStream queue group
    # under a SHARED durable consumer per (peer, subject) instead of
    # their default single-replica per-(peer, subject) durable. JetStream
    # load-balances messages across replicas in the same group; this is
    # the supported multi-replica deployment shape (Audit Finding 5).
    # Empty (default) preserves single-replica behavior with no
    # cross-replica coordination — flip to a non-empty group name only
    # after every replica is known to be on a build that understands it.
    nats_queue_group: str = Field("", validation_alias="MNEMOS_FEDERATION_NATS_QUEUE_GROUP")


class _OAuthSettings(BaseSettings):
    model_config = _config_model_config()

    trust_proxy: bool = Field(False, validation_alias="OAUTH_TRUST_PROXY")


class _AuthSettings(BaseSettings):
    model_config = _config_model_config()

    enabled: bool = Field(False, validation_alias="MNEMOS_AUTH_ENABLED")
    default_namespace: str = Field("default", validation_alias="MNEMOS_DEFAULT_NAMESPACE")
    personal_user_id: str = Field("default", validation_alias="MNEMOS_PERSONAL_USER_ID")


class _RuntimeSettings(BaseSettings):
    model_config = _config_model_config()

    worker_shutdown_cancel_seconds: float = Field(10.0, validation_alias="WORKER_SHUTDOWN_CANCEL_SECONDS")
    pool_acquire_timeout: float = Field(10.0, validation_alias="MNEMOS_POOL_ACQUIRE_TIMEOUT")
    loose_timeouts: bool = Field(False, validation_alias="MNEMOS_LOOSE_TIMEOUTS")
    task_classifier_factory: str = Field("", validation_alias="MNEMOS_TASK_CLASSIFIER_FACTORY")
    knemon_session_burn_requests_per_hour: int = Field(
        10,
        validation_alias="MNEMOS_KNEMON_SESSION_BURN_REQUESTS_PER_HOUR",
    )


class _ToolSettings(BaseSettings):
    model_config = _config_model_config()

    knossos_wing_axis: str = Field("namespace", validation_alias="KNOSSOS_WING_AXIS")
    knossos_default_wing: str = Field("default", validation_alias="KNOSSOS_DEFAULT_WING")
    neo4j_user: str = Field("neo4j", validation_alias="NEO4J_USER")
    neo4j_password: str = Field("", validation_alias="NEO4J_PASSWORD")
    falkordb_password: str | None = Field(None, validation_alias="FALKORDB_PASSWORD")


class _LoggingSettings(BaseSettings):
    model_config = _config_model_config()

    level: str = Field("INFO", validation_alias="MNEMOS_LOG_LEVEL")
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    file: str = "/tmp/mnemos.log"
    max_bytes: int = 10_485_760
    backup_count: int = 5


class _NatsSettings(BaseSettings):
    model_config = _config_model_config()

    url: str | None = Field(None, validation_alias="MNEMOS_NATS_URL")
    token: str | None = Field(None, validation_alias="MNEMOS_NATS_TOKEN")
    node_name: str = Field("", validation_alias="MNEMOS_NODE_NAME")
    publish_pantheon_routing: bool = Field(
        False,
        validation_alias="MNEMOS_NATS_PUBLISH_PANTHEON_ROUTING",
    )
    audit_consumer_enabled: bool = Field(
        False,
        validation_alias="MNEMOS_NATS_AUDIT_CONSUMER_ENABLED",
    )
    publish_timeout_seconds: float = Field(
        1.0,
        validation_alias="MNEMOS_NATS_PUBLISH_TIMEOUT",
    )
    # When set, the webhook NATS trigger uses a SHARED durable consumer
    # joined via this queue group instead of per-node durables. JetStream
    # load-balances delivery so only one replica receives each nudge
    # (rather than every replica racing for the Postgres SKIP LOCKED
    # claim). Empty (default) preserves the per-node behavior — safe for
    # both single- and multi-replica deployments, just wasteful in the
    # multi-replica case. Flip to a non-empty group name only after all
    # replicas understand it. (Audit Finding 5.)
    webhook_queue_group: str = Field("", validation_alias="MNEMOS_WEBHOOK_NATS_QUEUE_GROUP")

    @property
    def webhooks_enabled(self) -> str:
        return runtime_env_value_stripped("MNEMOS_NATS_WEBHOOKS_ENABLED")

    @property
    def federation_enabled(self) -> str:
        return runtime_env_value_stripped("MNEMOS_NATS_FEDERATION_ENABLED")

    @property
    def webhooks_queue_group(self) -> str:
        return nats_webhooks_queue_group_env()

    @property
    def federation_queue_group(self) -> str:
        return nats_federation_queue_group_env()

    @field_validator("publish_timeout_seconds", mode="before")
    @classmethod
    def _positive_timeout(cls, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0 else 1.0


class _AuditSettings(BaseModel):
    require_session_secret: str = Field(
        default_factory=lambda: runtime_env_value_stripped("MNEMOS_REQUIRE_SESSION_SECRET")
    )
    chain: str = Field(default_factory=lambda: runtime_env_value("MNEMOS_AUDIT_CHAIN", ""))
    root_private_key: str = Field(default_factory=lambda: runtime_env_value_stripped("MNEMOS_AUDIT_ROOT_PRIVKEY"))


class _HiveMindSettings(BaseModel):
    system_hive_url: str = Field(default_factory=lambda: system_hive_url_env())
    mcp_hive_url: str = Field(default_factory=lambda: mcp_hive_url_env())
    agent_host: str = Field(default_factory=lambda: agent_host_env())
    heartbeat_interval: float = Field(default_factory=lambda: heartbeat_interval_env())
    claim_jobs: str = Field(default_factory=lambda: claim_jobs_env())
    mcp_mnemos_url: str = Field(default_factory=lambda: mcp_mnemos_url_env())
    mcp_mnemos_token: str = Field(default_factory=lambda: mcp_mnemos_token_env())
    mcp_port: int = Field(default_factory=lambda: mcp_port_env())
    agent_bus_db: str = Field(default_factory=lambda: agent_bus_db_env())


class _LayerSettings(BaseSettings):
    """Feature-layer enable flags (GRAEAE consult de8f4b2b layering, 2026-06-01).

    Two install layers stack in mnemos-core: core (memory/persistence, always
    on) <- graeae (reasoning). Hive is the separate ncz-os/hive track and stays
    opt-in here. Direction enforced by Settings.enforce_layer_direction: hive
    requires graeae. See docs/LAYERED_INSTALL.md.
    """

    model_config = _config_model_config()

    enable_graeae: bool = Field(
        default=True,
        validation_alias=AliasChoices("MNEMOS_ENABLE_GRAEAE", "ENABLE_GRAEAE"),
    )
    # Default false: hive is a separate ncz-os/hive track, not a mnemos-core extra.
    enable_hive: bool = Field(
        default=False,
        validation_alias=AliasChoices("MNEMOS_ENABLE_HIVE", "ENABLE_HIVE"),
        description=(
            "Enable the hive/KNEMON routing layer. Hive is the separate ncz-os/hive track in this split and is opt-in."
        ),
    )
    strict_layers: bool = Field(
        default=False,
        validation_alias=AliasChoices("MNEMOS_STRICT_LAYERS", "STRICT_LAYERS"),
        description=(
            "When True, an enabled layer/service whose split distribution is missing "
            "fails fast at startup; when False (default), it is logged and skipped "
            "(degraded boot)."
        ),
    )

    @property
    def active_layers(self) -> set[str]:
        active = {"core"}
        if self.enable_graeae:
            active.add("graeae")
        if self.enable_hive:
            active.add("hive")
        return active


class Settings(BaseSettings):
    model_config = _config_model_config()

    _explicit_fields: dict[str, set[str]] = PrivateAttr(default_factory=dict)

    layers: _LayerSettings
    database: _DatabaseSettings
    graeae: _GraeaeSettings
    server: _ServerSettings
    services: _ProfileServiceSettings
    webhook: _WebhookSettings
    providers: _ProviderSettings
    mcp: _MCPSettings
    rate_limit: _RateLimitSettings
    resilience: _ResilienceSettings
    observability: _ObservabilitySettings
    compression: _CompressionSettings
    artemis: _ArtemisSettings
    morpheus: _MorpheusSettings
    persephone: _PersephoneSettings
    kronos: KronosSettings
    pantheon: PantheonSettings
    knemon: KnemonSettings
    federation: _FederationSettings
    oauth: _OAuthSettings
    auth: _AuthSettings
    runtime: _RuntimeSettings
    tools: _ToolSettings
    logging: _LoggingSettings
    nats: _NatsSettings
    audit: _AuditSettings
    hive_mind: _HiveMindSettings

    @model_validator(mode="after")
    def enforce_layer_direction(self) -> "Settings":
        # Layer dependency direction: core <- graeae <- hive. Hive (job
        # coordination + KNEMON routing) requires GRAEAE (reasoning). See
        # docs/LAYERED_INSTALL.md (GRAEAE consult de8f4b2b, 2026-06-01).
        if self.layers.enable_hive and not self.layers.enable_graeae:
            raise ValueError(
                "MNEMOS_ENABLE_HIVE requires MNEMOS_ENABLE_GRAEAE: the hive/KNEMON "
                "layer depends on the GRAEAE reasoning layer."
            )
        if self.layers.enable_hive and not is_extra_installed("hive"):
            raise ValueError(
                "MNEMOS_ENABLE_HIVE is enabled by configuration, but HIVE is provided "
                "by the separate ncz-os/hive track and is not installed in this "
                f"mnemos-core distribution. Disable MNEMOS_ENABLE_HIVE or repair the "
                f"separate HIVE deployment. Install hint: {install_hint('hive')}"
            )
        return self

    @property
    def profile(self) -> str:
        return self.server.profile

    @property
    def log_level(self) -> str:
        return self.logging.level

    def explicit_fields(self, group: str) -> set[str]:
        return set(self._explicit_fields.get(group, set()))


_settings: Settings | None = None
_registered_task_classifier: Any | None = None
PG_CONFIG: dict[str, Any] = {}
GRAEAE_CONFIG: dict[str, Any] = {}


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _build_settings()
        _sync_compat_exports(_settings)
    return _settings


def register_task_classifier(classifier: Any | None) -> None:
    """Register a process-local OpenAI-compatible task classifier override."""
    global _registered_task_classifier
    _registered_task_classifier = classifier


def get_registered_task_classifier() -> Any | None:
    return _registered_task_classifier


def normalize_profile(raw_profile: str | None) -> str:
    """Return the canonical deployment profile name.

    ``personal`` was the v3.x single-user profile name. In v4 it is an alias
    for the all-in-one ``edge`` profile.
    """
    profile = (raw_profile or "personal").strip().lower()
    profile = PROFILE_ALIASES.get(profile, profile)
    if profile not in PROFILE_DEFAULTS:
        valid = ", ".join(PROFILE_DEFAULTS)
        raise ValueError(
            f"Unsupported MNEMOS profile {raw_profile!r}; expected one of: {valid}. "
            "Legacy profile 'personal' is now an alias for 'edge'."
        )
    return profile


def _build_settings() -> Settings:
    toml_config = _load_toml()
    server_toml = _toml_section(toml_config, "server")
    server = _ServerSettings(**server_toml)
    server.profile = normalize_profile(_profile_from_sources(toml_config, server_toml, server))
    server.base_configured = "MNEMOS_BASE" in os.environ or "base" in server_toml
    # `[database].password = ""` is the documented production shape (secret
    # supplied via PG_PASSWORD env). Drop empty-string values across the
    # full set of env-aliased database fields so they don't win over the
    # BaseSettings env-alias resolution. backend / port / sqlite_path are
    # all common shapes operators sometimes leave empty in config expecting
    # env to fill them; without dropping them, an empty TOML `backend = ""`
    # would block PG_BACKEND and lifecycle would refuse to start with
    # `Unsupported persistence backend ''`.
    db_section = dict(_toml_section(toml_config, "database"))
    _ENV_BACKED_DB_FIELDS = (
        "backend",
        "dsn",
        "url",
        "host",
        "port",
        "database",
        "user",
        "password",
        "sqlite_path",
    )
    for env_filled_field in _ENV_BACKED_DB_FIELDS:
        if env_filled_field in db_section and db_section[env_filled_field] == "":
            db_section.pop(env_filled_field)
    groups = {
        "layers": _LayerSettings(**_toml_section(toml_config, "layers")),
        "database": _DatabaseSettings(**db_section),
        "graeae": _GraeaeSettings(**_toml_section(toml_config, "graeae")),
        "server": server,
        "services": _ProfileServiceSettings(**_toml_section(toml_config, "services")),
        "webhook": _WebhookSettings(**_toml_section(toml_config, "webhook")),
        "providers": _ProviderSettings(**_toml_section(toml_config, "providers")),
        "mcp": _MCPSettings(**_toml_section(toml_config, "mcp")),
        "rate_limit": _RateLimitSettings(**_toml_section(toml_config, "rate_limit")),
        "resilience": _ResilienceSettings(**_toml_section(toml_config, "resilience")),
        "observability": _ObservabilitySettings(**_toml_section(toml_config, "observability")),
        "compression": _CompressionSettings(**_toml_section(toml_config, "compression")),
        "artemis": _ArtemisSettings(**_toml_section(toml_config, "artemis")),
        "morpheus": _MorpheusSettings(**_toml_section(toml_config, "morpheus")),
        "persephone": _PersephoneSettings(**_toml_section(toml_config, "persephone")),
        "kronos": KronosSettings(**_toml_section(toml_config, "kronos")),
        "pantheon": PantheonSettings(**_toml_section(toml_config, "pantheon")),
        "knemon": KnemonSettings(**_toml_section(toml_config, "knemon")),
        "federation": _FederationSettings(**_toml_section(toml_config, "federation")),
        "oauth": _OAuthSettings(**_toml_section(toml_config, "oauth")),
        "auth": _AuthSettings(**_toml_section(toml_config, "auth")),
        "runtime": _RuntimeSettings(**_toml_section(toml_config, "runtime")),
        "tools": _ToolSettings(**_toml_section(toml_config, "tools")),
        "logging": _LoggingSettings(**_toml_section(toml_config, "logging")),
        "nats": _NatsSettings(**_toml_section(toml_config, "nats")),
        "audit": _AuditSettings(**_toml_section(toml_config, "audit")),
        "hive_mind": _HiveMindSettings(**_toml_section(toml_config, "hive_mind")),
    }
    settings = Settings(
        layers=groups["layers"],
        database=groups["database"],
        graeae=groups["graeae"],
        server=groups["server"],
        services=groups["services"],
        webhook=groups["webhook"],
        providers=groups["providers"],
        mcp=groups["mcp"],
        rate_limit=groups["rate_limit"],
        resilience=groups["resilience"],
        observability=groups["observability"],
        compression=groups["compression"],
        artemis=groups["artemis"],
        morpheus=groups["morpheus"],
        persephone=groups["persephone"],
        kronos=groups["kronos"],
        pantheon=groups["pantheon"],
        knemon=groups["knemon"],
        federation=groups["federation"],
        oauth=groups["oauth"],
        auth=groups["auth"],
        runtime=groups["runtime"],
        tools=groups["tools"],
        logging=groups["logging"],
        nats=groups["nats"],
        audit=groups["audit"],
        hive_mind=groups["hive_mind"],
    )
    settings._explicit_fields = {
        group_name: set(group.model_fields_set)
        for group_name, group in groups.items()
        if isinstance(group, BaseSettings)
    }
    _apply_profile_defaults(settings)
    settings.services.resolution = resolve_profile_services(
        profile=settings.profile,
        managed=settings.services.managed,
        selected_components=parse_component_selection(settings.services.selected_components),
        env=os.environ,
    )
    return settings


def _profile_from_sources(
    toml_config: dict[str, Any],
    server_toml: dict[str, Any],
    server: _ServerSettings,
) -> str:
    override = os.environ.get("MNEMOS_PROFILE_OVERRIDE", "").strip()
    if override:
        return override
    if "profile" in server_toml:
        return str(server_toml["profile"])
    deployment_toml = _toml_section(toml_config, "deployment")
    if "profile" in deployment_toml:
        return str(deployment_toml["profile"])
    return server.profile


def _apply_profile_defaults(settings: Settings) -> None:
    profile_defaults = PROFILE_DEFAULTS[settings.profile]
    for profile_key, value in profile_defaults.items():
        target = _PROFILE_DEFAULT_TARGETS.get(profile_key)
        if target is None:
            continue
        group_name, field_name = target
        if field_name in settings.explicit_fields(group_name):
            continue
        setattr(getattr(settings, group_name), field_name, value)


def _load_toml() -> dict[str, Any]:
    for toml_path in _config_paths():
        if toml_path.exists():
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def _config_paths() -> list[Path]:
    paths: list[Path] = []
    configured_path = os.environ.get("MNEMOS_CONFIG_PATH", "").strip()
    if configured_path:
        paths.append(Path(configured_path).expanduser())
    paths.extend(
        [
            Path.cwd() / "config.toml",
            Path(__file__).resolve().parents[2] / "config.toml",
            Path("/etc/mnemos/config.toml"),
        ]
    )

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = path.resolve() if path.exists() else path
        if normalized not in seen:
            unique.append(path)
            seen.add(normalized)
    return unique


def _toml_section(toml_config: dict[str, Any], section: str) -> dict[str, Any]:
    value = toml_config.get(section, {})
    return value if isinstance(value, dict) else {}


def _sync_compat_exports(settings: Settings) -> None:
    PG_CONFIG.clear()
    PG_CONFIG.update(settings.database.model_dump(mode="python"))
    GRAEAE_CONFIG.clear()
    GRAEAE_CONFIG.update(settings.graeae.model_dump(mode="python"))


def reload_settings() -> Settings:
    """Rebuild the settings singleton after changing env/config inputs."""
    global _settings
    _settings = _build_settings()
    _sync_compat_exports(_settings)
    return _settings


def set_profile_override(profile_value: str) -> Settings:
    """Pin the active deployment profile and refresh settings.

    Centralised so the CLI doesn't have to write os.environ directly
    (the env-discipline lint allowlists writes only in this module).
    Sets BOTH MNEMOS_PROFILE_OVERRIDE (the takes-precedence override)
    and MNEMOS_PROFILE (the default-from-env path) so any subprocess
    spawned afterwards inherits the same selection.
    """
    os.environ["MNEMOS_PROFILE_OVERRIDE"] = profile_value
    os.environ["MNEMOS_PROFILE"] = profile_value
    return reload_settings()


def runtime_env_value(name: str, default: str = "") -> str:
    """Return a raw environment value for dynamic-name runtime accessors."""
    return os.environ.get(name, default)


def runtime_env_value_stripped(name: str, default: str = "") -> str:
    """Return a stripped environment value for dynamic-name runtime accessors."""
    return runtime_env_value(name, default).strip()


def runtime_env_int(name: str, default: int) -> int:
    raw = runtime_env_value_stripped(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def runtime_env_float(name: str, default: float) -> float:
    raw = runtime_env_value_stripped(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def embedding_dim_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBEDDING_DIM", "768"))


def oracle_dsn_env() -> str:
    return runtime_env_value_stripped("ORACLE_DSN")


def db2_dsn_env() -> str:
    return runtime_env_value_stripped("DB2_DSN")


def required_capabilities_env() -> str:
    return runtime_env_value("MNEMOS_REQUIRE_CAPABILITIES", "")


def vector_dim_max_env() -> int:
    raw = runtime_env_value_stripped("MNEMOS_VECTOR_DIM_MAX")
    if not raw:
        return 4096
    try:
        parsed = int(raw)
    except ValueError:
        return 4096
    return parsed if parsed > 0 else 4096


def embed_http_model_override() -> str:
    """Return the explicit MNEMOS_EMBED_HTTP_MODEL env override, if set."""
    return runtime_env_value_stripped("MNEMOS_EMBED_HTTP_MODEL")


def embed_backend_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_BACKEND", "auto")


def embed_ov_model_id_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_OV_MODEL_ID", "BAAI/bge-base-en-v1.5")


def embed_ov_device_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_OV_DEVICE", "AUTO")


def embed_model_path_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_MODEL_PATH", "/opt/mnemos/models/nomic-embed-text-v1.5.Q8_0.gguf")


def embed_n_ctx_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBED_N_CTX", "8192"))


def embed_threads_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBED_THREADS", str(max(1, os.cpu_count() or 4))))


def embed_gpu_layers_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBED_GPU_LAYERS", "0"))


def embed_cix_model_path_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_CIX_MODEL_PATH", "/opt/mnemos/models/bge-small-zh-v1.5_256.cix")


def embed_cix_tokenizer_id_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_CIX_TOKENIZER_ID", "BAAI/bge-small-zh-v1.5")


def embed_cix_max_seq_len_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBED_CIX_MAX_SEQ_LEN", "256"))


def embed_hybrid_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_HYBRID", "False")


def embed_npu_threshold_chars_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBED_NPU_THRESHOLD_CHARS", "1000"))


def embed_http_url_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_HTTP_URL", "http://192.168.207.61:8090/v1/embeddings")


def embed_http_url_fallback_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_HTTP_URL_FALLBACK", "http://192.168.207.64:8090/v1/embeddings")


def embed_http_model_env() -> str:
    return runtime_env_value("MNEMOS_EMBED_HTTP_MODEL", "bge-m3")


def embed_http_timeout_env() -> float:
    return float(runtime_env_value("MNEMOS_EMBED_HTTP_TIMEOUT", "30.0"))


def embed_max_chars_env() -> int:
    return int(runtime_env_value("MNEMOS_EMBED_MAX_CHARS", "8000"))


def reranker_url_env() -> str:
    return runtime_env_value("MNEMOS_RERANKER_URL", "http://192.168.207.64:8091/v1/rerank")


def reranker_model_env() -> str:
    return runtime_env_value("MNEMOS_RERANKER_MODEL", "bge-reranker-v2-m3")


def reranker_timeout_secs_env() -> str | None:
    return os.environ.get("MNEMOS_RERANKER_TIMEOUT_SECS")


def morpheus_orphan_timeout_hours_env() -> str | None:
    return os.environ.get("MNEMOS_MORPHEUS_ORPHAN_TIMEOUT_HOURS")


def kronos_backend_env() -> str:
    return runtime_env_value("MNEMOS_KRONOS_BACKEND", "auto").strip().lower()


def oracle_pdb_env() -> str:
    return runtime_env_value_stripped("MNEMOS_ORACLE_PDB")


def db2_vector_index_override() -> str | None:
    """Return the raw Db2 vector-index env override, if present."""
    if "MNEMOS_DB2_VECTOR_INDEX" not in os.environ:
        return None
    return os.environ.get("MNEMOS_DB2_VECTOR_INDEX")


def db2_vector_indexing_override() -> str | None:
    """Return the raw ``DB2_VECTOR_INDEXING`` env override, if present.

    Distinguishes "unset" (``None``) from "set to a disabling value" so a caller
    can treat an explicit non-truthy value as an opt-out of vector-index DDL.
    """
    if "DB2_VECTOR_INDEXING" not in os.environ:
        return None
    return os.environ.get("DB2_VECTOR_INDEXING")


def db2_text_search_override() -> str | None:
    """Return the raw Db2 full-text-search env override, if present.

    ``MNEMOS_DB2_TEXT_SEARCH=contains`` opts into the Db2 Text Search
    ``CONTAINS()`` predicate (engages a Db2 text-search index); the default
    ``like`` keeps the stock substring scan that needs no Text Search server.
    """
    if "MNEMOS_DB2_TEXT_SEARCH" not in os.environ:
        return None
    return os.environ.get("MNEMOS_DB2_TEXT_SEARCH")


def nats_webhooks_queue_group_env() -> str:
    return runtime_env_value_stripped("MNEMOS_NATS_WEBHOOKS_QUEUE_GROUP")


def nats_federation_queue_group_env() -> str:
    return runtime_env_value_stripped("MNEMOS_NATS_FEDERATION_QUEUE_GROUP")


def nats_webhooks_enabled() -> bool:
    """Return whether webhook outbox NATS publishing/consuming is enabled."""
    return get_settings().services.resolution.enabled("nats_webhooks")


def nats_federation_enabled() -> bool:
    """Return whether federation memory NATS publishing/consuming is enabled."""
    return get_settings().services.resolution.enabled("nats_federation")


def session_secret_required() -> bool:
    """Return whether startup must fail when MNEMOS_SESSION_SECRET is unset."""
    return runtime_env_value_stripped("MNEMOS_REQUIRE_SESSION_SECRET").lower() in {"yes", "1", "true"}


def audit_chain_enabled_flag() -> bool:
    """Return whether MNEMOS_AUDIT_CHAIN enables audit-chain writes."""
    return runtime_env_value("MNEMOS_AUDIT_CHAIN", "").lower() == "on"


def system_hive_url_env() -> str:
    return runtime_env_value("HIVE_URL", "http://192.168.207.8:5005")


def mcp_hive_url_env() -> str:
    return runtime_env_value("HIVE_URL", "http://127.0.0.1:5005")


def agent_host_env() -> str:
    return runtime_env_value("AGENT_HOST", socket.gethostname().split(".")[0])


def heartbeat_interval_env() -> float:
    return float(runtime_env_value("HEARTBEAT_INTERVAL", "15"))


def claim_jobs_env() -> str:
    return runtime_env_value("CLAIM_JOBS", "0")


def claim_jobs_enabled_env() -> bool:
    return claim_jobs_env() == "1"


def mcp_mnemos_url_env() -> str:
    return runtime_env_value("MNEMOS_URL", "http://192.168.207.67:5002")


def mcp_mnemos_token_env() -> str:
    return runtime_env_value(
        "MNEMOS_TOKEN",
        "",  # no hardcoded fallback; set MNEMOS_TOKEN in env (leaked token, rotate server-side),
    )


def mcp_port_env() -> int:
    return int(runtime_env_value("PORT", "5006"))


def agent_bus_db_env() -> str:
    return runtime_env_value("AGENT_BUS_DB", "/srv/agent-bus/agents.db")


def connector_default_namespace() -> str | None:
    """Return the MCP connector namespace override, if explicitly set."""
    value = os.environ.get("MNEMOS_DEFAULT_NAMESPACE", "").strip()
    return value or None


def hot_rs_enabled() -> bool:
    """Return whether optional Rust hot-path acceleration is enabled."""
    return os.environ.get("MNEMOS_HOT_RS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def mcp_nats_raw_enabled() -> bool:
    """True if MNEMOS_MCP_NATS_RAW is set to a truthy value.

    Bypass for the JSON-summary path on NATS-backed MCP SSE streams —
    when set, the raw NATS message body is forwarded verbatim instead
    of being re-shaped into a {subject, summary} envelope.
    """
    return os.getenv("MNEMOS_MCP_NATS_RAW", "").strip().lower() in {"1", "true", "yes", "on"}


def _reset_settings_for_tests() -> None:
    """Clear the singleton and refresh compatibility dicts.

    This is intentionally not used by application code. It exists so tests can
    exercise environment/config-file overrides without process isolation.
    """
    reload_settings()


_sync_compat_exports(get_settings())
