import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, session, request, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import sqlite3
import os
import hashlib
import time
from collections import defaultdict
from better_profanity import profanity

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-to-something-long-and-random')
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins='*', max_http_buffer_size=5_000_000)

DB_PATH = os.path.join(os.path.dirname(__file__), 'chat.db')
ADMIN_SECRET = os.environ.get('ADMIN_SECRET', 'wokfjwjnf!$£2525r2wr23fwa!"£!')  # Set this in Render env vars

profanity.load_censor_words()

# In-memory state
online_users = {}          # {username: sid}
rate_limits = defaultdict(list)  # {username: [timestamps]}


# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            receiver TEXT,
            content TEXT NOT NULL,
            msg_type TEXT DEFAULT 'text',
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester TEXT NOT NULL,
            receiver TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            UNIQUE(requester, receiver)
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blocker TEXT NOT NULL,
            blocked TEXT NOT NULL,
            UNIQUE(blocker, blocked)
        )''')

def db():
    return sqlite3.connect(DB_PATH)

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_user(username):
    with db() as conn:
        return conn.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()

def get_global_history():
    with db() as conn:
        rows = conn.execute(
            'SELECT id,sender,content,msg_type,created_at FROM messages WHERE receiver IS NULL ORDER BY id DESC LIMIT 60'
        ).fetchall()
    return list(reversed(rows))

def get_dm_history(u1, u2):
    with db() as conn:
        rows = conn.execute(
            '''SELECT id,sender,content,msg_type,created_at FROM messages
               WHERE receiver IS NOT NULL
               AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?))
               ORDER BY id DESC LIMIT 60''', (u1, u2, u2, u1)
        ).fetchall()
    return list(reversed(rows))

def dm_room(u1, u2):
    return 'dm__' + '__'.join(sorted([u1, u2]))

def is_blocked(blocker, blocked):
    with db() as conn:
        return conn.execute('SELECT 1 FROM blocks WHERE blocker=? AND blocked=?', (blocker, blocked)).fetchone() is not None

def get_friends(username):
    with db() as conn:
        rows = conn.execute(
            '''SELECT CASE WHEN requester=? THEN receiver ELSE requester END
               FROM friends WHERE (requester=? OR receiver=?) AND status='accepted' ''',
            (username, username, username)
        ).fetchall()
        pending = conn.execute(
            'SELECT requester FROM friends WHERE receiver=? AND status=?', (username, 'pending')
        ).fetchall()
    return [r[0] for r in rows], [r[0] for r in pending]

def is_rate_limited(username):
    now = time.time()
    rate_limits[username] = [t for t in rate_limits[username] if now - t < 10]
    if len(rate_limits[username]) >= 6:
        return True
    rate_limits[username].append(now)
    return False

def push_friends(username):
    friends, pending = get_friends(username)
    sid = online_users.get(username)
    if sid:
        socketio.emit('friends_update', {'friends': friends, 'pending': pending}, room=sid)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    if 'username' not in session:
        return redirect(url_for('login'))
    return render_template('chat.html', username=session['username'], is_admin=session.get('is_admin', False))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = get_user(username)
        if not user or user[2] != hash_pw(password):
            error = 'Wrong username or password.'
        elif user[4]:
            error = 'Your account has been banned.'
        else:
            session['username'] = username
            session['is_admin'] = bool(user[3])
            return redirect(url_for('home'))
    return render_template('login.html', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if not username or not password:
            error = 'All fields required.'
        elif len(username) < 3:
            error = 'Username must be 3+ characters.'
        elif len(password) < 6:
            error = 'Password must be 6+ characters.'
        else:
            try:
                with db() as conn:
                    conn.execute('INSERT INTO users (username,password) VALUES (?,?)', (username, hash_pw(password)))
                session['username'] = username
                session['is_admin'] = False
                return redirect(url_for('home'))
            except sqlite3.IntegrityError:
                error = 'Username already taken.'
    return render_template('register.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# Admin setup - only works if ADMIN_SECRET env var is set
@app.route('/admin/setup', methods=['GET', 'POST'])
def admin_setup():
    if not ADMIN_SECRET:
        return 'Admin setup is disabled.', 403
    error = success = None
    if request.method == 'POST':
        if request.form.get('secret') != ADMIN_SECRET:
            error = 'Wrong secret key.'
        else:
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '').strip()
            if len(password) < 12:
                error = 'Admin password must be 12+ characters.'
            else:
                try:
                    with db() as conn:
                        conn.execute('INSERT INTO users (username,password,is_admin) VALUES (?,?,1)', (username, hash_pw(password)))
                    success = f'Admin account "{username}" created!'
                except sqlite3.IntegrityError:
                    error = 'Username taken.'
    return render_template('admin_setup.html', error=error, success=success)

@app.route('/admin')
def admin_panel():
    if not session.get('is_admin'):
        return redirect(url_for('home'))
    with db() as conn:
        users = conn.execute('SELECT id,username,is_admin,is_banned,created_at FROM users ORDER BY id').fetchall()
        msg_count = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        user_count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    return render_template('admin.html', users=users, msg_count=msg_count, user_count=user_count, online=list(online_users.keys()))

@app.route('/admin/ban/<username>')
def admin_ban(username):
    if not session.get('is_admin'):
        return 'denied', 403
    with db() as conn:
        conn.execute('UPDATE users SET is_banned=1 WHERE username=?', (username,))
    sid = online_users.get(username)
    if sid:
        socketio.emit('force_logout', {}, room=sid)
    return redirect(url_for('admin_panel'))

@app.route('/admin/unban/<username>')
def admin_unban(username):
    if not session.get('is_admin'):
        return 'denied', 403
    with db() as conn:
        conn.execute('UPDATE users SET is_banned=0 WHERE username=?', (username,))
    return redirect(url_for('admin_panel'))

@app.route('/admin/delete_user/<username>')
def admin_delete_user(username):
    if not session.get('is_admin'):
        return 'denied', 403
    with db() as conn:
        conn.execute('DELETE FROM users WHERE username=?', (username,))
        conn.execute('DELETE FROM messages WHERE sender=?', (username,))
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_chat')
def admin_clear_chat():
    if not session.get('is_admin'):
        return 'denied', 403
    with db() as conn:
        conn.execute('DELETE FROM messages WHERE receiver IS NULL')
    socketio.emit('clear_chat', {})
    return redirect(url_for('admin_panel'))


# ── SocketIO ──────────────────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    if 'username' not in session:
        return False
    username = session['username']
    user = get_user(username)
    if not user or user[4]:
        return False
    online_users[username] = request.sid
    join_room('global')
    # Send history
    for mid, sender, content, mtype, ts in get_global_history():
        emit('msg', {'id': mid, 'sender': sender, 'content': content, 'type': mtype, 'ts': ts, 'room': 'global'})
    # Broadcast online list
    socketio.emit('online_users', list(online_users.keys()))
    # Send friends
    push_friends(username)

@socketio.on('disconnect')
def on_disconnect():
    username = session.get('username')
    if username:
        online_users.pop(username, None)
    socketio.emit('online_users', list(online_users.keys()))

@socketio.on('send_msg')
def on_send_msg(data):
    if 'username' not in session:
        return
    username = session['username']
    content = (data.get('content') or '').strip()
    mtype = data.get('type', 'text')
    room = data.get('room', 'global')

    if not content:
        return

    if is_rate_limited(username):
        emit('toast', {'msg': '⚡ Slow down! Too many messages.', 'cls': 'error'})
        return

    if mtype == 'image' and len(content) > 4_000_000:
        emit('toast', {'msg': 'Image too large. Max ~2MB.', 'cls': 'error'})
        return

    if mtype == 'text' and profanity.contains_profanity(content):
        emit('toast', {'msg': '🚫 Message blocked by filter.', 'cls': 'error'})
        return

    if room == 'global':
        with db() as conn:
            cur = conn.execute('INSERT INTO messages (sender,content,msg_type) VALUES (?,?,?)', (username, content, mtype))
            mid = cur.lastrowid
        socketio.emit('msg', {'id': mid, 'sender': username, 'content': content, 'type': mtype, 'ts': int(time.time()), 'room': 'global'}, room='global')
    else:
        target = room
        if is_blocked(target, username):
            emit('toast', {'msg': 'You cannot message this user.', 'cls': 'error'})
            return
        with db() as conn:
            cur = conn.execute('INSERT INTO messages (sender,receiver,content,msg_type) VALUES (?,?,?,?)', (username, target, content, mtype))
            mid = cur.lastrowid
        r = dm_room(username, target)
        socketio.emit('msg', {'id': mid, 'sender': username, 'content': content, 'type': mtype, 'ts': int(time.time()), 'room': target}, room=r)

@socketio.on('open_dm')
def on_open_dm(data):
    if 'username' not in session:
        return
    username = session['username']
    target = data.get('target', '')
    if not target or target == username:
        return
    r = dm_room(username, target)
    join_room(r)
    # Also join target if they're online
    tsid = online_users.get(target)
    if tsid:
        socketio.server.enter_room(tsid, r)
    for mid, sender, content, mtype, ts in get_dm_history(username, target):
        emit('msg', {'id': mid, 'sender': sender, 'content': content, 'type': mtype, 'ts': ts, 'room': target})

@socketio.on('add_friend')
def on_add_friend(data):
    if 'username' not in session:
        return
    username = session['username']
    target = data.get('target', '').strip()
    if not target or target == username:
        return
    if not get_user(target):
        emit('toast', {'msg': f'User "{target}" not found.', 'cls': 'error'})
        return
    try:
        with db() as conn:
            conn.execute('INSERT INTO friends (requester,receiver) VALUES (?,?)', (username, target))
        emit('toast', {'msg': f'✅ Friend request sent to {target}!', 'cls': 'success'})
        push_friends(target)
    except sqlite3.IntegrityError:
        emit('toast', {'msg': 'Request already sent.', 'cls': 'error'})

@socketio.on('accept_friend')
def on_accept_friend(data):
    if 'username' not in session:
        return
    username = session['username']
    requester = data.get('from', '')
    with db() as conn:
        conn.execute("UPDATE friends SET status='accepted' WHERE requester=? AND receiver=?", (requester, username))
    push_friends(username)
    push_friends(requester)
    emit('toast', {'msg': f'✅ You are now friends with {requester}!', 'cls': 'success'})

@socketio.on('decline_friend')
def on_decline_friend(data):
    if 'username' not in session:
        return
    username = session['username']
    requester = data.get('from', '')
    with db() as conn:
        conn.execute('DELETE FROM friends WHERE requester=? AND receiver=?', (requester, username))
    push_friends(username)

@socketio.on('remove_friend')
def on_remove_friend(data):
    if 'username' not in session:
        return
    username = session['username']
    target = data.get('target', '')
    with db() as conn:
        conn.execute('DELETE FROM friends WHERE (requester=? AND receiver=?) OR (requester=? AND receiver=?)',
                     (username, target, target, username))
    push_friends(username)
    push_friends(target)
    emit('toast', {'msg': f'Removed {target} from friends.', 'cls': 'info'})

@socketio.on('block_user')
def on_block(data):
    if 'username' not in session:
        return
    username = session['username']
    target = data.get('target', '')
    if not target or target == username:
        return
    try:
        with db() as conn:
            conn.execute('INSERT INTO blocks (blocker,blocked) VALUES (?,?)', (username, target))
        emit('toast', {'msg': f'🚫 {target} blocked.', 'cls': 'info'})
    except sqlite3.IntegrityError:
        pass

@socketio.on('unblock_user')
def on_unblock(data):
    if 'username' not in session:
        return
    username = session['username']
    target = data.get('target', '')
    with db() as conn:
        conn.execute('DELETE FROM blocks WHERE blocker=? AND blocked=?', (username, target))
    emit('toast', {'msg': f'✅ {target} unblocked.', 'cls': 'success'})

@socketio.on('typing')
def on_typing(data):
    if 'username' not in session:
        return
    username = session['username']
    room = data.get('room', 'global')
    target_room = 'global' if room == 'global' else dm_room(username, room)
    socketio.emit('user_typing', {'user': username, 'room': room}, room=target_room, include_self=False)


if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
