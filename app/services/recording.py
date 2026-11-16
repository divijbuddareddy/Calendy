"""Recording-provider adapters for the notetaker bot (PRD 16.2).

Interface (`RecordingAdapter`):
  create_bot(meeting_url, bot_name, metadata) -> BotState   # bot joins as a visible participant
  get_bot(bot_id) -> BotState                                 # normalised status
  start_recording(bot_id) / stop_recording(bot_id)
  remove_bot(bot_id)                                          # leave the call
  fetch_transcript(bot_id) -> Optional[str]                   # "Speaker: text" lines

Adapters:
  * RecallAdapter - Recall.ai-style REST API (https://docs.recall.ai). Used when RECALL_API_KEY is
    set; base URL from RECALL_API_BASE (default https://us-east-1.recall.ai/api/v1). The HTTP client
    is injectable so tests never hit the network.
  * MockRecordingAdapter - offline simulation (the previous behaviour): the bot "joins" immediately,
    recording is toggled locally and the transcript is pasted/uploaded by the host.

Normalised statuses: joining | in_call | recording | stopped | done | removed | failed
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from app.security import opaque_token

log = logging.getLogger("recording")


@dataclass
class BotState:
    bot_id: str
    status: str
    detail: str = ""
    raw: dict = field(default_factory=dict)


class RecordingError(Exception):
    pass


class RecordingAdapter:
    name = "base"

    def create_bot(self, meeting_url: Optional[str], bot_name: str, metadata: Optional[dict] = None) -> BotState:
        raise NotImplementedError

    def get_bot(self, bot_id: str) -> BotState:
        raise NotImplementedError

    def start_recording(self, bot_id: str) -> None:
        raise NotImplementedError

    def stop_recording(self, bot_id: str) -> None:
        raise NotImplementedError

    def remove_bot(self, bot_id: str) -> None:
        raise NotImplementedError

    def fetch_transcript(self, bot_id: str) -> Optional[str]:
        raise NotImplementedError


class MockRecordingAdapter(RecordingAdapter):
    """Simulated bot. State is owned by the NotetakerBot row, so get_bot only echoes it."""

    name = "mock"

    def create_bot(self, meeting_url, bot_name, metadata=None) -> BotState:
        return BotState(bot_id="mockbot_" + opaque_token(10), status="in_call",
                        detail=f"{bot_name} joined (simulated)")

    def get_bot(self, bot_id: str) -> BotState:
        return BotState(bot_id=bot_id, status="unknown")

    def start_recording(self, bot_id: str) -> None:
        return None

    def stop_recording(self, bot_id: str) -> None:
        return None

    def remove_bot(self, bot_id: str) -> None:
        return None

    def fetch_transcript(self, bot_id: str) -> Optional[str]:
        return None  # host pastes / uploads the transcript


# Recall.ai bot status codes -> normalised status
RECALL_STATUS = {
    "ready": "joining",
    "joining_call": "joining",
    "in_waiting_room": "joining",
    "in_call_not_recording": "in_call",
    "recording_permission_allowed": "in_call",
    "recording_permission_denied": "in_call",
    "in_call_recording": "recording",
    "call_ended": "stopped",
    "recording_done": "stopped",
    "done": "done",
    "analysis_done": "done",
    "fatal": "failed",
}


class RecallAdapter(RecordingAdapter):
    name = "recall"

    def __init__(self, api_key: str, base_url: Optional[str] = None, client=None, timeout: float = 15.0):
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("RECALL_API_BASE") or "https://us-east-1.recall.ai/api/v1").rstrip("/")
        self._client = client
        self.timeout = timeout

    @property
    def client(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _req(self, method: str, path: str, json: Optional[dict] = None):
        r = self.client.request(method, f"{self.base_url}{path}", json=json,
                                headers={"Authorization": f"Token {self.api_key}", "Accept": "application/json"})
        if r.status_code >= 400:
            raise RecordingError(f"Recall {method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return {}

    @staticmethod
    def _state(data: dict) -> BotState:
        changes = data.get("status_changes") or []
        code = (changes[-1].get("code") if changes else None) or (data.get("status") or {}).get("code") or "ready"
        sub = (changes[-1].get("sub_code") if changes else None) or ""
        return BotState(bot_id=str(data.get("id")), status=RECALL_STATUS.get(code, "joining"),
                        detail=code + (f" ({sub})" if sub else ""), raw=data)

    def create_bot(self, meeting_url, bot_name, metadata=None) -> BotState:
        if not meeting_url:
            raise RecordingError("A meeting URL (Zoom, Google Meet or Microsoft Teams) is required for the bot to join.")
        body = {
            "meeting_url": meeting_url,
            "bot_name": bot_name,
            "metadata": metadata or {},
            # Captions-based transcript; the bot shows a recording banner + chat notice in the call.
            "recording_config": {"transcript": {"provider": {"meeting_captions": {}}}},
            "chat": {"on_bot_join": {"send_to": "everyone",
                                     "message": f"{bot_name} is recording and transcribing this meeting."}},
        }
        return self._state(self._req("POST", "/bot/", body))

    def get_bot(self, bot_id: str) -> BotState:
        return self._state(self._req("GET", f"/bot/{bot_id}/"))

    def start_recording(self, bot_id: str) -> None:
        self._req("POST", f"/bot/{bot_id}/start_recording/", {})

    def stop_recording(self, bot_id: str) -> None:
        self._req("POST", f"/bot/{bot_id}/stop_recording/", {})

    def remove_bot(self, bot_id: str) -> None:
        self._req("POST", f"/bot/{bot_id}/leave_call/", {})

    def fetch_transcript(self, bot_id: str) -> Optional[str]:
        data = self._req("GET", f"/bot/{bot_id}/transcript/")
        segments = data if isinstance(data, list) else (data.get("results") or data.get("transcript") or [])
        lines = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            speaker = seg.get("speaker") or (seg.get("participant") or {}).get("name") or "Speaker"
            words = seg.get("words") or []
            text = " ".join(str(w.get("text", "")).strip() for w in words if isinstance(w, dict)).strip()
            text = text or str(seg.get("text") or "").strip()
            if text:
                lines.append(f"{speaker}: {text}")
        return "\n".join(lines) or None


_override: Optional[RecordingAdapter] = None


def set_adapter(adapter: Optional[RecordingAdapter]) -> None:
    """Test hook: force a specific adapter (None restores env-based selection)."""
    global _override
    _override = adapter


def get_adapter(provider: Optional[str] = None) -> RecordingAdapter:
    if _override is not None:
        return _override
    key = os.environ.get("RECALL_API_KEY", "")
    if provider == "mock":
        return MockRecordingAdapter()
    if key and provider in (None, "recall"):
        return RecallAdapter(key)
    return MockRecordingAdapter()
