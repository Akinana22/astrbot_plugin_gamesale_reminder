"""
游戏折扣提醒插件
支持多平台游戏折扣监控，当前已实现任天堂平台爬虫框架（SQLite 存储，自动 credential 获取，排序固定为从旧到新）。
"""

import re
import asyncio
import random
import shutil
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import aiohttp
import aiosqlite
import sqlean
from pypinyin import pinyin, Style
import pykakasi

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.core.agent.tool import ToolSet
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import astrbot.api.message_components as Comp

from .common_utils import (
    ImageGenerator,
    AITool,
    FileNameGenerator,
)

from .session_managers import (
    SessionUtils,
    PermissionManager,
    PlatformManager,
    SessionGamesManager,
)

from .time_utils import (
    init_timezone_config,
    utc_now,
    utc_now_str,
    convert_to_utc,
    convert_to_utc_str,
    convert_to_config_tz,
    now_in_config_tz,
    DateParser,
    TaskScheduler,
    RemainingTimeCalculator,
)

from .ns_credential_fetcher import NSCredentialFetcher
from .sqlite_manager import SQLiteManager


class DiscountReminderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.name = "astrbpt_plugin_gamesale_reminder"

        # 初始化时区配置
        tz_config = self.config.get("timezone", "+8")
        tz_mode, tz_value = init_timezone_config(tz_config)

        # 数据目录
        self.data_root = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        logger.info(f"数据目录为: {self.data_root}")
        self.data_root.mkdir(parents=True, exist_ok=True)
        # 复制 Logo 文件
        src_logo = Path(__file__).parent / "logo"
        dst_logo = self.data_root / "logo"
        if src_logo.exists() and src_logo.is_dir():
            dst_logo.mkdir(parents=True, exist_ok=True)
            for item in src_logo.iterdir():
                if item.is_file():
                    shutil.copy2(item, dst_logo / item.name)
            logger.info(f"✅ 已复制 Logo 文件到 {dst_logo}")
        else:
            logger.warning("⚠️ 插件源码中未找到 logo 文件夹")

        # SQLite 管理器
        self.db = SQLiteManager(self.data_root / "games.db")

        # 其他组件
        self.image_gen = ImageGenerator(
            self.data_root, render_func=self.html_render, img_subdir="pushpng"
        )
        self.permission_mgr = PermissionManager(self.config, context)
        self.platform_mgr = PlatformManager(
            self.config, platforms=["ns", "steam", "ps", "xbox"]
        )
        self.ai_tool = AITool(context)
        self.scheduler = TaskScheduler(tz_mode, tz_value)  # 传入时区配置

        # 异步加载缓存
        self._cached_auth: Optional[str] = None
        self._cached_client: Optional[str] = None
        asyncio.create_task(self._load_initial_credentials())

        # 爬取任务控制
        self._crawler_running = False

        # 任务跟踪
        self._running_tasks: List[asyncio.Task] = []

        # 用户搜索结果暂存
        self.user_search_results: Dict[str, Any] = {}

        # 启动调度器
        asyncio.create_task(self._init_scheduler())

    # -------------------- 任务跟踪辅助方法 --------------------
    async def _run_task(self, coro, *args, **kwargs):
        """
        包装协程，将其加入运行中任务列表，等待完成后自动移除
        """
        task = asyncio.create_task(coro(*args, **kwargs))
        self._running_tasks.append(task)
        try:
            return await task
        finally:
            if task in self._running_tasks:
                self._running_tasks.remove(task)

    # -------------------- 认证信息管理 --------------------
    async def _load_initial_credentials(self):
        auth, client = await self.db.get_credentials()
        self._cached_auth = auth
        self._cached_client = client

    async def _get_credential(
        self, credential_type: str, force_refresh: bool = False
    ) -> Optional[str]:
        """
        获取认证信息（auth_token 或 client_id），内部维护内存缓存。
        :param credential_type: 'auth' 或 'client'
        :param force_refresh: 是否强制刷新（同时刷新两者）
        """
        # 如果强制刷新，重新获取并更新缓存
        if force_refresh:
            logger.info("强制刷新 credentials，启动浏览器获取...")
            fetcher = NSCredentialFetcher(max_retries=2)
            new_auth, new_client = await fetcher.get_credentials(self.data_root)
            if new_auth and new_client:
                self._cached_auth = new_auth
                self._cached_client = new_client
                await self.db.save_credentials(new_auth, new_client)
                logger.info("成功获取并保存新 credentials")
                return new_auth if credential_type == "auth" else new_client
            else:
                logger.error("强制刷新 credentials 失败")
                return None

        # 非强制刷新：优先使用缓存
        if credential_type == "auth":
            if self._cached_auth:
                return self._cached_auth
        else:
            if self._cached_client:
                return self._cached_client

        # 缓存未命中，从数据库加载
        auth, client = await self.db.get_credentials()
        if credential_type == "auth":
            if auth:
                self._cached_auth = auth
                return auth
        else:
            if client:
                self._cached_client = client
                return client

        # 数据库也没有，强制刷新
        logger.info(f"数据库中没有有效的 {credential_type}，启动浏览器获取...")
        return await self._get_credential(credential_type, force_refresh=True)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        payload: Any = None,
        content_type: Optional[str] = None,
        headers: Optional[Dict] = None,
        max_retries: int = 2,
        retry_delay: int = 5,
    ) -> Tuple[Optional[Dict], int]:
        """
        通用 HTTP 请求，仅处理重试（非 401 错误），遇到 401 直接返回失败。
        :param method: HTTP 方法
        :param url: 请求 URL
        :param params: URL 查询参数
        :param payload: 请求体数据（可以是 dict、str、bytes 等）
        :param content_type: 请求体 Content-Type，如 'application/json'，会自动设置头
        :param headers: 额外请求头
        :param max_retries: 非 401 错误的最大重试次数
        :param retry_delay: 重试间隔（秒）
        :return: (data, status_code)
        """
        retry_count = 0
        while True:
            req_headers = headers.copy() if headers else {}
            if content_type and "Content-Type" not in req_headers:
                req_headers["Content-Type"] = content_type

            # 准备请求参数
            request_kwargs = {
                "method": method,
                "url": url,
                "params": params,
                "headers": req_headers,
            }
            if payload is not None:
                # 如果 payload 是字典且 content_type 是 application/json，使用 json 参数发送
                if isinstance(payload, dict) and content_type == "application/json":
                    request_kwargs["json"] = payload
                else:
                    request_kwargs["data"] = payload

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(**request_kwargs, timeout=30) as resp:
                        status = resp.status
                        if status == 200:
                            data = await resp.json()
                            return data, status
                        # 401 直接返回，由上层处理刷新
                        if status == 401:
                            return None, status
                        # 其他非 200 状态码，记录并可能重试
                        if retry_count < max_retries:
                            logger.warning(
                                f"请求失败，状态码 {status}，重试 {retry_count+1}/{max_retries}"
                            )
                            retry_count += 1
                            await asyncio.sleep(retry_delay)
                            continue
                        else:
                            logger.error(f"请求失败，状态码 {status}，已达最大重试次数")
                            return None, status
            except Exception as e:
                logger.error(f"请求异常: {e}")
                if retry_count < max_retries:
                    retry_count += 1
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    return None, 0

    # --------------------任天堂平台 API 封装 --------------------
    async def _ns_search_api(
        self, page: int, platform: str = "", is_sale: bool = False
    ) -> Tuple[Optional[Dict], int]:
        """搜索 API，内部处理认证和 401 重试"""
        params = {
            "c_cgid": "software",
            "c_softType": "TITLE",
            "c_srule": "sorting-release-date",
            "c_page": page + 1,
            "siteId": "MNS",
        }
        if platform:
            params["c_labelPlatform"] = platform
        if is_sale:
            params["c_prefn2"] = "isSale"
            params["c_prefv2"] = "true"

        url = "https://store-jp.nintendo.com/mobify/proxy/api/custom/search/v1/organizations/f_ecom_bfgj_prd/search"
        # full_url = f"{url}?{urlencode(params)}"
        # logger.info(f"搜索 API 请求 URL: {full_url}")
        logger.info(f"正在搜索第 {page+1} 页游戏")

        # 获取认证
        auth_token = await self._get_credential("auth")
        if not auth_token:
            return None, 401
        headers = {"Authorization": auth_token}

        data, status = await self._request_with_retry(
            method="GET",
            url=url,
            params=params,
            headers=headers,
            max_retries=2,
            retry_delay=5,
        )
        if status == 401:
            logger.warning("搜索 API 返回 401，尝试刷新凭证...")
            new_token = await self._get_credential("auth", force_refresh=True)
            if new_token:
                headers["Authorization"] = new_token
                data, status = await self._request_with_retry(
                    method="GET",
                    url=url,
                    params=params,
                    headers=headers,
                    max_retries=2,
                    retry_delay=5,
                )
            else:
                logger.error("刷新 auth_token 失败")
        return data, status

    async def _ns_detail_api(self, ns_id: str) -> Tuple[Optional[Dict], int]:
        """详情 API，内部处理认证和 401 重试"""
        url = f"https://store-jp.nintendo.com/mobify/proxy/api/product/shopper-products/v1/organizations/f_ecom_bfgj_prd/products/{ns_id}?currency=JPY&locale=ja-JP&siteId=MNS"
        auth_token = await self._get_credential("auth")
        if not auth_token:
            return None, 401
        headers = {"Authorization": auth_token}

        data, status = await self._request_with_retry(
            method="GET", url=url, headers=headers, max_retries=2, retry_delay=5
        )
        if status == 401:
            logger.warning("详情 API 返回 401，尝试刷新凭证...")
            new_token = await self._get_credential("auth", force_refresh=True)
            if new_token:
                headers["Authorization"] = new_token
                data, status = await self._request_with_retry(
                    method="GET", url=url, headers=headers, max_retries=2, retry_delay=5
                )
            else:
                logger.error("刷新 auth_token 失败")
        return data, status

    async def _ns_graphql_api(self, ns_uids: List[int]) -> Tuple[Optional[Dict], int]:
        """GraphQL 折扣详情 API，内部处理认证和 401 重试"""
        url = "https://wb.lp1.savanna.srv.nintendo.net/graphql"
        payload = (
            '{"operationName":"GetLatestPrices","variables":{"nsUids":'
            + json.dumps(ns_uids)
            + '},"query":"query GetLatestPrices($nsUids: [NsUid!]!, $idToken: String) @inContext(country: \\"JP\\", language: \\"ja\\", shopId: 3) {\\n  prices(nsUids: $nsUids, idToken: $idToken) {\\n    discountPrice {\\n      rawValue\\n      startDatetime\\n      endDatetime\\n      __typename\\n    }\\n    regularPrice {\\n      rawValue\\n      __typename\\n    }\\n    nsUid\\n    __typename\\n  }\\n}"}'
        )
        headers = {"Content-Type": "application/json"}
        client_id = await self._get_credential("client")
        if not client_id:
            return None, 499
        headers["x-nintendo-savanna-client-id"] = client_id

        data, status = await self._request_with_retry(
            method="POST",
            url=url,
            payload=payload,
            content_type="application/json",
            headers=headers,
            max_retries=2,
            retry_delay=5,
        )
        if status == 401:
            logger.warning("GraphQL API 返回 401，尝试刷新凭证...")
            new_client = await self._get_credential("client", force_refresh=True)
            if new_client:
                headers["x-nintendo-savanna-client-id"] = new_client
                data, status = await self._request_with_retry(
                    method="POST",
                    url=url,
                    payload=payload,
                    content_type="application/json",
                    headers=headers,
                    max_retries=2,
                    retry_delay=5,
                )
            else:
                logger.error("刷新 client_id 失败")
        return data, status

    # --------------------任天堂平台基础信息爬取 --------------------
    async def _crawl_ns_basic_info(
        self, platform: str, incremental: bool = False
    ) -> str:
        """
        爬取任天堂游戏基础信息
        :param platform: 平台代码（HAC/BEE）
        :param incremental: True=增量爬取（从已发售游戏最大 last_page 开始），False=全量爬取（根据断点，可以重置进度）
        """
        sort_rule = "sorting-release-date"
        base_interval = self.config.get("ns_crawler_interval", 6)
        random_range = self.config.get("ns_crawler_random_range", 2)
        if random_range < 0:
            random_range = 0

        # ----- 并发控制：检查是否允许爬取 -----
        allowed, blocking = await self.db.is_crawling_allowed(platform)
        if not allowed:
            logger.warning(f"平台 {platform} 被阻止爬取，当前 {blocking} 正在爬取")
            return f"❌ 平台 {blocking} 正在爬取，请稍后再试"

        await self.db.set_crawling_state(platform, True)
        logger.info(f"平台 {platform} 爬取状态已设置为正在爬取")

        try:
            # ----- 确定起始页 -----
            if incremental:
                async with aiosqlite.connect(str(self.db.db_path)) as conn:
                    # 1. 获取最新发售日期
                    cursor = await conn.execute(
                        "SELECT MAX(releaseDate) FROM ns_game_info WHERE platform = ? AND hasReleased = 1 AND releaseDate IS NOT NULL",
                        (platform,),
                    )
                    row = await cursor.fetchone()
                    max_release_date = row[0]

                    if max_release_date:
                        # 2. 获取该发售日期下的最小 last_page
                        cursor = await conn.execute(
                            "SELECT MIN(last_page) FROM ns_game_info WHERE platform = ? AND releaseDate = ? AND hasReleased = 1",
                            (platform, max_release_date),
                        )
                        row = await cursor.fetchone()
                        start_page = row[0] if row[0] is not None else 0
                    else:
                        start_page = 0

                if start_page == 0:
                    logger.info(
                        f"平台 {platform} 无已发售游戏记录，将从第 1 页开始增量爬取"
                    )
                else:
                    logger.info(
                        f"平台 {platform} 最新发售日期 {max_release_date} 的最小页码为 {start_page+1}，从此页开始增量爬取"
                    )
                page = start_page
                # 增量模式不使用 crawler_state，直接进入循环
            else:
                # 全量模式：使用 crawler_state 状态，重置并从头开始
                state = await self.db.get_crawler_state(sort_rule, platform)
                if state["is_completed"] == 1:
                    logger.info(f"平台 {platform} 基础信息已完整，将从头开始")
                    await self.db.reset_crawler_state(sort_rule, platform)
                    state = await self.db.get_crawler_state(sort_rule, platform)

                current_page = state["current_page"]
                failed_page = state["failed_page"]
                total_before = state["total_before"]
                pages_count = state["pages_count"]

                if failed_page is not None:
                    start_page = failed_page
                    logger.info(f"检测到上次失败页 {failed_page}，从该页重试")
                    await self.db.update_crawler_state(
                        sort_rule, platform, failed_page=None
                    )
                else:
                    start_page = current_page

                page = start_page
                logger.info(
                    f"开始全量爬取任天堂游戏基础信息，平台 {platform}，起始页 {page+1}"
                )

            # ----- 公共爬取循环 -----
            while True:
                logger.info(f"开始抓取第 {page+1} 页")
                data, status = await self._ns_search_api(page, platform)
                if data is None:
                    if status == 401:
                        error_msg = f"无法获取第 {page+1} 页（认证失败）"
                        logger.error(error_msg)
                        return error_msg
                    else:
                        retry = 0
                        while retry < 2:
                            logger.warning(
                                f"第 {page+1} 页请求失败，状态码 {status}，重试 {retry+1}/2"
                            )
                            await asyncio.sleep(5)
                            data, status = await self._ns_search_api(page, platform)
                            if data is not None:
                                break
                            retry += 1
                        if data is None:
                            if not incremental:
                                # 全量模式记录失败页
                                await self.db.update_crawler_state(
                                    sort_rule, platform, failed_page=page
                                )
                            error_msg = f"第 {page+1} 页重试2次后仍失败"
                            logger.error(error_msg)
                            return error_msg

                result_products = data.get("resultProducts", [])
                paging_info = data.get("pagingInfo", {})
                max_page = paging_info.get("maxPage", 0)
                max_game = paging_info.get("totalCount", 0)
                if max_page is None:
                    error_msg = "返回数据缺少 maxPage"
                    logger.error(error_msg)
                    return error_msg

                logger.info(
                    f"第 {page+1} 页解析结果: 获取到 {len(result_products)} 款游戏 | 总页数 {max_page+1}，总计 {max_game} 款游戏"
                )

                for idx, product in enumerate(result_products):
                    ns_id = product.get("id")
                    name = product.get("name")
                    manufacturer_name = product.get("manufacturerName")
                    soft_type = product.get("softType")
                    is_free = product.get("isFree")
                    if not ns_id:
                        continue
                    internal_id = await self.db.get_or_create_game(ns_id, platform)
                    game_info = {
                        "name": name,
                        "manufacturer_name": manufacturer_name,
                        "soft_type": soft_type,
                        "is_free": is_free,
                        "last_page": page,
                        "last_idx": idx,
                    }
                    logger.info(
                        f"第 {idx+1} 个游戏，编号：{ns_id}，名称：{name}，制作商：{manufacturer_name}，软件类型：{soft_type}，是否免费：{is_free}"
                    )
                    await self.db.update_game_info(
                        internal_id,
                        **{k: v for k, v in game_info.items() if v is not None},
                    )
                    # 补充详情
                    detail_data, detail_status = await self._ns_detail_api(ns_id)
                    if detail_data:
                        original_regularPrice = detail_data.get(
                            "c_original_regularPrice"
                        )
                        hasReleased = detail_data.get("c_hasReleased")
                        releaseDate = detail_data.get("c_releaseDate")
                        releaseDateText = detail_data.get("c_releaseDateText")
                        hasTrial = detail_data.get("c_hasTrial")
                        is_digital = detail_data.get("c_original_isDigital", False)
                        extra_fields = {
                            "original_regularPrice": original_regularPrice,
                            "hasReleased": hasReleased,
                            "releaseDate": releaseDate,
                            "releaseDateText": releaseDateText,
                            "hasTrial": hasTrial,
                            "is_active": is_digital,
                        }
                        logger.info(
                            f"补充信息 | 原价：{original_regularPrice}，是否已发售：{hasReleased}，发售日期：{releaseDate}，发售日期文本：{releaseDateText}，是否有试用版：{hasTrial}，是否为数字版：{is_digital}"
                        )
                        await self.db.update_game_info(
                            internal_id,
                            **{k: v for k, v in extra_fields.items() if v is not None},
                        )
                    else:
                        logger.warning(
                            f"获取游戏 {ns_id} 详情失败，状态码 {detail_status}"
                        )
                    await asyncio.sleep(0.5)

                # ----- 更新爬取状态（仅全量模式）-----
                if not incremental:
                    page_count = len(result_products)
                    pages_count[str(page)] = page_count
                    new_total_before = total_before + page_count
                    await self.db.update_crawler_state(
                        sort_rule,
                        platform,
                        current_page=page,
                        pages_count=json.dumps(pages_count),
                        total_before=new_total_before,
                    )
                    total_before = new_total_before

                if page >= max_page:
                    if not incremental:
                        await self.db.complete_crawler_state(sort_rule, platform)
                    logger.info("爬取完成")
                    success_msg = f"✅ {platform} 平台游戏基础信息爬取完成！"
                    return success_msg

                wait_seconds = (
                    base_interval + random.randint(0, random_range)
                    if random_range > 0
                    else base_interval
                )
                await asyncio.sleep(wait_seconds)
                page += 1

        except asyncio.CancelledError:
            logger.info("爬取任务被取消")
            return "❌ 爬取任务被取消"
        except Exception as e:
            error_msg = f"❌ 爬取异常: {e}"
            logger.error(error_msg, exc_info=True)
            return error_msg
        finally:
            # 无论成功或失败，清除爬取标志，并记录最后爬取时间
            await self.db.set_crawling_state(
                platform, False, last_crawl_time=datetime.now().isoformat()
            )
            logger.info(f"平台 {platform} 爬取状态已清除")

    # --------------------任天堂平台折扣信息爬取 --------------------
    async def _crawl_ns_sales_info(self, platform: str) -> str:
        """
        增量爬取任天堂平台折扣信息，逐页处理并即时写入数据库。
        """
        # ----- 并发控制：检查是否允许爬取 -----
        allowed, blocking = await self.db.is_crawling_allowed(platform)
        if not allowed:
            logger.warning(f"平台 {platform} 被阻止爬取，当前 {blocking} 正在爬取")
            return f"❌ 平台 {blocking} 正在爬取，请稍后再试"

        await self.db.set_crawling_state(platform, True)
        logger.info(f"平台 {platform} 折扣爬取状态已设置为正在爬取")

        base_interval = self.config.get("ns_sales_crawler_interval", 6)
        random_range = self.config.get("ns_sales_crawler_random_range", 2)
        if random_range < 0:
            random_range = 0

        try:
            # 获取当前所有活跃折扣游戏的 ns_id 集合（用于快速判断新增）
            now_utc = utc_now_str()
            active_ids = set(await self.db.get_active_sale_games(now_utc))
            logger.info(f"当前活跃折扣游戏数量: {len(active_ids)}")

            page = 0
            total_new = 0

            while True:
                data, status = await self._ns_search_api(page, platform, is_sale=True)
                if data is None:
                    if status == 401:
                        error_msg = f"无法获取第 {page+1} 页（认证失败）"
                        logger.error(error_msg)
                        return error_msg
                    else:
                        retry = 0
                        while retry < 2:
                            logger.warning(
                                f"第 {page+1} 页请求失败，状态码 {status}，重试 {retry+1}/2"
                            )
                            await asyncio.sleep(5)
                            data, status = await self._ns_search_api(
                                page, platform, is_sale=True
                            )
                            if data is not None:
                                break
                            retry += 1
                        if data is None:
                            logger.error(f"第 {page+1} 页重试2次后仍失败，停止爬取")
                            break

                result_products = data.get("resultProducts", [])
                paging_info = data.get("pagingInfo", {})
                max_page = paging_info.get("maxPage", 0)

                # 提取本页中的所有游戏的信息
                page_ns_info = {}
                for product in result_products:
                    ns_id = product.get("id")
                    if ns_id:
                        page_ns_info[ns_id] = product

                page_ns_ids = list(page_ns_info.keys())

                # 筛选出不在活跃集合中的新游戏
                new_ids = [ns_id for ns_id in page_ns_ids if ns_id not in active_ids]

                if new_ids:
                    logger.info(f"第 {page+1} 页发现 {len(new_ids)} 个新折扣游戏")

                    # 分批次调用 GraphQL 获取详情并写入
                    batch_size = 5
                    for i in range(0, len(new_ids), batch_size):
                        batch = new_ids[i : i + batch_size]
                        ns_uids = [int(ns_id) for ns_id in batch]
                        logger.info(f"开始抓取{ns_uids}的折扣信息")
                        graphql_data, graphql_status = await self._ns_graphql_api(
                            ns_uids
                        )
                        if graphql_data is None:
                            if graphql_status == 499:
                                error_msg = f"未获取到认证信息"
                                logger.error(error_msg)
                                return error_msg
                            if graphql_status == 401:
                                error_msg = f"重新获取认证后仍认证失败"
                                logger.error(error_msg)
                                return error_msg
                            if graphql_data is None:
                                logger.error(
                                    f"认证通过，获取折扣详情失败，跳过本批: {batch}"
                                )
                                continue

                        prices = graphql_data.get("data", {}).get("prices", [])
                        logger.info(f"已获取到折扣信息")
                        for idx, price_info in enumerate(prices):
                            ns_uid = price_info.get("nsUid")
                            if ns_uid is None:
                                logger.error(
                                    f"GraphQL 返回数据缺少 nsUid: {price_info}"
                                )
                                continue
                            ns_id = str(ns_uid)
                            product = page_ns_info.get(ns_id)
                            if product is None:
                                logger.error(f"未在搜索结果中找到商品信息: {ns_id}")
                                continue
                            discount = price_info.get("discountPrice")
                            if not discount:
                                continue

                            regular_price = price_info.get("regularPrice", {}).get(
                                "rawValue"
                            )
                            discount_price = discount.get("rawValue")
                            start_time_str = discount.get("startDatetime")
                            end_time_str = discount.get("endDatetime")
                            sale_label = product.get("saleLabel")
                            if (
                                not discount_price
                                or not start_time_str
                                or not end_time_str
                            ):
                                logger.error(
                                    f"GraphQL 返回数据缺少应有的折扣信息: {price_info}"
                                )
                                continue

                            # 转换为 UTC 字符串
                            try:
                                start_utc_str = convert_to_utc_str(start_time_str)
                                end_utc_str = convert_to_utc_str(end_time_str)
                            except Exception as e:
                                logger.error(f"转换时间失败: {e}, 跳过游戏 {ns_id}")
                                continue
                            logger.info(
                                f"第 {i+idx+1} 个新折扣游戏 | 编号：{ns_id}，原价：{regular_price}，促销价：{discount_price}，促销力度：{sale_label}，促销时间（utc时间）：{start_time_str}-{end_time_str}"
                            )
                            # 写入数据库（事务）
                            async with aiosqlite.connect(str(self.db.db_path)) as conn:
                                await conn.execute("BEGIN")
                                try:
                                    internal_id = await self.db.get_or_create_game(
                                        ns_id, platform, conn=conn
                                    )

                                    # 如果游戏缺失名称等信息，尝试从本页的 product 中补全
                                    game = await self.db.get_game_by_ns_id(
                                        ns_id, conn=conn
                                    )
                                    if game and (not game.get("name")):
                                        # 查找 product 信息
                                        product_info = next(
                                            (
                                                p
                                                for p in result_products
                                                if p.get("id") == ns_id
                                            ),
                                            None,
                                        )
                                        if product_info:
                                            update_fields = {
                                                "name": product_info.get("name"),
                                                "manufacturer_name": product_info.get(
                                                    "manufacturerName"
                                                ),
                                                "soft_type": product_info.get(
                                                    "softType"
                                                ),
                                                "is_free": product_info.get("isFree"),
                                            }
                                            await self.db.update_game_info(
                                                internal_id,
                                                conn=conn,
                                                **{
                                                    k: v
                                                    for k, v in update_fields.items()
                                                    if v is not None
                                                },
                                            )

                                    await self.db.add_sale_period(
                                        internal_id,
                                        int(regular_price) if regular_price else None,
                                        int(discount_price),
                                        sale_label,
                                        start_utc_str,
                                        end_utc_str,
                                        conn=conn,
                                    )
                                    await conn.commit()
                                    total_new += 1
                                except Exception as e:
                                    await conn.rollback()
                                    logger.error(
                                        f"处理游戏 {ns_id} 折扣失败: {e}", exc_info=True
                                    )
                                    continue

                        # 批次间延迟
                        await asyncio.sleep(
                            base_interval + random.randint(0, random_range)
                            if random_range > 0
                            else base_interval
                        )

                    # 将新处理的 ns_id 加入活跃集合，避免同一次运行中重复处理（如果后续页面又出现）
                    active_ids.update(new_ids)

                # 更新页码并检查是否结束
                if page >= max_page:
                    break
                page += 1
                wait_seconds = (
                    base_interval + random.randint(0, random_range)
                    if random_range > 0
                    else base_interval
                )
                await asyncio.sleep(wait_seconds)

            return (
                f"✅ {platform} 平台游戏折扣信息更新完成，新增 {total_new} 个折扣周期"
            )
        finally:
            await self.db.set_crawling_state(
                platform, False, last_crawl_time=datetime.now().isoformat()
            )
            logger.info(f"平台 {platform} 折扣爬取状态已清除")

    # -------------------- 中文名填充功能 --------------------
    async def _fill_chinese_names(
        self,
        platform: str,
        batch_size: int,
        event: AstrMessageEvent,
        max_total: int = 200,
    ) -> Tuple[int, int]:
        """
        批量填充游戏中文名（使用 tool_loop_agent 分批处理）
        :param platform: 平台代码（HAC/BEE）
        :param batch_size: 每批处理的游戏数量
        :param event: 消息事件（用于获取 LLM 提供商及上下文）
        :param max_total: 最多处理总数，0 表示不限制
        :return: (成功数, 失败数)
        """

        # 1. 获取搜索工具对象
        tool_mgr = self.context.provider_manager.llm_tools
        tool_name = self.config.get("search_tool", "web_search")
        search_tool = None
        for tool in tool_mgr.func_list:
            if tool.name == tool_name:
                search_tool = tool
                break
        if not search_tool:
            logger.error(f"未找到搜索工具: {tool_name}，请确保插件 web_searcher 已启用")
            return 0, 0

        # 2. 获取 LLM 提供商 ID
        provider_id = await self.context.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        if not provider_id:
            logger.error("无法获取 LLM 提供商 ID")
            return 0, 0

        # 3. 查询缺少中文名的游戏
        async with aiosqlite.connect(str(self.db.db_path)) as conn:
            cursor = await conn.execute(
                "SELECT internal_id, name FROM ns_game_info WHERE platform = ? AND (chinese_name IS NULL OR chinese_name = '') AND is_active = 1",
                (platform,),
            )
            games = await cursor.fetchall()

        if not games:
            logger.info(f"平台 {platform} 没有需要填充中文名的游戏")
            return 0, 0

        # 限制总处理数量
        if max_total > 0 and len(games) > max_total:
            games = games[:max_total]
            logger.info(f"限制最多处理 {max_total} 个游戏")

        total = len(games)
        success_count = 0
        fail_count = 0

        # 分批处理
        for i in range(0, total, batch_size):
            batch = games[i : i + batch_size]
            batch_size_actual = len(batch)
            logger.info(f"处理第 {i//batch_size + 1} 批，共 {batch_size_actual} 个游戏")

            # 构建 prompt，列出所有游戏名
            prompt_parts = [
                "请依次为以下游戏搜索官方中文译名。对于每个游戏，遵循以下规则：\n"
                "1. 如果游戏名为日语片假名（如 スーパーマリオ），请先将其转换为英文罗马字（如 Super Mario），然后使用转换后的英文名进行搜索。\n"
                "2. 调用搜索工具时，如果返回错误或无结果，请直接返回 null，不要重试或编造信息。\n"
                "3. 如果找到官方中文译名，请返回完整的游戏名称，包括副标题、版本后缀等（例如「午夜以南：织者版」而非「午夜以南」）。\n"
                "4. 如果经过搜索后仍无法找到官方中文译名，则返回 null。\n"
                "5. 最终按顺序输出，每行格式为：序号: 中文名（若未找到则为 null）。\n"
                "6. 只输出结果，不要添加任何额外说明。\n"
            ]
            for idx, (internal_id, name) in enumerate(batch, 1):
                prompt_parts.append(f"{idx}. {name}")
            prompt = "\n".join(prompt_parts)

            # 调用 tool_loop_agent，设置足够的 max_steps（每个游戏可能需要1次工具调用+1次总结）
            max_steps = 3 + batch_size_actual * 2  # 预留足够步骤
            try:
                llm_resp = await self.context.tool_loop_agent(
                    event=event,
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    tools=ToolSet([search_tool]),
                    max_steps=max_steps,
                    tool_call_timeout=30,
                )
                if not llm_resp or not llm_resp.completion_text:
                    logger.error(f"第 {i//batch_size + 1} 批 AI 返回为空")
                    fail_count += batch_size_actual
                    continue

                # 解析返回结果（期望每行 "序号: 中文名" 或 "序号: null"）
                response_text = llm_resp.completion_text.strip()
                lines = response_text.split("\n")
                parsed = {}
                for line in lines:
                    if ":" not in line:
                        continue
                    parts = line.split(":", 1)
                    try:
                        idx = int(parts[0].strip())
                        chn_name = parts[1].strip()
                        if chn_name and 1 <= idx <= batch_size_actual:
                            # 只有非 null 的值才记录
                            if chn_name.lower() != "null":
                                parsed[batch[idx - 1][1]] = chn_name
                    except ValueError:
                        continue

                # 更新数据库
                for internal_id, name in batch:
                    chn_name = parsed.get(name)
                    if chn_name:
                        await self.db.update_game_info(
                            internal_id, chinese_name=chn_name
                        )
                        logger.info(f"成功获取中文名: {name} -> {chn_name}")
                        success_count += 1
                    else:
                        logger.warning(
                            f"未能获取游戏 {name} 的中文名（返回 null 或未识别）"
                        )
                        fail_count += 1

            except asyncio.CancelledError:
                logger.info(f"第 {i//batch_size + 1} 批处理被取消")
                fail_count += batch_size_actual
                raise  # 向上传递取消信号
            except Exception as e:
                logger.error(f"第 {i//batch_size + 1} 批处理异常: {e}", exc_info=True)
                fail_count += batch_size_actual
                continue

            # 批次间延迟，避免 API 过载
            await asyncio.sleep(1)

        return success_count, fail_count

    # -------------------- 定时任务 --------------------
    async def _init_scheduler(self):
        await self.scheduler.start()
        # 基础信息每日更新，默认为对应时区每天02:00，默认时区为北京时间
        basic_cron = self.config.get("ns_basic_info_cron", "0 2 * * *")
        await self.scheduler.add_task(
            "ns_basic_info", basic_cron, self._scheduled_ns_basic_crawl
        )

        # 折扣信息每日更新，默认为对应时区每天23:01，默认时区为北京时间
        # 北京时间23:01 => 日本时间00:01
        sales_cron = self.config.get("ns_sales_info_cron", "1 23 * * *")
        await self.scheduler.add_task(
            "ns_sales_info", sales_cron, self._scheduled_ns_sales_crawl
        )

    async def _scheduled_ns_basic_crawl(self):
        """定时全量基础信息爬取（增量模式，先 ns2 后 ns）"""
        platforms = [("ns2", "BEE"), ("ns", "HAC")]
        for platform_name, platform_code in platforms:
            logger.info(f"定时任务开始爬取 {platform_name} 基础信息（增量模式）")
            result = await self._run_task(
                self._crawl_ns_basic_info, platform_code, incremental=True
            )
            logger.info(f"定时任务任天堂游戏基础信息爬取结果: {result}")
            # 在两个平台之间增加延迟，避免 API 压力
            await asyncio.sleep(60)

    async def _scheduled_ns_sales_crawl(self):
        """定时折扣信息爬取（先 ns2 后 ns）"""
        platforms = [("ns2", "BEE"), ("ns", "HAC")]
        for platform_name, platform_code in platforms:
            logger.info(f"定时任务开始爬取 {platform_name} 折扣信息")
            result = await self._run_task(self._crawl_ns_sales_info, platform_code)
            logger.info(f"定时任务任天堂游戏折扣信息爬取结果: {result}")
            await asyncio.sleep(60)

    # -------------------- 指令 --------------------
    @filter.command("getdata")
    async def getdata(
        self, event: AstrMessageEvent, platform: str = None, mode: str = None
    ):
        """
        手动触发任天堂游戏基础信息爬取，默认增量爬取 NS2 平台游戏
        """
        allowed, reason = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限: {reason}")
            return

        # 平台映射
        platform_map = {"ns": "HAC", "ns2": "BEE"}
        if platform is None:
            platform = "ns2"
        else:
            if platform not in platform_map:
                yield event.plain_result("❌ 无效平台，请选择: ns, ns2")
                return
        platform_code = platform_map[platform]

        # 检查数据库爬取状态
        allowed, blocking = await self.db.is_crawling_allowed(platform_code)
        if not allowed:
            yield event.plain_result(f"⚠️ 平台 {blocking} 正在爬取，请勿重复启动")
            return

        # 模式处理
        if mode is None:
            incremental = True
        elif mode == "inc":
            incremental = True
        elif mode == "all":
            incremental = False
        else:
            yield event.plain_result("❌ 无效模式，请选择: inc（增量）或 all（全量）")
            return

        # 全量模式下重置爬取状态
        if not incremental:
            await self.db.reset_crawler_state("sorting-release-date", platform_code)

        try:
            result = await self._run_task(
                self._crawl_ns_basic_info, platform_code, incremental
            )
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"爬取任务异常: {e}")
            yield event.plain_result(f"❌ 爬取异常: {e}")

    @filter.command("getsaledata")
    async def getsaledata(self, event: AstrMessageEvent, platform: str = None):
        """
        手动触发任天堂游戏折扣信息增量爬取，默认爬取 NS2 平台
        """
        allowed, reason = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限: {reason}")
            return

        platform_map = {"ns": "HAC", "ns2": "BEE"}
        if platform is None:
            platform = "ns2"
        else:
            if platform not in platform_map:
                yield event.plain_result("❌ 无效平台，请选择: ns, ns2")
                return
        platform_code = platform_map[platform]

        # 检查数据库爬取状态
        allowed, blocking = await self.db.is_crawling_allowed(platform_code)
        if not allowed:
            yield event.plain_result(f"⚠️ 平台 {blocking} 正在爬取，请勿重复启动")
            return

        try:
            result = await self._run_task(self._crawl_ns_sales_info, platform_code)
            yield event.plain_result(result)
        except Exception as e:
            logger.error(f"爬取任务异常: {e}")
            yield event.plain_result(f"❌ 爬取异常: {e}")

    @filter.command("remindhelp")
    async def remind_help(self, event: AstrMessageEvent):
        """显示帮助"""
        help_text = self.config.get("help_text", "请查看插件文档或联系管理员。")
        yield event.plain_result(f"📢 折扣提醒帮助\n{help_text}")

    @filter.command("remindme")
    async def remind_me(self, event: AstrMessageEvent, game_name: str):
        """模糊查询游戏（使用下划线替代空格）"""
        allowed, reason = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限: {reason}")
            return

        search_name = game_name.replace("_", " ").strip()
        if not search_name:
            yield event.plain_result("❌ 请提供游戏名")
            return

        # 第一层：LIKE 多关键词匹配（汉字拆2-4子串，单词长度≥3保留完整词）
        def is_cjk(ch: str) -> bool:
            return "\u4e00" <= ch <= "\u9fff"

        keywords = set()
        segments = []
        current = []
        for ch in search_name:
            if is_cjk(ch) or ch.isalnum() or ch == "_":
                current.append(ch)
            else:
                if current:
                    segments.append("".join(current))
                    current = []
        if current:
            segments.append("".join(current))

        for seg in segments:
            if all(is_cjk(c) for c in seg):
                n = len(seg)
                for length in range(2, min(5, n + 1)):
                    for i in range(n - length + 1):
                        keywords.add(seg[i : i + length])
            else:
                if len(seg) >= 3:
                    keywords.add(seg)

        if not keywords:
            keywords.add(search_name)

        # 构建 SQL 查询（不限制数量）
        conditions = []
        params = []
        for kw in keywords:
            conditions.append("(name LIKE ? OR chinese_name LIKE ?)")
            params.extend([f"%{kw}%", f"%{kw}%"])

        sql = f"""
            SELECT internal_id, name, chinese_name
            FROM ns_game_info
            WHERE is_active = 1 AND ({' OR '.join(conditions)})
        """

        async with aiosqlite.connect(str(self.db.db_path)) as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()

        if rows:
            # 计算每个游戏匹配到的最大关键词长度
            scored = []
            for row in rows:
                internal_id, name, chn_name = row
                max_len = 0
                # 检查 name 和 chinese_name 中包含的所有关键词，取最大长度
                for kw in keywords:
                    kw_len = len(kw)
                    if kw_len > max_len:
                        if (name and kw in name) or (chn_name and kw in chn_name):
                            max_len = kw_len
                scored.append((max_len, internal_id, name, chn_name))
            # 按最大长度降序排序，长度相同按 internal_id 升序
            scored.sort(key=lambda x: (-x[0], x[1]))
            # 取前 10 条
            top_rows = [
                (internal_id, name, chn_name)
                for (_, internal_id, name, chn_name) in scored[:10]
            ]

            self.user_search_results[event.unified_msg_origin] = {
                "type": "fuzzy_search",
                "results": top_rows,
                "page": 0,
                "page_size": 3,
                "timestamp": datetime.now(),
                "layer": 1,
                "original_input": search_name,
            }
            await self._send_search_result(event, 0)
            return

        # 第一层无结果，直接进入第二层
        await self._second_layer_search(event, search_name)

    async def _second_layer_search(self, event: AstrMessageEvent, search_name: str):
        """第二层：片段匹配（拼音/罗马字 + 编辑距离）"""
        logger.info(f"[remindme] 进入第二层片段匹配搜索: {search_name}")

        # ---------- 辅助函数 ----------
        def is_cjk(ch: str) -> bool:
            return "\u4e00" <= ch <= "\u9fff"

        def get_pieces(text: str) -> list:
            """将文本按第一层规则拆分为片段（汉字拆2-4子串，单词保留整词）"""
            if not text:
                return []
            pieces = set()
            # 分段
            segments = []
            current = []
            for ch in text:
                if is_cjk(ch) or ch.isalnum() or ch == "_":
                    current.append(ch)
                else:
                    if current:
                        segments.append("".join(current))
                        current = []
            if current:
                segments.append("".join(current))

            for seg in segments:
                if all(is_cjk(c) for c in seg):
                    n = len(seg)
                    for length in range(2, min(5, n + 1)):
                        for i in range(n - length + 1):
                            pieces.add(seg[i : i + length])
                else:
                    if len(seg) >= 3:
                        pieces.add(seg)
            return list(pieces)

        def remove_suffix(text: str) -> str:
            return re.sub(r"(?i)\s*Nintendo Switch 2 Edition\s*$", "", text)

        def to_ascii(text: str, for_name: bool) -> str:
            """文本转 ASCII 字母（日文→罗马字，中文→拼音）"""
            if not text:
                return ""
            text = remove_suffix(text)
            has_kana = any("\u3040" <= ch <= "\u30ff" for ch in text)
            if has_kana:
                kks = pykakasi.kakasi()
                result = "".join(item["hepburn"] for item in kks.convert(text))
            else:
                result = "".join(
                    item[0].lower() for item in pinyin(text, style=Style.NORMAL)
                )
            return "".join(c for c in result if c.isalpha() and ord(c) < 128)

        # ---------- 准备 ----------
        user_ascii = to_ascii(search_name, for_name=False)
        len_user = len(user_ascii)
        tolerance = max(3, len_user // 2)
        logger.info(
            f"[remindme] 用户输入: {search_name} -> ASCII: {user_ascii}, 长度: {len_user}, 宽容度: {tolerance}"
        )

        # 获取所有活跃游戏（限制数量）
        async with aiosqlite.connect(str(self.db.db_path)) as conn:
            cursor = await conn.execute(
                "SELECT internal_id, name, chinese_name FROM ns_game_info WHERE is_active = 1 LIMIT 2000"
            )
            all_games = await cursor.fetchall()
        logger.info(f"[remindme] 共获取 {len(all_games)} 个活跃游戏")

        # ---------- 计算匹配 ----------
        def calc_matches():
            conn = sqlean.connect(":memory:")
            try:
                # 创建临时表存储候选片段
                conn.execute("CREATE TEMP TABLE pieces(id INTEGER, ascii TEXT)")
                game_piece_count = 0
                for internal_id, name, chn_name in all_games:
                    # 处理 name
                    name_pieces = get_pieces(name or "")
                    for piece in name_pieces:
                        ascii_piece = to_ascii(piece, for_name=True)
                        if ascii_piece:
                            conn.execute(
                                "INSERT INTO pieces VALUES (?, ?)",
                                (internal_id, ascii_piece),
                            )
                    # 处理 chinese_name
                    chn_pieces = get_pieces(chn_name or "")
                    for piece in chn_pieces:
                        ascii_piece = to_ascii(piece, for_name=False)
                        if ascii_piece:
                            conn.execute(
                                "INSERT INTO pieces VALUES (?, ?)",
                                (internal_id, ascii_piece),
                            )
                            game_piece_count += 1
                logger.info(f"[remindme] 共生成 {game_piece_count} 个片段（含重复）")

                # 计算每个游戏的最佳匹配（最小编辑距离）
                # 先获取所有不同的游戏ID
                cur = conn.execute("SELECT DISTINCT id FROM pieces")
                game_ids = [row[0] for row in cur.fetchall()]
                matches = []
                for gid in game_ids:
                    # 查询该游戏所有片段的 ASCII 及其与用户输入的编辑距离
                    cur = conn.execute("SELECT ascii FROM pieces WHERE id = ?", (gid,))
                    pieces_ascii = [row[0] for row in cur.fetchall()]
                    min_dist = None
                    for ascii_str in pieces_ascii:
                        dist = conn.execute(
                            "SELECT levenshtein(?, ?)", (ascii_str, user_ascii)
                        ).fetchone()[0]
                        if min_dist is None or dist < min_dist:
                            min_dist = dist
                    if min_dist is not None and min_dist <= tolerance:
                        matches.append((gid, min_dist))

                # 按距离排序，取前20
                if matches:
                    logger.info(f"[remindme] 匹配到 {len(matches)} 个游戏，前20条距离:")
                    for i, (gid, dist) in enumerate(matches[:20], 1):
                        logger.info(f"  {i}. id={gid}, dist={dist}")
                else:
                    logger.info("[remindme] 未匹配到任何游戏")
                matched_ids = [gid for gid, _ in matches[:20]]
                return matched_ids
            finally:
                conn.close()

        try:
            matched_ids = await asyncio.to_thread(calc_matches)
        except Exception as e:
            logger.error(f"片段匹配失败: {e}", exc_info=True)
            await event.send(event.plain_result("❌ 拼音搜索失败，请稍后重试"))
            return

        if not matched_ids:
            await event.send(
                event.plain_result(f"❌ 未找到与「{search_name}」相关的游戏")
            )
            return

        # 获取匹配的完整游戏信息
        async with aiosqlite.connect(str(self.db.db_path)) as conn:
            placeholders = ",".join("?" for _ in matched_ids)
            cursor = await conn.execute(
                f"SELECT internal_id, name, chinese_name FROM ns_game_info WHERE internal_id IN ({placeholders})",
                matched_ids,
            )
            rows = await cursor.fetchall()

        logger.info(f"[remindme] 最终返回 {len(rows)} 个匹配游戏")
        # 存储结果
        self.user_search_results[event.unified_msg_origin] = {
            "type": "fuzzy_search",
            "results": rows,
            "page": 0,
            "page_size": 3,
            "timestamp": datetime.now(),
            "layer": 2,
            "original_input": search_name,
        }
        await self._send_search_result(event, 0)

    async def _send_search_result(self, event: AstrMessageEvent, page: int):
        """发送当前页的搜索结果（内部方法）"""
        user_key = event.unified_msg_origin
        stored = self.user_search_results.get(user_key)
        if not stored or stored.get("type") != "fuzzy_search":
            return
        results = stored["results"]
        page_size = stored["page_size"]
        total = len(results)
        start = page * page_size
        end = start + page_size
        page_results = results[start:end]

        if not page_results:
            await event.send(event.plain_result("❌ 没有更多结果了"))
            return

        lines = [
            f"🔍 找到 {total} 个相关游戏（第 {page+1}/{((total-1)//page_size)+1} 页）："
        ]
        for idx, (_, name, chn_name) in enumerate(page_results, start + 1):
            # 显示格式：中文名（原名）
            if chn_name:
                display = f"{chn_name}（{name}）"
            else:
                display = name
            lines.append(f"{idx}. {display}")

        if total > page_size:
            lines.append("\n使用 confirm np 查看下一页，confirm pp 查看上一页")
        if stored.get("layer") == 1:
            lines.append("若未找到，可使用 confirm 0 进行更精确的拼音搜索")
        lines.append("使用 confirm [序号] 确认要添加的游戏（默认序号1）")
        await event.send(event.plain_result("\n".join(lines)))

    @filter.command("confirm")
    async def confirm(self, event: AstrMessageEvent, arg=None):
        """确认从 remindme 查询结果中选择的游戏"""
        allowed, reason = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限: {reason}")
            return

        user_key = event.unified_msg_origin
        if user_key not in self.user_search_results:
            await event.send(
                event.plain_result("❌ 没有待确认的搜索结果，请先使用 remindme 查询")
            )
            return

        stored = self.user_search_results[user_key]
        if stored.get("type") != "fuzzy_search":
            await event.send(
                event.plain_result(
                    "❌ 当前待确认内容不是游戏查询结果，请重新使用 remindme"
                )
            )
            return

        if (datetime.now() - stored["timestamp"]).total_seconds() > 600:
            del self.user_search_results[user_key]
            await event.send(event.plain_result("❌ 搜索结果已过期，请重新查询"))
            return

        arg_str = str(arg) if arg is not None else None

        if arg_str == "np":
            new_page = stored["page"] + 1
            if new_page * stored["page_size"] >= len(stored["results"]):
                await event.send(event.plain_result("❌ 已经是最后一页"))
                return
            stored["page"] = new_page
            await self._send_search_result(event, new_page)
            return

        if arg_str == "pp":
            new_page = stored["page"] - 1
            if new_page < 0:
                await event.send(event.plain_result("❌ 已经是第一页"))
                return
            stored["page"] = new_page
            await self._send_search_result(event, new_page)
            return

        if arg_str == "0":
            if stored.get("layer") != 1:
                await event.send(
                    event.plain_result("❌ 当前不是第一层搜索结果，无法进入二层搜索")
                )
                return
            original = stored.get("original_input")
            if not original:
                await event.send(
                    event.plain_result(
                        "❌ 无法获取原始搜索词，请重新使用 remindme 搜索"
                    )
                )
                return
            del self.user_search_results[user_key]
            await self._second_layer_search(event, original)
            return

        if arg_str is None:
            index = 1
        else:
            try:
                index = int(arg_str.strip())
            except ValueError:
                await event.send(event.plain_result("❌ 请输入数字序号、np、pp 或 0"))
                return

        results = stored["results"]
        if index < 1 or index > len(results):
            await event.send(
                event.plain_result(f"❌ 序号无效，请输入 1-{len(results)}")
            )
            return

        selected = results[index - 1]
        internal_id, name, chn_name = selected

        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        if not group_id:
            await event.send(event.plain_result("❌ 仅支持群聊添加愿望单"))
            return

        added = await self.db.add_to_wishlist(internal_id, group_id, user_id)
        if added:
            now_utc = utc_now_str()
            is_active = await self.db.is_sale_active(internal_id, now_utc)
            if is_active:
                discount_info = await self.db.get_discount_details(internal_id, now_utc)
                if discount_info:
                    discount_price, sale_label, end_time_utc = discount_info
                    end_local = convert_to_config_tz(end_time_utc)
                    end_time_local = end_local.strftime("%Y-%m-%d %H:%M")
                    msg = f"✅ 已将「{name}」添加到您的愿望单\n🎉 当前游戏正在促销！促销价格：{discount_price}日元，促销力度：{sale_label}\n截止时间：{end_time_local}"
                else:
                    msg = f"✅ 已将「{name}」添加到您的愿望单\n🎉 当前游戏正在促销！"
            else:
                msg = f"✅ 已将「{name}」添加到您的愿望单\n📢 当前游戏未在促销列表中"
            await event.send(event.plain_result(msg))
        else:
            await event.send(event.plain_result(f"⚠️ 您已关注过「{name}」"))

        del self.user_search_results[user_key]

    @filter.command("remindlist")
    async def remind_list(self, event: AstrMessageEvent):
        """查看当前会话的愿望单"""
        allowed, _ = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限")
            return

        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        if not group_id:
            await event.send(event.plain_result("❌ 仅支持群聊查看愿望单"))
            return

        wishlist = await self.db.get_user_wishlist(group_id, user_id)
        if not wishlist:
            await event.send(event.plain_result("📭 您的愿望单暂无游戏"))
            return

        lines = ["📋 您的愿望单："]
        for item in wishlist:
            display = item["name"]
            if item["chinese_name"]:
                display += f"（{item['chinese_name']}）"
            lines.append(f"• {display}")
        await event.send(event.plain_result("\n".join(lines)))

    @filter.command("remindnow")
    async def remind_now(self, event: AstrMessageEvent):
        allowed, _ = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限")
            return

        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        if not group_id:
            await event.send(event.plain_result("❌ 仅支持群聊使用此指令"))
            return

        wishlist = await self.db.get_user_wishlist(group_id, user_id)
        if not wishlist:
            await event.send(event.plain_result("📭 您的愿望单暂无游戏"))
            return

        # 生成标题图片（固定宽度 300px，使用 f-string 嵌入 logo_base64）
        title_file_name = "ns_remind_title.png"
        title_img_path = self.image_gen.img_dir / title_file_name
        if not title_img_path.exists():
            logo_path = self.data_root / "logo" / "ns.png"
            logo_base64 = ""
            if logo_path.exists():
                logo_base64 = ImageGenerator.get_image_base64(str(logo_path))
            title_html = f"""
            <div style="width: 300px; font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; border-radius: 12px; padding: 12px 20px;">
                <div style="text-align: center;">
                    <img src="{logo_base64}" width="32" height="32" style="vertical-align: middle;">
                    <span style="font-size: 18px; font-weight: bold; margin-left: 8px; color: #1e466e;">愿望单折扣提醒</span>
                </div>
            </div>
            """
            title_img_path = await self.image_gen.generate_image(
                html=title_html,
                output_name=title_file_name,
                jinja2_data=None,
                clip_selector="div",
                wait_selector="div",
            )
            if not title_img_path:
                logger.error("生成标题图片失败")
        else:
            logger.info(f"复用标题图片: {title_file_name}")

        components = []
        if title_img_path and title_img_path.exists():
            components.append(Comp.Image.fromFileSystem(str(title_img_path)))

        now_utc = utc_now_str()
        updated_cache = []

        for item in wishlist:
            internal_id = item["internal_id"]
            ns_id = item["ns_id"]
            name = item["name"]
            chn_name = item["chinese_name"]
            display_name = chn_name if chn_name else name

            is_active = await self.db.is_sale_active(internal_id, now_utc)
            if not is_active:
                continue

            discount_info = await self.db.get_discount_details(internal_id, now_utc)
            if not discount_info:
                continue

            discount_price, sale_label, end_time_utc = discount_info
            end_local = convert_to_config_tz(end_time_utc)
            end_time_local = end_local.strftime("%Y-%m-%d %H:%M")

            # 游戏图片 HTML（f-string）
            html = f"""
            <div style="display: inline-block; font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; border-radius: 12px; padding: 8px;">
                <div style="font-size: 14px; font-weight: bold; color: #2c3e50; text-align: center; margin-bottom: 6px;">🎮 {display_name}</div>
                <div style="display: flex; justify-content: space-between; align-items: baseline; background: white; padding: 4px 8px; border-radius: 6px;">
                    <span style="color: #7f8c8d; font-size: 11px;">{end_time_local}截止</span>
                    <div>
                        <span style="color: #27ae60; font-weight: 500; font-size: 12px;">{discount_price} 日元</span>
                        <span style="background: #e74c3c; color: white; padding: 2px 5px; border-radius: 10px; font-size: 10px; margin-left: 6px;">{sale_label}</span>
                    </div>
                </div>
                <div style="font-size: 9px; color: #95a5a6; text-align: center; margin-top: 4px;">折扣截止时间以配置时区为准</div>
            </div>
            """

            file_name = f"{ns_id}_saleinfo.png"
            data_version = end_time_utc

            cached_version = await self.db.get_cached_image(file_name)
            img_path = self.image_gen.img_dir / file_name
            if cached_version == data_version and img_path.exists():
                logger.info(f"命中缓存: {file_name}")
                components.append(Comp.Image.fromFileSystem(str(img_path)))
                continue

            logger.info(
                f"生成图片: {file_name} (旧版本 {cached_version} -> 新版本 {data_version})"
            )
            img_path = await self.image_gen.generate_image(
                html=html,
                output_name=file_name,
                jinja2_data=None,
                clip_selector="div",
                wait_selector="div",
            )
            if img_path:
                components.append(Comp.Image.fromFileSystem(str(img_path)))
                updated_cache.append((file_name, data_version))
            else:
                logger.error(f"生成图片失败: {file_name}")

        for file_name, data_version in updated_cache:
            await self.db.save_cached_image(file_name, data_version)

        if not components:
            await event.send(event.plain_result("🎉 您的愿望单中暂无游戏正在打折"))
            return

        await event.send(event.chain_result(components))

    @filter.command("releaselist")
    async def releaselist(
        self,
        event: AstrMessageEvent,
        month_str: Optional[str] = None,
        year_str: Optional[str] = None,
    ):
        """获取指定月份游戏发售列表（月份在前，年份可选），按原始游戏名去重，合并平台，按发售日+发行商排序"""
        allowed, reason = await self.permission_mgr.check_permission(event)
        if not allowed:
            logger.info(f"❌ 无权限: {reason}")
            return

        now = datetime.now()
        current_year = now.year
        current_month = now.month

        if month_str is None:
            target_year = current_year
            target_month = current_month
        elif year_str is None:
            try:
                target_month = int(month_str)
            except ValueError:
                await event.send(event.plain_result("❌ 月份必须是数字"))
                return
            target_year = current_year
        else:
            try:
                target_month = int(month_str)
                target_year = int(year_str)
            except ValueError:
                await event.send(event.plain_result("❌ 月份和年份必须是数字"))
                return

        if target_month < 1 or target_month > 12:
            await event.send(event.plain_result("❌ 月份必须在 1-12 之间"))
            return
        if target_year < current_year:
            await event.send(event.plain_result(f"❌ 年份不能早于 {current_year}"))
            return
        if target_year == current_year and target_month < current_month:
            await event.send(
                event.plain_result(
                    f"❌ 不能查询过去的月份（当前为 {current_year}年{current_month}月）"
                )
            )
            return

        # 查询数据库：增加 releaseDate 和 manufacturer_name 用于排序
        async with aiosqlite.connect(str(self.db.db_path)) as conn:
            cursor = await conn.execute(
                "SELECT name, chinese_name, releaseDateText, platform, changetime, releaseDate, manufacturer_name FROM ns_game_info WHERE is_active = 1 AND hasReleased = 0 AND releaseDateText IS NOT NULL AND releaseDateText != ''"
            )
            rows = await cursor.fetchall()

        games_dict = {}
        total_rows = 0
        no_year = 0
        year_mismatch = 0
        no_month = 0
        month_mismatch = 0
        matched = 0

        for row in rows:
            total_rows += 1
            (
                name,
                chn_name,
                text,
                platform,
                changetime_str,
                release_date,
                manufacturer,
            ) = row
            year_match = re.search(r"(\d{4})年", text)
            if not year_match:
                no_year += 1
                continue
            game_year = int(year_match.group(1))
            if game_year != target_year:
                year_mismatch += 1
                continue
            month_match = re.search(r"(\d{1,2})月", text)
            if not month_match:
                no_month += 1
                continue
            game_month = int(month_match.group(1))
            if game_month != target_month:
                month_mismatch += 1
                continue
            matched += 1

            # 基本去重：直接使用原始游戏名作为键
            norm_name = name.strip()
            display_name = chn_name if chn_name else norm_name

            if norm_name not in games_dict:
                games_dict[norm_name] = {
                    "display_name": display_name,
                    "releaseDateText": text,
                    "releaseDate": release_date,
                    "manufacturer": manufacturer or "",
                    "platforms": set(),
                    "max_changetime": 0,
                }
            games_dict[norm_name]["platforms"].add(platform)
            if changetime_str:
                try:
                    ct = datetime.fromisoformat(changetime_str).timestamp()
                    if ct > games_dict[norm_name]["max_changetime"]:
                        games_dict[norm_name]["max_changetime"] = ct
                except:
                    pass

        logger.info(
            f"releaselist 统计: 总记录 {total_rows}, 无年份 {no_year}, 年份不匹配 {year_mismatch}, 无月份 {no_month}, 月份不匹配 {month_mismatch}, 原始匹配 {matched}, 去重后 {len(games_dict)}"
        )

        if not games_dict:
            await event.send(
                event.plain_result(
                    f"📭 {target_year}年{target_month}月暂无未发售游戏信息"
                )
            )
            return

        # 转换为列表并按 (releaseDate, manufacturer) 排序
        games = []
        max_changetime = 0
        for norm_name, data in games_dict.items():
            platform_str = "/".join(sorted(data["platforms"]))
            games.append(
                {
                    "name": data["display_name"],
                    "releaseDateText": data["releaseDateText"],
                    "releaseDate": data["releaseDate"],
                    "manufacturer": data["manufacturer"],
                    "platform": platform_str,
                    "max_changetime": data["max_changetime"],
                }
            )
            if data["max_changetime"] > max_changetime:
                max_changetime = data["max_changetime"]
        games.sort(key=lambda x: (x["releaseDate"], x["manufacturer"]))

        # 生成图片（使用裁剪容器的方式）
        data_version = str(max_changetime) if max_changetime else "0"
        file_name = f"releaselist_{target_year}_{target_month}.png"

        cached_version = await self.db.get_cached_image(file_name)
        img_path = self.image_gen.img_dir / file_name
        if cached_version == data_version and img_path.exists():
            logger.info(f"命中缓存: {file_name}")
            await event.send(event.image_result(str(img_path)))
            return

        logger.info(
            f"重新生成图片: {file_name} (旧版本 {cached_version} -> 新版本 {data_version})"
        )

        logo_path = self.data_root / "logo" / "ns.png"
        logo_base64 = ""
        if logo_path.exists():
            logo_base64 = ImageGenerator.get_image_base64(str(logo_path))

        items_html = ""
        for g in games:
            items_html += f"""
            <div style="display: flex; justify-content: space-between; align-items: baseline; background: white; padding: 4px 8px; border-radius: 6px;">
                <div style="flex: 2;">
                    <span style="font-weight: 600; color: #2c3e50; font-size: 12px; white-space: nowrap;">{g['name']}</span>
                    <span style="color: #7f8c8d; font-size: 10px; margin-left: 8px;">{g['platform']}</span>
                </div>
                <span style="color: #7f8c8d; font-size: 11px; margin-left: 12px; white-space: nowrap;">{g['releaseDateText']}</span>
            </div>
            """

        html = f"""
        <div class="release-container" style="display: inline-block; font-family: 'Segoe UI', system-ui, sans-serif; background: #f0f2f5; border-radius: 12px; padding: 8px;">
            <div style="text-align: center; margin-bottom: 8px;">
                <img src="{logo_base64}" width="32" height="32" style="vertical-align: middle;">
                <span style="font-size: 18px; font-weight: bold; margin-left: 8px; color: #1e466e;">{target_year}年{target_month}月 游戏发售列表（共{len(games)}款游戏）</span>
            </div>
            <div style="display: flex; flex-direction: column; gap: 4px;">
                {items_html}
            </div>
        </div>
        """

        img_path = await self.image_gen.generate_image(
            html=html,
            output_name=file_name,
            jinja2_data=None,
            clip_selector=".release-container",
            wait_selector=".release-container",
            full_page=False,
        )
        if img_path:
            await self.db.save_cached_image(file_name, data_version)
            await event.send(event.image_result(str(img_path)))
        else:
            lines = [f"🎮 {target_year}年{target_month}月 游戏发售列表："]
            for g in games:
                lines.append(
                    f"• {g['name']} ({g['platform']}) - {g['releaseDateText']}"
                )
            await event.send(event.plain_result("\n".join(lines)))

    # -------------------- 占位方法 --------------------
    async def _check_session_discounts(self, session_id: str):
        pass

    async def terminate(self):
        # 取消所有正在运行的任务
        if self._running_tasks:
            logger.info(f"正在取消 {len(self._running_tasks)} 个运行中的任务...")
            for task in self._running_tasks:
                if not task.done():
                    task.cancel()
            # 等待所有任务取消完成
            await asyncio.gather(*self._running_tasks, return_exceptions=True)
            self._running_tasks.clear()

        await self.scheduler.stop()
        await self.image_gen.close()
        await super().terminate()
