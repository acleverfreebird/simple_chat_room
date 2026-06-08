"""
C/S 架构聊天室 - 客户端
基于 tkinter + websockets 的桌面客户端
现代聊天软件 UI 风格
"""
import asyncio
import json
import base64
import hashlib
import os
import sys
import platform
import socket
import uuid
import threading
import time
from collections import deque

import websockets

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

import urllib.request
import io

# ========== 配置 ==========
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_CONFIG_FILE = os.path.join(BASE_DIR, 'client_config.json')

def load_client_config():
    default_config = {
        'server_url': 'ws://192.168.0.12:9000',
        'http_server': 'http://192.168.0.12:8080'
    }
    if not os.path.exists(CLIENT_CONFIG_FILE):
        try:
            with open(CLIENT_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=4, ensure_ascii=False)
        except Exception:
            pass
        return default_config
    else:
        try:
            with open(CLIENT_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            updated = False
            for k, v in default_config.items():
                if k not in config:
                    config[k] = v
                    updated = True
            if updated:
                with open(CLIENT_CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
            return config
        except Exception:
            return default_config

def save_client_config(server_url, http_server):
    config = {
        'server_url': server_url,
        'http_server': http_server
    }
    try:
        with open(CLIENT_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

DEFAULT_SERVER = 'ws://192.168.0.12:9000'
DEFAULT_HTTP_SERVER = 'http://192.168.0.12:8080'
HEARTBEAT_INTERVAL = 30
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 30
MAX_WS_SIZE = 150 * 1024 * 1024

# ========== 字体常量 ==========
FONT_MAIN = ('Microsoft YaHei', 10)
FONT_BOLD = ('Microsoft YaHei', 10, 'bold')
FONT_TITLE = ('Microsoft YaHei', 14, 'bold')
FONT_SMALL = ('Microsoft YaHei', 9)
FONT_BADGE = ('Microsoft YaHei', 8, 'bold')

# ========== 主题色 ==========
COLORS = {
    'nav_bg': '#2e2e2e',           # 微信最左侧导航栏背景（深黑/灰）
    'list_bg': '#f7f7f7',          # 微信会话列表背景（浅灰）
    'list_hover': '#ebebeb',       # 会话列表悬停背景
    'list_active': '#e2e2e2',      # 会话列表选中背景
    'bg_chat': '#ededed',          # 微信聊天窗口背景
    'bg_input': '#f5f5f5',         # 微信输入区域背景
    'text_input_bg': '#ffffff',    # 输入框背景 (纯白)
    'green': '#07C160',            # 微信绿
    'green_dark': '#06AD56',       # 深绿
    'green_light': '#95EC69',      # 亮绿
    'bubble_self': '#95EC69',      # 自己气泡背景 (微信亮绿)
    'bubble_other': '#FFFFFF',     # 他人气泡背景 (纯白)
    'text_primary': '#1A1A1A',     # 主文字
    'text_secondary': '#8A8A8A',   # 次文字
    'text_light': '#BBBBBB',       # 浅色文字
    'text_white': '#DFE1E5',       # 白色文字
    'border': '#e5e5e5',           # 边框色
    'online_green': '#23C343',     # 在线状态绿
    'unread_badge': '#FF3B30',     # 未读红点
    'system_bg': '#dadada',        # 系统消息背景
    'system_text': '#ffffff',      # 系统消息文字（微信是白字在灰底上）
    
    # 兼容性映射
    'bg_dark': '#2E2E2E',
    'bg_darker': '#242424',
    'bg_chat': '#ededed',
    'bg_input': '#f5f5f5',
    'hover': '#ebebeb',
    'selected': '#e2e2e2',
}

PRESET_AVATARS = {
    'avatar_1': {'symbol': '猫', 'bg': ('#4F46E5', '#7C3AED')}, # Cat
    'avatar_2': {'symbol': '狗', 'bg': ('#F59E0B', '#EF4444')}, # Dog
    'avatar_3': {'symbol': '兔', 'bg': ('#EC4899', '#F43F5E')}, # Rabbit
    'avatar_4': {'symbol': '狐', 'bg': ('#F97316', '#F59E0B')}, # Fox
    'avatar_5': {'symbol': '熊', 'bg': ('#3B82F6', '#60A5FA')}, # Bear
    'avatar_6': {'symbol': '蛙', 'bg': ('#EAB308', '#F59E0B')}, # Frog
    'avatar_7': {'symbol': '鱼', 'bg': ('#10B981', '#34D399')}, # Fish
    'avatar_8': {'symbol': '鸟', 'bg': ('#06B6D4', '#22D3EE')}, # Bird
    'avatar_9': {'symbol': '鸭', 'bg': ('#1E293B', '#475569')}, # Duck
    'avatar_10': {'symbol': '猴', 'bg': ('#0284C7', '#38BDF8')}, # Monkey
    'avatar_11': {'symbol': '猪', 'bg': ('#8B5CF6', '#A78BFA')}, # Pig
    'avatar_12': {'symbol': '羊', 'bg': ('#D946EF', '#F472B6')}, # Sheep
}

def draw_gradient_round_rect(size: int, bg_colors: tuple, radius: int) -> 'Image':
    # Create the gradient background
    c1_rgb = tuple(int(bg_colors[0][i:i+2], 16) for i in (1, 3, 5))
    c2_rgb = tuple(int(bg_colors[1][i:i+2], 16) for i in (1, 3, 5))
    c1 = Image.new('RGBA', (size, size), c1_rgb + (255,))
    c2 = Image.new('RGBA', (size, size), c2_rgb + (255,))
    
    # We can mask c2 on top of c1 with a diagonal gradient mask
    mask = Image.new('L', (size, size))
    for y in range(size):
        for x in range(size):
            factor = int(((x + y) / (2 * size)) * 255)
            mask.putpixel((x, y), factor)
    
    gradient_img = Image.composite(c2, c1, mask)
    
    # Create the rounded rectangle mask
    mask_rr = Image.new('L', (size, size), 0)
    draw_rr = ImageDraw.Draw(mask_rr)
    draw_rr.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    
    # Apply rounded rectangle mask
    final_img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    final_img.paste(gradient_img, (0, 0), mask=mask_rr)
    return final_img

def make_preset_avatar(avatar_id: str, size: int = 40) -> 'ImageTk.PhotoImage':
    if not HAS_PIL:
        return None
    preset = PRESET_AVATARS.get(avatar_id)
    if not preset:
        if avatar_id in PRESET_AVATARS:
            preset = PRESET_AVATARS[avatar_id]
        else:
            preset = PRESET_AVATARS['avatar_1']
            
    symbol = preset['symbol']
    bg_colors = preset['bg']
    
    img = draw_gradient_round_rect(size, bg_colors, radius=size // 6)
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype('msyh.ttc', int(size * 0.55))
    except Exception:
        try:
            font = ImageFont.truetype('arial.ttf', int(size * 0.55))
        except Exception:
            font = ImageFont.load_default()
            
    bbox = draw.textbbox((0, 0), symbol, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), symbol, fill='white', font=font)
    return ImageTk.PhotoImage(img)



def generate_device_fingerprint() -> str:
    """生成多维度设备指纹（用于辅助识别，不持久化）"""
    parts = [
        platform.node(), platform.machine(), platform.processor(),
        str(os.cpu_count()), platform.system(), platform.release(),
        str(uuid.getnode()), socket.gethostname(),
    ]
    return hashlib.sha256('|||'.join(parts).encode()).hexdigest()[:64]


def get_device_token_path() -> str:
    """获取本地标识文件路径（用户数据目录下）"""
    if platform.system() == 'Windows':
        base = os.environ.get('APPDATA', os.path.expanduser('~'))
    elif platform.system() == 'Darwin':
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
    token_dir = os.path.join(base, 'ChatRoom')
    os.makedirs(token_dir, exist_ok=True)
    return os.path.join(token_dir, 'device.token')


def load_or_create_device_token() -> str:
    """加载或创建本地持久化设备令牌（防换IP小号）"""
    token_path = get_device_token_path()
    # 尝试读取已有 token
    if os.path.exists(token_path):
        try:
            with open(token_path, 'r') as f:
                token = f.read().strip()
            if token and len(token) >= 32:
                return token
        except Exception:
            pass

    # 生成新 token: 随机UUID + 设备信息哈希 + 时间戳
    raw = f"{uuid.uuid4().hex}||{generate_device_fingerprint()}||{time.time()}"
    token = hashlib.sha256(raw.encode()).hexdigest()

    # 写入文件（尝试设置隐藏属性）
    try:
        with open(token_path, 'w') as f:
            f.write(token)
        # Windows 下设置隐藏属性
        if platform.system() == 'Windows':
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(token_path, 0x02)
            except Exception:
                pass
    except Exception:
        pass

    return token


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _make_avatar(letter: str, size: int = 40, bg_color: str = None) -> 'ImageTk.PhotoImage':
    """生成字母头像"""
    if not HAS_PIL:
        return None
    # 根据名字生成颜色
    if bg_color is None:
        palette = ['#07c160', '#fa9d3b', '#576b95', '#f55f4e', '#10aeff',
                    '#c9a84c', '#8b5cf6', '#ec4899', '#14b8a6', '#f97316']
        idx = hash(letter) % len(palette)
        bg_color = palette[idx]

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # 圆角矩形背景
    radius = size // 6
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=bg_color)
    # 居中文字
    ch = letter[0].upper() if letter else '?'
    try:
        font = ImageFont.truetype('msyh.ttc', size // 2)
    except Exception:
        try:
            font = ImageFont.truetype('arial.ttf', size // 2)
        except Exception:
            font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), ch, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - tw) // 2 - bbox[0]
    y = (size - th) // 2 - bbox[1]
    draw.text((x, y), ch, fill='white', font=font)
    return ImageTk.PhotoImage(img)


# ========== 异步网络客户端 ==========
class NetworkClient:
    def __init__(self, root, on_message_cb, on_disconnect_cb, on_connect_cb):
        self.root = root
        self.on_message_cb = on_message_cb
        self.on_disconnect_cb = on_disconnect_cb
        self.on_connect_cb = on_connect_cb
        self.connected = False
        self.authenticated = False
        self._seq = 0
        self._loop = None
        self._thread = None
        self.server_url = DEFAULT_SERVER
        self._pending = deque(maxlen=200)
        self._stop_event = threading.Event()
        self.device_fp = ''
        self.local_ip = ''
        self.ws = None
        self._reconnect_trigger = None

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def start(self, device_fp: str, device_token: str, local_ip: str):
        self.device_fp = device_fp
        self.device_token = device_token
        self.local_ip = local_ip
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._do_disconnect(), self._loop)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._reconnect_trigger = asyncio.Event()
        self._loop.run_until_complete(self._lifecycle())

    async def _lifecycle(self):
        delay = RECONNECT_BASE_DELAY
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    self.server_url, ping_interval=None,
                    close_timeout=5, max_size=MAX_WS_SIZE
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    delay = RECONNECT_BASE_DELAY
                    self._schedule_ui(self.on_connect_cb)
                    auth_msg = json.dumps({
                        'type': 'auth',
                        'data': {
                            'ip': self.local_ip,
                            'device_fp': self.device_fp,
                            'device_token': self.device_token,
                        },
                        'seq': self.next_seq()
                    }, ensure_ascii=False)
                    await ws.send(auth_msg)
                    while self._pending:
                        try:
                            old_msg = self._pending.popleft()
                            await ws.send(old_msg)
                        except Exception:
                            break
                    recv_task = asyncio.create_task(self._receive_loop(ws))
                    send_task = asyncio.create_task(self._send_loop(ws))
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
                    done, pending = await asyncio.wait(
                        [recv_task, send_task, heartbeat_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
            except (websockets.exceptions.ConnectionClosed, ConnectionError, OSError):
                pass
            except Exception:
                pass
            finally:
                self.ws = None
                self.connected = False
                self._schedule_ui(self.on_disconnect_cb)
            if not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._reconnect_trigger.wait(), timeout=delay)
                    delay = RECONNECT_BASE_DELAY
                except asyncio.TimeoutError:
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
                self._reconnect_trigger.clear()

    def reconnect_with_new_url(self, new_url):
        self.server_url = new_url
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._do_reconnect(), self._loop)

    async def _do_reconnect(self):
        if hasattr(self, 'ws') and self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        if hasattr(self, '_reconnect_trigger') and self._reconnect_trigger:
            self._reconnect_trigger.set()

    async def _receive_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    self._schedule_ui(self.on_message_cb, msg)
                except json.JSONDecodeError:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            pass

    async def _send_loop(self, ws):
        while True:
            await asyncio.sleep(0.05)
            while self._pending:
                try:
                    msg = self._pending.popleft()
                    await ws.send(msg)
                except Exception:
                    self._pending.appendleft(msg)
                    return

    async def _heartbeat_loop(self, ws):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.connected:
                try:
                    await ws.send(json.dumps(
                        {'type': 'ping', 'data': {}, 'seq': self.next_seq()},
                        ensure_ascii=False
                    ))
                except Exception:
                    return

    async def _do_disconnect(self):
        self.connected = False
        self._stop_event.set()

    def send(self, msg_type: str, data: dict):
        msg = json.dumps({'type': msg_type, 'data': data, 'seq': self.next_seq()}, ensure_ascii=False)
        self._pending.append(msg)

    def _schedule_ui(self, callback, *args):
        if callback and self.root:
            try:
                self.root.after(0, callback, *args)
            except Exception:
                pass


# ========== 可滚动的 Frame ==========
class ScrollableFrame(tk.Frame):
    """可垂直滚动的 Frame（用于消息区域）"""
    def __init__(self, parent, **kwargs):
        bg = kwargs.pop('bg', COLORS['bg_chat'])
        on_scroll_top = kwargs.pop('on_scroll_top', None)  # 滚动到顶部回调
        super().__init__(parent, bg=bg, **kwargs)

        self._on_scroll_top_cb = on_scroll_top
        self._was_at_top = False

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.v_scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)

        self.scrollable_frame = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor='nw')
        self.canvas.configure(yscrollcommand=self.v_scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.v_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 宽度自适应：canvas 大小变化时，同步内部 frame 宽度
        self.canvas.bind('<Configure>', self._on_canvas_configure)

        # scrollregion 自动更新：内部 frame 内容变化时
        self.scrollable_frame.bind('<Configure>', self._on_frame_configure)

        # 鼠标滚轮
        self.canvas.bind('<Enter>', lambda e: self._bind_mousewheel())
        self.canvas.bind('<Leave>', lambda e: self._unbind_mousewheel())

    def _on_canvas_configure(self, event):
        """Canvas 大小变化时，让内部 frame 宽度跟随"""
        self.canvas.itemconfig(self._window, width=event.width)

    def _on_frame_configure(self, event):
        """内部 frame 内容变化时，更新 scrollregion"""
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _bind_mousewheel(self):
        self.canvas.bind_all('<MouseWheel>', self._on_mousewheel)
        self.canvas.bind_all('<Button-4>', self._on_mousewheel_linux)
        self.canvas.bind_all('<Button-5>', self._on_mousewheel_linux)

    def _unbind_mousewheel(self):
        self.canvas.unbind_all('<MouseWheel>')
        self.canvas.unbind_all('<Button-4>')
        self.canvas.unbind_all('<Button-5>')

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        self.after(50, self._check_scroll_top)

    def _on_mousewheel_linux(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-1, 'units')
        elif event.num == 5:
            self.canvas.yview_scroll(1, 'units')
        self.after(50, self._check_scroll_top)

    def _check_scroll_top(self):
        """检查是否滚动到顶部，触发回调"""
        if not self._on_scroll_top_cb:
            return
        try:
            bbox = self.canvas.bbox('all')
            if not bbox:
                return
            y_view = self.canvas.yview()
            # y_view[0] 是顶部可见比例，接近 0 表示滚动到顶部
            if y_view[0] <= 0.01 and not self._was_at_top:
                self._was_at_top = True
                self._on_scroll_top_cb()
            elif y_view[0] > 0.05:
                self._was_at_top = False
        except Exception:
            pass

    def scroll_to_bottom(self):
        """可靠地滚动到底部（延迟执行确保布局完成）"""
        self.canvas.update_idletasks()
        # 延迟再次滚动，确保布局更新后再定位
        self.after(30, self._do_scroll_bottom)
        self.after(100, self._do_scroll_bottom)

    def _do_scroll_bottom(self):
        bbox = self.canvas.bbox('all')
        if bbox:
            self.canvas.configure(scrollregion=bbox)
        self.canvas.yview_moveto(1.0)

    def get_content_width(self):
        """获取内容区可用宽度（减去滚动条和内边距）"""
        cw = self.canvas.winfo_width()
        sb_width = self.v_scrollbar.winfo_width() if self.v_scrollbar.winfo_ismapped() else 0
        return max(200, cw - sb_width - 30)


# ========== 消息气泡组件 ==========
class MessageBubble(tk.Frame):
    """聊天气泡"""
    def __init__(self, parent, msg: dict, is_self: bool, **kwargs):
        bg = kwargs.pop('bg', COLORS['bg_chat'])
        super().__init__(parent, bg=bg, **kwargs)

        sender_name = msg.get('sender_name', '未知')
        msg_type = msg.get('msg_type', 'text')
        content = msg.get('content', '')
        file_url = msg.get('file_url', '')
        file_name = msg.get('file_name', '')
        created_at = msg.get('created_at', '')
        is_private = msg.get('receiver_id') is not None

        # 简化时间显示
        time_str = self._format_time(created_at)

        # 主行：头像 + 气泡
        row = tk.Frame(self, bg=bg)
        row.pack(fill=tk.X, padx=16, pady=6)

        bubble_bg = COLORS['bubble_self'] if is_self else COLORS['bubble_other']
        text_fg = COLORS['text_primary']

        if is_self:
            # 自己：气泡在右，头像在右
            spacer = tk.Frame(row, bg=bg)
            spacer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            # 头像在最右边
            avatar = self._get_avatar(sender_name, msg.get('sender_avatar'))
            avatar_label = tk.Label(row, image=avatar, bg=bg)
            avatar_label.image = avatar  # 防 GC
            avatar_label.pack(side=tk.RIGHT, anchor=tk.N)

            # 气泡在头像左边
            bubble_frame = tk.Frame(row, bg=bubble_bg, padx=12, pady=8)
            bubble_frame.pack(side=tk.RIGHT, padx=(0, 10))

            # 时间戳（在spacer右侧，紧贴气泡底部或顶部都可以，这里放左侧底部）
            time_label = tk.Label(spacer, text=time_str, bg=bg,
                                  fg=COLORS['text_secondary'], font=FONT_SMALL)
            time_label.pack(side=tk.RIGHT, anchor=tk.S, padx=(0, 6), pady=(0, 2))
        else:
            # 他人：头像在左，气泡在右
            avatar = self._get_avatar(sender_name, msg.get('sender_avatar'))
            avatar_label = tk.Label(row, image=avatar, bg=bg)
            avatar_label.image = avatar
            avatar_label.pack(side=tk.LEFT, anchor=tk.N)

            left_col = tk.Frame(row, bg=bg)
            left_col.pack(side=tk.LEFT, padx=(10, 0), fill=tk.BOTH, expand=True)

            # 名字行
            name_row = tk.Frame(left_col, bg=bg)
            name_row.pack(fill=tk.X, pady=(0, 2))
            name_label = tk.Label(name_row, text=sender_name, bg=bg,
                                  fg=COLORS['text_secondary'], font=FONT_SMALL)
            name_label.pack(side=tk.LEFT)
            if is_private:
                priv_label = tk.Label(name_row, text=" ✉ 私信", bg=bg,
                                      fg='#8b5cf6', font=FONT_SMALL)
                priv_label.pack(side=tk.LEFT)
            time_label = tk.Label(name_row, text=time_str, bg=bg,
                                  fg=COLORS['text_light'], font=FONT_SMALL)
            time_label.pack(side=tk.LEFT, padx=8)

            bubble_frame = tk.Frame(left_col, bg=bubble_bg, padx=12, pady=8)
            bubble_frame.pack(anchor=tk.W)

        # 气泡内容
        if msg_type == 'image' and file_url:
            img_label = tk.Label(bubble_frame, text="[图片加载中...]",
                                 bg=bubble_bg, fg=text_fg, font=FONT_MAIN,
                                 cursor='hand2')
            img_label.pack(anchor=tk.W)
            # 存储引用供后续替换
            self._img_label = img_label
            self._bubble_bg = bubble_bg
            self._file_url = file_url
            self._msg_id = msg.get('id', -1)
        elif msg_type == 'file' and file_url:
            file_frame = tk.Frame(bubble_frame, bg=bubble_bg)
            file_frame.pack(anchor=tk.W)
            file_icon = tk.Label(file_frame, text="📎", bg=bubble_bg,
                                  fg=COLORS['green'], font=FONT_TITLE)
            file_icon.pack(side=tk.LEFT, padx=(0, 6))
            file_label = tk.Label(file_frame, text=f"{file_name}",
                                  bg=bubble_bg, fg=COLORS['green'],
                                  font=FONT_MAIN,
                                  cursor='hand2')
            file_label.pack(side=tk.LEFT)
        else:
            display = content if content else '[消息已撤回]'
            # 动态计算 wraplength：父容器宽度 - 头像 - 边距 - 气泡内边距
            wrap_len = self._calc_wraplength(parent)
            content_label = tk.Label(bubble_frame, text=display,
                                     bg=bubble_bg, fg=text_fg,
                                     font=FONT_MAIN,
                                     wraplength=wrap_len, justify=tk.LEFT)
            content_label.pack(anchor=tk.W)

        # 气泡圆角效果（tkinter 不支持真正圆角，用边框模拟）
        bubble_frame.configure(highlightbackground=COLORS['border'],
                               highlightthickness=1,
                               relief=tk.FLAT)

    def _format_time(self, time_str: str) -> str:
        """简化时间格式"""
        if not time_str:
            return ''
        try:
            # 格式: 2026-04-25T17:53:00+00:00 -> 17:53
            if 'T' in time_str:
                t_part = time_str.split('T')[1][:5]
                return t_part
        except Exception:
            pass
        return time_str[-5:] if len(time_str) >= 5 else time_str

    def _get_avatar(self, name: str, avatar_id: str = None) -> 'ImageTk.PhotoImage':
        """获取头像"""
        if not HAS_PIL:
            # 降级：文字头像
            return None
        if avatar_id and avatar_id in PRESET_AVATARS:
            return make_preset_avatar(avatar_id, size=36)
        ch = name[0] if name else '?'
        return _make_avatar(ch, size=36)

    def _calc_wraplength(self, parent) -> int:
        """根据父容器宽度动态计算文字换行长度"""
        try:
            # 尝试从 ScrollableFrame 获取内容区宽度
            scroll_frame = parent
            while scroll_frame and not isinstance(scroll_frame, ScrollableFrame):
                scroll_frame = scroll_frame.master
            if scroll_frame and isinstance(scroll_frame, ScrollableFrame):
                available = scroll_frame.get_content_width()
            else:
                available = parent.winfo_width()
            # 减去: padx(20) + avatar(36) + avatar_pad(8) + bubble_padx(20) + margin
            return max(100, available - 20 - 36 - 8 - 20 - 30)
        except Exception:
            return 350


# ========== 系统消息组件 ==========
class SystemMessage(tk.Frame):
    """系统消息（居中灰色）"""
    def __init__(self, parent, text: str, **kwargs):
        bg = kwargs.pop('bg', COLORS['bg_chat'])
        super().__init__(parent, bg=bg, **kwargs)

        label = tk.Label(self, text=text, bg=COLORS['system_bg'],
                         fg=COLORS['system_text'], font=FONT_SMALL,
                         padx=16, pady=4, relief=tk.FLAT)
        label.pack(pady=8)


# ========== 用户列表项 ==========
class UserListItem(tk.Frame):
    """用户列表中的单个项目"""
    def __init__(self, parent, user: dict, unread: int = 0, **kwargs):
        bg = kwargs.pop('bg', COLORS['bg_dark'])
        super().__init__(parent, bg=bg, cursor='hand2', **kwargs)

        name = user.get('username', '未命名')
        avatar_id = user.get('avatar', 'avatar_1')
        is_banned = user.get('is_banned', False)
        is_muted = user.get('is_muted', False)
        is_online = user.get('is_online', False)

        # 头像
        avatar = self._get_avatar(name, avatar_id)
        if avatar:
            self.avatar_lbl = tk.Label(self, image=avatar, bg=bg)
            self.avatar_lbl.image = avatar
            self.avatar_lbl.pack(side=tk.LEFT, padx=(10, 8), pady=6)
        else:
            self.avatar_lbl = tk.Label(self, text=name[0] if name else '?',
                                  bg=COLORS['green'], fg='white',
                                  font=FONT_TITLE,
                                  width=2, height=1)
            self.avatar_lbl.pack(side=tk.LEFT, padx=(10, 8), pady=6)

        # 名字和状态
        self.info = tk.Frame(self, bg=bg)
        self.info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=6)

        name_fg = 'black' if not is_banned else '#ff6b6b'
        self.name_label = tk.Label(self.info, text=name, bg=bg, fg=name_fg,
                              font=FONT_MAIN)
        self.name_label.pack(anchor=tk.W)

        presence_text = "在线" if is_online else "离线"
        status_fg = COLORS['online_green'] if is_online else COLORS['text_secondary']
        if is_banned:
            status_text = f"{presence_text} · 已封禁"
            status_fg = '#ff6b6b'
        elif is_muted:
            status_text = f"{presence_text} · 已禁言"
            status_fg = '#ffa500'
        else:
            status_text = presence_text

        self.status_dot = tk.Label(self.info, text=f"● {status_text}", bg=bg,
                              fg=status_fg, font=FONT_SMALL)
        self.status_dot.pack(anchor=tk.W)

        # 未读徽标
        self.badge = None
        if unread > 0:
            self.badge = tk.Label(self, text=str(unread), bg=COLORS['unread_badge'],
                             fg='white', font=FONT_BADGE,
                             padx=5, pady=0)
            self.badge.pack(side=tk.RIGHT, padx=8)

        # 绑定 hover
        self.default_bg = bg
        self._bind_hover(self)

    def _set_bg(self, color):
        self.configure(bg=color)
        if hasattr(self, 'avatar_lbl') and getattr(self.avatar_lbl, 'image', None):
            self.avatar_lbl.configure(bg=color)
        self.info.configure(bg=color)
        self.name_label.configure(bg=color)
        self.status_dot.configure(bg=color)

    def _bind_hover(self, widget):
        widget.bind('<Enter>', lambda e: self._set_bg(COLORS['hover']))
        widget.bind('<Leave>', lambda e: self._set_bg(self.default_bg))
        for child in widget.winfo_children():
            self._bind_hover(child)

    def bind_click(self, callback):
        def _bind(widget):
            widget.bind('<Button-1>', callback)
            for child in widget.winfo_children():
                _bind(child)
        _bind(self)

    def _get_avatar(self, name: str, avatar_id: str = None):
        if not HAS_PIL:
            return None
        if avatar_id and avatar_id in PRESET_AVATARS:
            return make_preset_avatar(avatar_id, size=32)
        return _make_avatar(name[0] if name else '?', size=32)




