# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import secrets
import sys
import threading
import tempfile
import time
from logging.handlers import RotatingFileHandler
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx
import trio

from flask import Flask, Response, flash, jsonify, redirect, render_template_string, request, url_for
from flask import cli as flask_cli
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.utils import secure_filename

from ._browser import TRUE_VALUES, open_browser as _open_browser

DATA_DIR = Path(os.environ.get("APC_DATA_DIR", Path.home() / "deleteme.d" / datetime.now().strftime("%Y-%m")))
UPLOAD_DIR = DATA_DIR / "chamber-apc-control-schedules"
DEVICE_URL = os.environ.get("DEVICE_BASE_URL", "http://127.0.0.1:8001/WeatherMonitor/").rstrip("/") + "/"
PARAMETER = "APC_P_SV"
# Terms used throughout this file:
#   setpoint  APC_P_SV, the requested pressure. It is not a pressure measurement.
#   command   One setpoint sent once and confirmed by readback.
#   schedule  Rows of (datetime, P (hPa)) that are sent as commands when due.
#             Every setpoint goes through a schedule. Ramp time 0 s is one row, sent now.
#   source    Where a schedule came from: "file" (an uploaded CSV) or "set"
#             (the page's To and Ramp time, with no file). This is the only
#             difference between schedules.
CSV_COLUMNS = ("date", "time", "pressure")
CSV_HEADER = ",".join(CSV_COLUMNS)
CSV_DATETIME_COLUMN = "datetime"
CSV_PRESSURE_COLUMN = "P (hPa)"
RUN_BIT = 1 << 0
REGULATION_OFF_MESSAGE = "Regulation is off. Schedule stopped."
REMOTE_OFF_MESSAGE = "REMOTE flag is off. Schedule stopped."
REMOTE_REFUSED_MESSAGE = "REMOTE flag is off, so the setpoint cannot be set."
ALARM_ON_MESSAGE = "ALARM is ON. Schedule stopped."
ALARM_REFUSED_MESSAGE = "ALARM is ON. Check the device."
ALARM_BIT = 1 << 1
REMOTE_BIT = 1 << 3
CONFIRM_TIMEOUT = 10.0
SETPOINT_TOLERANCE_HPA = Decimal("0.01")
# After a sleep or a long device stall, do not replay a backlog of old setpoints.
SCHEDULE_MAX_LATENESS = timedelta(seconds=60)
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def _pressure_limit(name, default):
    # A wrong safety limit must stop the panel, not fall back to another value.
    text = os.environ.get(name, default)
    try:
        limit = float(text)
    except ValueError:
        limit = math.nan
    if not math.isfinite(limit):
        raise SystemExit(f"{name} must be a pressure in hPa (got {text!r})")
    return limit


PRESSURE_MIN_HPA = _pressure_limit("APC_P_MIN", "0")
PRESSURE_MAX_HPA = _pressure_limit("APC_P_MAX", "100")
if PRESSURE_MIN_HPA >= PRESSURE_MAX_HPA:
    raise SystemExit("APC_P_MIN must be below APC_P_MAX")
PRESSURE_MAX_STEP_HPA = _pressure_limit("APC_P_MAX_STEP", "50")
if PRESSURE_MAX_STEP_HPA <= 0:
    raise SystemExit("APC_P_MAX_STEP must be above 0 hPa")
RAMP_STEP_SECONDS = 10
RAMP_MAX_SECONDS = 24 * 60 * 60
SOURCE_FILE = "file"
SOURCE_SET = "set"
DEFAULT_RAMP_SECONDS = 600
RAMP_CHOICES = ((0, "0 s"), (30, "30 s"), (60, "1 min"), (120, "2 min"), (300, "5 min"),
                (600, "10 min"), (1200, "20 min"), (1800, "30 min"), (3600, "1 h"))
regulation_lock = threading.Lock()
setpoint_lock = threading.Lock()
START_URL = DEVICE_URL + "Set_Data?APC_RUN_SV=1"
STOP_URL = DEVICE_URL + "Set_Data?APC_STOP_SV=1"
LOG_DIR = DATA_DIR
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "chamber-apc-control.log"
try:
    LOG_DISPLAY_LIMIT = max(10, min(1000, int(os.environ.get("LOG_DISPLAY_LIMIT", "100"))))
except ValueError:
    LOG_DISPLAY_LIMIT = 100
DEBUG_MODE = "--debug" in sys.argv[1:] or os.environ.get("APP_DEBUG", "").lower() in TRUE_VALUES

logging.basicConfig(handlers=[RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3, encoding="utf-8")],
                    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.INFO if DEBUG_MODE else logging.ERROR)
logging.getLogger("httpx").setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.DEBUG if DEBUG_MODE else logging.WARNING)
# Keep enough history for the largest browser-selectable display limit.
recent_logs: deque[str] = deque(maxlen=1000)
recent_logs_lock = threading.Lock()


class RecentLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with recent_logs_lock:
                recent_logs.append(message)
        except Exception:
            self.handleError(record)


recent_handler = RecentLogHandler()
recent_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(recent_handler)
app = Flask(__name__)
MAX_UPLOAD_MIB = 5
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MIB * 1024 * 1024
# The session only carries one-time flash messages, so it need not survive a restart.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)


class BrowserActivity:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_seen = time.monotonic()

    def touch(self):
        with self.lock:
            self.last_seen = time.monotonic()

    def reopen_if_idle(self, url):
        with self.lock:
            now = time.monotonic()
            if now - self.last_seen < 60:
                return
            # Rate-limit attempts even if the browser cannot be opened.
            self.last_seen = now
        _open_browser(url)


browser_activity = BrowserActivity()


