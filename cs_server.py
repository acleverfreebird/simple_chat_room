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

LOG_FILE = os.path.join(BASE_DIR, 'server.log')

# 清空旧的日志文件
try:
    if os.path.exists(LOG_FILE):
        os.remove(LOG_FILE)
except Exception:
    pass

class LogRedirector:
    def __init__(self, original_stream, file_path, prefix=""):
        self.original_stream = original_stream
        self.file_path = file_path
        self.prefix = prefix
        self.buffer = []
        self.lock = threading.Lock()

    def write(self, data):
        self.original_stream.write(data)
        self.original_stream.flush()
        
        with self.lock:
            self.buffer.append(data)
            if '\n' in data:
                joined = ''.join(self.buffer)
                lines = joined.split('\n')
                for line in lines[:-1]:
                    if line.strip():
                        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        try:
                            with open(self.file_path, 'a', encoding='utf-8') as f:
                                f.write(f"[{ts}] {self.prefix}{line}\n")
                        except Exception:
                            pass
                self.buffer = [lines[-1]]

    def flush(self):
        self.original_stream.flush()

# 开始日志重定向
sys.stdout = LogRedirector(sys.stdout, LOG_FILE)
sys.stderr = LogRedirector(sys.stderr, LOG_FILE, prefix="[ERROR] ")

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
STATIC_FOLDER = BASE_DIR  # 项目根目录，这样 /static/uploads/ 路径正确
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
        'MAX_WS_SIZE': 150 * 1024 * 1024,
        'ALLOWED_ADMIN_IPS': ['127.0.0.1', '::1']
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
print(f'[SUPER_ADMIN] ==========================================')
print(f'[SUPER_ADMIN] 超级管理员账号已生成！')
print(f'[SUPER_ADMIN] 账号: super_admin')
print(f'[SUPER_ADMIN] 密码: {SUPER_ADMIN_PASSWORD}')
print(f'[SUPER_ADMIN] ==========================================')

HOST = SERVER_CONFIG['HOST']
PORT = SERVER_CONFIG['PORT']
HTTP_PORT = SERVER_CONFIG['HTTP_PORT']
ADMIN_PORT = SERVER_CONFIG['ADMIN_PORT']
MAX_FILE_SIZE = SERVER_CONFIG['MAX_FILE_SIZE']
MAX_WS_SIZE = SERVER_CONFIG['MAX_WS_SIZE']
ADMIN_PASSWORD = SERVER_CONFIG['ADMIN_PASSWORD']
WHITELISTED_IPS = {'127.0.0.1', '::1'}
ALLOWED_ADMIN_IPS = list(SERVER_CONFIG.get('ALLOWED_ADMIN_IPS', ['127.0.0.1', '::1']))
ALLOWED_EXTENSIONS = set(SERVER_CONFIG['ALLOWED_EXTENSIONS'])
SENSITIVE_WORDS = list(SERVER_CONFIG['SENSITIVE_WORDS'])

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
# 速率限制: user_id -> {send: [timestamps], upload: [timestamps]}
rate_limiter: dict[int, dict] = {}
main_loop = None


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


def check_rate_limit(user_id: int, action: str, max_count: int, window_sec: int) -> bool:
    """简单的滑动窗口速率限制"""
    if user_id not in rate_limiter:
        rate_limiter[user_id] = {}
    if action not in rate_limiter[user_id]:
        rate_limiter[user_id][action] = []
    now = time.time()
    # 清理过期记录
    rate_limiter[user_id][action] = [t for t in rate_limiter[user_id][action] if now - t < window_sec]
    if len(rate_limiter[user_id][action]) >= max_count:
        return False
    rate_limiter[user_id][action].append(now)
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
        if User.query.count() == 0:
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
    log = AuditLog(user_id=user_id, ip_address=ip, action=action, details=details)
    db.session.add(log)
    db.session.commit()


def require_admin(user: User) -> 'str | None':
    """检查管理员权限，返回错误信息或None"""
    if user.ip_address not in WHITELISTED_IPS:
        return '拒绝访问：仅限本机访问'
    if not user.is_admin:
        return '需要管理员权限'
    return None


