from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CITEVYN_",
        env_file=".env",
        extra="ignore",
    )

    app_name: str = "CiteVyn AI Backend"
    environment: str = "local"
    demo_api_key: str = Field(default="local-demo-key", min_length=1)
    request_id_header: str = "X-Request-ID"


@lru_cache
def get_settings() -> Settings:
    return Settings()
