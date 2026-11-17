import os, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime, random, re, json, sys, threading, secrets, requests, resource, tempfile, socket
from collections import defaultdict, deque
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file, Response
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit

# Global process tracker
running_procs = {}
start_times = {}
# Per-user/per-server working directory for the EVO terminal session.
terminal_cwds = {}
PUBLIC_IP = os.environ.get('PUBLIC_IP', '127.0.0.1')
PUBLIC_PORT_MIN = 13506
PUBLIC_PORT_MAX = 13650
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@evo.local').strip()
# Never ship an administrator password in source; provide it through the environment.
# Never ship a default administrator password in source.
# Configure ADMIN_PASSWORD in the hosting environment; random fallback prevents blank/default login.
ADMIN_PASSWORD = (os.environ.get('ADMIN_PASSWORD') or '').strip() or secrets.token_urlsafe(32)

def generate_public_server_id(db):
    """Return a unique numeric public id suitable for /server/09156854."""
    for _ in range(100):
        candidate = f"{secrets.randbelow(100_000_000):08d}"
        if not db.execute('SELECT 1 FROM servers WHERE public_id=? LIMIT 1', (candidate,)).fetchone():
            return candidate
    raise RuntimeError('Could not allocate a public server id.')

def apply_process_limits():
    """Best-effort process limits; Docker remains the full isolation boundary."""
    # Do not set RLIMIT_NPROC for shared host processes: it is counted per Unix user,
    # so a low value can prevent an uploaded app from creating even its first thread.
    # Full process-count isolation belongs in Docker/cgroups, not this best-effort hook.
    for limit, value in ((resource.RLIMIT_CPU, 90), (resource.RLIMIT_NOFILE, 1024)):
        try:
            hard = resource.getrlimit(limit)[1]
            ceiling = min(value, hard) if hard != resource.RLIM_INFINITY else value
            resource.setrlimit(limit, (ceiling, ceiling))
        except (OSError, ValueError, AttributeError):
            pass

# Initialize SocketIO
socketio = SocketIO()