def browser_worker(url, stop_event):
    while not stop_event.wait(1):
        browser_activity.reopen_if_idle(url)


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auto Pressure Control</title>
<style>
:root{--lamp-on:#20a34a;--lamp-off:#98a2ad}
*{box-sizing:border-box}body{font-family:system-ui,sans-serif;background:#f3f5f7;color:#17202a;margin:0;padding:32px}
main{max-width:680px;margin:auto;background:white;padding:28px;border-radius:14px;box-shadow:0 4px 18px #0001}
.section{border:1px solid #d7dee5;border-radius:12px;padding:18px;margin:18px 0;background:#fff}
.section h2{margin:0 0 14px}
.status-updated{font-weight:400;margin:0}.status-updated.stale{color:#a83e3e}
#scheduler-state{text-transform:capitalize}#schedule-chart-wrap{position:relative;margin:10px 0}#schedule-chart{touch-action:pan-y}#schedule-chart svg{display:block;width:100%;height:190px}#schedule-chart .tick{font-size:11px;fill:#566573}#schedule-chart path{fill:none;stroke:#2563a8;stroke-linejoin:round}#schedule-chart .done{stroke-width:2.5}#schedule-chart .rest{stroke-width:1.5;stroke-dasharray:5 4}#schedule-chart .marker{stroke:#17202a}#schedule-chart .dot{fill:#17202a;stroke:#fff;stroke-width:2}#schedule-chart .next{fill:#fff;stroke:#17202a;stroke-width:2}#schedule-chart .cross{stroke:#98a2ad;stroke-dasharray:3 3}#schedule-chart .hover{fill:#2563a8;stroke:#fff;stroke-width:2}#schedule-chart-tip{position:absolute;top:0;left:0;background:#17202a;color:#fff;font-size:.8rem;padding:4px 8px;border-radius:6px;pointer-events:none;white-space:nowrap}#schedule-progress{padding:8px 10px;border-radius:7px}#schedule-progress p{margin:2px 0}#schedule-points{border-collapse:collapse;font-size:.85rem;color:#566573;font-variant-numeric:tabular-nums}#schedule-points th,#schedule-points td{padding:2px 12px 2px 0;text-align:left;font-weight:400}#schedule-points thead th{font-weight:700;font-size:.78rem}#schedule-points th,#schedule-points td span{white-space:nowrap}#schedule-points .pressure{text-align:right;padding-right:0}
#schedule-section.running{animation:schedule-pulse 2.4s ease-in-out infinite}
@keyframes schedule-pulse{0%,100%{background-color:transparent}50%{background-color:#d9efe4}}
@media(prefers-reduced-motion:reduce){ #schedule-section.running{animation:none;background-color:#d9efe4}}
.unit{align-self:center;font-weight:600;color:#566573;padding:0 4px}
.section-subheading{font-size:1.17em;margin:1em 0}
.indicators{display:grid;grid-template-columns:repeat(5,auto);gap:6px;margin:14px 0}@media(max-width:719px){.indicators{grid-template-columns:repeat(4,auto)}#schedule-flag{grid-column:1/-1}}
.indicator{display:flex;align-items:center;gap:6px;padding:6px;border:1px solid #d7dee5;border-radius:8px;white-space:nowrap;font-size:clamp(.72rem,1.5vw,.82rem)}
.lamp{width:16px;height:16px;flex:0 0 16px;border-radius:50%;background:var(--lamp-off)}
.lamp.on{background:var(--lamp-on)}.lamp.off{background:var(--lamp-off)}.lamp.unknown{background:#e0a52b}.lamp.alert.on{background:#d13438}
h1{margin:0}.page-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:20px}
.help{position:relative;flex:none}.help summary{list-style:none;width:32px;height:32px;line-height:30px;box-sizing:border-box;text-align:center;border:1px solid #b8c2cc;border-radius:50%;font-weight:700;color:#566573;background:#fff;cursor:pointer}.help summary::-webkit-details-marker{display:none}
.help-panel{position:absolute;right:0;top:40px;z-index:10;width:min(380px,calc(100vw - 64px));box-sizing:border-box;padding:4px 16px;background:#fff;border:1px solid #b8c2cc;border-radius:7px;box-shadow:0 8px 24px rgba(23,32,42,.18);font-size:.95rem;line-height:1.45}.help-panel ol{padding-left:20px}.help-panel li{margin:6px 0}
.row{display:flex;gap:12px;align-items:end;flex-wrap:wrap}.actions{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0}.actions.single{grid-template-columns:1fr}
label{display:block;font-weight:600;margin:12px 0 6px}input{font:inherit;padding:12px;border:1px solid #b8c2cc;border-radius:7px;width:100%}
button{font:inherit;font-weight:700;border:0;border-radius:8px;padding:14px;cursor:pointer;color:white;background:#216eaa;width:100%}
button:disabled{opacity:.45;cursor:not-allowed}button.running{outline:4px solid #19a34a;outline-offset:2px}
button.stop{background:#a83e3e}#regulation-toggle{background:var(--lamp-off);border:2px solid var(--lamp-off);color:#17202a;padding:12px}#regulation-toggle.on{background:#dff7e6;border-color:#00a83a;color:#00a83a}.set{display:flex;gap:10px}.set input{max-width:230px}.set button{max-width:190px;background:#646b73}
.sources{display:flex;flex-wrap:wrap;gap:6px 16px;margin:12px 0}.sources label{display:inline-flex;align-items:center;gap:8px;margin:0;font-weight:700;cursor:pointer}.sources input{width:18px;height:18px;margin:0}.from-to{display:flex;flex-wrap:wrap;gap:0 24px}.from-to label{margin-top:12px}.from-to input{width:6.5em}.field-label{display:block;font-weight:600;margin:12px 0 6px}.from-value{margin:0;padding:12px 0;font-size:1.1rem}.ramp-time{font:inherit;padding:10px;border:1px solid #b8c2cc;border-radius:7px;background:#fff;max-width:230px}
#schedule-start{background:#23766b}#schedule-end{background:#a85a24}
.csv-upload-form{min-width:0}.file-control{position:relative;display:flex;align-items:center;flex:1;min-width:0;height:46px;overflow:hidden;border:1px solid #b8c2cc;border-radius:7px;background:#fff}.file-control.disabled{opacity:.5}.file-control.disabled .file-picker{cursor:not-allowed}.csv-file-input{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.file-picker{display:inline-flex;align-items:center;align-self:stretch;flex:0 0 auto;margin:0;padding:0 14px;border:0;border-right:1px solid #b8c2cc;border-radius:0;background:#e5e7eb;color:#17202a;cursor:pointer}.file-picker:hover{background:#dfe3e8}.selected-file{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 10px;color:#566573}
.csv-format{margin:12px 0}.csv-format summary{cursor:pointer;font-weight:600;color:#566573}.csv-format pre{overflow:auto;max-height:16em;background:#eef1f4;padding:12px;border-radius:7px;font-size:.85rem}
.note{color:#566573;font-size:.92rem}.flash-slot{min-height:64px;display:flex;align-items:center;margin:0 0 8px}.flash{background:#e8f4ed;padding:12px;min-height:44px;width:100%;display:flex;align-items:center;margin:0;border-radius:7px}.err{background:#fdebea}
@media(max-width:479px){ #schedule-progress{padding:8px 2px}#schedule-points{font-size:.75rem}#schedule-points th,#schedule-points td{padding-right:6px}#schedule-points .pressure{padding-right:0}body{padding:16px}main{padding:16px}.sources{gap:6px 12px;font-size:.92rem}.sources label{gap:5px}.sources input{width:16px;height:16px}.section{padding:12px}.indicators{gap:4px}.indicator{flex-direction:column;gap:4px;padding:6px 2px;font-size:min(11px,2.9vw)}.from-to{flex-wrap:nowrap;align-items:flex-end;gap:0 8px;font-size:.85rem}.from-to>div{min-width:0}.from-to>div:last-child{flex:1}.from-to input{width:4.8em;padding:8px 4px;font-size:inherit}.from-to .set{gap:2px}.from-to .unit{padding:0 1px}.from-value{font-size:inherit;white-space:nowrap}.from-to .ramp-time{width:100%;padding:8px 2px;font-size:inherit}}
@media(max-width:359px){.from-to{flex-wrap:wrap}.from-to>div:last-child{flex:0 0 100%}}
</style></head><body><main><header class="page-head"><h1>Auto Pressure Control</h1>
<details class="help" id="help"><summary aria-label="Help" title="Help">?</summary><div class="help-panel">
<p>APC controls the pressure inside the chamber. It cannot handle large pressure changes.</p>
<ol>
<li>Without using APC, bring the chamber pressure to the initial pressure.</li>
<li>In the dedicated app, switch the APC from local mode to remote mode.</li>
<li>In `Pressure`, set the initial pressure.</li>
<li>Start regulation in `Regulation`.</li>
<li>Once regulation is achieved, plan the schedule in `Pressure`.</li>
<li>Start the schedule with `Go` in `Schedule`.</li>
</ol></div></details></header>
<div class="flash-slot">{% with messages=get_flashed_messages(with_categories=true) %}{% for category,message in messages %}<div class="flash {{ 'err' if category=='error' else '' }}">{{ message }}</div>{% endfor %}{% endwith %}</div>
<section class="section"><h2>Status</h2>
<p class="note err" id="device-status" role="alert" hidden></p>
<p class="note err" id="device-warning" role="alert" hidden></p>
<div class="indicators" aria-label="Status indicators">
<div class="indicator"><span class="lamp unknown" id="status-light-1"></span><span>3: REMOTE</span></div>
<div class="indicator"><span class="lamp unknown" id="status-light-2"></span><span>2: ZFIN</span></div>
<div class="indicator"><span class="lamp alert unknown" id="status-light-3"></span><span>1: ALARM</span></div>
<div class="indicator"><span class="lamp unknown" id="status-light-4"></span><span>0: REGULATION</span></div>
<div class="indicator" id="schedule-flag" title="Set by this app, not by the device: a schedule is running."><span class="lamp {{ 'on' if scheduler_state == 'running' else 'off' }}" id="status-light-schedule"></span><span>SCHEDULE</span></div>
</div>
<p class="note status-updated" id="status-updated">Last updated: waiting for device…</p>
<p class="note status-updated">Device: {{ device_url }}</p>
</section>
<section class="section"><h2>Regulation with P = <span id="regulation-p">—</span> hPa</h2>
<div class="actions"><form method="post" data-device-action id="regulation-form" action="{{ url_for('regulation', action='on') }}"><button id="regulation-toggle" disabled>—</button></form></div>
<p class="note" id="remote-hint" hidden>Switch the APC from local to remote mode in the dedicated app.</p>
<p class="note" id="action-status" role="status" hidden></p>
 </section>
<section class="section"><h2>Pressure</h2>
<form id="pressure-form" method="post" data-device-action novalidate action="{{ url_for('set_schedule') }}">
<div class="sources"><label><input type="radio" id="source-now" name="choice" value="now" {{ 'checked' if source_choice == 'now' else '' }}> Now</label><label><input type="radio" id="source-ramp" name="choice" value="ramp" {{ 'checked' if source_choice == 'ramp' else '' }}> Ramp</label><label><input type="radio" id="source-file" name="choice" value="file" {{ 'checked' if source_choice == 'file' else '' }}> File</label></div>
<div id="set-fields" {{ 'hidden' if source_choice == 'file' else '' }}>
<div class="from-to"><div id="from-field" {{ 'hidden' if source_choice == 'now' else '' }}><span class="field-label">From</span><p class="from-value"><strong id="from-value">—</strong> <span class="unit">hPa</span></p></div>
<div><label for="to-value">To</label><div class="set"><input id="to-value" name="to" type="number" step="0.1" value="{{ last_to }}"><span class="unit">hPa</span></div></div>
<div id="ramp-field" {{ '' if source_choice == 'ramp' else 'hidden' }}><label for="ramp-time">Ramp time</label><select id="ramp-time" name="ramp_seconds" class="ramp-time" {{ '' if source_choice == 'ramp' else 'disabled' }}>{% for seconds, label in ramp_choices if seconds %}<option value="{{ seconds }}" {{ 'selected' if seconds == (ramp_seconds or default_ramp) else '' }}>{{ label }}</option>{% endfor %}</select></div></div>
<p class="note" id="ramp-plan" role="status" data-step="{{ ramp_step }}" hidden></p>
</div></form>
<div id="file-fields" {{ 'hidden' if source_choice != 'file' else '' }}>
<details class="csv-format"><summary>CSV format</summary>
<pre id="csv-example">{{ csv_example_header }}
(an example appears when APC_P_SV is read)</pre>
</details>
<form id="schedule-upload" class="csv-upload-form" method="post" action="{{ url_for('upload_schedule') }}" enctype="multipart/form-data"><div class="file-control"><label class="file-picker" for="csv_file">Choose File</label><input class="csv-file-input" id="csv_file" name="csv_file" type="file" accept=".csv,text/csv"><span class="selected-file" id="selected-csv"></span></div></form>
<p class="note" id="csv-upload-status" role="status" hidden></p></div>
<p class="note" id="plan-hint" role="status" hidden>Turn Regulation on.</p>
<div class="actions single"><button id="pressure-ok" form="pressure-form" disabled>{{ 'OK' if source_choice == 'now' else 'Plan' }}</button></div>
</section>
<section class="section" id="schedule-section"><h2>Schedule</h2>
<p class="note" id="schedule-name-line" {{ '' if scheduler_loaded else 'hidden' }}><span id="schedule-kind">{{ schedule_kind }}</span>: <strong id="schedule-name">{{ schedule_name }}</strong> <a id="schedule-csv" href="{{ url_for('view_schedule_csv') }}" target="_blank" rel="noopener" {{ '' if scheduler_loaded else 'hidden' }}>[detail]</a></p>
<p class="note" id="schedule-state-line" {{ '' if scheduler_loaded else 'hidden' }}>State: <strong id="scheduler-state">{{ scheduler_state }}</strong></p>
<div id="schedule-chart-wrap" hidden><div id="schedule-chart" role="img" aria-label="Schedule pressure over time"></div><div id="schedule-chart-tip" hidden></div></div>
<p class="note err" id="schedule-error" role="alert" hidden></p>
<div id="schedule-progress"><table id="schedule-points" hidden><thead><tr><th></th><th>datetime</th><th class="pressure">P (hPa)</th></tr></thead><tbody><tr id="schedule-present" hidden><th>● Present</th><td id="present-time"></td><td id="present-value" class="pressure"></td></tr><tr id="schedule-next" hidden><th>○ Next</th><td id="next-time"></td><td id="next-value" class="pressure"></td></tr></tbody></table><p class="note" id="next-schedule">Loading…</p></div>
<div id="schedule-actions" class="actions" {{ '' if scheduler_loaded else 'hidden' }}><form method="post" data-device-action action="{{ url_for('start_schedule') }}"><input type="hidden" id="schedule-revision" name="revision" value="{{ scheduler_revision }}"><button id="schedule-start" class="{{ 'running' if scheduler_state == 'running' else '' }}" disabled>{{ 'Restart' if scheduler_state == 'stopped' and schedule_source == 'file' else 'Go' }}</button></form><form method="post" action="{{ url_for('end_schedule') }}"><button id="schedule-end" type="submit" class="stop" {{ '' if scheduler_state == 'running' else 'disabled' }}>Stop</button></form></div>
</section>
<section class="section"><h2>Logs</h2>
<span class="note">Showing the latest </span><select id="log-limit" aria-label="Log entries to display" style="font:inherit;padding:4px 8px;border:1px solid #b8c2cc;border-radius:7px">
{% for option in log_limit_options %}<option value="{{ option }}" {{ 'selected' if option == log_limit else '' }}>{{ option }}</option>{% endfor %}
</select> <span class="note">entries</span>
<pre id="logs" style="max-height:300px;overflow:auto;white-space:pre-wrap;background:#111827;color:#e5e7eb;padding:14px;border-radius:8px;font-size:.85rem">Loading logs…</pre>
</section>
<script>
const logLimitSelect=document.getElementById('log-limit');
const csvInput=document.getElementById('csv_file');
const selectedCsv=document.getElementById('selected-csv');
const scheduleStart=document.getElementById('schedule-start');
const scheduleEnd=document.getElementById('schedule-end');
const regulationForm=document.getElementById('regulation-form');
const regulationToggle=document.getElementById('regulation-toggle');
const regulationUrls={on:{{ url_for('regulation', action='on')|tojson }},off:{{ url_for('regulation', action='off')|tojson }}};
let scheduleState={{ scheduler_state|tojson }};
let scheduleLoaded={{ scheduler_loaded|tojson }};
let scheduleSource={{ schedule_source|tojson }};
let scheduleHasUpcoming={{ schedule_has_upcoming|tojson }};
let csvLoading=false;
let scheduleRevision=0;
let deviceRunning=false;
let deviceRemote=false;
let devicePressure=null;
let deviceStatusKnown=false;
let deviceAlarm=false;
let actionPending=false;
const confirmTimeout={{ confirm_timeout|tojson }};
// Help for the file format: from the next hour, 10% down from the current APC_P_SV over 10 min, every 30 s.
function csvExample(pressure,now){
    const start=new Date(now);
    start.setMinutes(0,0,0);
    start.setHours(start.getHours()+1);
    const lines=[{{ csv_example_header|tojson }}];
    for(let offset=0;offset<=600;offset+=30){
        const stamp=chartStamp(start.getTime()+offset*1000).replace(' ', ', ');
        const value=(pressure*(1-0.1*offset/600)).toFixed(2);
        lines.push(`${stamp}, ${value}`);
    }
    return lines.join('\n');
}
function updateCsvExample(){
    const example=document.getElementById('csv-example');
    if(example&&devicePressure!==null)example.textContent=csvExample(Number(devicePressure),Date.now());
}
function updateRegulationButtons(){
    updateCsvExample();
    document.getElementById('regulation-p').textContent=devicePressure===null?'—':devicePressure;
    // The color shows the current state, like the lamp. The text names what pressing it does.
    regulationToggle.textContent=deviceStatusKnown?(deviceRunning?'OFF':'ON'):'—';
    regulationToggle.classList.toggle('on',deviceStatusKnown&&deviceRunning);
    regulationToggle.title=deviceStatusKnown?(deviceRunning?'Regulation is ON — click to turn Regulation off':'Regulation is OFF — click to turn Regulation on'):'';
    regulationForm.action=deviceRunning?regulationUrls.off:regulationUrls.on;
    const offBlocked=deviceRunning&&scheduleState==='running';
    regulationToggle.disabled=actionPending||!deviceStatusKnown||!deviceRemote||(!deviceRunning&&deviceAlarm)||offBlocked;
    if(offBlocked)regulationToggle.title='Stop the schedule before turning Regulation off.';
}
function updateRampPlan(){
    const box=document.getElementById('ramp-plan');
    const from=parseFloat(document.getElementById('from-value').textContent);
    const to=parseFloat(document.getElementById('to-value').value);
    const seconds=document.getElementById('source-ramp').checked?Number(document.getElementById('ramp-time').value)||0:0;
    const step=Number(box.dataset.step);
    box.hidden=scheduleState==='running'||!isFinite(from)||!isFinite(to);
    if(box.hidden){box.textContent='';return;}
    const change=to-from;
    // Only a ramp has something worth saying: how many steps and how fast.
    box.hidden=!seconds||Math.abs(change)<=0.01;
    if(box.hidden)box.textContent='';
    else{
        // Rows round to 0.01 hPa and repeated values are left out.
        const rows=Math.min(Math.ceil(seconds/step),Math.ceil(Math.abs(change)/0.01));
        const rate=Number((change/seconds*60).toFixed(3));
        box.textContent=`${rows} ${rows===1?'step':'steps'}, ${rate>0?'+':''}${rate} hPa/min.`;
    }
}
// To starts at the current setpoint, on page load and on choosing Now or Ramp, never over typing.
// Ramp keeps a To it already has, so the value of a planned ramp stays after Plan.
let toPending=true;
function fillTo(){
    const toInput=document.getElementById('to-value');
    const rampShown=!!document.getElementById('source-ramp').checked;
    if(!toPending||document.getElementById('source-file').checked||devicePressure===null)return;
    if(!rampShown||toInput.value==='')toInput.value=devicePressure;
    toPending=false;
}
function chooseSource(id){document.getElementById(id).checked=true;updateScheduleButton();}
function updateDeviceWarning(){
    updateRampPlan();
    document.getElementById('remote-hint').hidden=!deviceStatusKnown||deviceRemote;
    const warnings=[];
    if(deviceAlarm)warnings.push('ALARM is ON. Check the device.');
    const box=document.getElementById('device-warning');
    box.textContent=warnings.join(' ');
    box.hidden=warnings.length===0;
}
let scheduleNeedsRegulation=true;
function updateScheduleButton(){
    const fileShown=!!document.getElementById('source-file').checked;
    const rampShown=!!document.getElementById('source-ramp').checked;
    document.getElementById('from-field').hidden=!rampShown;
    document.getElementById('ramp-field').hidden=!rampShown;
    document.getElementById('ramp-time').disabled=!rampShown;
    document.getElementById('file-fields').hidden=!fileShown;
    document.getElementById('set-fields').hidden=fileShown;
    // Plan sets a ramp or a file for Start; OK runs Now at once, so it needs REMOTE.
    document.getElementById('pressure-ok').textContent=fileShown||rampShown?'Plan':'OK';
    const nowCannotSend=!fileShown&&!rampShown&&(!deviceRemote||deviceAlarm);
    // A ramp or a file only runs with Regulation on, so do not plan one without it.
    const planNeedsRegulation=(fileShown||rampShown)&&!deviceRunning;
    document.getElementById('plan-hint').hidden=!planNeedsRegulation;
    const inputReady=!nowCannotSend&&!planNeedsRegulation&&(fileShown?!!(csvInput.files&&csvInput.files[0]):!!document.getElementById('to-value').value);
    document.getElementById('pressure-ok').disabled=actionPending||csvLoading||scheduleState==='running'||!inputReady;
    document.getElementById('schedule-actions').hidden=!scheduleLoaded;
    scheduleStart.textContent=scheduleSource==='file'&&scheduleState==='stopped'?'Restart':'Go';
    // One row only sets the setpoint, which comes before Regulation.
    const startable=scheduleLoaded&&(scheduleState==='ready'||scheduleState==='stopped')&&scheduleHasUpcoming;
    scheduleStart.disabled=actionPending||csvLoading||!startable||!deviceRemote||deviceAlarm||!(deviceRunning||!scheduleNeedsRegulation);
    scheduleEnd.disabled=actionPending||scheduleState!=='running';
    scheduleStart.classList.toggle('running',scheduleState==='running');
    csvInput.disabled=csvLoading||scheduleState==='running';
    csvInput.closest('.file-control').classList.toggle('disabled',csvInput.disabled);
    csvInput.closest('.file-control').title=scheduleState==='running'?'End the schedule before loading another CSV.':'';
}
async function loadSelectedCsv(){
    const file=csvInput.files[0];
    if(!file||csvLoading)return;
    chooseSource('source-file');
    const formData=new FormData(document.getElementById('schedule-upload'));
    const uploadStatus=document.getElementById('csv-upload-status');
    selectedCsv.textContent=file.name;
    csvLoading=true;
    scheduleRevision++;
    updateScheduleButton();
    uploadStatus.hidden=false;
    uploadStatus.classList.remove('err');
    uploadStatus.textContent=`Loading ${file.name}…`;
    try{
        const response=await fetch('{{ url_for("upload_schedule") }}',{method:'POST',body:formData});
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Could not load CSV.');
        window.history?.replaceState?.(null,'','{{ url_for("index") }}');
        showSchedule(data);
        uploadStatus.textContent=data.has_upcoming===false?`Loaded ${data.name}. No upcoming steps in this file.`:`Loaded ${data.name}. Press Go to begin.`;
    }catch(e){
        selectedCsv.textContent='';
        // A lost response may still have loaded the new CSV on the server.
        // Wait for authoritative status before allowing Start again.
        scheduleLoaded=false;
        uploadStatus.classList.add('err');
        uploadStatus.textContent='Could not load CSV: '+e.message;
    }finally{
        csvInput.value='';
        csvLoading=false;
        updateScheduleButton();
        refreshNextSchedule();
    }
}
csvInput.addEventListener('change',()=>{
    selectedCsv.textContent=csvInput.files[0]?csvInput.files[0].name:'';
    document.getElementById('csv-upload-status').hidden=true;
    chooseSource('source-file');
});
// With a file chosen, Plan uploads it instead of submitting the form.
document.getElementById('pressure-form').addEventListener('submit',event=>{
    if(!document.getElementById('source-file').checked)return;
    event.preventDefault();
    loadSelectedCsv();
});
const help=document.getElementById('help');
window.addEventListener('click',e=>{if(!help.contains(e.target))help.open=false;});
window.addEventListener('keydown',e=>{if(e.key==='Escape')help.open=false;});
['source-now','source-ramp','source-file'].forEach(id=>document.getElementById(id).addEventListener('change',()=>{updateScheduleButton();updateRampPlan();}));
['source-now','source-ramp'].forEach(id=>document.getElementById(id).addEventListener('change',()=>{toPending=true;fillTo();updateScheduleButton();updateRampPlan();}));
document.getElementById('to-value').addEventListener('input',()=>{toPending=false;});
['to-value','ramp-time'].forEach(id=>document.getElementById(id).addEventListener('input',()=>{updateScheduleButton();updateRampPlan();}));
// Regulation and Start wait for the device, so show progress and block repeat clicks.
document.querySelectorAll('form[data-device-action]').forEach(form=>form.addEventListener('submit',event=>{
    if(event.defaultPrevented)return;
    const status=document.getElementById('action-status');
    status.textContent=`Sending to the device and waiting for confirmation (up to ${confirmTimeout} seconds)…`;
    status.hidden=false;
    // Disable after this event so that the browser still submits the form.
    setTimeout(()=>{actionPending=true;updateRegulationButtons();updateScheduleButton();},0);
}));
// A page restored from the back/forward cache could show stale pending buttons.
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
document.getElementById('schedule-upload').addEventListener('submit',event=>{event.preventDefault();loadSelectedCsv();});
const savedLogLimit=localStorage.getItem('log-display-limit');
if(savedLogLimit&&[...logLimitSelect.options].some(option=>option.value===savedLogLimit))logLimitSelect.value=savedLogLimit;
logLimitSelect.addEventListener('change',()=>{localStorage.setItem('log-display-limit',logLimitSelect.value);refreshLogs();});
async function refreshLogs(){try{const response=await fetch('{{ url_for("recent_logs_api") }}?limit='+logLimitSelect.value);const data=await response.json();const box=document.getElementById('logs');const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<30;box.textContent=data.logs.join('\n')||'No logs yet';if(nearBottom)box.scrollTop=box.scrollHeight;}catch(e){}}
let statusBusy=false;
let lastStatusUpdate=null;
function renderLastUpdated(){
    if(lastStatusUpdate===null)return;
    const label=document.getElementById('status-updated');
    const elapsed=Math.max(0,Math.floor((Date.now()-lastStatusUpdate.getTime())/1000));
    const date=[lastStatusUpdate.getFullYear(),lastStatusUpdate.getMonth()+1,lastStatusUpdate.getDate()].map(value=>String(value).padStart(2,'0')).join('-');
    const time=[lastStatusUpdate.getHours(),lastStatusUpdate.getMinutes(),lastStatusUpdate.getSeconds()].map(value=>String(value).padStart(2,'0')).join(':');
    label.textContent=`Last updated: ${date} ${time} (${elapsed}s ago)`;
    label.title=lastStatusUpdate.toLocaleString();
}
const fromValue=document.getElementById('from-value');
function clearIndicators(){for(let i=1;i<=4;i++){const lamp=document.getElementById(`status-light-${i}`);lamp.classList.remove('on','off');lamp.classList.add('unknown');}}
function showIndicators(values){values.forEach((isOn,index)=>{const lamp=document.getElementById(`status-light-${index+1}`);lamp.classList.remove('unknown');lamp.classList.toggle('on',isOn);lamp.classList.toggle('off',!isOn);});}
async function refreshStatus(){
    if(statusBusy)return;
    statusBusy=true;
    const label=document.getElementById('device-status');
    const updatedLabel=document.getElementById('status-updated');
    try{
        const response=await fetch('{{ url_for("device_status_api") }}');
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Unknown error');
        deviceRunning=!!data.running;
        deviceRemote=!!data.remote;
        devicePressure=data.pressure_value??null;
        deviceAlarm=!!data.alarm;
        deviceStatusKnown=true;
        fillTo();
        updateScheduleButton();
        updateRegulationButtons();
        updateDeviceWarning();
        label.textContent='';
        label.hidden=true;
        showIndicators(data.indicators);
        fromValue.textContent=data.pressure_value===null?'—':data.pressure_value;
        updateRampPlan();
        lastStatusUpdate=new Date(data.updated_at);
        renderLastUpdated();
        updatedLabel.classList.remove('stale');
    }catch(e){
        deviceRunning=false;
        deviceRemote=false;
        devicePressure=null;
        deviceAlarm=false;
        deviceStatusKnown=false;
        fromValue.textContent='—';
        updateRampPlan();
        updateScheduleButton();
        updateRegulationButtons();
        updateDeviceWarning();
        clearIndicators();
        // fetch() raises TypeError when this control panel itself cannot be reached.
        label.textContent=e instanceof TypeError?'Control panel server is not responding.':'Status error: '+e.message;
        label.hidden=false;
        updatedLabel.classList.add('stale');
    }finally{statusBusy=false;}
}
refreshLogs();setInterval(refreshLogs,2000);refreshStatus();setInterval(refreshStatus,2000);setInterval(renderLastUpdated,1000);
const chartWrap=document.getElementById('schedule-chart-wrap');
const chartBox=document.getElementById('schedule-chart');
const chartTip=document.getElementById('schedule-chart-tip');
const presentRow=document.getElementById('schedule-present');
const nextRow=document.getElementById('schedule-next');
let chartRows=[],chartRevision=null,chartLoading=null,chartMarker=null,chartNext=null,chartHover=null;
function chartDuration(ms){
    const total=Math.max(0,Math.ceil(ms/1000)),d=Math.floor(total/86400),h=Math.floor(total/3600)%24,m=Math.floor(total/60)%60,s=total%60;
    if(d)return `${d}d ${h}h`;
    if(h)return `${h}h ${m}m`;
    return m?`${m}m ${s}s`:`${s}s`;
}
// Next counts down, so it is rendered every second from what the last poll said.
function renderNext(){
    nextRow.hidden=!chartNext;
    document.getElementById('schedule-points').hidden=presentRow.hidden&&nextRow.hidden;
    if(!chartNext)return;
    document.getElementById('next-time').textContent=`in ${chartDuration(chartNext[0]-Date.now())}`;
    document.getElementById('next-value').textContent=chartNext[1].toFixed(2);
}
function tickScheduleClock(){
    if(scheduleState==='running'&&chartMarker!==null&&chartRows.length>=2){
        const first=chartRows[0][0],last=chartRows[chartRows.length-1][0];
        chartMarker=Math.max(first,Math.min(Date.now(),last));
        drawChart();
    }else{
        renderNext();
    }
}
setInterval(tickScheduleClock,1000);
function chartStamp(ms){
    const d=new Date(ms);
    return `${d.getFullYear()}-${chartPad(d.getMonth()+1)}-${chartPad(d.getDate())} ${chartPad(d.getHours())}:${chartPad(d.getMinutes())}:${chartPad(d.getSeconds())}`;
}
function chartPad(value){return String(value).padStart(2,'0');}
function chartTime(ms,span){
    const d=new Date(ms);
    const clock=`${chartPad(d.getHours())}:${chartPad(d.getMinutes())}`;
    if(span<3600000)return `${clock}:${chartPad(d.getSeconds())}`;
    return span<86400000?clock:`${chartPad(d.getMonth()+1)}-${chartPad(d.getDate())} ${clock}`;
}
// The setpoint holds from one row to the next, so a time belongs to the last row at or before it.
function chartRowAt(time){
    let low=0,high=chartRows.length-1;
    while(low<high){const mid=Math.ceil((low+high)/2);if(chartRows[mid][0]<=time)low=mid;else high=mid-1;}
    return low;
}
function chartScale(){
    const width=chartBox.clientWidth||640,height=190,left=46,right=12,top=24,bottom=24;
    const first=chartRows[0][0],last=chartRows[chartRows.length-1][0];
    let low=chartRows.reduce((m,row)=>Math.min(m,row[1]),Infinity),high=chartRows.reduce((m,row)=>Math.max(m,row[1]),-Infinity);
    // The current setpoint may lie outside the plan, and must stay on the chart.
    if(chartMarker!==null&&devicePressure!==null){low=Math.min(low,devicePressure);high=Math.max(high,devicePressure);}
    if(low===high){low-=1;high+=1;}
    const rough=(high-low)/3,size=10**Math.floor(Math.log10(rough));
    const step=[1,2,5,10].find(m=>m*size>=rough)*size;
    low=Math.floor(low/step)*step;high=Math.ceil(high/step)*step;
    return {width,height,left,right,top,bottom,first,last,low,high,step,
            x:time=>left+(time-first)/((last-first)||1)*(width-left-right),
            y:value=>top+(high-value)/(high-low)*(height-top-bottom)};
}
function drawChart(){
    presentRow.hidden=chartRows.length<2||chartMarker===null;
    renderNext();
    if(chartRows.length<2){chartWrap.hidden=true;return;}
    chartWrap.hidden=false;
    const s=chartScale(),span=s.last-s.first,bottom=s.height-s.bottom,parts=[];
    for(let v=s.low;v<=s.high+s.step/2;v+=s.step){
        const label=Number(v.toFixed(6)),y=s.y(v);
        parts.push(`<line x1="${s.left}" x2="${s.width-s.right}" y1="${y}" y2="${y}" stroke="#e3e7eb"/><text x="${s.left-6}" y="${y+4}" text-anchor="end" class="tick">${label}</text>`);
    }
    parts.push(`<text x="${s.left-6}" y="9" text-anchor="end" class="tick">hPa</text>`);
    [[s.first,'start'],[s.first+span/2,'middle'],[s.last,'end']].forEach(([time,anchor])=>{
        parts.push(`<text x="${s.x(time)}" y="${s.height-6}" text-anchor="${anchor}" class="tick">${chartTime(time,span)}</text>`);
    });
    let path=`M${s.x(chartRows[0][0])} ${s.y(chartRows[0][1])}`;
    let held=chartRows[0][1];
    for(const [time,value] of chartRows){if(value!==held){path+=`H${s.x(time)}V${s.y(value)}`;held=value;}}
    path+=`H${s.x(s.last)}`;
    if(chartMarker===null){
        parts.push(`<path d="${path}" class="done"/>`);
    }else{
        const mx=s.x(chartMarker),value=devicePressure!==null?devicePressure:chartRows[chartRowAt(chartMarker)][1],my=s.y(value);
        parts.push(`<clipPath id="chart-past"><rect x="0" y="0" width="${mx}" height="${s.height}"/></clipPath><clipPath id="chart-future"><rect x="${mx}" y="0" width="${s.width-mx}" height="${s.height}"/></clipPath>`);
        parts.push(`<path d="${path}" class="rest" clip-path="url(#chart-future)"/><path d="${path}" class="done" clip-path="url(#chart-past)"/>`);
        parts.push(`<line x1="${mx}" x2="${mx}" y1="${s.top}" y2="${bottom}" class="marker"/><circle cx="${mx}" cy="${my}" r="4.5" class="dot"/>`);
        // The table under the chart names these two marks, in the columns of the schedule file.
        document.getElementById('present-time').innerHTML=chartStamp(chartMarker).split(' ').map(part=>`<span>${part}</span>`).join(' ');
        document.getElementById('present-value').textContent=Number(value).toFixed(2);
        if(chartNext)parts.push(`<circle cx="${s.x(chartNext[0])}" cy="${s.y(chartNext[1])}" r="4.5" class="next"/>`);
    }
    if(chartHover!==null){
        const hx=s.x(Math.max(chartRows[chartHover][0],s.first));
        parts.push(`<line x1="${hx}" x2="${hx}" y1="${s.top}" y2="${bottom}" class="cross"/><circle cx="${hx}" cy="${s.y(chartRows[chartHover][1])}" r="4" class="hover"/>`);
    }
    chartBox.innerHTML=`<svg viewBox="0 0 ${s.width} ${s.height}" aria-hidden="true">${parts.join('')}</svg>`;
    chartBox.setAttribute?.('aria-label',`Pressure from ${chartRows[0][1]} to ${chartRows[chartRows.length-1][1]} hPa, ${chartRows.length} steps, from ${chartTime(s.first,span)} to ${chartTime(s.last,span)}.`);
}
async function loadChartRows(revision){
    if(chartLoading===revision)return;
    chartLoading=revision;
    try{
        const response=await fetch('{{ url_for("schedule_rows_api") }}');
        const data=await response.json();
        if(!response.ok)return;
        chartRows=data.rows.map(([time,value])=>[new Date(time).getTime(),value]);
        chartHover=null;chartTip.hidden=true;
        chartRevision=data.revision;
        drawChart();
    }catch(e){
        // The chart is an extra view; the next poll tries again.
    }finally{chartLoading=null;}
}
function updateChart(data){
    chartMarker=data.marker_time?new Date(data.marker_time).getTime():null;
    chartNext=data.datetime&&isFinite(parseFloat(data.pressure))?[new Date(data.datetime.replace(' ','T')).getTime(),parseFloat(data.pressure)]:null;
    // A one-step Now has no shape to draw.
    if(!data.name||data.kind==='now'){chartRows=[];chartRevision=null;chartHover=null;chartTip.hidden=true;chartWrap.hidden=true;presentRow.hidden=true;}
    else if(data.revision!==chartRevision)loadChartRows(data.revision);
    else drawChart();
}
chartBox.addEventListener('pointermove',event=>{
    if(chartRows.length<2)return;
    const box=chartBox.getBoundingClientRect(),s=chartScale(),px=event.clientX-box.left;
    const fraction=Math.min(1,Math.max(0,(px-s.left)/(s.width-s.left-s.right)));
    const index=chartRowAt(s.first+fraction*(s.last-s.first));
    if(index!==chartHover){chartHover=index;drawChart();}
    const row=chartRows[index];
    chartTip.textContent=`${chartTime(row[0],s.last-s.first)} — ${row[1]} hPa`;
    chartTip.hidden=false;
    chartTip.style.left=`${Math.max(0,Math.min(box.width-chartTip.offsetWidth,px-chartTip.offsetWidth/2))}px`;
});
chartBox.addEventListener('pointerleave',()=>{chartHover=null;chartTip.hidden=true;drawChart();});
window.addEventListener('resize',drawChart);
// SCHEDULE is this app's own flag, shown with the device's. It is not a bit of the device status.
function showScheduleFlag(value){
    const lamp=document.getElementById('status-light-schedule');
    ['on','off','unknown'].forEach(name=>lamp.classList.toggle(name,name===value));
}
function showSchedule(data){
    document.getElementById('scheduler-state').textContent=data.state||'unknown';
    showScheduleFlag(data.state==='running'?'on':'off');
    scheduleState=data.state;
    scheduleLoaded=!!data.name;
    scheduleSource=data.source;
    scheduleNeedsRegulation=data.needs_regulation!==false;
    scheduleHasUpcoming=data.has_upcoming!==false;
    document.getElementById('schedule-revision').value=data.revision;
    if(!csvLoading&&!(csvInput.files&&csvInput.files[0]))selectedCsv.textContent=data.source==='file'?data.name:'';
    updateScheduleButton();
    document.getElementById('schedule-name').textContent=data.name||'None';
    document.getElementById('schedule-kind').textContent={file:'File',ramp:'Ramp',now:'Now'}[data.kind]||'Schedule';
    document.getElementById('schedule-name-line').hidden=!data.name;
    document.getElementById('schedule-state-line').hidden=!data.name;
    const errorBox=document.getElementById('schedule-error');
    const problems=[];
    // A halt is its own message; only failed sends have a count.
    if(data.error)problems.push(data.failed_count?`Last schedule error (${data.failed_count} failed): ${data.error}`:data.error);
    if(data.skipped_count)problems.push(`${data.skipped_count} late step(s) skipped (more than ${data.max_lateness_seconds} seconds late).`);
    errorBox.hidden=problems.length===0;
    errorBox.textContent=problems.join(' ');
    const nextSchedule=document.getElementById('next-schedule');
    document.getElementById('schedule-csv').hidden=!data.name;
    updateChart(data);
    nextSchedule.textContent=chartNext?'':(data.message||'');
    nextSchedule.hidden=!nextSchedule.textContent;
    renderNext();
    document.getElementById('schedule-section').classList.toggle('running',data.state==='running'&&!!data.datetime);
    updateRegulationButtons();
    updateDeviceWarning();
}
let scheduleBusy=false;
async function refreshNextSchedule(){
    if(csvLoading||scheduleBusy)return;
    scheduleBusy=true;
    const revision=scheduleRevision;
    try{
        const response=await fetch('{{ url_for("schedule_api") }}');
        const data=await response.json();
        if(csvLoading||revision!==scheduleRevision)return;
        if(!response.ok)throw new Error(data.error||'Unknown error');
        showSchedule(data);
    }catch(e){
        if(!csvLoading&&revision===scheduleRevision){
            const nextSchedule=document.getElementById('next-schedule');
            chartNext=null;
            renderNext();
            showScheduleFlag('unknown');
            nextSchedule.textContent='Schedule status unavailable: '+e.message;
            nextSchedule.hidden=false;
            document.getElementById('schedule-section').classList.remove('running');
        }
    }finally{scheduleBusy=false;}
}
refreshNextSchedule();setInterval(refreshNextSchedule,2000);
</script>
</main></body></html>'''


class ScheduleHalted(Exception):
    """The device is not regulating, so a setpoint cannot change the pressure."""


class ScheduleCancelled(Exception):
    pass


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ScheduleCancelled("Schedule was stopped")


class DeviceCommunicationError(RuntimeError):
    """A device HTTP exchange failed; the caller decides whether to continue."""


class DeviceTimeoutError(DeviceCommunicationError):
    pass


class DeviceHTTPError(DeviceCommunicationError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Unexpected device HTTP status: {status_code}")


async def _request_device_async(url, method, cancel_event, deadline, timeout):
    # Trio can abandon a stalled DNS wait when cancelled. The OS lookup may
    # finish later, but its result cannot continue this cancelled HTTP request.
    async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
        _check_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeviceTimeoutError(f"Device communication timed out after {timeout:g} seconds")
        response = None
        error = None

        async def watch_cancellation(scope):
            while not cancel_event.is_set():
                await trio.sleep(0.05)
            scope.cancel()

        with trio.move_on_after(remaining) as deadline_scope:
            async with trio.open_nursery() as nursery:
                if cancel_event is not None:
                    nursery.start_soon(watch_cancellation, nursery.cancel_scope)
                try:
                    if method == "POST":
                        print(f"Command: {method} {url}", flush=True)
                    response = await client.request(method, url)
                except Exception as exc:
                    # Raise outside the nursery to preserve the public exception type.
                    error = exc
                finally:
                    nursery.cancel_scope.cancel()
        _check_cancelled(cancel_event)
        if deadline_scope.cancelled_caught or time.monotonic() > deadline:
            raise DeviceTimeoutError(f"Device communication timed out after {timeout:g} seconds")
        if error is not None:
            raise error
        if method == "POST" or DEBUG_MODE:
            logging.log(logging.INFO if method == "POST" else logging.DEBUG,
                        "HTTP method=%s url=%s status=%d output=%s",
                        method, url, response.status_code, response.text[-2000:])
        if not 200 <= response.status_code < 300:
            raise DeviceHTTPError(response.status_code)
        return response.text.strip()


def _request_device(url, *, method="GET", cancel_event=None, timeout=10.0):
    _check_cancelled(cancel_event)
    deadline = time.monotonic() + timeout
    try:
        return trio.run(_request_device_async, url, method, cancel_event, deadline, timeout)
    except httpx.TimeoutException as exc:
        raise DeviceTimeoutError(f"Device communication timed out: {exc}") from exc
    except (httpx.RequestError, httpx.InvalidURL, OSError) as exc:
        raise DeviceCommunicationError(f"Device communication failed: {exc}") from exc


def send_command(url: str, cancel_event: threading.Event | None = None, *, timeout=10.0) -> str:
    return _request_device(url, method="POST", cancel_event=cancel_event, timeout=timeout)


def get_device_status(*, timeout=10.0, cancel_event=None) -> dict:
    body = _request_device(DEVICE_URL + "Get_Latest_Data", timeout=timeout, cancel_event=cancel_event)
    # Device output may contain diagnostics; its final non-empty line is JSON.
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The device returned an empty response")
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise ValueError("The device returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("The device response must be a JSON object")
    raw_status = payload.get("APC_STATUS_PV")
    if isinstance(raw_status, bool) or not isinstance(raw_status, (str, int)):
        raise ValueError("APC_STATUS_PV must be an integer")
    try:
        status = int(raw_status)
    except ValueError as exc:
        raise ValueError("APC_STATUS_PV must be an integer") from exc
    if not 0 <= status <= 15:
        raise ValueError("APC_STATUS_PV must be between 0 and 15")
    bits = [bool(status & (1 << shift)) for shift in (3, 2, 1, 0)]
    pressure_value = payload.get("APC_P_SV")
    if pressure_value is not None:
        if isinstance(pressure_value, bool):
            raise ValueError("APC_P_SV must be a finite number")
        try:
            pressure_value = float(pressure_value)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("APC_P_SV must be a finite number") from exc
        if not math.isfinite(pressure_value):
            raise ValueError("APC_P_SV must be a finite number")
    return {"status": status, "binary": format(status, "04b"),
            "running": bool(status & RUN_BIT), "alarm": bool(status & ALARM_BIT), "remote": bool(status & REMOTE_BIT),
            "indicators": bits, "pressure_value": pressure_value}


def _describe_readback(status):
    names = {"status": "APC_STATUS_PV", "pressure_value": "APC_P_SV"}
    return ", ".join(f"{name}={status[key]}" for key, name in names.items() if key in status)


def wait_for_status(predicate, deadline, description, *, cancel_event=None, require_remote=False):
    last_error = None
    last_status = None
    while True:
        _check_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = ""
            if last_error:
                detail = f" Last status error: {last_error}"
            elif last_status:
                detail = f" Last readback: {_describe_readback(last_status)}"
            raise RuntimeError(f"{description} was not confirmed within {CONFIRM_TIMEOUT:g} seconds "
                               f"after sending the command.{detail}")
        try:
            status = get_device_status(timeout=remaining, cancel_event=cancel_event)
        except ScheduleCancelled:
            raise
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            last_error = str(exc)
        else:
            last_error = None
            last_status = status
            _check_cancelled(cancel_event)
            if require_remote and not status["status"] & REMOTE_BIT:
                raise RuntimeError("REMOTE flag was cleared before confirmation")
            if time.monotonic() <= deadline and predicate(status):
                return status
        delay = min(0.2, max(0, deadline - time.monotonic()))
        if cancel_event is None:
            time.sleep(delay)
        else:
            cancel_event.wait(delay)


def regulate(action):
    if action == "off":
        # Keep activation and Off mutually exclusive until Off is confirmed.
        with scheduler.lock:
            if scheduler.state == "running":
                raise RuntimeError("Stop the schedule before turning Regulation off.")
            return _regulate(action)
    return _regulate(action)


def _regulate(action):
    """Turn regulation "on" or "off" with one device pulse, confirmed by the RUN flag.

    Returns False if it was already as requested.
    """
    initial = get_device_status(timeout=CONFIRM_TIMEOUT)
    if not initial["status"] & REMOTE_BIT:
        raise RuntimeError("REMOTE flag is off, so Regulation cannot be changed.")
    expected = action == "on"
    if expected and initial["status"] & ALARM_BIT:
        raise RuntimeError(ALARM_REFUSED_MESSAGE)
    if not expected:
        # No schedule is running under regulate()'s lock. Invalidate a Go
        # that already read RUN=ON before this Off request acquired the lock.
        scheduler.stop()
    if bool(initial["status"] & RUN_BIT) == expected:
        return False
    deadline = time.monotonic() + CONFIRM_TIMEOUT
    # These are pulse commands; never poll/rewrite the server's self-resetting SV fields.
    send_command(START_URL if expected else STOP_URL, timeout=max(0.001, deadline - time.monotonic()))
    wait_for_status(lambda status: bool(status["status"] & RUN_BIT) == expected,
                    deadline, "RUN flag ON" if expected else "RUN flag OFF", require_remote=True)
    return True


def value_url(value: str) -> str:
    # Restrict to numeric values so submitted input cannot alter the URL/query.
    try:
        # float() also accepts "1_000", which is not a normal pressure entry.
        if "_" in value:
            raise ValueError
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"Pressure must be a number in hPa (got {value!r})") from exc
    if not math.isfinite(number):
        raise ValueError(f"Pressure must be a finite number in hPa (got {value!r})")
    if not PRESSURE_MIN_HPA <= number <= PRESSURE_MAX_HPA:
        raise ValueError(f"Pressure must be from {PRESSURE_MIN_HPA:g} to {PRESSURE_MAX_HPA:g} hPa (got {value})")
    normalized = str(number) if "." in value or "e" in value.lower() else str(int(number))
    return DEVICE_URL + "Set_Data?" + urlencode({PARAMETER: normalized})


def send_value(value: str, cancel_event: threading.Event | None = None, *,
               deadline: float | None = None, require_regulation: bool = False) -> str:
    url = value_url(value)
    _check_cancelled(cancel_event)
    if not setpoint_lock.acquire(blocking=False):
        raise RuntimeError("Another setpoint command is awaiting confirmation")
    try:
        if deadline is None:
            deadline = time.monotonic() + CONFIRM_TIMEOUT
        send_command(url, cancel_event=cancel_event, timeout=max(0.001, deadline - time.monotonic()))
        expected = float(value)

        def confirmed_setpoint(status):
            raw = status.get("status")
            if raw is not None:
                if require_regulation and not raw & RUN_BIT:
                    raise ScheduleHalted(REGULATION_OFF_MESSAGE)
                if not raw & REMOTE_BIT:
                    if require_regulation:
                        raise ScheduleHalted(REMOTE_OFF_MESSAGE)
                    raise RuntimeError("REMOTE flag was cleared before confirmation")
                if require_regulation and raw & ALARM_BIT:
                    raise ScheduleHalted(ALARM_ON_MESSAGE)
            return (status.get("pressure_value") is not None and
                    abs(Decimal(str(status["pressure_value"])) - Decimal(str(expected))) <= SETPOINT_TOLERANCE_HPA)

        confirmed = wait_for_status(confirmed_setpoint, deadline, f"APC_P_SV={expected}", cancel_event=cancel_event)
        logging.info("Setpoint readback confirmed: requested=%s readback=%s tolerance=%s hPa (not a pressure measurement)",
                     expected, confirmed["pressure_value"], SETPOINT_TOLERANCE_HPA)
        return url
    finally:
        setpoint_lock.release()


def send_row(value: str, cancel_event: threading.Event | None = None) -> str:
    """Send one schedule row, unless the device is known not to accept it."""
    deadline = time.monotonic() + CONFIRM_TIMEOUT
    current = None
    try:
        device_status = get_device_status(timeout=CONFIRM_TIMEOUT, cancel_event=cancel_event)
        status = device_status["status"]
        current = device_status.get("pressure_value")
    except ScheduleCancelled:
        raise
    except Exception as exc:
        # An unreadable status is not "off"; the send and its readback report real trouble.
        logging.info("Could not check the device before a schedule row: %s", exc)
        status = RUN_BIT | REMOTE_BIT
    if not status & RUN_BIT:
        raise ScheduleHalted(REGULATION_OFF_MESSAGE)
    if not status & REMOTE_BIT:
        raise ScheduleHalted(REMOTE_OFF_MESSAGE)
    if status & ALARM_BIT:
        raise ScheduleHalted(ALARM_ON_MESSAGE)
    if current is not None:
        target = float(value)
        if abs(Decimal(str(target)) - Decimal(str(current))) > Decimal(str(PRESSURE_MAX_STEP_HPA)):
            raise ScheduleHalted(
                f"Step from {current:g} to {target:g} hPa exceeds the {PRESSURE_MAX_STEP_HPA:g} hPa limit. Schedule stopped."
            )
    return send_value(value, cancel_event=cancel_event, deadline=deadline, require_regulation=True)


def _parse_schedule_datetime(text):
    try:
        when = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid datetime {text!r}; use YYYY-MM-DD HH:MM:SS") from exc
    if when.tzinfo is not None:
        raise ValueError("use local times without a timezone")
    return when


def schedule_csv_text(rows) -> str:
    """Return (datetime, setpoint text) rows in the format read_schedule accepts."""
    lines = [CSV_HEADER]
    lines += [f"{when.date().isoformat()},{when.time().isoformat()},{value}" for when, value in rows]
    return "\n".join(lines) + "\n"


def read_schedule(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return parse_schedule(f)


def parse_schedule(f) -> list[dict]:
    """Validate schedule rows from a text stream. Every source comes through here."""
    result = []
    timestamps = set()
    n = 0
    try:
        reader = csv.DictReader(f, skipinitialspace=True)
        if not reader.fieldnames:
            raise ValueError("CSV is missing a header row")
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        if len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError("CSV contains duplicate column names")
        split_columns = set(CSV_COLUMNS).issubset(reader.fieldnames)
        legacy_columns = {CSV_DATETIME_COLUMN, CSV_PRESSURE_COLUMN}
        if not split_columns and not legacy_columns.issubset(reader.fieldnames):
            raise ValueError("CSV must contain date, time, and pressure columns (or datetime and P (hPa) columns)")
        for raw in reader:
            n = reader.line_num
            if None in raw:
                raise ValueError(f"CSV line {n}: too many columns")
            row = {k.strip(): (v or "").strip() for k, v in raw.items()}
            if not any(row.values()):
                continue
            if split_columns:
                row[CSV_DATETIME_COLUMN] = f"{row['date']} {row['time']}"
                row[CSV_PRESSURE_COLUMN] = row["pressure"]
            try:
                when = _parse_schedule_datetime(row[CSV_DATETIME_COLUMN])
                value_url(row[CSV_PRESSURE_COLUMN])
                if when in timestamps:
                    raise ValueError("duplicate datetime")
            except (ValueError, OverflowError) as exc:
                raise ValueError(f"CSV line {n}: {exc}") from exc
            timestamps.add(when)
            row.update(_line=str(n), _when=when)
            result.append(row)
    except UnicodeDecodeError as exc:
        raise ValueError("CSV must be saved as UTF-8 text") from exc
    except csv.Error as exc:
        raise ValueError(f"CSV line {n + 1}: {exc}") from exc
    if not result:
        raise ValueError("CSV contains no schedule rows")
    return sorted(result, key=lambda row: row["_when"])


class ScheduleController:
    """One in-memory schedule, advanced only by the single worker thread."""

    def __init__(self, sender, unregulated_sender=None):
        self.sender = sender
        self.unregulated_sender = unregulated_sender or sender
        self.needs_regulation = True
        self.plan = None  # Source set: (target, seconds). Rows are rebuilt from these at Start.
        self.lock = threading.RLock()
        self.path = None
        self.display_name = None
        self.source = None
        self.rows = ()
        self.index = 0
        self.event = None
        self.state = "idle"
        self.error = None
        self.failed_count = 0
        self.skipped_count = 0
        self.revision = 0

    def _activate(self, now):
        if self.event is not None:
            self.event.set()
        self.event = threading.Event()
        self.revision += 1
        # On resume, consume expired entries instead of replaying a backlog.
        while self.index < len(self.rows) and self.rows[self.index]["_when"] < now:
            self.index += 1
        self.error = None
        self.state = "running" if self.index < len(self.rows) else "completed"

    def start(self, path, rows, now=None, display_name=None):
        with self.lock:
            self.path = path
            self.display_name = display_name or path.name
            self.source = SOURCE_FILE if path is not None else SOURCE_SET
            self.rows = tuple(dict(row) for row in rows)
            self.index = 0
            self.failed_count = 0
            self.skipped_count = 0
            self._activate(now or datetime.now())

    def load(self, path, rows, display_name=None, needs_regulation=True, plan=None):
        with self.lock:
            if self.state == "running":
                raise RuntimeError("Stop the running schedule before loading another schedule.")
            if self.event is not None:
                self.event.set()
            self.path = path
            self.display_name = display_name or path.name
            self.source = SOURCE_FILE if path is not None else SOURCE_SET
            self.needs_regulation = needs_regulation
            self.plan = plan
            self.rows = tuple(dict(row) for row in rows)
            self.index = 0
            self.event = None
            self.error = None
            self.failed_count = 0
            self.skipped_count = 0
            self.state = "ready"
            self.revision += 1

    def resume(self, now=None, expected_revision=None):
        with self.lock:
            if expected_revision is not None and expected_revision != self.revision:
                raise RuntimeError("Schedule changed. Check the schedule and press Go again.")
            if not self.rows:
                raise ValueError("Create a schedule first: choose Ramp or File and press Plan.")
            if self.state == "running":
                return
            if self.state == "ready":
                self.start(self.path, self.rows, now=now, display_name=self.display_name)
                return
            self._activate(now or datetime.now())

    def clear(self):
        """Discard a held non-running schedule."""
        with self.lock:
            if self.state == "running":
                raise RuntimeError("Stop the running schedule before loading another schedule.")
            if self.event is not None:
                self.event.set()
            self.path = None
            self.display_name = None
            self.source = None
            self.needs_regulation = True
            self.plan = None
            self.rows = ()
            self.index = 0
            self.event = None
            self.error = None
            self.failed_count = 0
            self.skipped_count = 0
            self.state = "idle"
            self.revision += 1

    def stop(self):
        """Stop sending. Returns True if a schedule was running."""
        with self.lock:
            was_running = self.state == "running"
            self.revision += 1  # Also refuses a Go still waiting for the device.
            if self.event is not None:
                self.event.set()
            if was_running:
                self.state = "stopped"
            return was_running

    def halt(self, message, revision=None):
        """Stop a running schedule and report why. A stale `revision` does nothing."""
        with self.lock:
            if (self.state != "running" or not self.needs_regulation
                    or (revision is not None and revision != self.revision)):
                return False
            self.revision += 1
            if self.event is not None:
                self.event.set()
            self.state = "stopped"
            self.error = message
            logging.warning("%s", message)
            return True

    def csv_text(self):
        with self.lock:
            return schedule_csv_text((row["_when"], row[CSV_PRESSURE_COLUMN]) for row in self.rows)

    def report_internal_error(self, exc):
        with self.lock:
            self.error = f"Internal schedule error: {exc}"

    def chart_rows(self):
        with self.lock:
            return {"revision": self.revision,
                    "rows": [[row["_when"].isoformat(), float(row[CSV_PRESSURE_COLUMN])] for row in self.rows]}

    def _marker_time(self, now):
        """Where the schedule stands on its time axis, or None. Hold the lock."""
        if not self.rows:
            return None
        first, last = self.rows[0]["_when"], self.rows[-1]["_when"]
        if self.state == "completed":
            return last
        if self.state == "stopped":
            return self.rows[self.index - 1]["_when"] if self.index else first
        if self.state == "ready" and self.plan:
            return first  # A ramp's times are only placeholders until Go.
        if self.state in ("running", "ready"):
            return max(first, min(now, last))
        return None

    def snapshot(self):
        with self.lock:
            now = datetime.now()
            has_upcoming = bool(
                self.plan
                or (self.state == "running" and self.index < len(self.rows))
                or (self.state in ("ready", "stopped")
                    and any(row["_when"] >= now for row in self.rows[self.index:]))
            )
            result = {"state": self.state, "error": self.error, "failed_count": self.failed_count,
                      "skipped_count": self.skipped_count,
                      "max_lateness_seconds": int(SCHEDULE_MAX_LATENESS.total_seconds()),
                      "name": self.display_name, "source": self.source, "revision": self.revision,
                      "needs_regulation": self.needs_regulation, "has_upcoming": has_upcoming}
            # What kind of plan this is, in the words of the Pressure panel.
            result["kind"] = (None if not self.display_name else "file" if self.source == SOURCE_FILE
                              else "ramp" if self.needs_regulation else "now")
            messages = {"idle": "No schedule.",
                        # State has its own line, so these say only what to do next.
                        "ready": "" if self.plan else "No upcoming steps in this file.",
                        "stopped": (("Restart continues with the remaining steps."
                                     if has_upcoming else "No upcoming steps in this file.")
                                    if self.source == SOURCE_FILE else
                                    "Go begins again from the current setpoint.")}
            result["message"] = messages.get(self.state, "")
            result["total_rows"] = len(self.rows)
            marker = self._marker_time(now)
            result["marker_time"] = marker.isoformat() if marker else None
            index = None
            if self.state == "ready" and not self.plan:
                index = next((i for i, row in enumerate(self.rows) if row["_when"] >= now), None)
            elif self.state == "running" and self.index < len(self.rows):
                index = self.index
            if index is not None:
                row = self.rows[index]
                result.update(datetime=row["_when"].strftime("%Y-%m-%d %H:%M:%S"), pressure=row[CSV_PRESSURE_COLUMN],
                              position=index + 1)
            return result

    def _skip_stale_rows(self, current):
        """Skip rows too late to send, always keeping the newest due row. Hold the lock."""
        first = self.index
        cutoff = current - SCHEDULE_MAX_LATENESS
        while (self.index + 1 < len(self.rows) and self.rows[self.index]["_when"] < cutoff
               and self.rows[self.index + 1]["_when"] <= current):
            self.index += 1
        skipped = self.index - first
        if skipped:
            self.skipped_count += skipped
            logging.warning("Skipped %d late schedule rows (CSV lines %s to %s); more than %d seconds late",
                            skipped, self.rows[first]["_line"], self.rows[self.index - 1]["_line"],
                            SCHEDULE_MAX_LATENESS.total_seconds())

    def tick(self, now=None):
        while True:
            with self.lock:
                if self.state != "running":
                    return
                event = self.event
                current = now or datetime.now()
                if self.rows[self.index]["_when"] > current:
                    return
                self._skip_stale_rows(current)
                row = self.rows[self.index]
                self.index += 1
                sender = self.sender if self.needs_regulation else self.unregulated_sender
            try:
                sender(row[CSV_PRESSURE_COLUMN], cancel_event=event)
            except ScheduleCancelled:
                return
            except ScheduleHalted as exc:
                with self.lock:
                    # The read may predate a Stop and a new Go; that run is not affected.
                    if self.event is event and self.state == "running":
                        # Restart sends this row again; it may have been sent if the readback stopped it.
                        self.index -= 1
                        self.halt(str(exc))
                return
            except Exception as exc:
                with self.lock:
                    active = self.event is event and self.state == "running" and not event.is_set()
                    if active:
                        self.error = f"CSV line {row['_line']}: {exc}"
                        self.failed_count += 1
                logging.log(logging.ERROR if active else logging.INFO,
                            "Scheduled command failed at CSV line %s: %s", row["_line"], exc)
            with self.lock:
                if self.event is not event or event.is_set():
                    return
                if self.index == len(self.rows):
                    self.state = "completed"
                    return


def format_setpoint(value):
    return repr(float(round(value, 2)))


def plan_ramp(start, target, duration, step_seconds):
    """Return (seconds after start, setpoint text) pairs ending exactly at target.

    Rows land every step_seconds. Values have two decimals, matching the
    readback tolerance, and repeated values are left out.
    """
    step_count = max(1, math.ceil(duration / step_seconds))
    plan = []
    previous = format_setpoint(start)
    for step in range(1, step_count):
        text = format_setpoint(start + (target - start) * step / step_count)
        if text != previous:
            plan.append((step * step_seconds, text))
            previous = text
    # The final step sends the entered value without rounding.
    final = repr(target)
    if plan and float(plan[-1][1]) == target:
        plan.pop()
    plan.append((duration, final))
    return plan


scheduler = ScheduleController(send_row, send_value)


def _halt_if_device_stopped(status, revision):
    if not status["running"]:
        scheduler.halt(REGULATION_OFF_MESSAGE, revision)
    elif not status["status"] & REMOTE_BIT:
        scheduler.halt(REMOTE_OFF_MESSAGE, revision)
    elif status.get("alarm"):
        scheduler.halt(ALARM_ON_MESSAGE, revision)


def schedule_worker(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            scheduler.tick()
            snapshot = scheduler.snapshot()
            if (snapshot["state"] == "running" and snapshot["needs_regulation"]
                    and time.monotonic() - _last_status_check_time() >= 2.0):
                try:
                    status = get_device_status(timeout=CONFIRM_TIMEOUT)
                except Exception as exc:
                    _record_status_link(False, exc)
                else:
                    _record_status_link(True)
                    _halt_if_device_stopped(status, snapshot["revision"])
        except Exception as exc:
            # A dead worker would leave the schedule showing "running" forever.
            logging.exception("Schedule worker error; continuing")
            scheduler.report_internal_error(exc)
        stop_event.wait(0.2)


def _wants_json():
    return request.path.startswith("/api/") or request.path == url_for("upload_schedule")


@app.before_request
def reject_foreign_requests():
    # This local panel has no login. Refuse DNS rebinding and cross-site form
    # posts, which would otherwise let any web page send device commands.
    origin = request.headers.get("Origin") if request.method == "POST" else None
    fetch_site = request.headers.get("Sec-Fetch-Site") if request.method == "POST" else None
    if (urlsplit(f"//{request.host}").hostname in LOCAL_HOSTNAMES
            and (not origin or urlsplit(origin).netloc == request.host)
            and fetch_site not in {"cross-site", "same-site"}):
        if request.path in {"/", "/api/status"}:
            browser_activity.touch()
        return None
    logging.warning("Refused %s %s: host=%s origin=%s fetch_site=%s",
                    request.method, request.path, request.host, origin, fetch_site)
    return ("Requests must come from this control panel.", 403)


@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_exc):
    message = f"The CSV upload exceeds the {MAX_UPLOAD_MIB} MiB limit."
    logging.warning("CSV loading refused: %s", message)
    return (jsonify(error=message) if _wants_json() else message), 413


@app.errorhandler(HTTPException)
def http_exception(exc):
    return (jsonify(error=exc.description), exc.code) if _wants_json() else exc


@app.errorhandler(Exception)
def unexpected_exception(exc):
    logging.exception("Unexpected error handling %s %s", request.method, request.path)
    message = f"Internal error: {exc}"
    return (jsonify(error=message) if _wants_json() else message), 500


@app.get("/")
def index():
    schedule = scheduler.snapshot()
    last_to = request.args.get("to")
    if last_to is None:
        last_to = f"{scheduler.plan[0]:g}" if scheduler.plan else ""
    return render_template_string(PAGE, device_url=DEVICE_URL, csv_example_header=", ".join(CSV_COLUMNS),
                                  schedule_name=schedule["name"] or "None",
                                  schedule_kind={"file": "File", "ramp": "Ramp", "now": "Now"}.get(schedule["kind"], "Schedule"),
                                  scheduler_state=schedule["state"],
                                  scheduler_loaded=bool(schedule["name"]),
                                  schedule_source=schedule["source"],
                                  schedule_has_upcoming=schedule["has_upcoming"] if schedule["name"] else True,
                                  source_choice=_source_choice(schedule),
                                  default_ramp=DEFAULT_RAMP_SECONDS,
                                  scheduler_revision=schedule["revision"],
                                  log_limit=LOG_DISPLAY_LIMIT,
                                  log_limit_options=sorted({10, 100, 1000, LOG_DISPLAY_LIMIT}),
                                  confirm_timeout=CONFIRM_TIMEOUT,
                                  ramp_choices=RAMP_CHOICES,
                                  ramp_seconds=_chosen_ramp_seconds(),
                                  ramp_step=RAMP_STEP_SECONDS,
                                  last_to=last_to)


@app.get("/api/health")
def health_api():
    return jsonify(app="chamber-apc-control")


@app.get("/api/logs")
def recent_logs_api():
    try:
        limit = int(request.args.get("limit", LOG_DISPLAY_LIMIT))
    except ValueError:
        limit = LOG_DISPLAY_LIMIT
    limit = max(1, min(1000, limit))
    with recent_logs_lock:
        entries = list(recent_logs)[-limit:]
    return jsonify(logs=entries, limit=limit)


status_link_lock = threading.Lock()
status_link_ok = None
status_link_time = 0.0


def _last_status_check_time():
    with status_link_lock:
        return status_link_time


def _record_status_link(ok, error=None):
    """Log only link changes; the browser polls this every two seconds."""
    global status_link_ok, status_link_time
    with status_link_lock:
        changed = ok != status_link_ok
        status_link_ok = ok
        status_link_time = time.monotonic()
    if changed and ok:
        logging.info("Device status available")
    elif changed:
        logging.warning("Device status unavailable: %s", error)


@app.get("/api/status")
def device_status_api():
    # A schedule started after this read must not be stopped by it.
    revision = scheduler.snapshot()["revision"]
    try:
        status = get_device_status()
    except Exception as exc:
        if DEBUG_MODE:
            logging.exception("Get_Latest_Data processing failed")
        _record_status_link(False, exc)
        return jsonify(error=str(exc)), 502
    _record_status_link(True)
    _halt_if_device_stopped(status, revision)
    status["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return jsonify(status)


@app.get("/api/schedule")
def schedule_api():
    return jsonify(scheduler.snapshot())


@app.get("/api/schedule/rows")
def schedule_rows_api():
    return jsonify(scheduler.chart_rows())


def require_running_device():
    status = get_device_status(timeout=CONFIRM_TIMEOUT)
    if not status["status"] & RUN_BIT:
        raise RuntimeError("Regulation is off. Turn it on before Go or Restart.")
    if not status["status"] & REMOTE_BIT:
        raise RuntimeError(REMOTE_REFUSED_MESSAGE)
    if status["status"] & ALARM_BIT:
        raise RuntimeError(ALARM_REFUSED_MESSAGE)


def _store_upload(uploaded):
    """Save an uploaded file with a unique name, returning its path."""
    safe_name = secure_filename(uploaded.filename) or "schedule.csv"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents simultaneous/same-second uploads overwriting each other.
    with tempfile.NamedTemporaryFile(dir=UPLOAD_DIR, prefix=Path(safe_name).stem[:64] + "_", suffix=".csv", delete=False) as f:
        try:
            uploaded.save(f)
        except Exception:
            Path(f.name).unlink(missing_ok=True)
            raise
    return Path(f.name)


@app.post("/schedule/upload")
def upload_schedule():
    """Source: file. Validate and hold the schedule; Start begins it."""
    uploaded = request.files.get("csv_file")
    if uploaded is None or not uploaded.filename:
        return jsonify(error="Choose a CSV file to load."), 400
    if Path(uploaded.filename).suffix.lower() != ".csv":
        return jsonify(error="Please choose a .csv file."), 400
    display_name = uploaded.filename.replace("\\", "/").rsplit("/", 1)[-1]
    destination = None
    try:
        destination = _store_upload(uploaded)
        rows = read_schedule(destination)
        scheduler.load(destination, rows, display_name=display_name)
    except Exception as exc:
        if destination is not None:
            destination.unlink(missing_ok=True)
        logging.warning("Schedule loading refused: %s", exc)
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        if isinstance(exc, RuntimeError):
            return jsonify(error=str(exc)), 409
        return jsonify(error=f"Could not save the CSV on this PC: {exc}"), 500
    logging.info("Schedule loaded from file (not started): %s (%d rows)", destination, len(rows))
    return jsonify(scheduler.snapshot())


def describe_duration(seconds):
    return f"{seconds // 60} min" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} s"


def _chosen_ramp_seconds():
    """The ramp time to show: from the last OK, else from the schedule that is set."""
    if "ramp" in request.args:
        return request.args.get("ramp", 0, type=int)
    return scheduler.plan[1] if scheduler.plan else 0


def _source_choice(schedule):
    if schedule["source"] == SOURCE_FILE and "to" not in request.args:
        return "file"
    return "ramp" if _chosen_ramp_seconds() else "now"


def parse_ramp_seconds(text):
    try:
        seconds = int(text or 0)
    except ValueError:
        raise ValueError("Ramp time must be a whole number of seconds.") from None
    if seconds != 0 and not RAMP_STEP_SECONDS <= seconds <= RAMP_MAX_SECONDS:
        raise ValueError(f"Ramp time must be 0, or from {RAMP_STEP_SECONDS} to {RAMP_MAX_SECONDS} seconds.")
    return seconds


def _build_set_rows(current, target, seconds, start_time):
    if seconds and not PRESSURE_MIN_HPA <= current <= PRESSURE_MAX_HPA:
        raise ValueError(f"From {current:g} hPa is outside {PRESSURE_MIN_HPA:g} to {PRESSURE_MAX_HPA:g} hPa, "
                         "so a ramp cannot start there. Use Now.")
    text = schedule_csv_text((start_time + timedelta(seconds=offset), row_value)
                             for offset, row_value in plan_ramp(current, target, seconds, RAMP_STEP_SECONDS))
    name = f"from {current:g} hPa to {target:g} hPa"
    rows = parse_schedule(io.StringIO(text, newline=""))
    if seconds:
        name += f" over {describe_duration(seconds)} with {len(rows)} {'step' if len(rows) == 1 else 'steps'}"
    return rows, name


def _current_setpoint(status):
    current = status.get("pressure_value")
    if current is None:
        raise RuntimeError("The device did not report the current APC_P_SV, so a schedule cannot be created.")
    return current


def _at_setpoint(current, target):
    return abs(Decimal(str(current)) - Decimal(str(target))) <= SETPOINT_TOLERANCE_HPA


def set_pressure_schedule(value, seconds):
    """Source: set. Hold a schedule with no file, not started. Returns its name, or None if not needed."""
    target = float(value)
    with scheduler.lock:
        if scheduler.state == "running":
            raise RuntimeError("Stop the running schedule before loading another schedule.")
    current = _current_setpoint(get_device_status(timeout=CONFIRM_TIMEOUT))
    with scheduler.lock:
        if scheduler.state == "running":
            raise RuntimeError("Stop the running schedule before loading another schedule.")
        if _at_setpoint(current, target):
            scheduler.clear()
            return None
        rows, name = _build_set_rows(current, target, seconds, datetime.now().replace(microsecond=0))
        scheduler.load(None, rows, display_name=name, needs_regulation=bool(seconds), plan=(target, seconds))
    logging.info("Schedule set by user (not started): %s (%d rows)", name, len(rows))
    return name


def start_set_plan(target, seconds, *, expected_revision=None):
    """Start the held set schedule from the current setpoint. Returns its name, or None if not needed.

    Rows are built here, because they count from Start and From must be current.
    """
    with scheduler.lock:
        if expected_revision is not None and scheduler.revision != expected_revision:
            raise RuntimeError("Schedule changed. Check the schedule and press Go again.")
        if scheduler.state == "running":
            raise RuntimeError("Stop the running schedule before loading another schedule.")
    status = get_device_status(timeout=CONFIRM_TIMEOUT)
    # The setpoint is set before Regulation starts; only changing pressure over time needs Regulation.
    if seconds and not status["status"] & RUN_BIT:
        raise RuntimeError("Regulation is off. Turn it on before a ramp, or use Now.")
    if not status["status"] & REMOTE_BIT:
        raise RuntimeError(REMOTE_REFUSED_MESSAGE)
    if status["status"] & ALARM_BIT:
        raise RuntimeError(ALARM_REFUSED_MESSAGE)
    current = _current_setpoint(status)
    start_time = datetime.now().replace(microsecond=0)
    with scheduler.lock:
        if expected_revision is not None and scheduler.revision != expected_revision:
            raise RuntimeError("Schedule changed. Check the schedule and press Go again.")
        if scheduler.state == "running":
            raise RuntimeError("Stop the running schedule before loading another schedule.")
        if _at_setpoint(current, target):
            scheduler.clear()
            return None
        rows, name = _build_set_rows(current, target, seconds, start_time)
        scheduler.load(None, rows, display_name=name, needs_regulation=bool(seconds), plan=(target, seconds))
        # The same instant the rows were built from, so a row due now is not skipped as past.
        scheduler.resume(now=start_time, expected_revision=scheduler.revision)
    logging.info("Schedule started by user: %s (%d rows)", name, len(rows))
    return name


@app.get("/schedule/csv")
def view_schedule_csv():
    """Show the current schedule in the file format, whatever its source, as text in the browser."""
    schedule = scheduler.snapshot()
    if not schedule["name"]:
        return "No schedule is loaded.", 404
    name = schedule["name"] if schedule["source"] == SOURCE_FILE else "schedule.csv"
    return Response(scheduler.csv_text(), mimetype="text/plain",
                    headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}",
                             "X-Content-Type-Options": "nosniff"})


def _start_held(form):
    try:
        schedule = scheduler.snapshot()
        try:
            revision = int(form.get("revision", schedule["revision"]))
        except ValueError:
            revision = None
        if revision != schedule["revision"]:
            raise RuntimeError("Schedule changed. Check the schedule and press Go again.")
        if schedule["state"] == "running":
            flash("Schedule is already running.")
            return redirect(url_for("index"))
        if scheduler.plan:
            target, seconds = scheduler.plan
            name = start_set_plan(target, seconds, expected_revision=revision)
            flash(f"Schedule started: {name}" if name else f"Pressure setpoint is already {target:g} hPa. Nothing to send.")
            return redirect(url_for("index"))
        require_running_device()
        was_stopped = schedule["state"] == "stopped"
        scheduler.resume(expected_revision=revision)
        if scheduler.snapshot()["state"] == "completed":
            logging.warning("Schedule has no upcoming rows; nothing to send")
            flash("Nothing to send: every remaining step in the schedule is in the past.", "error")
            return redirect(url_for("index"))
        logging.info("Schedule %s by user", "restarted" if was_stopped else "started")
        flash("Schedule restarted." if was_stopped else "Schedule started.")
    except Exception as exc:
        logging.warning("Schedule start refused: %s", exc)
        flash(f"Could not start schedule: {exc}", "error")
    return redirect(url_for("index"))


@app.post("/schedule/set")
def set_schedule():
    """Pressure's button. Plan (`ramp_seconds`) holds a schedule for Start; OK (none) sends `to` now."""
    value = request.form.get("to", "").strip()
    ramp_text = request.form.get("ramp_seconds", "").strip()
    try:
        seconds = parse_ramp_seconds(ramp_text)
        if not value:
            raise ValueError("Enter the To pressure in hPa.")
        value_url(value)
        # OK runs Now at once. Plan only holds a ramp for Start.
        name = set_pressure_schedule(value, seconds) if seconds else start_set_plan(float(value), 0)
        if name is None:
            flash(f"Pressure setpoint is already {value} hPa. No schedule is needed.")
        elif seconds:
            flash(f"Schedule set: {name}. Press Go to begin.")
        else:
            flash(f"Schedule started: {name}")
    except Exception as exc:
        logging.warning("Schedule set refused: %s", exc)
        flash(f"Could not set schedule: {exc}", "error")
    return redirect(url_for("index", to=value, ramp=ramp_text or None))


@app.post("/schedule/start")
def start_schedule():
    """Start or restart the held schedule."""
    return _start_held(request.form)


@app.post("/schedule/end")
def end_schedule():
    if scheduler.stop():
        logging.info("Schedule stopped by user")
        flash("Schedule stopped. An already accepted device command cannot be undone.")
    else:
        flash("No schedule is running.")
    return redirect(url_for("index"))


@app.post("/regulation/<action>")
def regulation(action: str):
    if action not in {"on", "off"}:
        return "Not found", 404
    if not regulation_lock.acquire(blocking=False):
        flash("Another Regulation command is awaiting confirmation.", "error")
        return redirect(url_for("index"))
    try:
        if regulate(action):
            logging.info("Regulation %s confirmed by APC_STATUS_PV RUN flag", action)
            flash(f"Regulation turned {action}.")
        else:
            logging.info("Regulation %s not sent: already %s", action, action)
            flash(f"Regulation is already {action}.")
    except Exception as exc:
        logging.error("Regulation %s failed: %s", action, exc)
        flash(f"Regulation {action} failed: {exc}", "error")
    finally:
        regulation_lock.release()
    return redirect(url_for("index"))


def main():
    try:
        port = int(os.environ.get("APC_PORT", "5000"))
    except ValueError:
        raise SystemExit("APC_PORT must be a port number") from None
    url = f"http://127.0.0.1:{port}/"
    open_browser = "--no-browser" not in sys.argv[1:] and os.environ.get("APC_NO_BROWSER", "").lower() not in TRUE_VALUES
    try:
        response = httpx.get(url + "api/health", timeout=1.0, trust_env=False, follow_redirects=False)
        already_running = response.status_code == 200 and response.json() == {"app": "chamber-apc-control"}
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        already_running = False
    if already_running:
        print(f"Control panel: {url}", flush=True)
        if open_browser:
            _open_browser(url)
        return
    stop_event = threading.Event()
    worker = threading.Thread(target=schedule_worker, args=(stop_event,), daemon=True)
    worker.start()
    logging.info("Control panel starting: url=%s device=%s pressure limits=%g to %g hPa",
                 url, DEVICE_URL, PRESSURE_MIN_HPA, PRESSURE_MAX_HPA)
    print(f"Control panel: {url}\nDevice: {DEVICE_URL}\n"
          f"Pressure limits: {PRESSURE_MIN_HPA:g} to {PRESSURE_MAX_HPA:g} hPa\nLog: {LOG_PATH}", flush=True)
    browser_thread = None
    browser_timer = None
    if open_browser:
        browser_activity.touch()
        browser_timer = threading.Timer(1.0, _open_browser, args=(url,))
        browser_timer.start()
        browser_thread = threading.Thread(target=browser_worker, args=(url, stop_event), daemon=True)
        browser_thread.start()
    # The control panel prints its own startup information above.
    original_banner = flask_cli.show_server_banner
    flask_cli.show_server_banner = lambda *args, **kwargs: None
    try:
        # Local-only web UI.
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    finally:
        flask_cli.show_server_banner = original_banner
        scheduler.stop()
        stop_event.set()
        if browser_timer is not None:
            browser_timer.cancel()
        if browser_thread is not None:
            browser_thread.join(timeout=2)
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