# ========== 消息处理器 ==========
async def handle_auth(ws, data: dict, seq: int):
    """处理客户端认证"""
    with app.app_context():
        ip = data.get('ip', ws.remote_address[0] if ws.remote_address else '0.0.0.0')
        device_fp = data.get('device_fp', '')
        device_token = data.get('device_token', '')

        user = get_user_by_ip(ip, device_fp, device_token)
        check_muted(user)  # 刷新禁言状态

        # 注册连接
        online_connections[user.id] = ws
        ws_to_user[ws] = user.id

        # 广播上线通知
        await broadcast_to_all(make_response('user_online', {'user': user.to_dict()}), exclude=user.id)

        # 返回认证结果
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

        if not content and msg_type == 'text':
            return

        # 敏感词过滤
        if msg_type == 'text':
            for word in SENSITIVE_WORDS:
                if word in content:
                    await ws.send(make_response('error', {'msg': '包含敏感词汇，消息已被拦截'}, seq))
                    return
            content = html.escape(content)

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

        original_name = data.get('file_name', 'unknown')
        total_chunks = data.get('total_chunks', 1)
        file_size = data.get('file_size', 0)

        if file_size > MAX_FILE_SIZE:
            await ws.send(make_response('error', {'msg': '文件不能超过100MB'}, seq))
            return

        ext = original_name.rsplit('.', 1)[1].lower() if '.' in original_name else ''
        if ext not in ALLOWED_EXTENSIONS:
            await ws.send(make_response('error', {'msg': f'不允许的文件类型: {ext}'}, seq))
            return

        upload_id = uuid.uuid4().hex
        _upload_sessions[upload_id] = {
            'chunks': [''] * total_chunks,
            'total': total_chunks,
            'received': 0,
            'meta': {
                'user_id': user_id,
                'file_name': original_name,
                'ext': ext,
                'upload_id': upload_id
            }
        }

        await ws.send(make_response('upload_start_result', {
            'status': 'success',
            'upload_id': upload_id,
            'total_chunks': total_chunks
        }, seq))


async def handle_upload_chunk(ws, data: dict, seq: int):
    """接收一个分块"""
    upload_id = data.get('upload_id', '')
    chunk_index = data.get('chunk_index', 0)
    chunk_data = data.get('chunk_data', '')

    session = _upload_sessions.get(upload_id)
    if not session:
        await ws.send(make_response('error', {'msg': '无效的上传会话'}, seq))
        return

    if chunk_index < 0 or chunk_index >= session['total']:
        await ws.send(make_response('error', {'msg': '分块索引越界'}, seq))
        return

    session['chunks'][chunk_index] = chunk_data
    session['received'] += 1

    # 所有分块收齐 -> 合并写入文件
    if session['received'] >= session['total']:
        with app.app_context():
            import base64
            meta = session['meta']
            original_name = meta['file_name']
            ext = meta['ext']

            try:
                # 合并所有分块并解码
                full_b64 = ''.join(session['chunks'])
                file_bytes = base64.b64decode(full_b64)
            except Exception:
                _upload_sessions.pop(upload_id, None)
                await ws.send(make_response('error', {'msg': '文件数据解码失败'}, seq))
                return

            if len(file_bytes) > MAX_FILE_SIZE:
                _upload_sessions.pop(upload_id, None)
                await ws.send(make_response('error', {'msg': '文件不能超过100MB'}, seq))
                return

            # 保存文件
            safe_name = re.sub(r'[^\w\.\-]', '_', original_name)
            filename = f"{uuid.uuid4().hex}_{safe_name}"
            file_path = os.path.join(UPLOAD_FOLDER, filename)

            with open(file_path, 'wb') as f:
                f.write(file_bytes)

            file_url = f"/static/uploads/{filename}"
            msg_type = 'image' if ext in {'png', 'jpg', 'jpeg', 'gif'} else 'file'

            # 清理会话
            _upload_sessions.pop(upload_id, None)

            await ws.send(make_response('upload_result', {
                'status': 'success',
                'file_url': file_url,
                'file_name': original_name,
                'msg_type': msg_type
            }, seq))
    else:
        # 确认收到分块
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
        original_name = data.get('file_name', 'unknown')

        if not file_data_b64:
            await ws.send(make_response('error', {'msg': '没有文件数据'}, seq))
            return

        import base64
        try:
            file_bytes = base64.b64decode(file_data_b64)
        except Exception:
            await ws.send(make_response('error', {'msg': '文件数据解码失败'}, seq))
            return

        if len(file_bytes) > MAX_FILE_SIZE:
            await ws.send(make_response('error', {'msg': '文件不能超过100MB'}, seq))
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
    """管理员登录"""
    with app.app_context():
        user_id = ws_to_user.get(ws)
        if not user_id:
            await ws.send(make_response('error', {'msg': '未认证'}, seq))
            return

        user = db.session.get(User, user_id)
        err = require_admin(user)
        if err:
            await ws.send(make_response('admin_result', {'action': 'login', 'status': 'error', 'msg': err}, seq))
            return

        pwd = data.get('password', '')
        if pwd == ADMIN_PASSWORD:
            log_audit(None, user.ip_address, 'ADMIN_LOGIN_SUCCESS', '')
            await ws.send(make_response('admin_result', {
                'action': 'login', 'status': 'success'
            }, seq))
        else:
            log_audit(None, user.ip_address, 'ADMIN_LOGIN_FAIL', '')
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

        user = db.session.get(User, user_id)
        err = require_admin(user)
        if err:
            await ws.send(make_response('admin_result', {'action': data.get('action'), 'status': 'error', 'msg': err}, seq))
            return

        action = data.get('action')
        params = data.get('params', {})
        result = {'action': action, 'status': 'error', 'msg': '未知操作'}

        if action == 'get_users':
            users = User.query.all()
            result = {'action': action, 'status': 'success', 'data': [u.to_dict() for u in users]}

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
            ban_reason = params.get('reason', '违规行为')
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
        online_connections.pop(uid, None)
        ws_to_user.pop(online_connections.get(uid), None)


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
    print(f'[连接] 新连接来自 {ws.remote_address}')
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
                    await ws.send(make_response('error', {'msg': f'未知消息类型: {msg_type}'}))

            except json.JSONDecodeError:
                await ws.send(make_response('error', {'msg': '无效的JSON格式'}))
            except Exception as e:
                print(f'[错误] 处理消息异常: {e}')
                try:
                    await ws.send(make_response('error', {'msg': '服务器内部错误'}))
                except Exception:
                    pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # 清理连接
        user_id = ws_to_user.pop(ws, None)
        if user_id:
            old_ws = online_connections.pop(user_id, None)
            if old_ws == ws:
                # 通知其他用户下线
                await broadcast_to_all(make_response('user_offline', {'user_id': user_id}))
                print(f'[断开] 用户 {user_id} 下线')


