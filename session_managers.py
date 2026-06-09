"""
会话与权限管理模块
包含：会话工具、权限管理器、平台管理器、会话游戏管理器
"""

import asyncio
import json
from pathlib import Path
from typing import Dict, List, Any, Tuple

import aiofiles
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context


# ==================== 会话工具 ====================
class SessionUtils:
    """会话相关的工具方法"""

    @staticmethod
    def get_group_session_key(event: AstrMessageEvent) -> str:
        """获取群聊会话的唯一标识（用于隔离）"""
        gid = event.get_group_id()
        if gid:
            return f"{event.unified_msg_origin.split(':')[0]}:GroupMessage:{gid}"
        return event.unified_msg_origin

    @staticmethod
    def parse_session_times(
        config: Dict,
        global_key: str = "push_time",
        sessions_key: str = "push_sessions_times",
    ) -> Dict[str, int]:
        """
        从配置中解析每个会话的自定义时间
        :param config: 插件配置字典
        :param global_key: 全局时间配置键名
        :param sessions_key: 会话时间列表键名，格式 ["session_id,小时"]
        :return: {session_id: hour}
        """
        times = {}
        default_hour = config.get(global_key, 0)
        for item in config.get(sessions_key, []):
            if not isinstance(item, str):
                continue
            parts = item.rsplit(",", 1)
            if len(parts) != 2:
                continue
            sid, hour = parts[0].strip(), parts[1].strip()
            try:
                times[sid] = int(hour)
            except:
                pass
        for session in config.get("push_sessions", []):
            if session not in times:
                times[session] = default_hour
        return times


