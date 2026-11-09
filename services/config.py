import os
import secrets
from pathlib import Path

from services.storage import ROOT


class Settings:
    def __init__(self, overrides=None):
        env = {}
        path = ROOT / ".env"
        if path.exists():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    key, sep, value = line.partition("=")
                    if not sep:
                        raise ValueError("Invalid .env entry")
                    env[key.strip()] = value.strip().strip('"').strip("'")
        env.update(os.environ)
        env.update(overrides or {})
        self.host = env.get("HOST", "127.0.0.1")
        self.port = int(env.get("PORT", "8765"))
        self.public_demo = env.get("PUBLIC_DEMO", "0") == "1"
        self.persistent_storage = env.get("PERSISTENT_STORAGE", "0") == "1"
        self.admin = env.get("ADMIN_TOKEN") or secrets.token_urlsafe(32)
        self.ephemeral_admin = not self.public_demo and not env.get("ADMIN_TOKEN")
        self.artifacts = Path(env.get("ARTIFACT_DIR", "artifacts"))
        state = self.artifacts / "public" if self.public_demo else self.artifacts
        database = env.get("DATABASE_URL", f"sqlite:///{state / 'app.sqlite3'}")
        if not database.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must be a sqlite:/// URL")
        self.database = Path(database.removeprefix("sqlite:///"))
        self.business = Path(env.get("BUSINESS_DATABASE", state / "business.sqlite3"))
        if env.get("BUSINESS_ADAPTER", "mock") != "mock":
            raise ValueError("Official adapter requires the organizer's sandbox contract")
        self.demo = not self.public_demo and env.get("ENABLE_DEMO_FAULTS", "1") == "1"
        self.session_ttl = int(env.get("SESSION_TTL", "3600"))
        self.confirmation_ttl = int(env.get("CONFIRMATION_TTL", "120"))
        if self.session_ttl <= 0 or self.confirmation_ttl <= 0:
            raise ValueError("Session and confirmation TTL must be positive")
        ws = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
        self.providers = {}
        for name, endpoint, model in [
            ("ASR", ws, "paraformer-realtime-v2"),
            ("LLM", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", "qwen-plus"),
            ("TTS", ws, "cosyvoice-v2"),
        ]:
            provider = env.get(f"{name}_PROVIDER", "mock")
            key = env.get(f"{name}_API_KEY") or env.get("DASHSCOPE_API_KEY")
            url = env.get(f"{name}_ENDPOINT", endpoint)
            if provider not in {"mock", "dashscope"}:
                raise ValueError(f"Unsupported {name}_PROVIDER")
            if provider == "dashscope" and not key:
                raise ValueError(f"{name}_API_KEY or DASHSCOPE_API_KEY is required")
            scheme = "https://" if name == "LLM" else "wss://"
            if provider == "dashscope" and not url.startswith(scheme):
                raise ValueError(f"{name}_ENDPOINT must start with {scheme}")
            self.providers[name] = (provider, url, env.get(f"{name}_MODEL", model), key)
        self.voice = env.get("TTS_VOICE", "longxiaochun_v2")
        self.audio = all(self.providers[name][0] == "dashscope" for name in ("ASR", "TTS"))
