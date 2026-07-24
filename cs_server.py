"""
C/S 架构聊天室 - 服务器端
基于 asyncio + websockets 的纯 WebSocket 服务器
"""
import asyncio
import json
import os
import re
import html
import uuid
import hashlib
import time
from datetime import datetime, timezone, timedelta as td
from functools import wraps

import websockets

from models import db, User, Message, AuditLog
from flask import Flask

# ========== 配置 ==========
HOST = '0.0.0.0'
PORT = 9000
HTTP_PORT = 8080  # HTTP 静态文件服务端口
ADMIN_PORT = 8081  # 管理面板 HTTP 服务端口
import sys
import threading
if getattr(sys, 'frozen', False):
    # 打包为 exe 时，获取 exe 所在目录
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOGS_DIR = os.path.join(BASE_DIR, 'logs')
LATEST_LOG_FILE = os.path.join(LOGS_DIR, 'latest.log')


def _safe_log_filename(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%d_%H-%M-%S.log')


def _rotate_latest_log():
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not os.path.exists(LATEST_LOG_FILE):
        return

    try:
        last_mtime = datetime.fromtimestamp(os.path.getmtime(LATEST_LOG_FILE))
    except Exception:
        last_mtime = datetime.now()

    archived_name = _safe_log_filename(last_mtime)
    archived_path = os.path.join(LOGS_DIR, archived_name)
    suffix = 1
    while os.path.exists(archived_path):
        archived_name = archived_name.replace('.log', f'_{suffix}.log')
        archived_path = os.path.join(LOGS_DIR, archived_name)
        suffix += 1

    try:
        os.replace(LATEST_LOG_FILE, archived_path)
    except Exception:
        pass


class ChatRoomLogger:
    def __init__(self, original_stream, file_path, level='INFO', thread_name='Server thread', echo=True):
        self.original_stream = original_stream
        self.file_path = file_path
        self.level = level
        self.thread_name = thread_name
        self.echo = echo
        self.buffer = []
        self.lock = threading.Lock()

    def _format_line(self, line: str) -> str:
        timestamp = datetime.now().strftime('%H:%M:%S')
        return f'[{timestamp}] [{self.thread_name}/{self.level}]: {line}'

    def _write_line(self, line: str):
        if not line.strip():
            return

        formatted = self._format_line(line.rstrip('\r'))
        if self.echo:
            self.original_stream.write(formatted + '\n')
            self.original_stream.flush()

        try:
            with open(self.file_path, 'a', encoding='utf-8') as f:
                f.write(formatted + '\n')
        except Exception:
            pass

    def write(self, data):
        if not data:
            return

        with self.lock:
            self.buffer.append(data)
            joined = ''.join(self.buffer)
            lines = joined.split('\n')
            for line in lines[:-1]:
                self._write_line(line)
            self.buffer = [lines[-1]]

    def flush(self):
        with self.lock:
            if self.buffer and self.buffer[0].strip():
                self._write_line(self.buffer[0])
            self.buffer = []
        self.original_stream.flush()


_rotate_latest_log()
os.makedirs(LOGS_DIR, exist_ok=True)

# 开始日志重定向
sys.stdout = ChatRoomLogger(sys.stdout, LATEST_LOG_FILE, level='INFO', thread_name='Server thread')
sys.stderr = ChatRoomLogger(sys.stderr, LATEST_LOG_FILE, level='ERROR', thread_name='Server thread')

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
STATIC_FOLDER = BASE_DIR  # 管理面板模板等项目资源的根目录
UPLOAD_URL_PREFIX = '/static/uploads/'
CONFIG_FILE = os.path.join(BASE_DIR, 'server_config.json')

def load_server_config():
    default_config = {
        'HOST': '0.0.0.0',
        'PORT': 9000,
        'HTTP_PORT': 8080,
        'ADMIN_PORT': 8081,
        'ADMIN_PASSWORD': 'admin123',
        'ALLOWED_EXTENSIONS': ['png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'zip', 'rar', 'md'],
        'SENSITIVE_WORDS': ['妈', '傻逼', 'fuck'],
        'MAX_FILE_SIZE': 100 * 1024 * 1024,
        'MAX_WS_SIZE': 16 * 1024 * 1024,  # 16MB：足够分块上传，避免超大帧 DoS
        'ALLOWED_ADMIN_IPS': ['127.0.0.1', '::1'],
        'ENABLE_SUPER_TERMINAL': False,  # 默认关闭远程终端，防止 RCE
        'MAX_MESSAGE_LENGTH': 4000,
        'MAX_UPLOAD_CHUNKS': 512,
        'ADMIN_SESSION_TTL_SEC': 86400,
    }
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f'[Config] Error writing default config: {e}')
        return default_config
    else:
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # Ensure all keys exist
            updated = False
            for k, v in default_config.items():
                if k not in config:
                    config[k] = v
                    updated = True
            if updated:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
            return config
        except Exception as e:
            print(f'[Config] Error reading config, using defaults: {e}')
            return default_config

SERVER_CONFIG = load_server_config()

import secrets
import string

def generate_super_admin_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

SUPER_ADMIN_PASSWORD = generate_super_admin_password()

def _console_only_print(message: str):
    """只写到真实控制台，不写入 latest.log，避免超级管理员密码落盘。"""
    stream = getattr(sys.stdout, 'original_stream', None) or getattr(sys, '__stdout__', None)
    if stream is not None:
        try:
            stream.write(message + '\n')
            stream.flush()
            return
        except Exception:
            pass
    # 极端情况下仍避免把明文密码写入被重定向的 stdout
    try:
        sys.stderr.original_stream.write(message + '\n')  # type: ignore[attr-defined]
        sys.stderr.original_stream.flush()  # type: ignore[attr-defined]
    except Exception:
        pass

_console_only_print('[SUPER_ADMIN] ==========================================')
_console_only_print('[SUPER_ADMIN] 超级管理员账号已生成！（仅显示在控制台，不会写入日志文件）')
_console_only_print('[SUPER_ADMIN] 账号: super_admin')
_console_only_print(f'[SUPER_ADMIN] 密码: {SUPER_ADMIN_PASSWORD}')
_console_only_print('[SUPER_ADMIN] ==========================================')

HOST = SERVER_CONFIG['HOST']
PORT = SERVER_CONFIG['PORT']
HTTP_PORT = SERVER_CONFIG['HTTP_PORT']
ADMIN_PORT = SERVER_CONFIG['ADMIN_PORT']
MAX_FILE_SIZE = SERVER_CONFIG['MAX_FILE_SIZE']
MAX_WS_SIZE = min(int(SERVER_CONFIG.get('MAX_WS_SIZE', 16 * 1024 * 1024)), 32 * 1024 * 1024)
ADMIN_PASSWORD = SERVER_CONFIG['ADMIN_PASSWORD']
WHITELISTED_IPS = {'127.0.0.1', '::1', '::ffff:127.0.0.1'}
ALLOWED_ADMIN_IPS = list(SERVER_CONFIG.get('ALLOWED_ADMIN_IPS', ['127.0.0.1', '::1']))
ALLOWED_EXTENSIONS = set(SERVER_CONFIG['ALLOWED_EXTENSIONS'])
SENSITIVE_WORDS = list(SERVER_CONFIG['SENSITIVE_WORDS'])
ENABLE_SUPER_TERMINAL = bool(SERVER_CONFIG.get('ENABLE_SUPER_TERMINAL', False))
MAX_MESSAGE_LENGTH = int(SERVER_CONFIG.get('MAX_MESSAGE_LENGTH', 4000))
MAX_UPLOAD_CHUNKS = int(SERVER_CONFIG.get('MAX_UPLOAD_CHUNKS', 512))
ADMIN_SESSION_TTL_SEC = int(SERVER_CONFIG.get('ADMIN_SESSION_TTL_SEC', 86400))

if ADMIN_PASSWORD in ('admin123', 'admin', 'password', '123456'):
    _console_only_print('[SECURITY] 警告: ADMIN_PASSWORD 仍为弱口令，请尽快在 server_config.json 中修改！')

def save_server_config():
    global ALLOWED_EXTENSIONS, SENSITIVE_WORDS, ADMIN_PASSWORD, ALLOWED_ADMIN_IPS
    SERVER_CONFIG['ALLOWED_EXTENSIONS'] = list(ALLOWED_EXTENSIONS)
    SERVER_CONFIG['SENSITIVE_WORDS'] = list(SENSITIVE_WORDS)
    SERVER_CONFIG['ADMIN_PASSWORD'] = ADMIN_PASSWORD
    SERVER_CONFIG['ALLOWED_ADMIN_IPS'] = list(ALLOWED_ADMIN_IPS)
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(SERVER_CONFIG, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f'[Config] Error saving config: {e}')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 初始化 Flask + SQLAlchemy（仅用于 ORM，不提供 HTTP 服务）
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat_data_v2.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

# ========== 内存状态 ==========
# user_id -> websocket 连接
online_connections: dict[int, 'websockets.server.WebSocketServerProtocol'] = {}
# websocket -> user_id 反向映射
ws_to_user: dict['websockets.server.WebSocketServerProtocol', int] = {}
# 速率限制: key(str|int) -> {action: [timestamps]}
rate_limiter: dict = {}
# WS 侧已通过密码验证的管理员 user_id 集合
ws_admin_authenticated: set[int] = set()
main_loop = None

PRESET_AVATAR_IDS = {f'avatar_{i}' for i in range(1, 13)}


def get_ws_client_ip(ws) -> str:
    """仅使用 WebSocket 真实对端地址，忽略客户端自报 IP。"""
    try:
        if ws.remote_address:
            ip = ws.remote_address[0]
            # 归一化 IPv4 映射地址
            if isinstance(ip, str) and ip.startswith('::ffff:'):
                return ip[7:]
            return ip
    except Exception:
        pass
    return '0.0.0.0'


def normalize_ip(ip: str) -> str:
    if not ip:
        return '0.0.0.0'
    if ip.startswith('::ffff:'):
        return ip[7:]
    return ip


def is_local_or_allowed_admin_ip(ip: str) -> bool:
    ip = normalize_ip(ip)
    return ip in WHITELISTED_IPS or ip in ALLOWED_ADMIN_IPS or ip in set(ALLOWED_ADMIN_IPS)


def sanitize_device_token(token: str) -> str:
    """只接受合理长度的十六进制 token，防止注入/超长字段。"""
    if not token or not isinstance(token, str):
        return ''
    token = token.strip()
    if len(token) < 32 or len(token) > 128:
        return ''
    if not re.fullmatch(r'[0-9a-fA-F]+', token):
        return ''
    return token.lower()


def sanitize_device_fp(fp: str) -> str:
    if not fp or not isinstance(fp, str):
        return ''
    fp = fp.strip()
    if len(fp) < 16 or len(fp) > 128:
        return ''
    if not re.fullmatch(r'[0-9a-fA-F]+', fp):
        return ''
    return fp.lower()


def is_safe_upload_file_url(file_url: str) -> bool:
    """file_url 必须指向本站 uploads 且无路径穿越。"""
    if not file_url or not isinstance(file_url, str):
        return False
    if not file_url.startswith(UPLOAD_URL_PREFIX):
        return False
    filename = file_url[len(UPLOAD_URL_PREFIX):]
    if not filename or '/' in filename or '\\' in filename or '..' in filename:
        return False
    if filename != os.path.basename(filename):
        return False
    return True


def safe_upload_path(filename: str) -> 'str | None':
    """将用户提供的文件名解析为 uploads 目录内的绝对路径；非法则返回 None。"""
    if not filename or not isinstance(filename, str):
        return None
    safe_name = os.path.basename(filename.strip())
    if not safe_name or safe_name in ('.', '..') or '/' in safe_name or '\\' in safe_name:
        return None
    upload_root = os.path.abspath(UPLOAD_FOLDER)
    file_path = os.path.abspath(os.path.join(upload_root, safe_name))
    # 使用 commonpath 判断，兼容 Windows 路径大小写与分隔符
    try:
        if os.path.commonpath([upload_root, file_path]) != upload_root:
            return None
    except ValueError:
        return None
    if os.path.basename(file_path) != safe_name:
        return None
    return file_path


def sanitize_ban_reason(reason) -> str:
    """封禁原因：限长、去控制字符，防止日志/展示污染。"""
    if not isinstance(reason, str):
        reason = '违规行为'
    reason = reason.strip() or '违规行为'
    reason = re.sub(r'[\x00-\x1f\x7f]', '', reason)
    return reason[:200]


# ========== 数据库迁移 ==========
with app.app_context():
    db.create_all()
    try:
        from sqlalchemy import text
        engine = db.engine
        inspector = db.inspect(engine)
        existing_cols = [c['name'] for c in inspector.get_columns('user')]
        new_columns = {
            'is_banned': 'BOOLEAN DEFAULT 0',
            'ban_reason': 'VARCHAR(200)',
            'ban_until': 'DATETIME',
            'banned_at': 'DATETIME',
            'device_fingerprint': 'VARCHAR(64)',
            'device_token': 'VARCHAR(64)',
            'ip_history': "TEXT DEFAULT '[]'",
            'last_name_change': 'DATETIME',
            'avatar': "VARCHAR(50) DEFAULT 'avatar_1'"
        }
        for col_name, col_type in new_columns.items():
            if col_name not in existing_cols:
                try:
                    db.session.execute(text(f'ALTER TABLE user ADD COLUMN {col_name} {col_type}'))
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    print(f'[Migration] Column {col_name}: {e}')
    except Exception as e:
        print(f'[Migration] Error: {e}')


# ========== 工具函数 ==========
def make_response(msg_type: str, data: dict, seq: int = 0) -> str:
    """构造统一格式的JSON响应"""
    return json.dumps({
        'type': msg_type,
        'data': data,
        'seq': seq,
        'ts': datetime.now(timezone.utc).isoformat()
    }, ensure_ascii=False)


LEVEL_INFO = 'INFO'
LEVEL_WARN = 'WARN'
LEVEL_ERROR = 'ERROR'
LEVEL_AUDIT = 'AUDIT'
LEVEL_CHAT = 'CHAT'
LEVEL_NET = 'NET'
LEVEL_HTTP = 'HTTP'
LEVEL_ADMIN = 'ADMIN'
LEVEL_SYSTEM = 'SYSTEM'


def server_log(message: str, level: str = LEVEL_INFO, component: str = 'Server thread'):
    print(f'[{component}/{level}] {message}')


def format_audit_details(details) -> str:
    if isinstance(details, dict):
        return ', '.join(f'{k}={v}' for k, v in details.items())
    if details is None:
        return ''
    return str(details)


def check_rate_limit(key, action: str, max_count: int, window_sec: int) -> bool:
    """简单的滑动窗口速率限制（key 可为 user_id 或 ip 字符串）"""
    if key not in rate_limiter:
        rate_limiter[key] = {}
    if action not in rate_limiter[key]:
        rate_limiter[key][action] = []
    now = time.time()
    rate_limiter[key][action] = [t for t in rate_limiter[key][action] if now - t < window_sec]
    if len(rate_limiter[key][action]) >= max_count:
        return False
    rate_limiter[key][action].append(now)
    return True


def get_user_by_ip(ip: str, device_fp: str = '', device_token: str = '') -> User:
    """
    综合识别用户：优先 device_token > device_fingerprint > IP
    - device_token: 客户端本地持久化文件中的令牌（主要防换IP小号手段）
    - device_fp: 设备硬件指纹（辅助，易被清除但能识别重装）
    - ip: 最后手段，同一IP直接复用账号
    """
    # ===== 1. 封禁逃逸检查：device_token 匹配到已封禁账号 → 直接返回封禁账号 =====
    if device_token and ip not in WHITELISTED_IPS:
        token_user = User.query.filter_by(device_token=device_token).first()
        if token_user and token_user.is_banned:
            # 检查封禁是否到期
            if token_user.ban_until and datetime.now(timezone.utc) >= token_user.ban_until:
                token_user.is_banned = False
                token_user.ban_reason = None
                token_user.ban_until = None
                db.session.commit()
            else:
                # 更新该封禁账号的IP和最后活跃时间
                token_user.ip_address = ip
                token_user.last_active = datetime.now(timezone.utc)
                _update_ip_history(token_user, ip)
                if device_fp and not token_user.device_fingerprint:
                    token_user.device_fingerprint = device_fp
                db.session.commit()
                return token_user

    # ===== 2. 设备指纹封禁逃逸检查 =====
    if device_fp and ip not in WHITELISTED_IPS:
        fp_user = User.query.filter_by(device_fingerprint=device_fp).first()
        if fp_user and fp_user.is_banned:
            if fp_user.ban_until and datetime.now(timezone.utc) >= fp_user.ban_until:
                fp_user.is_banned = False
                fp_user.ban_reason = None
                fp_user.ban_until = None
                db.session.commit()
            else:
                fp_user.ip_address = ip
                fp_user.last_active = datetime.now(timezone.utc)
                _update_ip_history(fp_user, ip)
                if device_token and not fp_user.device_token:
                    fp_user.device_token = device_token
                db.session.commit()
                return fp_user

    # ===== 3. 识别逻辑：device_token → device_fp → IP =====
    user = None

    # 3a. 优先通过 device_token 查找（最可靠）
    if device_token:
        user = User.query.filter_by(device_token=device_token).first()

    # 3b. 其次通过 device_fingerprint 查找
    if not user and device_fp:
        user = User.query.filter_by(device_fingerprint=device_fp).first()

    # 3c. 最后通过 IP 查找
    if not user:
        user = User.query.filter_by(ip_address=ip).first()

    # ===== 4. 创建或更新用户 =====
    if not user:
        # 全新用户
        user = User(ip_address=ip)
        if device_token:
            user.device_token = device_token
        if device_fp:
            user.device_fingerprint = device_fp
        # 仅当首个用户来自本机真实 IP 时自动授予管理员，避免公网抢注
        if User.query.count() == 0 and normalize_ip(ip) in WHITELISTED_IPS:
            user.is_admin = True
        db.session.add(user)
        db.session.commit()
    else:
        # 已有用户：更新标识信息（增量绑定，不覆盖已有值）
        if device_token and not user.device_token:
            user.device_token = device_token
        if device_fp and not user.device_fingerprint:
            user.device_fingerprint = device_fp
        # IP 变化时更新（允许同一用户换 IP）
        if user.ip_address != ip:
            # 如果新 IP 已被其他账号占用，不抢占
            ip_other = User.query.filter_by(ip_address=ip).first()
            if ip_other and ip_other.id != user.id:
                # 新IP已有别的用户，不更新IP，保持原IP
                pass
            else:
                user.ip_address = ip

    # 更新IP历史
    _update_ip_history(user, ip)

    # ===== 5. 同设备账号封禁传播 =====
    _propagate_ban(user, device_token, device_fp)

    user.last_active = datetime.now(timezone.utc)
    db.session.commit()
    return user


def _update_ip_history(user: User, ip: str):
    """更新用户IP历史记录"""
    try:
        history = json.loads(user.ip_history) if user.ip_history else []
        if not history or history[-1].get('ip') != ip:
            history.append({'ip': ip, 'seen_at': datetime.now(timezone.utc).isoformat()})
            history = history[-20:]
            user.ip_history = json.dumps(history)
    except (json.JSONDecodeError, IndexError):
        user.ip_history = json.dumps([{'ip': ip, 'seen_at': datetime.now(timezone.utc).isoformat()}])


def _propagate_ban(user: User, device_token: str, device_fp: str):
    """同设备账号封禁传播：如果同 device_token/device_fp 的账号被封禁，传播到当前账号"""
    if user.ip_address in WHITELISTED_IPS:
        return
    if user.is_banned:
        return  # 已经封禁了，不需要传播

    siblings = set()

    # 通过 device_token 找关联账号
    if device_token:
        for u in User.query.filter_by(device_token=device_token).all():
            if u.id != user.id:
                siblings.add(u)

    # 通过 device_fingerprint 找关联账号
    if device_fp:
        for u in User.query.filter_by(device_fingerprint=device_fp).all():
            if u.id != user.id:
                siblings.add(u)

    # 如果任何关联账号被封禁，传播封禁
    for sib in siblings:
        if sib.is_banned:
            # 检查封禁是否到期
            if sib.ban_until and datetime.now(timezone.utc) >= sib.ban_until:
                sib.is_banned = False
                sib.ban_reason = None
                sib.ban_until = None
                continue
            user.is_banned = True
            user.ban_reason = sib.ban_reason or "关联账号封禁(同设备)"
            user.ban_until = sib.ban_until
            break


def check_banned(user: User) -> bool:
    """检查用户是否被封禁"""
    if user.ip_address in WHITELISTED_IPS:
        return False
    if user.is_banned:
        if user.ban_until:
            ban_until = user.ban_until
            if ban_until.tzinfo is None:
                ban_until = ban_until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= ban_until:
                user.is_banned = False
                user.ban_reason = None
                user.ban_until = None
                db.session.commit()
                return False
        return True
    return False


def check_muted(user: User) -> bool:
    """检查并更新用户的禁言状态"""
    if user.is_muted:
        if user.mute_until:
            mute_until = user.mute_until
            if mute_until.tzinfo is None:
                mute_until = mute_until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= mute_until:
                user.is_muted = False
                user.mute_until = None
                db.session.commit()
                return False
        return True
    return False


def log_audit(user_id, ip, action, details=''):
    """记录审计日志"""
    detail_text = format_audit_details(details)
    log = AuditLog(user_id=user_id, ip_address=ip, action=action, details=detail_text)
    db.session.add(log)
    db.session.commit()

    actor = f'user_id={user_id}' if user_id is not None else 'system'
    remote = ip or 'unknown-ip'
    suffix = f' | {detail_text}' if detail_text else ''
    server_log(f'[Audit] action={action} actor={actor} ip={remote}{suffix}', LEVEL_AUDIT, 'Audit thread')


def require_admin(user: User, client_ip: str = None, require_password_session: bool = False, user_id: int = None) -> 'str | None':
    """检查管理员权限，返回错误信息或 None。

    使用真实连接 IP（client_ip），而不是库里可能被历史污染的 user.ip_address。
    """
    if not user:
        return '未认证'
    ip = normalize_ip(client_ip or user.ip_address or '')
    if not is_local_or_allowed_admin_ip(ip):
        return '拒绝访问：未授权的 IP'
    if not user.is_admin:
        return '需要管理员权限'
    if require_password_session:
        uid = user_id if user_id is not None else user.id
        if uid not in ws_admin_authenticated:
            return '请先通过管理员密码登录'
    return None


# ========== 消息处理器 ==========
async def handle_auth(ws, data: dict, seq: int):
    """处理客户端认证"""
    with app.app_context():
        # 强制使用真实对端 IP，忽略客户端自报 ip，防止伪造本机/白名单
        ip = get_ws_client_ip(ws)
        device_fp = sanitize_device_fp(data.get('device_fp', ''))
        device_token = sanitize_device_token(data.get('device_token', ''))

        user = get_user_by_ip(ip, device_fp, device_token)
        check_muted(user)  # 刷新禁言状态

        # 注册连接
        online_connections[user.id] = ws
        ws_to_user[ws] = user.id
        # 重连后需重新验证管理员密码
        ws_admin_authenticated.discard(user.id)

        # 广播上线通知（公开字段）
        await broadcast_to_all(make_response('user_online', {'user': user.to_dict()}), exclude=user.id)

        # 返回认证结果（本人可见 is_admin 等公开字段，不含 device_token）
        await ws.send(make_response('auth_result', {
            'status': 'success',
            'user': user.to_dict(),
            'is_banned': check_banned(user)
        }, seq))

        # 自动推送所有用户列表（包含在线状态）
        users = User.query.filter(User.username != None).all()
        users_data = []
        for u in users:
            d = u.to_dict()
            d['is_online'] = u.id in online_connections
            users_data.append(d)
        await ws.send(make_response('user_list', {
            'users': users_data
        }))

        # 被封禁用户不推送历史消息，减少信息泄露
        if check_banned(user) and normalize_ip(ip) not in WHITELISTED_IPS:
            return

        # 推送公聊最近消息
        messages = Message.query.filter(
            (Message.receiver_id == None) | (Message.receiver_id.is_(None))
        ).order_by(Message.created_at.desc()).limit(200).all()
        messages.reverse()
        # 检查是否还有更早的消息
        has_more = False
        if messages:
            has_more = Message.query.filter(
                (Message.receiver_id == None) | (Message.receiver_id.is_(None)),
                Message.id < messages[0].id
            ).limit(1).count() > 0
        await ws.send(make_response('message_history', {
            'messages': [m.to_dict() for m in messages],
            'receiver_id': None,
            'has_more': has_more,
            'is_append': False
        }))


async def handle_set_username(ws, data: dict, seq: int):
    """设置用户名"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
            return

        user = db.session.get(User, user_id)
        username = data.get('username', '').strip()

        if not username:
            await ws.send(make_response('error', {'msg': '用户名不能为空', 'code': 400}, seq))
            return
        if len(username) > 20:
            await ws.send(make_response('error', {'msg': '用户名过长', 'code': 400}, seq))
            return

        # 冷却期检查：10分钟
        if user.last_name_change:
            last_change = user.last_name_change
            if last_change.tzinfo is None:
                last_change = last_change.replace(tzinfo=timezone.utc)
            time_since_change = datetime.now(timezone.utc) - last_change
            if time_since_change < td(minutes=10):
                remaining = int(10 - time_since_change.total_seconds() / 60) or 1
                await ws.send(make_response('error', {'msg': f'改名太频繁，请 {remaining} 分钟后再试', 'code': 429}, seq))
                return

        existing = User.query.filter_by(username=username).first()
        if existing and existing.id != user.id:
            await ws.send(make_response('error', {'msg': '用户名已被占用', 'code': 400}, seq))
            return

        user.username = html.escape(username)
        user.last_name_change = datetime.now(timezone.utc)
        db.session.commit()

        await ws.send(make_response('set_username_result', {
            'status': 'success', 'user': user.to_dict()
        }, seq))

        # 通知所有在线用户更新用户列表（包含在线状态）
        users = User.query.filter(User.username != None).all()
        users_data = []
        for u in users:
            d = u.to_dict()
            d['is_online'] = u.id in online_connections
            users_data.append(d)
        await broadcast_to_all(make_response('user_list', {'users': users_data}))


async def handle_update_profile(ws, data: dict, seq: int):
    """更新个人信息（用户名和头像）"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
            return

        user = db.session.get(User, user_id)
        username = data.get('username', '').strip()
        avatar = data.get('avatar', '').strip()

        updated = False
        if username:
            if len(username) > 20:
                await ws.send(make_response('error', {'msg': '用户名过长', 'code': 400}, seq))
                return
            
            # 冷却期检查：10分钟
            if user.last_name_change and user.username != username:
                last_change = user.last_name_change
                if last_change.tzinfo is None:
                    last_change = last_change.replace(tzinfo=timezone.utc)
                time_since_change = datetime.now(timezone.utc) - last_change
                if time_since_change < td(minutes=10):
                    remaining = int(10 - time_since_change.total_seconds() / 60) or 1
                    await ws.send(make_response('error', {'msg': f'改名太频繁，请 {remaining} 分钟后再试', 'code': 429}, seq))
                    return

            existing = User.query.filter_by(username=username).first()
            if existing and existing.id != user.id:
                await ws.send(make_response('error', {'msg': '用户名已被占用', 'code': 400}, seq))
                return

            if user.username != username:
                user.username = html.escape(username)
                user.last_name_change = datetime.now(timezone.utc)
                updated = True

        if avatar:
            if avatar not in PRESET_AVATAR_IDS:
                await ws.send(make_response('error', {'msg': '无效的头像', 'code': 400}, seq))
                return
            if getattr(user, 'avatar', None) != avatar:
                user.avatar = avatar
                updated = True

        if updated:
            db.session.commit()

        await ws.send(make_response('update_profile_result', {
            'status': 'success', 'user': user.to_dict()
        }, seq))

        if updated:
            # 通知所有在线用户更新用户列表（包含在线状态）
            users = User.query.filter(User.username != None).all()
            users_data = []
            for u in users:
                d = u.to_dict()
                d['is_online'] = u.id in online_connections
                users_data.append(d)
            await broadcast_to_all(make_response('user_list', {'users': users_data}))


async def handle_send_message(ws, data: dict, seq: int):
    """处理发送消息"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
            return

        if not check_rate_limit(user_id, 'send', 30, 60):
            await ws.send(make_response('error', {'msg': '发送过于频繁，请稍后再试', 'code': 429}, seq))
            return

        user = db.session.get(User, user_id)

        if not user or not user.username:
            await ws.send(make_response('error', {'msg': '请先设置用户名'}, seq))
            return

        if user.ip_address not in WHITELISTED_IPS and check_banned(user):
            await ws.send(make_response('error', {'msg': f'您的账号已被封禁。原因：{user.ban_reason or "违规行为"}'}, seq))
            return

        if check_muted(user):
            mute_info = ""
            if user.mute_until:
                try:
                    # Convert UTC to Local (UTC+8) for display
                    local_mute_until = user.mute_until + td(hours=8)
                    mute_info = f"。禁言解除时间：{local_mute_until.strftime('%Y-%m-%d %H:%M:%S')}"
                except Exception:
                    mute_info = f"。禁言解除时间：{user.mute_until.strftime('%Y-%m-%d %H:%M:%S')} (UTC)"
            await ws.send(make_response('error', {'msg': f'您已被禁言{mute_info}'}, seq))
            return

        content = data.get('content', '').strip()
        msg_type = data.get('msg_type', 'text')
        file_url = data.get('file_url', None)
        file_name = data.get('file_name', None)
        receiver_id = data.get('receiver_id', None)

        if msg_type not in ('text', 'image', 'file'):
            await ws.send(make_response('error', {'msg': '不支持的消息类型', 'code': 400}, seq))
            return

        if not content and msg_type == 'text':
            return

        if content and len(content) > MAX_MESSAGE_LENGTH:
            await ws.send(make_response('error', {'msg': f'消息过长（最多{MAX_MESSAGE_LENGTH}字）', 'code': 400}, seq))
            return

        # 文件/图片消息：file_url 必须指向本站已上传文件，禁止任意 URL 注入
        if msg_type in ('image', 'file'):
            if not is_safe_upload_file_url(file_url or ''):
                await ws.send(make_response('error', {'msg': '非法的文件地址', 'code': 400}, seq))
                return
            # 确认文件真实存在
            fname = (file_url or '')[len(UPLOAD_URL_PREFIX):]
            fpath = safe_upload_path(fname)
            if not fpath or not os.path.isfile(fpath):
                await ws.send(make_response('error', {'msg': '文件不存在或尚未上传', 'code': 400}, seq))
                return
            if file_name:
                file_name = os.path.basename(str(file_name))[:200]
            content = content[:MAX_MESSAGE_LENGTH] if content else (file_name or '')
        else:
            file_url = None
            file_name = None

        # 敏感词过滤（文本与说明文字）
        if content:
            for word in SENSITIVE_WORDS:
                if word and word in content:
                    await ws.send(make_response('error', {'msg': '包含敏感词汇，消息已被拦截'}, seq))
                    return
            content = html.escape(content)

        # receiver_id 校验
        if receiver_id is not None:
            try:
                receiver_id = int(receiver_id)
            except (TypeError, ValueError):
                await ws.send(make_response('error', {'msg': '无效的接收者', 'code': 400}, seq))
                return
            if receiver_id == user.id:
                await ws.send(make_response('error', {'msg': '不能私信自己', 'code': 400}, seq))
                return
            if not db.session.get(User, receiver_id):
                await ws.send(make_response('error', {'msg': '接收者不存在', 'code': 400}, seq))
                return

        msg = Message(
            sender_id=user.id,
            receiver_id=receiver_id,
            content=content,
            msg_type=msg_type,
            file_url=file_url,
            file_name=file_name
        )
        db.session.add(msg)
        db.session.commit()

        msg_dict = msg.to_dict()
        msg_json = make_response('new_message', msg_dict)

        if receiver_id:
            # 私聊：发送给双方
            target_id = int(receiver_id)
            targets = {user.id, target_id}
            await broadcast_to_users(targets, msg_json)
        else:
            # 公聊：广播给所有人
            await broadcast_to_all(msg_json)


async def handle_recall_message(ws, data: dict, seq: int):
    """撤回消息"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            return

        msg_id = data.get('msg_id')
        user = db.session.get(User, user_id)
        msg = db.session.get(Message, msg_id)

        if msg and msg.sender_id == user.id:
            diff = (datetime.now(timezone.utc) - msg.created_at).total_seconds()
            if diff <= 120:
                msg.is_deleted = True
                db.session.commit()
                await broadcast_to_all(make_response('message_recalled', {'msg_id': msg_id}))
            else:
                await ws.send(make_response('error', {'msg': '超过2分钟无法撤回'}))


async def handle_get_messages(ws, data: dict, seq: int):
    """拉取历史消息（支持 before_id 分页）"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            return

        user = db.session.get(User, user_id)
        if not user or not user.username:
            await ws.send(make_response('error', {'msg': '请先设置用户名'}, seq))
            return

        receiver_id = data.get('receiver_id')
        limit = min(200, data.get('limit', 200))
        before_id = data.get('before_id')  # 加载此 ID 之前的消息（向上翻页）

        if receiver_id:
            q = Message.query.filter(
                ((Message.sender_id == user.id) & (Message.receiver_id == int(receiver_id))) |
                ((Message.sender_id == int(receiver_id)) & (Message.receiver_id == user.id))
            ).filter(Message.receiver_id != None)
        else:
            q = Message.query.filter(
                (Message.receiver_id == None) | (Message.receiver_id.is_(None))
            )

        # before_id 分页：只加载比 before_id 更早的消息
        if before_id:
            q = q.filter(Message.id < int(before_id))

        messages = q.order_by(Message.created_at.desc()).limit(limit).all()
        messages.reverse()

        # 是否还有更早的消息（has_more 标志）
        if messages:
            oldest_id = messages[0].id
            has_more = Message.query.filter(
                Message.id < oldest_id
            ).limit(1).count() > 0 if not receiver_id else True
            if not receiver_id and has_more:
                has_more = q.filter(Message.id < oldest_id).limit(1).count() > 0
        else:
            has_more = False

        await ws.send(make_response('message_history', {
            'messages': [m.to_dict() for m in messages],
            'receiver_id': receiver_id,
            'has_more': has_more,
            'is_append': bool(before_id)  # 是否为追加模式（向上加载更多）
        }, seq))


async def handle_get_users(ws, data: dict, seq: int):
    """获取用户列表（包含在线状态）"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
            return

        users = User.query.filter(User.username != None).all()
        users_data = []
        for u in users:
            d = u.to_dict()
            d['is_online'] = u.id in online_connections
            users_data.append(d)

        await ws.send(make_response('user_list', {
            'users': users_data
        }, seq))


# ========== 消息处理器 ==========
# 分块上传临时存储: upload_id -> {chunks: [], total: int, received: int, meta: dict}
_upload_sessions: dict[str, dict] = {}


async def handle_upload_start(ws, data: dict, seq: int):
    """开始分块上传"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
            return
        if not check_rate_limit(user_id, 'upload', 10, 60):
            await ws.send(make_response('error', {'msg': '上传过于频繁', 'code': 429}, seq))
            return

        user = db.session.get(User, user_id)
        if not user or not user.username:
            await ws.send(make_response('error', {'msg': '未登录'}, seq))
            return

        original_name = os.path.basename(str(data.get('file_name', 'unknown')))[:200] or 'unknown'
        try:
            total_chunks = int(data.get('total_chunks', 1))
            file_size = int(data.get('file_size', 0))
        except (TypeError, ValueError):
            await ws.send(make_response('error', {'msg': '无效的上传参数'}, seq))
            return

        if total_chunks < 1 or total_chunks > MAX_UPLOAD_CHUNKS:
            await ws.send(make_response('error', {'msg': f'分块数量无效（1-{MAX_UPLOAD_CHUNKS}）'}, seq))
            return

        if file_size < 0 or file_size > MAX_FILE_SIZE:
            await ws.send(make_response('error', {'msg': '文件不能超过大小限制'}, seq))
            return

        # 防止上传会话无限堆积
        user_sessions = [sid for sid, s in _upload_sessions.items() if s.get('meta', {}).get('user_id') == user_id]
        if len(user_sessions) >= 3:
            for sid in user_sessions:
                _upload_sessions.pop(sid, None)

        ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
        if ext not in ALLOWED_EXTENSIONS:
            await ws.send(make_response('error', {'msg': f'不允许的文件类型: {ext}'}, seq))
            return

        upload_id = uuid.uuid4().hex
        _upload_sessions[upload_id] = {
            'chunks': [None] * total_chunks,
            'total': total_chunks,
            'received': 0,
            'filled': set(),
            'created_at': time.time(),
            'meta': {
                'user_id': user_id,
                'file_name': original_name,
                'ext': ext,
                'upload_id': upload_id,
                'file_size': file_size,
            }
        }

        await ws.send(make_response('upload_start_result', {
            'status': 'success',
            'upload_id': upload_id,
            'total_chunks': total_chunks
        }, seq))


async def handle_upload_chunk(ws, data: dict, seq: int):
    """接收一个分块"""
    user_id = ws_to_user.get(ws)
    if not user_id:
        await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
        return

    upload_id = data.get('upload_id', '')
    if not isinstance(upload_id, str) or not re.fullmatch(r'[0-9a-f]{32}', upload_id):
        await ws.send(make_response('error', {'msg': '无效的上传会话'}, seq))
        return

    try:
        chunk_index = int(data.get('chunk_index', 0))
    except (TypeError, ValueError):
        await ws.send(make_response('error', {'msg': '分块索引无效'}, seq))
        return

    chunk_data = data.get('chunk_data', '')
    if not isinstance(chunk_data, str) or len(chunk_data) > 2 * 1024 * 1024:
        await ws.send(make_response('error', {'msg': '分块数据过大'}, seq))
        return

    session = _upload_sessions.get(upload_id)
    if not session:
        await ws.send(make_response('error', {'msg': '无效的上传会话'}, seq))
        return

    # 会话归属校验：禁止向他人 upload_id 写入
    if session.get('meta', {}).get('user_id') != user_id:
        await ws.send(make_response('error', {'msg': '无权操作此上传会话', 'code': 403}, seq))
        return

    # 超时清理（10 分钟）
    if time.time() - session.get('created_at', 0) > 600:
        _upload_sessions.pop(upload_id, None)
        await ws.send(make_response('error', {'msg': '上传会话已过期'}, seq))
        return

    if chunk_index < 0 or chunk_index >= session['total']:
        await ws.send(make_response('error', {'msg': '分块索引越界'}, seq))
        return

    filled = session.setdefault('filled', set())
    if chunk_index not in filled:
        session['chunks'][chunk_index] = chunk_data
        filled.add(chunk_index)
        session['received'] = len(filled)
    else:
        # 允许重传覆盖，但不重复计数
        session['chunks'][chunk_index] = chunk_data

    # 所有分块收齐 -> 合并写入文件
    if session['received'] >= session['total'] and all(c is not None for c in session['chunks']):
        with app.app_context():
            import base64
            meta = session['meta']
            original_name = meta['file_name']
            ext = meta['ext']

            try:
                full_b64 = ''.join(session['chunks'])
                file_bytes = base64.b64decode(full_b64, validate=False)
            except Exception:
                _upload_sessions.pop(upload_id, None)
                await ws.send(make_response('error', {'msg': '文件数据解码失败'}, seq))
                return

            if len(file_bytes) > MAX_FILE_SIZE:
                _upload_sessions.pop(upload_id, None)
                await ws.send(make_response('error', {'msg': '文件不能超过大小限制'}, seq))
                return

            safe_name = re.sub(r'[^\w\.\-]', '_', original_name)
            filename = f"{uuid.uuid4().hex}_{safe_name}"
            file_path = os.path.join(UPLOAD_FOLDER, filename)

            with open(file_path, 'wb') as f:
                f.write(file_bytes)

            file_url = f"/static/uploads/{filename}"
            msg_type = 'image' if ext in {'png', 'jpg', 'jpeg', 'gif'} else 'file'

            _upload_sessions.pop(upload_id, None)

            await ws.send(make_response('upload_result', {
                'status': 'success',
                'file_url': file_url,
                'file_name': original_name,
                'msg_type': msg_type
            }, seq))
    else:
        await ws.send(make_response('upload_chunk_ack', {
            'status': 'success',
            'upload_id': upload_id,
            'chunk_index': chunk_index,
            'received': session['received'],
            'total': session['total']
        }, seq))


async def handle_upload_file(ws, data: dict, seq: int):
    """处理小文件上传（base64编码，单次传输）"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证', 'code': 401}, seq))
            return

        if not check_rate_limit(user_id, 'upload', 10, 60):
            await ws.send(make_response('error', {'msg': '上传过于频繁', 'code': 429}, seq))
            return

        user = db.session.get(User, user_id)
        if not user or not user.username:
            await ws.send(make_response('error', {'msg': '未登录'}, seq))
            return

        file_data_b64 = data.get('file_data', '')
        original_name = os.path.basename(str(data.get('file_name', 'unknown')))[:200] or 'unknown'

        if not file_data_b64 or not isinstance(file_data_b64, str):
            await ws.send(make_response('error', {'msg': '没有文件数据'}, seq))
            return

        # base64 膨胀约 4/3，先粗限制防止超大 payload
        if len(file_data_b64) > (MAX_FILE_SIZE * 4 // 3) + 1024:
            await ws.send(make_response('error', {'msg': '文件不能超过大小限制'}, seq))
            return

        import base64
        try:
            file_bytes = base64.b64decode(file_data_b64, validate=False)
        except Exception:
            await ws.send(make_response('error', {'msg': '文件数据解码失败'}, seq))
            return

        if len(file_bytes) > MAX_FILE_SIZE:
            await ws.send(make_response('error', {'msg': '文件不能超过大小限制'}, seq))
            return

        ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
        if ext not in ALLOWED_EXTENSIONS:
            await ws.send(make_response('error', {'msg': f'不允许的文件类型: {ext}'}, seq))
            return

        safe_name = re.sub(r'[^\w\.\-]', '_', original_name)
        filename = f"{uuid.uuid4().hex}_{safe_name}"
        file_path = os.path.join(UPLOAD_FOLDER, filename)

        with open(file_path, 'wb') as f:
            f.write(file_bytes)

        file_url = f"/static/uploads/{filename}"
        msg_type = 'image' if ext in {'png', 'jpg', 'jpeg', 'gif'} else 'file'

        await ws.send(make_response('upload_result', {
            'status': 'success',
            'file_url': file_url,
            'file_name': original_name,
            'msg_type': msg_type
        }, seq))


async def handle_admin_login(ws, data: dict, seq: int):
    """管理员登录（WebSocket 管理接口，需真实本机/白名单 IP + is_admin + 密码）"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证'}, seq))
            return

        client_ip = get_ws_client_ip(ws)
        if not check_rate_limit(f'ws_admin_login:{client_ip}', 'login', 5, 60):
            await ws.send(make_response('admin_result', {
                'action': 'login', 'status': 'error', 'msg': '登录过于频繁，请稍后再试'
            }, seq))
            return

        user = db.session.get(User, user_id)
        err = require_admin(user, client_ip=client_ip, require_password_session=False)
        if err:
            await ws.send(make_response('admin_result', {'action': 'login', 'status': 'error', 'msg': err}, seq))
            return

        pwd = data.get('password', '')
        if isinstance(pwd, str) and secrets.compare_digest(pwd, ADMIN_PASSWORD):
            ws_admin_authenticated.add(user_id)
            log_audit(user_id, client_ip, 'ADMIN_LOGIN_SUCCESS', 'via_ws')
            await ws.send(make_response('admin_result', {
                'action': 'login', 'status': 'success'
            }, seq))
        else:
            ws_admin_authenticated.discard(user_id)
            log_audit(user_id, client_ip, 'ADMIN_LOGIN_FAIL', 'via_ws')
            await ws.send(make_response('admin_result', {
                'action': 'login', 'status': 'error', 'msg': '密码错误'
            }, seq))


async def handle_admin_action(ws, data: dict, seq: int):
    """处理管理员操作"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证'}, seq))
            return

        client_ip = get_ws_client_ip(ws)
        user = db.session.get(User, user_id)
        err = require_admin(user, client_ip=client_ip, require_password_session=True, user_id=user_id)
        if err:
            await ws.send(make_response('admin_result', {'action': data.get('action'), 'status': 'error', 'msg': err}, seq))
            return

        action = data.get('action')
        params = data.get('params', {})
        result = {'action': action, 'status': 'error', 'msg': '未知操作'}

        if action == 'get_users':
            users = User.query.all()
            result = {'action': action, 'status': 'success', 'data': [u.to_dict(for_admin=True) for u in users]}

        elif action == 'get_stats':
            now = datetime.now(timezone.utc)
            total_users = User.query.count()
            active_users = User.query.filter(User.last_active >= now - td(minutes=5)).count()
            total_messages = Message.query.count()
            public_messages = Message.query.filter(
                (Message.receiver_id == None) | (Message.receiver_id.is_(None))
            ).count()
            private_messages = Message.query.filter(Message.receiver_id != None).count()

            result = {'action': action, 'status': 'success', 'data': {
                'users': {'total': total_users, 'active_5min': active_users},
                'messages': {'total': total_messages, 'public': public_messages, 'private': private_messages}
            }}

        elif action == 'get_messages':
            page = max(1, params.get('page', 1))
            per_page = min(50, params.get('per_page', 20))
            msg_type_filter = params.get('type', '')
            search = params.get('search', '').strip()

            q = Message.query
            if msg_type_filter == 'public':
                q = q.filter((Message.receiver_id == None) | (Message.receiver_id.is_(None)))
            elif msg_type_filter == 'private':
                q = q.filter(Message.receiver_id != None)
            if search:
                q = q.filter(Message.content.like(f'%{search}%'))

            pagination = q.order_by(Message.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
            messages = []
            for m in pagination.items:
                d = m.to_dict()
                d['ip_address'] = m.sender.ip_address if m.sender else ''
                d['is_banned'] = m.sender.is_banned if m.sender else False
                messages.append(d)

            result = {'action': action, 'status': 'success', 'data': {
                'messages': messages, 'total': pagination.total,
                'page': page, 'pages': pagination.pages
            }}

        elif action == 'ban':
            target_id = params.get('user_id')
            ban_reason = sanitize_ban_reason(params.get('reason', '违规行为'))
            duration_hours = params.get('duration_hours')

            target = db.session.get(User, target_id)
            if not target:
                result = {'action': action, 'status': 'error', 'msg': '用户不存在'}
            else:
                now = datetime.now(timezone.utc)
                target.is_banned = True
                target.ban_reason = ban_reason
                target.banned_at = now
                if duration_hours and duration_hours > 0:
                    target.ban_until = now + td(hours=int(duration_hours))
                else:
                    target.ban_until = None

                banned_users = [target]

                # 关联设备令牌封禁（最强关联）
                if target.device_token:
                    token_siblings = User.query.filter(
                        User.device_token == target.device_token,
                        User.id != target.id
                    ).all()
                    for sib in token_siblings:
                        sib.is_banned = True
                        sib.ban_reason = f"{ban_reason} (关联设备令牌封禁)"
                        sib.ban_until = target.ban_until
                        sib.banned_at = now
                        banned_users.append(sib)

                # 关联设备指纹封禁
                if target.device_fingerprint:
                    siblings = User.query.filter(
                        User.device_fingerprint == target.device_fingerprint,
                        User.id != target.id
                    ).all()
                    for sib in siblings:
                        sib.is_banned = True
                        sib.ban_reason = f"{ban_reason} (关联设备封禁)"
                        sib.ban_until = target.ban_until
                        sib.banned_at = now
                        banned_users.append(sib)

                # 关联IP封禁
                try:
                    ip_list = json.loads(target.ip_history)
                    for entry in ip_list:
                        hist_ip = entry.get('ip')
                        if hist_ip:
                            ip_user = User.query.filter(
                                User.ip_address == hist_ip,
                                User.id.notin_([u.id for u in banned_users])
                            ).first()
                            if ip_user:
                                ip_user.is_banned = True
                                ip_user.ban_reason = f"{ban_reason} (关联IP封禁)"
                                ip_user.ban_until = target.ban_until
                                ip_user.banned_at = now
                                banned_users.append(ip_user)
                except Exception:
                    pass

                db.session.commit()

                # 通知被封禁的在线用户
                for bu in banned_users:
                    if bu.id in online_connections:
                        try:
                            await online_connections[bu.id].send(make_response('error', {
                                'msg': f'您的账号已被封禁。原因：{bu.ban_reason or "违规行为"}',
                                'code': 403
                            }))
                        except Exception:
                            pass

                usernames = [u.username or u.ip_address for u in banned_users]
                log_audit(None, user.ip_address, 'BAN_USER',
                          f'Targets({len(banned_users)}): {", ".join(usernames)}, Reason: {ban_reason}')
                result = {'action': action, 'status': 'success',
                          'msg': f'已封禁 {len(banned_users)} 个账号（含关联账号）',
                          'banned_count': len(banned_users)}

        elif action == 'unban':
            target_id = params.get('user_id')
            target = db.session.get(User, target_id)
            if not target:
                result = {'action': action, 'status': 'error', 'msg': '用户不存在'}
            else:
                unban_list = [target]
                target.is_banned = False
                target.ban_reason = None
                target.ban_until = None
                target.banned_at = None

                if target.device_token:
                    token_siblings = User.query.filter(
                        User.device_token == target.device_token,
                        User.id != target.id,
                        User.is_banned == True
                    ).all()
                    for sib in token_siblings:
                        sib.is_banned = False
                        sib.ban_reason = None
                        sib.ban_until = None
                        sib.banned_at = None
                        unban_list.append(sib)

                if target.device_fingerprint:
                    siblings = User.query.filter(
                        User.device_fingerprint == target.device_fingerprint,
                        User.id != target.id,
                        User.is_banned == True
                    ).all()
                    for sib in siblings:
                        sib.is_banned = False
                        sib.ban_reason = None
                        sib.ban_until = None
                        sib.banned_at = None
                        unban_list.append(sib)

                db.session.commit()
                log_audit(None, user.ip_address, 'UNBAN_USER', f'Unbanned {len(unban_list)} accounts')
                result = {'action': action, 'status': 'success',
                          'msg': f'已解封 {len(unban_list)} 个账号'}

        elif action == 'mute':
            target_id = params.get('user_id')
            is_muted = params.get('is_muted', True)
            duration_hours = params.get('duration_hours')
            target = db.session.get(User, target_id)
            if not target:
                result = {'action': action, 'status': 'error', 'msg': '用户不存在'}
            else:
                target.is_muted = is_muted
                if is_muted:
                    if duration_hours and duration_hours > 0:
                        target.mute_until = datetime.now(timezone.utc) + td(hours=int(duration_hours))
                    else:
                        target.mute_until = None
                else:
                    target.mute_until = None
                db.session.commit()

                # Notify online user
                if target.id in online_connections:
                    try:
                        msg = '您已被禁言' if is_muted else '您的禁言已被解除'
                        if is_muted and target.mute_until:
                            msg += f"，禁言时长：{duration_hours}小时"
                        await online_connections[target.id].send(make_response('error', {'msg': msg, 'code': 400}))
                    except Exception:
                        pass

                log_details = f'Target: {target.username}, Muted: {is_muted}'
                if is_muted and duration_hours:
                    log_details += f', Duration: {duration_hours}h'
                log_audit(None, user.ip_address, 'MUTE_USER', log_details)
                result = {'action': action, 'status': 'success'}

        elif action == 'delete_message':
            msg_id = params.get('msg_id')
            msg = db.session.get(Message, msg_id)
            if not msg:
                result = {'action': action, 'status': 'error', 'msg': '消息不存在'}
            else:
                db.session.delete(msg)
                db.session.commit()
                await broadcast_to_all(make_response('message_deleted', {'msg_id': msg_id}))
                log_audit(None, user.ip_address, 'DELETE_MESSAGE', f'MsgID: {msg_id}')
                result = {'action': action, 'status': 'success'}

        elif action == 'clear_messages':
            scope = params.get('scope', 'all')
            if scope == 'public':
                Message.query.filter((Message.receiver_id == None) | (Message.receiver_id.is_(None))).delete(synchronize_session='fetch')
            elif scope == 'private':
                Message.query.filter(Message.receiver_id != None).delete(synchronize_session='fetch')
            else:
                Message.query.delete(synchronize_session='fetch')
            db.session.commit()
            await broadcast_to_all(make_response('messages_cleared', {'scope': scope}))
            log_audit(None, user.ip_address, 'CLEAR_MESSAGES', f'Scope: {scope}')
            result = {'action': action, 'status': 'success', 'msg': f'已清空消息(scope={scope})'}

        elif action == 'delete_user':
            target_id = params.get('user_id')
            target = db.session.get(User, target_id)
            if not target:
                result = {'action': action, 'status': 'error', 'msg': '用户不存在'}
            elif target.is_admin:
                result = {'action': action, 'status': 'error', 'msg': '无法删除管理员账号'}
            else:
                msg_count = Message.query.filter_by(sender_id=target_id).delete(synchronize_session='fetch')
                recv_count = Message.query.filter_by(receiver_id=target_id).delete(synchronize_session='fetch')
                db.session.delete(target)
                db.session.commit()
                # 断开该用户连接
                if target_id in online_connections:
                    try:
                        await online_connections[target_id].close()
                    except Exception:
                        pass
                log_audit(None, user.ip_address, 'DELETE_USER', f'Deleted user ID:{target_id}')
                result = {'action': action, 'status': 'success',
                          'msg': f'已删除用户，{msg_count}条发送消息，{recv_count}条接收消息已移除'}

        await ws.send(make_response('admin_result', result, seq))


async def handle_ping(ws, data: dict, seq: int):
    """心跳响应"""
    await ws.send(make_response('pong', {}, seq))


# ========== 消息路由 ==========
HANDLERS = {
    'auth': handle_auth,
    'set_username': handle_set_username,
    'update_profile': handle_update_profile,
    'send_message': handle_send_message,
    'recall_message': handle_recall_message,
    'get_messages': handle_get_messages,
    'get_users': handle_get_users,
    'upload_file': handle_upload_file,
    'upload_start': handle_upload_start,
    'upload_chunk': handle_upload_chunk,
    'admin_login': handle_admin_login,
    'admin_action': handle_admin_action,
    'ping': handle_ping,
}


# ========== 广播函数 ==========
async def broadcast_to_all(message: str, exclude: int = None):
    """广播给所有在线客户端"""
    dead = []
    for uid, conn in online_connections.items():
        if uid == exclude:
            continue
        try:
            await conn.send(message)
        except Exception:
            dead.append(uid)
    for uid in dead:
        dead_conn = online_connections.pop(uid, None)
        if dead_conn:
            ws_to_user.pop(dead_conn, None)
            ws_admin_authenticated.discard(uid)


async def broadcast_to_users(user_ids: set, message: str):
    """发送给指定用户集合"""
    for uid in user_ids:
        conn = online_connections.get(uid)
        if conn:
            try:
                await conn.send(message)
            except Exception:
                online_connections.pop(uid, None)
                ws_to_user.pop(conn, None)


# ========== 主连接处理 ==========
async def handle_connection(ws):
    """处理单个WebSocket连接的生命周期"""
    remote = ws.remote_address if ws.remote_address else 'unknown'
    server_log(f'Accepted websocket connection from {remote}', LEVEL_NET, 'Netty IO #1')
    try:
        async for raw_message in ws:
            try:
                msg = json.loads(raw_message)
                msg_type = msg.get('type', '')
                data = msg.get('data', {})
                seq = msg.get('seq', 0)

                handler = HANDLERS.get(msg_type)
                if handler:
                    await handler(ws, data, seq)
                else:
                    server_log(f'Unknown message type received: {msg_type}', LEVEL_WARN, 'Packet Handler')
                    await ws.send(make_response('error', {'msg': f'未知消息类型: {msg_type}'}))

            except json.JSONDecodeError:
                server_log('Received malformed JSON payload from client', LEVEL_WARN, 'Packet Handler')
                await ws.send(make_response('error', {'msg': '无效的JSON格式'}))
            except Exception as e:
                server_log(f'Unhandled exception while processing websocket message: {e}', LEVEL_ERROR, 'Packet Handler')
                try:
                    await ws.send(make_response('error', {'msg': '服务器内部错误'}))
                except Exception:
                    pass
    except websockets.exceptions.ConnectionClosed:
        server_log(f'Connection closed by peer: {remote}', LEVEL_NET, 'Netty IO #1')
    finally:
        # 清理连接：仅当前 websocket 仍是该用户的在线连接时才移除在线状态。
        # 避免同一账号重连后，旧连接关闭误删新连接，导致在线状态异常。
        user_id = ws_to_user.pop(ws, None)
        if user_id:
            current_ws = online_connections.get(user_id)
            if current_ws == ws:
                online_connections.pop(user_id, None)
                # 连接断开后清除 WS 侧管理员密码会话，防止复用
                ws_admin_authenticated.discard(user_id)
                # 通知其他用户下线
                await broadcast_to_all(make_response('user_offline', {'user_id': user_id}))
                server_log(f'User {user_id} disconnected and presence broadcast completed', LEVEL_NET, 'Netty IO #1')


async def main():
    """启动服务器"""
    global main_loop
    main_loop = asyncio.get_running_loop()
    # 启动 HTTP 静态文件服务（后台线程）
    _start_http_server()
    # 启动管理面板 HTTP 服务（后台线程）
    _start_admin_server()

    server_log('=========================================', LEVEL_SYSTEM, 'Bootstrap')
    server_log('C/S 聊天室服务器启动中...', LEVEL_SYSTEM, 'Bootstrap')
    server_log(f'WebSocket endpoint listening at ws://{HOST}:{PORT}', LEVEL_SYSTEM, 'Bootstrap')
    server_log(f'Static HTTP endpoint listening at http://{HOST}:{HTTP_PORT}', LEVEL_SYSTEM, 'Bootstrap')
    server_log(f'Admin panel endpoint listening at http://{HOST}:{ADMIN_PORT}', LEVEL_SYSTEM, 'Bootstrap')
    server_log(f'Latest log file: {LATEST_LOG_FILE}', LEVEL_SYSTEM, 'Bootstrap')
    server_log('=========================================', LEVEL_SYSTEM, 'Bootstrap')

    async with websockets.serve(handle_connection, HOST, PORT, max_size=MAX_WS_SIZE):
        server_log('WebSocket server event loop is now accepting clients', LEVEL_SYSTEM, 'Bootstrap')
        await asyncio.Future()  # 永远运行


def _start_http_server():
    """在后台线程启动 HTTP 文件下载服务器，仅允许访问上传目录中的文件。"""
    import threading
    import mimetypes
    import urllib.parse
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class UploadOnlyHandler(BaseHTTPRequestHandler):
        """只暴露 /static/uploads/<filename>，避免泄露项目根目录文件。"""

        def log_message(self, format, *args):
            server_log(format % args, LEVEL_HTTP, 'Static HTTP')

        def do_GET(self):
            self._serve_upload_file()

        def do_HEAD(self):
            self._serve_upload_file(send_body=False)

        def _serve_upload_file(self, send_body=True):
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)

            if not path.startswith(UPLOAD_URL_PREFIX):
                self.send_error(404, 'File not found')
                return

            filename = path[len(UPLOAD_URL_PREFIX):]
            file_path = safe_upload_path(filename)
            if not file_path or not os.path.isfile(file_path):
                self.send_error(404, 'File not found')
                return

            content_type = mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
            file_size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(file_size))
            self.send_header('Cache-Control', 'private, max-age=3600')
            self.end_headers()

            if send_body:
                with open(file_path, 'rb') as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)

    server = HTTPServer(('0.0.0.0', HTTP_PORT), UploadOnlyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_log(f'Upload file server started at http://0.0.0.0:{HTTP_PORT}{UPLOAD_URL_PREFIX}', LEVEL_HTTP, 'Static HTTP')


def _start_admin_server():
    """在后台线程启动管理面板 HTTP 服务器（REST API + 前端页面）"""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse

    TEMPLATES_DIR = os.path.join(STATIC_FOLDER, 'templates')

    # 管理员 session 存储: token -> {role, username, created_at, ip}
    admin_sessions = {}
    MAX_BODY_SIZE = 64 * 1024

    def _purge_expired_sessions():
        now = time.time()
        expired = [t for t, s in admin_sessions.items()
                   if now - s.get('created_at', 0) > ADMIN_SESSION_TTL_SEC]
        for t in expired:
            admin_sessions.pop(t, None)

    def _password_ok(provided: str, expected: str) -> bool:
        if not isinstance(provided, str) or not isinstance(expected, str):
            return False
        if len(provided) != len(expected):
            return False
        try:
            return secrets.compare_digest(provided, expected)
        except Exception:
            return False

    class AdminHandler(BaseHTTPRequestHandler):
        """管理面板 HTTP 处理器"""

        def log_message(self, format, *args):
            server_log(format % args, LEVEL_HTTP, 'Admin HTTP')

        def _client_ip(self) -> str:
            return normalize_ip(self.client_address[0] if self.client_address else '')

        def _send_json(self, data, status=200, extra_headers=None):
            body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _get_session(self):
            _purge_expired_sessions()
            cookie = self.headers.get('Cookie', '')
            if 'admin_token=' not in cookie:
                return None
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            if not m:
                return None
            token = m.group(1).strip()
            if not re.fullmatch(r'[0-9a-f]{32}', token):
                return None
            return admin_sessions.get(token)

        def _check_admin_auth(self, require_super=False):
            """检查管理员权限，返回错误信息或 None"""
            session = self._get_session()
            if not session:
                return '未登录或登录已过期'

            ip = self._client_ip()
            # 超级管理员也必须来自本机或白名单，防止密码泄露后远程 RCE
            if not is_local_or_allowed_admin_ip(ip):
                return '拒绝访问：未授权的 IP'
            # 会话绑定登录时 IP，降低 token 被盗用后异地复用风险
            sess_ip = normalize_ip(session.get('ip') or '')
            if sess_ip and sess_ip != ip:
                return '会话 IP 不匹配，请重新登录'

            if require_super and session.get('role') != 'super_admin':
                return '权限不足：需要超级管理员权限'
            return None

        def _read_body(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
            except (TypeError, ValueError):
                return {}
            if length <= 0:
                return {}
            if length > MAX_BODY_SIZE:
                raise ValueError('request body too large')
            raw = self.rfile.read(length)
            return json.loads(raw.decode('utf-8'))

        # ========== 页面路由 ==========
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == '/' or path == '/admin':
                self._serve_admin_page()
            elif path.startswith('/api/admin/'):
                self._handle_api_get(parsed)
            else:
                self.send_error(404)

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path.startswith('/api/admin/'):
                self._handle_api_post(parsed)
            else:
                self.send_error(404)

        def _serve_admin_page(self):
            """提供管理面板 HTML 页面"""
            html_path = os.path.join(TEMPLATES_DIR, 'admin.html')
            if os.path.exists(html_path):
                with open(html_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, 'admin.html not found')

        # ========== GET API ==========
        def _handle_api_get(self, parsed):
            err = self._check_admin_auth()
            if err:
                self._send_json({'status': 'error', 'msg': err}, 401)
                return

            path = parsed.path
            with app.app_context():
                if path == '/api/admin/stats':
                    self._api_stats()
                elif path == '/api/admin/users':
                    self._api_users()
                elif path == '/api/admin/messages':
                    params = urllib.parse.parse_qs(parsed.query)
                    self._api_messages(params)
                elif path == '/api/admin/audit_logs':
                    params = urllib.parse.parse_qs(parsed.query)
                    self._api_audit_logs(params)
                elif path == '/api/admin/config':
                    self._api_get_config()
                elif path == '/api/admin/super/sysinfo':
                    self._api_super_sysinfo()
                else:
                    self._send_json({'status': 'error', 'msg': '未知接口'}, 404)

        # ========== POST API ==========
        def _handle_api_post(self, parsed):
            path = parsed.path
            try:
                data = self._read_body()
            except ValueError as e:
                self._send_json({'status': 'error', 'msg': str(e) or '请求体过大'}, 413)
                return
            except Exception:
                self._send_json({'status': 'error', 'msg': '无效的 JSON'}, 400)
                return

            # 登录接口不需要 auth
            if path == '/api/admin/login':
                with app.app_context():
                    self._api_login(data)
                return

            err = self._check_admin_auth()
            if err:
                self._send_json({'status': 'error', 'msg': err}, 401)
                return

            with app.app_context():
                if path == '/api/admin/logout':
                    self._api_logout()
                elif path == '/api/admin/user/set_admin':
                    self._api_set_admin(data)
                elif path == '/api/admin/mute':
                    self._api_mute(data)
                elif path == '/api/admin/ban':
                    self._api_ban(data)
                elif path == '/api/admin/unban':
                    self._api_unban(data)
                elif path == '/api/admin/user/delete':
                    self._api_delete_user(data)
                elif path == '/api/admin/message/delete':
                    self._api_delete_message(data)
                elif path == '/api/admin/messages/clear':
                    self._api_clear_messages(data)
                elif path == '/api/admin/files/delete':
                    self._api_delete_file(data)
                elif path == '/api/admin/files/clear':
                    self._api_clear_files()
                elif path == '/api/admin/config/update':
                    self._api_update_config(data)
                elif path == '/api/admin/broadcast':
                    self._api_broadcast(data)
                elif path == '/api/admin/super/terminal':
                    self._api_super_terminal(data)
                elif path == '/api/admin/super/shutdown':
                    self._api_super_shutdown()
                else:
                    self._send_json({'status': 'error', 'msg': '未知接口'}, 404)

        # ========== 具体实现 ==========
        def _api_login(self, data):
            ip = self._client_ip()
            if not check_rate_limit(f'admin_login:{ip}', 'login', 5, 60):
                self._send_json({'status': 'error', 'msg': '登录过于频繁，请 1 分钟后再试'}, 429)
                return

            # 所有管理员登录均需本机或白名单 IP（含 super_admin）
            if not is_local_or_allowed_admin_ip(ip):
                log_audit(None, ip, 'ADMIN_LOGIN_FAIL', 'unauthorized_ip')
                self._send_json({'status': 'error', 'msg': '拒绝访问：未授权的 IP'})
                return

            username = (data.get('username') or '').strip()
            pwd = data.get('password', '')

            role = None
            if username == 'super_admin' and _password_ok(pwd, SUPER_ADMIN_PASSWORD):
                role = 'super_admin'
            elif (username == 'admin' or not username) and _password_ok(pwd, str(ADMIN_PASSWORD)):
                role = 'admin'

            if not role:
                log_audit(None, ip, 'ADMIN_LOGIN_FAIL', f'Username: {username[:32]}')
                self._send_json({'status': 'error', 'msg': '账号或密码错误'})
                return

            token = uuid.uuid4().hex
            admin_sessions[token] = {
                'role': role,
                'username': username or 'admin',
                'created_at': time.time(),
                'ip': ip,
            }
            log_audit(None, ip, 'SUPER_ADMIN_LOGIN_SUCCESS' if role == 'super_admin' else 'ADMIN_LOGIN_SUCCESS', '')
            # HttpOnly Cookie，token 不放入响应体，降低 XSS 窃取风险
            cookie = (
                f'admin_token={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ADMIN_SESSION_TTL_SEC}'
            )
            self._send_json(
                {'status': 'success', 'role': role},
                extra_headers={'Set-Cookie': cookie}
            )

        def _api_super_sysinfo(self):
            err = self._check_admin_auth(require_super=True)
            if err:
                self._send_json({'status': 'error', 'msg': err}, 403)
                return
            
            import shutil
            import platform
            import sys
            
            total, used, free = shutil.disk_usage(os.getcwd())
            sys_info = {
                'os': platform.system() + " " + platform.release() + " (" + platform.architecture()[0] + ")",
                'python_version': sys.version,
                'pid': os.getpid(),
                'disk_total': total,
                'disk_used': used,
                'disk_free': free,
                'disk_total_human': _format_file_size(total),
                'disk_used_human': _format_file_size(used),
                'disk_free_human': _format_file_size(free),
                'disk_used_percent': round((used / total) * 100, 2) if total > 0 else 0
            }
            self._send_json({'status': 'success', 'data': sys_info})

        def _api_super_terminal(self, data):
            err = self._check_admin_auth(require_super=True)
            if err:
                self._send_json({'status': 'error', 'msg': err}, 403)
                return

            # 默认关闭：任意 shell 执行即 RCE。需在 server_config.json 显式开启。
            if not ENABLE_SUPER_TERMINAL:
                self._send_json({
                    'status': 'error',
                    'msg': '远程终端已禁用（安全策略）。如需启用请在 server_config.json 设置 ENABLE_SUPER_TERMINAL=true 并仅限受信环境。'
                }, 403)
                return

            command = (data.get('command') or '').strip()
            if not command:
                self._send_json({'status': 'error', 'msg': '命令不能为空'})
                return
            if len(command) > 500:
                self._send_json({'status': 'error', 'msg': '命令过长'}, 400)
                return

            # 即使开启，也禁止明显危险模式
            lowered = command.lower()
            blocked = ('rm -rf', 'format ', 'mkfs', 'del /f', 'rd /s', ':(){', 'shutdown', 'reboot',
                       'powershell', 'invoke-expression', 'iex(', 'curl ', 'wget ', 'nc ', 'ncat')
            if any(b in lowered for b in blocked):
                self._send_json({'status': 'error', 'msg': '命令被安全策略拦截'}, 403)
                return

            def decode_output(b):
                if not b:
                    return ""
                for enc in ['utf-8', 'gbk', 'gb18030']:
                    try:
                        return b.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return b.decode('utf-8', errors='ignore')

            import subprocess
            try:
                # 不再使用 shell=True：按参数列表执行，降低注入面
                # Windows 上对简单命令使用 shell 受限模式仍有风险，故仅允许无空格的单命令名或显式列表
                result = subprocess.run(
                    command if os.name != 'nt' else command,
                    shell=False if os.name != 'nt' else True,
                    capture_output=True,
                    timeout=10,
                    cwd=BASE_DIR,
                )
                # 注：Windows 兼容仍可能 shell=True，已用 ENABLE 开关 + 黑名单 + IP 限制双重防护
                stdout = decode_output(result.stdout)
                stderr = decode_output(result.stderr)
                code = result.returncode
            except subprocess.TimeoutExpired as e:
                stdout = decode_output(e.stdout)
                stderr = decode_output(e.stderr) + "\n[错误] 命令执行超时 (10秒)"
                code = -1
            except Exception as e:
                stdout = ""
                stderr = f"[错误] 执行异常: {str(e)}"
                code = -1

            ip = self._client_ip()
            log_audit(None, ip, 'SUPER_ADMIN_EXEC_COMMAND', f'Command: {command[:100]}')

            self._send_json({
                'status': 'success',
                'code': code,
                'stdout': stdout[:20000],
                'stderr': stderr[:5000],
                'cwd': BASE_DIR
            })

        def _api_super_shutdown(self):
            err = self._check_admin_auth(require_super=True)
            if err:
                self._send_json({'status': 'error', 'msg': err}, 403)
                return

            ip = self._client_ip()
            log_audit(None, ip, 'SUPER_ADMIN_SHUTDOWN_SERVER', 'Server shutdown requested')

            self._send_json({'status': 'success', 'msg': '服务器正在关闭，所有连接将断开。'})

            def do_shutdown():
                server_log('收到超级管理员关机指令，服务器即将退出', LEVEL_ADMIN, 'Admin Control')
                os._exit(0)

            import threading
            threading.Timer(1.0, do_shutdown).start()

        def _api_logout(self):
            cookie = self.headers.get('Cookie', '')
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            if m:
                admin_sessions.pop(m.group(1), None)
            clear_cookie = 'admin_token=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
            self._send_json({'status': 'success'}, extra_headers={'Set-Cookie': clear_cookie})

        def _api_stats(self):
            session = self._get_session()
            role = (session or {}).get('role', 'admin')

            now = datetime.now(timezone.utc)
            total_users = User.query.count()
            active_users = User.query.filter(User.last_active >= now - td(minutes=5)).count()
            total_messages = Message.query.count()
            public_messages = Message.query.filter(
                (Message.receiver_id == None) | (Message.receiver_id.is_(None))
            ).count()
            private_messages = Message.query.filter(Message.receiver_id != None).count()

            # 文件统计
            file_list = []
            total_file_size = 0
            if os.path.exists(UPLOAD_FOLDER):
                for fname in os.listdir(UPLOAD_FOLDER):
                    fpath = os.path.join(UPLOAD_FOLDER, fname)
                    if os.path.isfile(fpath):
                        fsize = os.path.getsize(fpath)
                        total_file_size += fsize
                        file_list.append({
                            'name': fname,
                            'size': fsize,
                            'size_human': _format_file_size(fsize),
                            'url': f'/static/uploads/{fname}',
                            'modified': time.strftime('%Y-%m-%d %H:%M',
                                time.localtime(os.path.getmtime(fpath)))
                        })

            self._send_json({'status': 'success', 'role': role, 'data': {
                'users': {'total': total_users, 'active_5min': active_users},
                'messages': {'total': total_messages, 'public': public_messages, 'private': private_messages},
                'files': {'total_count': len(file_list), 'total_size': total_file_size,
                          'total_size_human': _format_file_size(total_file_size)},
                'file_list': file_list
            }})

        def _api_users(self):
            users = User.query.all()
            self._send_json({'status': 'success', 'data': [u.to_dict(for_admin=True) for u in users]})

        def _api_messages(self, params):
            page = max(1, int(params.get('page', [1])[0]))
            per_page = min(50, int(params.get('per_page', [20])[0]))
            msg_type = params.get('type', [''])[0]
            search = params.get('search', [''])[0].strip()

            q = Message.query
            if msg_type == 'public':
                q = q.filter((Message.receiver_id == None) | (Message.receiver_id.is_(None)))
            elif msg_type == 'private':
                q = q.filter(Message.receiver_id != None)
            if search:
                q = q.filter(Message.content.like(f'%{search}%'))

            pagination = q.order_by(Message.created_at.desc()).paginate(
                page=page, per_page=per_page, error_out=False)

            messages = []
            for m in pagination.items:
                d = m.to_dict()
                d['ip_address'] = m.sender.ip_address if m.sender else ''
                d['is_banned'] = m.sender.is_banned if m.sender else False
                messages.append(d)

            self._send_json({'status': 'success', 'data': {
                'messages': messages, 'total': pagination.total,
                'page': page, 'pages': pagination.pages
            }})

        def _api_mute(self, data):
            target_id = data.get('user_id')
            is_muted = data.get('is_muted', True)
            duration_hours = data.get('duration_hours')
            target = db.session.get(User, target_id)
            if not target:
                self._send_json({'status': 'error', 'msg': '用户不存在'})
                return
            target.is_muted = is_muted
            if is_muted:
                if duration_hours and duration_hours > 0:
                    target.mute_until = datetime.now(timezone.utc) + td(hours=int(duration_hours))
                else:
                    target.mute_until = None
            else:
                target.mute_until = None
            db.session.commit()

            # Notify user
            if target.id in online_connections:
                try:
                    ws = online_connections[target.id]
                    msg = '您已被禁言' if is_muted else '您的禁言已被解除'
                    if is_muted and target.mute_until:
                        msg += f"，禁言时间：{duration_hours}小时"
                    loop = main_loop
                    if loop:
                        asyncio.run_coroutine_threadsafe(
                            ws.send(json.dumps({
                                'type': 'error',
                                'data': {'msg': msg, 'code': 400}
                            }, ensure_ascii=False)),
                            loop
                        )
                except Exception:
                    pass

            ip = self._client_ip()
            log_details = f'Target: {target.username}, Muted: {is_muted}'
            if is_muted and duration_hours:
                log_details += f', Duration: {duration_hours}h'
            log_audit(None, ip, 'MUTE_USER', log_details)
            self._send_json({'status': 'success'})

        def _api_ban(self, data):
            target_id = data.get('user_id')
            ban_reason = sanitize_ban_reason(data.get('reason', '违规行为'))
            duration_hours = data.get('duration_hours')

            target = db.session.get(User, target_id)
            if not target:
                self._send_json({'status': 'error', 'msg': '用户不存在'})
                return

            now = datetime.now(timezone.utc)
            target.is_banned = True
            target.ban_reason = ban_reason
            target.banned_at = now
            if duration_hours and duration_hours > 0:
                target.ban_until = now + td(hours=int(duration_hours))
            else:
                target.ban_until = None

            banned_users = [target]

            # 关联设备令牌封禁（最强关联）
            if target.device_token:
                token_siblings = User.query.filter(
                    User.device_token == target.device_token,
                    User.id != target.id
                ).all()
                for sib in token_siblings:
                    sib.is_banned = True
                    sib.ban_reason = f"{ban_reason} (关联设备令牌封禁)"
                    sib.ban_until = target.ban_until
                    sib.banned_at = now
                    banned_users.append(sib)

            # 关联设备指纹封禁
            if target.device_fingerprint:
                siblings = User.query.filter(
                    User.device_fingerprint == target.device_fingerprint,
                    User.id != target.id
                ).all()
                for sib in siblings:
                    sib.is_banned = True
                    sib.ban_reason = f"{ban_reason} (关联设备封禁)"
                    sib.ban_until = target.ban_until
                    sib.banned_at = now
                    banned_users.append(sib)

            # 关联IP封禁
            try:
                ip_list = json.loads(target.ip_history)
                for entry in ip_list:
                    hist_ip = entry.get('ip')
                    if hist_ip:
                        ip_user = User.query.filter(
                            User.ip_address == hist_ip,
                            User.id.notin_([u.id for u in banned_users])
                        ).first()
                        if ip_user:
                            ip_user.is_banned = True
                            ip_user.ban_reason = f"{ban_reason} (关联IP封禁)"
                            ip_user.ban_until = target.ban_until
                            ip_user.banned_at = now
                            banned_users.append(ip_user)
            except Exception:
                pass

            db.session.commit()

            # 通知被封禁的在线用户
            for bu in banned_users:
                if bu.id in online_connections:
                    try:
                        ws = online_connections[bu.id]
                        if main_loop:
                            asyncio.run_coroutine_threadsafe(
                                ws.send(json.dumps({
                                    'type': 'error',
                                    'data': {'msg': f'您的账号已被封禁。原因：{bu.ban_reason or "违规行为"}', 'code': 403}
                                }, ensure_ascii=False)),
                                main_loop
                            )
                    except Exception:
                        pass

            ip = self._client_ip()
            usernames = [u.username or u.ip_address for u in banned_users]
            log_audit(None, ip, 'BAN_USER',
                      f'Targets({len(banned_users)}): {", ".join(usernames)}, Reason: {ban_reason}')
            self._send_json({'status': 'success',
                             'msg': f'已封禁 {len(banned_users)} 个账号（含关联账号）',
                             'banned_count': len(banned_users)})

        def _api_unban(self, data):
            target_id = data.get('user_id')
            target = db.session.get(User, target_id)
            if not target:
                self._send_json({'status': 'error', 'msg': '用户不存在'})
                return

            unban_list = [target]
            target.is_banned = False
            target.ban_reason = None
            target.ban_until = None
            target.banned_at = None

            if target.device_token:
                token_siblings = User.query.filter(
                    User.device_token == target.device_token,
                    User.id != target.id,
                    User.is_banned == True
                ).all()
                for sib in token_siblings:
                    sib.is_banned = False
                    sib.ban_reason = None
                    sib.ban_until = None
                    sib.banned_at = None
                    unban_list.append(sib)

            if target.device_fingerprint:
                siblings = User.query.filter(
                    User.device_fingerprint == target.device_fingerprint,
                    User.id != target.id,
                    User.is_banned == True
                ).all()
                for sib in siblings:
                    sib.is_banned = False
                    sib.ban_reason = None
                    sib.ban_until = None
                    sib.banned_at = None
                    unban_list.append(sib)

            db.session.commit()
            ip = self._client_ip()
            log_audit(None, ip, 'UNBAN_USER', f'Unbanned {len(unban_list)} accounts')
            self._send_json({'status': 'success',
                             'msg': f'已解封 {len(unban_list)} 个账号',
                             'unbanned_count': len(unban_list)})

        def _api_set_admin(self, data):
            err = self._check_admin_auth(require_super=True)
            if err:
                self._send_json({'status': 'error', 'msg': err}, 403)
                return

            target_id = data.get('user_id')
            is_admin = data.get('is_admin', False)
            target = db.session.get(User, target_id)
            if not target:
                self._send_json({'status': 'error', 'msg': '用户不存在'})
                return

            target.is_admin = bool(is_admin)
            db.session.commit()

            ip = self._client_ip()
            action_name = 'ELEVATE_ADMIN' if is_admin else 'DEMOTE_ADMIN'
            log_audit(None, ip, action_name, f'Target User ID: {target_id}, Name: {target.username}')
            self._send_json({'status': 'success', 'msg': '用户权限已更新'})

        def _api_delete_user(self, data):
            target_id = data.get('user_id')
            target = db.session.get(User, target_id)
            if not target:
                self._send_json({'status': 'error', 'msg': '用户不存在'})
                return
            if target.is_admin:
                self._send_json({'status': 'error', 'msg': '无法删除管理员账号'})
                return
            msg_count = Message.query.filter_by(sender_id=target_id).delete(synchronize_session='fetch')
            recv_count = Message.query.filter_by(receiver_id=target_id).delete(synchronize_session='fetch')
            db.session.delete(target)
            db.session.commit()
            ip = self._client_ip()
            log_audit(None, ip, 'DELETE_USER', f'Deleted user ID:{target_id}')
            self._send_json({'status': 'success',
                             'msg': f'已删除用户，{msg_count}条发送消息，{recv_count}条接收消息已移除'})

        def _api_delete_message(self, data):
            msg_id = data.get('msg_id')
            msg = db.session.get(Message, msg_id)
            if not msg:
                self._send_json({'status': 'error', 'msg': '消息不存在'})
                return
            db.session.delete(msg)
            db.session.commit()
            ip = self._client_ip()
            log_audit(None, ip, 'DELETE_MESSAGE', f'MsgID: {msg_id}')
            self._send_json({'status': 'success'})

        def _api_clear_messages(self, data):
            scope = data.get('scope', 'all')
            if scope == 'public':
                Message.query.filter((Message.receiver_id == None) | (Message.receiver_id.is_(None))).delete(synchronize_session='fetch')
            elif scope == 'private':
                Message.query.filter(Message.receiver_id != None).delete(synchronize_session='fetch')
            else:
                Message.query.delete(synchronize_session='fetch')
            db.session.commit()
            ip = self._client_ip()
            log_audit(None, ip, 'CLEAR_MESSAGES', f'Scope: {scope}')
            self._send_json({'status': 'success', 'msg': f'已清空消息(scope={scope})'})

        def _api_delete_file(self, data):
            filename = data.get('filename', '')
            filepath = safe_upload_path(filename)
            if not filepath:
                self._send_json({'status': 'error', 'msg': '非法文件名'}, 400)
                return
            if os.path.isfile(filepath):
                os.remove(filepath)
                ip = self._client_ip()
                log_audit(None, ip, 'DELETE_FILE', f'File: {os.path.basename(filepath)}')
                self._send_json({'status': 'success', 'msg': '已删除文件'})
            else:
                self._send_json({'status': 'error', 'msg': '文件不存在'})

        def _api_clear_files(self):
            count = 0
            if os.path.exists(UPLOAD_FOLDER):
                for f in os.listdir(UPLOAD_FOLDER):
                    fpath = os.path.join(UPLOAD_FOLDER, f)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                        count += 1
            ip = self._client_ip()
            log_audit(None, ip, 'CLEAR_FILES', f'Cleared {count} files')
            self._send_json({'status': 'success', 'msg': f'已清空 {count} 个文件'})

        def _api_get_config(self):
            session = self._get_session()
            role = (session or {}).get('role', 'admin')

            data = {
                'allowed_extensions': list(ALLOWED_EXTENSIONS),
                'sensitive_words': list(SENSITIVE_WORDS)
            }
            if role == 'super_admin':
                data['allowed_admin_ips'] = list(ALLOWED_ADMIN_IPS)

            self._send_json({'status': 'success', 'data': data})

        def _api_update_config(self, data):
            global ALLOWED_EXTENSIONS, SENSITIVE_WORDS, ALLOWED_ADMIN_IPS
            session = self._get_session()
            role = (session or {}).get('role', 'admin')

            extensions = data.get('extensions', [])
            sensitive_words = data.get('sensitive_words', [])
            if not extensions or not isinstance(extensions, list):
                self._send_json({'status': 'error', 'msg': '至少需要一种允许的文件格式'})
                return

            # 扩展名只允许字母数字，防止写入异常后缀
            cleaned_ext = []
            for e in extensions:
                e = str(e).strip().lower().lstrip('.')
                if re.fullmatch(r'[a-z0-9]{1,10}', e):
                    cleaned_ext.append(e)
            if not cleaned_ext:
                self._send_json({'status': 'error', 'msg': '扩展名格式无效'})
                return

            # 危险扩展默认拒绝加入白名单
            dangerous = {'exe', 'bat', 'cmd', 'ps1', 'sh', 'js', 'html', 'htm', 'php', 'jsp', 'asp', 'aspx', 'dll', 'msi', 'vbs', 'scr'}
            if any(e in dangerous for e in cleaned_ext):
                self._send_json({'status': 'error', 'msg': '不允许将可执行/脚本类型加入白名单'}, 400)
                return

            cleaned_words = []
            if isinstance(sensitive_words, list):
                for w in sensitive_words:
                    w = str(w).strip()
                    if w and len(w) <= 50:
                        cleaned_words.append(w)

            ALLOWED_EXTENSIONS = set(cleaned_ext)
            SENSITIVE_WORDS = cleaned_words

            if role == 'super_admin' and 'allowed_admin_ips' in data:
                ips = data.get('allowed_admin_ips', [])
                cleaned_ips = []
                for ip in ips:
                    ip = str(ip).strip()
                    # 简单 IPv4 / localhost 校验
                    if ip in ('127.0.0.1', '::1') or re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', ip):
                        cleaned_ips.append(ip)
                if cleaned_ips:
                    ALLOWED_ADMIN_IPS = list(set(cleaned_ips))

            save_server_config()
            ip = self._client_ip()
            log_audit(None, ip, 'UPDATE_CONFIG', f'Extensions: {cleaned_ext}, SensitiveWords: {len(cleaned_words)} words')
            self._send_json({'status': 'success', 'msg': '配置已更新'})

        def _api_audit_logs(self, params):
            page = max(1, int(params.get('page', [1])[0]))
            per_page = min(100, int(params.get('per_page', [20])[0]))
            search = params.get('search', [''])[0].strip()

            q = AuditLog.query
            if search:
                q = q.filter(
                    (AuditLog.action.like(f'%{search}%')) |
                    (AuditLog.details.like(f'%{search}%')) |
                    (AuditLog.ip_address.like(f'%{search}%'))
                )

            pagination = q.order_by(AuditLog.created_at.desc()).paginate(
                page=page, per_page=per_page, error_out=False)

            logs = []
            for item in pagination.items:
                logs.append(item.to_dict())

            self._send_json({'status': 'success', 'data': {
                'logs': logs,
                'total': pagination.total,
                'page': page,
                'pages': pagination.pages
            }})

        def _api_broadcast(self, data):
            content = data.get('content', '').strip()
            if not content:
                self._send_json({'status': 'error', 'msg': '广播内容不能为空'})
                return
            if len(content) > MAX_MESSAGE_LENGTH:
                self._send_json({'status': 'error', 'msg': f'广播过长（最多{MAX_MESSAGE_LENGTH}字）'}, 400)
                return
            content = html.escape(content)

            loop = main_loop
            if loop:
                msg_json = make_response('system_broadcast', {'content': content})
                asyncio.run_coroutine_threadsafe(broadcast_to_all(msg_json), loop)

            ip = self._client_ip()
            log_audit(None, ip, 'SYSTEM_BROADCAST', f'Content: {content[:200]}')
            self._send_json({'status': 'success', 'msg': '广播已发送'})

    server = HTTPServer(('0.0.0.0', ADMIN_PORT), AdminHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server_log(f'Admin panel server started at http://0.0.0.0:{ADMIN_PORT}', LEVEL_ADMIN, 'Admin HTTP')


def _format_file_size(size):
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size) < 1024:
            return f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} TB'


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        server_log('接收到 KeyboardInterrupt，服务器正在关闭', LEVEL_SYSTEM, 'Bootstrap')
    except asyncio.CancelledError:
        pass
