# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""What the device must allow before a value is sent, and how a schedule stops when it does not."""

from datetime import datetime, timedelta
import io
import threading
import unittest
from unittest.mock import patch

from support import control


def rows_from(base, *values):
    return [{'_when': base + timedelta(seconds=index), '_line': str(index + 2), 'P (hPa)': value}
            for index, value in enumerate(values)]


class PressureLimitTests(unittest.TestCase):
    def test_values_outside_the_limits_are_refused(self):
        for value in ('-1', '-0.01', '100.01', '500', '1e5'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'from 0 to 100 hPa'):
                control.value_url(value)

    def test_the_limits_themselves_are_allowed(self):
        for value in ('0', '100', '50.12'):
            with self.subTest(value=value):
                self.assertIn('APC_P_SV=', control.value_url(value))

    def test_limits_come_from_the_environment_and_a_bad_one_stops_the_panel(self):
        with patch.dict(control.os.environ, {'APC_P_MAX': '200'}):
            self.assertEqual(control._pressure_limit('APC_P_MAX', '100'), 200.0)
        for text in ('high', 'nan', 'inf'):
            with self.subTest(text=text), patch.dict(control.os.environ, {'APC_P_MAX': text}), self.assertRaises(SystemExit):
                control._pressure_limit('APC_P_MAX', '100')

    def test_page_and_csv_refuse_a_value_over_a_narrower_limit(self):
        runner = control.ScheduleController(lambda value, cancel_event: None)
        client = control.app.test_client()
        with patch.object(control, 'scheduler', runner), patch.object(control, 'PRESSURE_MAX_HPA', 80.0), \
                patch.object(control, 'get_device_status', return_value={'status': 9, 'pressure_value': 50.0}), \
                patch.object(control, 'send_command') as send:
            client.post('/schedule/set', data={'to': '500'})
            with client.session_transaction() as session:
                flashes = session.pop('_flashes', [])
            self.assertTrue(any(category == 'error' and 'from 0 to 80 hPa' in text for category, text in flashes), flashes)
            csv = b'datetime,P (hPa)\n2030-01-01 10:00:00,500\n'
            response = client.post('/schedule/upload', data={'csv_file': (io.BytesIO(csv), 'a.csv')})
            self.assertEqual(response.status_code, 400)
            self.assertIn('CSV line 2: Pressure must be from 0 to 80 hPa', response.json['error'])
        send.assert_not_called()
        self.assertEqual(runner.snapshot()['state'], 'idle')


class OutOfRangeFromTests(unittest.TestCase):
    def test_ramp_is_refused_by_name_and_now_can_still_bring_the_setpoint_back(self):
        start = datetime(2030, 1, 1, 10)
        with self.assertRaisesRegex(ValueError, r'^From 1013 hPa is outside 0 to 100 hPa.*Use Now'):
            control._build_set_rows(1013.0, 50.0, 600, start)
        rows, _ = control._build_set_rows(1013.0, 50.0, 0, start)
        self.assertEqual([row['P (hPa)'] for row in rows], ['50.0'])


