"""Pylontech Battery Health Check — Flask web app.

A focused tool for diagnosing failed Pylontech battery packs and producing
warranty-claim ready reports. Console mode only — that is the only mode
that exposes the per-cell SOH counters, real cycle count, and event log
that a Pylontech warranty claim requires.

Plug your USB-RS232 console cable into the master pack's RJ45 console port
(NOT the CAN port) to get the whole-rack view.
"""

import logging
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict
from datetime import date, datetime
from typing import Optional

from flask import Flask, Response, jsonify, render_template, request

from pylontech.connection import ConnectionManager
from pylontech.console import PylonConsole
from pylontech.diagnose import PackDiagnosis, diagnose_pack, parse_info, scan_rack
from pylontech.report import generate_report, generate_rack_report

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

connection = ConnectionManager()
console: Optional[PylonConsole] = None
last_diagnoses: dict[int, PackDiagnosis] = {}
last_rack_scan: dict = {'diagnoses': [], 'rack_raw': '', 'timestamp': None}

# Background dump jobs
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# Serial port is single-resource — only one request at a time may talk
# to the BMS, otherwise responses interleave and verdicts get computed
# against the wrong pack's data. Wrap every console._send / scan_rack /
# dump_* call with this lock.
serial_lock = threading.Lock()

# How long after a job finishes we keep its result before reaping.
JOB_TTL_SECONDS = 3600


def _reap_stale_jobs() -> None:
    """Drop finished jobs older than JOB_TTL_SECONDS to bound memory."""
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        for jid in [j for j, v in jobs.items()
                    if v.get('done') and v.get('updated_at', 0) < cutoff]:
            jobs.pop(jid, None)


def _has_unfinished_job(kind: str) -> bool:
    with jobs_lock:
        return any(not v.get('done') and v.get('kind') == kind
                   for v in jobs.values())


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

@app.route('/api/ports')
def api_ports():
    return jsonify({'ports': ConnectionManager.detect_ports()})


@app.route('/api/connect', methods=['POST'])
def api_connect():
    """Connect (with wakeup) to the master pack's console port."""
    global console
    data = request.get_json() or {}
    port = data.get('port', '')
    do_wakeup = data.get('wakeup', True)

    if not port:
        return jsonify({'error': 'No port specified'}), 400

    connection.disconnect()

    if do_wakeup:
        result = connection.wakeup(port)
        if not result.get('success'):
            return jsonify({
                'error': result.get('response', 'Wakeup failed'),
                'wakeup': result,
            }), 500
        wakeup_info = result
    else:
        if not connection.connect(port, 115200):
            return jsonify({'error': f'Failed to connect to {port}'}), 500
        wakeup_info = {'success': True, 'prompt_found': True}

    console = PylonConsole(connection)
    return jsonify({
        'success': True,
        'port': port,
        'wakeup': wakeup_info,
    })


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    global console
    if (_has_unfinished_job('rackscan') or _has_unfinished_job('eventlog')
            or _has_unfinished_job('eventhistory')):
        return jsonify({
            'error': 'A serial operation is in progress — wait for it to finish before disconnecting (otherwise the in-flight dump will be corrupted).'
        }), 409
    # Take the serial lock briefly to make sure no _send is mid-flight.
    with serial_lock:
        connection.disconnect()
        console = None
    last_diagnoses.clear()
    return jsonify({'success': True})


@app.route('/api/status')
def api_status():
    return jsonify({
        'connected': connection.is_connected,
        'port': connection.port if connection.is_connected else '',
    })


# ---------------------------------------------------------------------------
# Console passthrough — for power users
# ---------------------------------------------------------------------------

# Allowlist of read-only diagnostic commands. The first whitespace-separated
# token must match one of these; anything else is rejected. A blocklist is
# unsafe — Pylontech firmware exposes config writes (`config bov ...`),
# control commands (`ctrl cfet off`), and other undocumented strings that
# can disable a rack or alter protection params, and a substring blocklist
# cannot enumerate them all.
CONSOLE_ALLOWLIST = frozenset({
    'info', 'pwr', 'bat', 'soh', 'stat', 'log', 'data',
    'time', 'alarm', 'unit', 'help', 'ver', 'sn', 'fault',
})


