import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, session, request, redirect, url_for
from flask_socketio import SocketIO, send, emit
import sqlite3
import os
import hashlib
import json
import anthropic

app = Flask(__name__)
app.config['SECRET_KEY'] = 'changeme-use-a-long-random-string'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*')

DB_PATH = os.path.join(os.path.dirname(__file__), 'chat.db')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

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

# ── AI Moderation ─────────────────────────────────────────

def moderate_messages():
    """Run every 5 seconds — ask Claude if any recent messages are bad, delete them."""
    while True:
        eventlet.sleep(5)
        if not ANTHROPIC_API_KEY:
            continue
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.execute('SELECT id, content FROM messages ORDER BY id DESC LIMIT 20')
                msgs = cur.fetchall()

            if not msgs:
                continue

            # Build list for Claude to review
            msg_list = [{'id': m[0], 'content': m[1]} for m in msgs]
            prompt = (
                "You are a chat moderator. Review these chat messages and return a JSON array "
                "of IDs that contain hate speech, slurs, explicit sexual content, threats, spam, "
                "or illegal content. Only flag genuinely bad messages. If nothing is bad return []. "
                "Respond with ONLY a JSON array of integer IDs, nothing else.\n\n"
                "Messages:\n" + json.dumps(msg_list)
            )

            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            response = client.messages.create(
                model='claude-opus-4-6',
                max_tokens=256,
                messages=[{'role': 'user', 'content': prompt}]
            )

            raw = response.content[0].text.strip()
            bad_ids = json.loads(raw)

            if not isinstance(bad_ids, list) or not bad_ids:
                continue

            # Delete bad messages from DB
            with sqlite3.connect(DB_PATH) as conn:
                for mid in bad_ids:
                    conn.execute('DELETE FROM messages WHERE id = ?', (mid,))

            # Tell all clients to remove those messages
            socketio.emit('delete_messages', bad_ids)

        except Exception as e:
            print(f'Moderation error: {e}')

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
    # Send history with IDs so client can delete by ID later
    for mid, content in get_history():
        emit('chat_message', {'id': mid, 'content': content})

@socketio.on('message')
def handle_message(msg):
    if 'username' not in session:
        return
    username = session['username']
    full_msg = f"{username}: {msg}"
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('INSERT INTO messages (content) VALUES (?)', (full_msg,))
        msg_id = cur.lastrowid
    socketio.emit('chat_message', {'id': msg_id, 'content': full_msg})

# ── Start ─────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    # Start moderation loop in background
    eventlet.spawn(moderate_messages)
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
