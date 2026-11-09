# Copyright (C) 2026 Tak Kunihiro and Masa Yamanaka
# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the control panel's real page script in Node with a small fake DOM."""
import json
import re
import shutil
import subprocess
import unittest

from support import control

NODE = shutil.which('node')
HARNESS = r'''
const vm = require('node:vm');
const elements = new Map();
function element(id) {
    const classes = new Set();
    return {id, textContent: '', hidden: true, disabled: false, value: '', title: '', dataset: {}, files: [], options: [],
            scrollHeight: 0, scrollTop: 0, clientHeight: 0,
            classList: {add: (...names) => names.forEach(name => classes.add(name)),
                        remove: (...names) => names.forEach(name => classes.delete(name)),
                        toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
                        contains: name => classes.has(name)},
            addEventListener() {}, closest() { return element('parent'); }};
}
const responses = {};
const respond = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const context = vm.createContext({
    console, Date, Math, Promise, Error, TypeError, String,
    document: {getElementById(id) { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); },
               querySelectorAll: () => []},
    window: {addEventListener() {}},
    localStorage: {getItem: () => null, setItem() {}},
    setInterval() {}, setTimeout(callback) { callback(); },
    FormData: class {},
    fetch: async url => { const handler = responses[String(url).split('?')[0]]; if (!handler) throw new TypeError('Failed to fetch'); return handler(); },
});
const settle = () => new Promise(resolve => setImmediate(resolve));
const byId = id => context.document.getElementById(id);
(async () => {
    const {script, scenario} = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
    vm.runInContext(script, context);
    await settle();
    const result = await eval('(async () => {' + scenario + '})()');
    console.log(JSON.stringify(result));
    process.exit(0);
})().catch(error => { console.error(error); process.exit(1); });
'''
STATUS = "'/api/status'"


