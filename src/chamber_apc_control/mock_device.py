# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local mock of the chamber APC WeatherMonitor HTTP API.

The mock checks the control panel's protocol assumptions and failure handling.
Its faults are test behaviour, not a specification of the real device.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import threading
import time
from collections import Counter, deque
from datetime import datetime

from flask import Flask, Response, g, jsonify, redirect, render_template_string, request, url_for

# Test APC_STATUS_PV
# http://127.0.0.1:8001/WeatherMonitor/Set_Mock_Status?APC_STATUS_PV=15

RUN, ALARM, ZFIN, REMOTE = 1, 2, 4, 8
STATUS_PATH = "/WeatherMonitor/Get_Latest_Data"
COMMAND_PATH = "/WeatherMonitor/Set_Data"
BOTH, STATUS_ONLY, COMMAND_ONLY = {STATUS_PATH, COMMAND_PATH}, {STATUS_PATH}, {COMMAND_PATH}

# mode: (requests it affects, description shown on the mock page)
MODES = {
    "normal": (set(), "Normal responses."),
    "http_error": (BOTH, "Return the configured HTTP status. Commands are not applied."),
    "accept_then_error": (COMMAND_ONLY, "Apply the command, then return the configured HTTP status."),
    "drop_connection": (BOTH, "Close the connection without a response. Commands are not applied."),
    "redirect": (BOTH, "Return HTTP 302, which the client must not follow. Commands are not applied."),
    "malformed": (STATUS_ONLY, "Status reads return text that is not JSON."),
    "empty": (STATUS_ONLY, "Status reads return an empty body."),
    "bad_status": (STATUS_ONLY, "Status reads return APC_STATUS_PV outside 0 to 15."),
    "no_setpoint": (STATUS_ONLY, "Status reads omit APC_P_SV."),
    "slow": (BOTH, "Wait for Delay seconds, then respond. Commands apply after the wait."),
    "trickle": (BOTH, "Send the response body gradually over Delay seconds."),
    "ignore_control": (COMMAND_ONLY, "Accept Run/Stop without changing RUN."),
    "ignore_setpoint": (COMMAND_ONLY, "Accept Set without changing APC_P_SV."),
    "remote_drop": (COMMAND_ONLY, "Accept Run/Stop, clear REMOTE, and leave RUN unchanged."),
    "alarm_on_run": (COMMAND_ONLY, "Accept Run, then raise ALARM and leave RUN off."),
}
INITIAL_STATE = {"APC_STATUS_PV": 0, "APC_P_SV": 50.0, "APC_RUN_SV": 0, "APC_STOP_SV": 0}
INITIAL_FAULT = {"mode": "normal", "delay": 12.0, "http_status": 503, "fail_count": None,
                 "run_delay": 0.0, "setpoint_delay": 0.0, "setpoint_decimals": None}

app = Flask(__name__)
state_lock = threading.Lock()
state = dict(INITIAL_STATE)
fault = dict(INITIAL_FAULT)
commands = deque(maxlen=1000)
request_counts = Counter()
pending = []  # (due time, description, change); applied when next observed.

