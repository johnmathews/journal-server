"""Tests for configuration loading."""


import pytest

from journal.config import Config


class TestAllowedHosts:
    def test_default_allowed_hosts_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default (no env var) must be loopback-only so DNS rebinding
        # protection is always meaningful. An empty list would have
        # previously let mcp_server disable the protection entirely.
        monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
        config = Config()
        assert config.mcp_allowed_hosts == ["127.0.0.1", "localhost"]

    def test_allowed_hosts_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "MCP_ALLOWED_HOSTS", "192.168.2.105:8000,localhost:8000"
        )
        config = Config()
        assert config.mcp_allowed_hosts == [
            "192.168.2.105:8000",
            "localhost:8000",
        ]

    def test_allowed_hosts_strips_whitespace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "MCP_ALLOWED_HOSTS", " 192.168.2.105:8000 , localhost:8000 "
        )
        config = Config()
        assert config.mcp_allowed_hosts == [
            "192.168.2.105:8000",
            "localhost:8000",
        ]

    def test_allowed_hosts_ignores_empty_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "192.168.2.105:8000,,")
        config = Config()
        assert config.mcp_allowed_hosts == ["192.168.2.105:8000"]

    def test_allowed_hosts_wildcard_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_ALLOWED_HOSTS", "192.168.2.105:*")
        config = Config()
        assert config.mcp_allowed_hosts == ["192.168.2.105:*"]


class TestOcrContext:
    def test_default_context_dir_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OCR_CONTEXT_DIR", raising=False)
        config = Config()
        assert config.ocr_context_dir is None

    def test_context_dir_from_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: object
    ) -> None:
        monkeypatch.setenv("OCR_CONTEXT_DIR", "/etc/journal/context")
        config = Config()
        assert config.ocr_context_dir is not None
        assert str(config.ocr_context_dir) == "/etc/journal/context"

    def test_default_cache_ttl_is_1h(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OCR_CONTEXT_CACHE_TTL", raising=False)
        config = Config()
        assert config.ocr_context_cache_ttl == "1h"

    def test_cache_ttl_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OCR_CONTEXT_CACHE_TTL", "5m")
        config = Config()
        assert config.ocr_context_cache_ttl == "5m"


class TestRetiredApiBearerToken:
    def test_api_bearer_token_field_removed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The JOURNAL_API_TOKEN bearer-token scheme was replaced by the
        # multi-user session / API-key auth layer. Setting the old env
        # var must have no effect on config.
        monkeypatch.setenv("JOURNAL_API_TOKEN", "leftover-token")
        config = Config()
        assert not hasattr(config, "api_bearer_token")


class TestPreprocessImages:
    def test_default_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PREPROCESS_IMAGES", raising=False)
        assert Config().preprocess_images is True

    def test_env_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PREPROCESS_IMAGES", "false")
        assert Config().preprocess_images is False

    def test_env_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PREPROCESS_IMAGES", "0")
        assert Config().preprocess_images is False


class TestOcrDualPass:
    def test_default_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OCR_DUAL_PASS", raising=False)
        assert Config().ocr_dual_pass is False

    def test_env_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCR_DUAL_PASS", "true")
        assert Config().ocr_dual_pass is True

    def test_env_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OCR_DUAL_PASS", "1")
        assert Config().ocr_dual_pass is True


class TestHybridSearch:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "HYBRID_BM25_CANDIDATES",
            "HYBRID_DENSE_CANDIDATES",
            "HYBRID_FUSION_TOP_M",
            "HYBRID_RRF_K",
            "HYBRID_RERANKER",
            "RERANKER_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)
        c = Config()
        assert c.hybrid_bm25_candidates == 50
        assert c.hybrid_dense_candidates == 50
        assert c.hybrid_fusion_top_m == 30
        assert c.hybrid_rrf_k == 60
        assert c.hybrid_reranker == "anthropic"
        assert c.reranker_model == "claude-haiku-4-5"

    def test_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HYBRID_BM25_CANDIDATES", "20")
        monkeypatch.setenv("HYBRID_DENSE_CANDIDATES", "40")
        monkeypatch.setenv("HYBRID_FUSION_TOP_M", "15")
        monkeypatch.setenv("HYBRID_RRF_K", "30")
        monkeypatch.setenv("HYBRID_RERANKER", "none")
        monkeypatch.setenv("RERANKER_MODEL", "claude-sonnet-4-6")
        c = Config()
        assert c.hybrid_bm25_candidates == 20
        assert c.hybrid_dense_candidates == 40
        assert c.hybrid_fusion_top_m == 15
        assert c.hybrid_rrf_k == 30
        assert c.hybrid_reranker == "none"
        assert c.reranker_model == "claude-sonnet-4-6"


class TestAuthRateLimitConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "AUTH_RATE_LIMIT_ENABLED",
            "AUTH_RATE_LIMIT_MAX_REQUESTS",
            "AUTH_RATE_LIMIT_WINDOW_SECONDS",
        ):
            monkeypatch.delenv(var, raising=False)
        c = Config()
        assert c.auth_rate_limit_enabled is True
        assert c.auth_rate_limit_max_requests == 10
        assert c.auth_rate_limit_window_seconds == 300

    def test_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AUTH_RATE_LIMIT_ENABLED", "false")
        monkeypatch.setenv("AUTH_RATE_LIMIT_MAX_REQUESTS", "25")
        monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "60")
        c = Config()
        assert c.auth_rate_limit_enabled is False
        assert c.auth_rate_limit_max_requests == 25
        assert c.auth_rate_limit_window_seconds == 60


