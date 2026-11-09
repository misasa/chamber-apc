# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""Messages, request protection, and JSON error responses of the control panel."""
from datetime import datetime, timedelta
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from support import set_and_start, control

REAL_GET_DEVICE_STATUS = control.get_device_status


class InterfaceTestCase(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.runner = control.ScheduleController(lambda value, cancel_event: None)
        for name, value in {'scheduler': self.runner, 'UPLOAD_DIR': Path(self.storage.name)}.items():
            patcher = patch.object(control, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        status = patch.object(control, 'get_device_status', return_value={'status': 9})
        status.start()
        self.addCleanup(status.stop)
        self.client = control.app.test_client()

    def upload(self, content, filename='test.csv'):
        if isinstance(content, str):
            content = content.encode()
        return self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(content), filename)})

    def flashes(self):
        with self.client.session_transaction() as session:
            return session.pop('_flashes', [])


class CSVMessageTests(InterfaceTestCase):
    def test_rejections_name_the_problem(self):
        cases = [(b'datetime,P (hPa)\n2030-01-01 10:00:00,12\xff\n', 'UTF-8'),
                 ('datetime,P (hPa)\nsoon,12\n', "CSV line 2: invalid datetime 'soon'"),
                 ('datetime,P (hPa)\n2030-01-01 10:00:00,high\n', "CSV line 2: Pressure must be a number in hPa (got 'high')"),
                 ('datetime,P (hPa)\n2030-01-01 10:00:00\n', "CSV line 2: Pressure must be a number in hPa (got '')"),
                 ('datetime,P (hPa)\n2030-01-01 10:00:00,1_000\n', 'CSV line 2: Pressure must be a number'),
                 ('datetime,P (hPa)\n2030-01-01 10:00:00,inf\n', 'CSV line 2: Pressure must be a finite number'),
                 ('date,pressure\n2030-01-01,12\n', 'must contain date, time, and pressure'),
                 ('date,time,pressure\n2030-01-01,10:00:00+09:00,12\n', 'local times without a timezone'),
                 ('datetime,P (hPa)\n', 'no schedule rows')]
        for content, message in cases:
            with self.subTest(message=message):
                response = self.upload(content)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json['error'])

    def test_supplied_schedule_csv_loads(self):
        rows = control.read_schedule(Path(__file__).resolve().parent.parent / 'schedule.csv')
        self.assertEqual(len(rows), 21)
        self.assertEqual((rows[0]['P (hPa)'], rows[-1]['P (hPa)']), ('50.00', '45.00'))
        self.assertEqual(rows[0]['_when'].strftime('%M:%S'), '00:00')


