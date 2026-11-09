# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import webbrowser

TRUE_VALUES = {"1", "true", "yes", "on"}


def open_browser(url):
    try:
        if not webbrowser.open(url):
            logging.warning("Could not open browser: %s", url)
    except Exception:
        logging.exception("Could not open browser: %s", url)
