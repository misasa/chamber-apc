# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the installed control panel and mock device commands."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import socket
import subprocess
import sysconfig
import tempfile
import time
import unittest

import httpx


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class ProcessTests(unittest.TestCase):
    def start(self, entry_point, environment, *args):
        extension = '.exe' if os.name == 'nt' else ''
        launcher = Path(sysconfig.get_path('scripts')) / (entry_point + extension)
        self.assertTrue(launcher.is_file(), f'Installed launcher missing: {launcher}')
        output = tempfile.TemporaryFile()
        self.addCleanup(output.close)
        process = subprocess.Popen([str(launcher), *args], cwd=self.storage.name,
                                   env={**os.environ, **environment}, stdout=output, stderr=subprocess.STDOUT)
        def stop():
            if os.name == 'nt' and process.poll() is None:
                # Windows console launchers start a child Python process.
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if os.name == 'nt':
                # taskkill can return before child working-directory handles close.
                time.sleep(0.1)
        self.addCleanup(stop)
        self.outputs[entry_point] = (process, output)
        return process

    def wait_until(self, condition, seconds, message):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if condition():
                    return
            except httpx.TransportError:
                pass
            for script, (process, output) in self.outputs.items():
                if process.poll() is not None:
                    output.seek(0)
                    self.fail(f'{script} exited: {output.read().decode(errors="replace")}')
            time.sleep(0.05)
        self.fail(message)

    def test_real_processes_load_and_run_a_schedule(self):
        with tempfile.TemporaryDirectory(prefix='apc-process-test-') as directory:
            self.storage = type('Storage', (), {'name': directory})
            self.outputs = {}
            device_port, panel_port = free_port(), free_port()
            device_url = f'http://127.0.0.1:{device_port}'
            panel_url = f'http://127.0.0.1:{panel_port}'
            self.start('chamber-apc-mock', {}, '--port', str(device_port))
            self.start('chamber-apc-control',
                       {'APC_PORT': str(panel_port), 'APC_NO_BROWSER': '1', 'APC_DATA_DIR': directory,
                        'DEVICE_BASE_URL': f'{device_url}/WeatherMonitor/'})
            with httpx.Client(trust_env=False, timeout=15) as client:
                self.wait_until(lambda: client.get(f'{device_url}/mock/state').status_code == 200, 15, 'mock did not start')
                self.wait_until(lambda: client.get(f'{panel_url}/').status_code == 200, 15, 'panel did not start')
                client.get(f'{device_url}/WeatherMonitor/Set_Mock_Status', params={'APC_STATUS_PV': 8})
                self.assertEqual(client.post(f'{panel_url}/regulation/on').status_code, 302)
                self.assertTrue(client.get(f'{panel_url}/api/status').json()['running'])
                due = datetime.now() + timedelta(seconds=2)
                csv = f'datetime,P (hPa)\n{due.isoformat()},7.5\n'
                response = client.post(f'{panel_url}/schedule/upload', files={'csv_file': ('run.csv', csv.encode(), 'text/csv')})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['state'], 'ready')
                self.assertEqual(client.post(f'{panel_url}/schedule/start').status_code, 302)
                self.wait_until(lambda: client.get(f'{panel_url}/api/schedule').json()['state'] == 'completed',
                                10, 'worker thread did not run the schedule')
                self.assertEqual(client.get(f'{panel_url}/api/status').json()['pressure_value'], 7.5)
                self.assertEqual(client.get(f'{panel_url}/api/schedule').json()['failed_count'], 0)
            output = self.outputs['chamber-apc-control'][1]
            output.seek(0)
            console = output.read().decode(errors='replace')
            self.doCleanups()
            self.assertIn(f'Control panel: {panel_url}/', console)
            self.assertNotIn('Serving Flask app', console)
            self.assertIn(f'Command: POST {device_url}/WeatherMonitor/Set_Data?APC_RUN_SV=1', console)
            self.assertIn(f'Command: POST {device_url}/WeatherMonitor/Set_Data?APC_P_SV=7.5', console)
            self.assertNotIn('Command: GET', console)
            self.assertTrue((Path(directory) / 'chamber-apc-control.log').exists())
