# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime, timedelta
from html.parser import HTMLParser
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from support import control


class ButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.label = ''
    def handle_starttag(self, tag, attrs):
        if tag == 'button' and dict(attrs).get('id') == 'schedule-start':
            self.active = True
    def handle_data(self, data):
        if self.active:
            self.label += data
    def handle_endtag(self, tag):
        if tag == 'button':
            self.active = False


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.runner = control.ScheduleController(lambda value, cancel_event: self.sent.append(value))
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        for name, value in [('scheduler', self.runner), ('UPLOAD_DIR', Path(self.storage.name))]:
            patcher = patch.object(control, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        status_patch = patch.object(control, 'get_device_status', return_value={'status': 9})
        status_patch.start()
        self.addCleanup(status_patch.stop)
        self.client = control.app.test_client()
        self.base = datetime.now() + timedelta(seconds=60)

    def upload(self, filename='cycle.csv'):
        text = 'datetime,P (hPa)\n' + ''.join(f'{(self.base+timedelta(seconds=s)).isoformat()},{v}\n' for s,v in [(0,10),(1,20),(2,30)])
        self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(text.encode()), filename)})

    def label(self):
        parser = ButtonParser()
        parser.feed(self.client.get('/').text)
        return parser.label.strip()

    def test_end_keeps_filename_and_changes_start_to_restart(self):
        self.assertEqual(self.label(), 'Go')
        self.upload()
        self.assertEqual(self.label(), 'Go')
        self.client.post('/schedule/start')
        self.client.post('/schedule/end')
        self.assertEqual(self.label(), 'Restart')
        self.assertEqual(self.client.get('/api/schedule').json['name'], 'cycle.csv')
        self.assertIn('Restart', self.client.get('/api/schedule').json['message'])

    def test_restart_reuses_memory_skips_expired_rows_and_never_replays_sent_row(self):
        self.upload()
        self.client.post('/schedule/start')
        self.runner.tick(now=self.base)
        self.client.post('/schedule/end')
        with patch.object(control, 'datetime', wraps=datetime) as clock, patch.object(control, 'read_schedule', side_effect=AssertionError('must not reload file')):
            clock.now.return_value = self.base + timedelta(seconds=1, milliseconds=500)
            self.assertEqual(self.client.post('/schedule/start').status_code, 302)
        self.assertEqual(self.label(), 'Go')
        self.runner.tick(now=self.base + timedelta(seconds=2))
        self.assertEqual(self.sent, ['10', '30'])
        self.assertEqual(self.runner.snapshot()['state'], 'completed')
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)

    def test_new_upload_replaces_stopped_schedule(self):
        self.upload()
        self.client.post('/schedule/start')
        self.client.post('/schedule/end')
        self.upload('new.csv')
        self.assertEqual(self.label(), 'Go')
        self.assertEqual(self.runner.snapshot()['name'], 'new.csv')
        self.assertEqual(self.runner.snapshot()['state'], 'ready')


    def test_start_rejects_run_off_without_losing_loaded_csv(self):
        with patch.object(control, 'get_device_status', return_value={'status': 8}):
            self.upload()
            self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)
        with self.client.session_transaction() as session:
            self.assertTrue(any(category == 'error' and 'Regulation is off' in text for category, text in session['_flashes']))

    def test_restart_rechecks_run_and_keeps_stopped_schedule_when_off(self):
        self.upload()
        self.client.post('/schedule/start')
        self.client.post('/schedule/end')
        before = self.runner.snapshot()
        with patch.object(control, 'get_device_status', return_value={'status': 8}):
            self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot(), before)
        self.assertEqual(self.label(), 'Restart')
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)

    def test_unavailable_status_prevents_start_and_restart(self):
        with patch.object(control, 'get_device_status', side_effect=RuntimeError('device unavailable')):
            self.upload()
            self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.client.post('/schedule/start')
        self.client.post('/schedule/end')
        before = self.runner.snapshot()
        with patch.object(control, 'get_device_status', side_effect=RuntimeError('device unavailable')):
            self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot(), before)

    def test_run_is_checked_at_start_after_csv_loading(self):
        state = {'status': 9}
        original_read = control.read_schedule
        def load_then_stop(path):
            rows = original_read(path)
            state['status'] = 8
            return rows
        with patch.object(control, 'get_device_status', side_effect=lambda **kwargs: dict(state)), patch.object(control, 'read_schedule', side_effect=load_then_stop):
            self.upload()
            self.client.post('/schedule/start')
        self.assertEqual(self.runner.snapshot()['state'], 'ready')
        self.assertEqual(len(list(Path(self.storage.name).iterdir())), 1)
