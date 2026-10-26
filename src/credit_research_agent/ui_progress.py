"""Streamlit/CLI 共享的轻量进度事件。

这里只保存展示运行状态所需的最小信息。API 密钥、模型上下文、网页正文和
Function Tool 参数均不写入进度文件，完整审计信息仍由既有执行轨迹负责。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Optional


SAFE_DETAIL_KEYS = {
    "tool_name",
    "llm_call_number",
    "total_tokens",
    "planned_skills",
    "status",
    "delivered_topics",
    "missing_topics",
    "error_code",
}


class ProgressJsonlWriter:
    """把安全的运行进度逐行追加到 JSONL，供本地 UI 轮询。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._sequence = 0

    def write(
        self,
        stage: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._sequence += 1
            safe_details = {
                str(key): value
                for key, value in dict(details or {}).items()
                if str(key) in SAFE_DETAIL_KEYS
            }
            payload = {
                "sequence": self._sequence,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "stage": str(stage),
                "message": str(message),
                "details": safe_details,
            }
            line = json.dumps(payload, ensure_ascii=False, default=str)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()

    def __call__(self, event: Any) -> None:
        """兼容 RetrievalRunHooks 的 RetrievalEvent 回调接口。"""

        stage = getattr(event, "stage", "runtime_event")
        stage_value = getattr(stage, "value", stage)
        self.write(
            str(stage_value),
            str(getattr(event, "message", "运行状态更新")),
            getattr(event, "details", None),
        )


def read_progress_events(path: Path) -> list[dict[str, Any]]:
    """容错读取进度文件；忽略进程中断留下的半行或无效行。"""

    path = Path(path)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("stage") and payload.get("message"):
            events.append(payload)
    return events
