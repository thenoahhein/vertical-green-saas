from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://sitesense:sitesense@localhost:5432/sitesense"
    redis_url: str = "redis://localhost:6379/0"
    dev_bearer_token: str = "dev-token"
    dev_organization_id: str = "00000000-0000-0000-0000-000000000001"
    dev_user_id: str = "00000000-0000-0000-0000-000000000002"
    object_store_endpoint: str = "http://localhost:9000"
    object_store_bucket: str = "sitesense"


@lru_cache
def get_settings() -> Settings:
    return Settings()

