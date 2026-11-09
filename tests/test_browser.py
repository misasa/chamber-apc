# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import redirect_stdout
import io
import os
import threading
import unittest
from unittest.mock import patch

from werkzeug.serving import make_server
from support import Clock, QuietHandler, control


class BrowserTests(unittest.TestCase):
    def test_only_browser_activity_delays_reopening_and_attempts_are_one_minute_apart(self):
        clock = Clock()
        with patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.webbrowser, 'open') as opener:
            activity = control.BrowserActivity()
            clock.now = 59
            activity.reopen_if_idle('http://127.0.0.1:5000/')
            opener.assert_not_called()
            activity.touch()
            clock.now = 118
            activity.reopen_if_idle('http://127.0.0.1:5000/')
            opener.assert_not_called()
            clock.now = 119
            activity.reopen_if_idle('http://127.0.0.1:5000/')
            opener.assert_called_once_with('http://127.0.0.1:5000/')
            clock.now = 178
            activity.reopen_if_idle('http://127.0.0.1:5000/')
            self.assertEqual(opener.call_count, 1)
            clock.now = 179
            activity.reopen_if_idle('http://127.0.0.1:5000/')
            self.assertEqual(opener.call_count, 2)

    def test_failed_device_status_read_still_counts_as_browser_activity(self):
        with patch.object(control, 'browser_activity') as activity, patch.object(control, 'get_device_status', side_effect=RuntimeError('offline')):
            response = control.app.test_client().get('/api/status')
        self.assertEqual(response.status_code, 502)
        activity.touch.assert_called_once_with()

    def test_failed_browser_launch_is_rate_limited(self):
        clock = Clock()
        with patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.webbrowser, 'open', side_effect=OSError('unavailable')) as opener:
            activity = control.BrowserActivity()
            clock.now = 60
            with self.assertLogs(level='ERROR'):
                activity.reopen_if_idle('http://127.0.0.1:5000/')
            clock.now = 61
            activity.reopen_if_idle('http://127.0.0.1:5000/')
            self.assertEqual(opener.call_count, 1)

    def test_health_probe_does_not_contact_device_or_count_as_browser_activity(self):
        with patch.object(control, 'browser_activity') as activity, patch.object(control, 'get_device_status') as read:
            response = control.app.test_client().get('/api/health')
        self.assertEqual(response.json, {'app': 'chamber-apc-control'})
        read.assert_not_called()
        activity.touch.assert_not_called()

    def test_relaunch_opens_existing_panel_without_starting_another_server(self):
        server = make_server('127.0.0.1', 0, control.app, request_handler=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for no_browser in (False, True):
                with self.subTest(no_browser=no_browser):
                    with patch.dict(os.environ, {'APC_PORT': str(server.server_port), 'APC_NO_BROWSER': '0'}), \
                         patch.object(control.sys, 'argv', ['chamber-apc-control'] + (['--no-browser'] if no_browser else [])), \
                         patch.object(control.webbrowser, 'open', return_value=True) as opener, \
                         patch.object(control.threading, 'Timer') as timer, \
                         patch.object(control.app, 'run') as run, \
                         redirect_stdout(io.StringIO()) as console:
                        control.main()
                    run.assert_not_called()
                    timer.assert_not_called()
                    if no_browser:
                        opener.assert_not_called()
                    else:
                        opener.assert_called_once_with(f'http://127.0.0.1:{server.server_port}/')
                    self.assertIn(f'Control panel: http://127.0.0.1:{server.server_port}/', console.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
