"""
会话与权限管理模块
"""

import asyncio
from pathlib import Path
from typing import Dict, List, Any, Tuple

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
