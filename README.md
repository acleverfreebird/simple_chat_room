# ChatRoom - C/S 架构实时智能聊天室系统

这是一个基于 Python 的 C/S（客户端/服务器）架构实时聊天室系统。系统采用 WebSocket 实现全双工通信，配备美观的 Tkinter 桌面客户端、完善的后台管理面板、日志查看器以及防刷防封禁逃逸（双重设备指纹识别）机制。

---

## 📂 项目结构说明

- **核心服务端与客户端**
  - [`cs_server.py`](cs_server.py): 基于 `asyncio` 和 `websockets` 的高性能异步服务器，负责核心消息转发、速率限制、敏感词过滤，并通过内置的 HTTP 服务提供 Web 管理后台。
  - [`cs_client.py`](cs_client.py): 基于 `tkinter` 的现代桌面客户端，提供类似微信的主题色彩与布局，支持渐变色圆角字符头像、文件/图片发送及下载。
  - [`models.py`](models.py): 基于 Flask-SQLAlchemy 的 SQLite 数据库模型定义，包含用户 (`User`)、消息记录 (`Message`) 和审计日志 (`AuditLog`) 表结构。

- **分发与辅助服务**
  - [`download_server.py`](download_server.py): 基于 Flask 的独立客户端下载服务器，向局域网或公网用户分发打包好的客户端程序。

- **Web 管理前端**
  - [`templates/admin.html`](templates/admin.html): 现代化响应式后台管理面板，用于监控在线用户、管理用户禁言/封禁、查阅审计日志以及动态热修改敏感词与文件白名单。
  - [`templates/download.html`](templates/download.html): 客户端下载页面引导模板。

- **配置文件与脚本**
  - [`server_config.json`](server_config.json): 服务端配置文件，包括 IP 绑定、WebSocket 端口、管理端口、敏感词列表、允许上传文件类型及大小限制。
  - [`client_config.json`](client_config.json): 客户端配置文件，指定服务端 WebSocket 地址和 HTTP 文件存储服务器地址。
  - [`start_server.bat`](start_server.bat): 服务端快捷启动批处理脚本。
  - [`start_download.bat`](start_download.bat): 下载服务端快捷启动批处理脚本。

---

## 🚀 核心技术特性

1. **防多开与封禁逃逸机制（Anti-Evasion）**
   - **本地持久化令牌 (`device_token`)**: 客户端首次启动会在用户系统数据目录下生成唯一的 `device.token` 隐藏文件。即使更换 IP 或删除客户端重新配置，服务端仍能识别出同一台机器，有效遏制绕过封禁的行为。
   - **硬件指纹 (`device_fingerprint`)**: 采集主机名、处理器架构、网卡物理地址哈希等多维度信息生成设备硬件指纹，作为防逃逸的辅助手段。

2. **完善的管理员控制台**
   - 自动在服务端启动时为超级管理员 `super_admin` 随机生成高强度初始密码并打印在控制台。
   - 管理面板支持针对特定用户进行临时禁言、永久禁言、解禁、账户级封禁等动作。
   - 包含文件与敏感词的配置热加载，修改即时生效，无需重启服务。

3. **消息处理与安全性**
   - 消息支持文本、图片（实时缩略图预览）和任意白名单内文件的上传与分享。
   - 采用滑动窗口速率限制（Rate Limiter）防御恶意的消息泛洪与垃圾文件上传攻击。
   - 数据库记录消息撤回/软删除状态，确保撤回的消息内容在网络和数据库层皆不向非特权用户公开。

---

## 🛠️ 环境准备与安装

运行此系统需要在机器上安装 Python 3.8+ 及相关依赖。

### 1. 服务端及下载端依赖安装
```bash
pip install flask flask-sqlalchemy flask-bcrypt websockets pillow
```

### 2. 客户端依赖安装
```bash
pip install websockets pillow
```
*注：客户端主要使用 Python 内置的 `tkinter` 库构建 UI，若未安装 `Pillow` 库，头像等自定义图像功能将以纯文本 fallback 模式运行。*

---

## 📖 运行与部署指南

### 第一步：配置网络与参数

1. 服务端配置 [`server_config.json`](server_config.json):
   - 默认监听所有网卡 `0.0.0.0`。
   - WebSocket 端口：`9000`。
   - HTTP 静态资源服务端口：`8080`（用于文件/图片上传和下载）。
   - 管理后台端口：`8081`。

2. 客户端配置 [`client_config.json`](client_config.json):
   - 修改 `server_url` 指向服务端的 WebSocket 地址，例如 `ws://192.168.1.100:9000`。
   - 修改 `http_server` 指向服务端的文件服务地址，例如 `http://192.168.1.100:8080`。

---

### 第二步：启动服务端

直接双击 [`start_server.bat`](start_server.bat) 或在控制台运行：
```bash
python cs_server.py
```
*启动时注意保存控制台打印出的 `super_admin` 随机密码，用于首次登录 Web 后台。*

服务启动后，系统将自动创建或迁移 SQLite 数据库，并监听以下端口：
- **聊天服务 (WS)**: `ws://localhost:9000`
- **文件存储服务 (HTTP)**: `http://localhost:8080`
- **管理后台 (HTTP)**: `http://localhost:8081`

---

### 第三步：运行与分发客户端

#### 开发环境下直接运行：
```bash
python cs_client.py
```

#### 打包分发运行：
1. 若要生成独立运行的 `.exe` 可执行程序，可以使用 `pyinstaller`:
   ```bash
   pyinstaller -F -w cs_client.py
   ```
2. 将打包好的 `cs_client.exe` 放置到 `dist/` 文件夹下。
3. 运行下载服务器，双击 [`start_download.bat`](start_download.bat) 或执行：
   ```bash
   python download_server.py
   ```
   分发服务器默认在 `80` 端口启动，局域网内的其他用户可访问 `http://服务器IP/` 进行客户端的下载安装。

---

## 🔒 管理后台使用说明

1. 浏览器访问 `http://localhost:8081`。
2. 输入管理员账号及密码进行登录：
   - 超级管理员：`super_admin` / [控制台生成的随机密码]
   - 普通管理员：可在 [`server_config.json`](server_config.json) 中配置 `ADMIN_PASSWORD`（默认密码为 `admin123`）。
3. 后台功能模块：
   - **控制台仪表盘**: 在线人数、消息总量、注册用户数等统计。
   - **用户管理**: 查看所有用户的 IP 历史、设备指纹、在线状态。可在此处执行“禁言”或“封禁/解封”操作。
   - **消息监控**: 实时监控全局公开消息流并可做强制撤回操作。
   - **审计日志**: 记录管理员的每一次操作以供合规性核查。
   - **系统设置**: 热配置敏感词及文件后缀白名单。
