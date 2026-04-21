"""In-memory metrics registry with Prometheus exposition output."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, defaultdict, deque
from typing import Deque

from config.settings import settings

logger = logging.getLogger(__name__)


class MetricsRegistry:
    """Track bounded-cardinality counters and emit simple alerts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Reset all in-memory counters."""
        with self._lock:
            self.api_requests_total: Counter[tuple[str, str, str]] = Counter()
            self.api_request_duration_count: Counter[tuple[str, str]] = Counter()
            self.api_request_duration_sum: Counter[tuple[str, str]] = Counter()
            self.api_auth_failures_total = 0
            self.api_rate_limit_rejections_total = 0
            self.worker_tasks_total: Counter[tuple[str, str]] = Counter()
            self.job_enqueues_total: Counter[str] = Counter()
            self._window_events: dict[str, Deque[float]] = defaultdict(deque)
            self._last_alert_at: dict[str, float] = {}

    def record_api_request(self, method: str, path: str, status_code: int, duration_seconds: float) -> None:
        """Record API request traffic and latency."""
        bounded_path = path or "unknown"
        bounded_method = method.upper()
        bounded_status = str(status_code)

        with self._lock:
            self.api_requests_total[(bounded_method, bounded_path, bounded_status)] += 1
            self.api_request_duration_count[(bounded_method, bounded_path)] += 1
            self.api_request_duration_sum[(bounded_method, bounded_path)] += duration_seconds

    def record_auth_failure(self, *, path: str, client_id: str) -> None:
        """Record an authentication failure and evaluate alerts."""
        with self._lock:
            self.api_auth_failures_total += 1
        self._record_window_event(
            "auth_failures",
            threshold=settings.metrics_alert_auth_failures_threshold,
            payload={"path": path, "client_id": client_id},
        )

    def record_rate_limit_rejection(self, *, path: str, client_id: str) -> None:
        """Record a rate-limit rejection and evaluate alerts."""
        with self._lock:
            self.api_rate_limit_rejections_total += 1
        self._record_window_event(
            "rate_limit_rejections",
            threshold=settings.metrics_alert_rate_limit_threshold,
            payload={"path": path, "client_id": client_id},
        )

    def record_job_enqueued(self, task_type: str) -> None:
        """Record a queued background task."""
        with self._lock:
            self.job_enqueues_total[task_type] += 1

    def record_worker_task_result(self, *, task_type: str, result: str, job_id: str | None = None) -> None:
        """Record the outcome of a worker-executed task."""
        with self._lock:
            self.worker_tasks_total[(task_type, result)] += 1

        if result == "failed":
            self._record_window_event(
                "job_failures",
                threshold=settings.metrics_alert_job_failures_threshold,
                payload={"task_type": task_type, "job_id": job_id or ""},
            )

    def record_stale_workers(self, stale_count: int) -> None:
        """Emit an alert when stale worker heartbeats are detected."""
        if stale_count <= 0:
            return
        self._record_window_event(
            "stale_workers",
            threshold=1,
            payload={"stale_workers": stale_count},
        )

    def _record_window_event(self, name: str, *, threshold: int, payload: dict[str, object]) -> None:
        """Track a threshold-based alert window and log structured warnings."""
        now = time.time()
        window_seconds = max(1, settings.metrics_alert_window_seconds)
        cooldown_seconds = max(1, settings.metrics_alert_cooldown_seconds)

        with self._lock:
            bucket = self._window_events[name]
            cutoff = now - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            bucket.append(now)

            if len(bucket) < max(1, threshold):
                return

            last_emitted = self._last_alert_at.get(name, 0.0)
            if now - last_emitted < cooldown_seconds:
                return

            self._last_alert_at[name] = now

        logger.warning(
            json.dumps(
                {
                    "event": "metric_alert",
                    "name": name,
                    "count": len(bucket),
                    "window_seconds": window_seconds,
                    **payload,
                },
                sort_keys=True,
            )
        )

    def render_prometheus(self, queue_snapshot: dict[str, int] | None = None) -> str:
        """Render Prometheus text exposition output."""
        queue_snapshot = queue_snapshot or {}

        with self._lock:
            request_lines = [
                '# HELP hotel_kb_api_requests_total Total API requests grouped by method, path, and status.',
                "# TYPE hotel_kb_api_requests_total counter",
            ]
            for (method, path, status), count in sorted(self.api_requests_total.items()):
                request_lines.append(
                    f'hotel_kb_api_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
                )

            duration_lines = [
                "# HELP hotel_kb_api_request_duration_seconds Total observed API request duration in seconds.",
                "# TYPE hotel_kb_api_request_duration_seconds summary",
            ]
            duration_keys = sorted(self.api_request_duration_count)
            for method, path in duration_keys:
                count = self.api_request_duration_count[(method, path)]
                duration_sum = self.api_request_duration_sum[(method, path)]
                duration_lines.append(
                    f'hotel_kb_api_request_duration_seconds_count{{method="{method}",path="{path}"}} {count}'
                )
                duration_lines.append(
                    f'hotel_kb_api_request_duration_seconds_sum{{method="{method}",path="{path}"}} {duration_sum:.6f}'
                )

            auth_lines = [
                "# HELP hotel_kb_api_auth_failures_total Total rejected requests due to missing or invalid auth.",
                "# TYPE hotel_kb_api_auth_failures_total counter",
                f"hotel_kb_api_auth_failures_total {self.api_auth_failures_total}",
                "# HELP hotel_kb_api_rate_limit_rejections_total Total rejected requests due to API rate limiting.",
                "# TYPE hotel_kb_api_rate_limit_rejections_total counter",
                f"hotel_kb_api_rate_limit_rejections_total {self.api_rate_limit_rejections_total}",
            ]

            worker_lines = [
                "# HELP hotel_kb_worker_tasks_total Worker task outcomes grouped by task type and result.",
                "# TYPE hotel_kb_worker_tasks_total counter",
            ]
            for (task_type, result), count in sorted(self.worker_tasks_total.items()):
                worker_lines.append(
                    f'hotel_kb_worker_tasks_total{{task_type="{task_type}",result="{result}"}} {count}'
                )

            enqueue_lines = [
                "# HELP hotel_kb_job_enqueues_total Background job enqueues grouped by task type.",
                "# TYPE hotel_kb_job_enqueues_total counter",
            ]
            for task_type, count in sorted(self.job_enqueues_total.items()):
                enqueue_lines.append(f'hotel_kb_job_enqueues_total{{task_type="{task_type}"}} {count}')

        queue_lines = [
            "# HELP hotel_kb_queue_jobs Current queued background jobs grouped by queue state.",
            "# TYPE hotel_kb_queue_jobs gauge",
        ]
        for state_name in ("idle", "queued", "running", "retry_wait", "stale_running"):
            queue_lines.append(
                f'hotel_kb_queue_jobs{{state="{state_name}"}} {queue_snapshot.get(state_name, 0)}'
            )

        lines = request_lines + duration_lines + auth_lines + worker_lines + enqueue_lines + queue_lines
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
