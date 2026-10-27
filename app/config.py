from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    app_name: str = "CredResolve SmartDialer"
    database_url: str = "sqlite:///./smartdialer.db"
    max_predictive_overdial_ratio: float = 1.25
    max_ringing_calls_per_agent: float = 1.0
    provider_failure_threshold: int = 3

settings = Settings()
