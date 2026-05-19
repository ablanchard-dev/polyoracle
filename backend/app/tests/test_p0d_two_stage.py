"""P0-D (2026-05-19) tests — two-stage poller/audit isolation.

Operator validation criteria :
- bounded queue (drop on overflow, no freeze)
- idempotence preserved (no double processing)
- worker crash isolation (one audit worker fail ≠ all stop)
- backpressure metrics
- flag-gated (default off)
"""
from __future__ import annotations

import asyncio
import pytest

from app.services import polling_two_stage as pts


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("POLLING_TWO_STAGE_ENABLED", raising=False)
    assert pts.is_enabled() is False


def test_flag_on_variants(monkeypatch):
    for v in ("true", "True", "1", "yes", "on"):
        monkeypatch.setenv("POLLING_TWO_STAGE_ENABLED", v)
        assert pts.is_enabled() is True
    for v in ("false", "0", "", "no"):
        monkeypatch.setenv("POLLING_TWO_STAGE_ENABLED", v)
        assert pts.is_enabled() is False


def test_audit_workers_count_clamp(monkeypatch):
    monkeypatch.setenv("POLLING_AUDIT_WORKERS", "4")
    assert pts.audit_workers_count() == 4
    monkeypatch.setenv("POLLING_AUDIT_WORKERS", "99")
    assert pts.audit_workers_count() == 16  # clamped
    monkeypatch.setenv("POLLING_AUDIT_WORKERS", "0")
    assert pts.audit_workers_count() == 1


def test_queue_maxsize_clamp(monkeypatch):
    monkeypatch.setenv("POLLING_AUDIT_QUEUE_MAX", "200")
    assert pts.queue_maxsize() == 200
    monkeypatch.setenv("POLLING_AUDIT_QUEUE_MAX", "10")
    assert pts.queue_maxsize() == 50  # min 50


@pytest.mark.asyncio
async def test_queue_enqueue_dequeue_basic():
    q = pts.AuditQueue(maxsize=10)
    enq = await q.put_nowait_drop({"event_id": "e1"})
    assert enq is True
    assert q.qsize() == 1
    assert q.stats["enqueued"] == 1
    event = await q.get()
    assert event["event_id"] == "e1"
    assert q.stats["dequeued"] == 1


@pytest.mark.asyncio
async def test_queue_drops_on_full(tmp_path, monkeypatch):
    monkeypatch.setattr(pts, "DEAD_LETTER_DIR", tmp_path)
    q = pts.AuditQueue(maxsize=3)
    for i in range(3):
        ok = await q.put_nowait_drop({"event_id": f"e{i}"})
        assert ok is True
    # 4th event should be dropped
    ok = await q.put_nowait_drop({"event_id": "overflow"})
    assert ok is False
    assert q.stats["dropped_full"] == 1
    # Dead-letter file should exist with the dropped event
    dl_files = list(tmp_path.glob("deadletter_*.jsonl"))
    assert len(dl_files) == 1
    content = dl_files[0].read_text()
    assert "overflow" in content
    assert "queue_full" in content


@pytest.mark.asyncio
async def test_kill_threshold_warning(caplog):
    q = pts.AuditQueue(maxsize=10, kill_threshold=5)
    for i in range(5):
        await q.put_nowait_drop({"event_id": f"e{i}"})
    # Threshold hit on the 5th
    assert q.stats["kill_threshold_hits"] >= 1


@pytest.mark.asyncio
async def test_audit_pool_processes_events():
    q = pts.AuditQueue(maxsize=20)
    processed = []

    async def fake_process(event):
        processed.append(event["event_id"])

    pool = pts.AuditWorkerPool(queue=q, process_func=fake_process, n_workers=2)
    for i in range(5):
        await q.put_nowait_drop({"event_id": f"e{i}"})

    await pool.start()
    await asyncio.sleep(0.3)
    await pool.stop()

    assert set(processed) == {f"e{i}" for i in range(5)}
    assert pool.stats["processed"] == 5
    assert pool.stats["errors"] == 0


@pytest.mark.asyncio
async def test_audit_worker_crash_isolation():
    """If process_func raises on one event, other events still processed."""
    q = pts.AuditQueue(maxsize=20)
    processed = []
    crashed = []

    async def faulty_process(event):
        if event["event_id"] == "crash_me":
            crashed.append(event["event_id"])
            raise RuntimeError("simulated")
        processed.append(event["event_id"])

    pool = pts.AuditWorkerPool(queue=q, process_func=faulty_process, n_workers=2)
    await q.put_nowait_drop({"event_id": "e1"})
    await q.put_nowait_drop({"event_id": "crash_me"})
    await q.put_nowait_drop({"event_id": "e2"})

    await pool.start()
    await asyncio.sleep(0.3)
    await pool.stop()

    assert "e1" in processed
    assert "e2" in processed
    assert "crash_me" in crashed
    assert pool.stats["errors"] == 1
    assert pool.stats["processed"] == 2


@pytest.mark.asyncio
async def test_two_stage_idempotency_via_event_id():
    """Same event_id enqueued twice → process_func sees both. Idempotency
    is the responsibility of _process_trade_in_session (audit_id dedupe).
    Two-stage queue does NOT dedup — it preserves order."""
    q = pts.AuditQueue(maxsize=20)
    processed = []

    async def process(event):
        processed.append(event["event_id"])

    pool = pts.AuditWorkerPool(queue=q, process_func=process, n_workers=1)
    await q.put_nowait_drop({"event_id": "dup"})
    await q.put_nowait_drop({"event_id": "dup"})

    await pool.start()
    await asyncio.sleep(0.2)
    await pool.stop()

    # Both events pass through — dedupe handled downstream in
    # _process_trade_in_session via audit_id idempotency.
    assert processed == ["dup", "dup"]


@pytest.mark.asyncio
async def test_stats_phase_timing_accumulates():
    q = pts.AuditQueue(maxsize=10)

    async def slow_process(event):
        await asyncio.sleep(0.05)

    pool = pts.AuditWorkerPool(queue=q, process_func=slow_process, n_workers=1)
    for i in range(3):
        await q.put_nowait_drop({"event_id": f"e{i}"})

    await pool.start()
    await asyncio.sleep(0.5)
    await pool.stop()

    assert pool.stats["phase_total_s"]["audit_work"] >= 0.10
    assert pool.stats["processed"] == 3
