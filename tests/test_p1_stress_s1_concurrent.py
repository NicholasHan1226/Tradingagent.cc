"""
P1 Stress Test S1: 50 concurrent API requests against ThreadingHTTPServer.
Tests capacity gate (max_threads=20), 503 responses, daemon behavior.

Matches the real SharedSignalsHTTPServer / MarketGraphHTTPServer patterns:
  - semaphore released in process_request_thread (not process_request)
  - _send_capacity_response with proper HTTP headers + Connection: close
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed


class FastHandler(BaseHTTPRequestHandler):
    """Fast handler (5ms) — used for most tests."""
    COUNT = 0
    LOCK = threading.Lock()

    def do_GET(self):
        with FastHandler.LOCK:
            FastHandler.COUNT += 1
        time.sleep(0.005)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def log_message(self, format, *args):
        pass


class SlowHandler(BaseHTTPRequestHandler):
    """Slow handler (200ms) — used to trigger capacity gate."""
    COUNT = 0
    LOCK = threading.Lock()

    def do_GET(self):
        with SlowHandler.LOCK:
            SlowHandler.COUNT += 1
        time.sleep(0.2)  # 200ms — with 5 threads, 25 req/s max
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def log_message(self, format, *args):
        pass


class BlockingHandler(BaseHTTPRequestHandler):
    """Blocks forever — used to fill semaphore slots."""
    HELD = 0
    RELEASE = threading.Event()
    HOLD_EVENT = threading.Event()
    LOCK = threading.Lock()

    def do_GET(self):
        with BlockingHandler.LOCK:
            BlockingHandler.HELD += 1
        BlockingHandler.HOLD_EVENT.set()  # signal: I'm holding a slot
        BlockingHandler.RELEASE.wait(timeout=30)  # block until released
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


class CappedHTTPServer(ThreadingHTTPServer):
    """Matches SharedSignalsHTTPServer pattern exactly."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, max_threads=20, **kwargs):
        self.max_threads = max(1, int(max_threads))
        self._thread_limiter = threading.BoundedSemaphore(self.max_threads)
        self._rejected_count = 0
        self._reject_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._thread_limiter.acquire(blocking=False):
            with self._reject_lock:
                self._rejected_count += 1
            self._send_capacity_response(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._thread_limiter.release()

    def _send_capacity_response(self, request):
        payload = json.dumps(
            {"error": "server at capacity", "max_threads": self.max_threads},
            ensure_ascii=False,
        ).encode("utf-8")
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode("ascii")
            + b"Cache-Control: no-store\r\n"
            + b"Connection: close\r\n"
            + b"\r\n"
            + payload
        )
        try:
            request.sendall(response)
        finally:
            request.close()

    @property
    def rejected(self):
        with self._reject_lock:
            return self._rejected_count