@unittest.skipUnless(NODE, 'Node.js is not installed; page script tests are optional')
class FrontendTests(unittest.TestCase):
    def run_page(self, scenario):
        client = control.app.test_client()
        page = client.get('/').text
        script = re.search(r'<script>(.*?)</script>', page, re.S).group(1)
        process = subprocess.run([NODE, '-e', HARNESS], input=json.dumps({'script': script, 'scenario': scenario}),
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout.strip().splitlines()[-1])

    def status_body(self, **values):
        data = {'running': True, 'remote': True, 'alarm': False, 'indicators': [True, False, False, True],
                'pressure_value': 42.5, 'updated_at': '2030-01-01T10:00:00+09:00'}
        data.update(values)
        return json.dumps(data)

    def test_lost_upload_response_keeps_start_disabled_until_status_arrives(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            byId('source-file').checked = true;
            vm.runInContext("csvInput.files=[{{name:'B.csv'}}];scheduleLoaded=true;scheduleSource='file';scheduleState='ready';updateScheduleButton()", context);
            const before = byId('schedule-start').disabled;
            await vm.runInContext('loadSelectedCsv()', context);
            await settle();
            return {{before, after: byId('schedule-start').disabled, message: byId('csv-upload-status').textContent}};
        ''')
        self.assertFalse(result['before'])
        self.assertTrue(result['after'])
        self.assertIn('Could not load CSV', result['message'])

    def test_remote_controls_button_and_from_shows_current_setpoint(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(remote=False, pressure_value=0)});
            await context.refreshStatus();
            return {{regulation: byId('regulation-toggle').disabled, from: byId('from-value').textContent, to: byId('to-value').value}};
        ''')
        self.assertEqual(result, {'regulation': True, 'from': 0, 'to': 0})

    def test_to_starts_at_the_setpoint_and_keeps_later_typing(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=42.5)});
            await context.refreshStatus();
            const filled = byId('to-value').value;
            byId('to-value').value = '30';
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=41.5)});
            await context.refreshStatus();
            const typed = byId('to-value').value;
            vm.runInContext('toPending=true;fillTo()', context);
            const again = byId('to-value').value;
            byId('source-ramp').checked = true;
            byId('to-value').value = '20';
            vm.runInContext('toPending=true;fillTo()', context);
            const rampKept = byId('to-value').value;
            byId('to-value').value = '';
            vm.runInContext('toPending=true;fillTo()', context);
            const rampEmpty = byId('to-value').value;
            byId('source-ramp').checked = false;
            byId('source-file').checked = true;
            byId('to-value').value = '';
            vm.runInContext('toPending=true;fillTo()', context);
            return {{filled, typed, again, rampKept, rampEmpty, file: byId('to-value').value}};
        ''')
        self.assertEqual(result, {'filled': 42.5, 'typed': '30', 'again': 41.5, 'rampKept': '20',
                                  'rampEmpty': 41.5, 'file': ''})

    def test_from_shows_only_for_ramp(self):
        result = self.run_page('''
            const seen = {};
            for (const id of ['source-now', 'source-ramp', 'source-file']) {
                for (const other of ['source-now', 'source-ramp', 'source-file']) byId(other).checked = other === id;
                vm.runInContext('updateScheduleButton()', context);
                seen[id] = byId('from-field').hidden;
            }
            return seen;
        ''')
        self.assertEqual(result, {'source-now': True, 'source-ramp': False, 'source-file': True})

    def test_alarm_and_run_off_during_schedule_show_warnings(self):
        result = self.run_page(f'''
            vm.runInContext("scheduleState='running'", context);
            responses[{STATUS}] = () => respond({self.status_body(alarm=True, running=False)});
            await context.refreshStatus();
            const warning = {{hidden: byId('device-warning').hidden, text: byId('device-warning').textContent}};
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            return {{warning, clearedHidden: byId('device-warning').hidden}};
        ''')
        self.assertFalse(result['warning']['hidden'])
        self.assertIn('ALARM is ON', result['warning']['text'])
        self.assertNotIn('still being sent', result['warning']['text'])
        self.assertTrue(result['clearedHidden'])

    def test_end_is_only_available_while_running_and_completed_cannot_start(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            byId('source-file').checked = true;
            const states = {{}};
            for (const state of ['ready', 'running', 'stopped', 'completed']) {{
                vm.runInContext(`scheduleLoaded=true;scheduleSource='file';scheduleState='${{state}}';updateScheduleButton()`, context);
                states[state] = [byId('schedule-start').disabled, byId('schedule-end').disabled, byId('schedule-start').textContent];
            }}
            return states;
        ''')
        self.assertEqual(result, {'ready': [False, True, 'Go'], 'running': [True, False, 'Go'],
                                  'stopped': [False, True, 'Restart'], 'completed': [True, True, 'Go']})

    def test_unreachable_panel_has_its_own_message(self):
        result = self.run_page('''
            await context.refreshStatus();
            return {text: byId('device-status').textContent, regulation: byId('regulation-toggle').disabled};
        ''')
        self.assertEqual(result, {'text': 'Control panel server is not responding.', 'regulation': True})

    def test_schedule_problems_show_failures_and_skipped_rows(self):
        result = self.run_page('''
            vm.runInContext("showSchedule({state:'running',name:'a.csv',source:'file',revision:1,error:'CSV line 2: boom',failed_count:1,skipped_count:3,max_lateness_seconds:60})", context);
            return byId('schedule-error').textContent;
        ''')
        self.assertIn('Last schedule error (1 failed): CSV line 2: boom', result)
        self.assertIn('3 late step(s) skipped', result)

    def test_a_halt_is_shown_without_a_failure_count(self):
        result = self.run_page('''
            vm.runInContext("showSchedule({state:'stopped',name:'a.csv',source:'file',revision:1,error:'REMOTE flag is off. Schedule stopped.',failed_count:0,skipped_count:0,max_lateness_seconds:60})", context);
            return byId('schedule-error').textContent;
        ''')
        self.assertEqual(result, 'REMOTE flag is off. Schedule stopped.')

    def test_schedule_shows_row_progress_and_download_link(self):
        result = self.run_page('''
            vm.runInContext("showSchedule({state:'running',name:'from 50 hPa to 30 hPa over 10 min',source:'set',revision:1,datetime:'2030-01-01 10:00:10',pressure:'45.0',position:12,total_rows:60,failed_count:0,skipped_count:0})", context);
            const running = {next: [byId('next-time').textContent, byId('next-value').textContent, byId('schedule-next').hidden], message: byId('next-schedule').textContent, loaded: byId('schedule-name').textContent, end: byId('schedule-end').disabled,
                             csv: byId('schedule-csv').hidden, set: byId('schedule-start').disabled};
            vm.runInContext("showSchedule({state:'idle',name:null,source:null,revision:2,failed_count:0,skipped_count:0})", context);
            return {running, csvIdle: byId('schedule-csv').hidden};
        ''')
        self.assertRegex(result['running']['next'][0], r'^in (\d+d \d+h|\d+h \d+m|\d+m \d+s|\d+s)$')
        self.assertEqual(result['running']['next'][1:], ['45.00', False])
        self.assertEqual(result['running']['message'], '')
        self.assertEqual(result['running']['loaded'], 'from 50 hPa to 30 hPa over 10 min')
        self.assertFalse(result['running']['end'])
        self.assertFalse(result['running']['csv'])
        self.assertTrue(result['running']['set'])
        self.assertTrue(result['csvIdle'])

    def test_chart_draws_every_row_with_the_position_and_skips_now(self):
        rows = json.dumps({'revision': 3, 'rows': [['2030-01-01T10:00:00', 50.0], ['2030-01-01T10:00:10', 50.0],
                                                   ['2030-01-01T10:00:20', 40.0], ['2030-01-01T10:00:30', 30.0]]})
        result = self.run_page(f'''
            responses['/api/schedule/rows'] = () => respond({rows});
            const data = {{state:'running',name:'x',kind:'ramp',source:'set',revision:3,failed_count:0,skipped_count:0,marker_time:'2030-01-01T10:00:25',datetime:'2030-01-01 10:00:30',pressure:'30.00',position:4,total_rows:4}};
            context.showSchedule(data);
            await settle();
            const running = {{html: byId('schedule-chart').innerHTML, hidden: byId('schedule-chart-wrap').hidden, current: [byId('present-time').innerHTML, byId('present-value').textContent]}};
            context.showSchedule({{...data, marker_time: null, datetime: undefined, state:'ready'}});
            const ready = byId('schedule-chart').innerHTML;
            context.showSchedule({{...data, kind:'now'}});
            return {{running, ready, now: byId('schedule-chart-wrap').hidden}};
        ''')
        self.assertFalse(result['running']['hidden'])
        self.assertEqual(result['running']['current'], ['<span>2030-01-01</span> <span>10:00:25</span>', '40.00'])
        self.assertIn('class="rest"', result['running']['html'])
        self.assertIn('class="next"', result['running']['html'])
        self.assertNotIn('class="next"', result['ready'])
        self.assertIn('class="marker"', result['running']['html'])
        self.assertNotIn('class="marker"', result['ready'])
        self.assertTrue(result['now'])

    def test_schedule_flag_is_green_only_while_running(self):
        result = self.run_page('''
            const lamp = byId('status-light-schedule');
            const seen = {};
            for (const state of ['idle', 'ready', 'running', 'stopped', 'completed']) {
                context.showSchedule({state, name: state === 'idle' ? null : 'x', kind: 'now', source: 'set', revision: 1, failed_count: 0, skipped_count: 0});
                seen[state] = [lamp.classList.contains('on'), lamp.classList.contains('off')];
            }
            return seen;
        ''')
        self.assertEqual(result, {'idle': [False, True], 'ready': [False, True], 'running': [True, False],
                                  'stopped': [False, True], 'completed': [False, True]})

    def test_next_counts_down_in_short_units(self):
        result = self.run_page('''
            return [8, 75, 3700, 90000, -5].map(s => context.chartDuration(s * 1000));
        ''')
        self.assertEqual(result, ['8s', '1m 15s', '1h 1m', '1d 1h', '0s'])

    def test_new_rows_clear_a_hover_on_the_previous_rows(self):
        long_rows = [[f'2030-01-01T10:00:{second:02d}', 50.0 - second] for second in range(0, 60, 10)]
        result = self.run_page(f'''
            responses['/api/schedule/rows'] = () => respond({json.dumps({'revision': 3, 'rows': long_rows})});
            context.showSchedule({{state:'ready',name:'x',kind:'ramp',source:'set',revision:3,failed_count:0,skipped_count:0}});
            await context.loadChartRows(3);
            vm.runInContext('chartHover=5;drawChart()', context);
            responses['/api/schedule/rows'] = () => respond({json.dumps({'revision': 4, 'rows': long_rows[:2]})});
            await context.loadChartRows(4);
            vm.runInContext('drawChart()', context);
            return vm.runInContext('chartHover', context);
        ''')
        self.assertIsNone(result)

    def test_csv_example_starts_at_the_next_hour_from_the_current_setpoint(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=60.0)});
            await context.refreshStatus();
            const live = byId('csv-example').textContent.split('\\n');
            const fixed = vm.runInContext("csvExample(60, new Date(2026, 10, 8, 15, 22, 5).getTime())", context).split('\\n');
            const midnight = vm.runInContext("csvExample(50, new Date(2026, 10, 8, 23, 30).getTime())", context).split('\\n')[1];
            return {{live: [live.length, live[0], live[1].slice(14), live[21].slice(14)], fixed: [fixed[1], fixed[2], fixed[21]], midnight}};
        ''')
        self.assertIn('<pre id="csv-example">date, time, pressure\n(an example appears when APC_P_SV is read)',
                      control.app.test_client().get('/').text)
        self.assertEqual(result['live'], [22, 'date, time, pressure', ':00:00, 60.00', ':10:00, 54.00'])
        self.assertEqual(result['fixed'], ['2026-11-08, 16:00:00, 60.00', '2026-11-08, 16:00:30, 59.70', '2026-11-08, 16:10:00, 54.00'])
        self.assertEqual(result['midnight'], '2026-11-09, 00:00:00, 50.00')

    def test_go_needs_remote_even_while_regulating(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(remote=False)});
            await context.refreshStatus();
            vm.runInContext(`scheduleLoaded=true;scheduleSource='file';scheduleState='ready';updateScheduleButton()`, context);
            return byId('schedule-start').disabled;
        ''')
        self.assertTrue(result)

    def test_chart_position_uses_the_current_setpoint(self):
        rows = json.dumps({'revision': 3, 'rows': [['2030-01-01T10:00:00', 50.0], ['2030-01-01T10:00:30', 30.0]]})
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=70)});
            await context.refreshStatus();
            responses['/api/schedule/rows'] = () => respond({rows});
            context.showSchedule({{state:'ready',name:'x',kind:'ramp',source:'set',revision:3,failed_count:0,skipped_count:0,marker_time:'2030-01-01T10:00:00'}});
            await settle();
            return {{html: byId('schedule-chart').innerHTML, current: [byId('present-time').innerHTML, byId('present-value').textContent]}};
        ''')
        self.assertEqual(result['current'], ['<span>2030-01-01</span> <span>10:00:00</span>', '70.00'])
        self.assertIn('>80</text>', result['html'])  # The axis grows to keep the point on the chart.

    def test_remote_hint_is_only_shown_while_remote_is_off(self):
        result = self.run_page(f'''
            const seen = [byId('remote-hint').hidden];
            for (const [remote, running] of [[false, false], [true, false], [true, true]]) {{
                responses[{STATUS}] = () => respond({{...JSON.parse({json.dumps(self.status_body())}), remote, running}});
                await context.refreshStatus();
                seen.push(byId('remote-hint').hidden);
            }}
            return seen;
        ''')
        self.assertEqual(result[1:], [False, True, True])

    def test_plan_says_what_start_will_do(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            const plan = byId('ramp-plan');
            byId('source-ramp').checked = true;
            plan.dataset = {{step: '10'}};
            byId('from-value').textContent = '50';
            const seen = {{}};
            for (const [to, seconds] of [['', '0'], ['50', '600'], ['30', '0'], ['30', '600']]) {{
                byId('to-value').value = to;
                byId('source-ramp').checked = seconds !== '0';
                byId('ramp-time').value = seconds;
                vm.runInContext('updateRampPlan()', context);
                seen[to + '/' + seconds] = plan.hidden ? null : plan.textContent;
            }}
            return seen;
        ''')
        self.assertIsNone(result['/0'])
        self.assertIsNone(result['50/600'])
        self.assertIsNone(result['30/0'])
        self.assertRegex(result['30/600'], r'^60 steps, -2 hPa/min\.$')

    def test_with_regulation_off_only_a_one_row_schedule_can_start(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(running=False)});
            await context.refreshStatus();
            const seen = {{}};
            for (const needs of [false, true]) {{
                vm.runInContext(`showSchedule({{state:'ready',name:'x',source:'set',revision:1,failed_count:0,skipped_count:0,needs_regulation:${{needs}}}})`, context);
                seen[needs] = byId('schedule-start').disabled;
            }}
            return seen;
        ''')
        self.assertEqual(result, {'false': False, 'true': True})

    def test_button_says_ok_for_now_and_plan_for_ramp_and_file(self):
        result = self.run_page(f'''
            const seen = {{}};
            for (const id of ['source-now', 'source-ramp', 'source-file']) {{
                for (const other of ['source-now', 'source-ramp', 'source-file']) byId(other).checked = other === id;
                vm.runInContext('updateScheduleButton()', context);
                seen[id] = byId('pressure-ok').textContent;
            }}
            return seen;
        ''')
        self.assertEqual(result, {'source-now': 'OK', 'source-ramp': 'Plan', 'source-file': 'Plan'})

    def test_now_needs_remote_but_ramp_and_file_do_not(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(remote=False)});
            await context.refreshStatus();
            byId('to-value').value = '30';
            vm.runInContext('updateScheduleButton()', context);
            const now = byId('pressure-ok').disabled;
            byId('source-ramp').checked = true;
            vm.runInContext('updateScheduleButton()', context);
            return {{now, ramp: byId('pressure-ok').disabled}};
        ''')
        self.assertEqual(result, {'now': True, 'ramp': False})

    def test_plan_needs_regulation_on_but_now_does_not(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(running=False)});
            await context.refreshStatus();
            byId('to-value').value = '30';
            const update = () => vm.runInContext('updateScheduleButton()', context);
            const state = () => [byId('pressure-ok').disabled, byId('plan-hint').hidden];
            update();
            const now = state();
            byId('source-ramp').checked = true;
            update();
            const ramp = state();
            byId('source-ramp').checked = false;
            byId('source-file').checked = true;
            vm.runInContext("csvInput.files=[{{name:'a.csv'}}];updateScheduleButton()", context);
            const file = state();
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            return {{now, ramp, file, regulated: state()}};
        ''')
        self.assertEqual(result, {'now': [False, True], 'ramp': [True, False], 'file': [True, False],
                                  'regulated': [False, True]})

    def test_ok_needs_a_to_value_or_a_selected_file(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=None)});
            await context.refreshStatus();
            const ok = () => byId('pressure-ok').disabled;
            const update = () => vm.runInContext('updateScheduleButton()', context);
            const empty = ok();
            byId('to-value').value = '30';
            update();
            const entered = ok();
            byId('source-file').checked = true;
            update();
            const noFile = ok();
            vm.runInContext("csvInput.files=[{{name:'a.csv'}}];updateScheduleButton()", context);
            const chosen = ok();
            vm.runInContext("showSchedule({{state:'running',name:'x',source:'set',revision:1,failed_count:0,skipped_count:0}})", context);
            return {{empty, entered, noFile, chosen, running: ok()}};
        ''')
        self.assertEqual(result, {'empty': True, 'entered': False, 'noFile': True, 'chosen': False, 'running': True})

    def test_ramp_time_shows_only_for_ramp_and_to_only_for_now_and_ramp(self):
        result = self.run_page(f'''
            const seen = {{}};
            for (const id of ['source-now', 'source-ramp', 'source-file']) {{
                for (const other of ['source-now', 'source-ramp', 'source-file']) byId(other).checked = other === id;
                vm.runInContext('updateScheduleButton()', context);
                seen[id] = [byId('set-fields').hidden, byId('ramp-field').hidden, byId('ramp-time').disabled, byId('file-fields').hidden];
            }}
            return seen;
        ''')
        self.assertEqual(result, {'source-now': [False, True, True, True], 'source-ramp': [False, False, False, True],
                                  'source-file': [True, True, True, False]})

    def test_ramp_start_needs_regulation(self):
        result = self.run_page(f'''
            vm.runInContext("showSchedule({{state:'ready',name:'x',source:'set',revision:1,failed_count:0,skipped_count:0,needs_regulation:true}})", context);
            responses[{STATUS}] = () => respond({self.status_body(running=False)});
            await context.refreshStatus();
            const off = byId('schedule-start').disabled;
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            return {{off, on: byId('schedule-start').disabled}};
        ''')
        self.assertEqual(result, {'off': True, 'on': False})

    def test_regulation_off_is_disabled_only_while_the_schedule_is_running(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(running=True)});
            await context.refreshStatus();
            const disabled = [];
            for (const state of ['ready', 'running', 'stopped', 'completed']) {{
                vm.runInContext(`showSchedule({{state:'${{state}}',name:'x',source:'set',revision:1,failed_count:0,skipped_count:0}})`, context);
                disabled.push(byId('regulation-toggle').disabled);
            }}
            return disabled;
        ''')
        self.assertEqual(result, [False, True, False, False])

    def test_regulation_heading_follows_the_value(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=30)});
            await context.refreshStatus();
            const known = byId('regulation-p').textContent;
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=None)});
            await context.refreshStatus();
            return {{known, missing: byId('regulation-p').textContent}};
        ''')
        self.assertEqual(result, {'known': 30, 'missing': '—'})

    def test_regulation_button_names_and_sends_the_opposite_of_the_state(self):
        result = self.run_page(f'''
            const seen = [];
            for (const running of [false, true]) {{
                responses[{STATUS}] = () => respond({{...JSON.parse({json.dumps(self.status_body())}), running}});
                await context.refreshStatus();
                seen.push([byId('regulation-toggle').textContent, byId('regulation-toggle').disabled,
                           byId('regulation-toggle').classList.contains('on'), byId('regulation-form').action]);
            }}
            responses[{STATUS}] = () => respond({self.status_body(remote=False)});
            await context.refreshStatus();
            seen.push([byId('regulation-toggle').textContent, byId('regulation-toggle').disabled]);
            return seen;
        ''')
        self.assertEqual(result, [['ON', False, False, '/regulation/on'], ['OFF', False, True, '/regulation/off'], ['OFF', True]])

    def test_from_follows_the_device_and_clears_when_unavailable(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=42.5)});
            await context.refreshStatus();
            const known = byId('from-value').textContent;
            byId('to-value').value = '30';
            responses[{STATUS}] = () => respond({self.status_body(pressure_value=41.5)});
            await context.refreshStatus();
            const updated = byId('from-value').textContent;
            responses[{STATUS}] = () => respond({{error: 'no device'}}, 502);
            await context.refreshStatus();
            return {{known, updated, lost: byId('from-value').textContent, to: byId('to-value').value}};
        ''')
        self.assertEqual(result, {'known': 42.5, 'updated': 41.5, 'lost': '—', 'to': '30'})

    def test_start_acts_on_what_is_set_whichever_source_is_chosen(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            const idle = byId('schedule-start').disabled;
            const hiddenIdle = byId('schedule-actions').hidden;
            vm.runInContext("showSchedule({{state:'stopped',name:'from 50 hPa to 30 hPa',source:'set',revision:1,failed_count:0,skipped_count:0}})", context);
            const set = [byId('schedule-start').disabled, byId('schedule-start').textContent];
            byId('source-file').checked = true;
            vm.runInContext('updateScheduleButton()', context);
            const chosenFile = [byId('schedule-start').disabled, byId('schedule-start').textContent];
            vm.runInContext("showSchedule({{state:'stopped',name:'a.csv',source:'file',revision:2,failed_count:0,skipped_count:0}})", context);
            return {{idle, hiddenIdle, 'set': set, chosenFile, heldFile: [byId('schedule-start').disabled, byId('schedule-start').textContent]}};
        ''')
        self.assertEqual(result, {'idle': True, 'hiddenIdle': True, 'set': [False, 'Go'], 'chosenFile': [False, 'Go'], 'heldFile': [False, 'Restart']})

    def test_past_only_file_disables_go_and_alarm_allows_only_regulation_off(self):
        result = self.run_page(f'''
            responses[{STATUS}] = () => respond({self.status_body()});
            await context.refreshStatus();
            vm.runInContext("showSchedule({{state:'ready',name:'past.csv',source:'file',revision:1,has_upcoming:false,failed_count:0,skipped_count:0}})", context);
            const pastGoDisabled = byId('schedule-start').disabled;
            vm.runInContext("showSchedule({{state:'ready',name:'future.csv',source:'file',revision:2,has_upcoming:true,failed_count:0,skipped_count:0}})", context);
            const futureGoDisabled = byId('schedule-start').disabled;
            responses[{STATUS}] = () => respond({self.status_body(alarm=True, running=False)});
            await context.refreshStatus();
            const alarmIdle = [byId('regulation-toggle').disabled, byId('schedule-start').disabled, byId('pressure-ok').disabled];
            responses[{STATUS}] = () => respond({self.status_body(alarm=True, running=True)});
            await context.refreshStatus();
            const alarmRunning = [byId('regulation-toggle').disabled, byId('schedule-start').disabled];
            return {{pastGoDisabled, futureGoDisabled, alarmIdle, alarmRunning}};
        ''')
        self.assertTrue(result['pastGoDisabled'])
        self.assertFalse(result['futureGoDisabled'])
        self.assertEqual(result['alarmIdle'], [True, True, True])
        self.assertEqual(result['alarmRunning'], [False, True])

    def test_tick_schedule_clock_updates_present_marker_while_running(self):
        rows = json.dumps({'revision': 1, 'rows': [['2020-01-01T00:00:00', 50.0], ['2035-01-01T00:00:00', 30.0]]})
        result = self.run_page(f'''
            responses['/api/schedule/rows'] = () => respond({rows});
            context.showSchedule({{state:'running',name:'x',kind:'ramp',source:'set',revision:1,failed_count:0,skipped_count:0,marker_time:'2020-01-01T00:00:00',datetime:'2035-01-01 00:00:00',pressure:'30.00',position:2,total_rows:2}});
            await settle();
            const before = byId('present-time').innerHTML;
            context.tickScheduleClock();
            const after = byId('present-time').innerHTML;
            return {{before, after}};
        ''')
        self.assertIn('2020-01-01', result['before'])
        self.assertNotEqual(result['before'], result['after'])
