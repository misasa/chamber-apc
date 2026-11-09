# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime, timedelta
from pathlib import Path
import threading
import unittest
from support import control


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 10)
        self.sent = []
        self.scheduler = control.ScheduleController(lambda value, cancel_event: self.sent.append(value))

    def rows(self):
        return [{'_when': self.now + timedelta(seconds=s), '_line': str(i+2),
                 'datetime': str(self.now + timedelta(seconds=s)), 'P (hPa)': str(v)}
                for i, (s, v) in enumerate([(-1, 5), (1, 10), (2, 20), (3, 30)])]

    def test_skips_past_rows_and_sends_due_rows_once(self):
        self.scheduler.start(Path('test.csv'), self.rows(), now=self.now)
        self.scheduler.tick(now=self.now + timedelta(seconds=2))
        self.scheduler.tick(now=self.now + timedelta(seconds=2))
        self.assertEqual(self.sent, ['10', '20'])
        self.assertEqual(self.scheduler.snapshot()['pressure'], '30')
        self.scheduler.tick(now=self.now + timedelta(seconds=3))
        self.assertEqual(self.sent, ['10', '20', '30'])
        self.assertEqual(self.scheduler.snapshot()['state'], 'completed')

    def test_resume_skips_entries_that_expired_while_stopped(self):
        self.scheduler.start(Path('test.csv'), self.rows(), now=self.now)
        self.scheduler.tick(now=self.now + timedelta(seconds=1))
        self.scheduler.stop()
        self.scheduler.tick(now=self.now + timedelta(seconds=2))
        self.scheduler.resume(now=self.now + timedelta(seconds=2, milliseconds=500))
        self.scheduler.tick(now=self.now + timedelta(seconds=3))
        self.assertEqual(self.sent, ['10', '30'])

    def test_failure_is_reported_but_next_scheduled_command_still_runs(self):
        def fail_first(value, cancel_event):
            if value == '10':
                raise RuntimeError('device unavailable')
            self.sent.append(value)
        self.scheduler.sender = fail_first
        self.scheduler.start(Path('test.csv'), self.rows(), now=self.now)
        with self.assertLogs(level='ERROR') as messages:
            self.scheduler.tick(now=self.now + timedelta(seconds=1))
        self.assertIn('device unavailable', str(messages.output))
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot['state'], 'running')
        self.assertIn('device unavailable', snapshot['error'])
        self.scheduler.tick(now=self.now + timedelta(seconds=3))
        self.assertEqual(self.sent, ['20', '30'])
        self.assertEqual(self.scheduler.snapshot()['state'], 'completed')
        self.assertEqual(self.scheduler.snapshot()['failed_count'], 1)

    def test_repeated_start_while_running_does_not_reset_progress(self):
        self.scheduler.start(Path('test.csv'), self.rows(), now=self.now)
        self.scheduler.resume(now=self.now + timedelta(seconds=2))
        self.scheduler.tick(now=self.now + timedelta(seconds=2))
        self.assertEqual(self.sent, ['10', '20'])

    def test_replacement_cancels_old_request_without_stopping_new_schedule(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(value, cancel_event):
            entered.set()
            self.assertTrue(release.wait(2))
            self.assertTrue(cancel_event.is_set())
            raise control.ScheduleCancelled('cancelled')
        self.scheduler.sender = blocking
        self.scheduler.start(Path('old.csv'), self.rows(), now=self.now)
        thread = threading.Thread(target=self.scheduler.tick, kwargs={'now': self.now + timedelta(seconds=1)})
        thread.start()
        self.assertTrue(entered.wait(2))
        self.scheduler.start(Path('new.csv'), self.rows(), now=self.now)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.scheduler.snapshot()['state'], 'running')
        self.assertEqual(self.scheduler.snapshot()['name'], 'new.csv')

    def test_resume_without_loaded_csv_is_a_validation_error(self):
        with self.assertRaises(ValueError):
            self.scheduler.resume(now=self.now)
