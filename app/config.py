from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = Field(default="ollama", alias="LLM_PROVIDER")

    ollama_base_url: str = Field(
        default="http://localhost:11434/v1",
        alias="OLLAMA_BASE_URL",
    )
    ollama_model: str = Field(default="qwen2.5:14b", alias="OLLAMA_MODEL")

    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    claude_model: str = Field(default="claude-sonnet-4-6", alias="CLAUDE_MODEL")

    splunk_host: str = Field(default="", alias="SPLUNK_HOST")
    splunk_port: int = Field(default=8089, alias="SPLUNK_PORT")
    splunk_token: str = Field(default="", alias="SPLUNK_TOKEN")
    splunk_debug_response: bool = Field(default=False, alias="SPLUNK_DEBUG_RESPONSE")

    checkpoint_db_path: Path = Field(
        default=Path("./data/checkpoints.sqlite"),
        alias="CHECKPOINT_DB_PATH",
    )
    domains_yaml_path: Path = Field(
        default=Path(__file__).resolve().parent / "domains.yaml",
        alias="DOMAINS_YAML_PATH",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ensure_data_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
