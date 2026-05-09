# -*- coding: utf-8 -*-
"""Tests for the build cancellation system.
Run with: python -m pytest tests/test_cancellation.py -v
"""
import asyncio
import queue
import threading
import time
import unittest

# ---------------------------------------------------------------------------
# cancel_manager tests — pure Python, no Revit required
# ---------------------------------------------------------------------------

from revit_mcp.cancel_manager import (
    request_cancel, clear_cancel, is_cancelled, check_cancelled,
)


class TestCancelManager(unittest.TestCase):

    def setUp(self):
        clear_cancel()

    def test_initially_not_cancelled(self):
        self.assertFalse(is_cancelled())

    def test_request_sets_flag(self):
        request_cancel()
        self.assertTrue(is_cancelled())

    def test_clear_resets_flag(self):
        request_cancel()
        clear_cancel()
        self.assertFalse(is_cancelled())

    def test_check_cancelled_raises_when_set(self):
        request_cancel()
        with self.assertRaises(RuntimeError) as ctx:
            check_cancelled("test label")
        self.assertIn("cancelled", str(ctx.exception).lower())

    def test_check_cancelled_silent_when_clear(self):
        clear_cancel()
        check_cancelled("should not raise")  # must not raise

    def test_flag_is_thread_safe(self):
        """Set flag from a background thread; main thread must see it."""
        clear_cancel()
        t = threading.Thread(target=request_cancel)
        t.start()
        t.join()
        self.assertTrue(is_cancelled())


# ---------------------------------------------------------------------------
# Bridge run_on_main_thread — mocked without Revit
# ---------------------------------------------------------------------------

class TestBridgeCancelPolling(unittest.TestCase):
    """
    Verifies that run_on_main_thread() raises RuntimeError within ~1s when
    the cancel flag is set while it is blocked waiting for a slow main-thread job.
    """

    def setUp(self):
        clear_cancel()

    def _make_mock_bridge(self):
        """Return a run_on_main_thread-equivalent using a background thread
        (simulates Revit main thread) so we can test cancel detection."""
        work_q = queue.Queue()

        def fake_main_thread():
            while True:
                try:
                    item = work_q.get(timeout=0.1)
                    if item is None:
                        break
                    func, args, kwargs, evt, result_w, _ = item
                    # Check cancel before executing (mirrors pump_commands fix)
                    if is_cancelled():
                        result_w['error'] = RuntimeError("Build cancelled by user.")
                        evt.set()
                        work_q.task_done()
                        continue
                    try:
                        result_w['data'] = func(*args, **kwargs)
                    except Exception as e:
                        result_w['error'] = e
                    finally:
                        evt.set()
                        work_q.task_done()
                except queue.Empty:
                    continue

        worker = threading.Thread(target=fake_main_thread, daemon=True)
        worker.start()

        import datetime as _dt

        def run_on_main_thread(func, *args, **kwargs):
            if is_cancelled():
                raise RuntimeError("Build cancelled by user.")
            evt = threading.Event()
            result_w = {'data': None, 'error': None}
            queued_at = _dt.datetime.now()
            work_q.put((func, args, kwargs, evt, result_w, queued_at))
            deadline = queued_at + _dt.timedelta(seconds=10)
            while not evt.wait(0.5):
                if is_cancelled():
                    raise RuntimeError("Build cancelled by user.")
                if _dt.datetime.now() >= deadline:
                    raise TimeoutError("Fake main thread timed out.")
            if result_w['error']:
                raise result_w['error']
            return result_w['data']

        return run_on_main_thread, work_q, worker

    def test_cancel_before_submit_raises_immediately(self):
        run, work_q, worker = self._make_mock_bridge()
        request_cancel()
        with self.assertRaises(RuntimeError):
            run(lambda: "should not run")
        work_q.put(None)
        worker.join(timeout=2)

    def test_cancel_during_wait_raises_within_one_second(self):
        """Job takes 3s; cancel fires after 0.2s; worker should raise < 1s."""
        run, work_q, worker = self._make_mock_bridge()

        def slow_job():
            time.sleep(3)
            return "done"

        errors = []

        def call_run():
            try:
                run(slow_job)
            except RuntimeError as e:
                errors.append(e)

        t = threading.Thread(target=call_run)
        t.start()
        time.sleep(0.2)
        request_cancel()
        t.join(timeout=2.0)

        self.assertFalse(t.is_alive(), "run_on_main_thread did not unblock within 2s after cancel")
        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", str(errors[0]).lower())

        work_q.put(None)
        worker.join(timeout=2)

    def test_cancel_skips_queued_item(self):
        """If cancel is set before the main thread picks up an item, it is skipped."""
        run, work_q, worker = self._make_mock_bridge()
        executed = []

        def job():
            executed.append(True)
            return "ok"

        # Cancel BEFORE the fake main thread gets a chance to run the job.
        request_cancel()
        with self.assertRaises(RuntimeError):
            run(job)

        time.sleep(0.3)
        self.assertEqual(len(executed), 0, "Job should not have executed after cancel")

        work_q.put(None)
        worker.join(timeout=2)