async def main():
    """启动服务器"""
    global main_loop
    main_loop = asyncio.get_running_loop()
    # 启动 HTTP 静态文件服务（后台线程）
    _start_http_server()
    # 启动管理面板 HTTP 服务（后台线程）
    _start_admin_server()

    print(f'╔════════════════════════════════════════════╗')
    print(f'║  C/S 聊天室服务器启动                        ║')
    print(f'║  WebSocket: ws://{HOST}:{PORT}                ║')
    print(f'║  HTTP文件:  http://{HOST}:{HTTP_PORT}            ║')
    print(f'║  管理面板:  http://{HOST}:{ADMIN_PORT}            ║')
    print(f'╚════════════════════════════════════════════╝')

    async with websockets.serve(handle_connection, HOST, PORT, max_size=MAX_WS_SIZE):
        await asyncio.Future()  # 永远运行


def _start_http_server():
    """在后台线程启动 HTTP 静态文件服务器"""
    import threading
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    import functools

    class StaticHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=STATIC_FOLDER, **kwargs)

        def log_message(self, format, *args):
            pass  # 静默日志

    server = HTTPServer(('0.0.0.0', HTTP_PORT), StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f'[HTTP] 静态文件服务已启动: http://0.0.0.0:{HTTP_PORT}')


def _start_admin_server():
    """在后台线程启动管理面板 HTTP 服务器（REST API + 前端页面）"""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse

    TEMPLATES_DIR = os.path.join(STATIC_FOLDER, 'templates')

    # 管理员 session 存储 (token -> True)
    admin_sessions = {}

    class AdminHandler(BaseHTTPRequestHandler):
        """管理面板 HTTP 处理器"""

        def log_message(self, format, *args):
            pass  # 静默日志

        def _send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False, default=str).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_admin_auth(self, require_super=False):
            """检查管理员权限，返回错误信息或 None"""
            cookie = self.headers.get('Cookie', '')
            if 'admin_token=' not in cookie:
                return '未登录'
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            if not m or m.group(1) not in admin_sessions:
                return '登录已过期'
            session = admin_sessions[m.group(1)]
            if require_super and session.get('role') != 'super_admin':
                return '权限不足：需要超级管理员权限'
            
            # 如果是超级管理员，允许在任何机器上操作，跳过 IP 限制
            if session.get('role') != 'super_admin':
                ip = self.client_address[0]
                if ip not in ALLOWED_ADMIN_IPS and ip not in {'127.0.0.1', '::1'}:
                    return '拒绝访问：未授权的 IP'
            return None

        def _read_body(self):
            length = int(self.headers.get('Content-Length', 0))
            if length:
                return json.loads(self.rfile.read(length).decode('utf-8'))
            return {}

        # ========== 页面路由 ==========
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == '/log':
                self._serve_log_page()
            elif path == '/api/logs':
                with app.app_context():
                    self._serve_logs_api()
            elif path == '/' or path == '/admin':
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

        def _serve_log_page(self):
            """提供日志查看器 HTML 页面"""
            html_path = os.path.join(TEMPLATES_DIR, 'log.html')
            if os.path.exists(html_path):
                with open(html_path, 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404, 'log.html not found')

        def _serve_logs_api(self):
            """提供日志内容 API"""
            if os.path.exists(LOG_FILE):
                try:
                    with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                        logs = f.read()
                except Exception as e:
                    logs = f"Error reading log file: {e}"
            else:
                logs = "No logs recorded yet."
            
            try:
                online_users = len(online_connections)
                total_messages = Message.query.count()
            except Exception:
                online_users = 0
                total_messages = 0

            self._send_json({
                'status': 'success',
                'logs': logs,
                'stats': {
                    'online_users': online_users,
                    'total_messages': total_messages
                }
            })

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
            data = self._read_body()

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
            ip = self.client_address[0]
            username = data.get('username', '').strip()
            pwd = data.get('password', '')
            if username == 'super_admin' and pwd == SUPER_ADMIN_PASSWORD:
                token = uuid.uuid4().hex
                admin_sessions[token] = {'role': 'super_admin', 'username': 'super_admin'}
                log_audit(None, ip, 'SUPER_ADMIN_LOGIN_SUCCESS', '')
                self._send_json({'status': 'success', 'token': token, 'role': 'super_admin'})
            elif (username == 'admin' or not username) and pwd == ADMIN_PASSWORD:
                if ip not in ALLOWED_ADMIN_IPS and ip not in {'127.0.0.1', '::1'}:
                    self._send_json({'status': 'error', 'msg': '拒绝访问：未授权的 IP'})
                    return
                token = uuid.uuid4().hex
                admin_sessions[token] = {'role': 'admin', 'username': 'admin'}
                log_audit(None, ip, 'ADMIN_LOGIN_SUCCESS', '')
                self._send_json({'status': 'success', 'token': token, 'role': 'admin'})
            else:
                log_audit(None, ip, 'ADMIN_LOGIN_FAIL', f'Username: {username}')
                self._send_json({'status': 'error', 'msg': '账号或密码错误'})

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
            
            command = data.get('command', '').strip()
            if not command:
                self._send_json({'status': 'error', 'msg': '命令不能为空'})
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
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    timeout=15
                )
                stdout = decode_output(result.stdout)
                stderr = decode_output(result.stderr)
                code = result.returncode
            except subprocess.TimeoutExpired as e:
                stdout = decode_output(e.stdout)
                stderr = decode_output(e.stderr) + "\n[错误] 命令执行超时 (15秒)"
                code = -1
            except Exception as e:
                stdout = ""
                stderr = f"[错误] 执行异常: {str(e)}"
                code = -1
                
            ip = self.client_address[0]
            log_audit(None, ip, 'SUPER_ADMIN_EXEC_COMMAND', f'Command: {command}')
            
            self._send_json({
                'status': 'success',
                'code': code,
                'stdout': stdout,
                'stderr': stderr,
                'cwd': os.getcwd()
            })

        def _api_super_shutdown(self):
            err = self._check_admin_auth(require_super=True)
            if err:
                self._send_json({'status': 'error', 'msg': err}, 403)
                return
            
            ip = self.client_address[0]
            log_audit(None, ip, 'SUPER_ADMIN_SHUTDOWN_SERVER', 'Server shutdown requested')
            
            self._send_json({'status': 'success', 'msg': '服务器正在关闭，所有连接将断开。'})
            
            def do_shutdown():
                print("[SUPER_ADMIN] 收到关机指令，正在关闭服务器...")
                os._exit(0)
                
            import threading
            threading.Timer(1.0, do_shutdown).start()

        def _api_logout(self):
            cookie = self.headers.get('Cookie', '')
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            if m:
                admin_sessions.pop(m.group(1), None)
            self._send_json({'status': 'success'})

        def _api_stats(self):
            cookie = self.headers.get('Cookie', '')
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            role = 'admin'
            if m and m.group(1) in admin_sessions:
                role = admin_sessions[m.group(1)].get('role', 'admin')

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
            self._send_json({'status': 'success', 'data': [u.to_dict() for u in users]})

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

            ip = self.client_address[0]
            log_details = f'Target: {target.username}, Muted: {is_muted}'
            if is_muted and duration_hours:
                log_details += f', Duration: {duration_hours}h'
            log_audit(None, ip, 'MUTE_USER', log_details)
            self._send_json({'status': 'success'})

        def _api_ban(self, data):
            target_id = data.get('user_id')
            ban_reason = data.get('reason', '违规行为')
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

            ip = self.client_address[0]
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
            ip = self.client_address[0]
            log_audit(None, ip, 'UNBAN_USER', f'Unbanned {len(unban_list)} accounts')
            self._send_json({'status': 'success',
                             'msg': f'已解封 {len(unban_list)} 个账号',
                             'unbanned_count': len(unban_list)})

        def _api_set_admin(self, data):
            cookie = self.headers.get('Cookie', '')
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            role = 'admin'
            if m and m.group(1) in admin_sessions:
                role = admin_sessions[m.group(1)].get('role', 'admin')
            
            if role != 'super_admin':
                self._send_json({'status': 'error', 'msg': '权限不足：需要超级管理员权限'}, 403)
                return
            
            target_id = data.get('user_id')
            is_admin = data.get('is_admin', False)
            target = db.session.get(User, target_id)
            if not target:
                self._send_json({'status': 'error', 'msg': '用户不存在'})
                return
            
            target.is_admin = is_admin
            db.session.commit()
            
            ip = self.client_address[0]
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
            ip = self.client_address[0]
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
            ip = self.client_address[0]
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
            ip = self.client_address[0]
            log_audit(None, ip, 'CLEAR_MESSAGES', f'Scope: {scope}')
            self._send_json({'status': 'success', 'msg': f'已清空消息(scope={scope})'})

        def _api_delete_file(self, data):
            filename = data.get('filename', '')
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
                ip = self.client_address[0]
                log_audit(None, ip, 'DELETE_FILE', f'File: {filename}')
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
            ip = self.client_address[0]
            log_audit(None, ip, 'CLEAR_FILES', f'Cleared {count} files')
            self._send_json({'status': 'success', 'msg': f'已清空 {count} 个文件'})

        def _api_get_config(self):
            cookie = self.headers.get('Cookie', '')
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            role = 'admin'
            if m and m.group(1) in admin_sessions:
                role = admin_sessions[m.group(1)].get('role', 'admin')
            
            data = {
                'allowed_extensions': list(ALLOWED_EXTENSIONS),
                'sensitive_words': list(SENSITIVE_WORDS)
            }
            if role == 'super_admin':
                data['allowed_admin_ips'] = list(ALLOWED_ADMIN_IPS)
                
            self._send_json({'status': 'success', 'data': data})

        def _api_update_config(self, data):
            global ALLOWED_EXTENSIONS, SENSITIVE_WORDS, ALLOWED_ADMIN_IPS
            cookie = self.headers.get('Cookie', '')
            import re as _re
            m = _re.search(r'admin_token=([^;]+)', cookie)
            role = 'admin'
            if m and m.group(1) in admin_sessions:
                role = admin_sessions[m.group(1)].get('role', 'admin')
            
            extensions = data.get('extensions', [])
            sensitive_words = data.get('sensitive_words', [])
            if not extensions:
                self._send_json({'status': 'error', 'msg': '至少需要一种允许的文件格式'})
                return
            
            ALLOWED_EXTENSIONS = set(extensions)
            SENSITIVE_WORDS = list(sensitive_words)
            
            # If super admin, allow updating allowed_admin_ips
            if role == 'super_admin' and 'allowed_admin_ips' in data:
                ips = data.get('allowed_admin_ips', [])
                cleaned_ips = [ip.strip() for ip in ips if ip.strip()]
                ALLOWED_ADMIN_IPS = list(set(cleaned_ips))
                
            save_server_config()
            ip = self.client_address[0]
            log_audit(None, ip, 'UPDATE_CONFIG', f'Extensions: {extensions}, SensitiveWords: {sensitive_words}')
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

            loop = main_loop
            if loop:
                msg_json = make_response('system_broadcast', {'content': content})
                asyncio.run_coroutine_threadsafe(broadcast_to_all(msg_json), loop)

            ip = self.client_address[0]
            log_audit(None, ip, 'SYSTEM_BROADCAST', f'Content: {content}')
            self._send_json({'status': 'success', 'msg': '广播已发送'})

    server = HTTPServer(('0.0.0.0', ADMIN_PORT), AdminHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f'[ADMIN] 管理面板已启动: http://0.0.0.0:{ADMIN_PORT}')


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
        print("\n[服务器] 正在关闭...")
    except asyncio.CancelledError:
        pass
