from pydantic import BaseModel
import os


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    database_url: str = os.getenv("DATABASE_URL", "postgresql://app_user:app_pass@db:5432/hrchat")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8080/v1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "dummy")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    openai_reasoning_enabled: bool = _as_bool(os.getenv("OPENAI_REASONING_ENABLED"), False)
    # Seconds to wait for the model. Planning is a short JSON reply; the answer is streamed.
    llm_plan_timeout: float = float(os.getenv("LLM_PLAN_TIMEOUT", "30"))
    llm_answer_timeout: float = float(os.getenv("LLM_ANSWER_TIMEOUT", "90"))


settings = Settings()