# ========== 个人信息编辑对话框 ==========
class ProfileEditDialog(tk.Toplevel):
    def __init__(self, parent, current_username, current_avatar, on_save_cb):
        super().__init__(parent)
        self.title("个人信息")
        self.geometry("400x530")
        self.resizable(False, False)
        self.configure(bg='white')
        self.transient(parent)
        self.grab_set()
        
        self.on_save_cb = on_save_cb
        self.selected_avatar = current_avatar or 'avatar_1'
        
        # Center the window relative to parent
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        x = px + (pw - 400) // 2
        y = py + (ph - 530) // 2
        self.geometry(f"+{x}+{y}")
        
        # Header
        header = tk.Frame(self, bg=COLORS['green'], height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="修改个人资料", bg=COLORS['green'], fg='white', font=('Microsoft YaHei', 12, 'bold')).pack(pady=15)
        
        # Actions at bottom (packed at root level to prevent squeezing)
        actions = tk.Frame(self, bg='white', padx=24, pady=12)
        actions.pack(fill=tk.X, side=tk.BOTTOM)
        
        btn_cancel = tk.Button(actions, text="取消", bg='#F0F0F0', fg=COLORS['text_primary'], font=FONT_BOLD, relief=tk.FLAT, padx=20, pady=6, cursor='hand2', command=self.destroy)
        btn_cancel.pack(side=tk.LEFT)
        
        btn_save = tk.Button(actions, text="保存", bg=COLORS['green'], fg='white', font=FONT_BOLD, relief=tk.FLAT, padx=20, pady=6, cursor='hand2', command=self._on_save)
        btn_save.pack(side=tk.RIGHT)
        
        # Content in the middle
        content = tk.Frame(self, bg='white', padx=24, pady=16)
        content.pack(fill=tk.BOTH, expand=True)
        
        # Username edit
        tk.Label(content, text="用户名", bg='white', fg=COLORS['text_secondary'], font=FONT_SMALL).pack(anchor=tk.W)
        self.ent_username = ttk.Entry(content, font=FONT_MAIN)
        self.ent_username.pack(fill=tk.X, pady=(5, 12))
        self.ent_username.insert(0, current_username or '')
        
        # Avatar Selection
        tk.Label(content, text="选择头像", bg='white', fg=COLORS['text_secondary'], font=FONT_SMALL).pack(anchor=tk.W)
        
        # Avatar Grid
        grid_frame = tk.Frame(content, bg='white')
        grid_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        
        self.avatar_buttons = {}
        self.avatar_images = {}  # Prevent GC
        
        # 4 columns, 3 rows
        cols = 4
        for idx, (avatar_id, info) in enumerate(PRESET_AVATARS.items()):
            r = idx // cols
            c = idx % cols
            
            # Generate the avatar image
            img = make_preset_avatar(avatar_id, size=48)
            self.avatar_images[avatar_id] = img
            
            # We create a frame that acts as a button with a border
            btn_frame = tk.Frame(grid_frame, bg='white', padx=2, pady=2)
            btn_frame.grid(row=r, column=c, padx=6, pady=6)
            
            lbl = tk.Label(btn_frame, image=img, bg='white', cursor='hand2')
            lbl.pack()
            
            # Bind events
            lbl.bind('<Button-1>', lambda e, aid=avatar_id: self._select_avatar(aid))
            
            self.avatar_buttons[avatar_id] = btn_frame
            
        self._select_avatar(self.selected_avatar)
        

    def _select_avatar(self, avatar_id):
        # Deselect old
        if self.selected_avatar in self.avatar_buttons:
            self.avatar_buttons[self.selected_avatar].configure(bg='white')
        
        self.selected_avatar = avatar_id
        # Select new (highlight with WeChat green)
        if avatar_id in self.avatar_buttons:
            self.avatar_buttons[avatar_id].configure(bg=COLORS['green'])
            
    def _on_save(self):
        username = self.ent_username.get().strip()
        if not username:
            messagebox.showwarning("提示", "用户名不能为空", parent=self)
            return
        if len(username) > 20:
            messagebox.showwarning("提示", "用户名长度不能超过20个字符", parent=self)
            return
        self.on_save_cb(username, self.selected_avatar)
        self.destroy()


