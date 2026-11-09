# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import socket
import threading
import time
import unittest
from unittest.mock import patch
from support import control


class DNSLifecycleTests(unittest.TestCase):
    def run_slow_dns_case(self, cancel):
        original = socket.getaddrinfo
        release = threading.Event()
        finished = threading.Event()
        event = threading.Event()
        def slow_lookup(host, port, family=0, type=0, proto=0, flags=0):
            # Trio's synchronous numeric-host probe does not perform DNS.
            if flags & socket.AI_NUMERICHOST:
                return original(host, port, family, type, proto, flags)
            try:
                release.wait(1)
                raise socket.gaierror('simulated stalled DNS')
            finally:
                finished.set()
        timer = threading.Timer(0.15, event.set)
        if cancel:
            timer.start()
        started = time.monotonic()
        try:
            with patch('socket.getaddrinfo', side_effect=slow_lookup):
                with self.assertRaises(control.ScheduleCancelled if cancel else control.DeviceTimeoutError):
                    control._request_device('http://blocked.invalid/', timeout=2 if cancel else 0.15,
                                            cancel_event=event if cancel else None)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.6)
        finally:
            release.set()
            if cancel:
                timer.cancel()
                timer.join()
            self.assertTrue(finished.wait(2))

    def test_timeout_does_not_wait_for_stalled_dns_to_finish(self):
        self.run_slow_dns_case(cancel=False)

    def test_end_cancellation_does_not_wait_for_stalled_dns_to_finish(self):
        self.run_slow_dns_case(cancel=True)
