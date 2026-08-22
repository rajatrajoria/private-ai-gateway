from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_keys: str = ""
    allowed_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 20
    ollama_base_url: str = "http://ollama:11434"
    # Must match the `cpus:` limit on the ollama service in docker-compose.yml.
    # Ollama auto-detects the host's full logical core count for its thread
    # pool, ignoring the cgroup CPU quota Docker actually enforces — mismatch
    # causes severe thread-contention slowdowns (see TECHNICAL_OVERVIEW.md).
    ollama_num_thread: int = 5
    # Ollama defaults to a small context window (often 4096 tokens) regardless
    # of what the model actually supports, unless told otherwise per-request.
    # Qwen2.5-7B-Instruct supports up to 32768 — this must be set explicitly
    # to actually use it (see TECHNICAL_OVERVIEW.md). Larger values use more
    # RAM for the KV cache and slow down prompt processing, so this is a
    # deliberate middle ground, not the model's true maximum.
    ollama_num_ctx: int = 16384

    @property
    def api_keys_by_secret(self) -> dict[str, str]:
        """Maps 'the actual key' -> 'the app name that owns it', parsed from
        API_KEYS="name:key,name:key". Keyed by secret so auth lookups are O(1)."""
        result: dict[str, str] = {}
        for pair in self.api_keys.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            name, _, key = pair.partition(":")
            if name and key:
                result[key] = name
        return result

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
