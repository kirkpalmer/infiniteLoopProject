"""HTTP client for the local Ollama Hermes model."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

LOGGER = logging.getLogger("infiniteloop.hermes.client")


class HermesClient:
    """Small Ollama API client for Hermes 3."""

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.model = model or os.getenv("OLLAMA_MODEL", "hermes3")
        self.timeout_seconds = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return response.ok
        except requests.RequestException:
            return False

    def generate(self, prompt: str, stream: bool = False) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": stream}
        response = requests.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if stream:
            return ""
        return str(data.get("response", ""))

    def _parse_relaxed_suggested_params(self, raw: str) -> dict[str, Any] | None:
        """Parse simple key:value text blocks when model wraps JSON in prose."""

        matches = re.findall(r"['\"]?([a-zA-Z_][a-zA-Z0-9_]*)['\"]?\s*:\s*([-+]?[0-9]*\.?[0-9]+|true|false)", raw)
        if not matches:
            return None

        params: dict[str, Any] = {}
        for key, value in matches:
            lower = value.lower()
            if lower == "true":
                parsed: Any = True
            elif lower == "false":
                parsed = False
            elif "." in value:
                parsed = float(value)
            else:
                parsed = int(value)
            params[key] = parsed

        if not params:
            return None
        return {"suggested_params": params}

    def generate_json(self, prompt: str) -> dict[str, Any]:
        raw = self.generate(prompt)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Hermes sometimes wraps valid JSON in prose; extract the first JSON object.
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = raw[start : end + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
            relaxed = self._parse_relaxed_suggested_params(raw)
            if relaxed is not None:
                return relaxed
            raise ValueError(f"Hermes did not return valid JSON: {raw}") from exc
