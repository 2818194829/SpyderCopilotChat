# -*- coding: utf-8 -*-
"""
MCP (Model Context Protocol) 客户端

对标 GitHub Copilot Chat 的 MCP Server 集成。
支持标准 MCP 协议的 JSON-RPC 通信，提供工具发现和调用功能。

MCP 服务器配置 (~/.spyder_copilot_chat/mcp_servers.json):
[
    {
        "name": "my-server",
        "command": "python",
        "args": ["-m", "my_mcp_server"],
        "env": {"KEY": "VALUE"},
        "enabled": true
    }
]
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".spyder_copilot_chat")
MCP_CONFIG_FILE = os.path.join(CONFIG_DIR, "mcp_servers.json")

# Windows 下禁止弹出控制台窗口
_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
)


# ══════════════════════════════════════════════════════════════════════
# 配置管理
# ══════════════════════════════════════════════════════════════════════

def load_mcp_config():
    """加载 MCP 服务器配置"""
    try:
        with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_mcp_config(servers):
    """保存 MCP 服务器配置"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(MCP_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(servers, f, indent=2, ensure_ascii=False)


def get_enabled_servers():
    """获取已启用的服务器列表"""
    config = load_mcp_config()
    return [s for s in config if s.get("enabled", True)]


# ══════════════════════════════════════════════════════════════════════
# MCP 客户端 (Stdio 传输)
# ══════════════════════════════════════════════════════════════════════

class MCPClient:
    """MCP Stdio 客户端

    通过子进程 stdio 与 MCP 服务器通信。
    """

    def __init__(self, server_config):
        self._config = server_config
        self._process = None
        self._lock = threading.Lock()
        self._request_id = 0
        # v0.5.5：单次 JSON-RPC 读取上限，避免服务器沉默时永久占用线程
        self.timeout = float(server_config.get("timeout", 30) or 30)

    @property
    def name(self):
        return self._config.get("name", "unknown")

    @property
    def is_running(self):
        return self._process is not None and self._process.poll() is None

    def start(self):
        """启动 MCP 服务器进程"""
        if self.is_running:
            return True

        cmd = [self._config.get("command", "")]
        if not cmd[0]:
            logger.error("MCP server %s: no command specified", self.name)
            return False

        cmd.extend(self._config.get("args", []))
        env = os.environ.copy()
        env.update(self._config.get("env", {}))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=_SUBPROCESS_FLAGS,
            )
            # 发送 initialize 请求
            result = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "spyder-copilot-chat",
                    "version": "0.2.0",
                },
            })
            if result:
                logger.info("MCP server %s initialized", self.name)
                return True
            else:
                self.stop()
                return False
        except Exception as e:
            logger.error("Failed to start MCP server %s: %s", self.name, e)
            return False

    def stop(self):
        """停止 MCP 服务器进程"""
        if self._process:
            try:
                self._process.stdin.close()
                self._process.stdout.close()
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def _send_request(self, method, params=None):
        """发送 JSON-RPC 请求并等待响应"""
        with self._lock:
            self._request_id += 1
            req_id = self._request_id

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }

            try:
                req_json = json.dumps(request) + "\n"
                self._process.stdin.write(req_json.encode("utf-8"))
                self._process.stdin.flush()

                # v0.5.5 P0：MCP 服务器若不返回一行，原先会永久阻塞。
                # 用守护线程读 + 有界等待，避免耗尽线程池 / 卡死整个工具链。
                read_box = {}

                def _read_line():
                    try:
                        read_box["line"] = self._process.stdout.readline()
                    except Exception as exc:
                        read_box["error"] = exc

                reader = threading.Thread(target=_read_line, daemon=True)
                reader.start()
                reader.join(timeout=self.timeout)
                if reader.is_alive():
                    logger.warning("MCP 响应超时（%.0fs）: %s",
                                   self.timeout, method)
                    return None
                response_line = read_box.get("line")
                if not response_line:
                    return None

                response = json.loads(response_line.decode("utf-8"))
                if "error" in response:
                    logger.error("MCP error: %s", response["error"])
                    return None
                return response.get("result", {})
            except Exception as e:
                logger.error("MCP request failed: %s", e)
                return None

    def list_tools(self):
        """发现服务器提供的工具列表

        Returns: list of dicts with name, description, inputSchema
        """
        result = self._send_request("tools/list")
        if not result:
            return []
        return result.get("tools", [])

    def call_tool(self, tool_name, arguments=None):
        """调用 MCP 服务器工具

        Parameters
        ----------
        tool_name : str
            工具名称
        arguments : dict
            工具参数

        Returns: dict with content, isError
        """
        result = self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })
        if not result:
            return {"content": [], "isError": True}
        return result


# ══════════════════════════════════════════════════════════════════════
# 全局 MCP 管理器
# ══════════════════════════════════════════════════════════════════════

class MCPManager:
    """管理所有 MCP 服务器连接"""

    def __init__(self):
        self._clients = {}

    def refresh(self):
        """重新加载配置并启动/停止服务器"""
        servers = get_enabled_servers()
        current_names = set(self._clients.keys())
        config_names = {s["name"] for s in servers}

        # 停止已移除的服务器
        for name in current_names - config_names:
            self._clients[name].stop()
            del self._clients[name]

        # 启动新的或重启变更的服务器
        for server in servers:
            name = server["name"]
            if name in self._clients:
                continue
            client = MCPClient(server)
            if client.start():
                self._clients[name] = client

    def get_all_tools(self):
        """获取所有 MCP 服务器的工具列表

        Returns: list of dicts (OpenAI tool format)
        """
        all_tools = []
        for name, client in self._clients.items():
            if not client.is_running:
                continue
            try:
                tools = client.list_tools()
                for tool in tools:
                    all_tools.append({
                        "type": "function",
                        "function": {
                            "name": f"mcp_{name}__{tool.get('name', '')}",
                            "description": tool.get("description", ""),
                            "parameters": tool.get("inputSchema", {}),
                        },
                        "_mcp_server": name,
                        "_mcp_tool": tool.get("name", ""),
                    })
            except Exception as e:
                logger.warning("Failed to list tools from %s: %s", name, e)
        return all_tools

    def call_tool(self, full_name, arguments):
        """调用 MCP 工具

        Parameters
        ----------
        full_name : str
            格式: mcp_<server>__<tool>
        arguments : dict
            工具参数

        Returns: str (工具输出)
        """
        # 解析 server 和 tool 名称
        if not full_name.startswith("mcp_"):
            return "Error: Invalid MCP tool name format"

        rest = full_name[4:]  # 去掉 "mcp_"
        parts = rest.split("__", 1)
        if len(parts) != 2:
            return f"Error: Invalid MCP tool name: {full_name}"

        server_name, tool_name = parts
        client = self._clients.get(server_name)
        if not client or not client.is_running:
            return f"Error: MCP server '{server_name}' is not running"

        try:
            result = client.call_tool(tool_name, arguments)
            if result.get("isError"):
                return f"Error: {result.get('content', 'Unknown error')}"
            content = result.get("content", [])
            if isinstance(content, list):
                return "\n".join(
                    c.get("text", str(c)) for c in content
                )
            return str(content)
        except Exception as e:
            return f"Error calling MCP tool: {e}"

    def shutdown(self):
        """关闭所有 MCP 服务器"""
        for client in self._clients.values():
            client.stop()
        self._clients.clear()


# 全局单例
_mcp_manager = None


def get_mcp_manager():
    """获取全局 MCP 管理器单例"""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
        _mcp_manager.refresh()
    return _mcp_manager
