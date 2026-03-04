from flask import Flask, render_template, session
from flask_socketio import SocketIO, send
import sqlite3
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)

def init_db():
    with sqlite3.connect('chat.db') as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS messages (content TEXT)')

@app.route('/')
def home():
    if 'username' not in session:
        session['username'] = f"User_{os.urandom(2).hex()}"
    return render_template('main.html')

@socketio.on('connect')
def handle_connect():
    with sqlite3.connect('chat.db') as conn:
        cur = conn.cursor()
        cur.execute('SELECT content FROM messages ORDER BY rowid DESC LIMIT 50')
        rows = cur.fetchall()
        for row in reversed(rows):
            send(row[0])

@socketio.on('message')
def handle_message(msg):
    full_msg = f"{session['username']}: {msg}"
    with sqlite3.connect('chat.db') as conn:
        conn.execute('INSERT INTO messages (content) VALUES (?)', (full_msg,))
    send(full_msg, broadcast=True)

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
