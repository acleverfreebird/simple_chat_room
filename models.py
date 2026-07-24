from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
from flask_bcrypt import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(50), unique=True, nullable=False)
    username = db.Column(db.String(50), nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    # Admin roles: super_admin, moderator, observer
    admin_role = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(128), nullable=True)
    is_muted = db.Column(db.Boolean, default=False)
    mute_until = db.Column(db.DateTime, nullable=True)

    # === IP Ban & Anti-Evasion ===
    is_banned = db.Column(db.Boolean, default=False)  # Account-level ban
    ban_reason = db.Column(db.String(200), nullable=True)
    ban_until = db.Column(db.DateTime, nullable=True)  # null=permanent
    banned_at = db.Column(db.DateTime, nullable=True)

    # Device fingerprint (canvas/font/UA hash) - persists across IP changes
    device_fingerprint = db.Column(db.String(64), nullable=True, index=True)

    # Persistent device token (from client local file) - primary anti-alt identifier
    device_token = db.Column(db.String(64), nullable=True, index=True)

    # Historical IPs for this user account
    ip_history = db.Column(db.Text, default='[]')  # JSON array of {ip, seen_at}

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_active = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Anti-spam for name changes
    last_name_change = db.Column(db.DateTime, nullable=True)

    # Preferences
    theme_preference = db.Column(db.String(10), default='light')
    avatar = db.Column(db.String(50), default='avatar_1')

    messages_sent = db.relationship('Message', foreign_keys='Message.sender_id', backref='sender', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self, for_admin=False):
        """序列化用户信息。

        for_admin=False（默认）：面向普通客户端，不暴露 device_token / 设备指纹等可接管账号的字段。
        for_admin=True：管理后台用，可包含 IP 与设备标识摘要（token 仅前 8 位，防全量泄露）。
        """
        data = {
            'id': self.id,
            'username': self.username,
            'is_admin': bool(self.is_admin),
            'admin_role': self.admin_role,
            'is_muted': self.is_muted,
            'is_banned': self.is_banned,
            'ban_reason': self.ban_reason,
            'ban_until': self.ban_until.strftime('%Y-%m-%d %H:%M:%S') if self.ban_until else None,
            'banned_at': self.banned_at.strftime('%Y-%m-%d %H:%M:%S') if self.banned_at else None,
            'mute_until': self.mute_until.strftime('%Y-%m-%d %H:%M:%S') if self.mute_until else None,
            'last_name_change': self.last_name_change.strftime('%Y-%m-%d %H:%M:%S') if self.last_name_change else None,
            'theme_preference': self.theme_preference,
            'avatar': self.avatar or 'avatar_1',
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_active': self.last_active.strftime('%Y-%m-%d %H:%M:%S')
        }
        if for_admin:
            data['ip_address'] = self.ip_address
            data['device_fingerprint'] = self.device_fingerprint
            # 仅返回 token 前缀，管理端可关联同设备，但无法用于直接冒充登录
            data['device_token'] = (self.device_token[:8] + '…') if self.device_token else None
            data['device_token_prefix'] = self.device_token[:8] if self.device_token else None
            try:
                data['ip_history'] = self.ip_history
            except Exception:
                data['ip_history'] = '[]'
        return data

class Message(db.Model):
    __tablename__ = 'message'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    
    # receiver_id for 1v1 private chat. If null, it's a public chat message.
    receiver_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    # Message type: text, image, file
    msg_type = db.Column(db.String(20), default='text')
    content = db.Column(db.Text, nullable=True)
    file_url = db.Column(db.String(255), nullable=True)
    file_name = db.Column(db.String(255), nullable=True)
    
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_deleted = db.Column(db.Boolean, default=False)
    is_read = db.Column(db.Boolean, default=False)
    
    receiver = db.relationship('User', foreign_keys=[receiver_id])

    def to_dict(self):
        # 未设置用户名时不回落为 IP，避免把真实 IP 泄露给其他客户端
        if self.sender and self.sender.username:
            sender_name = self.sender.username
        elif self.sender:
            sender_name = f'用户{self.sender.id}'
        else:
            sender_name = '未知用户'
        return {
            'id': self.id,
            'sender_id': self.sender_id,
            'sender_name': sender_name,
            'sender_avatar': self.sender.avatar if hasattr(self.sender, 'avatar') and self.sender.avatar else 'avatar_1',
            'receiver_id': self.receiver_id,
            'msg_type': self.msg_type,
            'content': self.content if not self.is_deleted else '此消息已撤回/删除',
            'file_url': self.file_url if not self.is_deleted else None,
            'file_name': self.file_name if not self.is_deleted else None,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'is_deleted': self.is_deleted,
            'is_read': self.is_read
        }

class AuditLog(db.Model):
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    ip_address = db.Column(db.String(50), nullable=False)
    action = db.Column(db.String(100), nullable=False)
    details = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'ip_address': self.ip_address,
            'action': self.action,
            'details': self.details,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }
