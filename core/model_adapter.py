"""Responses API client shared by Chat and Main."""

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx


class ModelError(Exception):
    """A model request failed with a message safe to return to the caller."""


class ResponsesModel:
    def __init__(self, endpoint: str, model: str, api_key: str, *, timeout_seconds: float = 15):
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("PA_MODEL_BASE_URL must be a model API URL without credentials or query")
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("The model API must use HTTPS outside localhost")
        self.endpoint = endpoint.rstrip("/") + "/responses"
        self.model = model
        self.timeout_seconds = timeout_seconds
        headers = {"Accept": "text/event-stream"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.client = httpx.AsyncClient(headers=headers, follow_redirects=False, timeout=None)

    @classmethod
    def from_environment(cls):
        endpoint = os.environ.get("PA_MODEL_BASE_URL")
        model = os.environ.get("PA_MODEL_NAME")
        if not endpoint and not model:
            return None
        if not endpoint or not model:
            raise ValueError("PA_MODEL_BASE_URL and PA_MODEL_NAME must be configured together")
        return cls(endpoint, model, os.environ.get("PA_MODEL_API_KEY", ""))

    async def close(self) -> None:
        await self.client.aclose()

    async def ask(self, instructions: str, message: str) -> str:
        payload = {"model": self.model, "instructions": instructions, "input": message,
                   "stream": True, "store": False}
        chunks = []
        size = 0
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self.client.stream("POST", self.endpoint, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                            continue
                        event = json.loads(line[6:])
                        if event.get("type") == "response.output_text.delta":
                            delta = event["delta"]
                            if not isinstance(delta, str):
                                raise ModelError("Model provider returned an invalid event stream")
                            size += len(delta)
                            if size > 20000:
                                raise ModelError("Model response is too large")
                            chunks.append(delta)
                        elif event.get("type") == "response.completed":
                            text = "".join(chunks).strip()
                            if text:
                                return text
                            break
                        elif event.get("type") in ("response.failed", "response.incomplete", "error"):
                            break
        except TimeoutError:
            raise ModelError("Model request exceeded its total time limit") from None
        except httpx.HTTPStatusError as error:
            raise ModelError(f"Model provider returned HTTP {error.response.status_code}") from None
        except httpx.ProtocolError:
            raise ModelError("Model connection closed or returned an invalid HTTP response") from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError):
            raise ModelError("Model provider is unreachable or the connection failed") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ModelError("Model provider returned an invalid event stream") from None
        raise ModelError("Model provider did not return a complete answer")
