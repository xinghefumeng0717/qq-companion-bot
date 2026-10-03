"""青梓小窝桌面控制台 核心纯逻辑模块 (launcher/core.py)
无 GUI 依赖的纯函数：状态解析、持久化、命令构造，便于单元测试与自动化验证。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# 远程服务器地址。公开仓库不硬编码真实基础设施 IP：
# 优先读环境变量 QQC_REMOTE_HOST，未设置时使用占位符（fork 者填自己的服务器即可）。
DEFAULT_REMOTE_HOST = os.environ.get("QQC_REMOTE_HOST", "ubuntu@<你的服务器IP>")


def parse_status_data(api_json: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 /api/status 接口返回的 JSON 数据"""
    if not isinstance(api_json, dict):
        return {
            "bot_alive": False,
            "onebot_connected": False,
            "stage_name": "未知",
            "today_cost": 0.0,
            "composite": 0.0,
            "last_backup_time": None,
            "uptime_minutes": 0,
            "robot_online": False,
        }

    bot_alive = bool(api_json.get("bot_alive", False))
    onebot_conn = bool(api_json.get("onebot_connected", False))
    stage_name = str(api_json.get("stage_name", "未知"))
    today_cost = float(api_json.get("today_cost", 0.0))
    composite = float(api_json.get("composite", 0.0))
    last_backup = api_json.get("last_backup_time")
    uptime = int(api_json.get("uptime_minutes", 0))

    return {
        "bot_alive": bot_alive,
        "onebot_connected": onebot_conn,
        "stage_name": stage_name,
        "today_cost": today_cost,
        "composite": composite,
        "last_backup_time": last_backup,
        "uptime_minutes": uptime,
        "robot_online": bot_alive and onebot_conn,
    }


def format_header_info(stage_name: str, today_cost: float) -> str:
    """格式化顶部信息栏展示文本：'当前阶段：相识 · 今日费用：¥0.42'"""
    return f"当前阶段：{stage_name} · 今日费用：¥{today_cost:.2f}"


def load_sync_state(file_path: str) -> Dict[str, Any]:
    """读取备份同步状态文件 (data/backups/sync_state.json)"""
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_sync_state(file_path: str, state: Dict[str, Any]) -> None:
    """保存备份同步状态文件"""
    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_backup_command(
    remote_host: str = DEFAULT_REMOTE_HOST,
    remote_path: str = "/opt/qq-companion/data/backup/daily/latest.db",
    local_dest: str = r"D:\qq-companion\data\backups\\",
) -> List[str]:
    """生成 SCP 拉取备份的命令行参数列表"""
    return ["scp", f"{remote_host}:{remote_path}", local_dest]


def get_ssh_tunnel_command(
    remote_host: str = DEFAULT_REMOTE_HOST,
    local_port: int = 8080,
    remote_target: str = "127.0.0.1:8080",
) -> List[str]:
    """生成 SSH 端口映射隧道命令（BatchMode 快速失败，不静默挂起）"""
    return [
        "ssh", "-N", "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-L", f"{local_port}:{remote_target}", remote_host,
    ]


def get_simulation_cmd_args(
    remote_host: str = DEFAULT_REMOTE_HOST,
) -> List[str]:
    """生成新开 cmd 窗口执行仿真对话的命令"""
    remote_cmd = "cd /opt/qq-companion && ./venv/bin/python -m companion.chat"
    return ["cmd.exe", "/c", "start", "ssh", "-t", remote_host, remote_cmd]