# ========== GUI 应用 ==========
class ChatApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ChatRoom")
        self.root.geometry("960x700")
        self.root.minsize(760, 520)
        self.root.configure(bg=COLORS['bg_dark'])

        # 尝试设置窗口图标
        self._set_window_icon()

        # 状态
        self.current_user = None
        self.current_mode = 'public'
        self.current_target = None
        self.online_users = []
        self.unread_counts = {}
        self.chat_previews = {}
        self.msg_ids_shown = set()
        self.is_banned = False
        self.is_muted = False
        self.device_fp = generate_device_fingerprint()
        self.device_token = load_or_create_device_token()
        self.local_ip = get_local_ip()
        self._pending_upload = None
        self._image_cache = {}
        self._image_downloading = set()
        
        # 加载配置
        self.client_config = load_client_config()
        self._http_server = self.client_config.get('http_server', DEFAULT_HTTP_SERVER)
        
        self._msg_bubbles = {}  # msg_id -> MessageBubble (用于图片替换)
        self._has_more_messages = False  # 是否还有更早的历史消息
        self._loading_more = False  # 是否正在加载更多消息

        # 网络
        self.network = NetworkClient(
            root=self.root,
            on_message_cb=self._on_message,
            on_disconnect_cb=self._on_disconnect,
            on_connect_cb=self._on_connect
        )
        self.network.server_url = self.client_config.get('server_url', DEFAULT_SERVER)

        self._build_gui()
        self.network.start(self.device_fp, self.device_token, self.local_ip)

        try:
            from urllib.parse import urlparse
            parsed = urlparse(self.network.server_url)
            host = parsed.hostname or '127.0.0.1'
            self._http_server = f'http://{host}:8080'
        except Exception:
            pass

    def _set_server_url_dialog(self):
        new_url = simpledialog.askstring(
            "服务器配置",
            "请输入 WebSocket 服务器地址 (如 ws://127.0.0.1:9000):",
            initialvalue=self.network.server_url,
            parent=self.root
        )
        if new_url:
            new_url = new_url.strip()
            if not (new_url.startswith('ws://') or new_url.startswith('wss://')):
                messagebox.showerror("格式错误", "服务器地址必须以 ws:// 或 wss:// 开头", parent=self.root)
                return
            
            try:
                from urllib.parse import urlparse
                parsed = urlparse(new_url)
                host = parsed.hostname or '127.0.0.1'
                new_http = f"http://{host}:8080"
            except Exception:
                new_http = DEFAULT_HTTP_SERVER
                
            self.lbl_status.configure(text="Reconnecting...", fg='#ffa500')
            self.network.reconnect_with_new_url(new_url)
            self._http_server = new_http
            save_client_config(new_url, new_http)
            self._append_system_msg(f"服务器地址已更新为: {new_url}，正在尝试连接...")

    def _set_window_icon(self):
        """设置窗口图标"""
        if HAS_PIL:
            try:
                icon = _make_avatar('C', size=64, bg_color=COLORS['green'])
                if icon:
                    self.root.iconphoto(True, icon)
            except Exception:
                pass

    # ========== 连接管理 ==========
    def _on_connect(self):
        self.lbl_status.configure(text="Connected", fg=COLORS['online_green'])
        self._append_system_msg("Connected to server")

    def _on_disconnect(self):
        self.lbl_status.configure(text="Reconnecting...", fg='#ffa500')
        self._append_system_msg("Disconnected, reconnecting...")

    def _build_gui(self):
        # 全局样式
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except Exception:
            pass

        # 自定义 ttk 样式
        style.configure('Dark.TFrame', background=COLORS['nav_bg'])
        style.configure('Chat.TFrame', background=COLORS['bg_chat'])
        style.configure('Green.TButton', background=COLORS['green'],
                         foreground='white', font=FONT_BOLD,
                         borderwidth=0, focuscolor=COLORS['green_dark'])
        style.map('Green.TButton',
                   background=[('active', COLORS['green_dark'])],
                   foreground=[('active', 'white')])
        style.configure('Dark.TButton', background=COLORS['nav_bg'],
                         foreground=COLORS['text_light'], font=FONT_SMALL,
                         borderwidth=0, padding=(8, 4))
        style.map('Dark.TButton',
                   background=[('active', COLORS['list_hover'])])

        # 主容器
        main = tk.Frame(self.root, bg=COLORS['nav_bg'])
        main.pack(fill=tk.BOTH, expand=True)

        # ===== 1. 最左侧微信导航栏 =====
        self.nav_sidebar = tk.Frame(main, bg=COLORS['nav_bg'], width=60)
        self.nav_sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.nav_sidebar.pack_propagate(False)

        # 当前用户头像
        self._my_avatar = make_preset_avatar('avatar_1', size=38)
        self.lbl_my_avatar = tk.Label(self.nav_sidebar, image=self._my_avatar,
                                       bg=COLORS['nav_bg'], cursor='hand2')
        self.lbl_my_avatar.pack(side=tk.TOP, pady=20)
        self.lbl_my_avatar.bind('<Button-1>', lambda e: self._open_profile_dialog())

        # 导航 Tab (Chat)
        self.lbl_nav_chat = tk.Label(self.nav_sidebar, text="💬", bg=COLORS['nav_bg'],
                                      fg=COLORS['green'], font=('Microsoft YaHei', 18),
                                      cursor='hand2')
        self.lbl_nav_chat.pack(side=tk.TOP, pady=10)

        # 底部设置按钮
        self.lbl_nav_settings = tk.Label(self.nav_sidebar, text="⚙",
                                          bg=COLORS['nav_bg'],
                                          fg=COLORS['text_light'], font=('Microsoft YaHei', 18),
                                          cursor='hand2')
        self.lbl_nav_settings.pack(side=tk.BOTTOM, pady=20)
        self.lbl_nav_settings.bind('<Button-1>', lambda e: self._set_server_url_dialog())
        self.lbl_nav_settings.bind('<Enter>', lambda e: self.lbl_nav_settings.configure(fg='white'))
        self.lbl_nav_settings.bind('<Leave>', lambda e: self.lbl_nav_settings.configure(fg=COLORS['text_light']))


        # ===== 2. 中间好友/聊天会话列表 =====
        self.sidebar = tk.Frame(main, bg=COLORS['list_bg'], width=240)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)

        # 会话列表头部 (高60px，与右侧 header 对齐)
        self.sidebar_header = tk.Frame(self.sidebar, bg=COLORS['list_bg'], height=60)
        self.sidebar_header.pack(fill=tk.X)
        self.sidebar_header.pack_propagate(False)

        # 微信风格的粗体标题
        tk.Label(self.sidebar_header, text="微信", bg=COLORS['list_bg'],
                 fg=COLORS['text_primary'], font=('Microsoft YaHei', 13, 'bold')
                 ).pack(side=tk.LEFT, padx=12, pady=15)

        self.lbl_status = tk.Label(self.sidebar_header, text="Connecting...",
                                    bg=COLORS['list_bg'],
                                    fg=COLORS['text_secondary'], font=FONT_SMALL)
        self.lbl_status.pack(side=tk.RIGHT, padx=12, pady=18)

        # 个人名字展示区 (紧跟在会话头部下方)
        self.lbl_username = tk.Label(self.sidebar, text="Click to set name",
                                      bg=COLORS['list_bg'],
                                      fg=COLORS['text_primary'],
                                      font=FONT_BOLD,
                                      cursor='hand2',
                                      anchor=tk.W)
        self.lbl_username.pack(fill=tk.X, padx=12, pady=(0, 8))
        self.lbl_username.bind('<Button-1>', lambda e: self._open_profile_dialog())

        # 分割线
        tk.Frame(self.sidebar, bg=COLORS['border'], height=1).pack(fill=tk.X, padx=8, pady=(0, 4))

        # 公聊入口区域
        self.chat_section = tk.Frame(self.sidebar, bg=COLORS['list_bg'])
        self.chat_section.pack(fill=tk.X, padx=0, pady=(2, 0))

        self.btn_public = tk.Frame(self.chat_section, bg=COLORS['list_active'],
                                    cursor='hand2')
        self.btn_public.pack(fill=tk.X, padx=0, pady=1)

        pub_inner = tk.Frame(self.btn_public, bg=COLORS['list_active'])
        pub_inner.pack(fill=tk.X, padx=10, pady=10)

        # 公聊默认头像 (使用 avatar_1)
        self._pub_avatar = make_preset_avatar('avatar_1', size=36)
        self.lbl_pub_avatar = tk.Label(pub_inner, image=self._pub_avatar, bg=COLORS['list_active'])
        self.lbl_pub_avatar.image = self._pub_avatar
        self.lbl_pub_avatar.pack(side=tk.LEFT, padx=(0, 10))

        pub_info_frame = tk.Frame(pub_inner, bg=COLORS['list_active'])
        pub_info_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        pub_text = tk.Label(pub_info_frame, text="公共聊天室",
                             bg=COLORS['list_active'],
                             fg=COLORS['text_primary'],
                             font=FONT_BOLD)
        pub_text.pack(anchor=tk.W)

        pub_subtext = tk.Label(pub_info_frame, text="大家都在这里聊天",
                                bg=COLORS['list_active'],
                                fg=COLORS['text_secondary'],
                                font=FONT_SMALL)
        pub_subtext.pack(anchor=tk.W)

        # 公聊 hover 效果
        for w in [self.btn_public, pub_inner, self.lbl_pub_avatar, pub_info_frame, pub_text, pub_subtext]:
            w.bind('<Button-1>', lambda e: self._switch_to_public())
            w.bind('<Enter>', lambda e: self._set_pub_active_style(True))
            w.bind('<Leave>', lambda e: self._set_pub_active_style(False))

        # 分割线
        tk.Frame(self.sidebar, bg=COLORS['border'], height=1).pack(fill=tk.X, padx=8, pady=(4, 4))

        # 在线列表容器
        online_section = tk.Frame(self.sidebar, bg=COLORS['list_bg'])
        online_section.pack(fill=tk.BOTH, expand=True, padx=0, pady=(0, 0))

        online_header = tk.Frame(online_section, bg=COLORS['list_bg'])
        online_header.pack(fill=tk.X, padx=12, pady=(4, 2))

        tk.Label(online_header, text="用户列表", bg=COLORS['list_bg'],
                 fg=COLORS['text_secondary'], font=FONT_SMALL).pack(side=tk.LEFT)
        self.lbl_online_count = tk.Label(online_header, text="0",
                                          bg=COLORS['list_bg'],
                                          fg=COLORS['text_secondary'],
                                          font=FONT_SMALL)
        self.lbl_online_count.pack(side=tk.LEFT, padx=4)

        # 可滚动用户列表
        self.user_scroll = ScrollableFrame(online_section, bg=COLORS['list_bg'])
        self.user_scroll.pack(fill=tk.BOTH, expand=True)
        self.user_list_frame = self.user_scroll.scrollable_frame


        # ===== 3. 右侧微信聊天区域 =====
        right = tk.Frame(main, bg=COLORS['bg_chat'])
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 聊天头部 (高60px)
        chat_header = tk.Frame(right, bg=COLORS['bg_input'], height=60)
        chat_header.pack(fill=tk.X)
        chat_header.pack_propagate(False)

        # 底部分割线
        tk.Frame(chat_header, bg=COLORS['border'], height=1).pack(side=tk.BOTTOM, fill=tk.X)

        self.btn_back = tk.Label(chat_header, text="← 返回",
                                  bg=COLORS['bg_input'],
                                  fg=COLORS['green'],
                                  font=FONT_MAIN,
                                  cursor='hand2')
        self.btn_back.bind('<Button-1>', lambda e: self._switch_to_public())
        self.btn_back.bind('<Enter>', lambda e: self.btn_back.configure(fg=COLORS['green_dark']))
        self.btn_back.bind('<Leave>', lambda e: self.btn_back.configure(fg=COLORS['green']))

        self.lbl_chat_title = tk.Label(chat_header, text="公共聊天室",
                                        bg=COLORS['bg_input'],
                                        fg=COLORS['text_primary'],
                                        font=FONT_TITLE)
        self.lbl_chat_title.pack(side=tk.LEFT, padx=16, pady=15)

        self.lbl_chat_status = tk.Label(chat_header, text="公开频道",
                                         bg=COLORS['bg_input'],
                                         fg=COLORS['text_secondary'],
                                         font=FONT_SMALL)
        self.lbl_chat_status.pack(side=tk.LEFT, padx=4, pady=20)

        # 输入区容器 (微信灰白背景)
        input_section_container = tk.Frame(right, bg=COLORS['bg_chat'])
        input_section_container.pack(fill=tk.X, side=tk.BOTTOM, padx=16, pady=(0, 16))

        # 消息区域
        self.msg_scroll = ScrollableFrame(right, bg=COLORS['bg_chat'],
                                           on_scroll_top=self._on_scroll_to_top)
        self.msg_scroll.pack(fill=tk.BOTH, expand=True)
        self.msg_frame = self.msg_scroll.scrollable_frame

        # 表情面板 (Grid 布局, 默认不展示)
        self.emoji_panel = tk.Frame(input_section_container, bg=COLORS['text_input_bg'],
                                    highlightbackground=COLORS['border'], highlightthickness=1)
        
        # 64个表情
        emojis = [
            "😀", "😃", "😄", "😁", "😆", "😅", "😂", "🤣", "😊", "😇", "🙂", "🙃", "😉", "😌", "😍", "🥰", 
            "😘", "😗", "😙", "😚", "😋", "😛", "😝", "😜", "🤪", "🤨", "🧐", "🤓", "😎", "🤩", "🥳", "😏", 
            "😒", "😞", "😔", "😟", "😕", "🙁", "☹️", "😣", "😖", "😫", "😩", "🥺", "😢", "😭", "😤", "😠", 
            "😡", "🤬", "🤯", "😳", "🥵", "🥶", "😱", "😨", "😰", "😥", "😓", "🤗", "🤔", "🤭", "🤫", "🤥"
        ]
        
        emoji_grid = tk.Frame(self.emoji_panel, bg=COLORS['text_input_bg'])
        emoji_grid.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)
        
        cols = 16
        for idx, emoji in enumerate(emojis):
            row = idx // cols
            col = idx % cols
            lbl = tk.Label(emoji_grid, text=emoji, bg=COLORS['text_input_bg'], font=('', 14), cursor='hand2')
            lbl.grid(row=row, column=col, padx=4, pady=4)
            lbl.bind('<Button-1>', lambda e, em=emoji: self._insert_emoji(em))
            lbl.bind('<Enter>', lambda e, l=lbl: l.configure(bg=COLORS['border']))
            lbl.bind('<Leave>', lambda e, l=lbl: l.configure(bg=COLORS['text_input_bg']))

        # 输入框主容器
        self.input_box = tk.Frame(input_section_container, bg=COLORS['bg_input'],
                                  highlightbackground=COLORS['border'], highlightthickness=1)
        self.input_box.pack(fill=tk.BOTH, expand=True)

        # 微信风格的工具栏在上方
        toolbar = tk.Frame(self.input_box, bg=COLORS['bg_input'])
        toolbar.pack(fill=tk.X, side=tk.TOP, padx=8, pady=6)

        icons_frame = tk.Frame(toolbar, bg=COLORS['bg_input'])
        icons_frame.pack(side=tk.LEFT)

        # 使用 Unicode 符号模拟图标
        icons = [
            ("😀", lambda e: self._toggle_emoji_panel(e)), 
            ("📁", lambda e: self._upload_file()), 
            ("✂", lambda e: None), 
            ("🎤", lambda e: None)
        ]
        
        for text, cmd in icons:
            lbl = tk.Label(icons_frame, text=text, bg=COLORS['bg_input'], 
                           fg=COLORS['text_secondary'], font=('', 14), cursor='hand2')
            lbl.pack(side=tk.LEFT, padx=6)
            lbl.bind('<Button-1>', cmd)
            lbl.bind('<Enter>', lambda e, l=lbl: l.configure(fg=COLORS['text_primary']))
            lbl.bind('<Leave>', lambda e, l=lbl: l.configure(fg=COLORS['text_secondary']))

        # 发送按钮
        self._send_active = False
        self.btn_send = tk.Label(toolbar, text=" 发送 ",
                                  bg='#F0F0F0', fg='#CCCCCC',
                                  font=FONT_MAIN,
                                  padx=16, pady=6, cursor='hand2')
        self.btn_send.pack(side=tk.RIGHT, padx=4)
        self.btn_send.bind('<Button-1>', lambda e: self._send_message())
        self.btn_send.bind('<Enter>', self._on_send_hover_enter)
        self.btn_send.bind('<Leave>', self._on_send_hover_leave)

        # 输入文本区域
        self.msg_input = tk.Text(self.input_box, bg=COLORS['bg_input'],
                                 fg=COLORS['text_primary'], font=FONT_MAIN,
                                 bd=0, highlightthickness=0, height=4,
                                 insertbackground=COLORS['text_primary'],
                                 wrap=tk.WORD)
        self.msg_input.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))
        self.msg_input.bind('<KeyRelease>', self._on_input_change)
        self.msg_input.bind('<Return>', self._on_enter_press)

    def _on_input_change(self, event=None):
        content = self.msg_input.get('1.0', tk.END).strip()
        if content:
            self.btn_send.configure(bg=COLORS['green'], fg='white')
            self._send_active = True
        else:
            self.btn_send.configure(bg='#E1E1E1', fg='#999999')
            self._send_active = False

    def _on_send_hover_enter(self, event):
        if getattr(self, '_send_active', False):
            self.btn_send.configure(bg=COLORS['green_dark'])

    def _on_send_hover_leave(self, event):
        if getattr(self, '_send_active', False):
            self.btn_send.configure(bg=COLORS['green'])

    def _set_pub_active_style(self, active):
        bg = COLORS['list_hover'] if active else (COLORS['list_active'] if self.current_mode == 'public' else COLORS['list_bg'])
        self.btn_public.configure(bg=bg)
        for child in self.btn_public.winfo_children():
            child.configure(bg=bg)
            for gchild in child.winfo_children():
                gchild.configure(bg=bg)

    def _insert_emoji(self, emoji):
        self.msg_input.insert(tk.INSERT, emoji)
        self.msg_input.focus_set()
        self._on_input_change()

    def _toggle_emoji_panel(self, event=None):
        if self.emoji_panel.winfo_ismapped():
            self.emoji_panel.pack_forget()
        else:
            self.emoji_panel.pack(side=tk.TOP, fill=tk.X, before=self.input_box, pady=(0, 8))

    def _open_profile_dialog(self):
        current_name = self.current_user.get('username', '') if self.current_user else ''
        current_avatar = self.current_user.get('avatar', 'avatar_1') if self.current_user else 'avatar_1'
        
        def save_profile(new_name, new_avatar):
            self.network.send('update_profile', {
                'username': new_name,
                'avatar': new_avatar
            })
            
        ProfileEditDialog(self.root, current_name, current_avatar, save_profile)

    def _on_update_profile_result(self, data):
        user = data.get('user', {})
        self.current_user = user
        username = user.get('username', '')
        avatar_id = user.get('avatar', 'avatar_1')
        
        self.lbl_username.configure(text=username)
        self._update_my_avatar(username, avatar_id)
        self._append_system_msg("个人信息已成功更新")

    def _update_my_avatar(self, name: str, avatar_id: str = None):
        if HAS_PIL:
            if not avatar_id and self.current_user:
                avatar_id = self.current_user.get('avatar')
            
            avatar = None
            if avatar_id and avatar_id in PRESET_AVATARS:
                avatar = make_preset_avatar(avatar_id, size=38)
            else:
                avatar = _make_avatar(name[0] if name else '?', size=38)
                
            if avatar:
                self.lbl_my_avatar.configure(image=avatar)
                self.lbl_my_avatar.image = avatar

    def _set_username_dialog(self):
        self._open_profile_dialog()

    # ========== 服务器消息处理 ==========
    def _on_message(self, msg: dict):
        msg_type = msg.get('type', '')
        data = msg.get('data', {})
        handler = {
            'auth_result': self._on_auth_result,
            'update_profile_result': self._on_update_profile_result,
            'new_message': self._on_new_message,
            'message_history': self._on_message_history,
            'user_list': self._on_user_list,
            'user_online': self._on_user_online,
            'user_offline': self._on_user_offline,
            'message_recalled': self._on_msg_recalled,
            'message_deleted': self._on_msg_deleted,
            'messages_cleared': self._on_msgs_cleared,
            'upload_result': self._on_upload_result,
            'upload_start_result': self._on_upload_start_result,
            'upload_chunk_ack': self._on_upload_chunk_ack,
            'set_username_result': self._on_set_username_result,
            'error': self._on_error,
            'pong': lambda d: None,
        }.get(msg_type)
        if handler:
            try:
                handler(data)
            except Exception as e:
                print(f'[UI] handler error: {e}')

    def _on_auth_result(self, data):
        user = data.get('user', {})
        self.current_user = user
        self.is_banned = data.get('is_banned', False)
        self.is_muted = user.get('is_muted', False)

        if self.is_banned:
            self._show_banned_screen(user)
            return

        username = user.get('username')
        avatar_id = user.get('avatar', 'avatar_1')
        if username:
            self.lbl_username.configure(text=username)
            self._update_my_avatar(username, avatar_id)
        else:
            self.lbl_username.configure(text="Click to set name")
            self.root.after(500, self._set_username_dialog)

        self._append_system_msg("Authentication successful")

    def _show_banned_screen(self, user):
        reason = user.get('ban_reason', 'Violation')
        until = user.get('ban_until', '')
        until_str = f"Until: {until}" if until else "Permanent"
        self._append_system_msg(f"[BANNED] Reason: {reason}. {until_str}")
        self.msg_input.configure(state=tk.DISABLED)

    def _on_new_message(self, data):
        msg_id = data.get('id')
        if msg_id in self.msg_ids_shown:
            return
        self.msg_ids_shown.add(msg_id)

        is_private = data.get('receiver_id') is not None
        is_self = self.current_user and data.get('sender_id') == self.current_user.get('id')

        if self.current_mode == 'public' and not is_private:
            self._render_message(data)
        elif self.current_mode == 'private' and self.current_target and is_private:
            target_id = self.current_target['id']
            relevant = (data.get('sender_id') == target_id and
                        data.get('receiver_id') == self.current_user.get('id')) or \
                       (is_self and data.get('receiver_id') == target_id)
            if relevant:
                self._render_message(data)
            else:
                other_id = data.get('sender_id') if not is_self else data.get('receiver_id')
                if other_id:
                    self._increment_unread(other_id)
        elif is_private:
            other_id = data.get('sender_id') if not is_self else data.get('receiver_id')
            if other_id:
                self._increment_unread(other_id)
                self.chat_previews[other_id] = (data.get('content') or '[File]')[:20]
                self._refresh_user_list()

    def _on_message_history(self, data):
        is_append = data.get('is_append', False)
        has_more = data.get('has_more', False)
        self._has_more_messages = has_more
        self._loading_more = False

        messages = data.get('messages', [])

        if is_append:
            # 追加模式：移除"加载更多..."提示
            for w in self.msg_frame.winfo_children():
                try:
                    # SystemMessage 内部有一个 Label
                    for child in w.winfo_children():
                        if hasattr(child, 'cget') and '加载更多' in str(child.cget('text')):
                            w.destroy()
                            break
                except Exception:
                    pass

            if not messages:
                self._append_system_msg("没有更多历史消息")
                return

            # 记录当前滚动位置
            self.msg_scroll.canvas.update_idletasks()
            old_yview = self.msg_scroll.canvas.yview()
            old_bbox = self.msg_scroll.canvas.bbox('all')
            old_height = old_bbox[3] - old_bbox[1] if old_bbox else 0
            # 追加模式：向上插入更早的消息，保持滚动位置
            # 记录当前滚动位置
            self.msg_scroll.canvas.update_idletasks()
            old_yview = self.msg_scroll.canvas.yview()
            old_bbox = self.msg_scroll.canvas.bbox('all')
            old_height = old_bbox[3] - old_bbox[1] if old_bbox else 0

            # 临时禁用 auto scrollregion 更新
            for msg in messages:
                self._render_message_at_top(msg)

            # 恢复滚动位置（补偿新内容的高度）
            self.msg_scroll.canvas.update_idletasks()
            new_bbox = self.msg_scroll.canvas.bbox('all')
            new_height = new_bbox[3] - new_bbox[1] if new_bbox else 0
            height_diff = new_height - old_height
            if height_diff > 0 and old_yview[0] > 0:
                # 移动 scrollregion 补偿高度差
                total = new_height - self.msg_scroll.canvas.winfo_height()
                if total > 0:
                    new_top = (old_yview[0] * new_height + height_diff) / new_height
                    new_top = min(new_top, 1.0)
                    self.msg_scroll.canvas.yview_moveto(new_top)
        else:
            # 替换模式：清空并重新加载
            for w in self.msg_frame.winfo_children():
                w.destroy()
            self.msg_ids_shown.clear()
            self._msg_bubbles.clear()

            for msg in messages:
                self._render_message(msg, auto_scroll=False)

            # 所有消息加载完后滚动到底部
            self.msg_scroll.scroll_to_bottom()

    def _on_user_list(self, data):
        users = data.get('users', [])
        current_user_id = self.current_user.get('id') if self.current_user else None
        self.online_users = [u for u in users if u.get('id') != current_user_id]
        self.online_users.sort(key=lambda u: (not u.get('is_online', False), u.get('username', '')))
        self._refresh_user_list()

    def _on_user_online(self, data):
        user = data.get('user', {})
        if self.current_user and user.get('id') != self.current_user.get('id'):
            user['is_online'] = True
            existing_index = next((i for i, u in enumerate(self.online_users) if u.get('id') == user.get('id')), None)
            if existing_index is None:
                self.online_users.append(user)
            else:
                self.online_users[existing_index] = user
            self.online_users.sort(key=lambda u: (not u.get('is_online', False), u.get('username', '')))
            self._refresh_user_list()

    def _on_user_offline(self, data):
        user_id = data.get('user_id')
        for user in self.online_users:
            if user.get('id') == user_id:
                user['is_online'] = False
                break
        self.online_users.sort(key=lambda u: (not u.get('is_online', False), u.get('username', '')))
        self._refresh_user_list()

    def _on_msg_recalled(self, data):
        self._append_system_msg(f"Message #{data.get('msg_id')} recalled")

    def _on_msg_deleted(self, data):
        self._append_system_msg(f"Message #{data.get('msg_id')} deleted")

    def _on_msgs_cleared(self, data):
        self._append_system_msg(f"Messages cleared (scope={data.get('scope')})")

    def _on_upload_result(self, data):
        if data.get('status') == 'success':
            self.network.send('send_message', {
                'content': '',
                'msg_type': data.get('msg_type', 'file'),
                'file_url': data.get('file_url'),
                'file_name': data.get('file_name'),
                'receiver_id': self.current_target['id'] if self.current_mode == 'private' and self.current_target else None
            })
            self._append_system_msg("File uploaded successfully")
        else:
            messagebox.showerror("Upload Failed", data.get('msg', 'Unknown error'))
        self._pending_upload = None

    def _on_upload_start_result(self, data):
        if data.get('status') != 'success':
            messagebox.showerror("Upload Failed", data.get('msg', 'Init failed'))
            self._pending_upload = None
            return
        upload_id = data.get('upload_id')
        if not self._pending_upload:
            return
        self._pending_upload['upload_id'] = upload_id
        self._send_next_chunk()

    def _on_upload_chunk_ack(self, data):
        if data.get('status') != 'success':
            messagebox.showerror("Upload Failed", data.get('msg', 'Chunk failed'))
            self._pending_upload = None
            return
        received = data.get('received', 0)
        total = data.get('total', 1)
        self._append_system_msg(f"Upload progress: {received}/{total}")
        self._send_next_chunk()

    def _send_next_chunk(self):
        ctx = self._pending_upload
        if not ctx:
            return
        idx = ctx['current_chunk']
        total = ctx['total_chunks']
        if idx >= total:
            return
        chunk_size = ctx['chunk_size']
        start = idx * chunk_size
        end = min(start + chunk_size, len(ctx['file_data_b64']))
        chunk_data = ctx['file_data_b64'][start:end]
        self.network.send('upload_chunk', {
            'upload_id': ctx['upload_id'],
            'chunk_index': idx,
            'chunk_data': chunk_data
        })
        ctx['current_chunk'] = idx + 1

    def _on_set_username_result(self, data):
        if data.get('status') == 'success':
            user = data.get('user', {})
            self.current_user = user
            username = user.get('username', '')
            self.lbl_username.configure(text=username)
            self._update_my_avatar(username)
            self._append_system_msg(f"Username set: {username}")
        else:
            messagebox.showerror("Failed", data.get('msg', 'Unknown error'))

    def _on_error(self, data):
        msg = data.get('msg', 'Unknown error')
        code = data.get('code', 0)
        if code == 403:
            self.is_banned = True
            self._show_banned_screen(self.current_user or {})
        else:
            self._append_system_msg(f"[Error] {msg}")
        self._pending_upload = None

    # ========== UI 操作 ==========
    def _render_message(self, msg: dict, auto_scroll=True):
        msg_id = msg.get('id')
        is_self = self.current_user and msg.get('sender_id') == self.current_user.get('id')
        msg_type = msg.get('msg_type', 'text')
        content = msg.get('content', '')
        file_url = msg.get('file_url', '')

        bubble = MessageBubble(self.msg_frame, msg, is_self)
        bubble.pack(fill=tk.X, anchor=tk.W)

        # 记录气泡引用（用于图片异步替换）
        if msg_type == 'image' and file_url and HAS_PIL:
            msg_id_int = int(msg_id) if msg_id is not None else -1
            self._msg_bubbles[msg_id_int] = bubble
            self._download_and_show_image(file_url, msg_id_int)

        if auto_scroll:
            self.msg_scroll.scroll_to_bottom()

    def _render_message_at_top(self, msg: dict):
        """在消息区顶部插入一条消息（用于加载更多历史）"""
        msg_id = msg.get('id')
        if msg_id in self.msg_ids_shown:
            return
        self.msg_ids_shown.add(msg_id)

        is_self = self.current_user and msg.get('sender_id') == self.current_user.get('id')
        msg_type = msg.get('msg_type', 'text')
        file_url = msg.get('file_url', '')

        bubble = MessageBubble(self.msg_frame, msg, is_self)
        # pack 到顶部（在现有第一个子widget之前）
        children = self.msg_frame.winfo_children()
        if children:
            bubble.pack(fill=tk.X, anchor=tk.W, before=children[0])
        else:
            bubble.pack(fill=tk.X, anchor=tk.W)

        if msg_type == 'image' and file_url and HAS_PIL:
            msg_id_int = int(msg_id) if msg_id is not None else -1
            self._msg_bubbles[msg_id_int] = bubble
            self._download_and_show_image(file_url, msg_id_int)

    def _on_scroll_to_top(self):
        """滚动到顶部时加载更多历史消息"""
        if self._loading_more or not self._has_more_messages:
            return
        if not self.current_user or not self.current_user.get('username'):
            return

        # 获取当前最早消息的 ID
        children = self.msg_frame.winfo_children()
        if not children:
            return

        # 找到最早的消息 ID（遍历 msg_ids_shown 取最小值）
        oldest_id = min(self.msg_ids_shown) if self.msg_ids_shown else None
        if not oldest_id:
            return

        self._loading_more = True
        self._append_system_msg("加载更多消息...")

        receiver_id = None
        if self.current_mode == 'private' and self.current_target:
            receiver_id = self.current_target['id']

        self.network.send('get_messages', {
            'receiver_id': receiver_id,
            'before_id': oldest_id,
            'limit': 200
        })

    def _append_system_msg(self, text: str):
        sys_msg = SystemMessage(self.msg_frame, text)
        sys_msg.pack(fill=tk.X)
        self.msg_scroll.scroll_to_bottom()

    def _download_and_show_image(self, file_url: str, msg_id: int):
        if file_url in self._image_downloading:
            return
        self._image_downloading.add(file_url)

        def _download():
            try:
                import urllib.parse
                # 修复带中文或特殊字符的文件路径导致下载失败的问题
                safe_url = urllib.parse.quote(file_url, safe='/:?&=')
                full_url = f"{self._http_server}{safe_url}"
                req = urllib.request.Request(full_url,
                                             headers={'User-Agent': 'CSChatClient/1.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    img_data = resp.read()
                self.root.after(0, self._render_image, img_data, file_url, msg_id)
            except Exception as e:
                self.root.after(0, self._on_image_download_failed, file_url, msg_id, str(e))
            finally:
                self._image_downloading.discard(file_url)

        threading.Thread(target=_download, daemon=True).start()

    def _render_image(self, img_data: bytes, file_url: str, msg_id: int):
        try:
            pil_image = Image.open(io.BytesIO(img_data))
            max_w, max_h = 300, 225
            w, h = pil_image.size
            if w > max_w or h > max_h:
                ratio = min(max_w / w, max_h / h)
                pil_image = pil_image.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)

            photo = ImageTk.PhotoImage(pil_image)
            self._image_cache[file_url] = photo

            # 替换气泡中的占位 Label
            bubble = self._msg_bubbles.get(msg_id)
            if bubble and hasattr(bubble, '_img_label'):
                img_lbl = bubble._img_label
                bg = bubble._bubble_bg
                img_lbl.configure(image=photo, text='')
                img_lbl.image = photo

            self.msg_scroll.scroll_to_bottom()
        except Exception as e:
            self._on_image_download_failed(file_url, msg_id, str(e))

    def _on_image_download_failed(self, file_url: str, msg_id: int, error: str):
        bubble = self._msg_bubbles.get(msg_id)
        if bubble and hasattr(bubble, '_img_label'):
            bubble._img_label.configure(image='', text=f"[Image load failed]")
        self._msg_bubbles.pop(msg_id, None)

    def _refresh_user_list(self):
        # 清空现有列表
        for w in self.user_list_frame.winfo_children():
            w.destroy()

        online_count = sum(1 for u in self.online_users if u.get('is_online', False))
        self.lbl_online_count.configure(text=f"{online_count}/{len(self.online_users)}在线")

        for u in self.online_users:
            uid = u.get('id')
            unread = self.unread_counts.get(uid, 0)
            
            is_active = self.current_mode == 'private' and self.current_target and self.current_target['id'] == uid
            item_bg = COLORS['list_active'] if is_active else COLORS['list_bg']
            
            item = UserListItem(self.user_list_frame, u, unread, bg=item_bg)
            item.pack(fill=tk.X, pady=0)

            # 绑定点击
            item.bind_click(lambda e, user=u: self._open_private_chat(user))

    def _increment_unread(self, user_id: int):
        self.unread_counts[user_id] = self.unread_counts.get(user_id, 0) + 1
        self._refresh_user_list()

    def _switch_to_public(self):
        self.current_mode = 'public'
        self.current_target = None
        self.lbl_chat_title.configure(text="公共聊天室")
        self.lbl_chat_status.configure(text="公开频道")
        self.btn_back.pack_forget()
        
        self._set_pub_active_style(False)
        self._refresh_user_list()
        self.network.send('get_messages', {'receiver_id': None, 'limit': 200})

    def _open_private_chat(self, user: dict):
        if not user.get('username'):
            return
        self.current_mode = 'private'
        self.current_target = {'id': user['id'], 'username': user['username']}
        self.lbl_chat_title.configure(text=f"{user['username']}")
        self.lbl_chat_status.configure(text="私聊")
        self.btn_back.pack(side=tk.RIGHT, padx=12)

        self.unread_counts.pop(user['id'], None)
        
        self._set_pub_active_style(False)
        self._refresh_user_list()
        self.network.send('get_messages', {'receiver_id': user['id'], 'limit': 200})

    def _send_message(self):
        if self.is_banned:
            messagebox.showwarning("Banned", "Your account is banned")
            return
        if self.is_muted:
            messagebox.showwarning("Muted", "You are muted")
            return

        content = self.msg_input.get('1.0', tk.END).strip()
        if not content:
            return

        payload = {'content': content, 'msg_type': 'text'}
        if self.current_mode == 'private' and self.current_target:
            payload['receiver_id'] = self.current_target['id']

        self.network.send('send_message', payload)
        self.msg_input.delete('1.0', tk.END)
        self._on_input_change()

    def _on_enter_press(self, event):
        if not event.state & 0x1:
            self._send_message()
            return 'break'

    def _upload_file(self):
        if self.is_banned or self.is_muted:
            messagebox.showwarning("Restricted", "You cannot upload files")
            return

        filepath = filedialog.askopenfilename(
            title="Select File",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.gif"),
                ("Documents", "*.pdf *.doc *.docx *.txt"),
                ("Spreadsheets", "*.xls *.xlsx"),
                ("Archives", "*.zip *.rar"),
                ("All Files", "*.*")
            ]
        )
        if not filepath:
            return

        filename = os.path.basename(filepath)
        ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
        allowed = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx',
                    'xls', 'xlsx', 'txt', 'zip', 'rar'}
        if ext not in allowed:
            messagebox.showerror("Error", f"File type not allowed: {ext}")
            return

        file_size = os.path.getsize(filepath)
        if file_size > 100 * 1024 * 1024:
            messagebox.showerror("Error", "File size exceeds 100MB")
            return

        self._append_system_msg(f"Uploading: {filename} ({file_size // 1024}KB)")

        try:
            with open(filepath, 'rb') as f:
                raw_bytes = f.read()
            file_data_b64 = base64.b64encode(raw_bytes).decode('utf-8')
        except Exception as e:
            messagebox.showerror("Read Failed", str(e))
            return

        CHUNK_THRESHOLD = 512 * 1024

        # Important fix for PyInstaller/Windows exe issues:
        # Avoid creating huge strings in memory or sending them synchronously to the queue 
        # that could freeze the tkinter mainloop or the websocket thread.
        # Here we just schedule the upload to prevent locking the UI.

        def start_upload():
            if file_size <= CHUNK_THRESHOLD:
                self.network.send('upload_file', {
                    'file_data': file_data_b64,
                    'file_name': filename
                })
            else:
                CHUNK_SIZE = 400 * 1024
                total_chunks = (len(file_data_b64) + CHUNK_SIZE - 1) // CHUNK_SIZE

                self._pending_upload = {
                    'file_data_b64': file_data_b64,
                    'file_name': filename,
                    'chunk_size': CHUNK_SIZE,
                    'total_chunks': total_chunks,
                    'upload_id': None,
                    'current_chunk': 0
                }
                self.network.send('upload_start', {
                    'file_name': filename,
                    'total_chunks': total_chunks,
                    'file_size': file_size
                })
                
        # Use root.after to defer the start, avoiding locking the dialog response loop
        self.root.after(50, start_upload)

    def on_close(self):
        self.network.stop()
        self.root.destroy()


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = ChatApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
