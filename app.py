import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, session
from flask_socketio import SocketIO, send
import sqlite3
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'

socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

# Use absolute path for SQLite so it works on Render
DB_PATH = os.path.join(os.path.dirname(__file__), 'chat.db')

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS messages (content TEXT)')

def get_history():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute('SELECT content FROM messages ORDER BY rowid DESC LIMIT 50')
            return [row[0] for row in reversed(cur.fetchall())]
    except Exception:
        return []

@app.route('/')
def home():
    if 'username' not in session:
        session['username'] = f"User_{os.urandom(2).hex()}"
    return render_template('main.html', username=session['username'])

@socketio.on('connect')
def handle_connect():
    for msg in get_history():
        send(msg)

@socketio.on('message')
def handle_message(msg):
    username = session.get('username', 'Anonymous')
    full_msg = f"{username}: {msg}"
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('INSERT INTO messages (content) VALUES (?)', (full_msg,))
    except Exception:
        pass
    send(full_msg, broadcast=True)

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
