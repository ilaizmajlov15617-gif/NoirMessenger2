import sqlite3
from datetime import datetime
import os
from werkzeug.utils import secure_filename

from flask import (
    Flask,
    g,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    send_file,
)
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = 'change-me-to-a-real-secret'
socketio = SocketIO(app)

DB_PATH = 'chat.db'
UPLOAD_FOLDER = 'avatars'
MEDIA_FOLDER = 'media_uploads'
STORIES_FOLDER = 'stories'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
ALLOWED_MEDIA_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'mp4', 'webm', 'ogg'}

for folder in (UPLOAD_FOLDER, MEDIA_FOLDER, STORIES_FOLDER):
    if not os.path.exists(folder):
        os.makedirs(folder)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- Database helpers ---

def get_db():
    """Возвращает sqlite3.Connection, создаёт БД/таблицу при первом запросе."""
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        db.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                avatar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )
        db.execute(
            '''
            CREATE TABLE IF NOT EXISTS groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                creator TEXT NOT NULL,
                avatar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )
        db.execute(
            '''
            CREATE TABLE IF NOT EXISTS group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(group_id, username),
                FOREIGN KEY(group_id) REFERENCES groups(id)
            )
            '''
        )
        db.execute(
            '''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                username TEXT NOT NULL,
                msg TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cid TEXT,
                msg_type TEXT DEFAULT 'text',
                media_file TEXT
            )
            '''
        )

        db.execute(
            '''
            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                filename TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            '''
        )

        # Миграции для старых баз
        info = db.execute("PRAGMA table_info(messages)").fetchall()
        if not any(row['name'] == 'room' for row in info):
            db.execute("ALTER TABLE messages ADD COLUMN room TEXT NOT NULL DEFAULT 'global'")
            db.execute("UPDATE messages SET room = 'global' WHERE room IS NULL")

        if not any(row['name'] == 'msg_type' for row in info):
            db.execute("ALTER TABLE messages ADD COLUMN msg_type TEXT DEFAULT 'text'")

        if not any(row['name'] == 'media_file' for row in info):
            db.execute("ALTER TABLE messages ADD COLUMN media_file TEXT")

        # Добавляем поля аватара/avatar к users если их нет
        user_info = db.execute("PRAGMA table_info(users)").fetchall()
        if not any(row['name'] == 'avatar' for row in user_info):
            db.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
            db.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        db.commit()
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# --- Простая в памяти "онлайн"-статистика ---
online_users = set()


def broadcast_online_users():
    """Отправить всем клиентам обновлённый список онлайн-пользователей."""
    if not online_users:
        socketio.emit('online', {'users': []}, broadcast=True)
        return

    conn = get_db()
    placeholders = ','.join(['?'] * len(online_users))
    rows = conn.execute(
        f"SELECT username, avatar FROM users WHERE username IN ({placeholders})",
        tuple(online_users),
    ).fetchall()
    socketio.emit('online', {'users': [dict(r) for r in rows]}, broadcast=True)


def get_current_user():
    return session.get('username')


def login_required(func):
    def wrapper(*args, **kwargs):
        if not get_current_user():
            return redirect(url_for('login'))
        return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    return wrapper


@app.route('/')
def index():
    return redirect(url_for('chat'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            return render_template('login.html', error='Введите логин и пароль')

        conn = get_db()
        user = conn.execute(
            'SELECT username, password_hash FROM users WHERE username = ?', (username,)
        ).fetchone()

        if not user or not check_password_hash(user['password_hash'], password):
            return render_template('login.html', error='Неверный логин или пароль')

        session['username'] = username
        return redirect(url_for('chat'))

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            return render_template('register.html', error='Введите логин и пароль')

        conn = get_db()
        try:
            conn.execute(
                'INSERT INTO users (username, password_hash) VALUES (?, ?)',
                (username, generate_password_hash(password)),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return render_template('register.html', error='Пользователь уже существует')

        session['username'] = username
        return redirect(url_for('chat'))

    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/chat')
@login_required
def chat():
    """Главная страница чата."""
    return render_template('chat.html', username=get_current_user())


@app.route('/settings')
@login_required
def settings():
    """Страница настроек профиля."""
    return render_template('settings.html', username=get_current_user())


@app.route('/create_group')
@login_required
def create_group():
    """Страница создания группы."""
    return render_template('create_group.html', username=get_current_user())


@app.route('/api/messages')
@login_required
def api_messages():
    """Возвращает историю сообщений для комнаты (с пагинацией)."""
    room = request.args.get('room', 'global')
    before = request.args.get('before')
    limit = int(request.args.get('limit', 50))

    conn = get_db()
    query = 'SELECT id, username, msg, timestamp, cid FROM messages WHERE room = ?'
    args = [room]

    if before:
        query += ' AND id < ?'
        args.append(before)

    query += ' ORDER BY id DESC LIMIT ?'
    args.append(limit)

    rows = conn.execute(query, args).fetchall()
    messages = [dict(row) for row in rows]
    return jsonify(list(reversed(messages)))


@app.route('/api/online')
@login_required
def api_online():
    """Возвращает список сейчас онлайн (с аватарками)."""
    conn = get_db()
    users = []
    if online_users:
        placeholders = ','.join(['?'] * len(online_users))
        rows = conn.execute(
            f"SELECT username, avatar FROM users WHERE username IN ({placeholders})",
            tuple(online_users),
        ).fetchall()
        users = [dict(r) for r in rows]
    return jsonify({'users': users})


@app.route('/api/search')
@login_required
def search_users():
    """Поиск пользователей по нику."""
    q = request.args.get('q', '').strip().lower()
    if len(q) < 1:
        return jsonify({'users': []})

    conn = get_db()
    users = conn.execute(
        'SELECT username, avatar FROM users WHERE LOWER(username) LIKE ? AND username != ? LIMIT 20',
        (f'%{q}%', get_current_user()),
    ).fetchall()
    return jsonify({'users': [dict(row) for row in users]})


@app.route('/profile/<user_to_view>')
@login_required
def profile(user_to_view):
    """Страница профиля пользователя."""
    conn = get_db()
    user = conn.execute(
        'SELECT username, avatar FROM users WHERE username = ?', (user_to_view,)
    ).fetchone()

    if not user:
        return "Пользователь не найден", 404

    return render_template('profile.html', profile_user=user_to_view, profile_avatar=user['avatar'])


@app.route('/profile/upload_avatar', methods=['POST'])
@login_required
def upload_avatar():
    """Загрузка аватарки пользователя."""
    username = get_current_user()
    if 'avatar' not in request.files:
        return jsonify({'success': False, 'error': 'Нет файла'}), 400

    file = request.files['avatar']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'Недопустимый формат'}), 400

    filename = f"{username}.{file.filename.rsplit('.', 1)[1].lower()}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    conn = get_db()
    conn.execute('UPDATE users SET avatar = ? WHERE username = ?', (filename, username))
    conn.commit()

    # Обновляем список онлайн, чтобы другие пользователи увидели новый аватар
    try:
        broadcast_online_users()
    except Exception:
        pass

    return jsonify({'success': True, 'avatar': filename, 'url': f'/avatars/{filename}'})


@app.route('/profile/change_password', methods=['POST'])
@login_required
def change_password():
    """Смена пароля пользователя."""
    data = request.get_json()
    current_pwd = data.get('current_password', '')
    new_pwd = data.get('new_password', '')
    
    if len(new_pwd) < 6:
        return jsonify({'success': False, 'error': 'Пароль минимум 6 символов'})
    
    username = get_current_user()
    conn = get_db()
    user = conn.execute('SELECT password_hash FROM users WHERE username = ?', (username,)).fetchone()
    
    if not user or not check_password_hash(user['password_hash'], current_pwd):
        return jsonify({'success': False, 'error': 'Неверный текущий пароль'})
    
    new_hash = generate_password_hash(new_pwd)
    conn.execute('UPDATE users SET password_hash = ? WHERE username = ?', (new_hash, username))
    conn.commit()
    
    return jsonify({'success': True})


@app.route('/profile/delete_account', methods=['POST'])
@login_required
def delete_account():
    """Удалить аккаунт."""
    username = get_current_user()
    conn = get_db()
    
    conn.execute('DELETE FROM messages WHERE username = ?', (username,))
    conn.execute('DELETE FROM group_members WHERE username = ?', (username,))
    
    avatar_file = os.path.join(UPLOAD_FOLDER, username + '.png')
    if os.path.exists(avatar_file):
        os.remove(avatar_file)
    
    conn.execute('DELETE FROM users WHERE username = ?', (username,))
    conn.commit()
    
    session.clear()
    return jsonify({'success': True})


@app.route('/api/groups', methods=['GET', 'POST'])
@login_required
def manage_groups():
    """Создание группы или получение списка групп пользователя."""
    username = get_current_user()
    conn = get_db()

    if request.method == 'POST':
        data = request.get_json()
        group_name = data.get('name', '').strip()
        members = data.get('members', [])
        
        if not group_name or len(group_name) < 2:
            return jsonify({'success': False, 'error': 'Имя группы минимум 2 символа'}), 400

        try:
            cursor = conn.execute(
                'INSERT INTO groups (name, creator) VALUES (?, ?)',
                (group_name, username),
            )
            group_id = cursor.lastrowid
            
            members.append(username)
            for member in set(members):
                try:
                    conn.execute(
                        'INSERT INTO group_members (group_id, username) VALUES (?, ?)',
                        (group_id, member),
                    )
                except:
                    pass
            
            conn.commit()
            return jsonify({'success': True, 'group_id': f'group_{group_id}'})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    # GET: получить группы пользователя
    user_groups = conn.execute(
        '''SELECT g.id, g.name, g.creator, g.avatar FROM groups g 
           JOIN group_members gm ON g.id = gm.group_id 
           WHERE gm.username = ? ORDER BY g.id DESC''',
        (username,),
    ).fetchall()
    return jsonify({'groups': [dict(row) for row in user_groups]})


@app.route('/avatars/<filename>')
def serve_avatar(filename):
    """Скачивание аватарки. Поддерживает как прямой путь к файлу, так и запрос по нику."""
    path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if os.path.exists(path):
        response = send_file(path)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response

    # Если файл не найден, попробуем найти аватар в базе по нику
    conn = get_db()
    row = conn.execute('SELECT avatar FROM users WHERE username = ?', (filename,)).fetchone()
    if row and row['avatar']:
        avatar_path = os.path.join(app.config['UPLOAD_FOLDER'], row['avatar'])
        if os.path.exists(avatar_path):
            response = send_file(avatar_path)
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return response

    # Заглушка: вернем 404, клиент покажет emoji
    return '', 404


@app.route('/audio/<filename>')
def serve_audio(filename):
    """Скачивание аудиосообщения."""
    audio_dir = 'audio_messages'
    return send_file(os.path.join(audio_dir, filename))


@app.route('/stories/<filename>')
def serve_story(filename):
    """Скачивание файла истории."""
    return send_file(os.path.join(STORIES_FOLDER, filename))


@app.route('/media/<filename>')
def serve_media(filename):
    """Скачивание фото/видео для чата."""
    return send_file(os.path.join(MEDIA_FOLDER, filename))


@app.route('/upload_media', methods=['POST'])
@login_required
def upload_media():
    """Загрузка фото/видео для чата."""
    if 'media' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400

    file = request.files['media']
    if not file or file.filename == '':
        return jsonify({'error': 'Пустой файл'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ALLOWED_MEDIA_EXTENSIONS:
        return jsonify({'error': 'Недопустимый формат'}), 400

    filename = f"media_{int(datetime.now().timestamp())}_{get_current_user()}.{ext}"
    filepath = os.path.join(MEDIA_FOLDER, filename)
    file.save(filepath)

    return jsonify({'url': f'/media/{filename}'})


@app.route('/api/stories', methods=['GET', 'POST'])
@login_required
def api_stories():
    """Список историй и загрузка новой."""
    conn = get_db()

    if request.method == 'POST':
        if 'story' not in request.files:
            return jsonify({'success': False, 'error': 'Нет файла'}), 400
        file = request.files['story']
        if not file or file.filename == '':
            return jsonify({'success': False, 'error': 'Пустой файл'}), 400

        filename = secure_filename(file.filename)
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        ftype = 'image' if ext in ('png', 'jpg', 'jpeg', 'gif') else 'audio'
        stored_name = f"story_{int(datetime.now().timestamp())}_{get_current_user()}.{ext}"
        filepath = os.path.join(STORIES_FOLDER, stored_name)
        file.save(filepath)

        conn.execute(
            'INSERT INTO stories (username, filename, type) VALUES (?, ?, ?)',
            (get_current_user(), stored_name, ftype),
        )
        conn.commit()

        return jsonify({'success': True, 'url': f'/stories/{stored_name}'})

    # GET: вернуть свежие истории (24 часа)
    rows = conn.execute(
        '''SELECT id, username, filename, type, created_at FROM stories
           WHERE created_at >= datetime('now', '-24 hours')
           ORDER BY created_at DESC LIMIT 50'''
    ).fetchall()
    return jsonify({'stories': [dict(r) for r in rows]})


@app.route('/upload_audio', methods=['POST'])
@login_required
def upload_audio():
    """Загрузка аудиосообщения."""
    if 'audio' not in request.files:
        return jsonify({'error': 'Нет аудио'}), 400

    file = request.files['audio']
    if not file:
        return jsonify({'error': 'Пустой файл'}), 400

    audio_dir = 'audio_messages'
    if not os.path.exists(audio_dir):
        os.makedirs(audio_dir)

    ext = 'webm' if 'webm' in file.filename else 'wav'
    filename = f"audio_{int(datetime.now().timestamp())}_{get_current_user()}.{ext}"
    filepath = os.path.join(audio_dir, filename)
    file.save(filepath)

    return jsonify({'audio_file': filename, 'url': f'/audio/{filename}'})


@socketio.on('connect')
def on_connect():
    username = get_current_user()
    if not username:
        return False

    online_users.add(username)
    conn = get_db()
    if online_users:
        placeholders = ','.join(['?'] * len(online_users))
        rows = conn.execute(
            f"SELECT username, avatar FROM users WHERE username IN ({placeholders})",
            tuple(online_users),
        ).fetchall()
        emit('online', {'users': [dict(r) for r in rows]}, broadcast=True)
    else:
        emit('online', {'users': []}, broadcast=True)


@socketio.on('disconnect')
def on_disconnect():
    username = get_current_user()
    if username and username in online_users:
        online_users.remove(username)
        broadcast_online_users()


@socketio.on('join')
def on_join(data):
    room = data.get('room', 'global')
    join_room(room)


@socketio.on('leave')
def on_leave(data):
    room = data.get('room', 'global')
    leave_room(room)


@socketio.on('message')
def handle_message(data):
    """Сохраняет сообщение и рассылает его в комнату."""
    username = get_current_user() or data.get('username', '')
    room = data.get('room', 'global')
    msg = data.get('msg', '')
    msg_type = data.get('msg_type', 'text')
    media_file = data.get('media_file', '')
    timestamp = datetime.now().strftime('%H:%M')
    cid = data.get('cid')

    conn = get_db()
    conn.execute(
        'INSERT INTO messages (room, username, msg, timestamp, cid, msg_type, media_file) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (room, username, msg, timestamp, cid, msg_type, media_file),
    )
    conn.commit()

    emit(
        'message',
        {
            'username': username,
            'room': room,
            'msg': msg,
            'timestamp': timestamp,
            'cid': cid,
            'msg_type': msg_type,
            'media_file': media_file,
        },
        room=room,
    )

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
