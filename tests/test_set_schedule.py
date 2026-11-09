# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""Set creates an ordinary schedule with no file: from one row to a ramp."""
from datetime import datetime, timedelta
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from support import set_and_start, MockDeviceMixin, control


class RampPlanTests(unittest.TestCase):
    def test_ten_minutes_changes_every_ten_seconds_from_current_to_target(self):
        plan = control.plan_ramp(50.0, 20.0, 600, 10)
        self.assertEqual(len(plan), 60)
        self.assertEqual([offset for offset, _ in plan], list(range(10, 610, 10)))
        self.assertEqual(plan[0][1], '49.5')
        self.assertEqual(plan[29][1], '35.0')
        self.assertEqual(plan[-1], (600, '20.0'))

    def test_rising_ramp(self):
        self.assertEqual(control.plan_ramp(10.0, 13.0, 30, 10), [(10, '11.0'), (20, '12.0'), (30, '13.0')])

    def test_small_change_does_not_repeat_equal_values(self):
        plan = control.plan_ramp(10.0, 10.02, 600, 10)
        self.assertEqual([value for _, value in plan], ['10.01', '10.02'])
        self.assertEqual(plan[-1][0], 600)

    def test_duration_not_multiple_of_step_ends_on_time(self):
        self.assertEqual(control.plan_ramp(0.0, 25.0, 25, 10), [(10, '8.33'), (20, '16.67'), (25, '25.0')])

    def test_final_row_keeps_entered_precision(self):
        self.assertEqual(control.plan_ramp(0.0, 12.345, 10, 10), [(10, '12.345')])


