"""
图片生成器模块
"""

import asyncio
import base64
from pathlib import Path
from typing import Optional, Callable

import aiohttp
from jinja2 import Template
from playwright.async_api import async_playwright, Browser, Playwright

from astrbot.api import logger


class ImageGenerator:
    """
    基于 Playwright 的 HTML 转图片生成器，支持优先使用 AstrBot 远程渲染接口。
    远程渲染失败后自动回退到本地 Playwright，并在本次运行期间不再尝试远程。
    """

    def __init__(
        self,
        data_root: Path,
        render_func: Optional[Callable] = None,
        img_subdir: str = "images",
    ):
        """
        :param data_root: 数据根目录
        :param render_func: AstrBot 的渲染函数，应为 Star.html_render 方法。
                            签名: async def render_func(html: str, data: dict = None, options: dict = None) -> str
                            返回图片 URL。
        """
        self.data_root = Path(data_root)
        self.img_dir = self.data_root / img_subdir
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.render_func = render_func  # AstrBot 渲染函数
        self._remote_failed = False  # 标记远程服务是否已失败

        # 本地浏览器相关
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None

    async def _ensure_browser(self):
        """确保本地浏览器实例存在"""
        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
        return self.browser

    async def generate_image(
        self,
        html: str,
        output_name: str,
        clip_selector: str = ".card",
        wait_selector: Optional[str] = None,
        jinja2_data: Optional[dict] = None,
        render_options: Optional[dict] = None,
        full_page: bool = False,
        **kwargs,
    ) -> Optional[Path]:
        """
        生成图片，优先使用远程渲染（如果可用），失败则回退到本地 Playwright。

        :param html: HTML 模板字符串（支持 Jinja2 语法）
        :param output_name: 输出文件名
        :param clip_selector: 要裁剪的元素选择器，默认 ".card"
        :param wait_selector: 等待元素选择器（如果与 clip 不同），默认使用 clip_selector
        :param jinja2_data: 传递给 Jinja2 模板的数据字典
        :param render_options: 渲染选项（参考 Playwright screenshot API）
        :param full_page: 是否截取整个页面（True 时忽略 clip_selector）
        :return: 图片路径，失败返回 None
        """
        output_path = self.img_dir / output_name

        # 尝试远程渲染（如果 render_func 可用且未标记失败）
        if self.render_func and not self._remote_failed:
            try:
                url = await self.render_func(
                    html, jinja2_data or {}, render_options or {}
                )
                if url:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                with open(output_path, "wb") as f:
                                    f.write(await resp.read())
                                logger.info(f"远程图片生成成功: {output_name}")
                                return output_path
            except Exception as e:
                logger.warning(f"远程图片生成失败，将回退到本地渲染: {e}")
                self._remote_failed = True

        # 本地渲染
        try:
            if jinja2_data:
                template = Template(html)
                rendered_html = template.render(**jinja2_data)
            else:
                rendered_html = html

            browser = await self._ensure_browser()
            page = await browser.new_page()

            await page.set_viewport_size({"width": 1920, "height": 10800})
            await page.set_content(rendered_html, wait_until="networkidle")

            if full_page:
                content_width = await page.evaluate("() => document.body.scrollWidth")
                await page.set_viewport_size({"width": content_width, "height": 1080})
                await page.screenshot(path=str(output_path), type="png", full_page=True)
            else:
                selector = wait_selector or clip_selector
                element = await page.wait_for_selector(selector, state="attached")
                box = await element.bounding_box()
                if not box:
                    raise Exception(f"无法获取元素边界: {selector}")
                await element.scroll_into_view_if_needed()
                box = await element.bounding_box()
                if not box:
                    raise Exception(f"滚动后仍无法获取元素边界: {selector}")

                await page.screenshot(
                    path=str(output_path),
                    clip={
                        "x": box["x"],
                        "y": box["y"],
                        "width": box["width"],
                        "height": box["height"],
                    },
                    type="png",
                )
            await page.close()
            logger.info(f"本地图片生成成功: {output_name}")
            return output_path
        except Exception as e:
            logger.error(f"图片生成失败 {output_name}: {e}")
            return None

    async def close(self):
        """关闭本地浏览器资源"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    @staticmethod
    def get_image_base64(image_path: str) -> str:
        """获取图片的 base64 编码（用于嵌入 HTML）"""
        try:
            with open(image_path, "rb") as f:
                return f"data:image/png;base64,{base64.b64encode(f.read()).decode()}"
        except Exception:
            return ""
