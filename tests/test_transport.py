# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stdout
import io
import json
import threading
import time
import unittest
from unittest.mock import patch
from support import control


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.code, self.body, self.delay = 200, b'Start', 0
        self.received = threading.Event()
        self.requests = []
        self.trickle = False
        case = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.requests.append(self.path)
                case.received.set()
                time.sleep(case.delay)
                self.send_response(200 if self.path == '/redirected' else case.code)
                if 300 <= case.code < 400 and self.path != '/redirected':
                    self.send_header('Location', '/redirected')
                self.end_headers()
                try:
                    if case.trickle:
                        for _ in range(30):
                            self.wfile.write(b'x')
                            self.wfile.flush()
                            time.sleep(0.05)
                    else:
                        self.wfile.write(case.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            do_POST = do_GET
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/'
        self.base_patch = patch.object(control, 'DEVICE_URL', self.url)
        self.base_patch.start()

    def tearDown(self):
        self.base_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_command_http_error_is_not_success(self):
        self.code = 503
        with self.assertRaises(RuntimeError):
            control.send_command(self.url)

    def test_console_shows_command_url_even_when_device_rejects_it(self):
        self.code = 503
        url = self.url + 'Set_Data?APC_P_SV=12.5'
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(control.DeviceHTTPError):
            control.send_command(url)
        self.assertEqual(output.getvalue(), f'Command: POST {url}\n')
        self.assertEqual(self.requests, ['/Set_Data?APC_P_SV=12.5'])

    def test_console_omits_status_reads_and_commands_cancelled_before_sending(self):
        output = io.StringIO()
        cancelled = threading.Event()
        cancelled.set()
        with redirect_stdout(output):
            control._request_device(self.url)
            with self.assertRaises(control.ScheduleCancelled):
                control.send_command(self.url, cancelled)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.requests, ['/'])

    def test_status_http_error_is_not_accepted_even_with_valid_json(self):
        self.code = 500
        self.body = b'{"APC_STATUS_PV": 1, "APC_P_SV": 12}'
        with self.assertRaises(RuntimeError):
            control.get_device_status()

    def test_device_diagnostic_prefix_is_supported(self):
        self.body = b'Device diagnostics\n{"APC_STATUS_PV": 9, "APC_P_SV": 12.5}\n'
        result = control.get_device_status()
        self.assertEqual(result['status'], 9)
        self.assertEqual(result['indicators'], [True, False, False, True])
        self.assertEqual(result['pressure_value'], 12.5)

    def test_fractional_status_is_not_silently_truncated(self):
        self.body = b'{"APC_STATUS_PV": 1.5}'
        with self.assertRaises(ValueError):
            control.get_device_status()

    def test_malformed_response_is_reported_to_browser(self):
        self.body = b'not json'
        response = control.app.test_client().get('/api/status')
        self.assertEqual(response.status_code, 502)
        self.assertIn('error', response.json)

    def test_large_command_response_is_read_completely(self):
        self.body = b'x' * 131072
        self.assertEqual(control.send_command(self.url, threading.Event()), 'x' * 131072)

    def test_inflight_request_is_cancelled_promptly(self):
        self.delay = 2
        event = threading.Event()
        outcome = []
        def send():
            try:
                control.send_command(self.url, event)
            except Exception as exc:
                outcome.append(exc)
        thread = threading.Thread(target=send)
        thread.start()
        self.assertTrue(self.received.wait(2))
        event.set()
        thread.join(1.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], control.ScheduleCancelled)

    def test_redirect_is_not_a_successful_command_or_status(self):
        for code in (302, 307):
            with self.subTest(code=code):
                self.code = code
                self.body = b'{"APC_STATUS_PV": 9, "APC_P_SV": 12}'
                before = len(self.requests)
                with self.assertRaises(RuntimeError):
                    control.send_command(self.url)
                with self.assertRaises(RuntimeError):
                    control.get_device_status()
                self.assertEqual(len(self.requests) - before, 2)
                self.assertNotIn('/redirected', self.requests)

    def test_status_read_can_be_cancelled_while_server_is_slow(self):
        self.delay = 2
        event = threading.Event()
        outcome = []
        def read():
            try:
                control.get_device_status(cancel_event=event)
            except Exception as exc:
                outcome.append(exc)
        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(self.received.wait(2))
        event.set()
        thread.join(1.5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], control.ScheduleCancelled)

    def test_get_and_post_work_without_external_processes(self):
        with patch('subprocess.run', side_effect=AssertionError('external command used')), patch('subprocess.Popen', side_effect=AssertionError('external command used')):
            self.body = b'{"APC_STATUS_PV": 8, "APC_P_SV": 12.5}'
            self.assertEqual(control.get_device_status()['pressure_value'], 12.5)
            self.assertIn('APC_STATUS_PV', control._request_device(self.url, method='POST'))


    def test_total_deadline_includes_a_slowly_trickling_body(self):
        self.trickle = True
        started = time.monotonic()
        with self.assertRaises(control.DeviceTimeoutError):
            control._request_device(self.url, timeout=0.25)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(len(self.requests), 1)

    def test_connection_failure_has_a_communication_error(self):
        from unittest.mock import AsyncMock
        with patch.object(control.httpx.AsyncClient, 'request', new=AsyncMock(side_effect=control.httpx.ConnectError('connection refused'))):
            with self.assertRaises(control.DeviceCommunicationError) as error:
                control._request_device(self.url, timeout=1)
        self.assertIn('communication failed', str(error.exception))
        self.assertIn('connection refused', str(error.exception))

    def test_cancelled_request_releases_resources_for_next_request(self):
        self.delay = 2
        event = threading.Event()
        outcome = []
        def read():
            try:
                control._request_device(self.url, cancel_event=event)
            except Exception as exc:
                outcome.append(exc)
        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(self.received.wait(2))
        event.set()
        thread.join(1.5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], control.ScheduleCancelled)
        self.delay = 0
        self.assertEqual(control._request_device(self.url), 'Start')
