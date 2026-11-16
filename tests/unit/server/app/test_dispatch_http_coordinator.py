"""C1: HTTP routes share one coordinator and refuse while that drive is live."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.server.app.background_starts import _start_tasks
from src.server.app.dispatch import router
from src.server.services.dispatch import DispatchCoordinator, DispatchSettings, bind_process_coordinator
from src.server.utils.api import get_current_user_id
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)


def test_c1_http_routes_share_the_coordinator_and_one_write_wins(tmp_path) -> None:
    store = InMemoryDispatchStore()
    row = Dispatch(
        id="http-1",
        goal="ship",
        user_id="test-user-123",
        created_at="now",
        state=DispatchState.REVIEW,
        lines=[
            Line(
                id="line-0",
                name="n",
                task="t",
                assigned_model="kimi",
                status=LineStatus.BLOCKED,
            )
        ],
    )
    entered, release = threading.Event(), threading.Event()
    entered2, release2 = threading.Event(), threading.Event()
    # The third drive (started by the winning parked reassign) is held, so
    # the losing request always meets a live drive and gets drive_active.
    entered3, release3 = threading.Event(), threading.Event()
    runs = {"n": 0}

    class Machine:
        async def run(self, dispatch: Dispatch) -> Dispatch:
            runs["n"] += 1
            if runs["n"] == 1:
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.01)
                return dispatch
            if runs["n"] == 2:
                entered2.set()
                while not release2.is_set():
                    await asyncio.sleep(0.01)
                return dispatch
            entered3.set()
            while not release3.is_set():
                await asyncio.sleep(0.01)
            return dispatch

    coordinator = DispatchCoordinator(
        store=store,
        settings=DispatchSettings(proposed_dir=tmp_path / "proposed"),
        gate=FileApprovalGate(tmp_path / "proposed"),
        machine_factory=lambda _row: Machine(),
    )
    left = coordinator.service()
    right = coordinator.service()
    assert left is not right
    assert left.active_drives is right.active_drives
    bind_process_coordinator(coordinator)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.create(row)
        task = asyncio.create_task(coordinator.service()._drive(row.id))
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("drive did not start")
        try:
            yield
        finally:
            release.set()
            release2.set()
            release3.set()
            await asyncio.wait_for(task, timeout=2)
            extra = _start_tasks.get(f"dispatch-{row.id}")
            if extra is not None:
                await asyncio.wait_for(extra, timeout=2)

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.dependency_overrides[get_current_user_id] = lambda: "test-user-123"
    reassign = "/api/v1/dispatch/http-1/lines/line-0/reassign"
    try:
        with TestClient(app) as client:
            def post(path: str, payload: dict[str, str]):
                return client.post(path, json=payload)

            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = [
                    item.result()
                    for item in (
                        pool.submit(post, reassign, {"model": "stepfun"}),
                        pool.submit(post, reassign, {"model": "stepfun"}),
                    )
                ]
            # N25b: a BLOCKED line's reassign during the drive is queued. One
            # request wins; the other is told a request is already queued.
            pair = sorted((first, second), key=lambda item: item.status_code)
            assert [item.status_code for item in pair] == [200, 409]
            assert pair[1].json()["detail"] == "reassign_pending"
            queued = pair[0].json()["human_reassigns"]["queued"]
            assert [(item["line_id"], item["model"]) for item in queued] == [("line-0", "stepfun")]
            approved = client.post("/api/v1/dispatch/http-1/approve", json={"note": "n"})
            rejected = client.post("/api/v1/dispatch/http-1/reject", json={"note": "n"})
            assert approved.status_code == 409
            assert rejected.status_code == 409
            assert approved.json()["detail"] == "drive_active"
            assert rejected.json()["detail"] == "drive_active"
            # Queued only: the drive's line is untouched and nothing new is scheduled.
            held = store.rows["http-1"].line("line-0")
            assert held is not None
            assert held.assigned_model == "kimi"
            assert held.attempts == 0
            assert f"dispatch-{row.id}" not in _start_tasks
            assert runs["n"] == 1

            # The drive ends; it applies the queued request and drives again.
            release.set()
            assert entered2.wait(2)
            assert runs["n"] == 2
            applied = store.rows["http-1"].line("line-0")
            assert applied is not None
            assert applied.assigned_model == "stepfun"
            assert applied.attempts == 1
            # The re-driven line is ASSIGNED, so it belongs to the drive now.
            busy = client.post(reassign, json={"model": "glm"})
            assert busy.status_code == 409
            assert busy.json()["detail"] == "drive_active"
            release2.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and row.id in coordinator.active_drives:
                time.sleep(0.01)
            assert row.id not in coordinator.active_drives

            # No drive: one write wins, the other loses to the drive it started.
            with ThreadPoolExecutor(max_workers=2) as pool:
                later = [item.result() for item in (
                    pool.submit(post, reassign, {"model": "glm"}),
                    pool.submit(post, reassign, {"model": "glm"}),
                )]
            codes = sorted(item.status_code for item in later)
            assert codes == [200, 409]
            assert any(item.json()["detail"] == "drive_active" for item in later if item.status_code == 409)
            winner = store.rows["http-1"].line("line-0")
            assert winner is not None
            assert winner.assigned_model == "glm"
            assert winner.attempts == 2
            assert entered3.wait(2)
            release3.set()
    finally:
        bind_process_coordinator(None)
        release.set()
        release2.set()
        release3.set()
