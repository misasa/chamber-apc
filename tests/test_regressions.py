# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from support import control, mock_device


class CSVRegressionTests(unittest.TestCase):
    def read(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.csv'
            path.write_text(content, encoding='utf-8-sig')
            return control.read_schedule(path)

    def test_timezone_csv_rejected_before_scheduling(self):
        with self.assertRaisesRegex(ValueError, 'local|timezone'):
            self.read('datetime,P (hPa)\n2030-01-01T10:00:00+09:00,12\n')

    def test_extra_csv_values_report_validation_error(self):
        with self.assertRaises(ValueError):
            self.read('datetime,P (hPa)\n2030-01-01 10:00:00,12,extra\n')

    def test_duplicate_header_rejected(self):
        with self.assertRaises(ValueError):
            self.read('datetime,P (hPa),P (hPa)\n2030-01-01 10:00:00,12,13\n')

    def test_rows_are_sent_in_time_order(self):
        rows = self.read('datetime,P (hPa)\n2030-01-01 10:02:00,22\n2030-01-01 10:01:00,11\n')
        self.assertEqual([r['P (hPa)'] for r in rows], ['11', '22'])

    def test_duplicate_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            self.read('datetime,P (hPa)\n2030-01-01 10:00:00,12\n2030-01-01 10:00:00,13\n')


class CommandRegressionTests(unittest.TestCase):
    def test_cancelled_command_never_starts_http_request(self):
        event = threading.Event()
        event.set()
        with patch.object(control.httpx, 'AsyncClient') as client:
            with self.assertRaises(control.ScheduleCancelled):
                control.send_command('http://127.0.0.1:1/', event)
            client.assert_not_called()

    def test_upload_same_name_three_times_keeps_all_contents(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(control, 'get_device_status', return_value={'status': 1}), patch.object(control, 'UPLOAD_DIR', Path(directory)), patch.object(control, 'scheduler', control.ScheduleController(control.send_value)), patch.object(control.time, 'time', return_value=1234):
            client = control.app.test_client()
            for value in (11, 22, 33):
                csv = f'datetime,P (hPa)\n2030-01-01 10:00:00,{value}\n'.encode()
                response = client.post('/schedule/upload', data={'csv_file': (io.BytesIO(csv), 'same.csv')})
                self.assertEqual(response.status_code, 200)
            contents = [p.read_text() for p in Path(directory).iterdir()]
            self.assertEqual(len(contents), 3)
            for value in (11, 22, 33):
                self.assertTrue(any(f',{value}\n' in s for s in contents))


class MockRegressionTests(unittest.TestCase):
    def setUp(self):
        with mock_device.state_lock:
            mock_device.state.update(APC_STATUS_PV=0, APC_P_SV=50.0)
        self.client = mock_device.app.test_client()

    def test_invalid_pressure_is_400_and_does_not_partially_apply_run(self):
        response = self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1&APC_P_SV=bad')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(mock_device.state['APC_STATUS_PV'], 0)
        self.assertEqual(mock_device.state['APC_P_SV'], 50.0)

    def test_nonfinite_pressure_rejected_in_both_interfaces(self):
        for value in ('nan', 'inf', '-inf'):
            with self.subTest(value=value):
                self.assertEqual(self.client.post('/WeatherMonitor/Set_Data', query_string={'APC_P_SV': value}).status_code, 400)
                self.assertEqual(self.client.post('/set-status', data={'pressure': value}).status_code, 400)
