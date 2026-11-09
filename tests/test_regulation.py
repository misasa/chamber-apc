# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from datetime import datetime, timedelta
from unittest.mock import ANY, patch
from support import Clock, control


class RegulationTests(unittest.TestCase):
    def setUp(self):
        self.client = control.app.test_client()

    def post(self, action):
        self.client.post('/regulation/' + action)
        with self.client.session_transaction() as session:
            return session.pop('_flashes', [])

    def status(self, bits):
        return {'status': bits, 'running': bool(bits & 1), 'remote': bool(bits & 8)}

    def test_remote_off_rejects_both_commands_without_sending(self):
        for action in ('on', 'off'):
            for status in (0, 1, 2, 4, 7):
                with self.subTest(action=action, status=status), patch.object(control, 'get_device_status', return_value=self.status(status)), patch.object(control, 'send_command') as send:
                    flashes = self.post(action)
                    self.assertTrue(any(category == 'error' and 'REMOTE' in text for category, text in flashes))
                    send.assert_not_called()

    def test_success_requires_observing_requested_run_bit(self):
        for action, initial, expected in (('on', 8, 9), ('off', 15, 14)):
            clock = Clock()
            with self.subTest(action=action), patch.object(control, 'get_device_status', side_effect=[self.status(initial), self.status(initial), self.status(expected)]), patch.object(control, 'send_command', return_value='ok'), patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
                flashes = self.post(action)
                self.assertFalse(any(category == 'error' for category, _ in flashes))
                expected_text = f'Regulation turned {action}.'
                self.assertIn(('message', expected_text), flashes)
                self.assertGreater(clock.now, 0)

    def test_unchanged_run_bit_errors_at_ten_seconds_for_both_actions(self):
        for action, initial in (('on', 8), ('off', 9)):
            clock = Clock()
            with self.subTest(action=action), patch.object(control, 'get_device_status', return_value=self.status(initial)), patch.object(control, 'send_command', return_value='ok'), patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
                flashes = self.post(action)
                self.assertTrue(any(category == 'error' and '10' in text for category, text in flashes))
                self.assertAlmostEqual(clock.now, 10.0)

    def test_remote_lost_during_confirmation_errors(self):
        with patch.object(control, 'get_device_status', side_effect=[self.status(8), self.status(1)]), patch.object(control, 'send_command', return_value='ok'):
            flashes = self.post('on')
            self.assertTrue(any(category == 'error' and 'REMOTE' in text for category, text in flashes))

    def test_command_time_consumes_confirmation_budget(self):
        clock = Clock()
        def send(*args, **kwargs):
            clock.now += 9.5
            return 'ok'
        with patch.object(control, 'get_device_status', return_value=self.status(8)), patch.object(control, 'send_command', side_effect=send), patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
            flashes = self.post('on')
            self.assertTrue(any(category == 'error' for category, _ in flashes))
            self.assertAlmostEqual(clock.now, 10.0)

    def test_concurrent_regulation_request_does_not_send_another_pulse(self):
        control.regulation_lock.acquire()
        try:
            with patch.object(control, 'send_command') as send:
                flashes = self.post('off')
                self.assertTrue(any(category == 'error' for category, _ in flashes))
                send.assert_not_called()
        finally:
            control.regulation_lock.release()

    def test_off_invalidates_a_go_with_a_stale_regulation_on_read(self):
        runner = control.ScheduleController(lambda value, cancel_event: None)
        due = datetime.now() + timedelta(hours=1)
        runner.load(control.Path('future.csv'), [{'_when': due, '_line': '2', 'datetime': str(due), 'P (hPa)': '10'}])
        revision = runner.revision
        def stale_read(**kwargs):
            with patch.object(control, 'get_device_status', side_effect=[self.status(9), self.status(8)]):
                control.regulate('off')
            return self.status(9)
        with patch.object(control, 'scheduler', runner), \
             patch.object(control, 'get_device_status', side_effect=stale_read), \
             patch.object(control, 'send_command') as send:
            self.client.post('/schedule/start', data={'revision': revision})
        self.assertEqual(runner.state, 'ready')
        self.assertGreater(runner.revision, revision)
        send.assert_called_once_with(control.STOP_URL, timeout=ANY)