# ---------------------------------------------------------------------------
# Level-loop cancel checks in simulated _process_walls / _process_floors
# ---------------------------------------------------------------------------

class TestLevelLoopCancellation(unittest.TestCase):
    """
    Simulates the per-level loops from revit_workers.py to confirm that
    check_cancelled() inside the loop body aborts after the right level.
    """

    def setUp(self):
        clear_cancel()

    def _simulate_level_loop(self, num_levels, cancel_after_level):
        """
        Mimic the structure of _process_walls() / _process_floors():
          for k, lvl in enumerate(current_levels):
              check_cancelled(...)
              <build one storey>
        Returns the count of levels actually built.
        """
        built = []

        def cancel_trigger():
            time.sleep(0.01 * cancel_after_level)
            request_cancel()

        # Fire cancellation asynchronously
        threading.Thread(target=cancel_trigger, daemon=True).start()

        try:
            for k in range(num_levels):
                time.sleep(0.01)        # simulate per-level Revit work
                check_cancelled("level {}".format(k + 1))
                built.append(k + 1)
        except RuntimeError:
            pass

        return built

    def test_stops_mid_build(self):
        """Cancellation after level 3 should build at most levels 1-3."""
        built = self._simulate_level_loop(num_levels=10, cancel_after_level=3)
        # We may build level 3 before the cancel fires (race), but never all 10
        self.assertLess(len(built), 10, "Build should have been interrupted")
        self.assertGreaterEqual(len(built), 0)

    def test_full_build_when_not_cancelled(self):
        """No cancel → all levels complete."""
        built = self._simulate_level_loop(num_levels=5, cancel_after_level=99)
        self.assertEqual(len(built), 5)

    def test_cancel_before_first_level_builds_nothing(self):
        request_cancel()
        built = []
        try:
            for k in range(10):
                check_cancelled("level {}".format(k + 1))
                built.append(k + 1)
        except RuntimeError:
            pass
        self.assertEqual(len(built), 0)


# ---------------------------------------------------------------------------
# Async cancellation chain  (orchestrate_build → request_cancel)
# ---------------------------------------------------------------------------

class TestAsyncCancellationChain(unittest.TestCase):

    def setUp(self):
        clear_cancel()

    def test_cancelled_error_triggers_request_cancel(self):
        """
        Mimics the orchestrate_build try/except:
            try:
                await asyncio.to_thread(long_running)
            except asyncio.CancelledError:
                cancel_manager.request_cancel()
                raise
        Verifies cancel flag is set after CancelledError.
        """

        async def fake_orchestrate():
            clear_cancel()
            try:
                await asyncio.sleep(10)     # simulates to_thread(run_full_stack)
            except asyncio.CancelledError:
                request_cancel()
                raise

        async def runner():
            task = asyncio.create_task(fake_orchestrate())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertTrue(is_cancelled(), "cancel flag must be set after CancelledError")

        asyncio.run(runner())

    def test_clear_cancel_resets_between_builds(self):
        """orchestrate_build calls clear_cancel() at start — simulate that."""
        request_cancel()
        self.assertTrue(is_cancelled())
        clear_cancel()
        self.assertFalse(is_cancelled())


# ---------------------------------------------------------------------------
# RAG cancellation
# ---------------------------------------------------------------------------

