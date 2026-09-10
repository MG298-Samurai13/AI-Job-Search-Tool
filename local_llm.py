"""Local LLM client for the offline drafting MVP.

Talks to an OpenAI-compatible local endpoint using the standard
``/v1/chat/completions`` endpoint, so the server (Ollama, LM Studio, etc.)
applies its own correct chat template internally instead of us hand-building
one. This keeps things portable across models/backends without needing to
match a specific model's special-token format.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_MODEL = "qwen/qwen3.6-27b"

# Preference order when more than one usable model is offered. Substring match
# against the id (case-insensitive).
RECOMMENDED_MODELS = ("qwen3.6-27b", "qwen3.5-9b", "qwen2.5")


def resolve_model(available: list[str], preferred: str | None = None) -> str | None:
    """Pick which model id to use from what the endpoint currently offers."""
    if preferred and preferred in available:
        return preferred
    usable = [m for m in available if "embed" not in m.lower()]
    if len(usable) == 1:
        return usable[0]
    for rec in RECOMMENDED_MODELS:
        for m in usable:
            if rec in m.lower():
                return m
    return usable[0] if usable else None


class LocalLLMError(RuntimeError):
    """Raised when the endpoint is unreachable or returns unusable output."""


@dataclass
class LocalLLM:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: int = 180
    temperature: float = 0.0

    def is_up(self, connect_timeout: float = 1.5) -> bool:
        parsed = urlparse(self.base_url)
        host, port = parsed.hostname or "localhost", parsed.port or 80
        try:
            with socket.create_connection((host, port), timeout=connect_timeout):
                return True
        except OSError:
            return False

    def list_loaded_models(self, connect_timeout: float = 2.0) -> list[str] | None:
        parsed = urlparse(self.base_url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        try:
            req = urllib.request.Request(f"{root}/api/v0/models")
            with urllib.request.urlopen(req, timeout=connect_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (OSError, TimeoutError, ValueError):
            return None
        return sorted(
            str(m.get("id")) for m in body.get("data", [])
            if m.get("state") == "loaded" and m.get("type") != "embeddings" and m.get("id")
        )

    def list_models(self, connect_timeout: float = 2.0) -> list[str]:
        try:
            req = urllib.request.Request(f"{self.base_url}/models")
            with urllib.request.urlopen(req, timeout=connect_timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (OSError, TimeoutError, ValueError):
            return []
        return sorted(str(m.get("id")) for m in body.get("data", []) if m.get("id"))

    def complete_text(
        self, system: str, user: str, max_tokens: int = 800, prefill: str = ""
    ) -> str:
        content = user.strip()
        if prefill:
            content += f"\n\n(Begin your reply with exactly: {prefill})"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system.strip()},
                {"role": "user", "content": content},
            ],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"), strict=False)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except OSError:
                detail = ""
            raise LocalLLMError(
                f"endpoint returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise LocalLLMError(f"local endpoint unreachable at {self.base_url}: {exc}") from exc
        return (body["choices"][0]["message"].get("content") or "").strip()

    def complete_json(
        self, system: str, user: str, max_tokens: int = 900, retries: int = 2
    ) -> dict:
        sys_prompt = system
        last_err: Exception | None = None
        for _ in range(retries + 1):
            text = self.complete_text(sys_prompt, user, max_tokens=max_tokens, prefill="{")
            for candidate in (text, "{" + text):
                try:
                    return extract_json(candidate)
                except (ValueError, json.JSONDecodeError) as exc:
                    last_err = exc
            sys_prompt = (
                f"{system}\n\nReturn ONLY a single valid JSON object, no prose, "
                f"no markdown fences."
            )
        raise LocalLLMError(f"no valid JSON after {retries + 1} attempts: {last_err}")


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of a model's text output."""
    s = re.sub(r"```(?:json)?", "", text).replace("```", "").strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in output: {text[:120]!r}")
    obj = json.loads(s[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("parsed JSON is not an object")
    return obj