class ActionMessageTests(InterfaceTestCase):
    def test_empty_and_invalid_setpoints_explain_the_input(self):
        with patch.object(control, 'send_command') as send:
            set_and_start(self.client, to='')
            self.assertEqual(self.flashes(), [('error', 'Could not set schedule: Enter the To pressure in hPa.')])
            set_and_start(self.client, to='abc')
            self.assertIn('Pressure must be a number', self.flashes()[0][1])
            send.assert_not_called()

    def test_start_with_only_past_rows_does_not_claim_to_have_started(self):
        self.upload('datetime,P (hPa)\n2001-01-01 10:00:00,12\n')
        self.client.post('/schedule/start')
        flashes = self.flashes()
        self.assertEqual(flashes[0][0], 'error')
        self.assertIn('Nothing to send', flashes[0][1])
        self.assertEqual(self.runner.snapshot()['state'], 'completed')

    def test_second_start_reports_already_running(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        self.upload(f'datetime,P (hPa)\n{future},12\n')
        self.client.post('/schedule/start')
        self.flashes()
        self.client.post('/schedule/start')
        self.assertEqual(self.flashes(), [('message', 'Schedule is already running.')])

    def test_invalid_revision_is_a_clear_message(self):
        self.client.post('/schedule/start', data={'revision': 'old'})
        self.assertIn('Schedule changed', self.flashes()[0][1])

    def test_end_without_running_schedule_says_so_and_keeps_state(self):
        self.client.post('/schedule/end')
        self.assertEqual(self.flashes(), [('message', 'No schedule is running.')])
        self.assertEqual(self.runner.snapshot()['state'], 'idle')

    def test_end_after_completion_keeps_completed(self):
        self.upload('datetime,P (hPa)\n2001-01-01 10:00:00,12\n')
        self.client.post('/schedule/start')
        self.client.post('/schedule/end')
        self.assertEqual(self.runner.snapshot()['state'], 'completed')


class PageTests(InterfaceTestCase):
    def test_set_form_is_not_blocked_by_browser_step_validation(self):
        page = self.client.get('/').text
        self.assertRegex(page, r'<form[^>]*novalidate[^>]*schedule/set')
        self.assertIn('step="0.1"', page)

    def test_alarm_lamp_has_its_own_style(self):
        page = self.client.get('/').text
        self.assertIn('class="lamp alert unknown" id="status-light-3"', page)
        self.assertIn('.lamp.alert.on{background:#d13438}', page)

    def test_configured_log_limit_is_offered_and_selected(self):
        with patch.object(control, 'LOG_DISPLAY_LIMIT', 250):
            page = self.client.get('/').text
        self.assertIn('<option value="250" selected>', page)
        self.assertNotIn('<option value="100" selected>', page)

    def test_status_names_regulation_not_the_device_flag(self):
        page = self.client.get('/').text
        self.assertIn('<span>0: REGULATION</span>', page)
        self.assertNotIn('0: RUN', page)

    def test_regulation_button_uses_the_lamp_colors(self):
        page = self.client.get('/').text
        self.assertIn('.lamp.on{background:var(--lamp-on)}', page)
        self.assertIn('#regulation-toggle{background:var(--lamp-off)', page)
        self.assertIn('#regulation-toggle.on{background:#dff7e6;border-color:#00a83a;color:#00a83a}', page)
        self.assertIn('Regulation with P = <span id="regulation-p">', page)
        self.assertEqual(page.count('<button id="regulation-'), 1)

    def test_current_schedule_and_plan_are_separate_panels(self):
        page = self.client.get('/').text
        for name in ('Now', 'Ramp', 'File'):
            self.assertIn(f'> {name}</label>', page)
        self.assertIn('<h2>Schedule</h2>', page)
        self.assertNotIn('Current schedule', page)
        self.assertIn('State: <strong id="scheduler-state">idle</strong>', page)
        self.assertNotIn('Name:', page)
        self.assertLess(page.index('<h2>Pressure</h2>'), page.index('<h2>Schedule</h2>'))
        self.assertLess(page.index('<h2>Schedule</h2>'), page.index('id="schedule-start"'))
        self.assertIn('No schedule.', self.client.get('/api/schedule').json['message'])

    def test_only_the_chosen_source_shows_its_fields(self):
        page = self.client.get('/').text
        self.assertRegex(page, r'id="set-fields" >')
        self.assertRegex(page, r'id="file-fields" hidden')

    def test_go_and_stop_are_in_schedule_and_ok_is_in_pressure(self):
        page = self.client.get('/').text
        self.assertEqual(page.count('>Go</button>'), 1)
        self.assertEqual(page.count('>Stop</button>'), 1)
        self.assertEqual(page.count('>OK</button>'), 1)
        pressure, schedule = page.split('<h2>Schedule</h2>')
        self.assertIn('id="schedule-start"', schedule)
        self.assertIn('id="schedule-end"', schedule)
        self.assertRegex(schedule, r'id="schedule-actions"[^>]*hidden')
        self.assertIn('id="pressure-ok" form="pressure-form"', pressure)
        self.assertLess(pressure.index('id="csv_file"'), pressure.index('id="pressure-ok"'))
        self.assertRegex(page, r'id="source-now"[^>]*value="now"[^>]*checked')

    def test_held_file_is_the_chosen_source_after_reload(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        self.upload(f'datetime,P (hPa)\n{future},12\n')
        page = self.client.get('/').text
        self.assertRegex(page, r'id="source-file"[^>]*checked')
        self.assertRegex(self.client.get('/?to=30').text, r'id="source-now"[^>]*checked')
        self.assertRegex(self.client.get('/?to=30&ramp=600').text, r'id="source-ramp"[^>]*checked')

    def test_sources_come_before_the_schedule(self):
        page = self.client.get('/').text
        self.assertLess(page.index('id="source-file"'), page.index('<h2>Schedule</h2>'))
        self.assertLess(page.index('id="source-now"'), page.index('id="source-ramp"'))
        self.assertLess(page.index('id="source-ramp"'), page.index('id="source-file"'))

    def test_button_name_follows_the_choice_when_the_page_loads(self):
        self.assertIn('>OK</button>', self.client.get('/').text)
        ramp = self.client.get('/?to=30&ramp=300').text
        self.assertIn('>Plan</button>', ramp)
        self.assertNotIn('>OK</button>', ramp)

    def test_ramp_time_only_belongs_to_ramp(self):
        page = self.client.get('/').text
        self.assertRegex(page, r'id="ramp-field" hidden')
        self.assertRegex(page, r'id="ramp-time"[^>]*disabled')
        self.assertNotIn('<option value="0"', page)
        self.assertIn('<option value="600" selected>10 min</option>', page)
        ramp = self.client.get('/?to=30&ramp=300').text
        self.assertRegex(ramp, r'id="ramp-field" >')
        self.assertIn('<option value="300" selected>5 min</option>', ramp)

    def test_from_to_and_ramp_time_share_one_row(self):
        row = self.client.get('/').text.split('<div class="from-to">')[1].split('id="ramp-plan"')[0]
        for field in ('id="from-value"', 'id="to-value"', 'id="ramp-time"'):
            self.assertIn(field, row)

    def test_help_is_closed_and_lists_the_operating_order(self):
        page = self.client.get('/').text
        self.assertRegex(page, r'<details class="help" id="help"><summary aria-label="Help"')
        steps = ['bring the chamber pressure', 'local mode to remote mode', 'set the initial pressure',
                 'Start regulation', 'plan the schedule', 'Start the schedule']
        positions = [page.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        for name in ('`Pressure`', '`Regulation`', '`Schedule`', '`Go`'):
            self.assertIn(name, page.split('<div class="help-panel">')[1].split('</details>')[0])

    def test_status_bits_are_laid_out_in_one_row(self):
        page = self.client.get('/').text
        self.assertIn('grid-template-columns:repeat(4,auto)', page)
        self.assertEqual(page.count('class="indicator"'), 5)
        self.assertIn('<span>SCHEDULE</span>', page)
        self.assertIn('class="lamp off" id="status-light-schedule"', page)

    def test_device_url_is_shown(self):
        self.assertIn(control.DEVICE_URL, self.client.get('/').text)

    def test_user_value_is_escaped(self):
        page = self.client.get('/', query_string={'to': '"><script>x</script>'}).text
        self.assertNotIn('"><script>x</script>', page)


class RequestProtectionTests(InterfaceTestCase):
    def test_cross_site_post_cannot_send_commands(self):
        with patch.object(control, 'send_command') as send:
            for origin in ('https://evil.example', 'null'):
                for path in ('/regulation/on', '/schedule/set', '/schedule/start', '/schedule/end', '/schedule/upload'):
                    with self.subTest(origin=origin, path=path):
                        response = self.client.post(path, data={'to': '12'}, headers={'Origin': origin})
                        self.assertEqual(response.status_code, 403)
            send.assert_not_called()

    def test_same_origin_post_is_allowed(self):
        response = self.client.post('/schedule/end', headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 302)

    def test_cross_site_sec_fetch_site_is_refused_without_origin(self):
        for site in ('cross-site', 'same-site'):
            with self.subTest(site=site):
                response = self.client.post('/schedule/end', headers={'Sec-Fetch-Site': site})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post('/schedule/end', headers={'Sec-Fetch-Site': 'same-origin'}).status_code, 302)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.client.get('/', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/api/status', headers={'Host': 'evil.example:5000'}).status_code, 403)

    def test_local_host_names_are_allowed(self):
        for host in ('127.0.0.1:5000', 'localhost:5000', '[::1]:5000'):
            with self.subTest(host=host):
                self.assertEqual(self.client.get('/', headers={'Host': host}).status_code, 200)


class ErrorResponseTests(InterfaceTestCase):
    def test_oversized_upload_returns_json(self):
        with patch.dict(control.app.config, {'MAX_CONTENT_LENGTH': 100}):
            response = self.upload('x' * 1000)
        self.assertEqual(response.status_code, 413)
        self.assertIn('MiB limit', response.json['error'])

    def test_api_not_found_and_internal_errors_return_json(self):
        self.assertEqual(self.client.get('/api/unknown').status_code, 404)
        self.assertIn('error', self.client.get('/api/unknown').json)
        with patch.object(self.runner, 'snapshot', side_effect=KeyError('boom')), self.assertLogs(level='ERROR'):
            response = self.client.get('/api/schedule')
        self.assertEqual(response.status_code, 500)
        self.assertIn('Internal error', response.json['error'])

    def test_status_with_huge_setpoint_is_a_clear_error(self):
        for number in ('1e999', '1' + '0' * 400):
            with self.subTest(number=number[:8]), patch.object(control, 'get_device_status', REAL_GET_DEVICE_STATUS), patch.object(
                    control, '_request_device', return_value='{"APC_STATUS_PV": 1, "APC_P_SV": ' + number + '}'):
                with self.assertRaisesRegex(ValueError, 'finite'):
                    control.get_device_status()


class ScheduleBacklogTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2030, 1, 1, 10)
        self.sent = []
        self.scheduler = control.ScheduleController(lambda value, cancel_event: self.sent.append(value))

    def rows(self, seconds):
        return [{'_when': self.now + timedelta(seconds=s), '_line': str(i + 2), 'P (hPa)': str(i)}
                for i, s in enumerate(seconds)]

    def test_waking_from_sleep_does_not_replay_the_backlog(self):
        self.scheduler.start(Path('t.csv'), self.rows(range(0, 7200, 30)), now=self.now)
        with self.assertLogs(level='WARNING') as logs:
            self.scheduler.tick(now=self.now + timedelta(seconds=3600))
        # Rows less than 60 seconds late still run, in order.
        self.assertEqual(self.sent, ['118', '119', '120'])
        self.assertEqual(self.scheduler.snapshot()['skipped_count'], 118)
        self.assertIn('Skipped 118 late schedule rows', logs.output[0])
        self.assertEqual(self.scheduler.snapshot()['state'], 'running')

    def test_newest_due_row_is_kept_even_when_very_late(self):
        self.scheduler.start(Path('t.csv'), self.rows([0, 7200]), now=self.now)
        self.scheduler.tick(now=self.now + timedelta(seconds=3600))
        self.assertEqual(self.sent, ['0'])
        self.assertEqual(self.scheduler.snapshot()['skipped_count'], 0)

    def test_final_row_runs_after_skipping(self):
        self.scheduler.start(Path('t.csv'), self.rows([0, 30, 60]), now=self.now)
        self.scheduler.tick(now=self.now + timedelta(seconds=3600))
        self.assertEqual(self.sent, ['2'])
        self.assertEqual(self.scheduler.snapshot()['state'], 'completed')

    def test_loading_a_new_csv_clears_the_skip_count(self):
        self.scheduler.start(Path('t.csv'), self.rows([0, 30, 60]), now=self.now)
        self.scheduler.tick(now=self.now + timedelta(seconds=3600))
        self.scheduler.load(Path('n.csv'), self.rows([0]))
        self.assertEqual(self.scheduler.snapshot()['skipped_count'], 0)


class WorkerTests(unittest.TestCase):
    def test_worker_survives_unexpected_errors_and_reports_them(self):
        runner = control.ScheduleController(lambda value, cancel_event: None)
        stop = threading.Event()
        calls = []

        def tick():
            calls.append(1)
            if len(calls) == 1:
                raise KeyError('boom')
            stop.set()

        with patch.object(control, 'scheduler', runner), patch.object(runner, 'tick', tick), self.assertLogs(level='ERROR'):
            thread = threading.Thread(target=control.schedule_worker, args=(stop,))
            thread.start()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(calls), 2)
        self.assertIn('boom', runner.snapshot()['error'])

    def test_confirmation_limit_stays_ten_seconds(self):
        self.assertEqual(control.CONFIRM_TIMEOUT, 10.0)
