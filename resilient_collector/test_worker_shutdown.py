"""Focused unit tests for ShutdownController, heartbeat, interruptible_sleep, and locking."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from contextlib import suppress
from unittest.mock import MagicMock, patch

from worker_shutdown import (
    ShutdownController,
    WorkerProgress,
    acquire_instance_lock,
    heartbeat_loop,
    interruptible_sleep,
    release_instance_lock,
)


class InterruptibleSleepTests(unittest.IsolatedAsyncioTestCase):
    async def test_interruptible_sleep_times_out_normally(self) -> None:
        shutdown = ShutdownController()
        start = time.monotonic()
        result = await interruptible_sleep(0.05, shutdown, label="test_timeout")
        elapsed = time.monotonic() - start
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 0.04)

    async def test_interruptible_sleep_interrupted_by_shutdown(self) -> None:
        shutdown = ShutdownController()

        async def _trigger_shutdown():
            await asyncio.sleep(0.02)
            shutdown.request_shutdown("operator_abort")

        trigger_task = asyncio.create_task(_trigger_shutdown())
        start = time.monotonic()
        result = await interruptible_sleep(2.0, shutdown, label="test_interrupt")
        elapsed = time.monotonic() - start
        await trigger_task

        self.assertFalse(result)
        self.assertTrue(shutdown.is_requested())
        self.assertEqual(shutdown.reason, "operator_abort")
        self.assertLess(elapsed, 0.5)

    async def test_interruptible_sleep_already_requested(self) -> None:
        shutdown = ShutdownController()
        shutdown.request_shutdown("pre_stopped")
        result = await interruptible_sleep(1.0, shutdown)
        self.assertFalse(result)

    async def test_interruptible_sleep_none_controller(self) -> None:
        start = time.monotonic()
        result = await interruptible_sleep(0.03, None)
        elapsed = time.monotonic() - start
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 0.025)


class HeartbeatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_logging_after_timeout(self) -> None:
        logger = MagicMock()
        shutdown = ShutdownController()
        shutdown.progress.jobs_done = 5
        shutdown.progress.total_jobs = 10

        with patch.dict(os.environ, {"RESILIENT_HEARTBEAT_SECONDS": "0.03"}):
            task = asyncio.create_task(heartbeat_loop(logger, shutdown))
            # Wait for at least one heartbeat tick
            await asyncio.sleep(0.08)
            shutdown.request_shutdown("done_heartbeat_test")
            await asyncio.wait_for(task, timeout=0.5)

        logger.info.assert_called()
        logged_msgs = [call.args[0] for call in logger.info.call_args_list]
        self.assertTrue(any("RUN_HEARTBEAT" in msg for msg in logged_msgs))

    async def test_heartbeat_exit_after_shutdown(self) -> None:
        logger = MagicMock()
        shutdown = ShutdownController()

        with patch.dict(os.environ, {"RESILIENT_HEARTBEAT_SECONDS": "10"}):
            task = asyncio.create_task(heartbeat_loop(logger, shutdown))
            await asyncio.sleep(0.01)
            shutdown.request_shutdown("signal:SIGTERM")
            # Should exit immediately without waiting 10s
            await asyncio.wait_for(task, timeout=0.5)

        self.assertTrue(task.done())

    async def test_heartbeat_graceful_cancellation(self) -> None:
        logger = MagicMock()
        shutdown = ShutdownController()

        with patch.dict(os.environ, {"RESILIENT_HEARTBEAT_SECONDS": "10"}):
            task = asyncio.create_task(heartbeat_loop(logger, shutdown))
            await asyncio.sleep(0.01)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        self.assertTrue(task.cancelled())


class ControllerLoopSafetyTests(unittest.TestCase):
    def test_worker_running_under_asyncio_run_without_cross_loop_error(self) -> None:
        """Simulate creating controller before asyncio.run() and accessing event."""
        # 1. Instantiate controller before any event loop starts
        shutdown = ShutdownController()

        # 2. Even touch shutdown.event in synchronous code (binds to default/dummy in 3.9)
        _ = shutdown.event

        # 3. Define worker coroutine that awaits interruptible_sleep and checks heartbeat
        async def mock_worker(s: ShutdownController) -> str:
            # Inside asyncio.run(), s.event must be bound to this running loop
            slept = await interruptible_sleep(0.02, s)
            return f"success:{slept}"

        # 4. Run under asyncio.run()
        result = asyncio.run(mock_worker(shutdown))
        self.assertEqual(result, "success:True")

    def test_controller_lifecycle_repeated_worker_runs(self) -> None:
        """Verify the controller can be reused across multiple sequential asyncio.run() calls."""
        shutdown = ShutdownController()

        async def run1(s: ShutdownController) -> None:
            self.assertTrue(await interruptible_sleep(0.02, s))

        async def run2(s: ShutdownController) -> None:
            self.assertTrue(await interruptible_sleep(0.02, s))

        # First run creates and closes Loop 1
        asyncio.run(run1(shutdown))

        # Second run creates Loop 2 - should not raise "attached to a different loop"
        asyncio.run(run2(shutdown))

        # Third run with shutdown interruption across runs
        shutdown.reset()

        async def run3(s: ShutdownController) -> None:
            task = asyncio.create_task(interruptible_sleep(1.0, s))
            await asyncio.sleep(0.01)
            s.request_shutdown("repeat_stop")
            res = await task
            self.assertFalse(res)

        asyncio.run(run3(shutdown))
        self.assertTrue(shutdown.is_requested())
        self.assertEqual(shutdown.reason, "repeat_stop")

    def test_threadsafe_shutdown_request_while_loop_running(self) -> None:
        """Verify request_shutdown from a separate thread calls call_soon_threadsafe."""
        shutdown = ShutdownController()

        async def runner(s: ShutdownController) -> bool:
            import threading

            def _bg():
                time.sleep(0.03)
                s.request_shutdown("thread_signal")

            t = threading.Thread(target=_bg)
            t.start()
            res = await interruptible_sleep(1.0, s)
            t.join()
            return res

        result = asyncio.run(runner(shutdown))
        self.assertFalse(result)
        self.assertEqual(shutdown.reason, "thread_signal")


class LinuxFcntlLockTests(unittest.TestCase):
    def test_acquire_and_release_instance_lock_with_fcntl(self) -> None:
        """Verify Linux fcntl locking logic and error handling."""
        mock_fcntl = MagicMock()
        mock_fcntl.LOCK_EX = 2
        mock_fcntl.LOCK_NB = 4
        mock_fcntl.LOCK_UN = 8

        with patch("worker_shutdown.fcntl", mock_fcntl):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name

            try:
                fd = acquire_instance_lock(tmp_path)
                self.assertIsNotNone(fd)
                mock_fcntl.flock.assert_called_with(fd, mock_fcntl.LOCK_EX | mock_fcntl.LOCK_NB)

                release_instance_lock(fd)
                mock_fcntl.flock.assert_called_with(fd, mock_fcntl.LOCK_UN)
            finally:
                with suppress(OSError):
                    os.unlink(tmp_path)

    def test_acquire_instance_lock_busy_returns_none(self) -> None:
        """Verify BlockingIOError from fcntl returns None."""
        mock_fcntl = MagicMock()
        mock_fcntl.LOCK_EX = 2
        mock_fcntl.LOCK_NB = 4
        mock_fcntl.flock.side_effect = BlockingIOError("Resource temporarily unavailable")

        with patch("worker_shutdown.fcntl", mock_fcntl):
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name

            try:
                fd = acquire_instance_lock(tmp_path)
                self.assertIsNone(fd)
            finally:
                with suppress(OSError):
                    os.unlink(tmp_path)


class LoggingAndProcessAliveTests(unittest.TestCase):
    def test_setup_logging_streams_to_stdout_by_default(self) -> None:
        from logging_setup import setup_logging
        import logging

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESILIENT_BACKGROUND", None)
            logger, log_path = setup_logging()
            handlers = logging.getLogger().handlers
            has_stream = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in handlers)
            self.assertTrue(has_stream)

    def test_setup_logging_disables_stdout_in_background(self) -> None:
        from logging_setup import setup_logging
        import logging

        with patch.dict(os.environ, {"RESILIENT_BACKGROUND": "1"}):
            logger, log_path = setup_logging()
            handlers = logging.getLogger().handlers
            has_stream = any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in handlers)
            self.assertFalse(has_stream)

    def test_is_process_alive_cmdline_filtering(self) -> None:
        from worker_shutdown import is_process_alive

        with patch("os.kill", return_value=None), patch("os.path.exists", return_value=True):
            with patch("builtins.open", unittest.mock.mock_open(read_data=b"/usr/bin/nginx")):
                self.assertFalse(is_process_alive(1234))

            with patch("builtins.open", unittest.mock.mock_open(read_data=b"python run_resilient_sheet.py")):
                self.assertTrue(is_process_alive(1234))


if __name__ == "__main__":
    unittest.main()
