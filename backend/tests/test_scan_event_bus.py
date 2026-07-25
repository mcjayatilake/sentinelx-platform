"""`InMemoryEventBus` — fan-out pub/sub, isolation between subscribers."""

import uuid

from app.scan_engine.events import InMemoryEventBus, ScanCompleted, ScanQueued


def _event() -> ScanQueued:
    return ScanQueued(scan_id=uuid.uuid4(), tenant_id=uuid.uuid4(), correlation_id="corr-1")


async def test_publish_with_no_subscribers_does_not_raise() -> None:
    bus = InMemoryEventBus()
    await bus.publish(_event())


async def test_all_subscribers_for_an_event_type_are_called() -> None:
    bus = InMemoryEventBus()
    received: list[str] = []

    async def handler_a(event: ScanQueued) -> None:
        received.append("a")

    async def handler_b(event: ScanQueued) -> None:
        received.append("b")

    bus.subscribe(ScanQueued, handler_a)
    bus.subscribe(ScanQueued, handler_b)
    await bus.publish(_event())

    assert sorted(received) == ["a", "b"]


async def test_subscribers_are_scoped_to_their_event_type() -> None:
    bus = InMemoryEventBus()
    queued_calls: list[ScanQueued] = []
    completed_calls: list[ScanCompleted] = []

    async def on_queued(event: ScanQueued) -> None:
        queued_calls.append(event)

    async def on_completed(event: ScanCompleted) -> None:
        completed_calls.append(event)

    bus.subscribe(ScanQueued, on_queued)
    bus.subscribe(ScanCompleted, on_completed)
    await bus.publish(_event())

    assert len(queued_calls) == 1
    assert len(completed_calls) == 0


async def test_one_failing_handler_does_not_prevent_others_from_running() -> None:
    bus = InMemoryEventBus()
    received: list[str] = []

    async def failing_handler(event: ScanQueued) -> None:
        raise RuntimeError("boom")

    async def healthy_handler(event: ScanQueued) -> None:
        received.append("healthy")

    bus.subscribe(ScanQueued, failing_handler)
    bus.subscribe(ScanQueued, healthy_handler)
    await bus.publish(_event())  # must not raise

    assert received == ["healthy"]
