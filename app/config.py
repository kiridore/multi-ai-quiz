"""Configuration loader: .env + models.yaml + prompt.yaml."""
import hashlib
import hmac
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# 站点访问密钥（写死）。登录后签发会话 cookie，值由固定密钥的 HMAC 生成。
ACCESS_KEY = "MDAwNjA5"
SESSION_TOKEN = hmac.new(b"multi-ai-quiz", b"access-token", hashlib.sha256).hexdigest()


class AppConfig:
    def __init__(self) -> None:
        self.base_url: str = os.getenv(
            "OPENCODE_BASE_URL", "https://api.opencode-go.com/v1"
        ).rstrip("/")
        self.api_key: str = os.getenv("OPENCODE_API_KEY", "")
        self.db_path: str = os.getenv("OPENCODE_DB", str(BASE_DIR / "history.db"))
        models_path = Path(os.getenv("OPENCODE_MODELS_YAML", BASE_DIR / "models.yaml"))
        prompt_path = Path(os.getenv("OPENCODE_PROMPT_YAML", BASE_DIR / "prompt.yaml"))
        with models_path.open(encoding="utf-8") as f:
            self.models: list[dict] = yaml.safe_load(f)["models"]
        self.model_by_id: dict[str, dict] = {m["id"]: m for m in self.models}
        with prompt_path.open(encoding="utf-8") as f:
            self.default_prompt: str = yaml.safe_load(f)["template"]


config = AppConfig()