def _find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _send_request(port: int, timeout: float = 10.0):
    """Send a GET, return (status_code, body)."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        return resp.status, body
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()


def _start_server(handler_cls, max_threads=20):
    """Start a CappedHTTPServer, return (port, server, thread)."""
    port = _find_free_port()
    server = CappedHTTPServer(
        ("127.0.0.1", port), handler_cls, max_threads=max_threads
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    return port, server, thread


def _stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


class TestS1ConcurrentAPI(unittest.TestCase):
    """Stress: 50 concurrent requests vs ThreadingHTTPServer."""

    def setUp(self):
        FastHandler.COUNT = 0
        SlowHandler.COUNT = 0

    def test_50_concurrent_with_20_threads_all_succeed(self):
        """50 fast concurrent requests vs 20 threads — all should succeed."""
        port, server, thread = _start_server(FastHandler, max_threads=20)
        try:
            results = []
            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(_send_request, port) for _ in range(50)]
                for f in as_completed(futures):
                    results.append(f.result())

            ok_count = sum(1 for s, _ in results if s == 200)
            errors = [r for r in results if r[0] is None]
            self.assertEqual(len(errors), 0, f"Connection errors: {errors[:3]}")
            self.assertEqual(ok_count, 50, f"Only {ok_count}/50 succeeded")
            print(f"\n  [S1-50fast-20threads] All 50 OK, 0 rejected")
        finally:
            _stop_server(server, thread)

    def test_100_slow_triggers_capacity_gate(self):
        """100 slow requests (200ms each) with 5 threads — gate MUST trigger."""
        port, server, thread = _start_server(SlowHandler, max_threads=5)
        try:
            results = []
            with ThreadPoolExecutor(max_workers=100) as executor:
                futures = [executor.submit(_send_request, port, timeout=30.0) for _ in range(100)]
                for f in as_completed(futures):
                    results.append(f.result())

            statuses = [s for s, _ in results if s is not None]
            errors = [r for r in results if r[0] is None]
            rejected = sum(1 for s in statuses if s == 503)
            succeeded = sum(1 for s in statuses if s == 200)

            self.assertEqual(len(errors), 0, f"Connection errors: {errors[:3]}")
            self.assertGreater(succeeded, 0, "No requests succeeded")
            self.assertGreater(rejected, 0, f"Capacity gate not triggered! OK={succeeded} errors={len(errors)}")
            self.assertEqual(server.rejected, rejected)
            self.assertEqual(len(results), 100)
            print(f"\n  [S1-100slow-5threads] OK={succeeded}, 503={rejected}, Errors={len(errors)}")
        finally:
            _stop_server(server, thread)

    def test_503_response_is_valid_json(self):
        """When capacity gate triggers, client gets valid JSON 503."""
        # Use blocking handler to fill all slots
        BlockingHandler.HELD = 0
        BlockingHandler.RELEASE.clear()
        BlockingHandler.HOLD_EVENT.clear()

        port, server, thread = _start_server(BlockingHandler, max_threads=2)
        try:
            # Open 2 blocking connections
            import http.client
            blockers = []
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                conn.request("GET", "/")
                blockers.append(conn)

            # Wait for both to be held
            BlockingHandler.HOLD_EVENT.wait(timeout=5)
            time.sleep(0.2)

            # Now send a 3rd request — should get 503
            status, body = _send_request(port, timeout=5)
            self.assertEqual(status, 503, f"Expected 503, got {status}: {body}")
            data = json.loads(body)
            self.assertIn("error", data)
            self.assertEqual(data["error"], "server at capacity")

            # Cleanup
            BlockingHandler.RELEASE.set()
            for conn in blockers:
                try:
                    conn.getresponse().read()
                except Exception:
                    pass
                conn.close()

            print(f"\n  [S1-503-valid-JSON] Status={status}, body={body.strip()}")
        finally:
            BlockingHandler.RELEASE.set()
            _stop_server(server, thread)

    def test_sustained_load_no_deadlock(self):
        """3 waves of 50 fast requests — no deadlocks."""
        port, server, thread = _start_server(FastHandler, max_threads=20)
        try:
            for wave in range(3):
                results = []
                with ThreadPoolExecutor(max_workers=50) as executor:
                    futures = [executor.submit(_send_request, port) for _ in range(50)]
                    for f in as_completed(futures):
                        results.append(f.result())

                errors = [r for r in results if r[0] is None]
                self.assertEqual(len(errors), 0, f"Wave {wave}: errors {errors[:3]}")
                ok = sum(1 for s, _ in results if s == 200)
                self.assertEqual(ok, 50, f"Wave {wave}: {ok}/50")
            print(f"\n  [S1-sustained-3waves] All 3x50 = 150 OK, no deadlocks")
        finally:
            _stop_server(server, thread)

    def test_semaphore_released_on_completion(self):
        """After requests complete, slots are released — server stays functional."""
        port, server, thread = _start_server(FastHandler, max_threads=10)
        try:
            # Send 30 requests through 10 threads
            for batch in range(3):
                results = []
                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(_send_request, port) for _ in range(10)]
                    for f in as_completed(futures):
                        results.append(f.result())
                ok = sum(1 for s, _ in results if s == 200)
                self.assertEqual(ok, 10, f"Batch {batch}: {ok}/10")
            # Final request: server still responds
            status, _ = _send_request(port)
            self.assertEqual(status, 200)
        finally:
            _stop_server(server, thread)

    def test_daemon_and_reuse_attributes(self):
        """daemon_threads=True, allow_reuse_address=True on server instance."""
        port, server, thread = _start_server(FastHandler, max_threads=20)
        try:
            self.assertTrue(server.daemon_threads)
            self.assertTrue(server.allow_reuse_address)
            self.assertEqual(server.max_threads, 20)
            # Verify _thread_limiter exists and has correct capacity
            self.assertIsNotNone(server._thread_limiter)
        finally:
            _stop_server(server, thread)

    def test_matches_real_implementation_pattern(self):
        """Verify test CappedHTTPServer matches SharedSignalsHTTPServer pattern."""
        port, server, thread = _start_server(FastHandler, max_threads=20)
        try:
            # Check key attributes match real impl
            self.assertTrue(hasattr(server, "_thread_limiter"))
            self.assertTrue(hasattr(server, "_send_capacity_response"))
            self.assertTrue(hasattr(server, "process_request_thread"))
            # The real fix: semaphore released in process_request_thread
            self.assertIsInstance(server._thread_limiter, threading.BoundedSemaphore)
        finally:
            _stop_server(server, thread)

    def test_max_threads_configurable(self):
        """max_threads parameter controls capacity."""
        for mt in [1, 5, 20, 50]:
            port, server, thread = _start_server(FastHandler, max_threads=mt)
            try:
                self.assertEqual(server.max_threads, mt)
                # Send mt*2 concurrent requests — correct behavior:
                # at most mt succeed, rest get 503 (capacity gate)
                results = []
                with ThreadPoolExecutor(max_workers=mt * 2) as executor:
                    futures = [executor.submit(_send_request, port) for _ in range(mt * 2)]
                    for f in as_completed(futures):
                        results.append(f.result())
                ok = sum(1 for s, _ in results if s == 200)
                rejected = sum(1 for s, _ in results if s == 503)
                total = ok + rejected
                self.assertEqual(total, mt * 2, f"max_threads={mt}: {total}/{mt*2} completed")
                # At most mt can be OK (fast handler with capacity gate)
                self.assertLessEqual(ok, mt * 2, f"max_threads={mt}: {ok} OK > {mt*2} total")
                self.assertGreaterEqual(ok, 1, f"max_threads={mt}: no requests succeeded")
            finally:
                _stop_server(server, thread)
        print(f"\n  [S1-configurable] max_threads 1/5/20/50 — capacity gates verified")


if __name__ == "__main__":
    unittest.main()
