import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, session, request, redirect, url_for, jsonify
from flask_socketio import SocketIO, send
import sqlite3
import os
import hashlib

app = Flask(__name__)
app.config['SECRET_KEY'] = 'JEDGKJNSEDOJGKN533265!£#WF353212'

socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

DB_PATH = os.path.join(os.path.dirname(__file__), 'chat.db')

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL
        )''')

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_user(username):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('SELECT * FROM users WHERE username = ?', (username,))
        return cur.fetchone()

def get_history():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute('SELECT content FROM messages ORDER BY id DESC LIMIT 50')
            return [row[0] for row in reversed(cur.fetchall())]
    except Exception:
        return []

# ── Routes ──────────────────────────────────────────────

@app.route('/')
def home():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('main.html', username=session['username'])

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = get_user(username)
        if not user or user[2] != hash_password(password):
            error = 'Wrong username or password.'
        else:
            session['username'] = username
            return redirect(url_for('home'))
    return render_template('login.html', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            error = 'Username and password are required.'
        elif len(username) < 3:
            error = 'Username must be at least 3 characters.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        else:
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute('INSERT INTO users (username, password) VALUES (?, ?)',
                                 (username, hash_password(password)))
                session['username'] = username
                return redirect(url_for('home'))
            except sqlite3.IntegrityError:
                error = 'That username is already taken.'
    return render_template('register.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/reset')
def reset():
    if 'username' not in session:
        return redirect(url_for('login'))
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('DELETE FROM messages')
    return 'Chat cleared!'

# ── SocketIO ─────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    if 'username' not in session:
        return False  # reject connection
    for msg in get_history():
        send(msg)

@socketio.on('message')
def handle_message(msg):
    if 'username' not in session:
        return
    username = session['username']
    full_msg = f"{username}: {msg}"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO messages (content) VALUES (?)', (full_msg,))
    send(full_msg, broadcast=True)

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
