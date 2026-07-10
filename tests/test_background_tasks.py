"""Tests for entity-lifecycle tracking of fire-and-forget background tasks."""

from __future__ import annotations

import asyncio

import pytest

from homeassistant.core import HomeAssistant


async def test_background_task_auto_discarded_on_completion(
    hass: HomeAssistant, make_entity
) -> None:
    """A tracked task removes itself from the set once it finishes."""
    entity = make_entity()

    async def _work() -> None:
        return None

    entity._track_background_task(_work())
    assert len(entity._background_tasks) == 1

    task = next(iter(entity._background_tasks))
    await task
    await asyncio.sleep(0)  # let the done-callback run

    assert entity._background_tasks == set()


async def test_background_tasks_cancelled_on_removal(
    hass: HomeAssistant, make_entity
) -> None:
    """Pending background tasks are cancelled when the entity is removed.

    Prevents a superseded entity (e.g. after a config-entry reload) from
    living on inside an in-flight coroutine and issuing stale service calls.
    """
    entity = make_entity()
    started = asyncio.Event()

    async def _work() -> None:
        started.set()
        await asyncio.sleep(3600)

    entity._track_background_task(_work())
    await started.wait()
    task = next(iter(entity._background_tasks))

    await entity.async_will_remove_from_hass()

    assert entity._background_tasks == set()
    with pytest.raises(asyncio.CancelledError):
        await task