class TestRAGCancellation(unittest.TestCase):
    """
    Verifies that the RAG layer (vertex_rag._do_search and
    sub_agent.run_retrieve_rules) respects the cancel flag.
    """

    def setUp(self):
        clear_cancel()

    def test_do_search_aborts_during_inflight_http(self):
        """
        _do_search wraps httpx in a daemon thread and polls cancel every 0.5s.
        If cancel fires while the HTTP call is in-flight it must raise within ~1s.
        """
        import threading as _threading

        # Simulate a slow httpx.post (3s) via a mock module
        class _MockHttpx:
            @staticmethod
            def post(url, **kwargs):
                time.sleep(3)
                return type("R", (), {"status_code": 200, "json": lambda: {}})()

        errors = []
        done_evt = _threading.Event()

        def _run():
            try:
                from revit_mcp.rag.vertex_rag import _do_search
                _do_search(_MockHttpx, "http://fake", {}, "tok")
            except RuntimeError as e:
                errors.append(e)
            finally:
                done_evt.set()

        t = _threading.Thread(target=_run)
        t.start()
        time.sleep(0.2)
        request_cancel()
        done_evt.wait(timeout=2.0)

        self.assertFalse(t.is_alive(), "_do_search did not unblock within 2s")
        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", str(errors[0]).lower())

    def test_do_search_raises_immediately_if_already_cancelled(self):
        request_cancel()

        class _MockHttpx:
            @staticmethod
            def post(url, **kwargs):
                raise AssertionError("httpx.post must not be called when already cancelled")

        from revit_mcp.rag.vertex_rag import _do_search
        with self.assertRaises(RuntimeError):
            _do_search(_MockHttpx, "http://fake", {}, "tok")

    def test_run_retrieve_rules_polls_cancel(self):
        """
        run_retrieve_rules must stop within ~1s of cancel being set,
        not sit out the full 150s timeout.
        """
        import threading as _threading

        errors = []
        done_evt = _threading.Event()

        def _slow_retrieve_rules():
            # Simulate a slow RAG call (10s)
            time.sleep(10)
            return {}

        def _run():
            try:
                # Patch _run_in_new_loop to use our slow function
                import revit_mcp.agents.sub_agent as sa
                original = sa._run_in_new_loop
                sa._run_in_new_loop = lambda coro: _slow_retrieve_rules()
                try:
                    from revit_mcp.agents.sub_agent import run_retrieve_rules
                    run_retrieve_rules({"topics": ["staircase"]})
                finally:
                    sa._run_in_new_loop = original
            except RuntimeError as e:
                errors.append(e)
            finally:
                done_evt.set()

        t = _threading.Thread(target=_run)
        t.start()
        time.sleep(0.3)
        request_cancel()
        done_evt.wait(timeout=2.0)

        self.assertFalse(t.is_alive(), "run_retrieve_rules did not unblock within 2s after cancel")
        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", str(errors[0]).lower())

    def test_fetch_topic_skipped_when_cancelled(self):
        """check_cancelled() at top of _fetch_topic raises before any network call."""
        request_cancel()

        async def _run():
            from revit_mcp.agents.sub_agent import _fetch_topic
            with self.assertRaises(RuntimeError):
                await _fetch_topic("staircase", {"topics": ["staircase"]})

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# End-to-end: stop button scenario
# ---------------------------------------------------------------------------

class TestStopButtonScenario(unittest.TestCase):
    """
    Full scenario: background thread executes a multi-level build;
    'user clicks stop' after 3 levels by setting the cancel flag;
    build must abort and not reach level 10.
    """

    def setUp(self):
        clear_cancel()

    def test_stop_aborts_build_early(self):
        levels_built = []
        build_done = threading.Event()

        def fake_build():
            clear_cancel()
            try:
                for lvl in range(1, 11):
                    check_cancelled("level {}".format(lvl))
                    time.sleep(0.05)       # simulate Revit work per level
                    levels_built.append(lvl)
            except RuntimeError:
                pass
            finally:
                build_done.set()

        t = threading.Thread(target=fake_build)
        t.start()

        # User "clicks stop" after ~0.18s (≈ 3 levels × 0.05s each)
        time.sleep(0.18)
        request_cancel()

        build_done.wait(timeout=3)
        t.join(timeout=3)

        self.assertFalse(t.is_alive(), "Build thread did not terminate")
        self.assertGreater(len(levels_built), 0, "At least some levels should have been built")
        self.assertLess(len(levels_built), 10, "Build must have been cut short, not all 10 levels")
        print("  Stopped after {} / 10 levels built.".format(len(levels_built)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