class SetScheduleTests(MockDeviceMixin, unittest.TestCase):
    def setUp(self):
        self.start_mock_device()
        self.set_mock_status(9)

    def post(self, value, seconds):
        return set_and_start(self.client, to=value, ramp_seconds=seconds)

    def commands(self):
        return [c['parameters'].get('APC_P_SV') for c in self.device.get('/mock/history').json['commands']]

    def schedule(self):
        return self.client.get('/api/schedule').json

    def last_due(self):
        return self.runner.rows[-1]['_when']

    def upload_future_csv(self, name='a.csv'):
        due = (datetime.now() + timedelta(hours=1)).isoformat()
        return self.client.post('/schedule/upload', data={'csv_file': (io.BytesIO(f'datetime,P (hPa)\n{due},10\n'.encode()), name)})

    def test_set_becomes_a_running_schedule_without_a_file(self):
        before = datetime.now()
        self.post('30', '600')
        name = 'from 50 hPa to 30 hPa over 10 min with 60 steps'
        self.assertEqual(self.flashes()[-1], ('message', f'Schedule started: {name}'))
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['name'], schedule['source']), ('running', name, 'set'))
        self.assertEqual((schedule['position'], schedule['total_rows'], schedule['pressure']), (1, 60, '49.67'))
        self.assertLessEqual(abs((self.last_due() - before).total_seconds() - 600), 2)
        self.assertEqual(list(Path(self.storage.name).iterdir()), [])
        self.assertEqual(self.runner.path, None)

    def test_csv_export_can_be_read_back_as_the_same_rows_for_both_sources(self):
        self.post('44', '30')
        response = self.client.get('/schedule/csv')
        self.assertEqual(response.mimetype, 'text/plain')
        self.assertEqual(response.headers['Content-Disposition'], "inline; filename*=UTF-8''schedule.csv")
        rows = control.parse_schedule(io.StringIO(response.text, newline=''))
        self.assertEqual([(r['_when'], r['P (hPa)']) for r in rows], [(r['_when'], r['P (hPa)']) for r in self.runner.rows])
        self.client.post('/schedule/end')
        self.upload_future_csv('cycle.csv')
        response = self.client.get('/schedule/csv')
        self.assertIn('cycle.csv', response.headers['Content-Disposition'])
        self.assertTrue(response.text.startswith('datetime, P (hPa)\n'))
        self.assertEqual(self.schedule()['source'], 'file')

    def test_one_row_is_due_immediately_and_the_worker_path_sends_it(self):
        self.post('30', '0')
        self.assertEqual(self.flashes()[-1], ('message', 'Schedule started: from 50 hPa to 30 hPa'))
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['total_rows']), ('running', 1))
        self.runner.tick()
        self.assertEqual(self.commands(), ['30.0'])
        self.assertEqual(self.schedule()['state'], 'completed')

    def test_rows_are_sent_and_confirmed_like_any_schedule(self):
        self.post('44', '30')
        self.runner.tick(now=self.last_due())
        self.assertEqual(self.commands(), ['48.0', '46.0', '44.0'])
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['failed_count']), ('completed', 0))
        self.assertEqual(self.client.get('/api/status').json['pressure_value'], 44.0)

    def test_kind_uses_the_words_of_the_pressure_panel(self):
        self.assertIsNone(self.schedule()['kind'])
        self.upload_future_csv()
        self.assertEqual(self.schedule()['kind'], 'file')
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        self.assertEqual(self.schedule()['kind'], 'ramp')
        self.client.post('/schedule/set', data={'to': '40'})
        self.assertEqual(self.schedule()['kind'], 'now')

    def test_rows_api_returns_every_row_and_the_marker_follows_the_state(self):
        self.assertEqual(self.client.get('/api/schedule/rows').json['rows'], [])
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        rows = self.client.get('/api/schedule/rows').json
        self.assertEqual(rows['revision'], self.schedule()['revision'])
        self.assertEqual(len(rows['rows']), 60)
        self.assertEqual(rows['rows'][-1][1], 30.0)
        self.assertEqual(self.schedule()['marker_time'], rows['rows'][0][0])
        self.client.post('/schedule/start', data={'revision': self.schedule()['revision']})
        rows = self.client.get('/api/schedule/rows').json['rows']
        self.assertEqual(self.schedule()['state'], 'running')
        self.assertEqual(self.schedule()['marker_time'], rows[0][0])  # Before the first step is due.
        self.runner.tick(now=datetime.now() + timedelta(seconds=25))
        self.client.post('/schedule/end')
        schedule = self.schedule()
        self.assertEqual(schedule['state'], 'stopped')
        self.assertEqual(schedule['marker_time'], rows[1][0])

    def test_ok_only_sets_the_schedule_and_sends_nothing(self):
        self.set_mock_status(8)
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        self.assertEqual(self.flashes()[-1][1], 'Schedule set: from 50 hPa to 30 hPa over 10 min with 60 steps. Press Go to begin.')
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['source'], schedule['total_rows']), ('ready', 'set', 60))
        self.assertNotIn('datetime', schedule)
        self.assertEqual(schedule['message'], '')
        self.runner.tick(now=datetime.now() + timedelta(hours=1))
        self.assertEqual(self.commands(), [])

    def test_start_builds_the_steps_from_the_setpoint_at_that_moment(self):
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        self.device.post('/WeatherMonitor/Set_Data', query_string={'APC_P_SV': '40'})
        before = datetime.now().replace(microsecond=0)
        self.client.post('/schedule/start')
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['name']), ('running', 'from 40 hPa to 30 hPa over 10 min with 60 steps'))
        self.assertGreaterEqual((self.runner.rows[0]['_when'] - before).total_seconds(), 10)

    def test_ok_runs_now_at_once_as_one_row(self):
        self.set_mock_status(8)
        self.client.post('/schedule/set', data={'to': '30'})
        self.assertEqual(self.flashes()[-1], ('message', 'Schedule started: from 50 hPa to 30 hPa'))
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['total_rows'], schedule['needs_regulation']), ('running', 1, False))
        self.runner.tick()
        self.assertEqual(self.commands(), ['30.0'])
        self.assertEqual(self.schedule()['state'], 'completed')

    def test_now_is_refused_without_remote_and_keeps_what_is_set(self):
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        before = self.schedule()
        self.set_mock_status(0)
        self.client.post('/schedule/set', data={'to': '40'})
        self.assertIn('REMOTE flag is off', self.flashes()[-1][1])
        self.assertEqual(self.schedule(), before)
        self.assertEqual(self.commands(), [])

    def test_a_running_ramp_stays_chosen_on_the_page(self):
        self.post('30', '300')
        page = self.client.get('/').text
        self.assertRegex(page, r'id="source-ramp"[^>]*checked')
        self.assertIn('<option value="300" selected>5 min</option>', page)

    def test_start_with_a_ramp_needs_regulation_and_keeps_what_is_set(self):
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        before = self.schedule()
        self.set_mock_status(8)
        self.client.post('/schedule/start')
        self.assertIn('Regulation is off', self.flashes()[-1][1])
        self.assertEqual(self.schedule(), before)

    def test_ramp_replaces_a_loaded_schedule_that_is_not_running(self):
        self.upload_future_csv()
        self.post('30', '600')
        self.assertEqual(self.schedule()['name'], 'from 50 hPa to 30 hPa over 10 min with 60 steps')

    def test_ramp_is_refused_while_a_schedule_runs_and_csv_is_refused_during_a_ramp(self):
        self.post('30', '600')
        self.flashes()
        before = self.schedule()
        self.post('20', '600')
        self.assertIn('Stop the running schedule', self.flashes()[-1][1])
        self.assertEqual(self.upload_future_csv().status_code, 409)
        self.assertEqual(self.schedule(), before)
        self.assertEqual(list(Path(self.storage.name).iterdir()), [])

    def test_end_and_restart_work_on_a_ramp(self):
        self.post('44', '30')
        rows = self.runner.rows
        self.runner.tick(now=rows[0]['_when'])
        self.flashes()
        self.client.post('/schedule/end')
        self.assertEqual(self.flashes()[-1][1].split('.')[0], 'Schedule stopped')
        self.assertEqual(self.schedule()['state'], 'stopped')
        self.runner.tick(now=rows[1]['_when'])
        self.assertEqual(self.commands(), ['48.0'])
        self.client.post('/schedule/start')
        self.assertEqual(self.schedule()['state'], 'running')
        self.runner.tick(now=self.runner.rows[-1]['_when'])
        # Go builds the steps again from where the setpoint stopped, with times counted from Go.
        self.assertEqual(self.commands(), ['48.0', '46.67', '45.33', '44.0'])

    def test_failed_row_counts_as_a_schedule_failure_and_next_row_runs(self):
        self.post('44', '30')
        self.configure_mock(mode='http_error', fail_count=2)
        self.runner.tick(now=self.last_due())
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['failed_count']), ('completed', 1))
        self.assertEqual(self.commands(), ['46.0', '44.0'])

    def test_one_row_set_is_refused_while_a_schedule_runs(self):
        self.post('30', '600')
        self.flashes()
        before = self.schedule()
        self.post('40', '0')
        self.assertIn('Stop the running schedule', self.flashes()[-1][1])
        self.assertEqual(self.schedule(), before)
        self.assertEqual(self.commands(), [])

    def test_invalid_requests_send_nothing_and_load_nothing(self):
        for value, seconds, message in (('30', 'soon', 'whole number'), ('30', '-10', 'Ramp time'), ('30', '5', 'Ramp time'),
                                        ('30', '999999', 'Ramp time'), ('abc', '600', 'Pressure must be'), ('', '600', 'Enter the To')):
            with self.subTest(value=value, seconds=seconds):
                self.post(value, seconds)
                self.assertIn(message, self.flashes()[-1][1])
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.schedule()['state'], 'idle')

    def test_unreadable_setpoint_prevents_ramp(self):
        self.configure_mock(mode='no_setpoint')
        self.post('30', '600')
        self.assertIn('did not report the current APC_P_SV', self.flashes()[-1][1])
        self.configure_mock(mode='http_error')
        self.post('30', '600')
        self.assertIn('HTTP status: 503', self.flashes()[-1][1])
        self.assertEqual(self.schedule()['state'], 'idle')

    def test_target_equal_to_current_needs_no_schedule(self):
        self.post('50', '600')
        self.assertIn('No schedule is needed', self.flashes()[-1][1])
        self.assertEqual(self.schedule()['state'], 'idle')

    def test_ramp_needs_regulation(self):
        self.set_mock_status(8)
        self.post('30', '600')
        self.assertIn('Regulation is off', self.flashes()[-1][1])
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.schedule()['state'], 'ready')

    def test_zero_seconds_sets_the_setpoint_before_regulation(self):
        self.set_mock_status(8)
        self.post('30', '0')
        self.client.get('/api/status')
        self.assertEqual(self.schedule()['state'], 'running')
        self.runner.tick()
        schedule = self.schedule()
        self.assertEqual((schedule['state'], schedule['failed_count'], schedule['error']), ('completed', 0, None))
        self.assertEqual(self.commands(), ['30.0'])

    def test_zero_seconds_without_remote_is_refused(self):
        self.set_mock_status(0)
        self.post('30', '0')
        self.assertIn('REMOTE flag is off', self.flashes()[-1][1])
        self.assertEqual(self.commands(), [])

    def test_file_after_a_setpoint_still_needs_regulation(self):
        self.set_mock_status(8)
        self.post('30', '0')
        self.runner.tick()
        self.upload_future_csv()
        self.client.post('/schedule/start')
        self.assertEqual(self.schedule()['state'], 'ready')
        self.set_mock_status(9)
        self.client.post('/schedule/start')
        self.set_mock_status(8)
        self.client.get('/api/status')
        self.assertEqual(self.schedule()['state'], 'stopped')

    def test_download_without_a_schedule_is_not_found(self):
        self.assertEqual(self.client.get('/schedule/csv').status_code, 404)

    def test_page_offers_ramp_times_and_keeps_selection_after_redirect(self):
        page = self.client.get('/', query_string={'to': '30', 'ramp': '600'}).text
        self.assertIn('<option value="600" selected>10 min</option>', page)
        self.assertNotIn('<option value="0"', page)
        self.assertEqual(self.post('50', '600').headers['Location'], '/?to=50&ramp=600')

    def test_start_without_a_source_runs_the_held_schedule(self):
        self.upload_future_csv()
        self.client.post('/schedule/start')
        self.assertEqual((self.schedule()['state'], self.schedule()['source']), ('running', 'file'))

    def test_regulation_off_is_refused_without_stopping_the_running_schedule(self):
        self.post('20', '600')
        before = self.schedule()
        self.client.post('/regulation/off')
        after = self.schedule()
        self.assertEqual(after['state'], 'running')
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(self.device.get('/mock/history').json['commands'], [])
        self.assertIn('Stop the schedule before turning Regulation off', self.flashes()[-1][1])

    def test_regulation_off_is_allowed_after_explicitly_stopping_the_schedule(self):
        self.post('20', '600')
        self.client.post('/schedule/end')
        self.client.post('/regulation/off')
        self.assertEqual(self.schedule()['state'], 'stopped')
        self.assertEqual(self.device.get('/mock/history').json['commands'][-1]['parameters'], {'APC_STOP_SV': '1'})

    def test_regulation_off_refused_for_remote_leaves_the_schedule_running(self):
        self.post('20', '600')
        self.set_mock_status(1)
        self.client.post('/regulation/off')
        self.assertEqual(self.schedule()['state'], 'running')

    def test_status_read_stops_the_schedule_when_regulation_went_off_elsewhere(self):
        self.post('20', '600')
        self.set_mock_status(8)
        self.client.get('/api/status')
        snapshot = self.schedule()
        self.assertEqual(snapshot['state'], 'stopped')
        self.assertEqual(snapshot['error'], 'Regulation is off. Schedule stopped.')

    def test_row_is_not_sent_and_is_kept_when_regulation_is_off(self):
        self.post('20', '600')
        before = self.device.get('/mock/state').json['request_counts']['command']
        index = control.scheduler.index
        self.set_mock_status(8)
        control.scheduler.tick(now=datetime.now() + timedelta(seconds=10))
        self.assertEqual(self.schedule()['state'], 'stopped')
        self.assertEqual(self.device.get('/mock/state').json['request_counts']['command'], before)
        self.assertEqual(control.scheduler.index, index)

    def test_stale_status_cannot_stop_a_newer_schedule(self):
        stale = self.schedule()['revision']
        self.post('20', '600')
        self.assertFalse(control.scheduler.halt('off', stale))
        self.assertEqual(self.schedule()['state'], 'running')

    def test_target_equal_to_current_clears_a_previously_loaded_schedule(self):
        self.upload_future_csv('old.csv')
        self.assertEqual(self.schedule()['state'], 'ready')
        self.client.post('/schedule/set', data={'to': '50', 'ramp_seconds': '600'})
        self.assertIn('No schedule is needed', self.flashes()[-1][1])
        self.assertEqual(self.schedule()['state'], 'idle')
        self.assertIsNone(self.schedule()['name'])

    def test_planned_ramp_preserves_to_value_when_reloading_root(self):
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        page = self.client.get('/').text
        self.assertIn('id="to-value" name="to" type="number" step="0.1" value="30"', page)

    def test_stop_during_ramp_start_status_read_cancels_the_start(self):
        self.client.post('/schedule/set', data={'to': '30', 'ramp_seconds': '600'})
        revision = self.schedule()['revision']
        real_get_status = control.get_device_status

        def stop_while_reading(**kwargs):
            status = real_get_status(**kwargs)
            self.runner.stop()
            return status

        with patch.object(control, 'get_device_status', side_effect=stop_while_reading):
            self.client.post('/schedule/start', data={'revision': revision})
        self.assertIn('Schedule changed', self.flashes()[-1][1])
        self.assertEqual(self.schedule()['state'], 'ready')