# ==================== 权限管理器 ====================
class PermissionManager:
    """
    权限管理类，支持：
    - 全局管理员（AstrBot admin）
    - 个人认证（基于 unified_msg_origin）
    - 群管理员认证（基于群组，仅当群聊）
    - 简单群认证（全体成员）
    """

    def __init__(self, config: Dict, context: Context):
        self.config = config
        self.context = context
        self.cache: Dict[str, bool] = {}
        self.detail_cache: Dict[str, str] = {}
        self._build_cache()

    def _build_cache(self):
        """从配置初始化个人认证缓存"""
        self.cache.clear()
        self.detail_cache.clear()
        if self.config.get("enable_personal_auth", False):
            for origin in self.config.get("personal_auth_list", []):
                self.cache[origin] = True
                self.detail_cache[origin] = "个人认证"
        logger.info(f"权限缓存初始化，包含 {len(self.cache)} 个会话")

    async def _get_group_managers(
        self, event: AstrMessageEvent, group_id: str
    ) -> List[Dict]:
        """获取群管理员列表（适配器相关）"""
        managers = []
        try:
            bot = getattr(event, "bot", None)
            if bot and hasattr(bot, "get_group_member_list"):
                members = await bot.get_group_member_list(group_id=group_id)
                for m in members:
                    if m.get("role") in ["owner", "admin"]:
                        managers.append(m)
        except Exception as e:
            logger.error(f"获取群管理员失败: {e}")
        return managers

    async def check_permission(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        """
        检查事件是否有权限
        返回 (是否允许, 原因)
        """
        if event.is_admin():
            return True, "全局管理员"

        origin = event.unified_msg_origin
        if origin in self.cache:
            return self.cache[origin], self.detail_cache.get(origin, "未知")

        enable_manager = self.config.get("enable_manager_auth", False)
        enable_simple = self.config.get("enable_simple_auth", False)
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        platform = origin.split(":")[0]
        allowed = False
        reason = ""

        if enable_manager and group_id:
            group_ses = f"{platform}:GroupMessage:{group_id}"
            if group_ses in self.config.get("manager_auth_list", []):
                managers = await self._get_group_managers(event, group_id)
                if user_id in {str(m["user_id"]) for m in managers}:
                    allowed = True
                    reason = "群管理员认证"

        if not allowed and enable_simple and group_id:
            group_ses = f"{platform}:GroupMessage:{group_id}"
            if group_ses in self.config.get("simple_auth_list", []):
                allowed = True
                reason = "简单群认证"

        if allowed:
            self.cache[origin] = True
            self.detail_cache[origin] = reason

        return allowed, reason

    def get_allowed_sessions(self, group_id: str = None) -> Dict[str, str]:
        """
        获取所有被允许的会话及其来源（用于展示）
        若传入group_id，只返回该群下的会话
        """
        result = {}
        for origin, allowed in self.cache.items():
            if not allowed:
                continue
            if group_id and not origin.endswith(f"{group_id}"):
                continue
            result[origin] = self.detail_cache.get(origin, "未知")
        return result


# ==================== 平台管理器 ====================
class PlatformManager:
    """
    通用平台管理器，用于管理多个平台的启用状态和会话列表。

    配置格式：
        enable_<platform>: bool          # 平台总开关
        <platform>_sessions: List[str]   # 允许该平台的会话ID列表
    """

    def __init__(self, config: Dict, platforms: List[str]):
        """
        :param config: 插件配置字典
        :param platforms: 平台名称列表，例如 ['steam', 'ns', 'ps', 'xbox']
        """
        self.config = config
        self.platforms = platforms

    def is_platform_enabled(self, platform: str) -> bool:
        return self.config.get(f"enable_{platform}", False)

    def get_platform_sessions(self, platform: str) -> List[str]:
        return self.config.get(f"{platform}_sessions", [])

    def is_session_enabled_for_platform(self, session_id: str, platform: str) -> bool:
        if not self.is_platform_enabled(platform):
            return False
        return session_id in self.get_platform_sessions(platform)

    def get_enabled_platforms_for_session(self, session_id: str) -> List[str]:
        enabled = []
        for plat in self.platforms:
            if self.is_session_enabled_for_platform(session_id, plat):
                enabled.append(plat)
        return enabled

    def get_all_enabled_platforms(self) -> List[str]:
        return [p for p in self.platforms if self.is_platform_enabled(p)]

    def get_session_platform_config(self, session_id: str) -> Dict[str, bool]:
        return {
            plat: self.is_session_enabled_for_platform(session_id, plat)
            for plat in self.platforms
        }


# ==================== 会话游戏管理器 ====================
class SessionGamesManager:
    """
    管理每个会话的游戏列表，与公共游戏池协同。
    存储结构：{session_id: [game_name1, game_name2, ...]}
    持久化文件：sessions_games.json
    """

    def __init__(self, data_root: Path, game_pool: Dict[str, Any]):
        """
        :param data_root: 插件数据根目录
        :param game_pool: 公共游戏信息池引用（用于确保游戏名一致性）
        """
        self.data_root = Path(data_root)
        self.game_pool = game_pool
        self.file_path = self.data_root / "sessions_games.json"
        self.sessions: Dict[str, List[str]] = {}
        self._lock = asyncio.Lock()
        self._load()

    def _load(self):
        """同步加载会话游戏列表"""
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.sessions = data
            except Exception as e:
                logger.error(f"加载会话游戏列表失败: {e}")
                self.sessions = {}
        else:
            self.sessions = {}

    async def _save(self):
        """异步保存会话游戏列表"""
        async with self._lock:
            try:
                async with aiofiles.open(self.file_path, "w", encoding="utf-8") as f:
                    await f.write(
                        json.dumps(self.sessions, ensure_ascii=False, indent=2)
                    )
            except Exception as e:
                logger.error(f"保存会话游戏列表失败: {e}")

    def _ensure_session(self, session_id: str):
        if session_id not in self.sessions:
            self.sessions[session_id] = []

    async def add_game(self, session_id: str, game_name: str):
        norm_name = game_name.strip().lower()
        if norm_name not in self.game_pool:
            raise ValueError(f"游戏 '{game_name}' 不在公共池中，请先添加游戏信息")
        self._ensure_session(session_id)
        if norm_name not in self.sessions[session_id]:
            self.sessions[session_id].append(norm_name)
            await self._save()

    async def remove_game(self, session_id: str, game_name: str) -> bool:
        norm_name = game_name.strip().lower()
        if session_id in self.sessions and norm_name in self.sessions[session_id]:
            self.sessions[session_id].remove(norm_name)
            await self._save()
            return True
        return False

    def get_games(self, session_id: str) -> List[str]:
        return self.sessions.get(session_id, [])

    async def set_games(self, session_id: str, games: List[str]):
        norm_games = []
        for g in games:
            norm = g.strip().lower()
            if norm not in self.game_pool:
                raise ValueError(f"游戏 '{g}' 不在公共池中")
            norm_games.append(norm)
        self.sessions[session_id] = norm_games
        await self._save()

    def get_all_sessions(self) -> List[str]:
        return list(self.sessions.keys())
