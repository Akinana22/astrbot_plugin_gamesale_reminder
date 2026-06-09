"""
NS credential 获取器
通过 Playwright 模拟浏览器访问任天堂商店商品详情页，
捕获 Authorization 和 x-nintendo-savanna-client-id。
支持重试机制，记录请求但不输出具体 credential。
"""

import asyncio
import json
from pathlib import Path
from typing import Optional, Tuple

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
from astrbot.api import logger


class NSCredentialFetcher:
    def __init__(
        self, headless: bool = True, timeout: int = 60000, max_retries: int = 2
    ):
        self.headless = headless
        self.timeout = timeout
        self.max_retries = max_retries

    async def get_credentials(
        self, output_dir: Path
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        执行自动化流程，返回 (authorization, client_id)，支持重试。
        :param output_dir: 保存请求记录的目录
        """
        for attempt in range(1, self.max_retries + 1):
            logger.info(
                f"=== 开始模拟浏览器获取 credentials (尝试 {attempt}/{self.max_retries}) ==="
            )
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            # 清除旧的请求记录文件（每次重试前清空）
            request_file = output_dir / "request.json"
            if request_file.exists():
                try:
                    request_file.unlink()
                    logger.info("已删除旧的 request.json 文件")
                except Exception as e:
                    logger.warning(f"删除旧文件失败: {e}")

            captured_requests = []
            authorization = None
            client_id = None

            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(
                        headless=self.headless,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    context = await browser.new_context(
                        viewport={"width": 1280, "height": 800},
                        locale="ja-JP",
                        timezone_id="Asia/Tokyo",
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        extra_http_headers={
                            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8"
                        },
                    )
                    page = await context.new_page()

                    def on_request(request):
                        nonlocal authorization, client_id
                        url = request.url
                        headers = request.headers
                        auth = headers.get("authorization")
                        cid = headers.get("x-nintendo-savanna-client-id")
                        # 记录请求信息（不保存具体值）
                        captured_requests.append(
                            {
                                "url": url,
                                "has_authorization": auth is not None,
                                "has_client_id": cid is not None,
                            }
                        )
                        # 捕获目标 credential
                        if (
                            "store-jp.nintendo.com/mobify/proxy/api/product/shopper-products"
                            in url
                            and auth
                        ):
                            logger.info("✅ 捕获到 Authorization")
                            authorization = auth
                        if "wb.lp1.savanna.srv.nintendo.net/graphql" in url and cid:
                            logger.info("✅ 捕获到 client_id")
                            client_id = cid

                    page.on("request", on_request)

                    target_url = (
                        "https://store-jp.nintendo.com/item/software/D70010000000026"
                    )
                    logger.info(f"正在访问: {target_url}")
                    try:
                        response = await page.goto(
                            target_url,
                            wait_until="domcontentloaded",
                            timeout=self.timeout,
                        )
                        logger.info(
                            f"页面响应状态码: {response.status if response else '未知'}"
                        )
                    except PlaywrightTimeoutError:
                        logger.warning("页面加载超时，继续等待请求...")

                    # 等待页面主要内容出现，确保 JS 执行
                    try:
                        await page.wait_for_selector(".product-item", timeout=30000)
                        logger.info("找到商品元素，页面加载完成")
                    except PlaywrightTimeoutError:
                        logger.warning("未找到商品元素，可能页面结构变化")

                    # 额外等待，确保所有请求发出
                    await asyncio.sleep(5)

                    # 保存请求记录（不包含 credential 值）
                    with open(request_file, "w", encoding="utf-8") as f:
                        json.dump(captured_requests, f, ensure_ascii=False, indent=2)
                    logger.info(f"已保存请求记录到 {request_file}")

                    await browser.close()

                    if authorization and client_id:
                        logger.info("成功获取所有 credentials")
                        return authorization, client_id
                    else:
                        logger.warning(
                            f"未捕获到完整 credentials: auth={authorization is not None}, client_id={client_id is not None}"
                        )
                        if attempt < self.max_retries:
                            logger.info("等待 3 秒后重试...")
                            await asyncio.sleep(3)

            except Exception as e:
                logger.error(f"❌ 获取 credentials 异常: {e}", exc_info=True)
                if attempt < self.max_retries:
                    logger.info("等待 3 秒后重试...")
                    await asyncio.sleep(3)

        logger.error("重试次数已用尽，未能获取到有效的 credentials")
        return None, None