@app.route('/api/console', methods=['POST'])
def api_console():
    if not console:
        return jsonify({'error': 'Not connected'}), 400
    cmd = (request.get_json() or {}).get('command', '').strip()
    if not cmd:
        return jsonify({'error': 'No command specified'}), 400
    head = cmd.split()[0].lower() if cmd.split() else ''
    if head not in CONSOLE_ALLOWLIST:
        return jsonify({
            'error': f'Command `{head}` is not on the read-only allowlist. '
                     f'Permitted: {", ".join(sorted(CONSOLE_ALLOWLIST))}.'
        }), 403
    # `config` and `ctrl` are explicitly NOT on the allowlist because both
    # have write forms. If a future read-only need arises, add a narrow
    # subcommand check here rather than allowing the bare verb.
    with serial_lock:
        return jsonify({'command': cmd, 'response': console._send(cmd, timeout=20)})


# ---------------------------------------------------------------------------
# Rack overview — parse the multi-pack `pwr` table from the master
# ---------------------------------------------------------------------------

@app.route('/api/rack')
def api_rack():
    if not console:
        return jsonify({'error': 'Not connected'}), 400
    if _has_unfinished_job('rackscan') or _has_unfinished_job('eventlog') or _has_unfinished_job('eventhistory'):
        return jsonify({'error': 'A long-running serial operation is in progress — wait for it to finish before refreshing the rack overview.'}), 409
    with serial_lock:
        raw = console._send('pwr', timeout=20)

    packs = []
    in_data = False
    for line in raw.splitlines():
        line_strip = line.strip()
        # Header detection: line containing 'Power' and 'Volt' and 'Curr'
        if not in_data and 'Power' in line and 'Volt' in line and 'Curr' in line:
            in_data = True
            continue
        if not in_data:
            continue
        if not line_strip or line_strip.startswith('Command') or line_strip.startswith('$'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # Pack number is first
        try:
            pack_num = int(parts[0])
        except ValueError:
            continue
        # Absent rows
        if 'Absent' in line:
            packs.append({'pack': pack_num, 'absent': True})
            continue
        # Try parsing the multi-column rack table.
        # Column order observed on US5000 master with US3000C slaves:
        # Power Volt Curr Tempr Tlow Tlow.Id Thigh Thigh.Id Vlow Vlow.Id Vhigh Vhigh.Id ...
        try:
            row = {
                'pack': pack_num,
                'voltage_mv': int(parts[1]),
                'current_ma': int(parts[2]),
                'temp_mc': int(parts[3]),
            }
            if len(parts) >= 11:
                row['vlow_mv'] = int(parts[8])
                row['vhigh_mv'] = int(parts[10])
                row['spread_mv'] = row['vhigh_mv'] - row['vlow_mv']
            for p in parts:
                if p.endswith('%'):
                    try:
                        row['soc_percent'] = int(p[:-1])
                    except ValueError:
                        pass
            # State word (Charging / Discharging / Idle)
            for p in parts:
                if p in ('Dischg', 'Charge', 'Idle'):
                    row['state'] = p
                    break
            packs.append(row)
        except (ValueError, IndexError):
            packs.append({'pack': pack_num, 'parse_error': True, 'raw_line': line_strip})
    return jsonify({'packs': packs, 'raw': raw})


# ---------------------------------------------------------------------------
# Full diagnostic
# ---------------------------------------------------------------------------

@app.route('/api/diagnose/<int:pack_id>')
def api_diagnose(pack_id):
    if not console:
        return jsonify({'error': 'Not connected'}), 400
    if not 1 <= pack_id <= 16:
        return jsonify({'error': 'Pack ID out of range (1-16)'}), 400
    if _has_unfinished_job('rackscan') or _has_unfinished_job('eventlog') or _has_unfinished_job('eventhistory'):
        return jsonify({'error': 'A long-running serial operation is in progress — wait for it to finish before running an individual diagnostic.'}), 409
    try:
        with serial_lock:
            diag = diagnose_pack(console, pack_id)
        last_diagnoses[pack_id] = diag
        return jsonify(asdict(diag))
    except Exception as e:
        logger.exception('Diagnose error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/report/<int:pack_id>')
def api_report(pack_id):
    diag = last_diagnoses.get(pack_id)
    if not diag:
        return jsonify({'error': f'Run diagnostic on pack {pack_id} first.'}), 400
    md = generate_report(diag)
    safe_id = (diag.barcode or f'pack{pack_id}').strip().replace(' ', '_')
    filename = f'pylontech-report-{safe_id}.md'
    return Response(
        md,
        mimetype='text/markdown',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Background dump jobs — long-running captures with progress polling
# ---------------------------------------------------------------------------

def _new_job(kind: str) -> dict:
    job = {
        'id': str(uuid.uuid4()),
        'kind': kind,
        'pages': 0,
        'total': None,
        'started_at': time.time(),
        'updated_at': time.time(),
        'done': False,
        'error': None,
        'text': None,
        'filename': '',
        'barcode': '',
    }
    with jobs_lock:
        jobs[job['id']] = job
    return job


def _job_progress(job: dict, pages: int, total):
    with jobs_lock:
        job['pages'] = pages
        if total is not None:
            job['total'] = total
        job['updated_at'] = time.time()


def _start_dump_job(kind: str, dump_fn, header_fn) -> dict:
    """Kick off a background dump and return the job dict."""
    _reap_stale_jobs()
    job = _new_job(kind)
    if not console:
        job['error'] = 'Not connected'
        job['done'] = True
        return job

    if _has_unfinished_job('rackscan') or any(
        not v.get('done') and v.get('kind') in ('eventlog', 'eventhistory') and v['id'] != job['id']
        for v in jobs.values()
    ):
        with jobs_lock:
            job['error'] = 'Another serial operation is already running — wait for it to finish.'
            job['done'] = True
            job['updated_at'] = time.time()
        return job

    # Read `info` under the serial lock — must not race a concurrent scan.
    with serial_lock:
        info_text = console._send('info', timeout=10)
    info = parse_info(info_text)
    barcode = (info.get('barcode') or 'unknown').strip()
    address = info.get('address', '?')
    job['barcode'] = barcode

    def runner():
        try:
            with serial_lock:
                text = dump_fn(progress_cb=lambda p, t: _job_progress(job, p, t))
            header = header_fn(info, info_text)
            full = header + text
            safe_id = barcode.replace(' ', '_')
            with jobs_lock:
                job['text'] = full
                job['filename'] = f'pylontech-{kind}-{safe_id}-{date.today().isoformat()}.txt'
                job['done'] = True
                job['updated_at'] = time.time()
        except Exception as e:
            logger.exception(f'{kind} dump failed')
            with jobs_lock:
                job['error'] = str(e)
                job['done'] = True
                job['updated_at'] = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return job


def _eventlog_header(info: dict, info_text: str) -> str:
    return (
        "# Pylontech full event-log dump\n"
        f"# Captured:        {datetime.now().isoformat(timespec='seconds')}\n"
        f"# Tool:            pylontech-battery-health\n"
        f"# Barcode:         {info.get('barcode', '')}\n"
        f"# Pack address:    {info.get('address', '')}\n"
        f"# Model:           {info.get('model', '')}\n"
        f"# Release date:    {info.get('release_date', '')}\n"
        "#\n"
        "# Method: Pylontech native console `log` command, paged with CR\n"
        "# until the 'Press [Enter] to be continued' prompt no longer\n"
        "# appears (end of log).\n"
        "#\n"
        "# === info ===\n"
        f"{info_text.strip()}\n"
        "\n# === log (full) ===\n"
    )


def _eventhistory_header(info: dict, info_text: str) -> str:
    return (
        "# Pylontech full data-event history dump\n"
        f"# Captured:     {datetime.now().isoformat(timespec='seconds')}\n"
        f"# Tool:         pylontech-battery-health\n"
        f"# Barcode:      {info.get('barcode', '')}\n"
        f"# Pack address: {info.get('address', '')}\n"
        "#\n"
        "# Method: Pylontech native console `data event N` command, walked\n"
        "# from the latest item index down to 0.\n"
        "#\n"
        "# === info ===\n"
        f"{info_text.strip()}\n"
        "\n# === data event history (full) ===\n"
    )


@app.route('/api/eventlog/start', methods=['POST'])
def api_eventlog_start():
    """Start a full event-log dump. Returns job_id immediately."""
    if not console:
        return jsonify({'error': 'Not connected'}), 400
    job = _start_dump_job(
        'eventlog',
        console.dump_event_log,
        _eventlog_header,
    )
    return jsonify({'job_id': job['id']})


@app.route('/api/eventhistory/start', methods=['POST'])
def api_eventhistory_start():
    """Start a full data-event history dump. Returns job_id immediately."""
    if not console:
        return jsonify({'error': 'Not connected'}), 400
    job = _start_dump_job(
        'eventhistory',
        console.dump_data_event_history,
        _eventhistory_header,
    )
    return jsonify({'job_id': job['id']})


@app.route('/api/job/<job_id>')
def api_job_status(job_id):
    _reap_stale_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({'error': 'No such job'}), 404
        return jsonify({
            'id': job['id'],
            'kind': job['kind'],
            'pages': job['pages'],
            'total': job['total'],
            'done': job['done'],
            'error': job['error'],
            'filename': job['filename'],
            'barcode': job['barcode'],
            'elapsed': time.time() - job['started_at'],
        })


@app.route('/api/job/<job_id>/download')
def api_job_download(job_id):
    """Stream the finished job's output as a file. If the client passes
    `?pack=N`, the user-confirmed pack address is stamped into the
    filename so a multi-pack capture session doesn't end up with several
    files that all have the same name and barcode."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({'error': 'No such job'}), 404
        if not job['done']:
            return jsonify({'error': 'Job still running'}), 425
        if job['error']:
            return jsonify({'error': job['error']}), 500
        filename = job['filename']
        pack_q = request.args.get('pack', '').strip()
        if pack_q.isdigit() and 1 <= int(pack_q) <= 16:
            # Insert "-packN" right after the kind token in the filename:
            #   pylontech-eventlog-XYZ-DATE.txt -> pylontech-eventlog-pack8-XYZ-DATE.txt
            for kind in ('eventlog', 'eventhistory'):
                token = f'pylontech-{kind}-'
                if filename.startswith(token):
                    filename = token + f'pack{pack_q}-' + filename[len(token):]
                    break
        return Response(
            job['text'],
            mimetype='text/plain',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'},
        )


# ---------------------------------------------------------------------------
# Whole-rack scan — diagnose every online pack from the master
# ---------------------------------------------------------------------------

@app.route('/api/scan/start', methods=['POST'])
def api_scan_start():
    """Start a whole-rack scan. Returns job_id immediately."""
    if not console:
        return jsonify({'error': 'Not connected'}), 400

    _reap_stale_jobs()

    if _has_unfinished_job('rackscan'):
        return jsonify({'error': 'A rack scan is already in progress.'}), 409
    if _has_unfinished_job('eventlog') or _has_unfinished_job('eventhistory'):
        return jsonify({'error': 'An event-log dump is in progress — wait for it to finish before scanning.'}), 409

    job = _new_job('rackscan')

    def runner():
        try:
            def cb(p, t):
                _job_progress(job, p, t)

            with serial_lock:
                diagnoses, rack_raw = scan_rack(console, progress_cb=cb)

            # Save into shared state for in-app review without re-scanning
            with jobs_lock:
                last_rack_scan['diagnoses'] = diagnoses
                last_rack_scan['rack_raw'] = rack_raw
                last_rack_scan['timestamp'] = datetime.now().isoformat(timespec='seconds')
                # Also populate per-pack last_diagnoses so the diagnose UI shows them
                for d in diagnoses:
                    last_diagnoses[d.address] = d

            md = generate_rack_report(diagnoses, rack_raw)
            with jobs_lock:
                job['text'] = md
                job['filename'] = f'pylontech-rack-report-{date.today().isoformat()}.md'
                job['done'] = True
                job['updated_at'] = time.time()
                job['barcode'] = next((d.barcode for d in diagnoses if not d.via_master), '')
        except Exception as e:
            logger.exception('Rack scan failed')
            with jobs_lock:
                job['error'] = str(e)
                job['done'] = True
                job['updated_at'] = time.time()

    threading.Thread(target=runner, daemon=True).start()
    return jsonify({'job_id': job['id']})


@app.route('/api/scan/last')
def api_scan_last():
    """Return the JSON of the last completed rack scan (for in-app review)."""
    if not last_rack_scan['diagnoses']:
        return jsonify({'scanned': False})
    return jsonify({
        'scanned': True,
        'timestamp': last_rack_scan['timestamp'],
        'packs': [asdict(d) for d in last_rack_scan['diagnoses']],
    })


# ---------------------------------------------------------------------------
# Printable HTML view of any single pack report — open in browser, Cmd-P / Ctrl-P
# to "Save as PDF". Works the same on Mac / Windows / Linux without adding
# any heavy PDF-generation dependency to the toolkit.
# ---------------------------------------------------------------------------

@app.route('/api/report/<int:pack_id>/print')
def api_report_print(pack_id):
    diag = last_diagnoses.get(pack_id)
    if not diag:
        return Response(
            "<h1>Run the diagnostic first</h1><p>No saved diagnostic for pack "
            f"{pack_id}. Run a diagnostic on this pack first, then return to this URL.</p>",
            mimetype='text/html', status=400,
        )
    md = generate_report(diag)
    return _print_html('Pack {} — {}'.format(pack_id, diag.barcode or 'report'), md)


@app.route('/api/scan/last/print')
def api_scan_print():
    if not last_rack_scan['diagnoses']:
        return Response(
            "<h1>Run a rack scan first</h1>",
            mimetype='text/html', status=400,
        )
    md = generate_rack_report(last_rack_scan['diagnoses'], last_rack_scan['rack_raw'])
    return _print_html('Rack scan report', md)


def _print_html(title: str, md: str) -> Response:
    """Serve a markdown body as a printable HTML page.

    The page is styled for clean paper output (A4 / Letter), shows a brief
    "How to save as PDF" notice at the top that's hidden when printing.
    """
    # Minimal MD → HTML. Full markdown libs are overkill; this handles the
    # constructs we actually emit (headings, paragraphs, bold/italic,
    # inline code, code blocks, tables, blockquotes, lists, hr).
    html_body = _markdown_to_html(md)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{_html_escape(title)}</title>
<style>
@media print {{ .no-print {{ display: none !important; }} body {{ padding: 0; }} }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  max-width: 920px; margin: 0 auto; padding: 24px 32px; color: #0f172a; line-height: 1.55;
}}
h1, h2, h3, h4 {{ color: #1e3c72; margin-top: 1.6em; }}
h1 {{ border-bottom: 2px solid #1e3c72; padding-bottom: 6px; }}
h2 {{ border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; }}
table {{ border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 13.5px; page-break-inside: avoid; }}
th, td {{ border: 1px solid #cbd5e1; padding: 6px 10px; text-align: left; }}
th {{ background: #f1f5f9; font-weight: 600; }}
code {{ background: #f1f5f9; padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }}
pre {{ background: #0f172a; color: #d4d4d4; padding: 12px; border-radius: 6px; font-size: 11.5px; overflow-x: auto; page-break-inside: avoid; }}
pre code {{ background: none; padding: 0; color: inherit; }}
blockquote {{ border-left: 4px solid #1e3c72; padding: 8px 14px; background: #f8fafc; margin: 1em 0; }}
hr {{ border: none; border-top: 1px solid #cbd5e1; margin: 2em 0; }}
.print-banner {{
  background: #fef3c7; border: 1px solid #b45309; border-radius: 6px; padding: 10px 14px;
  margin-bottom: 24px; color: #b45309;
}}
.print-banner button {{ background: #1e3c72; color: white; border: none; border-radius: 4px;
  padding: 6px 14px; cursor: pointer; font-weight: 500; margin-left: 8px; }}
</style>
</head>
<body>
<div class="no-print print-banner">
  <strong>Save as PDF:</strong> press <kbd>⌘ P</kbd> (Mac) or <kbd>Ctrl + P</kbd> (Windows / Linux), then choose <em>Save as PDF</em> from the destination dropdown.
  <button onclick="window.print()">Open print dialog</button>
</div>
{html_body}
</body>
</html>"""
    return Response(page, mimetype='text/html')


def _html_escape(s: str) -> str:
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))


