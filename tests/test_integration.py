# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime, timedelta
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from support import set_and_start, CONFIRM_SECONDS, MockDeviceMixin, control


class IntegrationTests(MockDeviceMixin, unittest.TestCase):
    def setUp(self):
        self.start_mock_device()
        self.device.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=9')

    def load_and_start(self, rows):
        data = 'datetime,P (hPa)\n' + ''.join(f'{when.isoformat()},{value}\n' for when, value in rows)
        response = self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(data.encode()), 'test.csv')})
        if response.status_code == 200:
            return self.client.post('/schedule/start')
        return response

    def history(self):
        return self.device.get('/mock/history').json['commands']

    def test_regulation_on_setpoint_off_set_stop_round_trip_and_rendering(self):
        self.device.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.device.get('/').status_code, 200)
        self.client.post('/regulation/on')
        self.assertTrue(self.client.get('/api/status').json['running'])
        set_and_start(self.client, to='12.34')
        self.runner.tick()
        self.assertEqual(self.client.get('/api/status').json['pressure_value'], 12.34)
        self.client.post('/regulation/off')
        self.assertFalse(self.client.get('/api/status').json['running'])
        self.assertEqual(len(self.history()), 3)

    def test_upload_execute_stop_resume_and_complete(self):
        base = datetime.now() + timedelta(seconds=60)
        self.load_and_start([(base, 10), (base+timedelta(seconds=1), 20), (base+timedelta(seconds=2), 30)])
        self.runner.tick(now=base)
        self.client.post('/schedule/end')
        self.runner.tick(now=base+timedelta(seconds=1))
        self.assertEqual(len(self.history()), 1)
        self.runner.resume(now=base+timedelta(seconds=1, milliseconds=500))
        self.runner.tick(now=base+timedelta(seconds=2))
        self.assertEqual([c['state']['APC_P_SV'] for c in self.history()], [10.0, 30.0])
        self.assertEqual(self.client.get('/api/schedule').json['state'], 'completed')

    def test_failed_upload_preserves_running_schedule(self):
        base = datetime.now() + timedelta(seconds=60)
        self.load_and_start([(base, 10)])
        before = self.client.get('/api/schedule').json
        self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(b'datetime,P (hPa)\nbad,12\n'), 'bad.csv')})
        self.assertEqual(self.client.get('/api/schedule').json, before)
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)

    def test_http_failure_is_visible_and_later_commands_continue(self):
        base = datetime.now() + timedelta(seconds=60)
        self.load_and_start([(base, 10), (base+timedelta(seconds=1), 20)])
        self.device.post('/mock/config', json={'mode': 'http_error'})
        self.runner.tick(now=base)
        self.assertEqual(self.client.get('/api/schedule').json['state'], 'running')
        self.assertEqual(self.history(), [])
        self.assertTrue(any('Scheduled command failed' in line for line in self.client.get('/api/logs').json['logs']))
        self.device.post('/mock/config', json={'mode': 'normal'})
        self.runner.tick(now=base+timedelta(seconds=2))
        self.assertEqual([c['state']['APC_P_SV'] for c in self.history()], [20.0])

    def test_actual_worker_sends_scheduled_command(self):
        due = datetime.now() + timedelta(seconds=1)
        self.load_and_start([(due, 7.5)])
        stop = threading.Event()
        thread = threading.Thread(target=control.schedule_worker, args=(stop,))
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not self.history() and time.monotonic() < deadline:
                stop.wait(0.05)
            self.assertEqual([c['state']['APC_P_SV'] for c in self.history()], [7.5])
        finally:
            stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_unwritable_upload_location_reports_error_without_losing_schedule(self):
        base = datetime.now() + timedelta(seconds=60)
        self.load_and_start([(base, 10)])
        before = self.client.get('/api/schedule').json
        blocked = Path(self.storage.name) / 'file-not-directory'
        blocked.write_text('occupied')
        with patch.object(control, 'UPLOAD_DIR', blocked):
            self.assertEqual(self.load_and_start([(base, 20)]).status_code, 500)
        self.assertEqual(self.client.get('/api/schedule').json, before)

    def test_invalid_set_pressure_never_reaches_device(self):
        for value in ('nan', 'inf', '', '12&APC_RUN_SV=1'):
            set_and_start(self.client, to=value)
        self.assertEqual(self.history(), [])

    def test_real_run_stop_timeouts_and_next_operation_is_allowed(self):
        self.device.post('/mock/config', json={'mode': 'ignore_control'})
        for action, status in (('on', 8), ('off', 9)):
            with self.subTest(action=action):
                self.device.get('/WeatherMonitor/Set_Mock_Status', query_string={'APC_STATUS_PV': status})
                count = len(self.history())
                started = time.monotonic()
                self.client.post('/regulation/' + action)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, CONFIRM_SECONDS * 0.98)
                self.assertLess(elapsed, CONFIRM_SECONDS + 3)
                with self.client.session_transaction() as session:
                    flashes = session.pop('_flashes', [])
                self.assertTrue(any(category == 'error' and f'{CONFIRM_SECONDS:g} seconds' in text for category, text in flashes))
                self.assertEqual(len(self.history()), count + 1)  # Pulse sent once, never retried.
        self.device.post('/mock/config', json={'mode': 'normal'})
        self.client.post('/regulation/off')
        self.assertFalse(self.client.get('/api/status').json['running'])
        with self.client.session_transaction() as session:
            self.assertFalse(any(category == 'error' for category, _ in session.pop('_flashes', [])))

    def test_real_setpoint_timeout_reports_error_then_next_schedule_row_runs(self):
        base = datetime.now() + timedelta(seconds=60)
        self.load_and_start([(base, 10), (base+timedelta(seconds=1), 20)])
        self.device.post('/mock/config', json={'mode': 'ignore_setpoint'})
        started = time.monotonic()
        self.runner.tick(now=base)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, CONFIRM_SECONDS * 0.98)
        self.assertLess(elapsed, CONFIRM_SECONDS + 3)
        snapshot = self.client.get('/api/schedule').json
        self.assertEqual(snapshot['state'], 'running')
        self.assertEqual(snapshot['failed_count'], 1)
        self.assertIn(f'{CONFIRM_SECONDS:g} seconds', snapshot['error'])
        self.assertEqual(len(self.history()), 1)
        self.device.post('/mock/config', json={'mode': 'normal'})
        self.runner.tick(now=base+timedelta(seconds=1))
        self.assertEqual(self.client.get('/api/status').json['pressure_value'], 20.0)
        self.assertEqual(self.runner.snapshot()['state'], 'completed')


    def test_schedule_start_and_restart_require_observed_run_flag(self):
        self.device.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        base = datetime.now() + timedelta(seconds=60)
        self.load_and_start([(base, 10)])
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.client.post('/regulation/on')
        self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot()['state'], 'running')
        self.client.post('/schedule/end')
        self.client.post('/regulation/off')
        self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot()['state'], 'stopped')
        self.client.post('/regulation/on')
        self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot()['state'], 'running')
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)
