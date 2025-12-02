from pydantic import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: AnyUrl | str = "sqlite:///./iperf.db"
    request_timeout: int = 15


settings = Settings()
