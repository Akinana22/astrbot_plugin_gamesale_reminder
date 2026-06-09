"""
SQLite 数据库管理器（异步）
用于存储 NS 游戏基础信息和折扣周期。
支持可选传入连接对象以支持事务。
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple, Union

import aiosqlite
from astrbot.api import logger


class SQLiteManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_tables()

    def _init_tables(self):
        """同步创建表（在 __init__ 中调用，确保数据库文件存在）"""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        # 游戏基础信息表
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ns_game_info (
                internal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ns_id TEXT NOT NULL,
                name TEXT,
                manufacturer_name TEXT,
                soft_type TEXT,
                platform TEXT,
                original_regularPrice INTEGER,
                is_free INTEGER,
                hasReleased INTEGER,
                releaseDate TEXT,
                releaseDateText TEXT,
                hasTrial INTEGER,
                chinese_name TEXT,
                tags TEXT,
                changetime TEXT,
                is_active INTEGER DEFAULT 1,
                last_page INTEGER,
                last_idx INTEGER
            )
        """
        )
        # 折扣周期表
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ns_game_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                regular_price INTEGER,
                discount_price INTEGER NOT NULL,
                sale_label TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (game_id) REFERENCES ns_game_info(internal_id)
            )
        """
        )
        # 爬取状态表（每个平台+排序一条记录）
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS crawler_state (
                sort_rule TEXT NOT NULL,
                platform TEXT NOT NULL,
                current_page INTEGER NOT NULL,
                failed_page INTEGER,
                pages_count TEXT,
                total_before INTEGER NOT NULL,
                is_completed INTEGER DEFAULT 0,
                is_crawling INTEGER DEFAULT 0,
                last_crawl_time TEXT,
                PRIMARY KEY (sort_rule, platform)
            )
        """
        )
        # 认证信息表
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ns_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                auth_token TEXT,
                client_id TEXT,
                updated_at TEXT
            )
        """
        )
        # 愿望单表（NS 平台）
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ns_wishlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(game_id, group_id, user_id),
                FOREIGN KEY (game_id) REFERENCES ns_game_info(internal_id)
            )
        """
        )
        # 图片缓存信息
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS image_cache (
                file_name TEXT PRIMARY KEY,
                data_version TEXT NOT NULL
            )
        """
        )

        # 创建索引
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sales_game ON ns_game_sales(game_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_sales_time ON ns_game_sales(start_time, end_time)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_ns_id ON ns_game_info(ns_id, is_active)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_platform_released_page ON ns_game_info(platform, hasReleased, last_page)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_game_platform_released_date_page ON ns_game_info(platform, hasReleased, releaseDate, last_page)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_wishlist_game ON ns_wishlist(game_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_wishlist_user ON ns_wishlist(group_id, user_id)"
        )

        conn.commit()
        conn.close()
        logger.debug("SQLite 表初始化完成")

    # -------------------- 辅助方法：获取连接或使用传入连接 --------------------
    async def _get_connection(self, conn: Optional[aiosqlite.Connection] = None):
        """如果 conn 为 None，返回一个新连接；否则返回 None（表示使用传入的连接）"""
        if conn is None:
            return await aiosqlite.connect(str(self.db_path))
        return None

    # -------------------- 游戏基础信息操作 --------------------
    async def get_or_create_game(
        self, ns_id: str, platform: str, conn: Optional[aiosqlite.Connection] = None
    ) -> int:
        """根据 ns_id 和 platform 获取或创建游戏记录。如果提供了 conn，则使用该连接（用于事务）"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_or_create_game_impl(ns_id, platform, _conn)
        else:
            return await self._get_or_create_game_impl(ns_id, platform, conn)

    async def _get_or_create_game_impl(
        self, ns_id: str, platform: str, conn: aiosqlite.Connection
    ) -> int:
        cursor = await conn.execute(
            "SELECT internal_id FROM ns_game_info WHERE ns_id = ? AND is_active = 1",
            (ns_id,),
        )
        row = await cursor.fetchone()
        if row:
            return row[0]
        else:
            cursor = await conn.execute(
                "INSERT INTO ns_game_info (ns_id, platform, is_active, changetime) VALUES (?, ?, 1, ?)",
                (ns_id, platform, datetime.now().isoformat()),
            )
            await conn.commit()
            return cursor.lastrowid

    async def update_game_info(
        self, internal_id: int, conn: Optional[aiosqlite.Connection] = None, **fields
    ):
        """更新游戏信息，只更新传入的字段，同时更新 changetime"""
        if not fields:
            return
        fields["changetime"] = datetime.now().isoformat()
        set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
        values = list(fields.values()) + [internal_id]

        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                await _conn.execute(
                    f"UPDATE ns_game_info SET {set_clause} WHERE internal_id = ?",
                    values,
                )
                await _conn.commit()
        else:
            await conn.execute(
                f"UPDATE ns_game_info SET {set_clause} WHERE internal_id = ?", values
            )
            await conn.commit()

    async def get_game_by_ns_id(
        self, ns_id: str, conn: Optional[aiosqlite.Connection] = None
    ) -> Optional[Dict]:
        """获取游戏的完整信息（最新活跃记录）"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_game_by_ns_id_impl(ns_id, _conn)
        else:
            return await self._get_game_by_ns_id_impl(ns_id, conn)

    async def _get_game_by_ns_id_impl(
        self, ns_id: str, conn: aiosqlite.Connection
    ) -> Optional[Dict]:
        cursor = await conn.execute(
            "SELECT * FROM ns_game_info WHERE ns_id = ? AND is_active = 1", (ns_id,)
        )
        row = await cursor.fetchone()
        if row:
            columns = [desc[0] for desc in cursor.description]
            return dict(zip(columns, row))
        return None

    # -------------------- 折扣周期操作 --------------------
    async def add_sale_period(
        self,
        game_id: int,
        regular_price: int,
        discount_price: int,
        sale_label: str,
        start_time: str,
        end_time: str,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> bool:
        """添加折扣周期，如果已存在相同 (game_id, start_time, end_time) 则跳过"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._add_sale_period_impl(
                    game_id,
                    regular_price,
                    discount_price,
                    sale_label,
                    start_time,
                    end_time,
                    _conn,
                )
        else:
            return await self._add_sale_period_impl(
                game_id,
                regular_price,
                discount_price,
                sale_label,
                start_time,
                end_time,
                conn,
            )

    async def _add_sale_period_impl(
        self,
        game_id: int,
        regular_price: int,
        discount_price: int,
        sale_label: str,
        start_time: str,
        end_time: str,
        conn: aiosqlite.Connection,
    ) -> bool:
        cursor = await conn.execute(
            "SELECT id FROM ns_game_sales WHERE game_id = ? AND start_time = ? AND end_time = ?",
            (game_id, start_time, end_time),
        )
        if await cursor.fetchone():
            logger.debug(f"折扣周期已存在，跳过插入: game_id={game_id}")
            return False
        await conn.execute(
            "INSERT INTO ns_game_sales (game_id, regular_price, discount_price, sale_label, start_time, end_time) VALUES (?, ?, ?, ?, ?, ?)",
            (game_id, regular_price, discount_price, sale_label, start_time, end_time),
        )
        await conn.commit()
        return True

    async def get_active_sale_games(
        self, now: str, conn: Optional[aiosqlite.Connection] = None
    ) -> List[str]:
        """获取当前正在打折的游戏 ns_id 列表"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_active_sale_games_impl(now, _conn)
        else:
            return await self._get_active_sale_games_impl(now, conn)

    async def _get_active_sale_games_impl(
        self, now: str, conn: aiosqlite.Connection
    ) -> List[str]:
        cursor = await conn.execute(
            "SELECT DISTINCT g.ns_id FROM ns_game_sales s JOIN ns_game_info g ON s.game_id = g.internal_id WHERE s.start_time <= ? AND s.end_time >= ?",
            (now, now),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def is_sale_active(
        self, game_id: int, now: str, conn: Optional[aiosqlite.Connection] = None
    ) -> bool:
        """检查游戏是否当前正在打折"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._is_sale_active_impl(game_id, now, _conn)
        else:
            return await self._is_sale_active_impl(game_id, now, conn)

    async def _is_sale_active_impl(
        self, game_id: int, now: str, conn: aiosqlite.Connection
    ) -> bool:
        cursor = await conn.execute(
            "SELECT 1 FROM ns_game_sales WHERE game_id = ? AND start_time <= ? AND end_time >= ? LIMIT 1",
            (game_id, now, now),
        )
        return await cursor.fetchone() is not None

    async def get_discount_details(
        self, game_id: int, now: str, conn: Optional[aiosqlite.Connection] = None
    ) -> Optional[Tuple[int, str, str]]:
        """获取游戏当前折扣的详情，返回 (discount_price, sale_label, end_time) 或 None"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_discount_details_impl(game_id, now, _conn)
        else:
            return await self._get_discount_details_impl(game_id, now, conn)

    async def _get_discount_details_impl(
        self, game_id: int, now: str, conn: aiosqlite.Connection
    ) -> Optional[Tuple[int, str, str]]:
        cursor = await conn.execute(
            "SELECT discount_price, sale_label, end_time FROM ns_game_sales WHERE game_id = ? AND start_time <= ? AND end_time >= ? ORDER BY start_time DESC LIMIT 1",
            (game_id, now, now),
        )
        row = await cursor.fetchone()
        if row:
            return (row[0], row[1], row[2])
        return None

    # -------------------- 爬取状态管理 --------------------
    async def get_crawler_state(
        self, sort_rule: str, platform: str, conn: Optional[aiosqlite.Connection] = None
    ) -> Dict:
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_crawler_state_impl(sort_rule, platform, _conn)
        else:
            return await self._get_crawler_state_impl(sort_rule, platform, conn)

    async def _get_crawler_state_impl(
        self, sort_rule: str, platform: str, conn: aiosqlite.Connection
    ) -> Dict:
        cursor = await conn.execute(
            "SELECT current_page, failed_page, pages_count, total_before, is_completed, is_crawling, last_crawl_time FROM crawler_state WHERE sort_rule = ? AND platform = ?",
            (sort_rule, platform),
        )
        row = await cursor.fetchone()
        if row:
            return {
                "current_page": row[0],
                "failed_page": row[1],
                "pages_count": json.loads(row[2]) if row[2] else {},
                "total_before": row[3],
                "is_completed": row[4],
                "is_crawling": row[5],
                "last_crawl_time": row[6],
            }
        else:
            return {
                "current_page": 0,
                "failed_page": None,
                "pages_count": {},
                "total_before": 0,
                "is_completed": 1,
                "is_crawling": 0,
                "last_crawl_time": None,
            }

    async def update_crawler_state(
        self,
        sort_rule: str,
        platform: str,
        conn: Optional[aiosqlite.Connection] = None,
        **kwargs,
    ):
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                await self._update_crawler_state_impl(
                    sort_rule, platform, _conn, **kwargs
                )
        else:
            await self._update_crawler_state_impl(sort_rule, platform, conn, **kwargs)

    async def _update_crawler_state_impl(
        self, sort_rule: str, platform: str, conn: aiosqlite.Connection, **kwargs
    ):
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [sort_rule, platform]
        await conn.execute(
            f"UPDATE crawler_state SET {set_clause} WHERE sort_rule = ? AND platform = ?",
            values,
        )
        if conn.total_changes == 0:
            columns = ", ".join(kwargs.keys())
            placeholders = ", ".join(["?"] * len(kwargs))
            await conn.execute(
                f"INSERT INTO crawler_state (sort_rule, platform, {columns}) VALUES (?, ?, {placeholders})",
                (sort_rule, platform) + tuple(kwargs.values()),
            )
        await conn.commit()

    async def reset_crawler_state(
        self, sort_rule: str, platform: str, conn: Optional[aiosqlite.Connection] = None
    ):
        await self.update_crawler_state(
            sort_rule,
            platform,
            conn=conn,
            current_page=0,
            failed_page=None,
            pages_count=json.dumps({}),
            total_before=0,
            is_completed=0,
        )

    async def complete_crawler_state(
        self, sort_rule: str, platform: str, conn: Optional[aiosqlite.Connection] = None
    ):
        await self.update_crawler_state(sort_rule, platform, conn=conn, is_completed=1)

    # -------------------- 新增：爬取并发控制 --------------------
    async def set_crawling_state(
        self,
        platform: str,
        is_crawling: bool,
        last_crawl_time: Optional[str] = None,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> None:
        """
        设置指定平台的爬取状态。对于 NS 平台（HAC/BEE），会同时设置两个平台的 is_crawling 为相同值。
        last_crawl_time 仅更新当前平台。
        """
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                await self._set_crawling_state_impl(
                    platform, is_crawling, last_crawl_time, _conn
                )
        else:
            await self._set_crawling_state_impl(
                platform, is_crawling, last_crawl_time, conn
            )

    async def _set_crawling_state_impl(
        self,
        platform: str,
        is_crawling: bool,
        last_crawl_time: Optional[str],
        conn: aiosqlite.Connection,
    ) -> None:
        ns_platforms = ["HAC", "BEE"]
        platforms_to_update = [platform]
        if platform in ns_platforms:
            platforms_to_update = ns_platforms
        for plat in platforms_to_update:
            if plat == platform and last_crawl_time is not None:
                await conn.execute(
                    "UPDATE crawler_state SET is_crawling = ?, last_crawl_time = ? WHERE sort_rule = 'sorting-release-date' AND platform = ?",
                    (1 if is_crawling else 0, last_crawl_time, plat),
                )
            else:
                await conn.execute(
                    "UPDATE crawler_state SET is_crawling = ? WHERE sort_rule = 'sorting-release-date' AND platform = ?",
                    (1 if is_crawling else 0, plat),
                )
            if conn.total_changes == 0 and plat == platform:
                # 插入新记录（仅当前平台）
                await conn.execute(
                    "INSERT INTO crawler_state (sort_rule, platform, current_page, total_before, is_crawling, last_crawl_time) VALUES ('sorting-release-date', ?, 0, 0, ?, ?)",
                    (plat, 1 if is_crawling else 0, last_crawl_time),
                )
        await conn.commit()

    async def is_crawling_allowed(
        self, platform: str, conn: Optional[aiosqlite.Connection] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        检查指定平台是否允许开始爬取。
        对于 NS 平台（HAC/BEE），如果任一平台正在爬取，则不允许新任务。
        返回 (是否允许, 正在爬取的平台标识或 None)
        """
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._is_crawling_allowed_impl(platform, _conn)
        else:
            return await self._is_crawling_allowed_impl(platform, conn)

    async def _is_crawling_allowed_impl(
        self, platform: str, conn: aiosqlite.Connection
    ) -> Tuple[bool, Optional[str]]:
        ns_platforms = ["HAC", "BEE"]
        if platform in ns_platforms:
            placeholders = ",".join("?" for _ in ns_platforms)
            cursor = await conn.execute(
                f"SELECT platform FROM crawler_state WHERE sort_rule = 'sorting-release-date' AND platform IN ({placeholders}) AND is_crawling = 1",
                ns_platforms,
            )
            row = await cursor.fetchone()
            if row:
                return (False, row[0])
            return (True, None)
        else:
            cursor = await conn.execute(
                "SELECT is_crawling FROM crawler_state WHERE sort_rule = 'sorting-release-date' AND platform = ?",
                (platform,),
            )
            row = await cursor.fetchone()
            if row and row[0] == 1:
                return (False, platform)
            return (True, None)

    # -------------------- 请求头认证信息管理 --------------------
    async def save_credentials(
        self,
        auth_token: str,
        client_id: str,
        conn: Optional[aiosqlite.Connection] = None,
    ):
        """保存认证信息到数据库（覆盖原有）"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                await self._save_credentials_impl(auth_token, client_id, _conn)
        else:
            await self._save_credentials_impl(auth_token, client_id, conn)

    async def _save_credentials_impl(
        self, auth_token: str, client_id: str, conn: aiosqlite.Connection
    ):
        await conn.execute("DELETE FROM ns_credentials")
        await conn.execute(
            "INSERT INTO ns_credentials (auth_token, client_id, updated_at) VALUES (?, ?, ?)",
            (auth_token, client_id, datetime.now().isoformat()),
        )
        await conn.commit()

    async def get_credentials(
        self, conn: Optional[aiosqlite.Connection] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_credentials_impl(_conn)
        else:
            return await self._get_credentials_impl(conn)

    async def _get_credentials_impl(
        self, conn: aiosqlite.Connection
    ) -> Tuple[Optional[str], Optional[str]]:
        cursor = await conn.execute(
            "SELECT auth_token, client_id FROM ns_credentials ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row:
            return row[0], row[1]
        return None, None

    # -------------------- 愿望单操作 --------------------
    async def add_to_wishlist(
        self,
        game_id: int,
        group_id: str,
        user_id: str,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> bool:
        """添加游戏到用户愿望单，如果已存在则返回 False"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._add_to_wishlist_impl(
                    game_id, group_id, user_id, _conn
                )
        else:
            return await self._add_to_wishlist_impl(game_id, group_id, user_id, conn)

    async def _add_to_wishlist_impl(
        self, game_id: int, group_id: str, user_id: str, conn: aiosqlite.Connection
    ) -> bool:
        try:
            await conn.execute(
                "INSERT INTO ns_wishlist (game_id, group_id, user_id) VALUES (?, ?, ?)",
                (game_id, group_id, user_id),
            )
            await conn.commit()
            return True
        except sqlite3.IntegrityError:
            logger.debug(f"重复关注: game={game_id}, group={group_id}, user={user_id}")
            return False

    async def remove_from_wishlist(
        self,
        game_id: int,
        group_id: str,
        user_id: str,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> bool:
        """从愿望单移除游戏，返回是否删除成功"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._remove_from_wishlist_impl(
                    game_id, group_id, user_id, _conn
                )
        else:
            return await self._remove_from_wishlist_impl(
                game_id, group_id, user_id, conn
            )

    async def _remove_from_wishlist_impl(
        self, game_id: int, group_id: str, user_id: str, conn: aiosqlite.Connection
    ) -> bool:
        cursor = await conn.execute(
            "DELETE FROM ns_wishlist WHERE game_id = ? AND group_id = ? AND user_id = ?",
            (game_id, group_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def get_user_wishlist(
        self, group_id: str, user_id: str, conn: Optional[aiosqlite.Connection] = None
    ) -> List[Dict]:
        """获取用户的愿望单（游戏信息，包含 ns_id）"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_user_wishlist_impl(group_id, user_id, _conn)
        else:
            return await self._get_user_wishlist_impl(group_id, user_id, conn)

    async def _get_user_wishlist_impl(
        self, group_id: str, user_id: str, conn: aiosqlite.Connection
    ) -> List[Dict]:
        cursor = await conn.execute(
            """
            SELECT g.internal_id, g.ns_id, g.name, g.chinese_name
            FROM ns_wishlist w
            JOIN ns_game_info g ON w.game_id = g.internal_id
            WHERE w.group_id = ? AND w.user_id = ?
            ORDER BY w.created_at DESC
            """,
            (group_id, user_id),
        )
        rows = await cursor.fetchall()
        return [
            {
                "internal_id": row[0],
                "ns_id": row[1],
                "name": row[2],
                "chinese_name": row[3],
            }
            for row in rows
        ]

    async def get_game_subscribers(
        self, game_id: int, conn: Optional[aiosqlite.Connection] = None
    ) -> List[Tuple[str, str]]:
        """获取关注某游戏的所有用户（group_id, user_id）"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_game_subscribers_impl(game_id, _conn)
        else:
            return await self._get_game_subscribers_impl(game_id, conn)

    async def _get_game_subscribers_impl(
        self, game_id: int, conn: aiosqlite.Connection
    ) -> List[Tuple[str, str]]:
        cursor = await conn.execute(
            "SELECT group_id, user_id FROM ns_wishlist WHERE game_id = ?",
            (game_id,),
        )
        rows = await cursor.fetchall()
        return [(row[0], row[1]) for row in rows]

    # -------------------- 发售列表 --------------------
    async def get_games_by_release_month_year(
        self,
        month: int,
        year: int,
        limit: int = 50,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> List[Dict]:
        """根据发售月份和年份获取游戏列表（基于 releaseDateText 字段提取）"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_games_by_release_month_year_impl(
                    month, year, limit, _conn
                )
        else:
            return await self._get_games_by_release_month_year_impl(
                month, year, limit, conn
            )

    async def _get_games_by_release_month_year_impl(
        self, month: int, year: int, limit: int, conn: aiosqlite.Connection
    ) -> List[Dict]:
        # 使用正则表达式在 SQLite 中匹配年份和月份（注意 SQLite 默认没有正则函数，需要加载扩展或使用 LIKE）
        # 这里采用简单方法：先取出所有 is_active=1 且 releaseDateText 不为空的记录，再在 Python 中过滤
        # 因为数据量不会太大（一般几千条），性能可接受。
        cursor = await conn.execute(
            "SELECT internal_id, name, chinese_name, releaseDateText FROM ns_game_info WHERE is_active = 1 AND releaseDateText IS NOT NULL AND releaseDateText != ''"
        )
        rows = await cursor.fetchall()

        import re

        games = []
        for row in rows:
            internal_id, name, chn_name, text = row
            # 提取年份
            year_match = re.search(r"(\d{4})年", text)
            if not year_match:
                continue
            game_year = int(year_match.group(1))
            if game_year != year:
                continue
            # 提取月份
            month_match = re.search(r"(\d{1,2})月", text)
            if not month_match:
                continue
            game_month = int(month_match.group(1))
            if game_month != month:
                continue
            games.append(
                {
                    "internal_id": internal_id,
                    "name": name,
                    "chinese_name": chn_name,
                    "releaseDateText": text,
                }
            )
        # 按 releaseDateText 排序（简单按字符串排序，因为日期格式 YYYY年MM月DD日 可比较）
        games.sort(key=lambda x: x["releaseDateText"])
        return games[:limit]

    # -------------------- 图片缓存信息操作 --------------------
    async def save_cached_image(
        self,
        file_name: str,
        data_version: str,
        conn: Optional[aiosqlite.Connection] = None,
    ) -> None:
        """保存或更新图片缓存"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                await self._save_cached_image_impl(file_name, data_version, _conn)
        else:
            await self._save_cached_image_impl(file_name, data_version, conn)

    async def _save_cached_image_impl(
        self, file_name: str, data_version: str, conn: aiosqlite.Connection
    ) -> None:
        await conn.execute(
            "INSERT OR REPLACE INTO image_cache (file_name, data_version) VALUES (?, ?)",
            (file_name, data_version),
        )
        await conn.commit()
        logger.debug(f"缓存已更新: {file_name} -> {data_version}")

    async def get_cached_image(
        self, file_name: str, conn: Optional[aiosqlite.Connection] = None
    ) -> Optional[str]:
        """获取缓存的 data_version，若无则返回 None"""
        if conn is None:
            async with aiosqlite.connect(str(self.db_path)) as _conn:
                return await self._get_cached_image_impl(file_name, _conn)
        else:
            return await self._get_cached_image_impl(file_name, conn)

    async def _get_cached_image_impl(
        self, file_name: str, conn: aiosqlite.Connection
    ) -> Optional[str]:
        cursor = await conn.execute(
            "SELECT data_version FROM image_cache WHERE file_name = ?", (file_name,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
