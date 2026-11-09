# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import threading
import unittest
from unittest.mock import patch
from support import Clock, control


class SetpointTests(unittest.TestCase):
    def test_set_success_requires_matching_readback(self):
        clock = Clock()
        with patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', side_effect=[{'pressure_value': 50.0}, {'pressure_value': 12.34}]) as get, patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
            url = control.send_value('12.34')
            self.assertIn('APC_P_SV=12.34', url)
            self.assertEqual(get.call_count, 2)

    def test_unchanged_or_missing_setpoint_errors_at_ten_seconds(self):
        for value in (50.0, None):
            clock = Clock()
            with self.subTest(value=value), patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', return_value={'pressure_value': value}), patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
                with self.assertRaisesRegex(RuntimeError, '10'):
                    control.send_value('12.34')
                self.assertAlmostEqual(clock.now, 10.0)

    def test_cancellation_during_readback_stops_confirmation(self):
        event = threading.Event()
        def read(**kwargs):
            event.set()
            return {'pressure_value': 50.0}
        with patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', side_effect=read):
            with self.assertRaises(control.ScheduleCancelled):
                control.send_value('12.34', cancel_event=event)

    def test_cancelled_setpoint_never_sends_command(self):
        event = threading.Event()
        event.set()
        with patch.object(control, 'send_command') as send:
            with self.assertRaises(control.ScheduleCancelled):
                control.send_value('12.34', cancel_event=event)
            send.assert_not_called()

    def test_concurrent_setpoint_request_is_rejected_without_sending(self):
        control.setpoint_lock.acquire()
        try:
            with patch.object(control, 'send_command') as send:
                with self.assertRaises(RuntimeError):
                    control.send_value('12.34')
                send.assert_not_called()
        finally:
            control.setpoint_lock.release()

    def test_cancelled_confirmation_releases_lock_for_next_set(self):
        event = threading.Event()
        def read(**kwargs):
            event.set()
            return {'pressure_value': 50.0}
        with patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', side_effect=read):
            with self.assertRaises(control.ScheduleCancelled):
                control.send_value('12.34', cancel_event=event)
        with patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', return_value={'pressure_value': 22.0}):
            self.assertIn('APC_P_SV=22', control.send_value('22'))

    def test_rounded_readback_within_one_hundredth_is_accepted(self):
        cases = [('12.345', 12.35), ('12.345', 12.34), ('1.00', 1.01),
                 ('1.00', 0.99), ('0', 0.01), ('0.01', 0.0)]
        for requested, returned in cases:
            clock = Clock()
            with self.subTest(requested=requested, returned=returned), patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', return_value={'pressure_value': returned}), patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
                control.send_value(requested)
                self.assertEqual(clock.now, 0.0)

    def test_readback_outside_one_hundredth_is_rejected(self):
        for requested, returned in [('1.00', 1.0101), ('1.00', 0.9899), ('0', 0.011), ('100', 100.02)]:
            clock = Clock()
            with self.subTest(requested=requested, returned=returned), patch.object(control, 'send_command', return_value='ok'), patch.object(control, 'get_device_status', return_value={'pressure_value': returned}), patch.object(control.time, 'monotonic', clock.monotonic), patch.object(control.time, 'sleep', clock.sleep):
                with self.assertRaises(RuntimeError):
                    control.send_value(requested)
                self.assertAlmostEqual(clock.now, 10.0)