def _clean_transcription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TRANSCRIPTION_PROVIDER",
        "TRANSCRIPTION_FALLBACK_ENABLED",
        "TRANSCRIPTION_FALLBACK_MODEL",
        "TRANSCRIPTION_RETRY_MAX_ATTEMPTS",
        "TRANSCRIPTION_RETRY_BASE_DELAY",
        "TRANSCRIPTION_RETRY_MAX_DELAY",
        "TRANSCRIPTION_SHADOW_PROVIDER",
        "TRANSCRIPTION_SHADOW_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


class TestTranscriptionProviderConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_transcription_env(monkeypatch)
        config = Config()
        assert config.transcription_provider == "openai"
        assert config.transcription_fallback_enabled is True
        assert config.transcription_fallback_model == "whisper-1"
        assert config.transcription_retry_max_attempts == 3
        assert config.transcription_retry_base_delay == 1.0
        assert config.transcription_retry_max_delay == 30.0
        assert config.transcription_shadow_provider == ""
        assert config.transcription_shadow_model == ""

    def test_provider_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "gemini")
        assert Config().transcription_provider == "gemini"

    def test_fallback_disabled_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_ENABLED", "false")
        assert Config().transcription_fallback_enabled is False

    def test_fallback_model_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_FALLBACK_MODEL", "gpt-4o-mini-transcribe")
        assert Config().transcription_fallback_model == "gpt-4o-mini-transcribe"

    def test_retry_settings_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_RETRY_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("TRANSCRIPTION_RETRY_BASE_DELAY", "2.5")
        monkeypatch.setenv("TRANSCRIPTION_RETRY_MAX_DELAY", "60")
        config = Config()
        assert config.transcription_retry_max_attempts == 5
        assert config.transcription_retry_base_delay == 2.5
        assert config.transcription_retry_max_delay == 60.0

    def test_shadow_provider_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "gemini")
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_MODEL", "gemini-2.5-flash")
        config = Config()
        assert config.transcription_shadow_provider == "gemini"
        assert config.transcription_shadow_model == "gemini-2.5-flash"

    def test_invalid_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_PROVIDER", "foo")
        with pytest.raises(ValueError, match="TRANSCRIPTION_PROVIDER"):
            Config()

    def test_invalid_shadow_provider_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "foo")
        with pytest.raises(ValueError, match="TRANSCRIPTION_SHADOW_PROVIDER"):
            Config()

    def test_empty_shadow_provider_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_SHADOW_PROVIDER", "")
        # Empty string disables shadow — must not raise.
        Config()

    def test_zero_max_attempts_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_RETRY_MAX_ATTEMPTS", "0")
        with pytest.raises(ValueError, match="TRANSCRIPTION_RETRY_MAX_ATTEMPTS"):
            Config()

    def test_negative_base_delay_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_RETRY_BASE_DELAY", "-1")
        with pytest.raises(ValueError, match="TRANSCRIPTION_RETRY_BASE_DELAY"):
            Config()

    def test_negative_max_delay_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clean_transcription_env(monkeypatch)
        monkeypatch.setenv("TRANSCRIPTION_RETRY_MAX_DELAY", "-5")
        with pytest.raises(ValueError, match="TRANSCRIPTION_RETRY_MAX_DELAY"):
            Config()


