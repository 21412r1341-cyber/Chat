import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, session, request, redirect, url_for
from flask_socketio import SocketIO, emit
import sqlite3
import os
import hashlib
from better_profanity import profanity

app = Flask(__name__)
app.config['SECRET_KEY'] = 'changeme-use-a-long-random-string'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

DB_PATH = os.path.join(os.path.dirname(__file__), 'chat.db')

profanity.load_censor_words()

# ── Database ─────────────────────────────────────────────

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
            cur = conn.execute('SELECT id, content FROM messages ORDER BY id DESC LIMIT 50')
            return list(reversed(cur.fetchall()))
    except Exception:
        return []

def is_bad(content):
    # Strip "username: " prefix before checking
    text = content.split(': ', 1)[-1] if ': ' in content else content
    return profanity.contains_profanity(text)

# ── Routes ────────────────────────────────────────────────

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
    socketio.emit('delete_messages', 'all')
    return 'Chat cleared!'

# ── SocketIO ──────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    if 'username' not in session:
        return False
    for mid, content in get_history():
        emit('chat_message', {'id': mid, 'content': content})

@socketio.on('message')
def handle_message(msg):
    if 'username' not in session:
        return
    username = session['username']
    full_msg = f"{username}: {msg}"

    # Block bad messages immediately — don't store or broadcast
    if is_bad(full_msg):
        emit('blocked', {'reason': 'Your message was blocked by the filter.'})
        return

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('INSERT INTO messages (content) VALUES (?)', (full_msg,))
        msg_id = cur.lastrowid
    socketio.emit('chat_message', {'id': msg_id, 'content': full_msg})

# ── Start ─────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