def get_db():
    # Always use the database shipped with this application, regardless of launch cwd.
    db_path = os.path.join(PROJECT_ROOT, 'storage', 'evo.db')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    storage_path = os.path.join(PROJECT_ROOT, 'storage')
    if not os.path.exists(storage_path): os.makedirs(storage_path)
    db = get_db()
    # User Table
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT, pfp TEXT DEFAULT 'default.png',
        role TEXT DEFAULT 'free',
        status TEXT DEFAULT 'active',
        server_limit INTEGER DEFAULT 1,
        notifications TEXT DEFAULT '',
        premium_until TEXT DEFAULT NULL,
        plan_notice TEXT DEFAULT '',
        plan_notice_seen INTEGER DEFAULT 1
    )''')
    # Safe migration for databases created by older versions.
    user_columns = {row[1] for row in db.execute('PRAGMA table_info(users)').fetchall()}
    if 'premium_until' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN premium_until TEXT")
    if 'plan_notice' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN plan_notice TEXT DEFAULT ''")
    if 'plan_notice_seen' not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN plan_notice_seen INTEGER DEFAULT 1")

    # Server Table
    # Migration: on an existing installation, preserve the first registered account as admin.
    first_user = db.execute('SELECT id FROM users ORDER BY id ASC LIMIT 1').fetchone()
    has_admin = db.execute("SELECT id FROM users WHERE lower(role)='admin' LIMIT 1").fetchone()
    if first_user and not has_admin:
        db.execute("UPDATE users SET role='admin', server_limit=999 WHERE id=?", (first_user['id'],))

    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, name TEXT, folder TEXT,
        status TEXT, startup TEXT, pid INTEGER,
        server_status TEXT DEFAULT 'active',
        public_ip TEXT DEFAULT '${PUBLIC_IP}',
        public_port INTEGER,
        description TEXT DEFAULT '',
        plan TEXT DEFAULT 'free',
        server_type TEXT DEFAULT 'python',
        custom_domain TEXT DEFAULT '',
        demo_ip TEXT DEFAULT '',
        public_id TEXT DEFAULT ''
    )''')
    # Safe migration for databases created by older versions.
    server_columns = {row[1] for row in db.execute('PRAGMA table_info(servers)').fetchall()}
    if 'public_ip' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN public_ip TEXT DEFAULT '${PUBLIC_IP}'")
    if 'public_port' not in server_columns:
        db.execute('ALTER TABLE servers ADD COLUMN public_port INTEGER')
    if 'description' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN description TEXT DEFAULT ''")
    if 'plan' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN plan TEXT DEFAULT 'free'")
    if 'server_type' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN server_type TEXT DEFAULT 'python'")
    if 'custom_domain' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN custom_domain TEXT DEFAULT ''")
    if 'demo_ip' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN demo_ip TEXT DEFAULT ''")
    if 'public_id' not in server_columns:
        db.execute("ALTER TABLE servers ADD COLUMN public_id TEXT DEFAULT ''")
    allocated_public_ids = set()
    for public_row in db.execute("SELECT id, public_id FROM servers").fetchall():
        current_public_id = str(public_row['public_id'] or '').strip()
        if not re.fullmatch(r'\d{8}', current_public_id) or current_public_id in allocated_public_ids:
            current_public_id = generate_public_server_id(db)
            db.execute('UPDATE servers SET public_id=? WHERE id=?', (current_public_id, public_row['id']))
        allocated_public_ids.add(current_public_id)
    for demo_row in db.execute("SELECT id, demo_ip FROM servers").fetchall():
        if not (demo_row['demo_ip'] or '').strip():
            demo = f"10.20.{(demo_row['id'] // 250) % 250}.{(demo_row['id'] % 250) + 1}"
            db.execute('UPDATE servers SET demo_ip=? WHERE id=?', (demo, demo_row['id']))
    existing_ports = {row[0] for row in db.execute('SELECT public_port FROM servers WHERE public_port IS NOT NULL').fetchall()}
    legacy_servers = db.execute('SELECT id FROM servers WHERE public_port IS NULL').fetchall()
    for legacy in legacy_servers:
        candidate = next((p for p in range(PUBLIC_PORT_MIN, PUBLIC_PORT_MAX + 1) if p not in existing_ports), None)
        if candidate is not None:
            db.execute('UPDATE servers SET public_ip=?, public_port=? WHERE id=?', (PUBLIC_IP, candidate, legacy[0]))
            existing_ports.add(candidate)
    # Support Ticket Table
    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT, status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Admin Settings Table
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY,
        username TEXT, email TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')

    admin_columns = {row[1] for row in db.execute('PRAGMA table_info(admin_settings)').fetchall()}
    if 'email' not in admin_columns:
        db.execute("ALTER TABLE admin_settings ADD COLUMN email TEXT")
    if not db.execute('SELECT * FROM admin_settings WHERE id=1').fetchone():
        db.execute('INSERT INTO admin_settings (id, username, email, password) VALUES (1, ?, ?, ?)', (ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD))
    elif ADMIN_PASSWORD:
        db.execute('UPDATE admin_settings SET username=?, email=?, password=? WHERE id=1', (ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD))
    else:
        db.execute('UPDATE admin_settings SET username=?, email=? WHERE id=1', (ADMIN_USERNAME, ADMIN_EMAIL))

    db.execute('CREATE TABLE IF NOT EXISTS security_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    reset_done = db.execute("SELECT value FROM security_meta WHERE key='account_reset_v1'").fetchone()
    if not reset_done:
        # Account reset requested by the owner: keep only the configured administrator.
        # The comparison is case-insensitive and whitespace-normalized.
        configured_email = (ADMIN_EMAIL or '').strip().lower()
        db.execute("DELETE FROM tickets WHERE user_id NOT IN (SELECT id FROM users WHERE lower(trim(email))=?)", (configured_email,))
        db.execute("DELETE FROM servers WHERE user_id NOT IN (SELECT id FROM users WHERE lower(trim(email))=?)", (configured_email,))
        db.execute("DELETE FROM users WHERE lower(trim(email)) != ? OR email IS NULL", (configured_email,))
        admin_user = db.execute("SELECT id FROM users WHERE lower(trim(email))=? LIMIT 1", (configured_email,)).fetchone()
        if not admin_user:
            db.execute('INSERT INTO users (fname, username, email, password, server_limit, role, status) VALUES (?,?,?,?,?,?,?)', ('Administrator', ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD, 999, 'admin', 'active'))
        else:
            db.execute("UPDATE users SET username=?, password=?, role='admin', status='active', server_limit=999 WHERE id=?", (ADMIN_USERNAME, ADMIN_PASSWORD, admin_user['id']))
        db.execute("INSERT INTO security_meta (key, value) VALUES ('account_reset_v1', 'completed')")

    db.commit()
    db.close()

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    # Allow normal project archives while keeping an upper bound against accidental abuse.
    app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
    app.config['BASE_STORAGE'] = os.path.join(PROJECT_ROOT, 'storage', 'instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(PROJECT_ROOT, 'static', 'uploads')
    request_windows = defaultdict(deque)
    blocked_name = re.compile(r'(^\.|\.\.|(?:^|/)\.(?:env|git|svn|ssh|htaccess|htpasswd)(?:/|$)|(?:^|/)(?:config|settings|secrets?)\.(?:php|py|js|json|ini|yml|yaml|env)$)', re.I)
    blocked_methods = {'PUT', 'DELETE', 'PATCH', 'TRACE', 'TRACK', 'CONNECT', 'OPTIONS', 'PROPFIND', 'PROPPATCH', 'MKCOL', 'COPY', 'MOVE'}

    @app.before_request
    def request_security_guard():
        if request.method in blocked_methods:
            return jsonify({'status': 'error', 'msg': 'HTTP method not allowed.'}), 405
        raw_target = f'{request.full_path} {request.path}'
        if any(token in raw_target.lower() for token in ('%2e%2e', '%00', '\\x00', '../', '..\\')):
            return jsonify({'status': 'error', 'msg': 'Unsafe request rejected.'}), 400
        # Reject cross-site state-changing requests while keeping API clients without an Origin usable.
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            origin = request.headers.get('Origin') or request.headers.get('Referer')
            if origin and not origin.lower().startswith((f'https://{request.host.lower()}', f'http://{request.host.lower()}')):
                return jsonify({'status': 'error', 'msg': 'Cross-site request rejected.'}), 403
        # Rate-limit state-changing requests only. Page loads and file-list GETs
        # must not consume the upload budget, especially behind a shared proxy IP.
        if request.method == 'POST':
            now = time.monotonic()
            key = request.remote_addr or 'unknown'
            window = request_windows[key]
            while window and now - window[0] > 60:
                window.popleft()
            limit = 60 if ('upload' in request.path or 'unzip' in request.path) else 240
            if len(window) >= limit:
                return jsonify({'status': 'error', 'msg': 'Rate limit exceeded.'}), 429
            window.append(now)
        if blocked_name.search(request.path):
            return jsonify({'status': 'error', 'msg': 'Hidden or sensitive path rejected.'}), 404
        return None

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({'status': 'error', 'msg': 'File exceeds the 1 MB limit.'}), 413

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
        response.headers.setdefault('Cache-Control', 'no-store' if request.path.startswith(('/admin', '/dashboard', '/files', '/terminal')) else 'no-cache')
        if request.is_secure:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        return response

    @app.before_request
    def enforce_server_isolation():
        # Every server/file endpoint is restricted to the owning account.
        if request.path.startswith('/server/') or request.path.startswith('/files/') or request.path.startswith('/terminal/'):
            # Terminal commands are always scoped to the selected server workspace.
            folder = (request.view_args or {}).get('folder')
            if not folder: return None
            if safe_server_root(folder) is None:
                return jsonify({'status': 'error', 'msg': 'Invalid server workspace ID.'}), 400
            if session.get('admin_logged'): return None
            user_id = session.get('user_id')
            if not user_id: return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
            db_guard = get_db()
            owned = db_guard.execute('SELECT 1 FROM servers WHERE folder=? AND user_id=?', (folder, user_id)).fetchone()
            db_guard.close()
            if not owned: return jsonify({'status': 'error', 'msg': 'Access denied for this server.'}), 403
            supplied_paths = [request.args.get('path', ''), request.form.get('path', '')]
            payload_guard = request.get_json(silent=True) or {}
            supplied_paths.append(payload_guard.get('path', ''))
            for relative in supplied_paths:
                relative_text = str(relative or '')
                normalized = os.path.normpath(relative_text)
                if os.path.isabs(relative_text) or normalized == '..' or normalized.startswith('..' + os.sep) or blocked_name.search(relative_text):
                    return jsonify({'status': 'error', 'msg': 'Unsafe path rejected.'}), 400
            route_name = (request.view_args or {}).get('name', '')
            if route_name and (route_name in {'.', '..'} or os.path.basename(route_name) != route_name or '\\' in route_name or blocked_name.search(route_name)):
                return jsonify({'status': 'error', 'msg': 'Unsafe file name rejected.'}), 400
            for candidate in (payload_guard.get('old', ''), payload_guard.get('new', '')):
                if candidate and (os.path.basename(str(candidate)) != str(candidate) or str(candidate) in {'.', '..'}):
                    return jsonify({'status': 'error', 'msg': 'Unsafe file name rejected.'}), 400
            for candidate in payload_guard.get('names', []) or []:
                if os.path.basename(str(candidate)) != str(candidate) or str(candidate) in {'.', '..'}:
                    return jsonify({'status': 'error', 'msg': 'Unsafe file name rejected.'}), 400
        return None

    if not os.path.exists(app.config['BASE_STORAGE']):
        os.makedirs(app.config['BASE_STORAGE'])
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])

    # Canonical storage layout: one physical directory per public server ID.
    # Existing installations are migrated once while preserving their files.
    storage_root = os.path.realpath(app.config['BASE_STORAGE'])

    def ensure_server_dockerfile(instance_path, public_id, server_type='python'):
        """Create the visible per-server Docker contract; the runner image remains outside user control."""
        dockerfile = os.path.join(instance_path, 'Dockerfile')
        if os.path.exists(dockerfile):
            return
        runtime = 'node:22-bookworm-slim' if str(server_type).lower() in {'node', 'nodejs'} else 'python:3.12-slim'
        content = (
            '# EVO HUB reserved server Docker contract for public ID ' + str(public_id) + '\\n'
            '# This file is informational and is mounted read-only at runtime.\\n'
            '# The immutable production image and entrypoint are managed outside this workspace.\\n'
            'FROM ' + runtime + '\\n'
            'WORKDIR /workspace\\n'
            'COPY . /workspace\\n'
        )
        try:
            with open(dockerfile, 'x', encoding='utf-8') as handle:
                handle.write(content)
            os.chmod(dockerfile, 0o444)
        except FileExistsError:
            pass
        except OSError:
            pass

    init_db()

    migration_db = get_db()
    for migration_row in migration_db.execute('SELECT id, folder, public_id, server_type FROM servers').fetchall():
        public_id = str(migration_row['public_id'] or '').strip()
        old_folder = str(migration_row['folder'] or '').strip()
        if not re.fullmatch(r'\d{8}', public_id):
            continue
        new_path = os.path.join(storage_root, public_id)
        old_path = os.path.join(storage_root, old_folder)
        if old_folder != public_id and os.path.isdir(old_path) and not os.path.exists(new_path):
            shutil.move(old_path, new_path)
        if old_folder != public_id and os.path.isdir(new_path):
            migration_db.execute('UPDATE servers SET folder=? WHERE id=?', (public_id, migration_row['id']))
        if os.path.isdir(new_path):
            ensure_server_dockerfile(new_path, public_id, migration_row['server_type'] or 'python')
    migration_db.commit()
    migration_db.close()

    def safe_server_root(folder):
        # A server workspace is addressable only by its eight-digit public ID.
        folder = str(folder or '')
        if not re.fullmatch(r'\d{8}', folder):
            return None
        candidate = os.path.realpath(os.path.join(storage_root, folder))
        if candidate == storage_root or not candidate.startswith(storage_root + os.sep):
            return None
        return candidate

    def host_execution_explicitly_allowed():
        # In a local/demo installation, fall back to the restricted host runner when
        # Docker Engine is unavailable. Production installations can force the safe
        # Docker-only policy with EVO_HUB_PRODUCTION=1.
        if os.environ.get('EVO_HUB_ALLOW_UNSAFE_HOST_EXECUTION') == '1':
            return True
        return not docker_sandbox_enabled() and os.environ.get('EVO_HUB_PRODUCTION') != '1'

    def docker_sandbox_enabled():
        # Check the daemon, not just the docker executable. A CLI without a running
        # Engine used to leave the UI stuck at "waiting for Docker Engine".
        if os.environ.get('EVO_HUB_DOCKER_ENABLED', '1') != '1' or shutil.which('docker') is None:
            return False
        try:
            result = subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=3)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def docker_container_name(folder):
        return f'evo-server-{str(folder)}'

    def python_pip_command(root, venv_dir):
        """Return a usable pip command even on Python builds without ensurepip."""
        venv_python = os.path.join(venv_dir, 'bin', 'python')
        if os.path.exists(venv_python):
            try:
                probe = subprocess.run([venv_python, '-m', 'pip', '--version'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                if probe.returncode == 0:
                    return [venv_python, '-m', 'pip'], venv_python, None
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                subprocess.run([venv_python, '-m', 'ensurepip', '--upgrade'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                probe = subprocess.run([venv_python, '-m', 'pip', '--version'],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                if probe.returncode == 0:
                    return [venv_python, '-m', 'pip'], venv_python, None
            except (OSError, subprocess.TimeoutExpired):
                pass
        package_dir = os.path.join(root, '.packages')
        os.makedirs(package_dir, exist_ok=True)
        return [sys.executable, '-m', 'pip', 'install', '--target', package_dir], sys.executable, package_dir

    def bootstrap_python_dependencies(root, log_file, timeout=900):
        """Install manifest/import dependencies and return the Python runtime to use."""
        venv_dir = os.path.join(root, '.venv')
        env_file = None
        try:
            handle = tempfile.NamedTemporaryFile(prefix='.evo-deps-', suffix='.env', dir=root, delete=False)
            env_file = handle.name
            handle.close()
            bootstrap = os.path.join(PROJECT_ROOT, 'docker', 'evo-dependency-bootstrap.py')
            command = [sys.executable, bootstrap, '--root', root, '--venv', venv_dir,
                       '--env-file', env_file, '--timeout', str(timeout)]
            completed = subprocess.run(command, cwd=root, stdout=log_file, stderr=subprocess.STDOUT,
                                       text=True, timeout=timeout + 30)
            selected_python = None
            pythonpath = None
            try:
                with open(env_file, encoding='utf-8') as generated:
                    for line in generated:
                        key, separator, value = line.strip().partition('=')
                        if separator and key == 'EVO_PYTHON':
                            selected_python = value
                        elif separator and key == 'EVO_PYTHONPATH':
                            pythonpath = value
            except OSError:
                pass
            if not selected_python:
                selected_python = os.path.join(venv_dir, 'bin', 'python')
                if not os.path.exists(selected_python):
                    selected_python = sys.executable
            return completed.returncode == 0, selected_python, pythonpath
        except subprocess.TimeoutExpired:
            log_file.write(f"[EVO-DEPS] Dependency installation timed out after {timeout} seconds.\\n")
            return False, os.path.join(venv_dir, 'bin', 'python'), None
        except OSError as exc:
            log_file.write(f"[EVO-DEPS] Dependency bootstrap unavailable: {exc}\\n")
            return False, os.path.join(venv_dir, 'bin', 'python'), None
        finally:
            if env_file:
                try:
                    os.remove(env_file)
                except OSError:
                    pass

    def docker_container_running(folder):
        """Read the container state directly; Docker mode does not expose a host PID."""
        if shutil.which('docker') is None:
            return False
        try:
            result = subprocess.run(
                ['docker', 'inspect', '--format', '{{.State.Running}}', docker_container_name(folder)],
                capture_output=True, text=True, timeout=3
            )
            return result.returncode == 0 and result.stdout.strip().lower() == 'true'
        except (OSError, subprocess.TimeoutExpired):
            return False

    def stop_docker_container(folder):
        if shutil.which('docker') is None:
            return
        try:
            subprocess.run(['docker', 'rm', '-f', docker_container_name(folder)], capture_output=True, timeout=10)
            subprocess.run(['docker', 'network', 'rm', f'evo-net-{folder}'], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    RESERVED_SERVER_FILES = {'Dockerfile', '.evohub-runtime'}

    def is_reserved_server_file(name):
        return str(name or '') in RESERVED_SERVER_FILES

    def safe_server_path(folder, *relative_parts):
        root = safe_server_root(folder)
        if root is None:
            return None
        candidate = os.path.realpath(os.path.join(root, *[str(part or '') for part in relative_parts]))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def remove_user_servers(db, user_id, premium_only=False):
        """Stop and remove servers owned by one account, optionally only Premium servers."""
        query = 'SELECT folder, pid FROM servers WHERE user_id=?'
        params = [user_id]
        if premium_only:
            query += " AND lower(COALESCE(plan, 'free'))='premium'"
        rows = db.execute(query, params).fetchall()
        for server in rows:
            folder = server['folder']
            tracked = running_procs.get(folder)
            pid = tracked.pid if tracked else server['pid']
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            running_procs.pop(folder, None)
            start_times.pop(folder, None)
            stop_docker_container(folder)
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path):
                shutil.rmtree(path)
        if premium_only:
            db.execute("DELETE FROM servers WHERE user_id=? AND lower(COALESCE(plan, 'free'))='premium'", (user_id,))
        else:
            db.execute('DELETE FROM servers WHERE user_id=?', (user_id,))

    def expire_premium_accounts():
        """Downgrade expired plans, remove their servers, and queue a one-time notice."""
        now = datetime.datetime.now(datetime.timezone.utc)
        db = get_db()
        rows = db.execute("SELECT id, premium_until FROM users WHERE lower(role)='premium' AND premium_until IS NOT NULL AND premium_until != ''").fetchall()
        changed = False
        for user in rows:
            try:
                expiry = datetime.datetime.fromisoformat(str(user['premium_until']).replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=datetime.timezone.utc)
                expiry = expiry.astimezone(datetime.timezone.utc)
            except (TypeError, ValueError):
                continue
            if expiry <= now:
                remove_user_servers(db, user['id'], premium_only=True)
                db.execute("UPDATE users SET role='free', premium_until=NULL, plan_notice=?, plan_notice_seen=0 WHERE id=?", ('تم إنهاء الخطة الأسطورية، وتم حذف السيرفرات المرتبطة بها.', user['id']))
                changed = True
        if changed:
            db.commit()
        db.close()

    socketio.init_app(app)

    @app.before_request
    def expire_plans_before_request():
        expire_premium_accounts()
        user_id = session.get('user_id')
        if not user_id or request.endpoint in {'login', 'admin_login'}:
            return None
        db_guard = get_db()
        user_guard = db_guard.execute('SELECT status FROM users WHERE id=?', (user_id,)).fetchone()
        db_guard.close()
        if user_guard and user_guard['status'] == 'banned':
            session.clear()
            return redirect(url_for('login', notice='banned'))
        return None

    def run_plan_expiry_worker():
        while True:
            try:
                expire_premium_accounts()
            except Exception:
                pass
            time.sleep(60)

    if os.environ.get('NEHOST_DISABLE_EXPIRY_WORKER') != '1':
        threading.Thread(target=run_plan_expiry_worker, name='evo-plan-expiry', daemon=True).start()

    def get_docker_stats(folder):
        """Return live Docker metrics for one isolated container, or None when unavailable."""
        container = f"evo-server-{folder}"
        try:
            result = subprocess.run(
                ['docker', 'stats', '--no-stream', '--format', '{{json .}}', container],
                capture_output=True, text=True, timeout=3
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            data = json.loads(result.stdout.strip().splitlines()[-1])
            mem_usage = str(data.get('MemUsage') or '0MB').split('/', 1)[0].strip()
            mem_percent = str(data.get('MemPerc') or '').strip()
            return {'cpu': str(data.get('CPUPerc') or '0.00%'), 'ram': f"{mem_usage} {mem_percent}".strip(), 'source': 'docker'}
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def server_resource_limits(plan):
        return {'cpu_percent': 100.0 if (plan or 'free').lower() == 'free' else 200.0, 'ram_mb': 512 if (plan or 'free').lower() == 'free' else 2048, 'disk_mb': 1024 if (plan or 'free').lower() == 'free' else 10240}

    def parse_memory_mb(value):
        try:
            text = str(value or '0').split('%', 1)[0].strip().split()[0].upper()
            multiplier = 1
            if text.endswith('GIB') or text.endswith('GB'): multiplier, text = 1024, text[:-3] if text.endswith('GIB') else text[:-2]
            elif text.endswith('MIB') or text.endswith('MB'): text = text[:-3] if text.endswith('MIB') else text[:-2]
            elif text.endswith('KIB') or text.endswith('KB'): multiplier, text = 1 / 1024, text[:-3] if text.endswith('KIB') else text[:-2]
            return float(text) * multiplier
        except (TypeError, ValueError): return 0.0

    def folder_disk_mb(path):
        total = 0
        try:
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs if d not in {'.venv', 'node_modules', '__pycache__'}]
                for filename in files:
                    try: total += os.path.getsize(os.path.join(root, filename))
                    except OSError: pass
        except OSError: pass
        return round(total / (1024 * 1024), 1)

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)

        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    @app.route('/')
    def home():
      return render_template('index.html')

    # --- UPDATED SIGNUP ROUTE ---
    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        # The first account is the bootstrap administrator; later public signup is disabled.
        count_db = get_db()
        account_count = count_db.execute('SELECT COUNT(*) AS total FROM users').fetchone()['total']
        count_db.close()
        if not session.get('admin_logged'):
            if request.method == 'POST':
                return jsonify({'status': 'error', 'msg': 'Public account creation is disabled.'}), 403
            return redirect(url_for('login'))
        if request.method == 'POST':
            fname = request.form.get('fname')
            lname = request.form.get('lname')
            username = request.form.get('username')
            email = request.form.get('email')
            pwd = request.form.get('password')
            cpwd = request.form.get('confirm_password')
            pfp = request.files.get('pfp')

            if pwd != cpwd:
                return jsonify({'status': 'error', 'msg': 'Passwords do not match!'}), 400

            db = get_db()
            # Check if user already exists
            existing_user = db.execute('SELECT id FROM users WHERE email=? OR username=?', (email, username)).fetchone()
            if existing_user:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Email or Username already taken!'}), 400

            pfp_name = 'default.png'
            if pfp:
                pfp_name = secure_filename(pfp.filename)
                pfp.save(os.path.join(app.config['UPLOAD_FOLDER'], pfp_name))

            # The first account becomes the sole bootstrap administrator.
            is_first_account = db.execute('SELECT COUNT(*) AS total FROM users').fetchone()['total'] == 0
            account_role = 'admin' if is_first_account else 'free'
            account_limit = 999 if is_first_account else 1
            db.execute('''INSERT INTO users
                (fname, lname, username, email, password, pfp, server_limit, role, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (fname, lname, username, email, pwd, pfp_name, account_limit, account_role, 'active'))

            db.commit()
            db.close()
            return jsonify({'status': 'success', 'url': url_for('login'), 'is_admin': is_first_account})

        return render_template('web/signup.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email')
            pwd = request.form.get('password')
            email = (email or '').strip().lower()
            pwd = (pwd or '').strip()
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE (lower(email)=? OR lower(username)=?) AND password=?', (email, email, pwd)).fetchone()
            if not user and email == ADMIN_EMAIL.lower() and pwd == ADMIN_PASSWORD:
                # Bootstrap/login recovery: ensure the configured admin also has a user account.
                user = db.execute('SELECT * FROM users WHERE lower(email)=?', (ADMIN_EMAIL.lower(),)).fetchone()
                if user:
                    db.execute("UPDATE users SET role='admin', status='active', server_limit=999 WHERE id=?", (user['id'],))
                    db.commit()
                    user = db.execute('SELECT * FROM users WHERE id=?', (user['id'],)).fetchone()
                else:
                    db.execute('INSERT INTO users (fname, username, email, password, server_limit, role, status) VALUES (?,?,?,?,?,?,?)', ('Administrator', ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD, 999, 'admin', 'active'))
                    db.commit()
                    user = db.execute('SELECT * FROM users WHERE lower(email)=?', (ADMIN_EMAIL.lower(),)).fetchone()
            db.close()

            if user:
                if user['status'] == 'banned':
                    session.clear()
                    return jsonify({'status': 'banned', 'msg': 'هذا الحساب محظور ولا يمكنه تسجيل الدخول.'}), 403
                session['user_id'] = user['id']
                is_admin = (user['role'] or '').lower() == 'admin'
                if is_admin:
                    session['admin_logged'] = True
                return jsonify({'status': 'success', 'url': url_for('admin_panel' if is_admin else 'dashboard'), 'is_admin': is_admin}), 200
            else:
                return jsonify({'status': 'error', 'msg': 'Invalid credentials!'}), 401
        return render_template('web/login.html')

    @app.route('/my-files')
    def my_files():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('web/files.html')

    @app.route('/terminal')
    def terminal_page():
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return render_template('web/terminal.html')

    @app.route('/terminal/execute/<folder>', methods=['POST'])
    def terminal_execute(folder):
        """Run a user command inside one owned server workspace.

        The command never receives a project-independent cwd. It is run from the
        selected instance directory, with a bounded timeout and output size.
        The explicit cd handler keeps the terminal session's cwd inside that
        directory while allowing normal shell pipelines and package commands.
        """
        data = request.get_json(silent=True) or {}
        command = str(data.get('command') or '').strip()
        requested_cwd = str(data.get('cwd') or '').strip()
        if not command:
            return jsonify({'status': 'error', 'msg': 'Enter a command first.'}), 400
        if not docker_sandbox_enabled() and not host_execution_explicitly_allowed():
            return jsonify({'status': 'error', 'msg': 'Terminal execution is disabled until the Docker sandbox is configured for this server.'}), 503
        root = safe_server_root(folder)
        if root is None or not os.path.isdir(root):
            return jsonify({'status': 'error', 'msg': 'Server workspace not found.'}), 404
        session_key = f"{session.get('user_id', 'admin')}:{folder}"
        current = terminal_cwds.get(session_key, root)
        if requested_cwd:
            candidate = os.path.realpath(os.path.join(root, requested_cwd))
            if candidate == root or candidate.startswith(root + os.sep):
                current = candidate if os.path.isdir(candidate) else current
        if not (current == root or current.startswith(root + os.sep)):
            current = root
        try:
            import shlex
            parts = shlex.split(command)
        except ValueError as exc:
            return jsonify({'status': 'error', 'msg': f'Command parsing error: {exc}'}), 400
        package_notice = ''
        package_spec = re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:==|!=|~=|>=|<=|>|<)[A-Za-z0-9_.+*!-]+', command)
        if package_spec:
            package_notice = f"[EVO] Package specification detected; running: pip install {command}\\n"
            command = f'pip install {command}'
            parts = shlex.split(command)
        if parts and parts[0] == 'cd' and len(parts) <= 2:
            target = parts[1] if len(parts) == 2 else '~'
            if target in ('~', '/', ''):
                target_path = root
            else:
                target_path = os.path.realpath(os.path.join(current, target))
            if not (target_path == root or target_path.startswith(root + os.sep)):
                return jsonify({'status': 'error', 'msg': 'Terminal path must stay inside this server workspace.', 'cwd': os.path.relpath(current, root)}), 400
            if not os.path.isdir(target_path):
                return jsonify({'status': 'error', 'msg': f'cd: no such directory: {target}', 'cwd': os.path.relpath(current, root)}), 400
            terminal_cwds[session_key] = target_path
            return jsonify({'status': 'success', 'output': '', 'cwd': os.path.relpath(target_path, root) if target_path != root else '~', 'exit_code': 0})
        blocked = re.compile(r'(^|\s)(sudo|su|shutdown|reboot|poweroff|mkfs|fdisk|mount|umount|chroot)(\s|$)|rm\s+-[rf]*\s+/(\s|$)|:\(\)\s*\{', re.I)
        if blocked.search(command):
            return jsonify({'status': 'error', 'msg': 'This system-level command is disabled in the workspace terminal.', 'cwd': os.path.relpath(current, root) if current != root else '~'}), 403
        if docker_sandbox_enabled():
            rel_cwd = os.path.relpath(current, root)
            container_cwd = '/workspace' if rel_cwd == '.' else '/workspace/' + rel_cwd
            try:
                completed = subprocess.run(
                    ['docker', 'exec', '-i', '-w', container_cwd, docker_container_name(folder), '/bin/sh', '-lc', command],
                    capture_output=True, text=True, timeout=90
                )
            except subprocess.TimeoutExpired as exc:
                output = ((exc.stdout or '') if isinstance(exc.stdout, str) else '') + ((exc.stderr or '') if isinstance(exc.stderr, str) else '')
                return jsonify({'status': 'timeout', 'output': (package_notice + output + '\\n[process timed out after 90 seconds]')[-16000:], 'cwd': rel_cwd if rel_cwd != '.' else '~', 'exit_code': 124}), 408
            except OSError as exc:
                return jsonify({'status': 'error', 'msg': f'Docker terminal unavailable: {exc}', 'cwd': rel_cwd if rel_cwd != '.' else '~'}), 503
            output = (package_notice + (completed.stdout or '') + (completed.stderr or ''))[-16000:]
            terminal_cwds[session_key] = current
            try:
                with open(os.path.join(root, 'terminal.log'), 'a', encoding='utf-8') as audit:
                    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    audit.write(f"[{stamp}] $ {command}\\n{output[-4000:]}\\n")
            except OSError:
                pass
            return jsonify({'status': 'success' if completed.returncode == 0 else 'error', 'output': output, 'cwd': rel_cwd if rel_cwd != '.' else '~', 'exit_code': completed.returncode})
        env = os.environ.copy()
        env['EVO_TERMINAL'] = '1'
        env['EVO_SERVER_FOLDER'] = folder
        venv_dir = os.path.join(root, '.venv')
        venv_bin = os.path.join(venv_dir, 'bin')
        wants_pip = bool(parts and (parts[0] in {'pip', 'pip3'} or (len(parts) >= 3 and parts[0] in {'python', 'python3'} and parts[1:3] == ['-m', 'pip'])))
        if wants_pip and not os.path.exists(os.path.join(venv_bin, 'python')):
            try:
                prepared = subprocess.run([sys.executable, '-m', 'venv', venv_dir], cwd=root, capture_output=True, text=True, timeout=60)
                if prepared.returncode != 0:
                    return jsonify({'status': 'error', 'msg': 'Could not prepare the isolated Python environment. Check the server files.', 'output': (prepared.stdout or '') + (prepared.stderr or ''), 'cwd': os.path.relpath(current, root) if current != root else '~'}), 400
            except subprocess.TimeoutExpired:
                return jsonify({'status': 'timeout', 'msg': 'Preparing the isolated Python environment timed out.', 'cwd': os.path.relpath(current, root) if current != root else '~'}), 408
        if os.path.isdir(venv_bin):
            # Put the server's isolated tools first, so pip never resolves to
            # the host's uv compatibility wrapper.
            env['PATH'] = venv_bin + os.pathsep + env.get('PATH', '')
            env['VIRTUAL_ENV'] = venv_dir
        else:
            local_bin = os.path.join(root, '.evo', 'bin')
            os.makedirs(local_bin, exist_ok=True)
            pip_shim = os.path.join(local_bin, 'pip')
            if not os.path.exists(pip_shim):
                with open(pip_shim, 'w', encoding='utf-8') as shim:
                    shim.write('#!/bin/sh\nexec python3 -m pip "$@"\n')
                os.chmod(pip_shim, 0o755)
            env['PATH'] = local_bin + os.pathsep + env.get('PATH', '')
        try:
            completed = subprocess.run(command, cwd=current, shell=True, executable='/bin/bash', capture_output=True, text=True, timeout=90, env=env, preexec_fn=apply_process_limits)
            output = (package_notice + (completed.stdout or '') + (completed.stderr or ''))[-16000:]
            terminal_cwds[session_key] = current
            try:
                with open(os.path.join(root, 'terminal.log'), 'a', encoding='utf-8') as audit:
                    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    audit.write(f"[{stamp}] $ {command}\\n{output[-4000:]}\\n")
            except OSError:
                pass
            return jsonify({'status': 'success' if completed.returncode == 0 else 'error', 'output': output, 'cwd': os.path.relpath(current, root) if current != root else '~', 'exit_code': completed.returncode})
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or '') if isinstance(exc.stdout, str) else '') + ((exc.stderr or '') if isinstance(exc.stderr, str) else '')
            return jsonify({'status': 'timeout', 'output': (output + '\\n[process timed out after 90 seconds]')[-16000:], 'cwd': os.path.relpath(current, root) if current != root else '~', 'exit_code': 124}), 408
        except Exception as exc:
            return jsonify({'status': 'error', 'msg': str(exc), 'cwd': os.path.relpath(current, root) if current != root else '~'}), 500

    @app.route('/guide')
    def guide():
        if 'user_id' not in session: return redirect(url_for('login'))
        return render_template('web/guide.html')

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    @app.route('/profile/update', methods=['POST'])
    def update_profile():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        uid = session['user_id']
        fname = request.form.get('fname')
        lname = request.form.get('lname')
        pwd = request.form.get('password')
        db = get_db()
        if pwd:
            db.execute('UPDATE users SET fname=?, lname=?, password=? WHERE id=?', (fname, lname, pwd, uid))
        else:
            db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, uid))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/profile/details', methods=['POST'])
    def update_profile_details():
        if 'user_id' not in session: return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
        data = request.get_json(silent=True) or {}
        fname = (data.get('fname') or '').strip()[:60]
        lname = (data.get('lname') or '').strip()[:60]
        if not fname: return jsonify({'status': 'error', 'msg': 'First name is required.'}), 400
        db = get_db(); db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, session['user_id'])); db.commit(); db.close()
        return jsonify({'status': 'success', 'msg': 'Profile updated successfully.'})

    @app.route('/account-settings')
    def account_settings():
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db(); user = db.execute('SELECT fname, lname, email, username, pfp FROM users WHERE id=?', (session['user_id'],)).fetchone(); db.close()
        return render_template('web/account_settings.html', user=user)

    @app.route('/profile/password', methods=['POST'])
    def change_password():
        if 'user_id' not in session: return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
        data = request.get_json(silent=True) or {}
        current = data.get('current_password') or ''; new_password = data.get('new_password') or ''; confirm = data.get('confirm_password') or ''
        if len(new_password) < 6: return jsonify({'status': 'error', 'msg': 'New password must be at least 6 characters.'}), 400
        if new_password != confirm: return jsonify({'status': 'error', 'msg': 'Passwords do not match.'}), 400
        db = get_db(); user = db.execute('SELECT password FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not user or user['password'] != current:
            db.close(); return jsonify({'status': 'error', 'msg': 'Current password is incorrect.'}), 400
        db.execute('UPDATE users SET password=? WHERE id=?', (new_password, session['user_id'])); db.commit(); db.close()
        return jsonify({'status': 'success', 'msg': 'Password updated successfully.'})

    def _public_server_record(public_id):
        if not re.fullmatch(r'\d{8}', str(public_id or '')):
            return None
        db = get_db()
        srv = db.execute('SELECT name, folder, public_id, public_ip, public_port, demo_ip, server_status, pid, server_type FROM servers WHERE public_id=?', (public_id,)).fetchone()
        db.close()
        return srv

    def _server_is_online(srv):
        if not srv:
            return False
        if str(srv['server_status'] or '').lower() in {'suspended', 'stopped', 'offline'}:
            return False

        # The public URL is only considered online when the assigned service
        # port accepts TCP connections.  A process can still exist while its
        # application has crashed, is booting, or is no longer listening.
        try:
            port = int(srv['public_port'] or 0)
            if 1 <= port <= 65535:
                with socket.create_connection(('127.0.0.1', port), timeout=1.2):
                    return True
                return False
        except (OSError, TypeError, ValueError):
            pass

        # Legacy records without an assigned port retain the old fallback.
        if srv['pid'] and psutil.pid_exists(srv['pid']):
            try:
                if psutil.Process(srv['pid']).is_running(): return True
            except Exception:
                pass
        tracked = running_procs.get(srv['folder'])
        return bool(tracked and tracked.poll() is None)

    @app.route('/server/<public_id>/workspace')
    def server_workspace(public_id):
        """Authenticated full workspace for the owner; public /server/<id> stays app-only."""
        if 'user_id' not in session:
            return redirect(url_for('login'))
        srv = _public_server_record(public_id)
        if not srv:
            return render_template('web/server_status.html', server=None), 404
        is_admin = bool(session.get('admin_logged'))
        db = get_db()
        owned = db.execute('SELECT 1 FROM servers WHERE public_id=? AND (user_id=? OR ?=1) LIMIT 1', (public_id, session['user_id'], 1 if is_admin else 0)).fetchone()
        db.close()
        if not owned:
            return 'Access denied: this server belongs to another account.', 403
        return render_template('web/server_workspace.html', server=dict(srv), public_url=url_for('public_server_page', public_id=public_id, _external=True))

    @app.route('/server/<public_id>', defaults={'subpath': ''}, methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    @app.route('/server/<public_id>/<path:subpath>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    def public_server_page(public_id, subpath=''):
        """Public server site: proxy to the selected app when online, status page otherwise."""
        srv = _public_server_record(public_id)
        if not srv:
            return render_template('web/server_status.html', server=None), 404
        # The public URL is always a status/application URL.  Workspace access
        # remains available explicitly at /server/<id>/workspace.
        online = _server_is_online(srv)
        public_url = url_for('public_port_page', port_token=f':{int(srv["public_port"] or 0)}', _external=True)
        status_view = {**dict(srv), 'online': online, 'public_url': public_url}
        # Keep the root URL useful even before deployment: it shows EVO HUB status.
        # Once the server is running, the same URL behaves like that server's site.
        if not online:
            return render_template('web/server_status.html', server=status_view)
        upstream = f"http://127.0.0.1:{int(srv['public_port'] or 5000)}/{subpath}"
        if request.query_string:
            upstream += '?' + request.query_string.decode('utf-8', errors='ignore')
        hop_by_hop = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length'}
        forward_headers = {key: value for key, value in request.headers.items() if key.lower() not in hop_by_hop}
        forward_headers['X-EVO-HUB-SERVER-ID'] = str(public_id)
        forward_headers['X-Forwarded-Prefix'] = f'/server/{public_id}'
        try:
            upstream_response = requests.request(request.method, upstream, headers=forward_headers, data=request.get_data(cache=True), allow_redirects=False, timeout=20)
        except requests.RequestException:
            return render_template('web/server_status.html', server={**status_view, 'online': False}), 503
        response_headers = [(key, value) for key, value in upstream_response.headers.items() if key.lower() not in hop_by_hop]
        response_headers.append(('X-EVO-HUB-SERVER', str(public_id)))
        return Response(upstream_response.content, upstream_response.status_code, response_headers)

    @app.route('/<port_token>', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'])
    def public_port_page(port_token):
        """Public port alias, e.g. https://host/:2274.

        The colon is intentionally part of the path (not the TCP host-port
        syntax). This lets one public web origin route each server by the
        port number shown to the user while still displaying a status page
        when that server is stopped.
        """
        match = re.fullmatch(r':([0-9]{1,5})', str(port_token or ''))
        if not match:
            return jsonify({'status': 'error', 'msg': 'Not found.'}), 404
        port = int(match.group(1))
        db = get_db()
        srv = db.execute('SELECT public_id FROM servers WHERE public_port=? LIMIT 1', (port,)).fetchone()
        db.close()
        if not srv:
            return render_template('web/server_status.html', server=None), 404
        return public_server_page(srv['public_id'], '')

    @app.route('/server-status/<folder>')
    def public_server_status(folder):
        db = get_db(); srv = db.execute('SELECT name, folder, public_id, public_ip, public_port, demo_ip, server_status, pid, server_type FROM servers WHERE folder=?', (folder,)).fetchone(); db.close()
        if not srv: return render_template('web/server_status.html', server=None), 404
        online = docker_container_running(folder)
        if not online and srv['pid'] and psutil.pid_exists(srv['pid']):
            try: online = psutil.Process(srv['pid']).is_running()
            except Exception: online = False
        if folder in running_procs and running_procs[folder].poll() is None: online = True
        public_url = url_for('public_port_page', port_token=f':{int(srv["public_port"] or 0)}', _external=True) if srv['public_port'] else url_for('public_server_status', folder=folder, _external=True)
        return render_template('web/server_status.html', server={**dict(srv), 'online': online, 'public_url': public_url})

    @app.route('/ticket/create', methods=['POST'])
    def create_ticket():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO tickets (user_id, subject, message) VALUES (?,?,?)', (session['user_id'], d['subject'], d['message']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/api/announcement')
    def get_announcement():
        db = get_db()
        conf = db.execute('SELECT popup_title, popup_msg, popup_img, show_popup FROM admin_settings WHERE id=1').fetchone()
        db.close()
        return jsonify(dict(conf))

    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            user = (request.form.get('username') or '').strip().lower()
            pwd = (request.form.get('password') or '').strip()
            db = get_db()
            admin = db.execute('SELECT * FROM admin_settings WHERE (lower(username)=? OR lower(email)=?) AND password=?', (user, user, pwd)).fetchone()
            db.close()
            # Accept the configured administrator credentials even if an older database is present.
            configured_admin = user == ADMIN_EMAIL.lower() and pwd == ADMIN_PASSWORD
            if admin or configured_admin:
                session['admin_logged'] = True
                return redirect(url_for('admin_panel'))
        return render_template('web/admin_login.html')

    @app.route('/admin')
    def admin_home():
        return redirect(url_for('admin_panel'))

    @app.route('/admin/panel')
    def admin_panel():
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        if not session.get('admin_logged'): return jsonify({})
        db = get_db()
        users = db.execute('SELECT * FROM users').fetchall()
        user_list = []
        total_cpu = psutil.cpu_percent()
        total_ram = psutil.virtual_memory().percent
        total_servers = db.execute('SELECT COUNT(*) AS total FROM servers').fetchone()['total']
        active_servers = 0
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = 0
            for s in srvs:
                is_on = False
                if s['pid'] and psutil.pid_exists(s['pid']):
                    try:
                        proc = psutil.Process(s['pid'])
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            is_on = True
                    except: pass
                elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                    is_on = True
                if is_on:
                    active_srvs += 1
                    active_servers += 1
            user_list.append({
                'id': u['id'], 'fname': u['fname'], 'email': u['email'],
                'srv_count': len(srvs), 'active_srvs': active_srvs,
                'status': u['status'], 'role': u['role'], 'server_limit': u['server_limit'],
                'premium_until': u['premium_until'], 'plan_active': (u['role'] or '').lower() == 'premium'
            })
        db.close()
        banned_users = sum(1 for u in user_list if u['status'] == 'banned')
        return jsonify({'users': user_list, 'sys_cpu': f"{total_cpu}%", 'sys_ram': f"{total_ram}%", 'total_servers': total_servers, 'active_servers': active_servers, 'banned_users': banned_users})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        d = request.get_json(silent=True) or {}
        try:
            user_id = int(d.get('user_id'))
            limit = max(1, min(50, int(d.get('limit', 1))))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Invalid user or server limit.'}), 400
        role = (d.get('role') or '').strip().lower()
        status = (d.get('status') or '').strip().lower()
        if role not in {'free', 'premium', 'admin'}:
            return jsonify({'status': 'error', 'msg': 'Invalid plan.'}), 400
        if status not in {'active', 'banned'}:
            return jsonify({'status': 'error', 'msg': 'Invalid account status.'}), 400
        db = get_db()
        target = db.execute('SELECT id, role FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (target['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator accounts cannot be changed here.'}), 403
        if role == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'A regular account cannot be promoted to administrator.'}), 403
        db.execute('UPDATE users SET role=?, status=?, server_limit=? WHERE id=?', (role, status, limit, user_id))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'role': role, 'status_value': status, 'server_limit': limit})

    @app.route('/admin/user/plan', methods=['POST'])
    def update_user_plan():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get('user_id'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Invalid account.'}), 400
        action = (payload.get('action') or 'activate').strip().lower()
        if action not in {'activate', 'deactivate'}:
            return jsonify({'status': 'error', 'msg': 'Invalid plan action.'}), 400
        db = get_db()
        target = db.execute('SELECT id, role FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (target['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator accounts cannot be changed here.'}), 403
        if action == 'deactivate':
            db.execute("UPDATE users SET role='free', premium_until=NULL WHERE id=?", (user_id,))
            db.execute("UPDATE servers SET plan='free' WHERE user_id=?", (user_id,))
            message = 'The legendary plan was stopped for this account.'
            expiry = None
        else:
            try:
                duration_days = int(payload.get('duration_days', 0) or 0)
            except (TypeError, ValueError):
                db.close()
                return jsonify({'status': 'error', 'msg': 'Duration must be a number of days.'}), 400
            if duration_days < 0 or duration_days > 3650:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Duration must be between 1 and 3650 days, or 0 for no expiry.'}), 400
            expiry = None
            if duration_days:
                expiry = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=duration_days)).isoformat()
            db.execute("UPDATE users SET role='premium', premium_until=?, plan_notice='', plan_notice_seen=1 WHERE id=?", (expiry, user_id))
            message = 'The legendary plan was activated for this account.'
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'role': 'premium' if action == 'activate' else 'free', 'premium_until': expiry, 'msg': message})

    @app.route('/admin/user/password', methods=['POST'])
    def admin_change_user_password():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        payload = request.get_json(silent=True) or {}
        try:
            user_id = int(payload.get('user_id'))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Invalid account.'}), 400
        password = (payload.get('password') or '').strip()
        if len(password) < 6:
            return jsonify({'status': 'error', 'msg': 'Password must be at least 6 characters.'}), 400
        db = get_db()
        target = db.execute('SELECT id, role FROM users WHERE id=?', (user_id,)).fetchone()
        if not target:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (target['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator passwords cannot be changed here.'}), 403
        db.execute('UPDATE users SET password=? WHERE id=?', (password, user_id))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'msg': 'Password updated successfully.'})

    @app.route('/notifications/plan/ack', methods=['POST'])
    def acknowledge_plan_notice():
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'msg': 'Authentication required.'}), 401
        db = get_db()
        db.execute('UPDATE users SET plan_notice_seen=1 WHERE id=?', (session['user_id'],))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/set-popup', methods=['POST'])
    def set_popup():
        if not session.get('admin_logged'): return jsonify({'status':'error'})
        title, msg, show = request.form.get('title'), request.form.get('msg'), request.form.get('show')
        img = request.files.get('image')
        db = get_db()
        old_data = db.execute('SELECT popup_img FROM admin_settings WHERE id=1').fetchone()
        img_name = old_data['popup_img'] if old_data else None
        if img:
            img_name = secure_filename(img.filename)
            img.save(os.path.join(app.config['UPLOAD_FOLDER'], img_name))
        db.execute('UPDATE admin_settings SET popup_title=?, popup_msg=?, popup_img=?, show_popup=? WHERE id=1', (title, msg, img_name, 1 if show == 'true' else 0))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/send-warning', methods=['POST'])
    def send_warning():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET notifications=? WHERE id=?', (d['message'], d['user_id']))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user_servers(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not user:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        servers = []
        for r in rows:
            f = r['folder']
            online = (f in running_procs and running_procs[f].poll() is None) or (r['pid'] and psutil.pid_exists(r['pid']))
            servers.append({'id': r['id'], 'name': r['name'], 'folder': f, 'online': online, 'status': r['server_status']})
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        payload = request.get_json(silent=True) or {}
        status = (payload.get('status') or '').strip().lower()
        if status not in {'active', 'suspended'}:
            return jsonify({'status': 'error', 'msg': 'Invalid server status.'}), 400
        db = get_db()
        srv = db.execute('SELECT folder, pid FROM servers WHERE id=?', (sid,)).fetchone()
        if not srv:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server not found.'}), 404
        if status == 'suspended':
            tracked = running_procs.get(srv['folder'])
            pid = tracked.pid if tracked else srv['pid']
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            running_procs.pop(srv['folder'], None)
            start_times.pop(srv['folder'], None)
            db.execute('UPDATE servers SET server_status=?, pid=NULL WHERE id=?', (status, sid))
        else:
            db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'server_status': status})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        db = get_db()
        srv = db.execute('SELECT folder, pid FROM servers WHERE id=?', (sid,)).fetchone()
        if not srv:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server not found'}), 404
        folder = srv['folder']
        tracked = running_procs.get(folder)
        pid = tracked.pid if tracked else srv['pid']
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        running_procs.pop(folder, None)
        start_times.pop(folder, None)
        db.execute('DELETE FROM servers WHERE id=?', (sid,))
        db.commit()
        path = safe_server_root(folder)
        if path and os.path.exists(path):
            shutil.rmtree(path)
        db.close()
        return jsonify({'status': 'deleted'})

    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        d = request.get_json(silent=True) or {}
        name = (d.get('name') or '').strip()[:80]
        email = (d.get('email') or '').strip().lower()[:160]
        password = (d.get('pass') or '').strip()
        try:
            limit = max(1, min(50, int(d.get('limit', 1) or 1)))
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'msg': 'Server limit must be a number.'}), 400
        if not name or not email or not password:
            return jsonify({'status': 'error', 'msg': 'Name, email and password are required.'}), 400
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email) or len(password) < 6:
            return jsonify({'status': 'error', 'msg': 'Enter a valid email and a password of at least 6 characters.'}), 400
        db = get_db()
        exists = db.execute('SELECT id FROM users WHERE lower(email)=? OR lower(username)=?', (email, email)).fetchone()
        if exists:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Email already exists.'}), 409
        base_username = re.sub(r'[^a-z0-9_]+', '', email.split('@', 1)[0].lower())[:32] or 'user'
        username = base_username
        suffix = 2
        while db.execute('SELECT id FROM users WHERE lower(username)=?', (username,)).fetchone():
            username = f'{base_username}{suffix}'[:40]
            suffix += 1
        db.execute('INSERT INTO users (fname, username, email, password, server_limit, role, status) VALUES (?,?,?,?,?,?,?)', (name, username, email, password, limit, 'free', 'active'))
        user_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.commit()
        db.close()
        return jsonify({'status': 'success', 'user_id': user_id})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        if not session.get('admin_logged'):
            return jsonify({'status': 'error', 'msg': 'Admin authentication required.'}), 401
        db = get_db()
        user = db.execute('SELECT id, role FROM users WHERE id=?', (uid,)).fetchone()
        if not user:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Account not found.'}), 404
        if (user['role'] or '').lower() == 'admin':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Administrator accounts cannot be deleted.'}), 403
        srvs = db.execute('SELECT folder, pid FROM servers WHERE user_id=?', (uid,)).fetchall()
        for server in srvs:
            folder = server['folder']
            tracked = running_procs.get(folder)
            pid = tracked.pid if tracked else server['pid']
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            running_procs.pop(folder, None)
            start_times.pop(folder, None)
            stop_docker_container(folder)
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path):
                shutil.rmtree(path)
        db.execute('DELETE FROM tickets WHERE user_id=?', (uid,))
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit()
        db.close()
        return jsonify({'status': 'deleted', 'user_id': uid})

    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    @app.route('/files/list/<folder>')
    def flist(folder):
        sub_path = request.args.get('path', '')
        full_path = safe_server_path(folder, sub_path)
        if full_path is None: return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        if not os.path.exists(full_path): return jsonify([])
        items = []
        for f in sorted(os.listdir(full_path)):
            if f == 'console.log': continue
            p = os.path.join(full_path, f)
            items.append({'name': f, 'is_dir': os.path.isdir(p), 'is_zip': f.lower().endswith('.zip'), 'rel_path': os.path.join(sub_path, f)})
        return jsonify(items)

    @app.route('/files/content/<folder>/<name>')
    def fcontent(folder, name):
        sub_path = request.args.get('path', '')
        p = safe_server_path(folder, sub_path, name)
        if p is None: return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read()})
        except: return jsonify({'content': 'Error reading file'})

    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    def fsave(folder, name):
        sub_path = request.args.get('path', '')
        if is_reserved_server_file(name): return jsonify({'status': 'error', 'msg': 'Reserved EVO HUB file cannot be modified.'}), 403
        p = safe_server_path(folder, sub_path, name)
        if p is None: return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        try:
            with open(p, 'w', encoding='utf-8') as f: f.write(request.json.get('content'))
            return jsonify({'status': 'saved'})
        except: return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        d = request.json
        sub_path, names = d.get('path', ''), d.get('names', [])
        base = safe_server_path(folder, sub_path)
        if base is None or not os.path.isdir(base): return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        if any(is_reserved_server_file(name) for name in names):
            return jsonify({'status': 'error', 'msg': 'Reserved EVO HUB files cannot be deleted.'}), 403
        for name in names:
            p = safe_server_path(folder, sub_path, name)
            if p is None: continue
            if name == 'console.log': continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except: pass
        return jsonify({"status": "ok"})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        d = request.json
        safe_name = secure_filename(d.get('name'))
        if is_reserved_server_file(safe_name): return jsonify({'status': 'error', 'msg': 'Reserved EVO HUB file cannot be created.'}), 403
        p = safe_server_path(folder, d.get('path', ''), safe_name)
        if p is None: return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        with open(p, 'w') as f: f.write("")
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        d = request.json
        safe_name = secure_filename(d.get('name'))
        if is_reserved_server_file(safe_name): return jsonify({'status': 'error', 'msg': 'Reserved EVO HUB file cannot be created.'}), 403
        p = safe_server_path(folder, d.get('path', ''), safe_name)
        if p is None: return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub_path = request.form.get('path', '')
        uploaded = request.files.get('file')
        if uploaded is None or not uploaded.filename:
            return jsonify({'status': 'error', 'msg': 'No file was selected.'}), 400
        safe_name = secure_filename(uploaded.filename)
        if not safe_name:
            return jsonify({'status': 'error', 'msg': 'The file name is not valid.'}), 400
        if is_reserved_server_file(safe_name):
            return jsonify({'status': 'error', 'msg': 'Reserved EVO HUB file cannot be uploaded.'}), 403
        dest = safe_server_path(folder, sub_path)
        target = safe_server_path(folder, sub_path, safe_name)
        if dest is None or target is None:
            return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        os.makedirs(dest, exist_ok=True)
        try:
            uploaded.save(target)
        except OSError:
            return jsonify({'status': 'error', 'msg': 'The file could not be saved. Check workspace permissions.'}), 500
        return jsonify({'status': 'success', 'name': safe_name})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        d = request.json or {}
        base = safe_server_path(folder, d.get('path', ''))
        old_name, new_name = secure_filename(d.get('old', '')), secure_filename(d.get('new', ''))
        if is_reserved_server_file(old_name) or is_reserved_server_file(new_name): return jsonify({'status': 'error', 'msg': 'Reserved EVO HUB file cannot be renamed.'}), 403
        root = safe_server_root(folder)
        if base is None or not old_name or not new_name or os.path.realpath(base) != root and not os.path.realpath(base).startswith(root + os.sep):
            return jsonify({'status': 'error', 'msg': 'Invalid file name or path.'}), 400
        old_path = safe_server_path(folder, d.get('path', ''), old_name)
        new_path = safe_server_path(folder, d.get('path', ''), new_name)
        if old_path is None or new_path is None: return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        if not os.path.exists(old_path): return jsonify({'status': 'error', 'msg': 'File not found.'}), 404
        if os.path.exists(new_path): return jsonify({'status': 'error', 'msg': 'A file with this name already exists.'}), 409
        os.rename(old_path, new_path)
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    def download_file(folder, name):
        sub_path = request.args.get('path', '')
        p = safe_server_path(folder, sub_path, name)
        if p is None or not os.path.isfile(p): return "Access Denied", 403
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    def zip_bulk(folder):
        d = request.json
        names, sub_path = d.get('names', []), d.get('path', '')
        base = safe_server_path(folder, sub_path)
        if base is None or not os.path.isdir(base): return jsonify({'status': 'error', 'msg': 'Unsafe server path rejected.'}), 400
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = safe_server_path(folder, sub_path, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as z:
            for n in names:
                p = safe_server_path(folder, sub_path, n)
                if p is None or n == zip_name: continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        d = request.get_json(silent=True) or {}
        zip_name = secure_filename(str(d.get('name') or ''))
        sub_path = str(d.get('path') or '')
        base = safe_server_path(folder, sub_path)
        zip_path = safe_server_path(folder, sub_path, zip_name)
        if not zip_name or base is None or zip_path is None:
            return jsonify({'status': 'error', 'msg': 'Unsafe archive path rejected.'}), 400
        if not os.path.isfile(zip_path) or not zipfile.is_zipfile(zip_path):
            return jsonify({'status': 'error', 'msg': 'Invalid ZIP file.'}), 400
        staging = None
        try:
            base_real = os.path.realpath(base)
            with zipfile.ZipFile(zip_path, 'r') as archive:
                members = archive.infolist()
                if len(members) > 50000:
                    return jsonify({'status': 'error', 'msg': 'The archive contains too many files (maximum 50,000).'}), 413
                total_uncompressed = sum(max(0, member.file_size) for member in members)
                if total_uncompressed > 200 * 1024 * 1024:
                    return jsonify({'status': 'error', 'msg': 'The uncompressed archive is too large (maximum 200 MB).'}), 413
                safe_members = []
                skipped_members = 0
                for member in members:
                    member_name = member.filename.replace('\\', '/')
                    while member_name.startswith('./'):
                        member_name = member_name[2:]
                    target = safe_server_path(folder, sub_path, member_name)
                    mode = (member.external_attr >> 16) & 0o170000
                    unsafe = (not member_name or '\x00' in member_name or target is None or
                              (target != base_real and not target.startswith(base_real + os.sep)) or
                              member_name.startswith('/') or '..' in member_name.split('/') or
                              mode == 0o120000 or
                              is_reserved_server_file(os.path.basename(member_name.rstrip('/'))))
                    if unsafe:
                        # Ignore only the unsafe entry so a normal archive can still
                        # be extracted even if it contains a symlink or platform file.
                        skipped_members += 1
                        continue
                    safe_members.append((member, member_name))
                staging = tempfile.mkdtemp(prefix='.zip-staging-', dir=base)
                for member, relative_name in safe_members:
                    if member.is_dir():
                        os.makedirs(os.path.join(staging, relative_name), exist_ok=True)
                        continue
                    output_path = os.path.join(staging, relative_name)
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    with archive.open(member, 'r') as source, open(output_path, 'wb') as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
            shutil.copytree(staging, base, dirs_exist_ok=True)
            message = 'ZIP archive extracted successfully.'
            if skipped_members:
                message += f' {skipped_members} unsafe or reserved item(s) skipped.'
            return jsonify({'status': 'success', 'msg': message, 'skipped': skipped_members})
        except (OSError, zipfile.BadZipFile, RuntimeError):
            return jsonify({'status': 'error', 'msg': 'The ZIP archive could not be extracted.'}), 400
        finally:
            if staging and os.path.exists(staging):
                shutil.rmtree(staging, ignore_errors=True)

    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        srv_data = db.execute('SELECT server_status FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'This server is suspended by Admin.'})

        path = safe_server_root(folder)
        if path is None: return jsonify({'status': 'error', 'msg': 'Invalid server workspace ID.'}), 400
        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if act in ['install', 'start', 'restart'] and not docker_sandbox_enabled() and not host_execution_explicitly_allowed():
            f_log = open(log_file_path, 'a', buffering=1)
            f_log.write(f"\n[{now}] ℹ️ Docker Engine is unavailable and host execution is disabled by production policy.\n")
            f_log.close(); db.close()
            return jsonify({'status': 'error', 'msg': 'Docker Engine is required in production. Set EVO_HUB_ALLOW_UNSAFE_HOST_EXECUTION=1 only for a trusted local/demo installation.'}), 503

        if act == 'install' and docker_sandbox_enabled():
            with open(log_file_path, 'a', buffering=1) as f_log:
                f_log.write(f"[{now}] ℹ️ Dependencies are installed inside the isolated Docker container at startup.\\n")
            db.close()
            return jsonify({'status': 'ready', 'sandbox': 'docker'})

        if act == 'install':
            f_log = open(log_file_path, 'a', buffering=1)
            f_log.write(f"\n[{now}] 🧩 Scanning Python manifests and imports for all dependencies...\n")
            try:
                ok, selected_python, package_dir = bootstrap_python_dependencies(path, f_log, timeout=900)
            except Exception as exc:
                ok, selected_python, package_dir = False, os.path.join(path, '.venv', 'bin', 'python'), None
                f_log.write(f"[EVO-DEPS] Unexpected dependency bootstrap error: {exc}\n")
            f_log.close()
            db.close()
            if ok:
                return jsonify({'status': 'ready', 'msg': 'All discovered Python dependencies are installed.', 'python': selected_python})
            return jsonify({'status': 'warning', 'msg': 'Dependency installation finished with warnings. Check the Console for packages that could not be installed.', 'python': selected_python}), 200

        if act in ['start', 'restart']:
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            old_pid = row['pid'] if row else None
            if folder in running_procs or (old_pid and psutil.pid_exists(old_pid)):
                try:
                    t_pid = running_procs[folder].pid if folder in running_procs else old_pid
                    os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except (OSError, ProcessLookupError, ValueError):
                    pass
            running_procs.pop(folder, None)
            start_times.pop(folder, None)
            stop_docker_container(folder)
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            srv = db.execute('SELECT startup, server_type, public_port, public_id, plan FROM servers WHERE folder=?', (folder,)).fetchone()
            startup_file = srv['startup'] if srv and srv['startup'] else 'main.py'
            is_node = bool(srv and (srv['server_type'] or '').lower() in ('node', 'nodejs'))
            if not is_node and not os.path.isfile(safe_server_path(folder, startup_file) or ''):
                for candidate in ('main.py', 'app.py', 'run.py', 'bot.py', 'server.py', 'index.py'):
                    if os.path.isfile(safe_server_path(folder, candidate) or ''):
                        startup_file = candidate
                        break
            runtime = 'node' if is_node else 'python3'
            service_env = os.environ.copy()
            service_env['PORT'] = str(srv['public_port'] if srv and srv['public_port'] else 5000)
            service_env['EVO_HUB_SERVER_ID'] = str(srv['public_id'] if srv and srv['public_id'] else folder)
            service_env['EVO_HUB_SERVER_ROOT'] = '/workspace' if docker_sandbox_enabled() else path
            # Uploaded bots and API clients need outbound HTTPS (for example Telegram polling).
            # The workspace remains isolated; only egress is enabled, never host networking.
            service_env.setdefault('EVO_HUB_ALLOW_EGRESS', '1')
            if docker_sandbox_enabled():
                runner = os.path.join(PROJECT_ROOT, 'docker', 'run_server.sh')
                docker_runtime = 'nodejs' if is_node else 'python'
                try:
                    docker_start = subprocess.run(
                        [runner, str(srv['public_id'] if srv and srv['public_id'] else folder), path,
                         str(srv['public_port'] if srv and srv['public_port'] else 5000), docker_runtime],
                        cwd=PROJECT_ROOT, env=service_env, capture_output=True, text=True, timeout=600
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    f_log = open(log_file_path, 'a', buffering=1)
                    f_log.write(f"[{now}] ❌ Docker sandbox could not start: {exc}\\n"); f_log.close(); db.close()
                    return jsonify({'status': 'error', 'msg': 'Docker sandbox could not be started.'}), 503
                f_log = open(log_file_path, 'a', buffering=1)
                if docker_start.stdout: f_log.write(docker_start.stdout[-8000:])
                if docker_start.stderr: f_log.write(docker_start.stderr[-8000:])
                if docker_start.returncode != 0:
                    f_log.write(f"[{now}] ❌ Docker sandbox exited with code {docker_start.returncode}.\\n")
                    f_log.close(); db.close()
                    return jsonify({'status': 'error', 'msg': 'Docker sandbox failed to start. Check the Console.'}), 503
                start_times[folder] = time.time()
                db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,)); db.commit()
                f_log.write(f"[{now}] ✅ Isolated Docker container started for public ID {folder}.\\n")
                f_log.close(); db.close()
                return jsonify({'status': 'started', 'sandbox': 'docker'})
            if not is_node:
                f_log = open(log_file_path, 'a', buffering=1)
                f_log.write(f"\n[{now}] 🧩 Scanning manifests and Python imports before startup...\n")
                try:
                    dependencies_ok, runtime, package_dir = bootstrap_python_dependencies(path, f_log, timeout=900)
                except Exception as exc:
                    dependencies_ok, runtime, package_dir = False, sys.executable, None
                    f_log.write(f"[EVO-DEPS] Unexpected dependency bootstrap error: {exc}\n")
                if package_dir:
                    service_env['PYTHONPATH'] = package_dir + os.pathsep + service_env.get('PYTHONPATH', '')
                if not dependencies_ok:
                    f_log.write(f"[{now}] ⚠️ Some dependencies could not be installed; attempting to start anyway.\n")
                f_log.write(f"[{now}] ✅ Python dependency preparation completed; runtime: {runtime}\n")
                f_log.close()
            f_log = open(log_file_path, 'a', buffering=1)
            f_log.write(f"\n[{now}] 🚀 Instance {act.upper()}ED Successfully ({runtime})\n")
            if runtime == 'node' and os.path.isfile(os.path.join(path, 'package.json')):
                f_log.write(f"[{now}] 📦 Installing Node.js dependencies from package.json...\n")
                try:
                    npm_env = os.environ.copy()
                    npm_env['npm_config_registry'] = 'https://registry.npmjs.org/'
                    npm_env['NPM_CONFIG_AUDIT'] = 'false'
                    npm_env['NPM_CONFIG_FUND'] = 'false'
                    npm_args = ['npm', 'install', '--registry=https://registry.npmjs.org/', '--package-lock=false', '--ignore-scripts', '--no-audit', '--no-fund', '--omit=dev']
                    install = subprocess.run(npm_args, cwd=path, env=npm_env, stdout=f_log, stderr=subprocess.STDOUT, timeout=180)
                    if install.returncode != 0:
                        f_log.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ First npm install attempt failed; retrying without lockfile metadata...\n")
                        install = subprocess.run(npm_args + ['--prefer-online'], cwd=path, env=npm_env, stdout=f_log, stderr=subprocess.STDOUT, timeout=180)
                except subprocess.TimeoutExpired:
                    f_log.write(f"[{now}] ❌ npm install timed out.\n")
                    f_log.close(); db.close()
                    return jsonify({'status': 'error', 'msg': 'Node.js dependency installation timed out.'}), 504
                if install.returncode != 0:
                    f_log.write(f"[{now}] ❌ npm install failed with code {install.returncode}.\n")
                    f_log.write(f"[{now}] ℹ️ If the log contains npm E403, one of the requested package versions is blocked by the configured registry policy; update package.json to an allowed version.\n")
                    f_log.close(); db.close()
                    return jsonify({'status': 'error', 'msg': 'Node.js dependency installation was rejected by the npm registry/security policy. Check the Console for the package name and update package.json.'}), 400
                f_log.write(f"[{now}] ✅ Node.js dependencies installed successfully.\n")
            def start_child_process():
                os.setsid()
                apply_process_limits()
            proc = subprocess.Popen([runtime, startup_file], cwd=path, stdout=f_log, stderr=f_log, preexec_fn=start_child_process, env=service_env)
            running_procs[folder], start_times[folder] = proc, time.time()
            time.sleep(0.35)
            early_exit = proc.poll()
            if early_exit is not None:
                running_procs.pop(folder, None)
                start_times.pop(folder, None)
                db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
                db.commit()
                f_log.write(f'[{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] ❌ Instance exited immediately with code {early_exit}. Check the Console for the application traceback.\n')
                f_log.close()
                db.close()
                return jsonify({'status': 'error', 'msg': 'The application stopped immediately. Check the Console for the exact Python error.', 'exit_code': early_exit}), 400
            db.execute('UPDATE servers SET pid=? WHERE folder=?', (proc.pid, folder))
            db.commit()
            f_log.flush()
            db.close()
            return jsonify({'status': 'started'})
        elif act == 'stop':
            stop_docker_container(folder)
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            t_pid = running_procs[folder].pid if folder in running_procs else (row['pid'] if row else None)
            if t_pid:
                try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            if folder in running_procs: del running_procs[folder]
            db.execute('UPDATE servers SET pid=NULL WHERE folder=?', (folder,))
            db.commit()
            db.close()
            with open(log_file_path, 'a') as f: f.write(f"\n[{now}] 🛑 Instance STOPPED\n")
            return jsonify({'status': 'stopped'})
        db.close()
        return jsonify({'status': 'ok'})

    @app.route('/server/log/<folder>')
    def server_log(folder):
        path = safe_server_path(folder, 'console.log')
        if path is None: return jsonify({'log': '', 'online': False, 'msg': 'Invalid server workspace ID.'}), 400
        db = get_db()
        row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
        db.close()
        online = is_server_online(folder, row['pid'] if row else None)
        if os.path.exists(path):
            with open(path, 'r') as f: return jsonify({'log': f.read()[-5000:], 'online': online})
        return jsonify({'log': 'Waiting for logs...', 'online': online})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    def set_startup(folder):
        cmd = request.json.get('file')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit()
        db.close()
        return jsonify({'status': 'success'})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        db = get_db()
        srv_data = db.execute('SELECT server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended servers cannot be deleted!'})
        t_pid = running_procs[folder].pid if folder in running_procs else (srv_data['pid'] if srv_data else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit()
        db.close()
        path = safe_server_root(folder)
        if path and os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    def is_server_online(folder, saved_pid=None):
        """Use the live child process or live Docker container, not a stale DB status."""
        proc = running_procs.get(folder)
        if proc is not None:
            try:
                if proc.poll() is None:
                    return True
            except Exception:
                pass
        if saved_pid:
            try:
                process = psutil.Process(int(saved_pid))
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
                pass
        return docker_container_running(folder) or get_docker_stats(folder) is not None

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session: return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = is_server_online(f, saved_pid)
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram, metrics_source = "0%", "0MB", "process"
            docker_metrics = get_docker_stats(f)
            if docker_metrics:
                online = True
                cpu, ram, metrics_source = docker_metrics['cpu'], docker_metrics['ram'], docker_metrics['source']
            elif online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu, ram = f"{process.cpu_percent(interval=None)}%", f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass
            plan_name = (r['plan'] or 'free').lower()
            limits = server_resource_limits(plan_name)
            cpu_used = parse_memory_mb(str(cpu).replace('%', ''))
            ram_used_mb = round(parse_memory_mb(ram), 1)
            disk_used_mb = folder_disk_mb(safe_server_root(f) or '')
            usage_percent = min(100.0, round(max((cpu_used / limits['cpu_percent']) * 100, (ram_used_mb / limits['ram_mb']) * 100, (disk_used_mb / limits['disk_mb']) * 100), 1))
            limit_reached = usage_percent >= 95
            if online and limit_reached:
                try:
                    if docker_metrics:
                        subprocess.run(['docker', 'stop', '--time', '3', f'evo-server-{f}'], capture_output=True, timeout=8)
                    else:
                        active_pid = running_procs[f].pid if f in running_procs else saved_pid
                        if active_pid: os.killpg(os.getpgid(active_pid), signal.SIGKILL)
                except Exception: pass
                running_procs.pop(f, None)
                start_times.pop(f, None)
                db_limit = get_db(); db_limit.execute('UPDATE servers SET pid=NULL WHERE folder=?', (f,)); db_limit.commit(); db_limit.close()
                online = False
                uptime = 'Stopped: resource limit'
            public_ip = r['public_ip'] or PUBLIC_IP
            public_port = r['public_port'] or (PUBLIC_PORT_MIN + (r['id'] % (PUBLIC_PORT_MAX - PUBLIC_PORT_MIN + 1)))
            custom_domain = (r['custom_domain'] or '').strip() if 'custom_domain' in r.keys() else ''
            demo_ip = (r['demo_ip'] or '').strip() if 'demo_ip' in r.keys() else ''
            if not demo_ip: demo_ip = f"10.20.{(r['id'] // 250) % 250}.{(r['id'] % 250) + 1}"
            port_url = url_for('public_port_page', port_token=f':{public_port}', _external=True)
            srvs.append({'name': r['name'], 'folder': f, 'online': online, 'startup': r['startup'], 'uptime': uptime, 'cpu': cpu, 'ram': ram, 'status': r['server_status'], 'ip': public_ip, 'port': public_port, 'url': port_url, 'domain': custom_domain, 'description': r['description'] or '', 'plan': plan_name, 'type': r['server_type'] or 'python', 'metrics_source': metrics_source, 'ram_used_mb': ram_used_mb, 'disk_used_mb': disk_used_mb, 'usage_percent': usage_percent, 'limit_reached': limit_reached, 'limits': limits, 'public_id': r['public_id'], 'server_url': port_url, 'status_url': port_url, 'legacy_status_url': url_for('public_server_status', folder=f, _external=True), 'demo_ip': demo_ip})
        return jsonify({'servers': srvs})

    @app.route('/add', methods=['POST'])
    def add_srv():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (session['user_id'],)).fetchone()['count']
        server_limit = max(1, int(user['server_limit'] or 1)) if user else 1
        if count >= server_limit:
            db.close()
            return jsonify({'status': 'error', 'msg': f'Your account allows up to {server_limit} server(s).'}), 409
        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or '').strip()
        if not name:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server name is required.'}), 400
        used_ports = {row[0] for row in db.execute('SELECT public_port FROM servers WHERE public_port IS NOT NULL').fetchall()}
        available_ports = [p for p in range(PUBLIC_PORT_MIN, PUBLIC_PORT_MAX + 1) if p not in used_ports]
        if not available_ports:
            db.close()
            return jsonify({'status': 'error', 'msg': 'No public ports are available right now.'}), 503
        public_port = random.choice(available_ports)
        description = (payload.get('description') or '').strip()[:240]
        requested_plan = (payload.get('plan') or 'free').strip().lower()
        account_role = (user['role'] or 'free').strip().lower() if user else 'free'
        plan = 'premium' if requested_plan == 'premium' and account_role in {'premium', 'admin'} else 'free'
        server_type = (payload.get('type') or 'python').strip().lower()
        if server_type not in {'python', 'nodejs'}: server_type = 'python'
        custom_domain = (payload.get('domain') or '').strip().lower()[:120]
        if custom_domain.startswith('http://') or custom_domain.startswith('https://'): custom_domain = custom_domain.split('://', 1)[1].split('/', 1)[0]
        if custom_domain and not re.match(r'^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$', custom_domain): custom_domain = ''
        startup = 'main.py' if server_type == 'python' else 'index.js'
        # The physical workspace name is the same immutable public server ID.
        public_id = generate_public_server_id(db)
        folder = public_id
        cursor = db.execute('INSERT INTO servers (user_id, name, folder, status, startup, public_ip, public_port, description, plan, server_type, custom_domain, public_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (session['user_id'], name, folder, 'Offline', startup, PUBLIC_IP, public_port, description, plan, server_type, custom_domain, public_id))
        server_id = cursor.lastrowid
        demo_ip = f"10.20.{(server_id // 250) % 250}.{(server_id % 250) + 1}"
        db.execute('UPDATE servers SET demo_ip=? WHERE id=?', (demo_ip, server_id))
        db.commit()
        db.close()
        instance_path = safe_server_root(folder)
        if instance_path is None: return jsonify({'status': 'error', 'msg': 'Invalid server workspace ID.'}), 500
        os.makedirs(instance_path, exist_ok=True)
        ensure_server_dockerfile(instance_path, public_id, server_type)
        if server_type == 'nodejs':
            with open(os.path.join(instance_path, 'index.js'), 'w', encoding='utf-8') as starter:
                starter.write("console.log('EVO Node.js server is ready');\n")
        port_url = url_for('public_port_page', port_token=f':{public_port}', _external=True)
        return jsonify({'status': 'success', 'ip': PUBLIC_IP, 'port': public_port, 'domain': custom_domain, 'type': server_type, 'public_id': public_id, 'server_url': port_url, 'url': port_url})

    return app

app = create_app()
if __name__ == "__main__":
    port = int(os.environ.get('PORT', '13506'))
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)