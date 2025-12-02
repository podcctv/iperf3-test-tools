from pathlib import Path

from pydantic import AnyUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: AnyUrl | str = "sqlite:///./iperf.db"
    request_timeout: int = 15
    dashboard_password: str = "iperf-pass"
    dashboard_secret: str = "iperf-dashboard-secret"
    dashboard_cookie_name: str = "iperf_dashboard_auth"
    agent_config_path: str = str(Path(__file__).resolve().parent.parent / "agent_configs.json")
    agent_image: str = "iperf-agent:latest"

    @property
    def agent_config_file(self) -> Path:
        return Path(self.agent_config_path)


settings = Settings()
