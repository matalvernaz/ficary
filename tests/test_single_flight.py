"""Round-10 H1: single-flight download dedupe + output-path lock."""
import threading
import time
from concurrent.futures import Future

from ficary import single_flight
from ficary.download_queue import DownloadQueues


class TestClaimRegistry:
    def setup_method(self):
        single_flight._inflight.clear()

    def test_first_claim_owns_second_joins(self):
        f1 = Future()
        assert single_flight.claim("k", f1) is None  # free
        f2 = Future()
        joined = single_flight.claim("k", f2)
        assert joined is f1  # second caller joins the first

    def test_release_frees_key(self):
        f1 = Future()
        single_flight.claim("k", f1)
        single_flight.release("k", f1)
        f2 = Future()
        assert single_flight.claim("k", f2) is None  # free again

    def test_release_is_identity_guarded(self):
        f1 = Future()
        single_flight.claim("k", f1)
        f2 = Future()  # a superseding registration
        # A stale release naming a different future must not evict f1.
        single_flight.release("k", f2)
        f3 = Future()
        assert single_flight.claim("k", f3) is f1

    def test_done_future_is_not_joined(self):
        f1 = Future()
        f1.set_result(None)  # already settled
        single_flight._inflight["k"] = f1
        f2 = Future()
        assert single_flight.claim("k", f2) is None  # stale done entry replaced


class TestEnqueueDedupe:
    def setup_method(self):
        single_flight._inflight.clear()
        DownloadQueues._queues.clear()

    def test_same_key_runs_once(self):
        started = threading.Event()
        release = threading.Event()
        runs = []

        def job():
            runs.append(1)
            started.set()
            release.wait(2.0)
            return "done"

        f1 = DownloadQueues.enqueue("testsite", job, dedupe_key="story:1")
        started.wait(2.0)
        # Second enqueue while the first is in flight joins it.
        f2 = DownloadQueues.enqueue("testsite", job, dedupe_key="story:1")
        assert f2 is f1
        release.set()
        assert f1.result(timeout=2.0) == "done"
        assert runs == [1]  # body ran once

    def test_different_keys_both_run(self):
        runs = []
        f1 = DownloadQueues.enqueue(
            "s", lambda: runs.append("a"), dedupe_key="k-a")
        f2 = DownloadQueues.enqueue(
            "s", lambda: runs.append("b"), dedupe_key="k-b")
        f1.result(timeout=2.0)
        f2.result(timeout=2.0)
        assert sorted(runs) == ["a", "b"]

    def test_key_reusable_after_completion(self):
        f1 = DownloadQueues.enqueue("s", lambda: 1, dedupe_key="k")
        f1.result(timeout=2.0)
        # After the first settled, the key is free for a fresh flight.
        f2 = DownloadQueues.enqueue("s", lambda: 2, dedupe_key="k")
        assert f2 is not f1
        assert f2.result(timeout=2.0) == 2


class TestPathLock:
    def test_serialises_same_path(self, tmp_path):
        p = tmp_path / "out.epub"
        order = []
        gate = threading.Event()

        def writer(tag):
            with single_flight.path_lock(p):
                order.append(f"{tag}-in")
                gate.wait(1.0)
                order.append(f"{tag}-out")

        t1 = threading.Thread(target=writer, args=("a",))
        t1.start()
        time.sleep(0.05)  # let t1 acquire
        t2 = threading.Thread(target=writer, args=("b",))
        t2.start()
        time.sleep(0.05)
        gate.set()
        t1.join(2.0)
        t2.join(2.0)
        # Second writer's critical section never interleaves the first's.
        assert order[:2] == ["a-in", "a-out"] or order[:2] == ["b-in", "b-out"]

    def test_reentrant_same_thread(self, tmp_path):
        p = tmp_path / "out.epub"
        with single_flight.path_lock(p):
            with single_flight.path_lock(p):  # would deadlock on a plain Lock
                pass

    def test_different_paths_dont_block(self, tmp_path):
        with single_flight.path_lock(tmp_path / "a"):
            # Acquiring a different path from the same thread must not block.
            acquired = threading.Event()

            def other():
                with single_flight.path_lock(tmp_path / "b"):
                    acquired.set()

            t = threading.Thread(target=other)
            t.start()
            assert acquired.wait(1.0)
            t.join(1.0)

    def test_path_normalisation_collapses(self, tmp_path):
        p1 = tmp_path / "sub" / ".." / "out.epub"
        p2 = tmp_path / "out.epub"
        assert single_flight._normalise(p1) == single_flight._normalise(p2)
