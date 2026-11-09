# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime, timedelta
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import control


class CSVLoadingTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.runner = control.ScheduleController(lambda value, cancel_event: self.sent.append(value))
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        for name, value in [('scheduler', self.runner), ('UPLOAD_DIR', Path(self.storage.name))]:
            patcher = patch.object(control, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        status_patch = patch.object(control, 'get_device_status', return_value={'status': 8})
        self.device_status = status_patch.start()
        self.addCleanup(status_patch.stop)
        self.client = control.app.test_client()
        self.base = datetime.now() + timedelta(seconds=60)

    def upload(self, filename='cycle.csv', text=None):
        if text is None:
            text = f'datetime,P (hPa)\n{self.base.isoformat()},12.5\n{(self.base + timedelta(seconds=2)).isoformat()},11.5\n'
        return self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(text.encode()), filename)})

    def test_loading_updates_filename_and_preview_without_starting_or_sending(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state'], 'ready')
        self.assertEqual(response.json['name'], 'cycle.csv')
        self.assertEqual(response.json['pressure'], '12.5')
        self.assertIn('cycle.csv', self.client.get('/').text)
        self.runner.tick(now=self.base + timedelta(seconds=3))
        self.assertEqual(self.sent, [])

    def test_loading_does_not_need_a_reachable_device(self):
        self.device_status.side_effect = RuntimeError('device unavailable')
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.runner.snapshot()['state'], 'ready')

    def test_start_rechecks_run_and_keeps_loaded_csv_when_run_is_off(self):
        self.upload()
        before = self.runner.snapshot()
        response = self.client.post('/schedule/start')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.runner.snapshot(), before)
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, [])
        self.device_status.return_value = {'status': 9}
        self.client.post('/schedule/start')
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, ['12.5'])

    def test_start_uses_validated_rows_and_skips_entries_expired_since_loading(self):
        self.upload()
        self.device_status.return_value = {'status': 9}
        with patch.object(control, 'datetime', wraps=datetime) as clock, patch.object(
            control, 'read_schedule', side_effect=AssertionError('must not reload CSV at Start')
        ):
            clock.now.return_value = self.base + timedelta(seconds=1)
            self.client.post('/schedule/start')
        self.runner.tick(now=self.base + timedelta(seconds=2))
        self.assertEqual(self.sent, ['11.5'])

    def test_invalid_upload_preserves_loaded_csv_and_removes_rejected_file(self):
        self.upload()
        before = self.runner.snapshot()
        response = self.upload('bad.csv', 'datetime,P (hPa)\nbad,12\n')
        self.assertEqual(response.status_code, 400)
        self.assertIn('CSV line', response.json['error'])
        self.assertEqual(self.runner.snapshot(), before)
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)

    def test_missing_file_and_wrong_extension_do_not_replace_loaded_csv(self):
        self.upload()
        before = self.runner.snapshot()
        self.assertEqual(self.client.post('/schedule/upload').status_code, 400)
        self.assertEqual(self.upload('bad.txt').status_code, 400)
        self.assertEqual(self.runner.snapshot(), before)
        self.assertEqual(self.sent, [])

    def test_loading_another_csv_cannot_replace_a_running_schedule(self):
        self.upload()
        self.device_status.return_value = {'status': 9}
        self.client.post('/schedule/start')
        before = self.runner.snapshot()
        response = self.upload('replacement.csv')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.runner.snapshot(), before)
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, ['12.5'])

    def test_new_upload_after_end_is_ready_until_started(self):
        self.upload()
        self.device_status.return_value = {'status': 9}
        self.client.post('/schedule/start')
        self.client.post('/schedule/end')
        response = self.upload('replacement.csv')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.assertEqual(self.runner.snapshot()['name'], 'replacement.csv')
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, [])

    def start_while(self, change):
        entered, release = threading.Event(), threading.Event()

        def wait_for_run(**kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test did not release RUN response')
            return {'status': 9}

        self.device_status.side_effect = wait_for_run
        thread = threading.Thread(target=lambda: control.app.test_client().post('/schedule/start'))
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            change()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_start_waiting_for_run_cannot_activate_a_newly_loaded_csv(self):
        self.upload()
        self.start_while(lambda: self.upload('replacement.csv'))
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.assertEqual(self.runner.snapshot()['name'], 'replacement.csv')

    def test_end_cancels_start_waiting_for_run(self):
        self.upload()
        self.start_while(lambda: self.client.post('/schedule/end'))
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.runner.snapshot()['state'], 'ready')

    def test_start_rejects_a_stale_browser_selection(self):
        self.upload()
        revision = self.runner.snapshot().get('revision', 0)
        self.upload('replacement.csv')
        self.device_status.return_value = {'status': 9}
        self.client.post('/schedule/start', data={'revision': revision})
        self.runner.tick(now=self.base)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