class RemoteGateTests(unittest.TestCase):
    """RUN on with REMOTE off: the device would not take a value."""

    def setUp(self):
        self.base = datetime.now().replace(microsecond=0) + timedelta(seconds=60)
        self.runner = control.ScheduleController(control.send_row, control.send_value)
        self.addCleanup(self.runner.stop)
        self.client = control.app.test_client()
        for name, value in {'scheduler': self.runner, 'send_command': None,
                            'get_device_status': None}.items():
            patcher = patch.object(control, name, value) if value else patch.object(control, name)
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            setattr(self, name, mock)
        self.get_device_status.return_value = {'status': 1, 'running': True, 'remote': False, 'pressure_value': 50.0}

    def flashes(self):
        with self.client.session_transaction() as session:
            return session.pop('_flashes', [])

    def test_go_and_now_are_refused(self):
        self.runner.load(control.Path('a.csv'), rows_from(self.base, '10'))
        self.client.post('/schedule/start', data={'revision': self.runner.revision})
        self.assertIn('REMOTE', self.flashes()[0][1])
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.client.post('/schedule/set', data={'to': '60'})
        self.assertIn('REMOTE', self.flashes()[0][1])
        self.send_command.assert_not_called()

    def test_a_running_schedule_stops_at_its_next_row_and_keeps_it(self):
        self.runner.start(control.Path('a.csv'), rows_from(self.base, '10', '20'), now=self.base)
        self.runner.tick(now=self.base)
        self.send_command.assert_not_called()
        snapshot = self.runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error'], snapshot['failed_count']),
                         ('stopped', control.REMOTE_OFF_MESSAGE, 0))
        self.assertEqual(self.runner.index, 0)

    def test_status_polling_stops_a_running_schedule(self):
        self.runner.start(control.Path('a.csv'), rows_from(self.base, '10'), now=self.base)
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        snapshot = self.runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error']), ('stopped', control.REMOTE_OFF_MESSAGE))

    def test_regulation_off_is_reported_first(self):
        self.get_device_status.return_value = {'status': 0, 'running': False, 'remote': False}
        with self.assertRaisesRegex(control.ScheduleHalted, 'Regulation is off'):
            control.send_row('10')


class StopTimingTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2030, 1, 1, 10)

    def test_stale_refusal_does_not_halt_a_newer_run(self):
        def stop_and_go_then_refuse(value, cancel_event):
            runner.stop()
            runner.resume(now=self.base)
            raise control.ScheduleHalted(control.REGULATION_OFF_MESSAGE)

        runner = control.ScheduleController(stop_and_go_then_refuse)
        runner.start(control.Path('a.csv'), rows_from(self.base, '10', '20'), now=self.base)
        runner.tick(now=self.base)
        snapshot = runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error']), ('running', None))
        self.assertEqual(runner.index, 1)

    def test_stop_interrupts_the_status_read_before_a_row(self):
        entered, seen = threading.Event(), {}

        def blocked_status(*, timeout, cancel_event=None):
            seen['cancel_event'] = cancel_event
            entered.set()
            cancel_event.wait(5)
            raise control.ScheduleCancelled()

        runner = control.ScheduleController(control.send_row)
        runner.start(control.Path('a.csv'), rows_from(self.base, '10'), now=self.base)
        with patch.object(control, 'get_device_status', side_effect=blocked_status), \
                patch.object(control, 'send_command') as send:
            worker = threading.Thread(target=runner.tick, kwargs={'now': self.base})
            worker.start()
            self.assertTrue(entered.wait(3))
            runner.stop()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        send.assert_not_called()
        snapshot = runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error'], snapshot['failed_count']), ('stopped', None, 0))

    def test_regulation_off_leaves_a_ready_schedule_ready(self):
        runner = control.ScheduleController(lambda value, cancel_event: None)
        client = control.app.test_client()
        statuses = [{'status': 9}, {'status': 8}]
        runner.load(control.Path('a.csv'), rows_from(self.base, '10'))
        with patch.object(control, 'scheduler', runner), patch.object(control, 'send_command'), \
                patch.object(control, 'get_device_status', side_effect=statuses):
            client.post('/regulation/off')
        snapshot = runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error']), ('ready', None))
        self.assertNotIn('Restart', snapshot['message'])

    def test_stop_refuses_a_go_waiting_for_the_device(self):
        runner = control.ScheduleController(lambda value, cancel_event: None)
        runner.load(control.Path('a.csv'), rows_from(self.base, '10'))
        revision = runner.revision
        self.assertFalse(runner.stop())
        with self.assertRaisesRegex(RuntimeError, 'Schedule changed'):
            runner.resume(expected_revision=revision)


class SafetyGateTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime.now().replace(microsecond=0) + timedelta(seconds=60)
        self.runner = control.ScheduleController(control.send_row, control.send_value)
        self.addCleanup(self.runner.stop)
        self.client = control.app.test_client()

    def test_alarm_blocks_start_and_regulation_on_but_allows_regulation_off(self):
        alarm_on_idle = {'status': 10, 'running': False, 'alarm': True, 'remote': True, 'pressure_value': 50.0}
        alarm_on_running = {'status': 11, 'running': True, 'alarm': True, 'remote': True, 'pressure_value': 50.0}
        alarm_cleared_off = {'status': 10, 'running': False, 'alarm': True, 'remote': True, 'pressure_value': 50.0}
        with patch.object(control, 'scheduler', self.runner), \
                patch.object(control, 'get_device_status', return_value=alarm_on_idle), \
                patch.object(control, 'send_command') as send:
            self.client.post('/regulation/on')
            with self.client.session_transaction() as session:
                flashes = session.pop('_flashes', [])
            self.assertTrue(any(cat == 'error' and 'ALARM is ON' in msg for cat, msg in flashes), flashes)
            send.assert_not_called()
        with patch.object(control, 'scheduler', self.runner), \
                patch.object(control, 'get_device_status', return_value=alarm_on_running), \
                patch.object(control, 'send_command') as send:
            self.runner.load(control.Path('a.csv'), rows_from(self.base, '45'))
            self.client.post('/schedule/start', data={'revision': self.runner.revision})
            with self.client.session_transaction() as session:
                flashes = session.pop('_flashes', [])
            self.assertTrue(any(cat == 'error' and 'ALARM is ON' in msg for cat, msg in flashes), flashes)
            send.assert_not_called()
        with patch.object(control, 'scheduler', self.runner), \
                patch.object(control, 'get_device_status', side_effect=[alarm_on_running, alarm_cleared_off]), \
                patch.object(control, 'send_command', return_value='ok') as send:
            self.client.post('/regulation/off')
            send.assert_called_once()

    def test_alarm_or_excessive_step_halts_running_schedule(self):
        self.runner.start(control.Path('a.csv'), rows_from(self.base, '10', '20'), now=self.base)
        with patch.object(control, 'PRESSURE_MAX_STEP_HPA', 5.0), \
                patch.object(control, 'get_device_status', return_value={'status': 9, 'running': True, 'remote': True, 'pressure_value': 50.0}), \
                patch.object(control, 'send_command') as send:
            self.runner.tick(now=self.base)
            send.assert_not_called()
        snapshot = self.runner.snapshot()
        self.assertEqual(snapshot['state'], 'stopped')
        self.assertIn('exceeds the 5 hPa limit', snapshot['error'])
        self.assertEqual(self.runner.index, 0)

    def test_regulation_lost_during_readback_confirmation_halts_schedule(self):
        self.runner.start(control.Path('a.csv'), rows_from(self.base, '48'), now=self.base)
        statuses = [
            {'status': 9, 'running': True, 'remote': True, 'pressure_value': 50.0},
            {'status': 8, 'running': False, 'remote': True, 'pressure_value': 48.0},
        ]
        with patch.object(control, 'get_device_status', side_effect=statuses), \
                patch.object(control, 'send_command', return_value='ok'):
            self.runner.tick(now=self.base)
        snapshot = self.runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error']), ('stopped', control.REGULATION_OFF_MESSAGE))

    def test_worker_halts_running_schedule_without_browser_polling(self):
        self.runner.start(control.Path('a.csv'), rows_from(self.base + timedelta(minutes=10), '48'), now=self.base)
        stop = threading.Event()

        def status_off(**kwargs):
            stop.set()
            return {'status': 8, 'running': False, 'remote': True, 'alarm': False, 'pressure_value': 50.0}

        with patch.object(control, 'scheduler', self.runner), \
                patch.object(control, '_last_status_check_time', return_value=0.0), \
                patch.object(control, 'get_device_status', side_effect=status_off):
            control.schedule_worker(stop)
        snapshot = self.runner.snapshot()
        self.assertEqual((snapshot['state'], snapshot['error']), ('stopped', control.REGULATION_OFF_MESSAGE))


if __name__ == '__main__':
    unittest.main()