def _markdown_to_html(md: str) -> str:
    """Tiny Markdown subset → HTML converter sufficient for our reports.

    Handles: headings, paragraphs, **bold**, *italic*, `code`, fenced code
    blocks, tables (GFM-style), blockquotes, lists, horizontal rules.
    Deliberately small and dependency-free.
    """
    import re
    lines = md.split('\n')
    out: list[str] = []
    i = 0
    in_code = False
    code_buf: list[str] = []

    def inline(s: str) -> str:
        s = _html_escape(s)
        s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        s = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)
        s = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', s)
        return s

    while i < len(lines):
        line = lines[i]
        if line.startswith('```'):
            if in_code:
                out.append('<pre><code>' + '\n'.join(_html_escape(c) for c in code_buf) + '</code></pre>')
                code_buf = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        # Horizontal rule
        if stripped == '---':
            out.append('<hr>')
            i += 1
            continue

        # Headings
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            level = len(m.group(1))
            out.append(f'<h{level}>{inline(m.group(2))}</h{level}>')
            i += 1
            continue

        # Blockquote (possibly multi-line)
        if stripped.startswith('>'):
            buf = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                buf.append(lines[i].strip().lstrip('>').lstrip())
                i += 1
            out.append('<blockquote>' + '<br>'.join(inline(l) for l in buf if l) + '</blockquote>')
            continue

        # GFM table
        if '|' in line and i + 1 < len(lines) and re.match(r'^\s*\|?\s*[-: ]+\|', lines[i + 1]):
            header_cells = [c.strip() for c in line.strip().strip('|').split('|')]
            i += 2  # skip header + alignment row
            rows: list[list[str]] = []
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            html_table = ['<table><thead><tr>']
            for c in header_cells:
                html_table.append(f'<th>{inline(c)}</th>')
            html_table.append('</tr></thead><tbody>')
            for r in rows:
                html_table.append('<tr>')
                for c in r:
                    html_table.append(f'<td>{inline(c)}</td>')
                html_table.append('</tr>')
            html_table.append('</tbody></table>')
            out.append(''.join(html_table))
            continue

        # List
        if re.match(r'^\s*[-*]\s+', line):
            items = []
            while i < len(lines) and re.match(r'^\s*[-*]\s+', lines[i]):
                items.append(re.sub(r'^\s*[-*]\s+', '', lines[i]))
                i += 1
            out.append('<ul>' + ''.join(f'<li>{inline(it)}</li>' for it in items) + '</ul>')
            continue

        # Paragraph
        out.append(f'<p>{inline(stripped)}</p>')
        i += 1

    if in_code and code_buf:
        out.append('<pre><code>' + '\n'.join(_html_escape(c) for c in code_buf) + '</code></pre>')

    return '\n'.join(out)


if __name__ == '__main__':
    logger.info('Pylontech Battery Health Check')
    logger.info('Open http://localhost:8080 in your browser')
    # When running as a PyInstaller-packaged exe (sys.frozen is set), open
    # the browser automatically so the user doesn't have to know to type
    # localhost:8080. We do NOT do this when run from a terminal, so headless
    # Pi / SSH deployments aren't disturbed.
    if getattr(sys, 'frozen', False):
        def _open_browser():
            time.sleep(1.5)
            webbrowser.open('http://localhost:8080')
        threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
