# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from support import mock_device


class MockDeviceTests(unittest.TestCase):
    def setUp(self):
        self.client = mock_device.app.test_client()
        self.client.post('/mock/reset')

    def tearDown(self):
        self.client.post('/mock/reset')

    def test_reset_restores_pressure_status_and_history(self):
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1&APC_P_SV=12.5')
        history = self.client.get('/mock/history')
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json['commands'][0]['state']['APC_P_SV'], 12.5)
        self.assertEqual(self.client.post('/mock/reset').status_code, 200)
        self.assertEqual(self.client.get('/mock/history').json['commands'], [])
        self.assertIn('50.0', self.client.get('/WeatherMonitor/Get_Latest_Data').text)

    def test_http_error_mode_does_not_apply_command(self):
        self.assertEqual(self.client.post('/mock/config', json={'mode': 'http_error'}).status_code, 200)
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1').status_code, 503)
        self.assertEqual(self.client.get('/mock/history').json['commands'], [])
        self.assertEqual(mock_device.state['APC_STATUS_PV'], 0)

    def test_malformed_mode_and_recovery(self):
        self.client.post('/mock/config', json={'mode': 'malformed'})
        response = self.client.get('/WeatherMonitor/Get_Latest_Data')
        self.assertNotIn('APC_STATUS_PV', response.text)
        self.client.post('/mock/config', json={'mode': 'normal'})
        self.assertIn('APC_STATUS_PV', self.client.get('/WeatherMonitor/Get_Latest_Data').text)

    def test_invalid_fault_configuration_rejected(self):
        for config in ({'mode': 'unknown'}, {'mode': 'slow', 'delay': -1}, {'mode': 'slow', 'delay': 'nan'}):
            with self.subTest(config=config):
                self.assertEqual(self.client.post('/mock/config', json=config).status_code, 400)

    def test_run_stop_preserve_other_status_bits(self):
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=14')
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1').json['state']['APC_STATUS_PV'], 15)
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_STOP_SV=1').json['state']['APC_STATUS_PV'], 14)

    def test_empty_command_rejected(self):
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data').status_code, 400)

    def test_remote_off_rejects_run_and_stop(self):
        for name in ('APC_RUN_SV', 'APC_STOP_SV'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post('/WeatherMonitor/Set_Data', query_string={name: 1}).status_code, 409)
        self.assertEqual(self.client.get('/mock/history').json['commands'], [])

    def test_pulse_sv_resets_and_status_is_authoritative(self):
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        response = self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1')
        self.assertEqual(response.json['state']['APC_RUN_SV'], 0)
        self.assertEqual(response.json['state']['APC_STATUS_PV'], 9)
        response = self.client.post('/WeatherMonitor/Set_Data?APC_STOP_SV=1')
        self.assertEqual(response.json['state']['APC_STOP_SV'], 0)
        self.assertEqual(response.json['state']['APC_STATUS_PV'], 8)

    def test_ignored_commands_accept_but_do_not_change_readback(self):
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        self.assertEqual(self.client.post('/mock/config', json={'mode':'ignore_control'}).status_code, 200)
        response = self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state']['APC_STATUS_PV'], 8)
        self.assertEqual(self.client.post('/mock/config', json={'mode':'ignore_setpoint'}).status_code, 200)
        response = self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12.3')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state']['APC_P_SV'], 50.0)

    def test_delayed_changes_apply_when_due_and_reset_clears_them(self):
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        self.client.post('/mock/config', json={'run_delay': 0.2, 'setpoint_delay': 60})
        response = self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1')
        self.assertEqual(response.json['state']['APC_STATUS_PV'], 8)
        self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12.5')
        self.assertEqual(self.client.get('/mock/state').json['pending'], ['RUN on', 'APC_P_SV=12.5'])
        with mock_device.state_lock:
            mock_device.apply_due_locked(mock_device.pending[0][0])
        self.assertEqual(self.client.get('/mock/state').json['pending'], ['APC_P_SV=12.5'])
        self.assertEqual(mock_device.state['APC_STATUS_PV'], 9)
        self.assertEqual(mock_device.state['APC_P_SV'], 50.0)
        self.client.post('/mock/reset')
        self.assertEqual(self.client.get('/mock/state').json['pending'], [])

    def test_setpoint_rounding_uses_configured_decimals(self):
        self.client.post('/mock/config', json={'setpoint_decimals': 1})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12.34').json['state']['APC_P_SV'], 12.3)
        self.client.post('/mock/config', json={'setpoint_decimals': None})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12.34').json['state']['APC_P_SV'], 12.34)

    def test_transient_fault_reverts_after_the_requested_count(self):
        self.client.post('/mock/config', json={'mode': 'http_error', 'fail_count': 2, 'http_status': 500})
        for _ in range(2):
            self.assertEqual(self.client.get('/WeatherMonitor/Get_Latest_Data').status_code, 500)
        self.assertEqual(self.client.get('/WeatherMonitor/Get_Latest_Data').status_code, 200)
        fault = self.client.get('/mock/state').json['fault']
        self.assertEqual((fault['mode'], fault['fail_count']), ('normal', None))

    def test_status_only_fault_is_not_consumed_by_commands(self):
        self.client.post('/mock/config', json={'mode': 'malformed', 'fail_count': 1})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12').status_code, 200)
        self.assertEqual(self.client.get('/mock/state').json['fault']['mode'], 'malformed')

    def test_partial_configuration_keeps_other_settings(self):
        self.client.post('/mock/config', json={'delay': 3, 'run_delay': 1})
        fault = self.client.post('/mock/config', json={'mode': 'slow'}).json
        self.assertEqual((fault['mode'], fault['delay'], fault['run_delay']), ('slow', 3.0, 1.0))

    def test_invalid_settings_change_nothing(self):
        before = self.client.get('/mock/state').json['fault']
        for config in ({'unknown': 1}, {'delay': True}, {'http_status': 200}, {'http_status': 500.5},
                       {'fail_count': 0}, {'setpoint_decimals': 7}, {'mode': 'slow', 'delay': 61}):
            with self.subTest(config=config):
                self.assertEqual(self.client.post('/mock/config', json=config).status_code, 400)
                self.assertEqual(self.client.get('/mock/state').json['fault'], before)
        self.assertEqual(self.client.post('/mock/config', data='[]', content_type='application/json').status_code, 400)

    def test_form_configuration_treats_blank_optional_values_as_unset(self):
        response = self.client.post('/mock/config', data={'mode': 'slow', 'delay': '2', 'fail_count': '', 'setpoint_decimals': ''})
        self.assertEqual(response.status_code, 302)
        fault = self.client.get('/mock/state').json['fault']
        self.assertEqual((fault['fail_count'], fault['setpoint_decimals'], fault['delay']), (None, None, 2.0))

    def test_request_counts_and_history_record_modes(self):
        self.client.get('/WeatherMonitor/Get_Latest_Data')
        self.client.post('/mock/config', json={'mode': 'ignore_setpoint'})
        self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12')
        self.assertEqual(self.client.get('/mock/state').json['request_counts'], {'status': 1, 'command': 1})
        self.assertEqual(self.client.get('/mock/history').json['commands'][0]['mode'], 'ignore_setpoint')

    def test_status_faults(self):
        self.client.post('/mock/config', json={'mode': 'bad_status'})
        self.assertIn('"APC_STATUS_PV": 99', self.client.get('/WeatherMonitor/Get_Latest_Data').text)
        self.client.post('/mock/config', json={'mode': 'no_setpoint'})
        self.assertNotIn('APC_P_SV', self.client.get('/WeatherMonitor/Get_Latest_Data').text)
        self.client.post('/mock/config', json={'mode': 'empty'})
        self.assertEqual(self.client.get('/WeatherMonitor/Get_Latest_Data').text, '')

    def test_mode_faults_on_commands(self):
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        self.client.post('/mock/config', json={'mode': 'remote_drop'})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1').json['state']['APC_STATUS_PV'], 0)
        self.client.get('/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=8')
        self.client.post('/mock/config', json={'mode': 'alarm_on_run'})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_RUN_SV=1').json['state']['APC_STATUS_PV'], 10)
        self.client.post('/mock/config', json={'mode': 'accept_then_error', 'http_status': 502})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=12').status_code, 502)
        self.assertEqual(mock_device.state['APC_P_SV'], 12.0)
        self.client.post('/mock/config', json={'mode': 'redirect'})
        self.assertEqual(self.client.post('/WeatherMonitor/Set_Data?APC_P_SV=13').status_code, 302)
        self.assertEqual(mock_device.state['APC_P_SV'], 12.0)

    def test_page_renders_every_mode(self):
        page = self.client.get('/').text
        for mode in mock_device.MODES:
            self.assertIn(f'<option value="{mode}"', page)
