# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import atexit
import importlib
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

from werkzeug.serving import WSGIRequestHandler, make_server

# HTTP tests confirm over this period. The fake-clock tests always check the
# real 10 second limit; APC_TEST_REALTIME=1 also waits 10 seconds over HTTP.
REALTIME = os.environ.get('APC_TEST_REALTIME') == '1'
CONFIRM_SECONDS = 10.0 if REALTIME else 1.0

# Isolate the control module's import-time log files from the user's real logs.
_storage = tempfile.TemporaryDirectory(prefix='apc-tests-')
_test_environment = {'APC_DATA_DIR': _storage.name, 'DEVICE_BASE_URL': 'http://127.0.0.1:1/WeatherMonitor/',
                     'APP_DEBUG': '', 'LOG_DISPLAY_LIMIT': '100'}
with patch.dict(os.environ, _test_environment), patch.object(sys, 'argv', [sys.argv[0]]):
    control = importlib.import_module('chamber_apc_control.control')
mock_device = importlib.import_module('chamber_apc_control.mock_device')


def _cleanup():
    # Windows cannot remove a directory containing an open log file.
    logging.shutdown()
    _storage.cleanup()


atexit.register(_cleanup)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args):
        pass

    def log_error(self, *args):
        pass


def set_and_start(client, **form):
    """Press OK in Pressure, then Start in Schedule if OK set a schedule."""
    response = client.post('/schedule/set', data=form)
    with client.session_transaction() as session:
        flashes = session.get('_flashes', [])
        was_set = bool(flashes) and flashes[-1][1].startswith('Schedule set:')
    return client.post('/schedule/start') if was_set else response


class MockDeviceMixin:
    """Serve the mock over real HTTP and point the control panel at it."""

    def start_mock_device(self):
        self.device = mock_device.app.test_client()
        self.device.post('/mock/reset')
        self.addCleanup(self.device.post, '/mock/reset')
        self.server = make_server('127.0.0.1', 0, mock_device.app, threaded=True, request_handler=QuietHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server_thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        base = f'http://127.0.0.1:{self.server.server_port}/WeatherMonitor/'
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.runner = control.ScheduleController(control.send_row, control.send_value)
        self.addCleanup(self.runner.stop)
        for name, value in {'DEVICE_URL': base, 'START_URL': base + 'Set_Data?APC_RUN_SV=1',
                            'STOP_URL': base + 'Set_Data?APC_STOP_SV=1', 'UPLOAD_DIR': Path(self.storage.name),
                            'CONFIRM_TIMEOUT': CONFIRM_SECONDS, 'scheduler': self.runner}.items():
            patcher = patch.object(control, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = control.app.test_client()

    def configure_mock(self, **settings):
        response = self.device.post('/mock/config', json=settings)
        self.assertEqual(response.status_code, 200, response.text)

    def set_mock_status(self, value):
        self.device.get('/WeatherMonitor/Set_Mock_Status', query_string={'APC_STATUS_PV': value})

    def flashes(self):
        with self.client.session_transaction() as session:
            return session.pop('_flashes', [])
