# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""Control panel behaviour against each mock device fault, over real HTTP."""
from datetime import datetime, timedelta
import io
import time
import unittest

from support import set_and_start, CONFIRM_SECONDS, MockDeviceMixin, control


class DeviceResponseTests(MockDeviceMixin, unittest.TestCase):
    def setUp(self):
        self.start_mock_device()
        self.set_mock_status(8)

    def history(self):
        return self.device.get('/mock/history').json['commands']

    def setpoint(self):
        return self.device.get('/mock/state').json['state']['APC_P_SV']

    def set_now(self, value, **fault):
        """Create a one-row set schedule, apply a fault, and run the row."""
        self.set_mock_status(9)
        set_and_start(self.client, to=value)
        self.flashes()
        if fault:
            self.configure_mock(**fault)
        self.runner.tick()
        return self.runner.snapshot()

    def assert_error_flash(self, *fragments):
        flashes = self.flashes()
        errors = [text for category, text in flashes if category == 'error']
        self.assertEqual(len(errors), 1, flashes)
        for fragment in fragments:
            self.assertIn(fragment, errors[0])

    def test_run_confirmation_waits_for_a_delayed_run_flag(self):
        self.configure_mock(run_delay=0.4)
        started = time.monotonic()
        self.client.post('/regulation/on')
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        self.assertIn(('message', 'Regulation turned on.'), self.flashes())
        self.assertEqual(len(self.history()), 1)

    def test_stale_button_press_does_not_send_an_unneeded_pulse(self):
        for status, action, message in ((9, 'on', 'Regulation is already on.'), (8, 'off', 'Regulation is already off.')):
            with self.subTest(action=action):
                self.set_mock_status(status)
                self.client.post('/regulation/' + action)
                self.assertEqual(self.flashes(), [('message', message)])
        self.assertEqual(self.history(), [])

    def test_set_confirmation_waits_for_a_delayed_setpoint(self):
        self.configure_mock(setpoint_delay=0.4)
        snapshot = self.set_now('33.3')
        self.assertEqual((snapshot['state'], snapshot['failed_count']), ('completed', 0))
        self.assertEqual(self.setpoint(), 33.3)

    def test_two_decimal_rounding_is_within_tolerance(self):
        self.configure_mock(setpoint_decimals=2)
        self.assertEqual(self.set_now('12.345')['failed_count'], 0)

    def test_coarse_rounding_fails_and_reports_the_readback(self):
        self.configure_mock(setpoint_decimals=1)
        snapshot = self.set_now('12.34')
        self.assertEqual(snapshot['failed_count'], 1)
        for fragment in ('APC_P_SV=12.34', 'Last readback', 'APC_P_SV=12.3'):
            self.assertIn(fragment, snapshot['error'])
        self.assertEqual(len(self.history()), 1)

    def test_remote_lost_after_run_is_reported_without_a_second_pulse(self):
        self.configure_mock(mode='remote_drop')
        self.client.post('/regulation/on')
        self.assert_error_flash('Regulation on failed', 'REMOTE flag was cleared')
        self.assertEqual(len(self.history()), 1)

    def test_alarm_after_run_fails_confirmation_and_is_visible(self):
        self.configure_mock(mode='alarm_on_run')
        self.client.post('/regulation/on')
        self.assert_error_flash('Regulation on failed', f'{CONFIRM_SECONDS:g} seconds', 'APC_STATUS_PV=10')
        status = self.client.get('/api/status').json
        self.assertTrue(status['alarm'])
        self.assertFalse(status['running'])

    def test_command_applied_then_http_error_is_a_failure_without_retry(self):
        self.configure_mock(mode='accept_then_error', http_status=500)
        snapshot = self.set_now('21')
        self.assertEqual(snapshot['failed_count'], 1)
        self.assertIn('HTTP status: 500', snapshot['error'])
        self.assertEqual(self.setpoint(), 21.0)
        self.assertEqual(len(self.history()), 1)

    def test_configured_http_status_reaches_the_browser_message(self):
        self.configure_mock(mode='http_error', http_status=418)
        response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 502)
        self.assertIn('418', response.json['error'])

    def test_invalid_status_responses_have_clear_messages(self):
        cases = {'malformed': 'invalid JSON', 'empty': 'empty response', 'bad_status': 'between 0 and 15',
                 'drop_connection': 'communication failed', 'redirect': 'HTTP status: 302'}
        for mode, message in cases.items():
            with self.subTest(mode=mode):
                self.configure_mock(mode=mode)
                response = self.client.get('/api/status')
                self.assertEqual(response.status_code, 502)
                self.assertIn(message, response.json['error'])

    def test_dropped_command_connection_is_reported_and_not_applied(self):
        snapshot = self.set_now('21', mode='drop_connection', fail_count=2)
        self.assertIn('communication failed', snapshot['error'])
        self.assertEqual(self.history(), [])

    def test_missing_setpoint_keeps_status_available_but_no_schedule_starts(self):
        self.configure_mock(mode='no_setpoint')
        self.assertIsNone(self.client.get('/api/status').json['pressure_value'])
        self.set_mock_status(9)
        set_and_start(self.client, to='21')
        self.assert_error_flash('Could not set schedule', 'did not report the current APC_P_SV')
        self.assertEqual(self.history(), [])

    def test_confirmation_survives_transient_status_errors_without_resending(self):
        snapshot = self.set_now('21', mode='malformed', fail_count=2)
        self.assertEqual(snapshot['failed_count'], 0)
        self.assertEqual(self.device.get('/mock/state').json['request_counts']['command'], 1)

    def test_trickled_body_completes_or_times_out_on_the_total_deadline(self):
        self.configure_mock(mode='trickle', delay=0.2)
        self.assertEqual(control.get_device_status(timeout=5)['status'], 8)
        self.configure_mock(mode='trickle', delay=1.5)
        with self.assertRaises(control.DeviceTimeoutError):
            control.get_device_status(timeout=0.2)

    def test_scheduled_row_fails_then_next_row_runs_after_recovery(self):
        self.set_mock_status(9)
        base = datetime.now() + timedelta(seconds=60)
        csv = f'datetime,P (hPa)\n{base.isoformat()},10\n{(base + timedelta(seconds=1)).isoformat()},20\n'
        self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(csv.encode()), 'test.csv')})
        self.client.post('/schedule/start')
        self.configure_mock(mode='http_error', fail_count=2)
        self.runner.tick(now=base + timedelta(seconds=1))
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot['state'], 'completed')
        self.assertEqual(snapshot['failed_count'], 1)
        self.assertIn('HTTP status: 503', snapshot['error'])
        self.assertEqual(self.setpoint(), 20.0)

    def test_status_link_changes_are_logged_once(self):
        logs = lambda: [line for line in self.client.get('/api/logs?limit=1000').json['logs']
                        if 'Device status ' in line]
        self.client.get('/api/status')
        before = len(logs())
        self.configure_mock(mode='http_error')
        self.client.get('/api/status')
        self.client.get('/api/status')
        self.assertEqual(len(logs()), before + 1)
        self.assertIn('unavailable', logs()[-1])
        self.configure_mock(mode='normal')
        self.client.get('/api/status')
        self.client.get('/api/status')
        self.assertEqual(len(logs()), before + 2)
        self.assertIn('Device status available', logs()[-1])