class TestFitnessConfig:
    """Defaults + env-var overrides for the fitness pipeline fields
    added in W3 of docs/fitness-tier-plan.md."""

    def _clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # W6: GARMIN_USERNAME / GARMIN_PASSWORD are no longer config fields.
        # Per-user Garmin credentials live in `fitness_auth_state`.
        for key in (
            "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REDIRECT_URI",
            "STRAVA_ENABLED",
            "FITNESS_TRANSIENT_FAILURE_THRESHOLD", "FITNESS_BACKFILL_START",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty creds are tolerated at construct-time — code that needs
        them errors at use-site, matching how anthropic_api_key works."""
        self._clean(monkeypatch)
        config = Config()
        assert config.strava_client_id == ""
        assert config.strava_client_secret == ""
        assert config.strava_redirect_uri == "http://localhost:8400/strava/callback"
        # No Garmin fields on Config post-W6.
        assert not hasattr(config, "garmin_username")
        assert not hasattr(config, "garmin_password")
        assert config.fitness_transient_failure_threshold == 3
        assert config.fitness_backfill_start == "2026-01-01"
        # Strava is mothballed (roadmap D8): the integration is dark
        # unless the operator explicitly opts back in.
        assert config.strava_enabled is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_strava_enabled_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str,
    ) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("STRAVA_ENABLED", value)
        assert Config().strava_enabled is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "banana"])
    def test_strava_enabled_falsy_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str,
    ) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("STRAVA_ENABLED", value)
        assert Config().strava_enabled is False

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
        monkeypatch.setenv("STRAVA_CLIENT_SECRET", "shh")
        monkeypatch.setenv("STRAVA_REDIRECT_URI", "http://localhost:9000/cb")
        monkeypatch.setenv("FITNESS_TRANSIENT_FAILURE_THRESHOLD", "5")
        monkeypatch.setenv("FITNESS_BACKFILL_START", "2024-06-01")
        config = Config()
        assert config.strava_client_id == "12345"
        assert config.strava_client_secret == "shh"
        assert config.strava_redirect_uri == "http://localhost:9000/cb"
        assert config.fitness_transient_failure_threshold == 5
        assert config.fitness_backfill_start == "2024-06-01"

    def test_garmin_env_vars_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """W6 acceptance: setting GARMIN_USERNAME / GARMIN_PASSWORD does
        not produce config fields. Operators may leave the vestigial env
        vars set in prod during the transition; they must have no effect."""
        self._clean(monkeypatch)
        monkeypatch.setenv("GARMIN_USERNAME", "leftover@example.com")
        monkeypatch.setenv("GARMIN_PASSWORD", "leftover_pw")
        config = Config()
        assert not hasattr(config, "garmin_username")
        assert not hasattr(config, "garmin_password")

    def test_zero_threshold_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("FITNESS_TRANSIENT_FAILURE_THRESHOLD", "0")
        with pytest.raises(ValueError, match="FITNESS_TRANSIENT_FAILURE_THRESHOLD"):
            Config()


class TestFitnessCredentialKey:
    """FITNESS_CREDENTIAL_KEY (W4): optional Fernet key for encrypted
    saved Garmin credentials. Unset = feature off; malformed = fail
    fast at startup with the generation command in the message."""

    def test_unset_key_defaults_to_empty(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Optional-secret idiom (like SMTP_PASSWORD): absence is valid."""
        monkeypatch.delenv("FITNESS_CREDENTIAL_KEY", raising=False)
        config = Config()
        assert config.fitness_credential_key == ""

    def test_empty_key_is_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FITNESS_CREDENTIAL_KEY", "")
        config = Config()
        assert config.fitness_credential_key == ""

    def test_valid_key_stored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()
        monkeypatch.setenv("FITNESS_CREDENTIAL_KEY", key)
        config = Config()
        assert config.fitness_credential_key == key

    def test_malformed_key_fails_startup(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FITNESS_CREDENTIAL_KEY", "not-a-fernet-key")
        with pytest.raises(ValueError, match="FITNESS_CREDENTIAL_KEY"):
            Config()

    def test_malformed_key_error_includes_generation_command(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The failure message must tell the operator how to mint a key."""
        monkeypatch.setenv("FITNESS_CREDENTIAL_KEY", "not-a-fernet-key")
        with pytest.raises(ValueError, match=r"Fernet\.generate_key"):
            Config()


class TestJobWorkerCount:
    """`JOB_WORKER_COUNT` controls Pool A (ingestion/fast) worker count
    for the background job runner. The storyline pool is always
    single-worker and has no knob."""

    def test_default_is_four(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JOB_WORKER_COUNT", raising=False)
        config = Config()
        assert config.job_worker_count == 4
        assert isinstance(config.job_worker_count, int)

    def test_env_override_parses_to_int(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("JOB_WORKER_COUNT", "8")
        config = Config()
        assert config.job_worker_count == 8
        assert isinstance(config.job_worker_count, int)

    def test_zero_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOB_WORKER_COUNT", "0")
        with pytest.raises(ValueError, match="JOB_WORKER_COUNT"):
            Config()

    def test_negative_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOB_WORKER_COUNT", "-1")
        with pytest.raises(ValueError, match="JOB_WORKER_COUNT"):
            Config()


def test_fitness_sync_enabled_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FITNESS_SYNC_ENABLED", raising=False)
    assert Config().fitness_sync_enabled is True


def test_fitness_sync_enabled_respects_env_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FITNESS_SYNC_ENABLED", "false")
    assert Config().fitness_sync_enabled is False


def test_answer_config_defaults(monkeypatch):
    for var in ("ANSWER_PROVIDER", "ANSWER_MODEL", "ANSWER_CONTEXT_ENTRIES"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config()
    assert cfg.answer_provider == "anthropic"
    assert cfg.answer_model == "claude-sonnet-4-6"
    assert cfg.answer_context_entries == 8


def test_answer_config_from_env(monkeypatch):
    monkeypatch.setenv("ANSWER_PROVIDER", "none")
    monkeypatch.setenv("ANSWER_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("ANSWER_CONTEXT_ENTRIES", "5")
    cfg = Config()
    assert cfg.answer_provider == "none"
    assert cfg.answer_model == "claude-haiku-4-5"
    assert cfg.answer_context_entries == 5


def test_storyline_judge_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORYLINE_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("STORYLINE_MIN_PUBLISH_ENTRIES", raising=False)
    cfg = Config()
    assert cfg.storyline_judge_model == "claude-haiku-4-5"
    assert cfg.storyline_min_publish_entries == 3


def test_storyline_judge_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORYLINE_JUDGE_MODEL", "claude-opus-4-7")
    monkeypatch.setenv("STORYLINE_MIN_PUBLISH_ENTRIES", "5")
    cfg = Config()
    assert cfg.storyline_judge_model == "claude-opus-4-7"
    assert cfg.storyline_min_publish_entries == 5


def test_removed_storyline_knobs_are_gone() -> None:
    cfg = Config()
    assert not hasattr(cfg, "storyline_glue_model")
    assert not hasattr(cfg, "storyline_chapter_target_words")
    assert not hasattr(cfg, "storyline_chapter_min_words")
    assert not hasattr(cfg, "storyline_chapter_max_words")
    assert not hasattr(cfg, "storyline_fts_fallback_threshold")
    assert not hasattr(cfg, "storyline_default_window_days")


def test_min_entry_date_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MIN_ENTRY_DATE", raising=False)
    assert Config().min_entry_date == "2026-01-01"
    monkeypatch.setenv("MIN_ENTRY_DATE", "2026-03-01")
    assert Config().min_entry_date == "2026-03-01"


def test_min_entry_date_invalid_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIN_ENTRY_DATE", "March 2026")
    with pytest.raises(ValueError, match="MIN_ENTRY_DATE"):
        Config()