MOCK_PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mock Device Status</title><style>
*{box-sizing:border-box}body{font:16px system-ui,sans-serif;background:#f3f5f7;color:#17202a;margin:0;padding:28px}
main{max-width:640px;margin:auto;background:white;padding:24px;border-radius:12px;box-shadow:0 4px 18px #0001}
h2{margin:24px 0 8px}.item{display:flex;gap:10px;align-items:center;padding:7px 0}.item input{width:18px;height:18px}
.field{display:grid;grid-template-columns:minmax(0,1fr) 170px;gap:12px;align-items:center;margin:8px 0}
.field input,.field select{font:inherit;padding:8px;border:1px solid #b8c2cc;border-radius:7px;width:100%}
button{margin-top:12px;padding:10px 16px;font:inherit;font-weight:650;color:white;background:#216eaa;border:0;border-radius:7px;cursor:pointer}
button.reset{background:#646b73}.note{color:#566573;font-size:.92rem}.bits{font:18px ui-monospace,monospace;background:#eef1f4;padding:4px 8px;border-radius:6px}
.fault{padding:8px 10px;border-radius:7px;background:#eef1f4}.fault.active{background:#fdebea}
table{border-collapse:collapse;width:100%;font-size:.86rem}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid #e4e8ec;overflow-wrap:anywhere}
.scroll{overflow-x:auto}
</style></head><body><main><h1>Mock Device Status</h1>
<p>APC_STATUS_PV: <strong id="status">{{ status }}</strong> <span class="bits" id="binary">{{ binary }}</span></p>
<p class="fault" id="fault-summary" role="status">Mode: {{ fault.mode }}</p>
<form id="status-form" action="{{ url_for('set_status_form') }}" method="post">
<label class="item"><input type="checkbox" id="bit-0" name="run" value="1" {% if run %}checked{% endif %}> 0: RUN</label>
<label class="item"><input type="checkbox" id="bit-1" name="alarm" value="1" {% if alarm %}checked{% endif %}> 1: ALARM</label>
<label class="item"><input type="checkbox" id="bit-2" name="zfin" value="1" {% if zfin %}checked{% endif %}> 2: ZFIN</label>
<label class="item"><input type="checkbox" id="bit-3" name="remote" value="1" {% if remote %}checked{% endif %}> 3: REMOTE</label>
<label class="field" for="pressure">APC_P_SV (hPa) <input id="pressure" name="pressure" type="number" step="any" value="{{ pressure }}"></label>
<button type="submit">Apply status</button></form>
<h2>Communication test</h2>
<form action="{{ url_for('configure_fault') }}" method="post">
<label class="field">Mode <select name="mode" id="mode">
{% for mode, info in modes.items() %}<option value="{{ mode }}" data-description="{{ info[1] }}" {% if fault.mode == mode %}selected{% endif %}>{{ mode }}</option>
{% endfor %}</select></label>
<p class="note" id="mode-description"></p>
<label class="field">Delay for slow / trickle (s) <input name="delay" type="number" min="0" max="60" step="0.1" value="{{ fault.delay }}"></label>
<label class="field">HTTP status for errors <input name="http_status" type="number" min="400" max="599" step="1" value="{{ fault.http_status }}"></label>
<label class="field">Revert to normal after (requests) <input name="fail_count" type="number" min="1" step="1" placeholder="Never" value="{{ '' if fault.fail_count is none else fault.fail_count }}"></label>
<h2>Device response</h2>
<label class="field">RUN change delay (s) <input name="run_delay" type="number" min="0" max="60" step="0.1" value="{{ fault.run_delay }}"></label>
<label class="field">Setpoint change delay (s) <input name="setpoint_delay" type="number" min="0" max="60" step="0.1" value="{{ fault.setpoint_delay }}"></label>
<label class="field">Round setpoint to decimals <input name="setpoint_decimals" type="number" min="0" max="6" step="1" placeholder="No rounding" value="{{ '' if fault.setpoint_decimals is none else fault.setpoint_decimals }}"></label>
<button>Apply settings</button></form>
<form action="{{ url_for('reset_mock') }}" method="post"><button class="reset">Reset mock</button></form>
<h2>Recent accepted commands</h2>
<p class="note" id="counts"></p>
<div class="scroll"><table><thead><tr><th>Time</th><th>Parameters</th><th>Mode</th><th>STATUS after</th><th>SV after</th></tr></thead><tbody id="history"></tbody></table></div>
<p class="note"><a href="{{ url_for('command_history') }}">History (JSON)</a> · <a href="{{ url_for('mock_state') }}">State (JSON)</a></p>
<p class="note">Enable REMOTE before Run/Stop. APC_P_SV is a setpoint, not a pressure measurement.</p>
</main><script>
const statusForm=document.getElementById('status-form');
const modeSelect=document.getElementById('mode');
let statusEdited=false;
statusForm.addEventListener('input',()=>{statusEdited=true;});
function showModeDescription(){document.getElementById('mode-description').textContent=modeSelect.selectedOptions[0].dataset.description;}
modeSelect.addEventListener('change',showModeDescription);showModeDescription();
function addCell(row,text){const cell=document.createElement('td');cell.textContent=text;row.append(cell);}
async function refresh(){
    try{
        const [stateResponse,historyResponse]=await Promise.all([fetch('{{ url_for("mock_state") }}'),fetch('{{ url_for("command_history") }}')]);
        const data=await stateResponse.json();
        const history=await historyResponse.json();
        document.getElementById('status').textContent=data.state.APC_STATUS_PV;
        document.getElementById('binary').textContent=data.binary;
        // Keep unsent edits. Otherwise Apply could write back a stale RUN bit.
        if(!statusEdited){
            for(let bit=0;bit<4;bit++)document.getElementById(`bit-${bit}`).checked=!!(data.state.APC_STATUS_PV&(1<<bit));
            document.getElementById('pressure').value=data.state.APC_P_SV;
        }
        const fault=data.fault;
        const remaining=fault.fail_count===null?'':`, ${fault.fail_count} request(s) left`;
        const summary=document.getElementById('fault-summary');
        summary.textContent=`Mode: ${fault.mode}${remaining}. Pending changes: ${data.pending.length}.`;
        summary.classList.toggle('active',fault.mode!=='normal');
        document.getElementById('counts').textContent=`Requests since reset: status ${data.request_counts.status}, commands ${data.request_counts.command}.`;
        const rows=history.commands.slice(-10).reverse().map(entry=>{
            const row=document.createElement('tr');
            addCell(row,entry.datetime.slice(11,23));
            addCell(row,Object.entries(entry.parameters).map(([key,value])=>`${key}=${value}`).join(' '));
            addCell(row,entry.mode);
            addCell(row,entry.state.APC_STATUS_PV);
            addCell(row,entry.state.APC_P_SV);
            return row;
        });
        document.getElementById('history').replaceChildren(...rows);
    }catch(e){document.getElementById('fault-summary').textContent='Mock status unavailable: '+e.message;}
}
refresh();setInterval(refresh,1000);
</script></body></html>'''


def apply_due_locked(now=None):
    """Apply delayed device changes whose time has come. Hold state_lock."""
    now = time.monotonic() if now is None else now
    while pending and pending[0][0] <= now:
        _, _, change = pending.pop(0)
        change()


def schedule_locked(delay, description, change):
    pending.append((time.monotonic() + delay, description, change))
    pending.sort(key=lambda item: item[0])


def active_fault_mode_locked(path):
    """Return this request's fault mode, consuming a transient fault."""
    mode = fault["mode"]
    if path not in MODES[mode][0]:
        return "normal"
    if fault["fail_count"] is not None:
        fault["fail_count"] -= 1
        if fault["fail_count"] <= 0:
            fault.update(mode="normal", fail_count=None)
    return mode


def drop_connection():
    # The development server provides its socket. Test clients have no socket
    # and receive this response instead.
    connection = request.environ.get("werkzeug.socket")
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    return Response("Connection dropped", status=500, mimetype="text/plain")


@app.before_request
def simulate_fault():
    g.fault_mode = "normal"
    if request.path not in BOTH:
        return None
    with state_lock:
        request_counts["status" if request.path == STATUS_PATH else "command"] += 1
        mode = g.fault_mode = active_fault_mode_locked(request.path)
        settings = dict(fault)
    if mode == "slow":
        time.sleep(settings["delay"])
    elif mode == "http_error":
        return jsonify(error="Simulated device failure"), settings["http_status"]
    elif mode == "drop_connection":
        return drop_connection()
    elif mode == "redirect":
        return redirect(url_for("mock_state"), code=302)
    elif mode == "malformed":
        return Response("Mock invalid JSON response", mimetype="text/plain")
    elif mode == "empty":
        return Response("", mimetype="text/plain")
    g.fault_settings = settings
    return None


@app.after_request
def trickle_body(response):
    if getattr(g, "fault_mode", "normal") != "trickle":
        return response
    body = response.get_data()
    delay = g.fault_settings["delay"]

    def chunks():
        for index in range(len(body)):
            yield body[index:index + 1]
            time.sleep(delay / max(1, len(body)))

    response.response = chunks()
    response.headers.pop("Content-Length", None)
    return response


@app.get("/")
def index():
    with state_lock:
        apply_due_locked()
        snapshot = dict(state)
        fault_snapshot = dict(fault)
    status = snapshot["APC_STATUS_PV"]
    return render_template_string(
        MOCK_PAGE,
        status=status,
        binary=format(status, "04b"),
        run=bool(status & RUN),
        alarm=bool(status & ALARM),
        zfin=bool(status & ZFIN),
        remote=bool(status & REMOTE),
        pressure=snapshot["APC_P_SV"],
        fault=fault_snapshot,
        modes=MODES,
    )


@app.post("/set-status")
def set_status_form():
    status = 0
    for name, bit in (("run", 0), ("alarm", 1), ("zfin", 2), ("remote", 3)):
        if request.form.get(name) == "1":
            status |= 1 << bit
    try:
        pressure = finite_pressure(request.form["pressure"])
    except (KeyError, ValueError):
        return "APC_P_SV must be a number", 400
    with state_lock:
        state["APC_STATUS_PV"] = status
        state["APC_P_SV"] = pressure
    return redirect(url_for("index"))


@app.get(STATUS_PATH)
def get_latest_data():
    with state_lock:
        apply_due_locked()
        snapshot = dict(state)
    if g.fault_mode == "bad_status":
        snapshot["APC_STATUS_PV"] = 99
    elif g.fault_mode == "no_setpoint":
        del snapshot["APC_P_SV"]
    # Match the device's multi-line response: JSON is the final non-empty line.
    body = "Mock WeatherMonitor\nLatest values follow\n" + json.dumps(snapshot)
    return Response(body, mimetype="text/plain")


def finite_pressure(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("APC_P_SV must be finite")
    return number


def set_status_bits_locked(*, set_bits=0, clear_bits=0):
    state["APC_STATUS_PV"] = (state["APC_STATUS_PV"] | set_bits) & ~clear_bits


def apply_run_stop_locked(mode, is_run, delay):
    if mode == "ignore_control":
        return
    if mode == "remote_drop":
        set_status_bits_locked(clear_bits=REMOTE)
    elif mode == "alarm_on_run" and is_run:
        schedule_locked(delay, "ALARM on", lambda: set_status_bits_locked(set_bits=ALARM, clear_bits=RUN))
    elif is_run:
        schedule_locked(delay, "RUN on", lambda: set_status_bits_locked(set_bits=RUN))
    else:
        schedule_locked(delay, "RUN off", lambda: set_status_bits_locked(clear_bits=RUN))


@app.post(COMMAND_PATH)
def set_data():
    # Validate every field before mutating any state.
    try:
        if not request.args or set(request.args) - {"APC_RUN_SV", "APC_STOP_SV", "APC_P_SV"}:
            raise ValueError("Expected a supported control parameter")
        for name in ("APC_RUN_SV", "APC_STOP_SV"):
            if name in request.args and request.args[name] != "1":
                raise ValueError(f"{name} must be 1")
        if "APC_RUN_SV" in request.args and "APC_STOP_SV" in request.args:
            raise ValueError("Run and Stop cannot be combined")
        pressure = finite_pressure(request.args["APC_P_SV"]) if "APC_P_SV" in request.args else None
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    mode = g.fault_mode
    with state_lock:
        apply_due_locked()
        is_run = "APC_RUN_SV" in request.args
        if (is_run or "APC_STOP_SV" in request.args):
            if not state["APC_STATUS_PV"] & REMOTE:
                return jsonify(error="REMOTE flag is off"), 409
            apply_run_stop_locked(mode, is_run, fault["run_delay"])
        if pressure is not None and mode != "ignore_setpoint":
            decimals = fault["setpoint_decimals"]
            stored = pressure if decimals is None else round(pressure, decimals)
            schedule_locked(fault["setpoint_delay"], f"APC_P_SV={stored}",
                            lambda: state.__setitem__("APC_P_SV", stored))
        apply_due_locked()
        # Pulse command fields are reset by the device; results live in STATUS.
        state["APC_RUN_SV"] = state["APC_STOP_SV"] = 0
        snapshot = dict(state)
        commands.append({"datetime": datetime.now().isoformat(), "parameters": request.args.to_dict(),
                         "mode": mode, "state": snapshot})
        http_status = fault["http_status"]
    if mode == "accept_then_error":
        return jsonify(error="Simulated failure after applying the command"), http_status
    return jsonify(result="ok", state=snapshot)


def bounded_number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = finite_pressure(value)
    if not low <= number <= high:
        raise ValueError(f"{name} must be from {low:g} to {high:g}")
    return number


def bounded_integer(value, name, low, high):
    number = bounded_number(value, name, low, high)
    if number != int(number):
        raise ValueError(f"{name} must be an integer")
    return int(number)


def optional(parser, value, *args):
    return None if value is None or value == "" else parser(value, *args)


def parse_fault_config(data, current):
    """Merge a partial configuration into `current` after validating all fields."""
    if not hasattr(data, "get") or not hasattr(data, "keys"):
        raise ValueError("Expected an object")
    unknown = set(data.keys()) - set(INITIAL_FAULT)
    if unknown:
        raise ValueError(f"Unknown setting: {', '.join(sorted(unknown))}")
    merged = dict(current)
    if "mode" in data:
        if data["mode"] not in MODES:
            raise ValueError(f"Unknown mode: {data['mode']}")
        merged["mode"] = data["mode"]
        # A transient count belongs to the mode it was set with.
        merged["fail_count"] = None
    for name in ("delay", "run_delay", "setpoint_delay"):
        if name in data:
            merged[name] = bounded_number(data[name], name, 0, 60)
    if "http_status" in data:
        merged["http_status"] = bounded_integer(data["http_status"], "http_status", 400, 599)
    if "fail_count" in data:
        merged["fail_count"] = optional(bounded_integer, data["fail_count"], "fail_count", 1, 1_000_000)
    if "setpoint_decimals" in data:
        merged["setpoint_decimals"] = optional(bounded_integer, data["setpoint_decimals"], "setpoint_decimals", 0, 6)
    return merged


@app.post("/mock/config")
def configure_fault():
    data = request.get_json(silent=True) if request.is_json else request.form
    try:
        with state_lock:
            merged = parse_fault_config(data, fault)
            fault.update(merged)
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(merged) if request.is_json else redirect(url_for("index"))


@app.get("/mock/history")
def command_history():
    with state_lock:
        history = list(commands)
    return jsonify(commands=history)


@app.get("/mock/state")
def mock_state():
    with state_lock:
        apply_due_locked()
        return jsonify(state=dict(state), binary=format(state["APC_STATUS_PV"], "04b"), fault=dict(fault),
                       pending=[description for _, description, _ in pending],
                       request_counts={"status": request_counts["status"], "command": request_counts["command"]})


@app.post("/mock/reset")
def reset_mock():
    with state_lock:
        state.update(INITIAL_STATE)
        fault.update(INITIAL_FAULT)
        commands.clear()
        pending.clear()
        request_counts.clear()
    if request.is_json or not request.accept_mimetypes.accept_html:
        return jsonify(result="ok")
    return redirect(url_for("index"))


@app.get("/WeatherMonitor/Set_Mock_Status")
def set_mock_status():
    try:
        value = int(request.args["APC_STATUS_PV"])
        if not 0 <= value <= 15:
            raise ValueError
    except (KeyError, ValueError):
        return jsonify(error="APC_STATUS_PV must be an integer from 0 to 15"), 400
    with state_lock:
        state["APC_STATUS_PV"] = value
        snapshot = dict(state)
    return jsonify(result="ok", state=snapshot)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOCK_PORT", "8001")),
                        help="listening port on 127.0.0.1 (default: MOCK_PORT or 8001)")
    port = parser.parse_args().port
    print(f"Mock device page: http://127.0.0.1:{port}/", flush=True)
    print(f"Mock device API: http://127.0.0.1:{port}/WeatherMonitor/", flush=True)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
