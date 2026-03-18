from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'attendance.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS employees (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id TEXT,
            event_time TEXT,
            event_type TEXT DEFAULT 'fingerprint',
            status TEXT DEFAULT 'unknown',
            raw_data TEXT,
            FOREIGN KEY (employee_id) REFERENCES employees(id)
        );
        CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time);
        CREATE INDEX IF NOT EXISTS idx_events_employee ON events(employee_id);
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/webhook', methods=['POST'])
def webhook():
    """Receive events from Hikvision device"""
    data = request.get_data(as_text=True)
    content_type = request.content_type or ''

    employee_id = None
    event_time = None

    if 'json' in content_type:
        json_data = request.get_json(silent=True)
        if json_data:
            event_info = json_data.get('AccessControllerEvent', json_data)
            employee_id = event_info.get('employeeNoString', '')
            event_time = event_info.get('time', '')
    elif 'xml' in content_type:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(data)
            ns = {'ns': 'http://www.isapi.org/ver20/XMLSchema'}
            emp = root.find('.//ns:employeeNoString', ns)
            time_el = root.find('.//ns:time', ns)
            if emp is not None:
                employee_id = emp.text
            if time_el is not None:
                event_time = time_el.text
        except:
            pass

    if not event_time:
        event_time = datetime.now().isoformat()

    if employee_id:
        conn = get_db()
        conn.execute(
            'INSERT INTO events (employee_id, event_time, raw_data) VALUES (?, ?, ?)',
            (employee_id, event_time, data[:2000])
        )
        conn.commit()

        # Auto-create employee if not exists
        existing = conn.execute('SELECT id FROM employees WHERE id = ?', (employee_id,)).fetchone()
        if not existing:
            conn.execute('INSERT INTO employees (id, name) VALUES (?, ?)',
                        (employee_id, f'موظف {employee_id}'))
            conn.commit()
        conn.close()

    return jsonify({'status': 'ok'}), 200

@app.route('/api/employees', methods=['GET'])
def get_employees():
    conn = get_db()
    employees = conn.execute('SELECT * FROM employees ORDER BY id').fetchall()
    conn.close()
    return jsonify([dict(e) for e in employees])

@app.route('/api/employees', methods=['POST'])
def add_employee():
    data = request.get_json()
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO employees (id, name, department) VALUES (?, ?, ?)',
        (data['id'], data['name'], data.get('department', ''))
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/employees/<emp_id>', methods=['DELETE'])
def delete_employee(emp_id):
    conn = get_db()
    conn.execute('DELETE FROM employees WHERE id = ?', (emp_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/api/attendance', methods=['GET'])
def get_attendance():
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    conn = get_db()

    employees = conn.execute('SELECT * FROM employees ORDER BY id').fetchall()
    events = conn.execute(
        'SELECT * FROM events WHERE event_time LIKE ? ORDER BY event_time',
        (f'{date}%',)
    ).fetchall()

    attendance = []
    for emp in employees:
        emp_events = [e for e in events if e['employee_id'] == emp['id']]
        first_event = emp_events[0]['event_time'] if emp_events else None
        last_event = emp_events[-1]['event_time'] if len(emp_events) > 1 else None

        if first_event:
            try:
                check_in_time = first_event.split('T')[1][:5]
            except:
                check_in_time = first_event
        else:
            check_in_time = None

        if last_event:
            try:
                check_out_time = last_event.split('T')[1][:5]
            except:
                check_out_time = last_event
        else:
            check_out_time = None

        status = 'غائب'
        if emp_events:
            status = 'حاضر'

        attendance.append({
            'employee_id': emp['id'],
            'name': emp['name'],
            'department': emp['department'],
            'check_in': check_in_time,
            'check_out': check_out_time,
            'total_events': len(emp_events),
            'status': status
        })

    conn.close()
    return jsonify(attendance)

@app.route('/api/events', methods=['GET'])
def get_events():
    date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    emp_id = request.args.get('employee_id', None)
    conn = get_db()

    if emp_id:
        events = conn.execute(
            'SELECT * FROM events WHERE event_time LIKE ? AND employee_id = ? ORDER BY event_time DESC',
            (f'{date}%', emp_id)
        ).fetchall()
    else:
        events = conn.execute(
            'SELECT * FROM events WHERE event_time LIKE ? ORDER BY event_time DESC',
            (f'{date}%',)
        ).fetchall()

    conn.close()
    return jsonify([dict(e) for e in events])

@app.route('/api/sync', methods=['POST'])
def sync_from_device():
    """Pull events directly from device (when on same network)"""
    import requests
    from requests.auth import HTTPDigestAuth

    device_ip = request.json.get('device_ip', '172.20.10.2')
    device_user = request.json.get('user', 'admin')
    device_pass = request.json.get('password', 'Ad2026Ad')
    date = request.json.get('date', datetime.now().strftime('%Y-%m-%d'))

    try:
        url = f'http://{device_ip}/ISAPI/AccessControl/AcsEvent?format=json'
        payload = {
            "AcsEventCond": {
                "searchID": "1",
                "searchResultPosition": 0,
                "maxResults": 100,
                "major": 0,
                "minor": 0,
                "startTime": f"{date}T00:00:00+03:00",
                "endTime": f"{date}T23:59:59+03:00"
            }
        }

        resp = requests.post(url, json=payload, auth=HTTPDigestAuth(device_user, device_pass), timeout=15)
        data = resp.json()

        conn = get_db()
        events_added = 0

        if 'AcsEvent' in data and 'InfoList' in data['AcsEvent']:
            for event in data['AcsEvent']['InfoList']:
                emp_id = event.get('employeeNoString', '')
                event_time = event.get('time', '')

                if emp_id and event.get('major') == 5:
                    existing = conn.execute(
                        'SELECT id FROM events WHERE employee_id = ? AND event_time = ?',
                        (emp_id, event_time)
                    ).fetchone()

                    if not existing:
                        conn.execute(
                            'INSERT INTO events (employee_id, event_time) VALUES (?, ?)',
                            (emp_id, event_time)
                        )
                        events_added += 1

                    emp_existing = conn.execute('SELECT id FROM employees WHERE id = ?', (emp_id,)).fetchone()
                    if not emp_existing:
                        conn.execute('INSERT INTO employees (id, name) VALUES (?, ?)',
                                    (emp_id, f'موظف {emp_id}'))

        # Also sync users
        try:
            url2 = f'http://{device_ip}/ISAPI/AccessControl/UserInfo/Search?format=json'
            payload2 = {
                "UserInfoSearchCond": {
                    "searchID": "1",
                    "searchResultPosition": 0,
                    "maxResults": 50
                }
            }
            resp2 = requests.post(url2, json=payload2, auth=HTTPDigestAuth(device_user, device_pass), timeout=15)
            data2 = resp2.json()

            if 'UserInfoSearch' in data2 and 'UserInfo' in data2['UserInfoSearch']:
                for user in data2['UserInfoSearch']['UserInfo']:
                    emp_id = user.get('employeeNo', '')
                    name = user.get('name', '') or f'موظف {emp_id}'
                    if emp_id:
                        conn.execute(
                            'INSERT OR REPLACE INTO employees (id, name) VALUES (?, ?)',
                            (emp_id, name)
                        )
        except:
            pass

        conn.commit()
        conn.close()

        return jsonify({'status': 'ok', 'events_added': events_added})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
